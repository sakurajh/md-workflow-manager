# -*- coding: utf-8 -*-
"""CHARMM-GUI 输出识别：目录定位、力场解析、全原子/粗粒化判定、阶段 mdp 发现。"""
from __future__ import annotations

import os
import re
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from . import config
from .mdp import parse_mdp

# 匹配形如 step6.0_minimization.mdp / step6.6_production.mdp / step7_production.mdp 的文件名
# （注意：Martini 旧版输出为 step7_production.mdp，没有小数点）
STAGE_FILE_RE = re.compile(r"step(\d+)(?:\.(\d+))?(?:_([a-zA-Z0-9]+))?\.mdp$", re.I)

# 拓扑文件可能的名字（新版 CHARMM-GUI 用 topol.top，Martini/旧版用 system.top）
TOPOL_NAMES = ("topol.top", "system.top")

# 结构文件可能的名字
STRUCTURE_NAMES = ("step5_input.gro", "step5_input.pdb", "step5_input.crd",
                   "step5_charmm2gmx.pdb", "step5_charmm2gmx.gro")


@dataclass
class Stage:
    """一个模拟阶段（能量最小化 / 平衡 / 生产）。"""
    index: int
    key: str                 # 例如 "step6.1"
    name: str                # 界面显示名
    kind: str                # em / equil / production
    mdp_path: Optional[Path] = None
    parsed: dict = field(default_factory=dict)


@dataclass
class DetectResult:
    ok: bool = False
    gromacs_dir: Optional[Path] = None
    topol_path: Optional[Path] = None
    topol_missing: bool = False
    structure_path: Optional[Path] = None
    ff_name: Optional[str] = None
    sim_type: Optional[str] = None
    sim_type_name: str = ""
    stages: List[Stage] = field(default_factory=list)
    atoms: Optional[int] = None
    martini_v2: bool = False
    warnings: List[str] = field(default_factory=list)
    error: str = ""


# ---------------------------------------------------------------------------
# 目录定位
# ---------------------------------------------------------------------------
def _find_topol(p: Path) -> Optional[Path]:
    for n in TOPOL_NAMES:
        cand = p / n
        if cand.exists():
            return cand
    return None


def _find_structure(p: Path) -> Optional[Path]:
    for n in STRUCTURE_NAMES:
        cand = p / n
        if cand.exists():
            return cand
    return None


def _looks_like_gromacs_dir(p: Path) -> bool:
    """判断目录是否为 CHARMM-GUI 的 GROMACS 输出。

    兼容：topol.top 或 system.top；结构文件 step5_input.* 或 step5_charmm2gmx.*。
    """
    if not p.is_dir():
        return False
    has_topol = _find_topol(p) is not None
    has_structure = _find_structure(p) is not None
    has_mdp = bool(list(p.glob("step*.mdp")))
    has_toppar = (p / "toppar").is_dir()
    if has_topol and (has_mdp or has_structure):
        return True
    # 兼容缺少拓扑文件（需先运行 gen_gromacstop.sh）但其余文件齐全的目录
    if has_mdp and (has_structure or has_toppar):
        return True
    return False


def find_gromacs_dir(selected: str) -> Optional[Path]:
    """定位 CHARMM-GUI 的 GROMACS 输出目录：
    1) 所选目录本身就是；2) 其下 gromacs/ 子目录；3) 浅扫一层子目录。"""
    p = Path(selected)
    if not p.exists():
        return None
    if _looks_like_gromacs_dir(p):
        return p
    cand = p / "gromacs"
    if _looks_like_gromacs_dir(cand):
        return cand
    try:
        for child in sorted(p.iterdir()):
            if child.is_dir() and _looks_like_gromacs_dir(child):
                return child
    except OSError:
        pass
    return None


def _find_gromacs_dir_in_tree(root: Path, max_depth: int = 8) -> Optional[Path]:
    """在目录树中查找含 GROMACS 输出的目录。

    策略：先按目录 BFS（限深），再用文件级兜底搜索（不限深）：
    topol.top 或 step6.*.mdp 所在目录。
    """
    queue_list = [root]
    depth = 0
    while queue_list and depth <= max_depth:
        nxt = []
        for p in queue_list:
            if _looks_like_gromacs_dir(p):
                return p
            if depth < max_depth:
                try:
                    nxt.extend(c for c in p.iterdir() if c.is_dir())
                except OSError:
                    pass
        queue_list = nxt
        depth += 1
    # 文件级兜底：找 topol.top
    try:
        for dirpath, _, filenames in os.walk(root):
            if "topol.top" in filenames:
                p = Path(dirpath)
                if _looks_like_gromacs_dir(p):
                    return p
    except OSError:
        pass
    # 兜底：找 step6.*.mdp 所在目录
    try:
        for dirpath, _, filenames in os.walk(root):
            for fn in filenames:
                if STAGE_FILE_RE.match(fn):
                    return Path(dirpath)
    except OSError:
        pass
    return None


def _strip_archive_ext(name: str) -> str:
    """去掉压缩包扩展名得到目录名（正确去掉 .tar.gz 两层扩展）。"""
    low = name.lower()
    for ext in (".tar.gz", ".tar.bz2", ".tar.xz", ".zip", ".tgz", ".gz", ".bz2", ".xz", ".tar"):
        if low.endswith(ext):
            return name[: -len(ext)]
    return Path(name).stem


@dataclass
class ExtractResult:
    """压缩包解压结果。"""
    ok: bool = False
    extracted_dir: Optional[Path] = None
    gromacs_dir: Optional[Path] = None
    hint: str = ""          # 供界面展示的诊断信息


_ARCHIVE_EXTS = (".zip", ".tgz", ".tar.gz", ".tar.bz2", ".tar.xz", ".tar")


def extract_charmmgui_archive(archive_path: str,
                              dest_root: Optional[Path] = None) -> ExtractResult:
    """解压 CHARMM-GUI 的压缩包（zip / tgz / tar.gz / tar）。

    - 自动处理嵌套压缩包（如 zip 内含 tgz，最多递归 2 层）
    - 返回 ExtractResult，找不到时附带诊断信息（解压目录、内容、线索）
    """
    ap = Path(archive_path)
    if not ap.exists():
        return ExtractResult(ok=False, hint=f"文件不存在: {archive_path}")
    if not ap.name.lower().endswith(_ARCHIVE_EXTS):
        return ExtractResult(ok=False, hint=f"不支持的文件类型: {ap.name}（支持 zip/tgz/tar.gz/tar）")
    root = Path(dest_root) if dest_root else \
        config.app_config_dir() / "imports" / _strip_archive_ext(ap.name)
    root.mkdir(parents=True, exist_ok=True)

    try:
        _extract_one(ap, root)
        gdir = _find_gromacs_dir_in_tree(root)
        # 未找到 → 可能是嵌套压缩包，尝试再解一层
        if gdir is None:
            nested = _find_nested_archives(root)
            for _ in range(2):
                if not nested:
                    break
                _extract_one(nested[0], root)
                gdir = _find_gromacs_dir_in_tree(root)
                if gdir is not None:
                    break
                nested = _find_nested_archives(root)
    except (zipfile.BadZipFile, tarfile.TarError, OSError) as exc:
        return ExtractResult(ok=False, extracted_dir=root,
                             hint=f"解压失败: {exc}\n\n请确认这是 CHARMM-GUI 下载的有效压缩包。")

    if gdir is not None:
        return ExtractResult(ok=True, extracted_dir=root, gromacs_dir=gdir)
    return ExtractResult(ok=False, extracted_dir=root,
                         hint=_describe_extraction(root))


def _extract_one(ap: Path, root: Path) -> None:
    """安全解压单个压缩包到 root（含路径穿越防护）。"""
    if ap.name.lower().endswith(".zip"):
        with zipfile.ZipFile(ap) as zf:
            for info in zf.infolist():
                _check_member(root, info.filename)
            zf.extractall(root)
    else:
        if not tarfile.is_tarfile(str(ap)):
            raise tarfile.TarError(f"不是有效的 tar/tgz 压缩包: {ap.name}")
        with tarfile.open(ap) as tf:
            for member in tf.getmembers():
                _check_member(root, member.name)
            tf.extractall(root)


def _check_member(root: Path, member_name: str) -> None:
    target = root / member_name
    try:
        target.resolve().relative_to(root.resolve())
    except ValueError:
        raise ValueError(f"压缩包内含非法路径: {member_name}")


def _find_nested_archives(root: Path) -> List[Path]:
    """查找目录树中的压缩包文件（用于处理嵌套压缩）。"""
    out = []
    try:
        for p in root.rglob("*"):
            if p.is_file() and p.name.lower().endswith(_ARCHIVE_EXTS):
                out.append(p)
    except OSError:
        pass
    return sorted(out)


def _describe_extraction(root: Path) -> str:
    """生成解压诊断信息：解压目录、内容、有无 topol/step 文件。"""
    lines = ["未在压缩包中找到 GROMACS 输出目录（需要 topol.top 和 step6*.mdp）。", ""]
    lines.append(f"解压目录: {root}")
    if not root.exists():
        lines.append("（解压目录不存在，可能未解压成功）")
        return "\n".join(lines)
    entries = sorted(p.name for p in root.iterdir())
    if not entries:
        lines.append("解压后目录为空，压缩包内可能没有内容。")
        return "\n".join(lines)
    lines.append(f"解压出 {len(entries)} 个条目，前 15 个:")
    lines.append("  " + "、".join(entries[:15]))
    topols = [str(p.relative_to(root)) for p in root.rglob("topol.top")][:5]
    mdps = [str(p.relative_to(root)) for p in root.rglob("step6*.mdp")][:5]
    if topols:
        lines.append(f"找到 topol.top: {topols}")
    else:
        lines.append("未找到 topol.top 文件。")
    if mdps:
        lines.append(f"找到 step6*.mdp: {mdps}")
    lines.append("")
    lines.append("提示：请在 CHARMM-GUI 里选择 Gromacs 程序下载的压缩包；"
                 "若包内还有压缩包，本软件会自动解开。")
    return "\n".join(lines)


def extract_charmmgui_zip(zip_path: str, dest_root: Optional[Path] = None) -> ExtractResult:
    """兼容旧名称：等同于 extract_charmmgui_archive。"""
    return extract_charmmgui_archive(zip_path, dest_root)


# ---------------------------------------------------------------------------
# 力场 / 模拟类型
# ---------------------------------------------------------------------------
def read_forcefield(topol: Path) -> Optional[str]:
    """从 topol.top 的 #include 行识别力场名，例如 charmm36.ff / martini_v3.0.0。"""
    try:
        text = topol.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("#include"):
            continue
        m = re.search(r'#include\s+"([^"]+)"', line)
        if not m:
            continue
        inc = m.group(1)
        low = inc.lower()
        if "martini" in low:
            return "martini"
        if ".ff" in low:
            parts = Path(inc.replace("\\", "/")).parts
            return parts[-2] if len(parts) >= 2 else parts[-1]
    return None


def classify_sim_type(ff_name: Optional[str]) -> Optional[str]:
    """根据力场名判定模拟类型；无命中时默认全原子。"""
    if not ff_name:
        return None
    low = ff_name.lower()
    for keyword, sim_type in config.FF_TYPE_RULES:
        if keyword in low:
            return sim_type
    return config.ALL_ATOM


def count_atoms(struct: Optional[Path]) -> Optional[int]:
    """统计结构文件中的原子数：.gro 读第二行；.pdb 数 ATOM/HETATM 行。"""
    if not struct or not struct.exists():
        return None
    try:
        lines = struct.read_text(encoding="utf-8", errors="replace").splitlines()
        if struct.suffix.lower() == ".gro":
            if len(lines) >= 2:
                return int(lines[1].strip())
        else:  # pdb / crd
            n = sum(1 for ln in lines if ln.startswith(("ATOM  ", "HETATM")))
            if n > 0:
                return n
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# 阶段发现
# ---------------------------------------------------------------------------
def _stage_display_name(kind: str, key: str) -> str:
    return f"{key} {config.KIND_NAMES.get(kind, kind)}"


def discover_stages(gdir: Path) -> List[Stage]:
    """扫描目录下 step6.*.mdp / step7*.mdp，按顺序归类为 em / equil / production。

    阶段类型按文件名关键词判定（minim→EM，equil→平衡，production→生产），
    兼容全原子（step6.6_production）与 Martini（step7_production）两种命名。
    """
    found = []
    for mdp_path in gdir.glob("*.mdp"):
        m = STAGE_FILE_RE.match(mdp_path.name)
        if not m:
            continue
        major = int(m.group(1))
        minor = int(m.group(2)) if m.group(2) else 0
        desc = (m.group(3) or "").lower()
        if "minim" in desc:
            kind = config.KIND_EM
        elif "production" in desc or "prod" in desc:
            kind = config.KIND_PRODUCTION
        elif "equil" in desc:
            kind = config.KIND_EQUIL
        elif major >= 7:
            kind = config.KIND_PRODUCTION
        elif major == 6 and minor == 0:
            kind = config.KIND_EM
        else:
            kind = config.KIND_EQUIL
        key = f"step{major}.{minor}" if m.group(2) else f"step{major}"
        found.append((major, minor, kind, key, mdp_path))
    found.sort(key=lambda x: (x[0], x[1]))
    stages = []
    for i, (major, minor, kind, key, mdp_path) in enumerate(found):
        stage = Stage(index=i, key=key, name=_stage_display_name(kind, key),
                      kind=kind, mdp_path=mdp_path)
        stage.parsed = parse_mdp(mdp_path)
        stages.append(stage)
    return stages


def default_stages() -> List[Stage]:
    """未发现任何 step6.*.mdp 时的兜底阶段序列（使用内置模板）。"""
    specs = [(0, config.KIND_EM)] + [(i, config.KIND_EQUIL) for i in range(1, 6)] \
        + [(6, config.KIND_PRODUCTION)]
    return [Stage(index=i, key=f"step6.{minor}", name=_stage_display_name(kind, f"step6.{minor}"),
                  kind=kind, mdp_path=None) for i, (minor, kind) in enumerate(specs)]


# ---------------------------------------------------------------------------
# 总入口
# ---------------------------------------------------------------------------
def detect(selected: str) -> DetectResult:
    """对用户选择的目录（或压缩包文件）执行完整检测。"""
    res = DetectResult(ok=False)
    p = Path(selected)

    # 如果用户直接给的是压缩包文件（tgz/zip/tar.gz），自动解压后检测
    if p.is_file() and p.name.lower().endswith(_ARCHIVE_EXTS):
        ex = extract_charmmgui_archive(str(p))
        if not ex.ok or ex.gromacs_dir is None:
            res.error = "压缩包未能定位到 GROMACS 输出目录。\n\n" + ex.hint
            return res
        selected = str(ex.gromacs_dir)

    gdir = find_gromacs_dir(selected)
    if gdir is None:
        res.error = ("未找到 CHARMM-GUI 的 GROMACS 输出目录。\n"
                     "请选择包含拓扑文件（topol.top 或 system.top）和 step6*.mdp 的文件夹"
                     "（即 CHARMM-GUI 输出的 Gromacs 文件夹或其父目录）；\n"
                     "如果是压缩包（tgz/zip），可直接把压缩包路径填入此处，软件会自动解压。")
        return res

    res.gromacs_dir = gdir
    res.topol_path = _find_topol(gdir)
    res.topol_missing = res.topol_path is None
    if res.topol_missing:
        res.warnings.append(
            "未找到拓扑文件（topol.top / system.top）。真实模拟前需要先运行压缩包内的"
            " gen_gromacstop.sh 生成拓扑文件（或用 CHARMM 运行 gen_gromacstop.str），"
            "再重新检测；当前仅支持 dry-run 体验。")

    res.structure_path = _find_structure(gdir)

    res.ff_name = read_forcefield(res.topol_path) if res.topol_path else None
    res.sim_type = classify_sim_type(res.ff_name)
    res.sim_type_name = config.SIM_TYPE_NAMES.get(res.sim_type or "", "未知")
    if res.topol_path and res.topol_path.exists():
        try:
            _t = res.topol_path.read_text(encoding="utf-8", errors="replace").lower()
            _martini_v2 = ("martini_v2" in _t or "martini-v2" in _t)
            _martini_v3 = ("martini_v3" in _t or "martini-v3" in _t or "martini3" in _t)
        except OSError:
            _martini_v2 = _martini_v3 = False
        res.martini_v2 = bool(_martini_v2 and not _martini_v3)
        if res.martini_v2:
            res.warnings.append(
                "检测到 Martini v2 力场：GROMACS 2023.4+ 会报软核扰动兼容性错误，"
                "软件运行时会自动禁用 free-energy 重试以兼容任何版本"
                "（2023.3 为官方推荐版本）。")

    res.stages = discover_stages(gdir)
    if not res.stages:
        res.warnings.append("未发现 step6*.mdp 阶段文件，将使用内置模板生成各阶段参数。")
        res.stages = default_stages()

    res.atoms = count_atoms(res.structure_path)
    if res.ff_name is None:
        res.warnings.append("未能识别力场，请在“模拟类型”中手动选择。")

    res.ok = True
    return res
