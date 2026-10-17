# -*- coding: utf-8 -*-
"""日志：同时写入文件，并通过回调推送到界面。线程安全。"""
from __future__ import annotations

import datetime
import threading
from typing import Callable, List, Optional

INFO = "INFO"
WARN = "WARN"
ERROR = "ERROR"
DEBUG = "DEBUG"


class MDLogger:
    """分级日志。GUI 注册回调后可实时收到每一条日志。"""

    def __init__(self) -> None:
        self._callbacks: List[Callable[[str, str], None]] = []
        self._file = None
        self._lock = threading.Lock()

    def set_log_file(self, path: str) -> None:
        """切换日志文件（追加模式打开）。"""
        with self._lock:
            if self._file:
                try:
                    self._file.close()
                except Exception:
                    pass
            self._file = open(path, "w", encoding="utf-8")

    def add_callback(self, cb: Callable[[str, str], None]) -> None:
        self._callbacks.append(cb)

    def _write(self, level: str, msg: str) -> None:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] [{level}] {msg}"
        with self._lock:
            if self._file:
                try:
                    self._file.write(line + "\n")
                    self._file.flush()
                except Exception:
                    pass
        for cb in list(self._callbacks):
            try:
                cb(level, msg)
            except Exception:
                pass

    def info(self, msg: str) -> None:
        self._write(INFO, msg)

    def warn(self, msg: str) -> None:
        self._write(WARN, msg)

    def error(self, msg: str) -> None:
        self._write(ERROR, msg)

    def debug(self, msg: str) -> None:
        self._write(DEBUG, msg)

    def close(self) -> None:
        with self._lock:
            if self._file:
                try:
                    self._file.close()
                except Exception:
                    pass
                self._file = None
