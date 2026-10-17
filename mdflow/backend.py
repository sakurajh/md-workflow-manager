# -*- coding: utf-8 -*-
"""运行后端抽象：本机直接执行 gmx，或通过 WSL2 Linux 执行 gmx。

设计要点：
- 所有 gmx 命令使用「相对路径 + 指定工作目录」执行，同一组命令在
  本机（Windows/Linux）与 WSL 内均可直接使用，无需翻译路径。
- WSL 后端把命令包进 `wsl -d <发行版> -- bash -lc "cd <挂载路径> && <命令>"`，
  用 shlex.quote 保证含空格/中文的路径安全。
"""
from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

from . import config


def win_to_wsl(path) -> str:
    """Windows 路径 → WSL 挂载路径（C:\\a\\b → /mnt/c/a/b）。"""
    p = str(Path(path))
    m = re.match(r"^([A-Za-z]):(.*)$", p)
    if m:
        drive = m.group(1).lower()
        rest = m.group(2).replace("\\", "/")
        return f"/mnt/{drive}{rest}"
    return p.replace("\\", "/")


def decode_wsl_bytes(data: bytes) -> str:
    """wsl.exe 输出为 UTF-16LE 编码，统一转成 str。"""
    if not data:
        return ""
    if data.startswith(b"\xff\xfe"):
        return data.decode("utf-16-le").replace("\x00", "") \
            .replace("\ufeff", "").strip()
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8").replace("﻿", "").strip()
    # \u65e0 BOM\uff1a\u5148\u6309 UTF-8 \u89e3\u6790\uff1b\u82e5\u7ed3\u679c\u542b NUL \u5b57\u8282\uff08\u8bf4\u660e\u5176\u5b9e\u662f UTF-16LE\uff09\uff0c
    # \u5219\u6539\u6309 UTF-16LE \u89e3\u7801\uff0c\u907f\u514d\u4e71\u7801\u3002
    try:
        s = data.decode("utf-8")
    except UnicodeDecodeError:
        s = None
    if s is not None:
        if "\x00" in s:
            return data.decode("utf-16-le", errors="replace") \
                .replace("\x00", "").strip()
        return s.strip()
    return data.decode("utf-16-le", errors="replace").replace("\x00", "").strip()


class Backend:
    """模拟运行后端抽象。"""
    name = "base"
    display = "base"
    modules: List[str] = []   # 环境模块（仅 LocalBackend 超算场景使用）

    def wrap(self, cmd: List[str], cwd) -> List[str]:
        """把命令包装为可在本机执行的完整 argv。"""
        raise NotImplementedError

    def check_gmx(self, gmx_exe: str) -> Tuple[bool, str]:
        raise NotImplementedError

    def probe(self, script: str) -> Tuple[int, str]:
        """执行探测脚本（bash 语法），返回 (退出码, 输出)。"""
        raise NotImplementedError

    def stream(self, script: str, on_output) -> int:
        """流式执行脚本，逐行回调输出，返回退出码。"""
        raise NotImplementedError


class LocalBackend(Backend):
    """本机直接执行 gmx（Linux 原生 / 超算登录与计算节点）。

    modules: 超算环境模块列表（如 ["gromacs/2023.3", "cuda/12.2"]），
    配置后所有命令通过 `bash -lc "module load ... && cd ... && cmd"` 执行。
    """
    name = config.BACKEND_LOCAL
    display = "本机"

    def __init__(self, modules: Optional[List[str]] = None):
        self.modules = [m.strip() for m in (modules or []) if m and m.strip()]

    def _prefix(self) -> str:
        if not self.modules:
            return ""
        return "module load " + " ".join(self.modules) + " && "

    def _script(self, cmd: List[str], cwd) -> str:
        return (self._prefix() + "cd " + shlex.quote(str(cwd)) + " && "
                + " ".join(shlex.quote(str(a)) for a in cmd))

    def wrap(self, cmd: List[str], cwd) -> List[str]:
        if not self.modules:
            return [str(x) for x in cmd]
        return ["bash", "-lc", self._script(cmd, cwd)]

    def probe(self, script: str) -> Tuple[int, str]:
        if sys.platform == "win32":
            return -1, "本机（Windows）不支持 bash 探测，请使用 WSL 后端"
        try:
            proc = subprocess.run(["bash", "-lc", self._prefix() + script],
                                  capture_output=True, text=True, timeout=300)
            return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except Exception as exc:
            return -1, str(exc)

    def stream(self, script: str, on_output) -> int:
        if sys.platform == "win32":
            on_output("本机（Windows）不支持 bash 脚本，请使用 WSL 后端")
            return -1
        try:
            proc = subprocess.Popen(["bash", "-lc", self._prefix() + script],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except Exception as exc:
            on_output(str(exc))
            return -1
        assert proc.stdout is not None
        for raw in proc.stdout:
            for ln in raw.decode("utf-8", errors="replace").splitlines():
                if ln.strip():
                    on_output(ln.rstrip())
        return proc.wait()

    def check_gmx(self, gmx_exe: str) -> Tuple[bool, str]:
        if self.modules:
            rc, out = self.probe(shlex.quote(gmx_exe) + " --version")
            for line in out.splitlines():
                if "gromacs version" in line.lower() or ":-)" in line:
                    return True, line.strip()
            # 展示真实错误信息（模块输出 + gmx 报错），便于排查
            detail = "\n".join(out.splitlines()[-6:]) if out.strip() else "无输出"
            return False, (f"模块 {', '.join(self.modules)} 加载后仍无法运行 gmx。"
                           f"模块输出：\n{detail}")
        try:
            proc = subprocess.run([gmx_exe, "--version"], capture_output=True,
                                  text=True, timeout=30)
            out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
            for line in out.splitlines():
                if line.lower().startswith("gromacs version"):
                    return True, line.strip()
            if out:
                return True, out.splitlines()[0]
            return True, gmx_exe
        except FileNotFoundError:
            return False, f"找不到命令 “{gmx_exe}”。请安装 GROMACS 或点击“自动安装”。"
        except Exception as exc:
            return False, f"无法运行 gmx: {exc}"


class WslBackend(Backend):
    """Windows 界面 + WSL2 Linux 后端：模拟在 Linux 环境里运行。"""
    name = config.BACKEND_WSL
    display = "WSL2 Linux"

    def __init__(self, distro: str = ""):
        self.distro = distro or ""

    def _base(self) -> List[str]:
        return ["wsl"] + (["-d", self.distro] if self.distro else [])

    @staticmethod
    def _quote_arg(a: str) -> str:
        """bash 参数引用：以 ~ 开头的路径展开为 $HOME（单引号内 ~ 不展开）。"""
        a = str(a)
        if a.startswith("~"):
            return "$HOME" + shlex.quote(a[1:])
        return shlex.quote(a)

    def wrap(self, cmd: List[str], cwd) -> List[str]:
        guest_cwd = win_to_wsl(cwd)
        script = "cd " + shlex.quote(guest_cwd) + " && " + \
                 " ".join(self._quote_arg(a) for a in cmd)
        return self._base() + ["--", "bash", "-lc", script]

    def probe(self, script: str) -> Tuple[int, str]:
        try:
            proc = subprocess.run(self._base() + ["--", "bash", "-lc", script],
                                  capture_output=True, timeout=300)
            out = (proc.stdout or b"") + (proc.stderr or b"")
            return proc.returncode, decode_wsl_bytes(out)
        except FileNotFoundError:
            return -1, "未找到 wsl 命令（需要 Windows 10 2004+ 或 Windows 11）"
        except Exception as exc:
            return -1, str(exc)

    def stream(self, script: str, on_output) -> int:
        try:
            proc = subprocess.Popen(self._base() + ["--", "bash", "-lc", script],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except Exception as exc:
            on_output(str(exc))
            return -1
        assert proc.stdout is not None
        for raw in proc.stdout:
            text = raw.decode("utf-8", errors="replace").replace("\x00", "")
            for ln in text.splitlines():
                if ln.strip():
                    on_output(ln.rstrip())
        return proc.wait()

    def check_gmx(self, gmx_exe: str) -> Tuple[bool, str]:
        g = self._quote_arg(gmx_exe)
        rc, out = self.probe(f"{g} --version")
        for line in out.splitlines():
            low = line.lower()
            if low.startswith("gromacs version") or ":-)" in low:
                return True, line.strip()
        if rc == 0 and out.strip():
            return True, out.strip().splitlines()[0]
        return False, f"WSL（{self.distro or '默认发行版'}）中未找到 {gmx_exe}"


def make_backend(kind: str, distro: str = "",
                 modules: Optional[List[str]] = None) -> Backend:
    if kind == config.BACKEND_WSL:
        return WslBackend(distro)
    return LocalBackend(modules)
