# -*- coding: utf-8 -*-
"""自检：构造合成 CHARMM-GUI 输出目录，验证 检测 / mdp 覆盖 / dry-run 工作流 / 取消。

运行: python main.py --selftest
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
import zipfile
from pathlib import Path
from typing import Callable, List, Optional

from . import config, detect, envsetup, mdp as mdp_mod, workflow
from .backend import LocalBackend, WslBackend, decode_wsl_bytes, win_to_wsl
from .gmx import check_gmx, fmt_eta
from .logger import MDLogger

PASS = 0
FAIL = 1

# 兼容打包后的 exe（无控制台）：输出同时写入报告文件
_REPORT_LINES: list = []


def _out(text: str) -> None:
    _REPORT_LINES.append(text)
    try:
        print(text)
    except Exception:
        pass


def _write_mdps(gdir: Path, dt: str) -> None:
    """写一套与 CHARMM-GUI 结构一致的 step6 系列 mdp。"""
    base = (
        "; synthetic equilibration\n"
        "integrator = md\n"
        f"dt = {dt}\n"
        "nsteps = 50000\n"
        "nstxout-compressed = 5000\n"
        "nstenergy = 1000\n"
        "cutoff-scheme = Verlet\n"
        "pbc = xyz\n"
        "define = -DPOSRES\n"
        "tcoupl = v-rescale\n"
        "tc-grps = Protein Water_and_ions\n"
        "tau_t = 1.0 1.0\n"
        "ref_t = 310 310\n"
        "pcoupl = Parrinello-Rahman\n"
        "pcoupltype = isotropic\n"
        "tau_p = 2.0\n"
        "ref_p = 1.0\n"
        "compressibility = 4.5e-5\n"
    )
    specs = [
        ("step6.0_minimization.mdp",
         "integrator = steep\nemtol = 1000\nnsteps = 1000\n"
         "nstlist = 10\ncutoff-scheme = Verlet\npbc = xyz\n"),
        ("step6.1_equilibration.mdp", base),
        ("step6.2_equilibration.mdp", base),
        ("step6.3_equilibration.mdp", base.replace("define = -DPOSRES", "; restraint off")),
        ("step6.4_equilibration.mdp", base),
        ("step6.5_equilibration.mdp", base.replace("define = -DPOSRES", "; restraint off")),
        ("step6.6_production.mdp", base.replace("define = -DPOSRES", "; production")),
    ]
    for name, text in specs:
        (gdir / name).write_text(text, encoding="utf-8")


def make_synthetic_gromacs_dir(root: Path, coarse: bool) -> Path:
    """构造合成 CHARMM-GUI GROMACS 输出目录。"""
    gdir = root / ("gromacs_martini" if coarse else "gromacs")
    gdir.mkdir(parents=True, exist_ok=True)
    if coarse:
        (gdir / "topol.top").write_text(
            '#include "toppar/martini_v3.0.0.itp"\n'
            '#include "toppar/martini_v3.0.0_ions_v1.itp"\n'
            "[ system ]\nSYN\n[ molecules ]\nSOL 2000\n", encoding="utf-8")
        ffdir = gdir / "toppar" / "martini_v3.0.0.ff"
        ffdir.mkdir(parents=True, exist_ok=True)
        _write_mdps(gdir, "0.020")
    else:
        (gdir / "topol.top").write_text(
            '#include "toppar/charmm36-jul2022.ff/forcefield.itp"\n'
            '#include "toppar/charmm36-jul2022.ff/water.itp"\n'
            "[ system ]\nSYN\n[ molecules ]\nSOL 1000\n", encoding="utf-8")
        ffdir = gdir / "toppar" / "charmm36-jul2022.ff"
        ffdir.mkdir(parents=True, exist_ok=True)
        _write_mdps(gdir, "0.002")
    (ffdir / "forcefield.itp").write_text("; placeholder\n", encoding="utf-8")
    (gdir / "step5_input.gro").write_text(
        "SYNTHETIC\n1000\n   1.0   1.0   1.0\n", encoding="utf-8")
    return gdir


def _tiny_cfgs(stages: List[detect.Stage], prod_ns: float = 1.0) -> dict:
    """让 dry-run 快速走完的每阶段配置。"""
    cfg = {}
    for s in stages:
        if s.kind == config.KIND_PRODUCTION:
            cfg[s.key] = {"duration_ps": prod_ns * 1000.0}
        else:
            cfg[s.key] = {"duration_ps": 2.0}
    return cfg


def run_selftest() -> int:
    # 兼容 Windows GBK 控制台：统一按 UTF-8 输出，避免特殊字符报错
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    _out("=" * 62)
    _out(" MD Workflow Manager 自检")
    _out("=" * 62)
    results: List[tuple] = []
    tmp = Path(tempfile.mkdtemp(prefix="mdwf_selftest_"))
    try:
        results.append(("check_gmx（本机应报不可用或可用）", _test_check_gmx()))
        results.append(("backend: 路径转换与命令包装", _test_backend()))
        results.append(("envsetup: 配置持久化", _test_config(tmp)))
        results.append(("detect: 全原子识别", _test_detect_aa(tmp)))
        results.append(("detect: 粗粒化识别", _test_detect_cg(tmp)))
        results.append(("detect: system.top 变体识别", _test_detect_variants(tmp)))
        results.append(("detect: zip 导入", _test_zip(tmp)))
        results.append(("mdp: 参数覆盖与 first_float", _test_mdp_override(tmp)))
        results.append(("envsetup: 作业历史", _test_history(tmp)))
        results.append(("workflow: dry-run 全流程", _test_workflow_dry(tmp)))
        results.append(("workflow: 取消", _test_cancel(tmp)))
        results.append(("cli: dry-run 全流程", _test_cli(tmp)))
        results.append(("mdp: 软核扰动禁用（版本兼容）", _test_disable_free_energy(tmp)))
        results.append(("envsetup: 真实 WSL 探测（本机有则验证）", _test_wsl_probe()))
        results.append(("envsetup: 环境自测真实运行（WSL+gmx 可用时）", _test_env_selftest()))
        results.append(("gmx: 三种进度格式解析", _test_progress_parse()))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    _out("-" * 62)
    failed = 0
    for name, ok in results:
        _out(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            failed += 1
    _out("-" * 62)
    if failed:
        _out(f"共 {len(results)} 项，失败 {failed} 项")
        code = FAIL
    else:
        _out(f"全部 {len(results)} 项通过")
        code = PASS
    # 打包成 exe 后没有控制台，结果写入报告文件
    try:
        (Path.cwd() / "mdwf_selftest_report.txt").write_text(
            "\n".join(_REPORT_LINES) + "\n", encoding="utf-8")
    except Exception:
        pass
    return code


def _report(fn: Callable) -> bool:
    try:
        fn()
        return True
    except AssertionError as exc:
        _out(f"    断言失败: {exc}")
        return False
    except Exception:
        traceback.print_exc()
        return False


# ---------------------------------------------------------------------------
def _test_check_gmx():
    def run():
        ok, msg = check_gmx("definitely_not_a_real_gmx_xyz")
        assert ok is False, "不存在的 gmx 应报不可用"
        assert "gmx" in msg
        assert fmt_eta(3661) == "1小时1分1秒"
        assert fmt_eta(65) == "1分5秒"
    return _report(run)


def _test_backend():
    def run():
        # win_to_wsl 路径转换
        assert win_to_wsl(r"C:\Users\张三\a b") == "/mnt/c/Users/张三/a b"
        assert win_to_wsl(r"D:\x\y") == "/mnt/d/x/y"
        assert win_to_wsl("/home/user/x") == "/home/user/x"
        # 本机后端原样透传
        lb = LocalBackend()
        argv = lb.wrap(["gmx", "grompp", "-f", "a.mdp"], "C:\\work")
        assert argv == ["gmx", "grompp", "-f", "a.mdp"]
        # WSL 后端：bash -lc 包装 + 挂载路径 + 引号安全
        wb = WslBackend("Ubuntu")
        argv = wb.wrap(["gmx", "grompp", "-f", "a b.mdp", "-c", "../inputs/x.gro"],
                       r"C:\Users\张三\out\job")
        assert argv[0:4] == ["wsl", "-d", "Ubuntu", "--"], argv
        script = argv[-1]
        assert script.startswith("cd ")
        assert '/mnt/c/Users/张三/out/job' in script
        assert "'a b.mdp'" in script, "含空格的路径应被 shlex 引用"
        assert "-c" in script and "../inputs/x.gro" in script
        # WSL 输出解码：各种编码都不应乱码
        assert decode_wsl_bytes("Ubuntu\n".encode("utf-8")) == "Ubuntu"
        assert decode_wsl_bytes("默认发行版".encode("utf-8")) == "默认发行版"
        assert decode_wsl_bytes(b"\xff\xfe" + "Ubuntu".encode("utf-16-le")) == "Ubuntu"
        assert decode_wsl_bytes("Ubuntu".encode("utf-16-le")) == "Ubuntu"  # 无 BOM
        assert decode_wsl_bytes(b"\xef\xbb\xbf" + "Ubuntu".encode("utf-8")) == "Ubuntu"
        assert decode_wsl_bytes(b"") == ""
    return _report(run)


def _test_detect_variants(tmp: Path):
    """兼容 CHARMM-GUI Martini 输出：system.top + step5_charmm2gmx.pdb。"""
    def run():
        gdir = make_synthetic_gromacs_dir(tmp, coarse=True)
        # 改成 system.top 命名 + step5_charmm2gmx.pdb 结构（模拟旧版 Martini 输出）
        (gdir / "topol.top").rename(gdir / "system.top")
        (gdir / "step5_input.gro").rename(gdir / "step5_charmm2gmx.pdb")
        res = detect.detect(str(gdir))
        assert res.ok, res.error
        assert res.sim_type == config.COARSE_GRAINED, res.sim_type
        assert res.topol_path and res.topol_path.name == "system.top"
        assert res.structure_path and res.structure_path.name == "step5_charmm2gmx.pdb"
        assert len(res.stages) == 7
        # 缺失拓扑 → 检测仍成功，但标记 topol_missing
        (gdir / "system.top").unlink()
        res2 = detect.detect(str(gdir))
        assert res2.ok and res2.topol_missing
        assert any("拓扑" in w for w in res2.warnings)
        # detect 直接吃压缩包文件路径（自动解压）
        import tarfile as _tarfile
        tgz = tmp / "user_out.tgz"
        with _tarfile.open(tgz, "w:gz") as tf:
            for f in gdir.rglob("*"):
                if f.is_file():
                    tf.add(f, f"charmm-gui-123/{f.relative_to(gdir.parent)}")
        res3 = detect.detect(str(tgz))
        assert res3.ok, res3.error
        assert res3.gromacs_dir is not None
        # Martini v2 触发兼容性警告
        v2dir = make_synthetic_gromacs_dir(tmp, coarse=True)
        (v2dir / "topol.top").write_text(
            '#include "toppar/martini_v2.2.itp"\n[ system ]\nS\n[ molecules ]\nSOL 1\n',
            encoding="utf-8")
        res4 = detect.detect(str(v2dir))
        assert res4.ok and res4.martini_v2, res4.warnings
        assert any("Martini v2" in w for w in res4.warnings), res4.warnings
        # GROMACS 版本参数
        assert envsetup._gmx_package_spec(None) == "gromacs"
        assert envsetup._gmx_package_spec("2023.3") == "gromacs=2023.3"
        # GPU 模式只加 -nb gpu（不加 -pme gpu：Martini 体系/EM 阶段会报错）
        cmd = config.build_mdrun_cmd("gmx", "md", 0, use_gpu=True)
        assert "-nb" in cmd and "gpu" in cmd, cmd
        assert "-pme" not in cmd, cmd
        cmd_cpu = config.build_mdrun_cmd("gmx", "md", 0, use_gpu=False)
        assert "-nb" not in cmd_cpu and "-pme" not in cmd_cpu, cmd_cpu
        # 版本解析与兼容性判断（2023.3 实测可用；2023.4+ 报错）
        assert envsetup._parse_version_tuple("2025.4-Ubuntu_1") == (2025, 4)
        assert envsetup._parse_version_tuple("2023.3") == (2023, 3)
        assert envsetup._parse_version_tuple("2023.3-conda_forge") == (2023, 3)
        assert envsetup._parse_version_tuple("garbage") is None
        assert envsetup.martini_v2_incompatible((2023, 3), True) is False, "2023.3 实测可用"
        assert envsetup.martini_v2_incompatible((2023, 2), True) is False
        assert envsetup.martini_v2_incompatible((2023, 4), True) is True
        assert envsetup.martini_v2_incompatible((2024, 0), True) is True
        assert envsetup.martini_v2_incompatible((2025, 4), True) is True
        assert envsetup.martini_v2_incompatible((2025, 4), False) is False
        assert envsetup.martini_v2_incompatible(None, True) is False
    return _report(run)


def _test_config(tmp: Path):
    def run():
        cfg_path = tmp / "cfg" / "config.json"
        envsetup.save_config({"backend": "wsl", "wsl_distro": "Ubuntu",
                              "gmx_path": "~/micromamba/envs/md/bin/gmx",
                              "channels": ["conda-forge"]}, cfg_path)
        cfg = envsetup.load_config(cfg_path)
        assert cfg["backend"] == "wsl"
        assert cfg["wsl_distro"] == "Ubuntu"
        assert cfg["gmx_path"].endswith("gmx")
        # 缺失时用默认值
        cfg2 = envsetup.load_config(tmp / "nope.json")
        assert cfg2["backend"] == config.BACKEND_WSL
        assert cfg2["gmx_path"] == config.GMX_EXE_DEFAULT
    return _report(run)


def _test_zip(tmp: Path):
    def run():
        import tarfile as _tarfile
        gdir = make_synthetic_gromacs_dir(tmp, coarse=False)

        # 1) zip 格式
        zip_path = tmp / "charmm_gui_out.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in gdir.rglob("*"):
                if f.is_file():
                    zf.write(f, f"charmm-gui-download/{f.relative_to(gdir.parent)}")
        res1 = detect.extract_charmmgui_archive(str(zip_path),
                                                dest_root=tmp / "zip_extract")
        assert res1.ok and res1.gromacs_dir is not None, "zip 中应找到 GROMACS 输出目录"
        d1 = detect.detect(str(res1.gromacs_dir))
        assert d1.ok and d1.sim_type == config.ALL_ATOM and len(d1.stages) == 7

        # 2) tgz 格式（CHARMM-GUI 实际输出格式）
        tgz_path = tmp / "charmm_gui_out.tgz"
        with _tarfile.open(tgz_path, "w:gz") as tf:
            for f in gdir.rglob("*"):
                if f.is_file():
                    tf.add(f, f"charmm-gui-download/{f.relative_to(gdir.parent)}")
        res2 = detect.extract_charmmgui_archive(str(tgz_path),
                                                dest_root=tmp / "tgz_extract")
        assert res2.ok and res2.gromacs_dir is not None, "tgz 中应找到 GROMACS 输出目录"
        d2 = detect.detect(str(res2.gromacs_dir))
        assert d2.ok and len(d2.stages) == 7

        # 3) 深层结构（模拟 CHARMM-GUI 多层包装目录）
        deep = tmp / "deep.tgz"
        with _tarfile.open(deep, "w:gz") as tf:
            for f in gdir.rglob("*"):
                if f.is_file():
                    tf.add(f, f"aaa/bbb/ccc/ddd/{f.relative_to(gdir.parent)}")
        res3 = detect.extract_charmmgui_archive(str(deep), dest_root=tmp / "deep_ext")
        assert res3.ok and res3.gromacs_dir is not None, "深层目录应能找到"

        # 4) 嵌套压缩包（zip 内含 tgz）
        inner = tmp / "inner.tgz"
        with _tarfile.open(inner, "w:gz") as tf:
            for f in gdir.rglob("*"):
                if f.is_file():
                    tf.add(f, f"gromacs/{f.name}")
        nest = tmp / "nested.zip"
        with zipfile.ZipFile(nest, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(inner, "charmm-gui-inner/inner.tgz")
        res4 = detect.extract_charmmgui_archive(str(nest), dest_root=tmp / "nest_ext")
        assert res4.ok and res4.gromacs_dir is not None, "嵌套压缩包应自动解开"

        # 5) 非法/缺失输入 → 失败 + 诊断信息
        res5 = detect.extract_charmmgui_archive(str(tmp / "not_exist.tgz"))
        assert not res5.ok
        (tmp / "readme.txt").write_text("hi", encoding="utf-8")
        res6 = detect.extract_charmmgui_archive(str(tmp / "readme.txt"))
        assert not res6.ok and "不支持的文件类型" in res6.hint, res6.hint
        # 空 zip → 诊断信息包含"为空"
        empty_zip = tmp / "empty.zip"
        with zipfile.ZipFile(empty_zip, "w") as zf:
            pass
        res7 = detect.extract_charmmgui_archive(str(empty_zip), dest_root=tmp / "e7")
        assert not res7.ok and "为空" in res7.hint, res7.hint
        # 无 gromacs 目录的 zip → 诊断信息给出线索
        bad_zip = tmp / "bad.zip"
        with zipfile.ZipFile(bad_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("readme/notes.txt", "hello")
        res8 = detect.extract_charmmgui_archive(str(bad_zip), dest_root=tmp / "e8")
        assert not res8.ok and "解压目录" in res8.hint and "topol.top" in res8.hint
    return _report(run)


def _test_history(tmp: Path):
    def run():
        hp = tmp / "history.json"
        envsetup.add_history({"sim_type": "全原子", "ok": True, "mode": "真实",
                              "output_dir": "C:/x"}, hp)
        envsetup.add_history({"sim_type": "粗粒化", "ok": False, "mode": "dry-run",
                              "output_dir": "C:/y"}, hp)
        hist = envsetup.load_history(hp)
        assert len(hist) == 2
        assert hist[0]["ok"] is False and hist[0]["sim_type"] == "粗粒化"
        assert hist[1]["output_dir"] == "C:/x"
        # 上限 50 条
        for i in range(60):
            envsetup.add_history({"sim_type": "t", "ok": True}, hp)
        assert len(envsetup.load_history(hp)) == 50
    return _report(run)


def _test_env_selftest():
    """本机 WSL + gmx 可用时，真实跑通环境自测。"""
    def run():
        from .backend import WslBackend
        wsl = envsetup.detect_wsl()
        if not wsl.get("available") or not wsl.get("distros"):
            return  # 无 WSL：跳过（视为通过）
        distro = str(wsl.get("default") or wsl["distros"][0])
        backend = WslBackend(distro)
        ok, _ = backend.check_gmx("gmx")
        if not ok:
            return  # WSL 内无 gmx：跳过
        work = Path(tempfile.mkdtemp(prefix="mdwf_envtest_"))
        lines = []
        ok, msg = envsetup.self_test(backend, "gmx", lines.append, work)
        shutil.rmtree(work, ignore_errors=True)
        assert ok, f"环境自测应通过: {msg}"
    return _report(run)


def _test_progress_parse():
    """三种 gmx 进度输出格式都要能解析（新版 will finish / 旧版 time / EM Step=）。"""
    def run():
        from mdflow.gmx import extract_progress
        # 新版格式（gmx 2019+，含完成时刻）
        r = extract_progress("step 750000, will finish Thu Sep 24 12:20:22 2026", 1500000)
        assert r is not None and abs(r[0] - 0.5) < 0.001, r
        assert r[1] is not None, "will finish 格式应解析出完成时刻"
        # 旧版格式
        r2 = extract_progress("step 5000, time 10 (ps)", 10000)
        assert r2 is not None and abs(r2[0] - 0.5) < 0.001 and r2[1] is None
        # EM（steep）格式
        r3 = extract_progress("Step=   39, Dmax= 3.1e-01 nm, Epot=  4.68812e+05 "
                              "Fmax= 4.42076e+06, atom= 8729", 100)
        assert r3 is not None and abs(r3[0] - 0.39) < 0.001, r3
        # 非进度行 → None
        assert extract_progress("Reading file md.tpr, VERSION 2025.4", 100) is None
        assert extract_progress("There were 3 NOTEs", 100) is None
        # 超过总步数截断到 100%
        r4 = extract_progress("step 99999999, will finish Thu Sep 24 12:20:22 2026", 1500000)
        assert r4 is not None and r4[0] == 1.0
    return _report(run)


def _test_cli(tmp: Path):
    def run():
        from mdflow import cli
        gdir = make_synthetic_gromacs_dir(tmp, coarse=True)
        out = tmp / "cli_out"
        # 1) dry-run 基本流程
        code = cli.main([str(gdir), "-o", str(out), "--dry-run",
                         "--em-ps", "1", "--equil-ps", "1", "--prod-ns", "0.001"])
        assert code == 0, f"CLI 应返回 0，实际 {code}"
        jobs = list(out.glob("MD_sim_*"))
        assert jobs, "CLI 应生成作业目录"
        assert (jobs[0] / "run_summary.txt").exists()
        assert (jobs[0] / "md_sim.log").exists()
        # 2) --save-config 导出参数模板
        pf = tmp / "params.json"
        code = cli.main([str(gdir), "--save-config", str(pf)])
        assert code == 0 and pf.exists()
        data = json.loads(pf.read_text(encoding="utf-8"))
        assert "step6.6" in data["stage_cfgs"], "应包含生产阶段"
        assert "_help" in data, "模板应带键说明"
        assert data["stage_cfgs"]["step6.6"]["duration_ps"] > 0
        # 3) 编辑模板（改生产温度）→ -c 运行 → 覆盖生效
        data["stage_cfgs"]["step6.6"]["temperature_k"] = 320.0
        data["stage_cfgs"]["step6.6"]["duration_ps"] = 1.0
        pf2 = tmp / "params2.json"
        pf2.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        out2 = tmp / "cli_out2"
        code = cli.main([str(gdir), "-o", str(out2), "--dry-run", "-c", str(pf2)])
        assert code == 0
        # 生产阶段 = step6.6（合成体系无 step7）
        jobs2 = sorted(out2.glob("MD_sim_*/07_step6.6/md_step6.6.mdp"))
        assert jobs2, "应生成生产阶段 mdp"
        text = jobs2[0].read_text(encoding="utf-8")
        assert "ref_t = 320" in text, "参数文件中的温度应覆盖 mdp"
        # 4) --show-params 正常退出
        code = cli.main([str(gdir), "--show-params"])
        assert code == 0
        # 5) 超算作业脚本生成（Slurm）
        js = tmp / "submit.sh"
        code = cli.main([str(gdir), "-o", str(out), "--scheduler", "slurm",
                         "--partition", "gpu", "--cpus", "16", "--ngpus", "1",
                         "--module-load", "gromacs/2023.3", "--job-script", str(js),
                         "--em-ps", "1", "--equil-ps", "1", "--prod-ns", "0.001"])
        assert code == 0 and js.exists(), "应生成作业脚本"
        assert os.access(js, os.X_OK), "作业脚本应具有执行权限"
        content = js.read_text(encoding="utf-8")
        assert "#SBATCH" in content and "gres=gpu:1" in content
        assert "module load gromacs/2023.3" in content
        assert "--backend local" in content and "--prod-ns 0.001" in content
        # PBS 生成
        js2 = tmp / "submit.pbs"
        code = cli.main([str(gdir), "--scheduler", "pbs", "--job-script", str(js2)])
        assert code == 0 and "#PBS" in js2.read_text(encoding="utf-8")
    return _report(run)


def _test_disable_free_energy(tmp: Path):
    def run():
        from mdflow.mdp import disable_free_energy
        text = ("integrator = md\n"
                "free-energy = yes\n"
                "init-lambda = 0.01\n"
                "sc-alpha = 0.5\n"
                "couple-lambda0 = vdw-q\n"
                "ref_t = 310 310\n")
        out = disable_free_energy(text)
        assert "free-energy = no" in out
        for key in ("init-lambda", "sc-alpha", "couple-lambda0"):
            assert key not in out, key
        assert "ref_t = 310 310" in out, "普通参数应保留"
        assert "integrator = md" in out
        # 无 free-energy 的文件保持不变
        out2 = disable_free_energy("integrator = md\nnsteps = 10\n")
        assert "free-energy" not in out2 and "nsteps = 10" in out2
    return _report(run)


def _test_wsl_probe():
    """本机有 WSL 时验证真实探测链路（无 WSL 则跳过）。"""
    def run():
        wsl = envsetup.detect_wsl()
        if not wsl.get("available"):
            return  # 本机无 WSL：跳过（视为通过）
        distros = wsl.get("distros") or []
        if not distros:
            return  # WSL 可用但没有发行版：跳过
        b = WslBackend(str(wsl.get("default") or distros[0]))
        rc, out = b.probe("echo WSL_OK")
        assert rc == 0 and "WSL_OK" in out
        ok, msg = b.check_gmx("gmx")
        if ok:
            assert "gromacs" in msg.lower() or ":-)" in msg or "gmx" in msg.lower()
    return _report(run)


def _test_detect_aa(tmp: Path):
    def run():
        gdir = make_synthetic_gromacs_dir(tmp, coarse=False)
        res = detect.detect(str(gdir))
        assert res.ok, res.error
        assert res.sim_type == config.ALL_ATOM, res.sim_type
        assert res.ff_name == "charmm36-jul2022.ff", res.ff_name
        assert len(res.stages) == 7, len(res.stages)
        assert res.stages[0].kind == config.KIND_EM
        assert res.stages[-1].kind == config.KIND_PRODUCTION
        assert res.atoms == 1000, res.atoms
        # 也能识别父目录
        res2 = detect.detect(str(tmp))
        assert res2.ok and res2.gromacs_dir == gdir
        # step6.3 define 被注释 → 无位置限制
        s3 = [s for s in res.stages if s.key == "step6.3"][0]
        assert "-DPOSRES" not in str(mdp_mod.get(s3.parsed, "define", ""))
    return _report(run)


def _test_detect_cg(tmp: Path):
    def run():
        gdir = make_synthetic_gromacs_dir(tmp, coarse=True)
        res = detect.detect(str(gdir))
        assert res.ok, res.error
        assert res.sim_type == config.COARSE_GRAINED, res.sim_type
        assert res.ff_name == "martini", res.ff_name
        assert len(res.stages) == 7
    return _report(run)


def _test_mdp_override(tmp: Path):
    def run():
        gdir = make_synthetic_gromacs_dir(tmp, coarse=False)
        mdp_path = gdir / "step6.1_equilibration.mdp"
        params = mdp_mod.parse_mdp(mdp_path)
        text = mdp_path.read_text(encoding="utf-8")
        ov = mdp_mod.make_override_map(
            {"duration_ps": 200.0, "temperature_k": 300.0,
             "pressure_bar": 0.8, "restraints": False, "nstenergy": 2000},
            params, config.ALL_ATOM)
        out = mdp_mod.apply_overrides(text, ov)
        assert "nsteps = 100000" in out, "200ps / 0.002 = 100000 步"
        assert "ref_t = 300 300" in out
        assert "ref_p = 0.8" in out, "单值 ref_p 应被替换为 0.8"
        assert "-DPOSRES" not in out
        assert "nstenergy = 2000" in out
        # first_float：多值 ref_t 取第一个
        assert mdp_mod.first_float(params, "ref_t", None) == 310.0
        assert mdp_mod.first_float({}, "ref_t", 300.0) == 300.0
        # 时长与 dt 的换算一致性
        ov3 = mdp_mod.make_override_map({"duration_ps": 100.0, "dt_ps": 0.010},
                                        params, config.ALL_ATOM)
        assert ov3["nsteps"] == "10000" and ov3["dt"] == "0.01"
        # 打开位置限制
        ov2 = mdp_mod.make_override_map({"restraints": True},
                                        mdp_mod.parse_mdp(gdir / "step6.3_equilibration.mdp"),
                                        config.ALL_ATOM)
        out2 = mdp_mod.apply_overrides(
            (gdir / "step6.3_equilibration.mdp").read_text(encoding="utf-8"), ov2)
        assert "-DPOSRES" in out2
        # 缺失 mdp 时的模板
        for kind in (config.KIND_EM, config.KIND_EQUIL, config.KIND_PRODUCTION):
            tmpl = mdp_mod.generate_fallback_mdp(kind, config.ALL_ATOM)
            assert "nsteps" in tmpl and "integrator" in tmpl
        for kind in (config.KIND_EQUIL, config.KIND_PRODUCTION):
            tmpl_cg = mdp_mod.generate_fallback_mdp(kind, config.COARSE_GRAINED)
            assert "dt = 0.02" in tmpl_cg, "粗粒化平衡/生产模板应使用 20 fs 步长"
    return _report(run)


def _test_workflow_dry(tmp: Path):
    def run():
        gdir = make_synthetic_gromacs_dir(tmp, coarse=False)
        res = detect.detect(str(gdir))
        assert res.ok
        out_root = tmp / "out1"
        events: List[dict] = []
        logger = MDLogger()
        wf = workflow.Workflow(
            detect=res, sim_type=config.ALL_ATOM, output_root=out_root,
            gmx_exe="gmx", dry_run=True, dry_sleep=0.005,
            backend=LocalBackend(),
            stage_configs=_tiny_cfgs(res.stages),
            logger=logger, emit=events.append)
        result = wf.run()
        assert result.ok, result
        assert result.output_dir and result.output_dir.exists()
        job = result.output_dir
        assert (job / "md_sim.log").exists()
        assert (job / "run_summary.txt").exists()
        assert (job / "inputs" / "topol.top").exists()
        prod = job / "07_step6.6"
        assert (prod / "md.xtc").exists() and (prod / "md.gro").exists()
        # 进度事件单调不减
        fracs = [e["overall"] for e in events if e["type"] == workflow.EV_PROGRESS]
        assert len(fracs) > 10, "应产生足够多的进度事件"
        assert all(b >= a for a, b in zip(fracs, fracs[1:])), "整体进度应单调递增"
        done = [e for e in events if e["type"] == workflow.EV_STAGE_DONE]
        assert len(done) == 7, f"应有 7 个阶段完成, 实际 {len(done)}"
        # 汇总内容
        summary = (job / "run_summary.txt").read_text(encoding="utf-8")
        assert "全部阶段成功完成" in summary
    return _report(run)


def _test_cancel(tmp: Path):
    def run():
        gdir = make_synthetic_gromacs_dir(tmp, coarse=True)
        res = detect.detect(str(gdir))
        assert res.ok
        out_root = tmp / "out2"
        events: List[dict] = []
        logger = MDLogger()
        # 所有阶段都设置极长时长 → 取消发生在第一个阶段
        big = {s.key: {"duration_ps": 1e9} for s in res.stages}
        wf = workflow.Workflow(
            detect=res, sim_type=config.COARSE_GRAINED, output_root=out_root,
            gmx_exe="gmx", dry_run=True, dry_sleep=0.05,
            backend=LocalBackend(),
            stage_configs=big, logger=logger, emit=events.append)
        holder = {}

        def target():
            holder["result"] = wf.run()

        t = threading.Thread(target=target, daemon=True)
        t.start()
        # 等第一个进度事件出现后取消
        deadline = time.time() + 10
        while time.time() < deadline:
            if any(e["type"] == workflow.EV_PROGRESS for e in events):
                break
            time.sleep(0.02)
        assert any(e["type"] == workflow.EV_PROGRESS for e in events), "应出现进度事件"
        wf.runner.request_cancel()
        t.join(timeout=15)
        assert not t.is_alive(), "取消后工作线程应退出"
        result = holder.get("result")
        assert result is not None and result.canceled, "应报告取消"
        assert result.failed_stage == "step6.0"
        assert any(e["type"] == workflow.EV_CANCELED for e in events)
    return _report(run)


if __name__ == "__main__":
    sys.exit(run_selftest())
