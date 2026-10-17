# -*- coding: utf-8 -*-
"""GROMACS 执行器：grompp/mdrun 子进程管理、进度解析、ETA 计算、取消、dry-run。"""
from __future__ import annotations

import datetime
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .backend import Backend, LocalBackend
from .logger import MDLogger

# gmx mdrun 进度输出的三种格式：
#   新版: "step 1193500, will finish Thu Sep 24 12:20:22 2026"
#   旧版: "step 5000, time 10 (ps)"
#   EM:   "Step=   39, Dmax= 3.1e-01 nm, Epot=  4.68812e+05 ..."
STEP_NEW_RE = re.compile(r"step\s+(\d+),\s*will finish\s+(.+?)\s*$", re.I)
STEP_OLD_RE = re.compile(r"step\s+(\d+)[,.\s]+time\s+([\d.]+)\s*\(ps\)", re.I)
STEP_EM_RE = re.compile(r"Step=\s*(\d+)", re.I)

_GMX_FINISH_FMT = "%a %b %d %H:%M:%S %Y"


def _parse_gmx_finish(s: str) -> Optional[datetime.datetime]:
    """解析 'Thu Sep 24 12:20:22 2026'（gmx 输出恒为英文缩写）。"""
    try:
        return datetime.datetime.strptime(s.strip(), _GMX_FINISH_FMT)
    except (ValueError, TypeError):
        return None


def extract_progress(line: str, total: int) -> Optional[Tuple[float, Optional[datetime.datetime], int]]:
    """从 gmx 输出行提取 (阶段内进度 0~1, 预计完成时刻|None, 当前步)。

    兼容三种输出格式（新版 will finish / 旧版 time (ps) / EM Step=），
    无进度信息时返回 None。
    """
    step = None
    finish_str = None
    m = STEP_NEW_RE.search(line)
    if m:
        step = int(m.group(1))
        finish_str = m.group(2)
    else:
        m = STEP_OLD_RE.search(line)
        if m:
            step = int(m.group(1))
        else:
            m = STEP_EM_RE.search(line)
            if m:
                step = int(m.group(1))
    if step is None:
        return None
    frac = min(step / max(int(total), 1), 1.0)
    return frac, (_parse_gmx_finish(finish_str) if finish_str else None), step


def check_gmx(gmx_exe: str):
    """检测本机 gmx 是否可用（等价于 LocalBackend 检测）。"""
    return LocalBackend().check_gmx(gmx_exe)


def fmt_eta(seconds: float) -> str:
    """把秒数格式化为 “X小时X分X秒”。"""
    secs = int(max(0.0, seconds))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}小时{m}分{s}秒"
    if m:
        return f"{m}分{s}秒"
    return f"{s}秒"


class GromacsRunner:
    """封装 grompp / mdrun 调用，可运行在本机或 WSL2 Linux 后端。

    dry_run=True 时使用内置模拟器走流程（生成伪进度与占位输出文件），
    便于在未安装 GROMACS 的机器上验证完整流程。
    """

    def __init__(self, logger: MDLogger, backend: Optional[Backend] = None,
                 dry_run: bool = False, dry_sleep: float = 0.02):
        self.logger = logger
        self.backend = backend or LocalBackend()
        self.dry_run = dry_run
        self.dry_sleep = dry_sleep
        self.cancel_requested = False
        self._proc: Optional[subprocess.Popen] = None
        self.output_tail: List[str] = []   # 最近输出（供错误识别）

    # ------------------------------------------------------------------
    # 取消
    # ------------------------------------------------------------------
    def request_cancel(self) -> None:
        self.cancel_requested = True
        proc = self._proc
        if proc is not None and proc.poll() is None:
            self._kill_proc(proc)

    def _kill_proc(self, proc: subprocess.Popen) -> None:
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True)
            else:
                import os
                os.killpg(os.getpgid(proc.pid), 15)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 通用命令执行（grompp 等）
    # ------------------------------------------------------------------
    def _spawn(self, argv: List[str], cwd: Path):
        kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} \
            if sys.platform == "win32" else {"start_new_session": True}
        return subprocess.Popen(
            argv, cwd=str(cwd),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1, **kwargs)

    def run_cmd(self, cmd: List[str], cwd: Path) -> int:
        self.logger.info("$ " + " ".join(str(x) for x in cmd))
        if self.dry_run:
            self.logger.info("[dry-run] 模拟执行（不真正运行 gmx）")
            return 0
        try:
            proc = self._spawn(self.backend.wrap(cmd, cwd), cwd)
        except FileNotFoundError as exc:
            self.logger.error(f"无法启动命令: {exc}")
            return -1
        self._proc = proc
        self.output_tail = []
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip("\n")
                self.logger.debug(line)
                self.output_tail.append(line)
                if len(self.output_tail) > 200:
                    self.output_tail.pop(0)
                if self.cancel_requested:
                    self._kill_proc(proc)
                    break
            proc.wait()
        finally:
            self._proc = None
        return proc.returncode

    # ------------------------------------------------------------------
    # mdrun（带进度解析）
    # ------------------------------------------------------------------
    def mdrun(self, cmd: List[str], cwd: Path, nsteps: int,
              on_progress: Optional[Callable[[float, float, str], None]] = None) -> int:
        """执行 mdrun。on_progress(阶段内进度0~1, 已耗时秒, ETA文本)。"""
        self.logger.info("$ " + " ".join(str(x) for x in cmd))
        if self.dry_run:
            return self._dry_mdrun(cmd, cwd, nsteps, on_progress)

        total = max(int(nsteps), 1)
        start = time.time()
        try:
            proc = self._spawn(self.backend.wrap(cmd, cwd), cwd)
        except FileNotFoundError as exc:
            self.logger.error(f"无法启动命令: {exc}")
            return -1
        self._proc = proc
        self.output_tail = []
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip("\n")
                self.logger.debug(line)
                self.output_tail.append(line)
                if len(self.output_tail) > 200:
                    self.output_tail.pop(0)
                prog = extract_progress(line, total)
                if prog and on_progress:
                    frac, ft, step = prog
                    elapsed = time.time() - start
                    if ft is not None:
                        eta_str = fmt_eta((ft - datetime.datetime.now()).total_seconds())
                    else:
                        eta_str = fmt_eta(elapsed * (total - step) / max(step, 1))
                    on_progress(frac, elapsed, eta_str)
                if self.cancel_requested:
                    self._kill_proc(proc)
                    break
            proc.wait()
        finally:
            self._proc = None
        return proc.returncode

    # ------------------------------------------------------------------
    # dry-run 模拟器
    # ------------------------------------------------------------------
    def _dry_mdrun(self, cmd: List[str], cwd: Path, nsteps: int,
                   on_progress: Optional[Callable[[float, float, str], None]]) -> int:
        total = max(int(nsteps), 1)
        start = time.time()
        chunk = max(total // 20, 1)
        step = 0
        while step < total:
            if self.cancel_requested:
                self.logger.warn("[dry-run] 已收到取消请求")
                return -1
            time.sleep(self.dry_sleep)
            step = min(step + chunk, total)
            frac = step / total
            elapsed = time.time() - start
            eta = elapsed * (total - step) / max(step, 1)
            if on_progress:
                on_progress(frac, elapsed, fmt_eta(eta))
        # 生成占位输出文件，模拟 gmx 的产物
        for name in ("md.log", "md.gro", "md.cpt", "md.edr", "md.xtc"):
            (Path(cwd) / name).write_text("dry-run placeholder\n", encoding="utf-8")
        self.logger.info("Finished mdrun [dry-run]")
        return 0
