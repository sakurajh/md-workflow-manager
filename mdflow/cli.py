# -*- coding: utf-8 -*-
"""控制台（CLI）版本：为无桌面的 Linux 服务器设计。

与 GUI 共用同一套核心引擎（detect / mdp / workflow / envsetup），
在 SSH 终端 / nohup / tmux 中运行：

    ./MDWorkflowManager-cli <CHARMM-GUI目录或压缩包> -o 输出目录

默认完全按 CHARMM-GUI 原始 mdp 参数运行（与手动在控制台跑一致）；
可通过 --config（GUI 保存的参数 JSON）或 --em-ps/--equil-ps/--prod-ns 覆盖。

退出码: 0=成功, 1=失败, 130=取消
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional

from . import config, detect, envsetup, mdp as mdp_mod, workflow
from .backend import make_backend
from .gmx import fmt_eta
from .logger import MDLogger

IS_WINDOWS = sys.platform == "win32"
NOISE_RE = __import__("re").compile(r"step\s+\d+,|Step=\s*\d+")


def _use_color() -> bool:
    return sys.stdout.isatty() and not IS_WINDOWS or \
        (IS_WINDOWS and sys.stdout.isatty())


def _c(text: str, code: str) -> str:
    if not _use_color():
        return text
    return f"\033[{code}m{text}\033[0m"


class CliApp:
    """CLI 主控。"""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.logger = MDLogger()
        self._last_progress = [0.0]
        self._result: Optional[workflow.JobResult] = None
        self.workflow: Optional[workflow.Workflow] = None

    # ------------------------------------------------------------------
    def _log_to_console(self, level: str, msg: str) -> None:
        """INFO/WARN/ERROR 上屏（DEBUG 只进文件）。"""
        if level == "DEBUG":
            return
        if level == "ERROR":
            print(_c(f"[错误] {msg}", "31"), flush=True)
        elif level == "WARN":
            print(_c(f"[警告] {msg}", "33"), flush=True)
        else:
            print(f"[信息] {msg}", flush=True)

    def _emit(self, ev: dict) -> None:
        t = ev.get("type")
        if t == "stage_start":
            print("", flush=True)
            print(_c(f"=== [{ev['index'] + 1}/{ev['count']}] {ev['name']} ===",
                     "1;36"), flush=True)
        elif t == "progress":
            self._render_progress(ev)
        elif t == "stage_done":
            self._clear_progress_line()
            print(_c(f"    完成", "32"), flush=True)
        elif t == "stage_fail":
            self._clear_progress_line()
            status = "已取消" if ev.get("canceled") else "失败"
            print(_c(f"    {status}", "31" if not ev.get("canceled") else "33"),
                  flush=True)

    def _clear_progress_line(self) -> None:
        if sys.stdout.isatty():
            print("\r" + " " * 100 + "\r", end="", flush=True)

    def _render_progress(self, ev: dict) -> None:
        now = time.time()
        if now - self._last_progress[0] < 0.5 and ev["fraction"] < 1.0:
            return
        self._last_progress[0] = now
        if not sys.stdout.isatty():
            return  # nohup/管道模式：不刷行，靠定期 INFO 日志
        n = 28
        filled = int(round(ev["fraction"] * n))
        bar = "#" * filled + "-" * (n - filled)
        line = (f"    [{bar}] {ev['fraction'] * 100:5.1f}%"
                f"   剩余 {ev['eta']}   已运行 {fmt_eta(ev['elapsed'])}")
        print("\r" + line + "\r" + line, end="", flush=True)

    # ------------------------------------------------------------------
    def _build_stage_configs(self, stages) -> dict:
        """组装每阶段参数覆盖。优先级: 命令行 flags > --config JSON > 默认(不覆盖)。"""
        a = self.args
        cfgs: dict = {}
        if a.config:
            data = json.loads(Path(a.config).read_text(encoding="utf-8"))
            cfgs = {str(k): dict(v) for k, v in data.get("stage_cfgs", {}).items()}

        for s in stages:
            cfg = cfgs.setdefault(s.key, {})
            if s.kind == config.KIND_EM:
                if a.em_ps is not None:
                    cfg["duration_ps"] = a.em_ps
                if a.emtol is not None:
                    cfg["emtol"] = a.emtol
                continue
            # 平衡 / 生产阶段
            if s.kind == config.KIND_EQUIL and a.equil_ps is not None:
                cfg["duration_ps"] = a.equil_ps
            if s.kind == config.KIND_PRODUCTION and a.prod_ns is not None:
                cfg["duration_ps"] = a.prod_ns * 1000.0
            if a.temp is not None:
                cfg["temperature_k"] = a.temp
            if a.pres is not None and self._stage_has_pressure(s):
                cfg["pressure_bar"] = a.pres
            if a.dt is not None:
                cfg["dt_ps"] = a.dt
            if a.restraints is not None and s.kind == config.KIND_EQUIL:
                cfg["restraints"] = a.restraints
            if a.nstxout is not None:
                cfg["nstxout_compressed"] = a.nstxout
            if a.nstenergy is not None:
                cfg["nstenergy"] = a.nstenergy
            if a.tcoupl:
                cfg["tcoupl"] = a.tcoupl
            if a.pcoupl:
                cfg["pcoupl"] = a.pcoupl
        return {k: v for k, v in cfgs.items() if v}

    @staticmethod
    def _stage_has_pressure(stage) -> bool:
        """恒压阶段（生产 / 开了压力耦合的平衡）才可设置压力。"""
        if stage.kind == config.KIND_PRODUCTION:
            return True
        pcoupl = str(mdp_mod.get(stage.parsed, "pcoupl", "")).lower()
        return pcoupl not in ("", "no")

    # ------------------------------------------------------------------
    def _show_params(self, stages, sim_type: str, cfgs: dict) -> None:
        """打印每个阶段的当前参数（默认值 + 覆盖）。"""
        print(_c("当前各阶段参数（未覆盖项 = CHARMM-GUI 原始 mdp 值）", "1;36"))
        for s in stages:
            p = s.parsed
            fb = config.FALLBACK_PARAMS[sim_type]
            parts = [f"[{s.key}] {s.name}"]
            if s.kind == config.KIND_EM:
                emtol = cfgs.get(s.key, {}).get("emtol",
                                                 mdp_mod.to_float(p, "emtol", 1000.0))
                nsteps = mdp_mod.to_int(p, "nsteps", None)
                parts.append(f"nsteps={nsteps if nsteps else '原始'}"
                             f"  emtol={emtol}")
            else:
                cfg = cfgs.get(s.key, {})
                dur = cfg.get("duration_ps", mdp_mod.stage_duration_ps(p))
                parts.append(f"时长={dur:g}ps")
                temp = cfg.get("temperature_k",
                               mdp_mod.first_float(p, "ref_t", float(fb["temperature_k"])))
                parts.append(f"T={temp}K")
                if self._stage_has_pressure(s):
                    pres = cfg.get("pressure_bar",
                                   mdp_mod.first_float(p, "ref_p", float(fb["pressure_bar"])))
                    parts.append(f"P={pres}bar")
                rest = cfg.get("restraints",
                               "-DPOSRES" in str(mdp_mod.get(p, "define", "")).upper())
                if s.kind == config.KIND_EQUIL:
                    parts.append(f"位置限制={'开' if rest else '关'}")
                nstx = cfg.get("nstxout_compressed",
                               mdp_mod.to_int(p, "nstxout-compressed",
                                              int(fb["nstxout_compressed"])))
                parts.append(f"轨迹输出每{nstx}步")
                dt = cfg.get("dt_ps", mdp_mod.to_float(p, "dt", float(fb["dt_ps"])))
                parts.append(f"dt={dt}ps")
            print("  " + "  ".join(parts))
        print("")

    @staticmethod
    def _config_template(stages, sim_type: str) -> dict:
        """生成参数文件模板（各阶段当前默认值 + 键说明）。"""
        stage_cfgs: dict = {}
        for s in stages:
            p = s.parsed
            fb = config.FALLBACK_PARAMS[sim_type]
            c: dict = {}
            if s.kind == config.KIND_EM:
                c["emtol"] = mdp_mod.to_float(p, "emtol", 1000.0)
                # EM 不写 duration_ps = 使用 CHARMM-GUI 原始步数
            else:
                dur = mdp_mod.stage_duration_ps(p)
                if dur > 0:
                    c["duration_ps"] = round(dur, 6)
                c["temperature_k"] = mdp_mod.first_float(
                    p, "ref_t", float(fb["temperature_k"]))
                if CliApp._stage_has_pressure(s):
                    c["pressure_bar"] = mdp_mod.first_float(
                        p, "ref_p", float(fb["pressure_bar"]))
                c["nstxout_compressed"] = mdp_mod.to_int(
                    p, "nstxout-compressed", int(fb["nstxout_compressed"]))
                c["nstenergy"] = mdp_mod.to_int(p, "nstenergy", int(fb["nstenergy"]))
                c["dt_ps"] = mdp_mod.to_float(p, "dt", float(fb["dt_ps"]))
                c["tcoupl"] = mdp_mod.get(p, "tcoupl", "v-rescale")
                if CliApp._stage_has_pressure(s):
                    c["pcoupl"] = mdp_mod.get(p, "pcoupl", "Parrinello-Rahman")
                if s.kind == config.KIND_EQUIL:
                    c["restraints"] = "-DPOSRES" in str(
                        mdp_mod.get(p, "define", "")).upper()
            stage_cfgs[s.key] = c
        return {
            "_help": {
                "duration_ps": "阶段时长(ps)。EM 阶段不写 = 使用 CHARMM-GUI 原始步数",
                "emtol": "最小化力收敛阈值（仅 EM）",
                "temperature_k": "温度 K",
                "pressure_bar": "压力 bar（仅恒压阶段）",
                "restraints": "位置限制 true/false（仅平衡阶段）",
                "nstxout_compressed": "轨迹输出频率（步）",
                "nstenergy": "能量输出频率（步）",
                "dt_ps": "步长(ps)",
                "tcoupl": "恒温器: v-rescale / Nose-Hoover / Berendsen / no",
                "pcoupl": "恒压器: Parrinello-Rahman / C-rescale / Berendsen / no",
                "删除某个键": "表示不覆盖该项，使用 CHARMM-GUI 原始 mdp 值",
            },
            "sim_type": sim_type,
            "stage_cfgs": stage_cfgs,
        }

    def run(self) -> int:
        a = self.args
        if a.restraints is not None:
            a.restraints = {"on": True, "off": False}.get(a.restraints)
        print(_c(f"MD Workflow Manager CLI v{config.APP_VERSION}", "1;36"))

        # 1) 检测输入
        res = detect.detect(a.input)
        if not res.ok:
            print(_c("输入检测失败:\n" + res.error, "31"))
            return 1
        sim_type = a.type or res.sim_type
        if not sim_type:
            print(_c("无法确定模拟类型，请用 --type all_atom 或 coarse_grained 指定。", "31"))
            return 1
        self.logger.add_callback(self._log_to_console)
        stage_cfgs = self._build_stage_configs(res.stages)

        # --show-params：查看各阶段参数后退出
        if a.show_params:
            self._show_params(res.stages, sim_type, stage_cfgs)
            return 0
        # --save-config：导出参数模板（含各阶段默认值与键说明），编辑后 -c 使用
        if a.save_config:
            data = self._config_template(res.stages, sim_type)
            for k, v in stage_cfgs.items():
                data["stage_cfgs"].setdefault(k, {}).update(v)
            Path(a.save_config).write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"参数模板已保存: {a.save_config}")
            print(f"用 vi/nano 编辑后，运行时加: -c {a.save_config}")
            return 0

        print(f"输入: {res.gromacs_dir}")
        print(f"力场: {res.ff_name or '未知'} | 类型: "
              f"{config.SIM_TYPE_NAMES.get(sim_type, sim_type)} | "
              f"阶段数: {len(res.stages)}")
        if res.atoms is not None:
            print(f"原子数: {res.atoms}")
        if res.martini_v2:
            print(_c("注意: Martini v2 体系需要 GROMACS <= 2023.3。", "33"))
        for w in res.warnings:
            print(_c(f"注意: {w}", "33"))

        # 2) 后端（超算环境模块）与调度器
        backend_kind = a.backend
        if backend_kind == "auto":
            backend_kind = config.BACKEND_WSL if IS_WINDOWS else config.BACKEND_LOCAL
        self.modules = self._modules()
        self.backend = make_backend(
            backend_kind, a.distro or "",
            self.modules if backend_kind == config.BACKEND_LOCAL else None)
        if self.modules:
            print(f"环境模块: {' '.join(self.modules)}")
        self._out_root = Path(a.output) if a.output else Path.home() / "MD_sim"
        sched = self._resolve_scheduler()

        # 超算模式：生成作业脚本（正式模拟应提交计算节点，不在登录节点跑）
        if sched != "none" and not a.dry_run:
            script = self._make_job_script(str(Path(a.input).resolve()))
            print(_c(f"检测到调度器（{sched}），已生成作业脚本: {script}", "1;36"))
            print("正式模拟请提交到计算节点运行:")
            print(f"  {'sbatch' if sched == 'slurm' else 'qsub'} {script}")
            if a.submit:
                self._submit_job(script, sched)
            return 0
        if sched != "none" and a.dry_run:
            print(_c("dry-run 在登录节点执行即可。", "33"))

        # 3) 本机直接运行（登录节点快速验证 / 普通服务器）
        if not a.dry_run:
            info = envsetup.probe_gmx_info(self.backend, a.gmx)
            if not info.get("ok"):
                if a.auto_install_gmx:
                    print("未检测到 gmx，开始自动安装...")
                    ok, msg = envsetup.install_gmx(
                        backend_kind, a.distro or "", None,
                        lambda l: self._log_to_console("INFO", l),
                        gmx_version=a.gmx_version)
                    if not ok:
                        print(_c("自动安装失败: " + msg, "31"))
                        return 1
                    a.gmx = msg.strip()
                else:
                    print(_c(f"未找到 gmx: {info.get('msg', '')}\n"
                             "若在超算上，请先 module load gromacs（或用 --module-load "
                             "指定让软件自动加载），\n或用 --gmx 指定路径，或加 "
                             "--auto-install-gmx。", "31"))
                    return 1
            else:
                print(f"gmx: {info.get('msg', '')}")
                if sched != "none":
                    print(_c("提示: 当前在登录节点直接运行，正式长时间模拟建议 "
                             "--scheduler auto --submit 提交计算节点。", "33"))
                if (res.martini_v2
                        and envsetup.martini_v2_incompatible(info.get("version_tuple"), True)):
                    print(_c("警告: 该 GROMACS 版本与 Martini v2 不兼容，运行会报 "
                             "reaction-field 错误。建议 --gmx 指向 2023.3。", "33"))

        # 3) 运行
        self._out_root.mkdir(parents=True, exist_ok=True)
        self.logger.info("CLI 模式启动")
        wf = workflow.Workflow(
            detect=res, sim_type=sim_type, output_root=self._out_root,
            gmx_exe=a.gmx, ntomp=a.ntomp, use_gpu=a.gpu, dry_run=a.dry_run,
            backend=self.backend, stage_configs=stage_cfgs,
            logger=self.logger, emit=self._emit)
        self.workflow = wf

        worker = threading.Thread(target=self._run_worker, args=(wf, 0), daemon=True)
        worker.start()
        try:
            while worker.is_alive():
                worker.join(timeout=0.2)
        except KeyboardInterrupt:
            print(_c("\n收到 Ctrl+C，正在取消当前阶段（进程会被终止）...", "33"),
                  flush=True)
            wf.runner.request_cancel()
            worker.join(timeout=30)

        result = self._result
        if result is None:
            return 1
        if result.canceled:
            print(_c("运行已取消。", "33"))
            return 130
        if result.ok:
            print("")
            print(_c("全部阶段完成！", "32"))
            print(f"输出目录: {result.output_dir}")
            print(f"运行汇总: {Path(str(result.output_dir)) / 'run_summary.txt'}")
            return 0
        print(_c(f"运行失败于阶段 {result.failed_stage}，"
                 f"详见 {result.output_dir / 'md_sim.log'}", "31"))
        return 1

    def _run_worker(self, wf: workflow.Workflow, start_from: int) -> None:
        try:
            self._result = wf.run(start_from=start_from)
        except Exception as exc:
            self.logger.error(f"内部错误: {exc}")
            self._result = workflow.JobResult(ok=False)


    # ------------------------------------------------------------------
    # 超算支持：环境模块 + 作业脚本
    # ------------------------------------------------------------------
    def _modules(self) -> List[str]:
        return [m for m in (self.args.module_load or "").replace(",", " ").split()
                if m]

    _SCHEDULER_CMDS = {  # 调度器命令 → 脚本类型
        "sbatch": "slurm", "yhbatch": "pbs", "llsubmit": "pbs",
        "jsub": "pbs", "qsub": "pbs",
    }

    def _resolve_scheduler(self) -> str:
        a = self.args
        if a.scheduler != "auto":
            return a.scheduler
        # 用登录 shell 探测真实命令（避免 stub/别名误导）
        try:
            out = subprocess.run(
                ["bash", "-lc", "command -v " + " ".join(self._SCHEDULER_CMDS)
                                + " 2>/dev/null"],
                capture_output=True, text=True, timeout=20).stdout or ""
            for line in out.splitlines():
                base = Path(line.strip()).name
                if base in self._SCHEDULER_CMDS:
                    return self._SCHEDULER_CMDS[base]
        except (FileNotFoundError, OSError):
            pass
        for base, kind in self._SCHEDULER_CMDS.items():
            if shutil.which(base):
                return kind
        return "none"

    def _cli_argv(self, input_abs: str) -> List[str]:
        """重建作业脚本里的 CLI 参数（去掉调度器专属参数）。"""
        a = self.args
        v = [input_abs, "-o", str(self._out_root)]
        if a.config:
            v += ["-c", str(Path(a.config).resolve())]
        if a.em_ps is not None:
            v += ["--em-ps", str(a.em_ps)]
        if a.equil_ps is not None:
            v += ["--equil-ps", str(a.equil_ps)]
        if a.prod_ns is not None:
            v += ["--prod-ns", str(a.prod_ns)]
        if a.temp is not None:
            v += ["--temp", str(a.temp)]
        if a.pres is not None:
            v += ["--pres", str(a.pres)]
        if a.dt is not None:
            v += ["--dt", str(a.dt)]
        if a.restraints is not None:
            v += ["--restraints", "on" if a.restraints else "off"]
        if a.nstxout is not None:
            v += ["--nstxout", str(a.nstxout)]
        if a.nstenergy is not None:
            v += ["--nstenergy", str(a.nstenergy)]
        if a.emtol is not None:
            v += ["--emtol", str(a.emtol)]
        if a.tcoupl:
            v += ["--tcoupl", a.tcoupl]
        if a.pcoupl:
            v += ["--pcoupl", a.pcoupl]
        if a.type:
            v += ["--type", a.type]
        if a.gmx and a.gmx != config.GMX_EXE_DEFAULT:
            v += ["--gmx", a.gmx]
        if a.ntomp:
            v += ["--ntomp", str(a.ntomp)]
        if a.gpu:
            v += ["--gpu"]
        if self.modules:
            v += ["--module-load", " ".join(self.modules)]
        v += ["--backend", "local"]
        return v

    def _make_job_script(self, input_abs: str) -> Path:
        """生成 Slurm/PBS 作业脚本（在计算节点上运行 CLI）。"""
        a = self.args
        sched = self._resolve_scheduler()
        py = sys.executable
        base = Path(__file__).resolve().parent.parent
        cand = base / "runtime" / "python" / "bin" / "python3"
        if cand.exists():
            py = str(cand)
        app_main = base / "app" / "main.py"
        if not app_main.exists():
            app_main = base / "main.py"
        argv = " ".join(shlex.quote(v) for v in self._cli_argv(input_abs))

        lines = ["#!/bin/bash"]
        if sched == "slurm":
            if a.partition:
                lines.append(f"#SBATCH --partition={a.partition}")
            lines += [f"#SBATCH --job-name={a.job_name}",
                      "#SBATCH --nodes=1",
                      f"#SBATCH --cpus-per-task={a.cpus}",
                      f"#SBATCH --time={a.time}",
                      f"#SBATCH --output={a.job_name}-%j.log"]
            if a.ngpus > 0:
                lines.append(f"#SBATCH --gres=gpu:{a.ngpus}")
        else:  # PBS / Torque（队列名在 -q，GPU 常写成 nodes=...:gpus=G）
            lines.append(f"#PBS -N {a.job_name}")
            if a.partition:
                lines.append(f"#PBS -q {a.partition}")
            gpu_part = f":gpus={a.ngpus}" if a.ngpus > 0 else ""
            lines += [f"#PBS -l nodes=1:ppn={a.cpus}{gpu_part}",
                      f"#PBS -l walltime={a.time}",
                      "#PBS -j oe"]
        lines.append("")
        if self.modules:
            lines.append("module load " + " ".join(self.modules))
        lines.append("cd " + shlex.quote(str(Path.cwd())))
        lines.append(f"{py} {app_main} --cli {argv}")
        lines.append('echo "MDWF 退出码: $?"')

        path = Path(a.job_script) if a.job_script else Path("mdwf_submit.sh").resolve()
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        # 作业脚本需可执行（jsub 等调度器直接 ./script 运行，缺权限会 126 退出）
        try:
            os.chmod(path, 0o755)
        except OSError:
            pass
        return path

    def _submit_job(self, script: Path, sched: str) -> None:
        """提交作业：先登录 shell（加载集群 profile，等同手动敲命令）→ 直接调用 → 失败给手动命令。"""
        if getattr(self.args, "submit_cmd", None):
            cmd = self.args.submit_cmd
        elif sched == "slurm":
            cmd = "sbatch"
        else:
            # 优先用实际探测到的提交命令
            for base in ("yhbatch", "llsubmit", "jsub", "qsub"):
                if shutil.which(base):
                    cmd = base
                    break
            else:
                cmd = "qsub"
        s = shlex.quote(str(script))
        attempts = [
            ["bash", "-lc", f"{cmd} {s}"],
            [cmd, str(script)],
        ]
        last = ""
        for attempt in attempts:
            try:
                proc = subprocess.run(attempt, capture_output=True, text=True,
                                      timeout=60)
                out = (proc.stdout or proc.stderr or "").strip()
                if proc.returncode == 0 and out \
                        and "not found" not in out.lower() \
                        and "permission denied" not in out.lower():
                    print(out)
                    return
                last = out or f"退出码 {proc.returncode}"
            except (FileNotFoundError, PermissionError,
                    subprocess.TimeoutExpired) as exc:
                last = str(exc)
        print(_c(f"自动提交失败（{last}）。", "31"))
        print(f"请手动提交: {cmd} {script}")
        print("（部分超算提交命令不同，如 llsubmit/yhrun/jsub 等，请按本集群要求"
              "提交；队列名/GPU 写法可直接修改生成脚本）")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="MDWorkflowManager-cli",
        description="MD Workflow Manager 控台版：CHARMM-GUI → GROMACS 全流程"
                    "（能量最小化 → 平衡 → 生产模拟），适合 SSH/终端/nohup 运行。")
    p.add_argument("input", help="CHARMM-GUI 输出目录或压缩包（tgz/zip）")
    p.add_argument("-o", "--output", help="输出根目录（默认 ~/MD_sim）")
    p.add_argument("-c", "--config", help="参数 JSON（GUI“保存参数”生成的文件）")
    p.add_argument("--em-ps", type=float, default=None,
                   help="能量最小化时长(ps)；默认不覆盖，使用 CHARMM-GUI 原始步数")
    p.add_argument("--equil-ps", type=float, default=None,
                   help="每个平衡阶段时长(ps)；默认不覆盖")
    p.add_argument("--prod-ns", type=float, default=None,
                   help="生产模拟时长(ns)；默认不覆盖")
    p.add_argument("--temp", type=float, default=None, help="温度 K（平衡+生产）")
    p.add_argument("--pres", type=float, default=None, help="压力 bar（恒压阶段）")
    p.add_argument("--dt", type=float, default=None, help="步长 ps")
    p.add_argument("--restraints", choices=["on", "off"], default=None,
                   help="平衡阶段位置限制开关")
    p.add_argument("--nstxout", type=int, default=None, help="轨迹输出频率（步）")
    p.add_argument("--nstenergy", type=int, default=None, help="能量输出频率（步）")
    p.add_argument("--emtol", type=float, default=None, help="最小化力收敛阈值")
    p.add_argument("--tcoupl", default=None, help="恒温器（v-rescale 等）")
    p.add_argument("--pcoupl", default=None, help="恒压器（Parrinello-Rahman 等）")
    p.add_argument("--show-params", action="store_true",
                   help="显示各阶段当前参数后退出")
    p.add_argument("--save-config", metavar="PATH",
                   help="导出参数模板 JSON（含各阶段默认值与键说明），编辑后 -c 使用")
    p.add_argument("--type", choices=["all_atom", "coarse_grained"], default=None,
                   help="模拟类型（默认自动检测）")
    p.add_argument("--gmx", default=config.GMX_EXE_DEFAULT,
                   help="gmx 命令或路径（默认 gmx）")
    p.add_argument("--backend", choices=["auto", "local", "wsl"], default="auto",
                   help="运行后端（默认: Linux=local, Windows=wsl）")
    p.add_argument("--distro", default="", help="WSL 发行版名（Windows 用）")
    p.add_argument("--ntomp", type=int, default=0, help="并行线程数（0=自动）")
    p.add_argument("--gpu", action="store_true",
                   help="尝试 GPU 加速（-nb gpu；出错自动回退 CPU）")
    p.add_argument("--dry-run", action="store_true",
                   help="模拟运行（不调用 gmx，验证流程）")
    p.add_argument("--auto-install-gmx", action="store_true",
                   help="gmx 缺失时自动安装（micromamba + conda-forge）")
    p.add_argument("--gmx-version", default=None,
                   help="自动安装的 GROMACS 版本，如 2023.3（默认最新）")
    # ---- 超算支持 ----
    p.add_argument("--module-load", default="",
                   help="超算环境模块，空格分隔，如 'gromacs/2023.3 cuda/12.2'（先 module avail gromacs 查询）")
    p.add_argument("--scheduler", choices=["auto", "slurm", "pbs", "none"],
                   default="auto",
                   help="auto=检测到 sbatch/qsub 就生成作业脚本提交计算节点；none=直接在本机跑")
    p.add_argument("--partition", default="", help="调度器队列/分区名")
    p.add_argument("--cpus", type=int, default=8, help="每任务 CPU 核数")
    p.add_argument("--ngpus", type=int, default=0, help="申请 GPU 数")
    p.add_argument("--time", default="24:00:00", help="最长运行时限，如 24:00:00")
    p.add_argument("--job-name", default="MDWF", help="作业名")
    p.add_argument("--job-script", default=None, help="作业脚本输出路径（默认 mdwf_submit.sh）")
    p.add_argument("--submit", action="store_true",
                   help="生成作业脚本后立即提交（sbatch/qsub/yhbatch 等自动识别）")
    p.add_argument("--submit-cmd", default=None,
                   help="自定义提交命令（如 yhbatch/llsubmit/jsub），默认自动识别")
    return p


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    return CliApp(args).run()


if __name__ == "__main__":
    sys.exit(main())
