# -*- coding: utf-8 -*-
"""环境配置：WSL 检测、gmx 检测、micromamba + conda-forge 自动安装 GROMACS。

自动安装流程（跨平台，无需管理员权限）：
    1. 下载 micromamba（静态单文件，本机临时目录）
    2. 解压到 ~/micromamba
    3. `micromamba create -n md -c conda-forge gromacs` 安装 GROMACS
    4. 验证 gmx 并保存配置（~/.mdwf/config.json）
"""
from __future__ import annotations

import datetime
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import config
from .backend import Backend, LocalBackend, WslBackend, decode_wsl_bytes, win_to_wsl

OnOutput = Callable[[str], None]


def _null_out(line: str) -> None:
    pass


# ---------------------------------------------------------------------------
# 配置持久化
# ---------------------------------------------------------------------------
def load_config(path=None) -> Dict[str, object]:
    cfg_path = Path(path) if path else config.config_path()
    default: Dict[str, object] = {
        "backend": config.BACKEND_WSL,
        "wsl_distro": "",
        "gmx_path": config.GMX_EXE_DEFAULT,
        "channels": list(config.CONDA_CHANNELS_DEFAULT),
        "gmx_version": "",
        "theme": "System",
    }
    if cfg_path.exists():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            for k in default:
                if k in data and data[k] is not None:
                    default[k] = data[k]
        except Exception:
            pass
    return default


def save_config(cfg: Dict[str, object], path=None) -> None:
    cfg_path = Path(path) if path else config.config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                        encoding="utf-8")


# ---------------------------------------------------------------------------
# WSL 检测
# ---------------------------------------------------------------------------
def detect_wsl() -> Dict[str, object]:
    """检测 WSL 可用性与已安装发行版。"""
    res: Dict[str, object] = {"available": False, "distros": [], "default": "",
                              "error": ""}
    try:
        proc = subprocess.run(["wsl", "--list", "--quiet"], capture_output=True,
                              timeout=60)
    except FileNotFoundError:
        res["error"] = "未检测到 wsl 命令（需要 Windows 10 2004+ 或 Windows 11）"
        return res
    out = decode_wsl_bytes(proc.stdout or b"")
    distros = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if distros and "no distributions" in " ".join(distros).lower():
        distros = []
    res["available"] = proc.returncode == 0 or bool(distros)
    res["distros"] = distros
    # 默认发行版
    try:
        proc2 = subprocess.run(["wsl", "--list", "--verbose"], capture_output=True,
                               timeout=60)
        out2 = decode_wsl_bytes(proc2.stdout or b"")
        for ln in out2.splitlines():
            if "(默认)" in ln or "(Default)" in ln:
                parts = ln.split()
                if parts:
                    res["default"] = parts[0]
                break
    except Exception:
        pass
    if not res["default"] and distros:
        res["default"] = distros[0]
    return res


def install_wsl_distro(distro: str = "Ubuntu",
                       on_output: OnOutput = _null_out) -> Tuple[bool, str]:
    """安装 WSL 发行版（需要管理员权限，Windows 会弹出 UAC）。"""
    try:
        proc = subprocess.Popen(["wsl", "--install", "-d", distro],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except Exception as exc:
        return False, f"无法启动 wsl --install: {exc}"
    assert proc.stdout is not None
    for raw in proc.stdout:
        text = raw.decode("utf-8", errors="replace").replace("\x00", "")
        for ln in text.splitlines():
            if ln.strip():
                on_output(ln.rstrip())
    rc = proc.wait()
    if rc != 0:
        return False, ("安装命令返回非零。若提示需要管理员权限，请以管理员身份运行 "
                       "`wsl --install -d %s` 后重启，再回到本软件继续。" % distro)
    return True, f"发行版 {distro} 已安装（首次启动会要求创建 Linux 用户）"


# ---------------------------------------------------------------------------
# micromamba / GROMACS 自动安装
# ---------------------------------------------------------------------------
def _opener():
    """优先使用系统代理（Windows 注册表代理 / 环境变量）。"""
    proxies = urllib.request.getproxies()
    if proxies:
        return urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
    return urllib.request.build_opener()


def _download(url: str, target: Path, on_output: OnOutput) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "MDWorkflowManager"})
    with _opener().open(req, timeout=180) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        with open(target, "wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if total:
                    on_output(f"  下载进度: {got / 1048576:.1f} / {total / 1048576:.1f} MB")


def _micromamba_ready_wsl(b: WslBackend) -> Tuple[bool, str]:
    rc, out = b.probe(
        "test -x ~/micromamba/bin/micromamba && ~/micromamba/bin/micromamba --version "
        "|| echo MICROMAMBA_NOT_FOUND")
    if "MICROMAMBA_NOT_FOUND" in out:
        return False, ""
    return True, out.strip().splitlines()[-1] if out.strip() else ""


def _env_path(b: Backend, mm_cmd: str, env_name: str) -> Optional[str]:
    """从 `micromamba env list` 查询指定环境名的实际路径。"""
    rc, out = b.probe(f"{mm_cmd} env list 2>/dev/null")
    if rc != 0:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == env_name:
            return parts[-1]
    return None


def _gmx_package_spec(gmx_version: Optional[str]) -> str:
    """返回 conda 包名；指定版本时返回 gromacs=<版本>。"""
    if gmx_version and str(gmx_version).strip():
        return f"gromacs={str(gmx_version).strip()}"
    return "gromacs"


def install_gmx_wsl(distro: str, on_output: OnOutput = _null_out,
                    channels: Optional[List[str]] = None,
                    base_url: str = config.MICROMAMBA_LINUX_URL,
                    gmx_version: Optional[str] = None) -> Tuple[bool, str]:
    """在 WSL 发行版内自动安装 GROMACS（micromamba + conda-forge）。"""
    b = WslBackend(distro)
    channels = channels or list(config.CONDA_CHANNELS_DEFAULT)
    on_output(f"[1/4] 检查 WSL 发行版 {distro or '默认'}…")
    rc, out = b.probe("echo READY")
    if rc != 0:
        return False, f"无法访问 WSL 发行版 {distro or '默认'}: {out[-300:]}"

    ok, ver = _micromamba_ready_wsl(b)
    if ok:
        on_output(f"[1/4] micromamba 已存在: {ver}")
    else:
        on_output("[1/4] 下载 micromamba（约 10 MB）…")
        tmp = Path(tempfile.mkdtemp(prefix="mdwf_mm_"))
        target = tmp / "micromamba.tar.bz2"
        try:
            _download(base_url, target, on_output)
        except Exception as exc:
            return False, f"下载 micromamba 失败: {exc}"
        on_output("[2/4] 解压 micromamba 到 WSL ~/micromamba …")
        script = ("mkdir -p ~/micromamba && tar -xjf " + shlex.quote(win_to_wsl(target)) +
                  " -C ~/micromamba bin/micromamba && chmod +x ~/micromamba/bin/micromamba "
                  "&& ~/micromamba/bin/micromamba --version")
        rc, out = b.probe(script)
        if rc != 0:
            return False, "解压 micromamba 失败: " + out[-400:]
        on_output("[2/4] micromamba 就绪")

    chan = " ".join(f"-c {shlex.quote(c)}" for c in channels)
    pkg = _gmx_package_spec(gmx_version)
    mm = "~/micromamba/bin/micromamba"
    env_path = _env_path(b, mm, config.ENV_NAME)
    if env_path and b.probe(f"test -x {shlex.quote(env_path)}/bin/gmx && echo OK")[0] == 0:
        on_output(f"[3/4] 环境 {config.ENV_NAME} 已存在（{env_path}），跳过安装")
    else:
        on_output(f"[3/4] 创建环境并安装 {pkg}（首次需数分钟，请耐心等待）…")
        # 显式指定根目录，保证环境落在 ~/micromamba（默认可能跑到 ~/.local/share/mamba）
        rc = b.stream(f"MAMBA_ROOT_PREFIX=$HOME/micromamba {mm} create -y "
                      f"-n {config.ENV_NAME} {chan} {pkg}", on_output)
        if rc != 0:
            return False, f"conda 安装 {pkg} 失败，请查看上方输出（网络问题可换用镜像通道）"
        env_path = _env_path(b, mm, config.ENV_NAME) or f"$HOME/micromamba/envs/{config.ENV_NAME}"

    gmx_path = f"{env_path}/bin/gmx"
    on_output("[4/4] 验证 gmx …")
    ok, ver = b.check_gmx(gmx_path)
    if ok:
        on_output("GROMACS 安装成功: " + ver)
        return True, gmx_path
    return False, "安装流程结束但未能验证 gmx，请查看日志"


def install_gmx_local(on_output: OnOutput = _null_out,
                      channels: Optional[List[str]] = None,
                      base_url: str = config.MICROMAMBA_LINUX_URL,
                      gmx_version: Optional[str] = None) -> Tuple[bool, str]:
    """在 Linux 本机自动安装 GROMACS（供 Linux 版软件使用）。"""
    if sys.platform == "win32":
        return False, "Windows 本机自动安装请使用 WSL2 后端"
    b = LocalBackend()
    channels = channels or list(config.CONDA_CHANNELS_DEFAULT)
    mm = Path.home() / "micromamba" / "bin" / "micromamba"
    if not mm.exists():
        on_output("[1/4] 下载 micromamba（约 10 MB）…")
        tmp = Path(tempfile.mkdtemp(prefix="mdwf_mm_"))
        target = tmp / "micromamba.tar.bz2"
        try:
            _download(base_url, target, on_output)
        except Exception as exc:
            return False, f"下载 micromamba 失败: {exc}"
        mm.parent.mkdir(parents=True, exist_ok=True)
        rc = subprocess.run(["tar", "-xjf", str(target), "-C", str(mm.parent),
                             "bin/micromamba"], capture_output=True).returncode
        if rc != 0:
            return False, "解压 micromamba 失败"
        os.chmod(mm, 0o755)
        on_output("[2/4] micromamba 就绪")
    else:
        on_output("[1/4] micromamba 已存在")

    chan = " ".join(f"-c {shlex.quote(c)}" for c in channels)
    pkg = _gmx_package_spec(gmx_version)
    mm_cmd = shlex.quote(str(mm))
    # 已有 md 环境则跳过创建（实际路径用 env list 查询，可能在任意根目录）
    env_path = _env_path(b, mm_cmd, config.ENV_NAME)
    if env_path and b.probe(
            f"test -x {shlex.quote(env_path)}/bin/gmx && echo OK")[0] == 0:
        on_output(f"[3/4] 环境 {config.ENV_NAME} 已存在（{env_path}），跳过安装")
    else:
        on_output(f"[3/4] 创建环境并安装 {pkg}（首次需数分钟）…")
        rc = b.stream(f"MAMBA_ROOT_PREFIX=$HOME/micromamba {mm_cmd} create "
                      f"-y -n {config.ENV_NAME} {chan} {pkg}", on_output)
        if rc != 0:
            return False, f"conda 安装 {pkg} 失败，请查看上方输出"
        env_path = _env_path(b, mm_cmd, config.ENV_NAME)

    if not env_path:
        return False, "未能定位 conda 环境路径，请查看上方输出"
    gmx_path = f"{env_path}/bin/gmx"
    on_output("[4/4] 验证 gmx …")
    ok, ver = b.check_gmx(gmx_path)
    if ok:
        on_output("GROMACS 安装成功: " + ver)
        return True, gmx_path
    return False, "安装流程结束但未能验证 gmx，请查看日志"


def check_env(backend_kind: str, distro: str = "",
              gmx_path: str = config.GMX_EXE_DEFAULT) -> Dict[str, object]:
    """一键检测：后端 + gmx 可用性。"""
    from .backend import make_backend
    backend = make_backend(backend_kind, distro)
    ok, msg = backend.check_gmx(gmx_path)
    return {"backend": backend_kind, "distro": distro, "gmx_ok": ok,
            "gmx_msg": msg, "gmx_path": gmx_path}


# ---------------------------------------------------------------------------
# gmx 能力探测（版本 / GPU 支持）
# ---------------------------------------------------------------------------
def _parse_version_tuple(s: str) -> Optional[tuple]:
    m = re.search(r"(\d+)\.(\d+)", s or "")
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return None


def probe_gmx_info(backend: Backend, gmx_exe: str) -> Dict[str, object]:
    """探测 gmx 版本与 GPU 支持，返回 {ok, msg, version, version_tuple, gpu}。"""
    info: Dict[str, object] = {"ok": False, "msg": "", "version": "",
                               "version_tuple": None, "gpu": False}
    rc, out = backend.probe(f"{shlex.quote(gmx_exe)} --version 2>/dev/null")
    if rc != 0 or not out.strip():
        info["msg"] = (out or "").strip() or f"{gmx_exe} 不可用"
        return info
    info["ok"] = True
    for line in out.splitlines():
        low = line.lower()
        if "gromacs version" in low:
            ver = line.split(":", 1)[-1].strip()
            info["version"] = ver
            info["version_tuple"] = _parse_version_tuple(ver)
        elif "gpu support" in low:
            # 值可能是 enabled / disabled / CUDA / OpenCL 等
            val = (line.split(":", 1)[-1] or "").strip().lower()
            info["gpu"] = val not in ("", "disabled", "none")
    info["msg"] = f"GROMACS {info['version'] or '?'}" + \
                  ("（GPU 版）" if info["gpu"] else "（CPU 版，不支持 GPU 加速）")
    return info


def martini_v2_incompatible(version_tuple, martini_v2: bool) -> bool:
    """Martini v2 体系 + GROMACS > 2023.3 不兼容（reaction-field 报错）。

    实测：2023.3-conda_forge 可正常运行 Martini v2 体系；2025.4 报错。
    因此以 2023.3 为兼容上限（更保守，避免用户踩坑）。
    """
    if not martini_v2 or not version_tuple:
        return False
    return version_tuple > (2023, 3)


def install_gmx(backend_kind: str, distro: str = "",
                channels: Optional[List[str]] = None,
                on_output: OnOutput = _null_out,
                gmx_version: Optional[str] = None) -> Tuple[bool, str]:
    """按后端自动安装 GROMACS。gmx_version 如 "2023.3" 可指定版本。"""
    if backend_kind == config.BACKEND_WSL:
        return install_gmx_wsl(distro, on_output, channels, gmx_version=gmx_version)
    return install_gmx_local(on_output, channels, gmx_version=gmx_version)


# ---------------------------------------------------------------------------
# 作业历史
# ---------------------------------------------------------------------------
def history_path() -> Path:
    return config.app_config_dir() / "history.json"


def load_history(path=None) -> List[dict]:
    hp = Path(path) if path else history_path()
    if not hp.exists():
        return []
    try:
        data = json.loads(hp.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def add_history(entry: dict, path=None) -> None:
    """记录一条作业历史（保留最近 50 条）。"""
    hp = Path(path) if path else history_path()
    hist = load_history(path)
    record = {"time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    record.update(entry)
    hist.insert(0, record)
    del hist[50:]
    try:
        hp.parent.mkdir(parents=True, exist_ok=True)
        hp.write_text(json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 环境自测（用最小水体系真实跑通 grompp + mdrun）
# ---------------------------------------------------------------------------
def write_test_water_system(dest: Path) -> Path:
    """构造最小 SPC/E 水体系（27 个水分子），返回 gromacs 目录。"""
    gdir = dest / "gromacs"
    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / "topol.top").write_text(
        '#include "oplsaa.ff/forcefield.itp"\n#include "oplsaa.ff/spce.itp"\n\n'
        "[ system ]\nMDWF ENV SELF-TEST\n\n[ molecules ]\nSOL 27\n", encoding="utf-8")
    lines = ["WATER TEST", "81"]
    idx = 1
    for m in range(27):
        x = (m % 3) * 0.75 + 0.2
        y = ((m // 3) % 3) * 0.75 + 0.2
        z = (m // 9) * 0.75 + 0.2
        for an, off in (("OW", (0, 0, 0)), ("HW1", (0.05, 0, 0)),
                        ("HW2", (-0.02, 0.045, 0))):
            lines.append(f"{m + 1:5d}SOL{an:>6s}{idx:5d}"
                         f"{x + off[0]:8.3f}{y + off[1]:8.3f}{z + off[2]:8.3f}")
            idx += 1
    lines.append(" 2.70000   2.70000   2.70000")
    (gdir / "step5_input.gro").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return gdir


def self_test(backend: Backend, gmx_exe: str, on_output: OnOutput = _null_out,
              work_dir: Optional[Path] = None) -> Tuple[bool, str]:
    """真实运行一次 grompp + mdrun（能量最小化 20 步），验证环境端到端可用。

    与正式模拟走完全相同的执行链路（GromacsRunner + backend.wrap）。
    """
    from .gmx import GromacsRunner
    from .logger import MDLogger

    work = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="mdwf_envtest_"))
    logger = MDLogger()
    logger.add_callback(lambda level, msg: on_output(f"[{level}] {msg}"))
    runner = GromacsRunner(logger, backend=backend, dry_run=False)
    try:
        gdir = write_test_water_system(work)
        em_dir = work / "01_em"
        em_dir.mkdir(exist_ok=True)
        (em_dir / "em.mdp").write_text(
            "integrator = steep\nemtol = 1000\nemstep = 0.01\nnsteps = 20\n"
            "nstlist = 10\ncutoff-scheme = Verlet\npbc = xyz\n", encoding="utf-8")

        def rel(p: Path) -> str:
            return os.path.relpath(str(p), str(em_dir)).replace("\\", "/")

        on_output("环境自测：执行真实 grompp …")
        rc = runner.run_cmd(
            [gmx_exe, "grompp", "-f", "em.mdp",
             "-c", rel(gdir / "step5_input.gro"),
             "-p", rel(gdir / "topol.top"), "-o", "md.tpr", "-maxwarn", "10"],
            em_dir)
        if rc != 0 or not (em_dir / "md.tpr").exists():
            return False, "grompp 失败（详见日志）"

        on_output("环境自测：执行真实 mdrun（20 步最小化）…")

        def on_progress(frac: float, elapsed: float, eta: str) -> None:
            on_output(f"  最小化进度 {frac * 100:.0f}%，已用 {elapsed:.0f} 秒")

        rc = runner.mdrun([gmx_exe, "mdrun", "-deffnm", "md", "-v"],
                          em_dir, 20, on_progress)
        mdlog = em_dir / "md.log"
        if rc != 0 or not mdlog.exists():
            return False, "mdrun 失败（详见日志）"
        if "Finished mdrun" not in mdlog.read_text(encoding="utf-8", errors="replace"):
            return False, "mdrun 未正常结束"
        return True, "环境自测通过：grompp + mdrun 真实运行成功（后端可用）"
    except Exception as exc:
        return False, f"环境自测异常: {exc}"
