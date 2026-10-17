# -*- coding: utf-8 -*-
"""工作流编排：能量最小化 → 平衡(NVT/NPT) → 生产模拟，产出作业目录与运行汇总。

通过 emit 回调向上层（GUI / 测试）推送事件：
    log / stage_list / stage_start / progress / stage_done /
    stage_fail / error / finished / canceled
"""
from __future__ import annotations

import datetime
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import config
from . import mdp as mdp_mod
from .backend import Backend, LocalBackend
from .detect import DetectResult, Stage
from .gmx import GromacsRunner
from .logger import MDLogger

# 事件类型
EV_LOG = "log"
EV_STAGE_LIST = "stage_list"
EV_STAGE_START = "stage_start"
EV_PROGRESS = "progress"
EV_STAGE_DONE = "stage_done"
EV_STAGE_FAIL = "stage_fail"
EV_ERROR = "error"
EV_FINISHED = "finished"
EV_CANCELED = "canceled"


@dataclass
class JobResult:
    ok: bool = False
    canceled: bool = False
    failed_stage: Optional[str] = None
    output_dir: Optional[Path] = None


class Workflow:
    """MD 模拟工作流。

    run(start_from) 从第 start_from 个阶段开始执行；重复调用会复用
    已创建的作业目录，并从已有输出文件重建 -c/-t 链（支持断点续跑）。
    """

    def __init__(self, detect: DetectResult, sim_type: str, output_root: Path,
                 gmx_exe: str = config.GMX_EXE_DEFAULT, ntomp: int = 0,
                 use_gpu: bool = False, dry_run: bool = False,
                 dry_sleep: float = 0.02,
                 backend: Optional[Backend] = None,
                 stage_configs: Optional[Dict[str, dict]] = None,
                 logger: Optional[MDLogger] = None,
                 emit: Optional[Callable[[dict], None]] = None):
        self.detect = detect
        self.sim_type = sim_type
        self.output_root = Path(output_root)
        self.gmx_exe = gmx_exe
        self.ntomp = ntomp
        self.use_gpu = use_gpu
        self.dry_run = dry_run
        self.dry_sleep = dry_sleep
        self.backend = backend or LocalBackend()
        self.stage_configs = stage_configs or {}
        self.logger = logger or MDLogger()
        self.emit = emit or (lambda ev: None)
        self.runner = GromacsRunner(self.logger, backend=self.backend,
                                    dry_run=dry_run, dry_sleep=dry_sleep)
        self.job_dir: Optional[Path] = None
        self.stage_dirs: List[Path] = []
        self.weights: List[int] = []

    @staticmethod
    def _rel(cwd: Path, target: Optional[Path]) -> Optional[str]:
        """目标路径 → 相对 cwd 的路径（正斜杠，本机与 WSL 通用）。"""
        if target is None:
            return None
        return os.path.relpath(str(target), str(cwd)).replace("\\", "/")

    def _is_gpu_error(self) -> bool:
        """根据 mdrun 输出尾部判断是否为 GPU 相关错误。

        注意把多行输出合并成一行再匹配（错误文字可能跨行折行）。
        """
        tail = " ".join(getattr(self.runner, "output_tail", []))
        keys = ("built without GPU support",
                "Cannot compute PME interactions on a GPU",
                "PME GPU does not support",
                "No compatible GPU",
                "no GPU is detected",
                "Cannot run short-ranged nonbonded interactions on a GPU",
                "A GPU device was queried, but none")
        return any(k in tail for k in keys)

    def _is_rf_perturbation_error(self) -> bool:
        """判断是否为 GROMACS 2023.4+ 的软核扰动 + reaction-field 兼容性错误。"""
        tail = " ".join(getattr(self.runner, "output_tail", []))
        return ("excluded and perturbed atom pairs" in tail
                and "reaction-field" in tail)

    def _high_energy_hint(self) -> Optional[str]:
        """初始势能过高时的中文诊断与建议。"""
        tail = " ".join(getattr(self.runner, "output_tail", []))
        if "extremely high" not in tail and "potential energy is" not in tail:
            return None
        return ("初始势能过高（体系未充分松弛）。建议：① 加大能量最小化时长后从本阶段重试；"
                "② 或使用 GROMACS 2023.3 以保留 CHARMM-GUI 软核逐步加力协议")

    # ------------------------------------------------------------------
    def _event(self, ev_type: str, **kw) -> None:
        self.emit({"type": ev_type, **kw})

    def _init_job_dir(self) -> None:
        if self.job_dir is not None:
            return
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.job_dir = self.output_root / f"MD_sim_{stamp}"
        self.job_dir.mkdir(parents=True, exist_ok=True)
        self._copy_inputs()
        self.stage_dirs = [self.job_dir / f"{i + 1:02d}_{st.key}"
                           for i, st in enumerate(self.detect.stages)]
        self.weights = [max(mdp_mod.estimate_nsteps(
            self.stage_configs.get(st.key, {}), st.parsed, self.sim_type), 1)
            for st in self.detect.stages]

    def _copy_inputs(self) -> None:
        assert self.job_dir is not None
        inputs = self.job_dir / "inputs"
        inputs.mkdir(exist_ok=True)
        src = self.detect.gromacs_dir

        def _copy(f: Optional[Path]) -> None:
            if f and f.exists():
                try:
                    shutil.copy2(f, inputs / f.name)
                except OSError as exc:
                    self.logger.warn(f"复制输入文件失败 {f.name}: {exc}")

        _copy(self.detect.topol_path)
        _copy(self.detect.structure_path)
        if src:
            # 拓扑引用的附属文件（.itp / .ndx / .mdp / 其他 .pdb/.gro）一并复制
            for pattern in ("*.itp", "*.top", "*.ndx", "*.mdp", "*.pdb", "*.gro"):
                for f in src.glob(pattern):
                    if f.name not in {self.detect.topol_path.name if self.detect.topol_path else "",
                                      self.detect.structure_path.name if self.detect.structure_path else ""}:
                        _copy(f)
            toppar = src / "toppar"
            if toppar.is_dir() and not (inputs / "toppar").exists():
                shutil.copytree(toppar, inputs / "toppar")
            for cand in src.iterdir():
                if cand.is_dir() and cand.name.lower().endswith(".ff") \
                        and not (inputs / cand.name).exists():
                    shutil.copytree(cand, inputs / cand.name)
        self.logger.info(f"输入文件已复制到: {inputs}")

    def _prev_chain(self, start_from: int):
        """从已完成的阶段目录重建 -c / -t 链。"""
        reference = None
        if self.detect.structure_path:
            ref = self.job_dir / "inputs" / self.detect.structure_path.name
            if ref.exists():
                reference = ref
        prev_gro = reference
        prev_cpt = None
        for i in range(start_from):
            g = self.stage_dirs[i] / "md.gro"
            if g.exists():
                prev_gro = g
                c = self.stage_dirs[i] / "md.cpt"
                prev_cpt = c if c.exists() else None
        return reference, prev_gro, prev_cpt

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def run(self, start_from: int = 0) -> JobResult:
        self.logger.info("=" * 62)
        sim_name = config.SIM_TYPE_NAMES.get(self.sim_type, self.sim_type)
        mode = "dry-run 模拟模式" if self.dry_run else f"真实 GROMACS（后端: {self.backend.display}）"
        self.logger.info(f"开始 MD 模拟工作流 | 模拟类型: {sim_name} | 模式: {mode}")
        self.runner = GromacsRunner(self.logger, backend=self.backend,
                                    dry_run=self.dry_run, dry_sleep=self.dry_sleep)
        self._init_job_dir()
        assert self.job_dir is not None
        self.logger.set_log_file(str(self.job_dir / "md_sim.log"))
        self.logger.info(f"作业目录: {self.job_dir}")

        self._event(EV_STAGE_LIST, stages=[
            {"key": s.key, "name": s.name, "kind": s.kind}
            for s in self.detect.stages])

        reference, prev_gro, prev_cpt = self._prev_chain(start_from)
        topol_copy = None
        if self.detect.topol_path is not None:
            topol_copy = self.job_dir / "inputs" / self.detect.topol_path.name
        # CHARMM-GUI 提供的 index.ndx（mdp 可能引用自定义索引组，如 membrane）
        index_copy = self.job_dir / "inputs" / "index.ndx"
        if not index_copy.exists():
            index_copy = None
        if reference is None and not self.dry_run:
            self.logger.error("缺少结构文件（step5_input.gro/.pdb），无法运行模拟。")
            self._event(EV_ERROR, message="缺少结构文件（step5_input.gro/.pdb），无法运行模拟。",
                        canceled=False)
            return self._finish_failed("结构文件缺失", detail="缺少结构文件")
        if topol_copy is None and not self.dry_run:
            self.logger.error("缺少拓扑文件（topol.top / system.top），无法运行模拟。"
                              "请先运行压缩包内的 gen_gromacstop.sh 生成拓扑文件。")
            self._event(EV_ERROR,
                        message="缺少拓扑文件（topol.top / system.top）。请先运行压缩包内的 "
                                "gen_gromacstop.sh 生成拓扑文件后再检测。",
                        canceled=False)
            return self._finish_failed("拓扑文件缺失", detail="缺少拓扑文件")

        stages = self.detect.stages
        # 整个流程中第一个动力学阶段（EM 之后的首个 NVT/生产）：它从最小化后的
        # 零速度结构起步，必须由 gen-vel 生成初速度。EM 也会写 md.cpt，故不能用
        # 是否有 checkpoint 判断，只能按阶段类型确定。
        _dyn = [k for k, s in enumerate(stages) if s.kind != config.KIND_EM]
        first_dyn_idx = _dyn[0] if _dyn else -1
        for i in range(start_from, len(stages)):
            stage = stages[i]
            if self.runner.cancel_requested:
                return self._finish_failed(stage.key, canceled=True, detail="运行前取消")

            self._event(EV_STAGE_START, index=i, key=stage.key, name=stage.name,
                        kind=stage.kind, count=len(stages))
            self.logger.info(f"--- 阶段 {i + 1}/{len(stages)}: {stage.name} ---")
            stage_dir = self.stage_dirs[i]
            stage_dir.mkdir(parents=True, exist_ok=True)

            # 1) 生成 mdp（CHARMM-GUI 原文件 + 界面参数覆盖；缺失时用模板）
            cfg = self.stage_configs.get(stage.key, {})
            override_map = mdp_mod.make_override_map(cfg, stage.parsed, self.sim_type)
            if stage.mdp_path and stage.mdp_path.exists():
                text = stage.mdp_path.read_text(encoding="utf-8", errors="replace")
            else:
                text = mdp_mod.generate_fallback_mdp(stage.kind, self.sim_type)
            base_params = mdp_mod.parse_mdp_from_text(text)
            # 首个动力学阶段（从最小化后的零速度结构起步）：若最终 mdp 未显式
            # 设置 gen-vel，自动补上速度生成参数，否则体系从静止起步无法正常升温。
            # CHARMM-GUI 的 NVT mdp 本就写了 gen-vel=yes（被 base_params 命中跳过），
            # 故本逻辑只修正内置模板兜底 / 直接从生产起跑的场景。
            merged = {**base_params,
                      **{k.lower(): v for k, v in override_map.items()}}
            if (i == first_dyn_idx and stage.kind != config.KIND_EM
                    and "gen-vel" not in merged
                    and str(merged.get("integrator", "md")).lower().startswith("md")):
                override_map["gen-vel"] = "yes"
                gen_temp = mdp_mod.first_float(merged, "ref_t", None)
                if gen_temp is not None:
                    override_map["gen-temp"] = f"{gen_temp:g}"
                self.logger.info(
                    f"首个动力学阶段 {stage.name}：自动补充 gen-vel=yes"
                    + (f", gen-temp={gen_temp:g} K" if gen_temp is not None else ""))
            mdp_out = stage_dir / f"md_{stage.key}.mdp"
            mdp_out.write_text(mdp_mod.apply_overrides(text, override_map),
                               encoding="utf-8")
            final_params = mdp_mod.parse_mdp(mdp_out)
            nsteps = mdp_mod.to_int(final_params, "nsteps", 1) or 1
            self.logger.info(f"输入文件: {mdp_out.name} (nsteps={nsteps})")

            # 2) grompp（全部使用相对路径，本机与 WSL 通用）
            use_r = stage.kind in (config.KIND_EM, config.KIND_EQUIL)
            tpr_out = stage_dir / "md.tpr"
            grompp_cmd = config.build_grompp_cmd(
                self.gmx_exe,
                f=self._rel(stage_dir, mdp_out),
                c=self._rel(stage_dir, prev_gro),
                r=self._rel(stage_dir, reference) if use_r else None,
                t=self._rel(stage_dir, prev_cpt) if prev_cpt is not None else None,
                p=self._rel(stage_dir, topol_copy),
                n=self._rel(stage_dir, index_copy) if index_copy is not None else None,
                o="md.tpr")
            rc = self.runner.run_cmd(grompp_cmd, stage_dir)
            if rc != 0 or self.runner.cancel_requested:
                return self._finish_failed(stage.key,
                                           canceled=self.runner.cancel_requested,
                                           detail="grompp 失败")
            if self.dry_run and not tpr_out.exists():
                tpr_out.write_bytes(b"dry-run tpr placeholder")

            # 3) mdrun（实时进度）
            # 全原子动力学阶段在 GPU 模式下额外用 -pme gpu 加速 PME；
            # 粗粒化(reaction-field 无 PME)与 EM 阶段不加，避免报错。
            use_pme_gpu = (self.sim_type == config.ALL_ATOM
                           and stage.kind != config.KIND_EM)
            mdrun_cmd = config.build_mdrun_cmd(self.gmx_exe, "md",
                                               self.ntomp, self.use_gpu,
                                               use_pme_gpu)

            # 进度事件节流：每秒最多 1 次刷新界面；每 15 秒写一条 INFO 进度
            last_emit = [0.0]
            last_info = [0.0]

            def on_progress(frac: float, elapsed: float, eta: str,
                            _i=i, _stage=stage) -> None:
                now = time.time()
                if now - last_emit[0] < 1.0 and frac < 1.0:
                    return
                last_emit[0] = now
                total_weight = sum(self.weights)
                done_weight = sum(self.weights[:max(_i, 0)])
                overall = min((done_weight + self.weights[_i] * frac) / max(total_weight, 1), 1.0)
                self._event(EV_PROGRESS, index=_i, key=_stage.key, name=_stage.name,
                            fraction=frac, overall=overall, eta=eta,
                            elapsed=elapsed, nsteps=nsteps)
                if now - last_info[0] >= 15.0 or frac >= 1.0:
                    last_info[0] = now
                    self.logger.info(f"{_stage.name} 进度 {frac * 100:.1f}%，"
                                     f"预计剩余 {eta}")

            rc = self.runner.mdrun(mdrun_cmd, stage_dir, nsteps, on_progress)
            # GPU 相关错误自动回退 CPU 重试本阶段（等价于控制台去掉 GPU 参数重跑）
            if rc != 0 and self.use_gpu and not self.runner.cancel_requested \
                    and self._is_gpu_error():
                self.logger.warn("检测到 GPU 运行错误，自动回退为 CPU 重试本阶段…")
                self.use_gpu = False
                mdrun_cmd = config.build_mdrun_cmd(self.gmx_exe, "md",
                                                   self.ntomp, self.use_gpu,
                                                   use_pme_gpu=False)
                rc = self.runner.mdrun(mdrun_cmd, stage_dir, nsteps, on_progress)
            # GROMACS 2023.4+ 兼容性：软核扰动 + reaction-field 报错时，
            # 自动禁用 free-energy（跳过逐步加力技巧）后重跑本阶段
            if rc != 0 and not self.runner.cancel_requested \
                    and self._is_rf_perturbation_error():
                self.logger.warn(
                    "检测到 GROMACS 版本兼容性错误（软核扰动 + reaction-field），"
                    "自动禁用 free-energy（跳过逐步加力）后重试本阶段…")
                text = mdp_out.read_text(encoding="utf-8", errors="replace")
                mdp_out.write_text(mdp_mod.disable_free_energy(text),
                                   encoding="utf-8")
                rc_g = self.runner.run_cmd(grompp_cmd, stage_dir)
                if rc_g != 0 or self.runner.cancel_requested:
                    return self._finish_failed(stage.key,
                                               canceled=self.runner.cancel_requested,
                                               detail="兼容性重试 grompp 失败")
                rc = self.runner.mdrun(mdrun_cmd, stage_dir, nsteps, on_progress)
            if rc != 0 or self.runner.cancel_requested:
                detail = self._high_energy_hint() or "mdrun 失败"
                return self._finish_failed(stage.key,
                                           canceled=self.runner.cancel_requested,
                                           detail=detail)

            self._event(EV_STAGE_DONE, index=i, key=stage.key, name=stage.name)
            self.logger.info(f"阶段完成: {stage.name}")
            prev_gro = stage_dir / "md.gro"
            cpt = stage_dir / "md.cpt"
            prev_cpt = cpt if cpt.exists() else None

        return self._finish_ok()

    # ------------------------------------------------------------------
    # 收尾
    # ------------------------------------------------------------------
    def _finish_failed(self, key: str, canceled: bool = False,
                       detail: str = "") -> JobResult:
        self._event(EV_STAGE_FAIL, key=key, canceled=canceled, detail=detail)
        msg = "运行已取消" if canceled else f"阶段 {key} 失败: {detail or '运行出错'}"
        self.logger.error(msg)
        self._event(EV_ERROR, message=msg, canceled=canceled)
        if canceled:
            self._event(EV_CANCELED, key=key)
        summary = self._write_summary(ok=False, canceled=canceled)
        self._event(EV_FINISHED, output_dir=str(self.job_dir), summary=summary,
                    ok=False, canceled=canceled)
        return JobResult(ok=False, canceled=canceled, failed_stage=key,
                         output_dir=self.job_dir)

    def _finish_ok(self) -> JobResult:
        summary = self._write_summary(ok=True)
        self.logger.info("全部阶段完成！")
        self._event(EV_FINISHED, output_dir=str(self.job_dir), summary=summary,
                    ok=True, canceled=False)
        return JobResult(ok=True, output_dir=self.job_dir)

    # ------------------------------------------------------------------
    # 运行汇总
    # ------------------------------------------------------------------
    def _write_summary(self, ok: bool, canceled: bool = False) -> str:
        lines = []
        add = lines.append
        add("=" * 62)
        add(" MD Workflow Manager 运行汇总")
        add("=" * 62)
        add(f"生成时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        add("")
        add("[基本信息]")
        add(f"  输入目录:   {self.detect.gromacs_dir or ''}")
        add(f"  力场:       {self.detect.ff_name or '未知'}")
        add(f"  模拟类型:   {config.SIM_TYPE_NAMES.get(self.sim_type, self.sim_type)}")
        add(f"  原子数:     {self.detect.atoms if self.detect.atoms is not None else '未知'}")
        add(f"  运行模式:   {'dry-run 模拟' if self.dry_run else '真实 GROMACS'}")
        add(f"  输出目录:   {self.job_dir}")
        add("")
        add("[阶段与参数]")
        stages = self.detect.stages
        for i, stage in enumerate(stages):
            cfg = self.stage_configs.get(stage.key, {})
            params = mdp_mod.parse_mdp(self.stage_dirs[i] / f"md_{stage.key}.mdp") \
                if self.job_dir and (self.stage_dirs[i] / f"md_{stage.key}.mdp").exists() \
                else stage.parsed
            dur = mdp_mod.stage_duration_ps(params)
            temp = mdp_mod.first_float(params, "ref_t", None)
            press = mdp_mod.first_float(params, "ref_p", None)
            rest = "-DPOSRES" in str(mdp_mod.get(params, "define", "")).upper()
            dur_str = (f"~{dur / 1000.0:.4g}ns" if stage.kind == config.KIND_PRODUCTION
                       else f"~{dur:.3g}ps")
            dt_val = mdp_mod.to_float(params, "dt", None)
            dt_str = f"{dt_val}ps" if dt_val is not None else "—"
            add(f"  {i + 1:02d}. {stage.name}")
            add(f"      nsteps={mdp_mod.to_int(params, 'nsteps', '?')}"
                f"  dt={dt_str}  {dur_str}"
                + (f"  T={temp}K" if temp is not None else "")
                + (f"  P={press}bar" if press is not None else "")
                + ("  位置限制" if rest else ""))
        add("")
        if canceled:
            add("[结果] 已取消")
        elif ok:
            add("[结果] 全部阶段成功完成")
        else:
            add("[结果] 运行失败（详见 md_sim.log）")
        add("")
        add("[输出文件]")
        if self.job_dir:
            for f in sorted(self.job_dir.rglob("*")):
                if f.is_file() and "inputs" not in f.parts:
                    add(f"  {f.relative_to(self.job_dir)}")
        assert self.job_dir is not None
        (self.job_dir / "run_summary.txt").write_text("\n".join(lines) + "\n",
                                                      encoding="utf-8")
        return "\n".join(lines)
