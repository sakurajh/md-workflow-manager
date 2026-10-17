# -*- coding: utf-8 -*-
"""mdp 文件处理：解析、参数覆盖（override）、内置模板生成。"""
from __future__ import annotations

import re
from typing import Dict, Optional

from . import config

# 匹配 "键 = 值 ; 注释"
PARAM_RE = re.compile(r"^\s*([A-Za-z0-9_\-]+)\s*=\s*(.*?)(?:\s*;.*)?$")
COMMENT_TAIL_RE = re.compile(r"(\s*;.*)$")


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def parse_mdp_from_text(text: str) -> Dict[str, str]:
    """把 mdp 文本解析为 {小写键: 值} 字典（忽略注释与 #include）。"""
    params: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(";") or line.startswith("#"):
            continue
        m = PARAM_RE.match(raw)
        if m:
            params[m.group(1).lower()] = m.group(2).strip()
    return params


def parse_mdp(path) -> Dict[str, str]:
    """读取 mdp 文件为 {小写键: 值} 字典（忽略注释与 #include）。"""
    if path is None or not hasattr(path, "exists") or not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}
    return parse_mdp_from_text(text)


def get(params: Dict[str, str], key: str, default=None):
    return params.get(key.lower(), default)


def to_float(params: Dict[str, str], key: str, default: Optional[float]) -> Optional[float]:
    try:
        return float(params.get(key.lower(), default))
    except (TypeError, ValueError):
        return default


def first_float(params: Dict[str, str], key: str, default: Optional[float]) -> Optional[float]:
    """取多值参数（如 ref_t = 310 310）的第一个数值。"""
    raw = get(params, key, "")
    try:
        return float(str(raw).split()[0])
    except (TypeError, ValueError, IndexError):
        return default


def to_int(params: Dict[str, str], key: str, default: Optional[int]) -> Optional[int]:
    try:
        return int(float(params.get(key.lower(), default)))
    except (TypeError, ValueError):
        return default


def stage_duration_ps(params: Dict[str, str]) -> float:
    """该阶段总时长（ps）= nsteps × dt。"""
    dt = to_float(params, "dt", 0.002)
    nsteps = to_int(params, "nsteps", 0)
    return (nsteps or 0) * (dt or 0.0)


# ---------------------------------------------------------------------------
# 覆盖
# ---------------------------------------------------------------------------
def _fmt_float(v: float) -> str:
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return s or "0"


def _ref_values(params: Dict[str, str], key: str, value: float) -> str:
    """ref_t / ref_p 可能含多个值（如两套温度组），全部替换为用户值。"""
    n = len(get(params, key, "").split())
    n = max(n, 1)
    return " ".join([_fmt_float(value)] * n)


def make_override_map(cfg: Dict[str, object], params: Dict[str, str],
                      sim_type: str) -> Dict[str, str]:
    """把界面配置 cfg 转成 mdp 参数覆盖表。

    cfg 支持的键：
        duration_ps     阶段时长(ps)，换算为 nsteps
        nsteps          直接指定步数
        dt_ps           步长
        temperature_k   温度（覆盖 ref_t 全部值）
        pressure_bar    压力（覆盖 ref_p 全部值）
        restraints      是否启用 -DPOSRES 位置限制（True/False）
        emtol           最小化力收敛阈值
        nstxout_compressed / nstenergy   输出频率
        tcoupl / pcoupl 恒温器 / 恒压器
    """
    overrides: Dict[str, str] = {}

    dt = cfg.get("dt_ps")
    if dt is None:
        dt = to_float(params, "dt", None)
    if dt is None:
        dt = float(config.FALLBACK_PARAMS[sim_type]["dt_ps"])

    duration = cfg.get("duration_ps")
    if duration is not None and dt and dt > 0:
        overrides["nsteps"] = str(max(int(round(float(duration) / dt)), 1))
    elif cfg.get("nsteps") is not None:
        overrides["nsteps"] = str(int(cfg["nsteps"]))

    if cfg.get("dt_ps") is not None:
        overrides["dt"] = _fmt_float(float(cfg["dt_ps"]))

    temp = cfg.get("temperature_k")
    if temp is not None:
        overrides["ref_t"] = _ref_values(params, "ref_t", float(temp))

    press = cfg.get("pressure_bar")
    if press is not None:
        overrides["ref_p"] = _ref_values(params, "ref_p", float(press))

    if cfg.get("nstxout_compressed") is not None:
        overrides["nstxout-compressed"] = str(int(cfg["nstxout_compressed"]))
    if cfg.get("nstenergy") is not None:
        overrides["nstenergy"] = str(int(cfg["nstenergy"]))
    if cfg.get("emtol") is not None:
        overrides["emtol"] = _fmt_float(float(cfg["emtol"]))
    if cfg.get("tcoupl") is not None:
        overrides["tcoupl"] = str(cfg["tcoupl"])
    if cfg.get("pcoupl") is not None:
        overrides["pcoupl"] = str(cfg["pcoupl"])

    restraints = cfg.get("restraints")
    if restraints is not None:
        define = get(params, "define", "")
        has_posres = "-DPOSRES" in str(define).upper()
        if restraints and not has_posres:
            new_define = (str(define) + " -DPOSRES").strip()
            overrides["define"] = new_define or "-DPOSRES"
        elif not restraints and has_posres:
            cleaned = " ".join(x for x in str(define).split() if x.upper() != "-DPOSRES")
            overrides["define"] = cleaned

    return overrides


def apply_overrides(text: str, overrides: Dict[str, str]) -> str:
    """按键覆盖 mdp 参数行（保留行内注释）；不存在的键追加到文件末尾。"""
    if not overrides:
        return text
    keys = {k.lower(): v for k, v in overrides.items()}
    done = set()
    out = []
    for raw in text.splitlines():
        m = PARAM_RE.match(raw)
        key = m.group(1).lower() if m else None
        if key in keys:
            comment = ""
            cm = COMMENT_TAIL_RE.search(raw)
            if cm:
                comment = cm.group(1)
            out.append(f"{m.group(1)} = {keys[key]}{comment}")
            done.add(key)
        else:
            out.append(raw)
    for k, v in overrides.items():
        if k.lower() not in done:
            out.append(f"{k} = {v}")
    return "\n".join(out) + "\n"


def estimate_nsteps(cfg: Dict[str, object], params: Dict[str, str], sim_type: str) -> int:
    """估算某阶段的总步数（用于整体进度权重，运行前即可计算）。"""
    dt = cfg.get("dt_ps")
    if dt is None:
        dt = to_float(params, "dt", None)
    if dt is None:
        dt = float(config.FALLBACK_PARAMS[sim_type]["dt_ps"])
    duration = cfg.get("duration_ps")
    if duration is not None and dt and dt > 0:
        return max(int(round(float(duration) / dt)), 1)
    nsteps = to_int(params, "nsteps", None)
    if nsteps:
        return nsteps
    return 1


# ---------------------------------------------------------------------------
# GROMACS 版本兼容：软核扰动（free-energy）与 reaction-field
# ---------------------------------------------------------------------------
# CHARMM-GUI Martini v2 的 mdp 用 free-energy 软核扰动做逐步加力；
# GROMACS 2023.4+ 遇到 "扰动+排除对超出 reaction-field 截断" 会直接报错。
# 兼容方案：禁用 free-energy（跳过逐步加力技巧），任何版本都能跑。
FREE_ENERGY_KEYS = {
    "free-energy", "init-lambda", "init-lambda-state", "init-lambda0",
    "init-lambda1", "delta-lambda", "couple-lambda0", "couple-lambda1",
    "couple-moltype", "couple-intramol", "nstdhdl", "dhdl-print-energy",
    "separable-dhdl-file", "dh-deriv", "sc-alpha", "sc-power", "sc-sigma",
    "sc-coul", "sc-r-power", "sc-calcs-per-step", "b-state", "b-fact",
}


def disable_free_energy(text: str) -> str:
    """移除 mdp 中所有自由能/软核扰动相关键，并显式设 free-energy = no。"""
    out = []
    dropped = False
    for raw in text.splitlines():
        m = PARAM_RE.match(raw)
        if m and m.group(1).lower() in FREE_ENERGY_KEYS:
            dropped = True
            continue
        out.append(raw)
    if dropped:
        out.append("free-energy = no")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# 内置模板（mdp 文件缺失时兜底）
# ---------------------------------------------------------------------------
def generate_fallback_mdp(kind: str, sim_type: str) -> str:
    """按阶段类型与模拟类型生成可用的 mdp 文本。"""
    aa = sim_type != config.COARSE_GRAINED
    dt = 0.002 if aa else 0.020
    constraints = "h-bonds" if aa else "none"

    if kind == config.KIND_EM:
        return (
            "; Auto-generated minimization input (MD Workflow Manager)\n"
            "integrator = steep\n"
            "emtol = 1000\n"
            "emstep = 0.01\n"
            f"nsteps = {config.TEMPLATE_NSTEPS[config.KIND_EM]}\n"
            "nstlist = 10\n"
            "cutoff-scheme = Verlet\n"
            "ns_type = grid\n"
            "pbc = xyz\n"
        )

    electro = (
        "coulombtype = PME\nrcoulomb = 1.2\nfourierspacing = 0.12\n"
        if aa else
        "vdwtype = Cut-off\nvdw-modifier = Potential-shift\nrvdw = 1.1\n"
        "coulombtype = Reaction-Field\nrcoulomb = 1.1\nepsilon_r = 15\n"
    )

    if kind == config.KIND_PRODUCTION:
        nsteps = config.TEMPLATE_NSTEPS[config.KIND_PRODUCTION]
        return (
            "; Auto-generated production input (MD Workflow Manager)\n"
            "integrator = md\n"
            f"nsteps = {nsteps}\n"
            f"dt = {_fmt_float(dt)}\n"
            "nstlog = 5000\nnstenergy = 5000\nnstxout-compressed = 50000\n"
            "nstlist = 10\ncutoff-scheme = Verlet\nns_type = grid\npbc = xyz\n"
            + electro +
            "tcoupl = v-rescale\ntc-grps = System\ntau_t = 1.0\nref_t = 310\n"
            "pcoupl = Parrinello-Rahman\npcoupltype = isotropic\ntau_p = 2.0\n"
            "ref_p = 1.0\ncompressibility = 4.5e-5\n"
            f"constraints = {constraints}\n"
        )

    # 平衡阶段（默认 NVT + 位置限制）
    nsteps = config.TEMPLATE_NSTEPS[config.KIND_EQUIL]
    return (
        "; Auto-generated equilibration input (MD Workflow Manager)\n"
        "integrator = md\n"
        "define = -DPOSRES\n"
        f"nsteps = {nsteps}\n"
        f"dt = {_fmt_float(dt)}\n"
        "nstlog = 1000\nnstenergy = 1000\nnstxout-compressed = 5000\n"
        "nstlist = 10\ncutoff-scheme = Verlet\nns_type = grid\npbc = xyz\n"
        + electro +
        "tcoupl = v-rescale\ntc-grps = System\ntau_t = 1.0\nref_t = 310\n"
        "pcoupl = no\n"
        f"constraints = {constraints}\n"
    )
