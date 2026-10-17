# -*- coding: utf-8 -*-
"""全局配置：常量、模拟类型、回退默认参数、gmx 命令构建、运行后端。"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List

APP_NAME = "MD Workflow Manager"
APP_VERSION = "0.4.0"

# ---- 模拟类型 ----
ALL_ATOM = "all_atom"
COARSE_GRAINED = "coarse_grained"
AUTO = "auto"

SIM_TYPE_NAMES: Dict[str, str] = {
    ALL_ATOM: "全原子 (All-atom)",
    COARSE_GRAINED: "粗粒化 (Coarse-grained / Martini)",
}

# 力场名关键字 → 模拟类型（顺序匹配，命中即返回）
FF_TYPE_RULES: List[tuple] = [
    ("martini", COARSE_GRAINED),
    ("cg", COARSE_GRAINED),
]

# ---- 阶段类型 ----
KIND_EM = "em"
KIND_EQUIL = "equil"
KIND_PRODUCTION = "production"

KIND_NAMES: Dict[str, str] = {
    KIND_EM: "能量最小化",
    KIND_EQUIL: "平衡",
    KIND_PRODUCTION: "生产模拟",
}

# ---- GROMACS ----
GMX_EXE_DEFAULT = "gmx"
MAX_WARN = 10  # grompp -maxwarn，避免交互式提问导致挂起

# ---- 运行后端 ----
BACKEND_LOCAL = "local"      # 本机直接执行 gmx
BACKEND_WSL = "wsl"          # Windows 界面 + WSL2 Linux 后端

# ---- 环境自动安装（micromamba + conda-forge）----
ENV_NAME = "md"              # 软件创建的 conda 环境名
CONDA_CHANNELS_DEFAULT = ["conda-forge"]
MICROMAMBA_LINUX_URL = "https://micro.mamba.pm/api/micromamba/linux-64/latest"
MICROMAMBA_WIN_URL = "https://micro.mamba.pm/api/micromamba/win-64/latest"

# 国内镜像（可选，填入后自动安装走镜像加速）
CONDA_MIRROR_URL = "https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud"


def app_config_dir() -> Path:
    """软件配置目录（~/.mdwf）。"""
    return Path.home() / ".mdwf"


def config_path() -> Path:
    return app_config_dir() / "config.json"

# ---- 回退默认参数（当 mdp 读不到某参数时使用）----
FALLBACK_PARAMS: Dict[str, Dict[str, object]] = {
    ALL_ATOM: {
        "dt_ps": 0.002,            # 2 fs
        "temperature_k": 310.0,
        "pressure_bar": 1.0,
        "nstxout_compressed": 5000,
        "nstenergy": 5000,
    },
    COARSE_GRAINED: {
        "dt_ps": 0.020,            # Martini 20 fs
        "temperature_k": 310.0,
        "pressure_bar": 1.0,
        "nstxout_compressed": 5000,
        "nstenergy": 5000,
    },
}

# 内置模板生成时各阶段默认时长（ps）
TEMPLATE_DURATION_PS: Dict[str, int] = {
    KIND_EM: 10,
    KIND_EQUIL: 100,
    KIND_PRODUCTION: 100000,  # 100 ns
}

# 内置模板生成时的默认步数
TEMPLATE_NSTEPS: Dict[str, int] = {
    KIND_EM: 5000,
    KIND_EQUIL: 50000,
    KIND_PRODUCTION: 50000000,
}


def build_grompp_cmd(gmx_exe: str, f, c=None, r=None, t=None, p=None, o=None,
                     n=None, maxwarn: int = MAX_WARN) -> List[str]:
    """构建 gmx grompp 命令。"""
    cmd = [gmx_exe, "grompp", "-f", str(f)]
    if c is not None:
        cmd += ["-c", str(c)]
    if r is not None:
        cmd += ["-r", str(r)]
    if t is not None:
        cmd += ["-t", str(t)]
    if p is not None:
        cmd += ["-p", str(p)]
    if o is not None:
        cmd += ["-o", str(o)]
    if n is not None:
        cmd += ["-n", str(n)]
    if maxwarn:
        cmd += ["-maxwarn", str(maxwarn)]
    return cmd


def build_mdrun_cmd(gmx_exe: str, deffnm: str, ntomp: int = 0,
                    use_gpu: bool = False, use_pme_gpu: bool = False) -> List[str]:
    """构建 gmx mdrun 命令。

    非绑电任务固定加 -nb gpu。PME 是否也上 GPU（-pme gpu）由 use_pme_gpu
    决定：仅全原子且动力学（非 EM）阶段才建议开启——Martini 体系用
    reaction-field 静电（无 PME），EM 是非动力学积分器，这两种情况加
    -pme gpu 会直接报错。开启后全原子生产模拟可获明显加速；若该 gmx
    不支持 GPU-PME，工作流会自动回退 CPU 重跑本阶段。
    """
    cmd = [gmx_exe, "mdrun", "-deffnm", str(deffnm)]
    if ntomp and ntomp > 0:
        cmd += ["-ntomp", str(ntomp)]
    if use_gpu:
        cmd += ["-nb", "gpu"]
        if use_pme_gpu:
            cmd += ["-pme", "gpu"]
    cmd += ["-v"]
    return cmd
