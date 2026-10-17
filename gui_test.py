# -*- coding: utf-8 -*-
"""GUI 全功能自动化测试（在 Windows 桌面环境运行，Tk 窗口会短暂出现）。

覆盖：环境检测/后端切换/自测、zip 导入、检测、全部阶段表单、
参数保存/加载、dry-run 全流程、进度/日志、失败重试、取消、作业历史。

用法: python gui_test.py
"""
from __future__ import annotations

import json
import os
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mdflow import config, envsetup
from mdflow.gui import MDApp, AUTO_LABEL
from mdflow.selftest import make_synthetic_gromacs_dir

RESULTS: list = []


def check(name: str, cond, detail: str = "") -> None:
    RESULTS.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" +
          (f"  ← {detail}" if detail and not cond else ""))


def pump(app, seconds: float = 0.3) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.update()
        time.sleep(0.02)


def wait_until(app, cond, timeout: float = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.update()
        if cond():
            return True
        time.sleep(0.05)
    return False


def tree_status(app):
    return [app.tree_steps.item(it, "values")[1]
            for it in app.tree_steps.get_children()]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="mdwf_guitest_"))
    gdir = make_synthetic_gromacs_dir(tmp, coarse=False)

    print("=" * 62)
    print(" MD Workflow Manager GUI 全功能测试")
    print("=" * 62)

    # 静默弹窗，避免测试挂起
    msgbox_patchers = [
        mock.patch("mdflow.gui.messagebox.showinfo"),
        mock.patch("mdflow.gui.messagebox.showwarning"),
        mock.patch("mdflow.gui.messagebox.showerror"),
        mock.patch("mdflow.gui.messagebox.askyesno", return_value=False),
    ]
    for p in msgbox_patchers:
        p.start()

    app = MDApp()
    pump(app)
    try:
        check("窗口标题", "MD Workflow Manager" in app.title())
        check("四页面", len(app._pages) == 4)
        check("图标已设置", hasattr(app, "_icon_img"))

        # ============ ① 环境配置页 ============
        check("WSL 自动检测",
              wait_until(app, lambda: "WSL" in app.lbl_wsl.cget("text")
                         and "检测中" not in app.lbl_wsl.cget("text"), 20))
        check("gmx 自动检测",
              wait_until(app, lambda: "检测中" not in app.lbl_gmx.cget("text"), 30))
        check("发行版下拉已填充", bool(app.cmb_distro["values"]))
        gmx_ok = app.lbl_gmx.cget("text").startswith("已就绪")

        app.var_backend.set(config.BACKEND_LOCAL)
        app._on_backend_changed()
        check("切换本机后端", app.backend.name == config.BACKEND_LOCAL)
        app.var_backend.set(config.BACKEND_WSL)
        app._on_backend_changed()
        check("切回 WSL 后端", app.backend.name == config.BACKEND_WSL)

        # 环境自测（真实 grompp+mdrun，约 10~30 秒）
        if gmx_ok:
            with mock.patch("mdflow.gui.messagebox.showinfo") as mb:
                app._self_test_env()
                done = wait_until(app, lambda: mb.called, 120)
                check("环境自测执行完成", done)
                if mb.called:
                    msg = mb.call_args[0][1] if len(mb.call_args[0]) > 1 else str(mb.call_args)
                    check("环境自测通过", "通过" in str(msg), str(msg))
        else:
            check("环境自测（本机无 gmx，跳过）", True)

        # ============ ② 输入与检测页 ============
        app.var_input_dir.set(str(gdir))
        app._do_detect()
        pump(app)
        check("力场识别", "charmm36" in app.lbl_ff.cget("text"), app.lbl_ff.cget("text"))
        check("类型自动检测", app.var_sim_type.get() == AUTO_LABEL)
        check("发现 7 阶段", len(app.detect_result.stages) == 7)
        check("默认输出目录自动填充", bool(app.var_output_dir.get().strip()))
        app.cmb_sim_type.set(list(config.SIM_TYPE_NAMES.values())[1])
        check("手动切换粗粒化", app._resolved_sim_type() == config.COARSE_GRAINED)
        app.cmb_sim_type.set(AUTO_LABEL)

        # 压缩包导入（tgz，CHARMM-GUI 实际输出格式）
        tgz_path = tmp / "charmm_out.tgz"
        with tarfile.open(tgz_path, "w:gz") as tf:
            for f in gdir.rglob("*"):
                if f.is_file():
                    tf.add(f, f"download/{f.name}")
        with mock.patch("mdflow.gui.filedialog.askopenfilename",
                        return_value=str(tgz_path)):
            app._import_zip()
        pump(app)
        check("tgz 导入并检测", app.detect_result is not None
              and "charmm36" in app.lbl_ff.cget("text"))
        # zip 导入
        zip_path = tmp / "charmm_out.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in gdir.rglob("*"):
                if f.is_file():
                    zf.write(f, f"download/{f.name}")
        with mock.patch("mdflow.gui.filedialog.askopenfilename",
                        return_value=str(zip_path)):
            app._import_zip()
        pump(app)
        check("zip 导入并检测", app.detect_result is not None
              and "charmm36" in app.lbl_ff.cget("text"))

        # ============ ③ 模拟参数页 ============
        app.var_input_dir.set(str(gdir))
        app._do_detect()
        pump(app)
        all_forms_ok = True
        for i in range(len(app.detect_result.stages)):
            app.cmb_stage.current(i)
            app._on_stage_selected()
            pump(app, 0.03)
            if app._cur_stage_key != app.detect_result.stages[i].key or not app._form:
                all_forms_ok = False
        check("全部阶段表单可构建", all_forms_ok)

        # 修改生产阶段时长 → 保存 → 加载
        last = len(app.detect_result.stages) - 1
        app.cmb_stage.current(last)
        app._on_stage_selected()
        app._form["duration"].set("5")
        app._form["unit"].set("ns")
        app._save_current_form()
        check("生产 5ns 换算为 5000ps", app.stage_cfgs["step6.6"]["duration_ps"] == 5000.0)

        pf = tmp / "params.json"
        with mock.patch("mdflow.gui.filedialog.asksaveasfilename",
                        return_value=str(pf)):
            app._save_params()
        check("参数保存到文件", pf.exists())
        data = json.loads(pf.read_text(encoding="utf-8"))
        check("保存内容含阶段参数", "step6.6" in data["stage_cfgs"])

        app.stage_cfgs = {}
        app._form["duration"].set("1")
        with mock.patch("mdflow.gui.filedialog.askopenfilename",
                        return_value=str(pf)):
            app._load_params()
        pump(app)
        check("参数加载回填 5000ps",
              app.stage_cfgs.get("step6.6", {}).get("duration_ps") == 5000.0)

        # ============ ④ 运行与日志页 ============
        # 未检测时启动被拦截
        app2 = MDApp()
        pump(app2)
        with mock.patch("mdflow.gui.messagebox.showwarning") as w:
            app2._start(False)
            check("未检测时启动被拦截", w.called)
        app2._on_close()

        # 时长校验：<=0 被拦截
        app.stage_cfgs["step6.0"] = {"duration_ps": 0.0}
        with mock.patch("mdflow.gui.messagebox.showwarning") as w:
            app._start(False)
            check("非法时长被拦截", w.called)
        app.stage_cfgs.pop("step6.0", None)

        # dry-run 全流程
        app.var_output_dir.set(str(tmp / "out"))
        app.var_dry.set(True)
        app._start(False)
        check("启动后进入运行态", app._running)
        done = wait_until(app, lambda: not app._running, 60)
        check("dry-run 全流程完成", done)
        check("全部阶段完成标记", all("完成" in v for v in tree_status(app)),
              str(tree_status(app)))
        check("总体进度 100%", app.pb_overall["value"] == 100)
        log_txt = app.log_text.get("1.0", "end")
        check("日志含时间戳与级别",
              "[INFO]" in log_txt and ("[" in log_txt.splitlines()[0][:8]),
              log_txt[:200])
        job = app.workflow.job_dir
        check("run_summary 生成", (job / "run_summary.txt").exists())
        check("md_sim.log 生成", (job / "md_sim.log").exists())
        check("生产目录含 xtc", (job / "07_step6.6" / "md.xtc").exists())
        hist = envsetup.load_history()
        check("作业历史已记录", len(hist) >= 1 and hist[0].get("ok") is True)

        # 失败路径 → 继续按钮可用
        app.var_dry.set(False)
        app.var_gmx.set("definitely_not_a_real_gmx")
        fake_info = {"ok": True, "msg": "fake ok", "version": "2025.4",
                     "version_tuple": (2025, 4), "gpu": False}
        with mock.patch("mdflow.gui.make_backend") as fake_mb, \
             mock.patch("mdflow.envsetup.probe_gmx_info",
                        return_value=fake_info):
            fake_backend = mock.MagicMock()
            fake_backend.display = "测试后端"
            fake_backend.wrap.side_effect = lambda cmd, cwd: [str(x) for x in cmd]
            fake_mb.return_value = fake_backend
            app._start(False)
        done = wait_until(app, lambda: not app._running, 60)
        check("gmx 缺失时阶段失败", done)
        check("失败状态标记", any("失败" in v for v in tree_status(app)),
              str(tree_status(app)))
        check("“从失败处继续”可用", str(app.btn_resume.cget("state")) == "normal")
        app.var_gmx.set(config.GMX_EXE_DEFAULT)

        # 取消路径
        app.var_dry.set(True)
        app.stage_cfgs = {s.key: {"duration_ps": 1e9} for s in app.detect_result.stages}
        app._start(False)
        pump(app, 1.0)
        app._cancel_run()
        done = wait_until(app, lambda: not app._running, 30)
        check("取消后运行结束", done)
        check("取消状态标记", any("取消" in v for v in tree_status(app)),
              str(tree_status(app)))

        # 作业历史弹窗（构造空历史 → 提示）
        with mock.patch("mdflow.gui.envsetup.load_history", return_value=[]), \
             mock.patch("mdflow.gui.messagebox.showinfo") as mb:
            app._show_history()
            check("空历史提示", mb.called)

        print("-" * 62)
        failed = sum(1 for _, ok in RESULTS if not ok)
        print(f"GUI 测试: 共 {len(RESULTS)} 项, 失败 {failed} 项")
        return 1 if failed else 0
    finally:
        for p in msgbox_patchers:
            p.stop()
        try:
            app._on_close()
        except Exception:
            app.destroy()


if __name__ == "__main__":
    sys.exit(main())
