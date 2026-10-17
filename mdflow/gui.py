# -*- coding: utf-8 -*-
"""CustomTkinter 图形界面：左侧边栏导航 + 四页面（环境/输入/参数/运行）+ 底部状态栏。

设计语言（参考现代科学工具/SaaS 桌面应用）：
- 深色导航侧栏 + 浅色内容区、卡片式分组（圆角 12）、8px 间距节奏
- 统一色板（亮/暗双值，CTk 按主题自动切换）、靛蓝主色
- 排版层级：页标题 22 / 卡片标题 14 / 正文 13 / 辅助 11 / 数据大字 26
- CustomTkinter 自带 Windows DPI 感知（150% 缩放下文字清晰不模糊）

线程模型不变：工作线程跑 Workflow，queue 推事件，主线程 after(100) 轮询。
"""
from __future__ import annotations

import datetime
import json
import os
import queue
import re
import sys
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Dict, Optional, List

import customtkinter as ctk

from . import config, detect, envsetup, mdp as mdp_mod, workflow
from .backend import Backend, LocalBackend, WslBackend, make_backend
from .logger import MDLogger

AUTO_LABEL = "自动检测"
TCOUPL_CHOICES = ["v-rescale", "Nose-Hoover", "Berendsen", "no"]
PCOUPL_CHOICES = ["Parrinello-Rahman", "C-rescale", "Berendsen", "no"]
IS_WINDOWS = sys.platform == "win32"
# 字体按平台选择（缺失时 Tk 自动回退默认字体）
FONT_FAMILY = "Microsoft YaHei UI" if IS_WINDOWS else "Noto Sans CJK SC"
MONO_FAMILY = "Cascadia Mono" if IS_WINDOWS else "Monospace"
# gmx 步进日志（只写文件，不上屏，避免刷屏）
STEP_NOISE_RE = re.compile(r"step\s+\d+,|Step=\s*\d+")

# ---------------------------------------------------------------------------
# 色板：每项为 (浅色模式值, 深色模式值)；CTk 控件自动按主题取用
# ---------------------------------------------------------------------------
C_BG = ("#f3f5fa", "#12151f")          # 内容区底色
C_CARD = ("#ffffff", "#1b2030")        # 卡片
C_CARD_SUB = ("#f8fafd", "#222840")    # 卡片内衬
C_SIDE = ("#151b2e", "#0d1120")        # 侧栏（两模式都深色，导航栏惯例）
C_SIDE_HOVER = ("#232b45", "#1b2138")
C_SIDE_TEXT = ("#aab3c5", "#aab3c5")
C_PRIMARY = "#3563e9"
C_PRIMARY_HOVER = "#2b53cf"
C_TEXT = ("#1f2937", "#e6e9f2")
C_MUTED = ("#64748b", "#93a0b8")
C_BORDER = ("#e2e8f2", "#2a3148")
C_OK = ("#16a34a", "#4ade80")
C_WARN = ("#d97706", "#fbbf24")
C_ERR = ("#dc2626", "#f87171")
C_GHOST = ("#eef1f8", "#242b42")
TAG_COLORS = {"info": ("#1f2937", "#e6e9f2"), "warn": ("#d97706", "#fbbf24"),
              "error": ("#dc2626", "#f87171"), "debug": ("#8a94a6", "#7c8aa5")}

PAGE_ICON = {"env": "1", "input": "2", "params": "3", "run": "4"}


# ---------------------------------------------------------------------------
# 兼容子类：让既有逻辑/测试沿用 ttk 风格 API
# ---------------------------------------------------------------------------
class Combo(ctk.CTkComboBox):
    """支持 ["values"] 读写、current(idx) 与 textvariable 双向同步的 CTkComboBox。"""

    def __init__(self, master=None, textvariable=None, **kwargs):
        self._var = textvariable
        self._var_ready = False        # 初始化期间不回写变量
        super().__init__(master, **kwargs)
        # CTk 默认会把类名 "CTkComboBox" 显示为空值文本，这里统一清掉
        v = str(self._var.get() or "") if textvariable is not None else ""
        super().set(v)
        if self._var is not None:
            self._var.trace_add("write", self._sync_from_var)
            # CTk 下拉选择走 _dropdown_callback 直接改 entry，不经过 set()，
            # 必须在这里把 控件→变量 的同步补上（手输/失焦同理）。
            self._entry.bind("<KeyRelease>", self._sync_to_var, add="+")
            self._entry.bind("<FocusOut>", self._sync_to_var, add="+")
        self._var_ready = True

    def _dropdown_callback(self, value):
        super()._dropdown_callback(value)
        self._sync_to_var()

    def _sync_to_var(self, _event=None) -> None:
        var = getattr(self, "_var", None)
        if var is None or not getattr(self, "_var_ready", False):
            return
        v = self.get()
        if str(var.get()) != v:
            var.set(v)

    def _sync_from_var(self, *_args) -> None:
        v = str(self._var.get())
        if v != self.get():
            super().set(v)

    def set(self, value) -> None:
        super().set(value)
        var = getattr(self, "_var", None)
        if var is not None and getattr(self, "_var_ready", False) \
                and str(var.get()) != str(value):
            var.set(str(value))

    def __setitem__(self, key, value):
        if key == "values":
            self.configure(values=list(value))
        else:
            super().__setitem__(key, value)

    def __getitem__(self, key):
        if key == "values":
            return list(self.cget("values"))
        return self.cget(key)

    def current(self, idx=None):
        vals = list(self.cget("values") or [])
        if idx is None:
            try:
                return vals.index(self.get())
            except ValueError:
                return -1
        if 0 <= idx < len(vals):
            self.set(str(vals[idx]))


class Progress(ctk.CTkProgressBar):
    """支持 ["value"]=0~100 与 start()/stop() 的进度条。"""

    def __setitem__(self, key, value):
        if key == "value":
            self.set(max(0.0, min(float(value), 100.0)) / 100.0)
        elif key == "mode":
            self.configure(mode=str(value))
        else:
            super().__setitem__(key, value)

    def __getitem__(self, key):
        if key == "value":
            return self.get() * 100.0
        return self.cget(key)

    def start(self, _interval: int = 0) -> None:
        # CTk 原生 start()/stop() 负责不定进度动画；忽略 interval 参数以兼容 ttk API
        self.configure(mode="indeterminate")
        if not getattr(self, "_loop_running", False):
            super().start()


class MDApp(ctk.CTk):
    # ==================================================================
    # 初始化
    # ==================================================================
    def __init__(self) -> None:
        super().__init__(fg_color=C_BG)
        self.title(f"{config.APP_NAME} v{config.APP_VERSION}")
        self.geometry("1260x850")
        self.minsize(1050, 700)

        self._setup_fonts()
        self._restore_theme()
        self._setup_ttk_style()   # Treeview 等保留 ttk 的控件
        self._setup_icon()

        self.event_q: "queue.Queue[dict]" = queue.Queue()
        self.worker: Optional[threading.Thread] = None
        self.workflow: Optional[workflow.Workflow] = None
        self.detect_result: Optional[detect.DetectResult] = None
        self.stage_cfgs: Dict[str, dict] = {}
        self._form: Dict[str, object] = {}
        self._cur_stage_key: Optional[str] = None
        self._failed_index: Optional[int] = None
        self._stage_items: Dict[str, str] = {}
        self._stage_stripes: Dict[str, str] = {}
        self._running = False
        self._last_overall = 0.0
        self._last_sim_type = ""
        self._last_dry = False
        self.gmx_info: dict = {}

        self.logger = MDLogger()
        self.logger.add_callback(self._on_log)

        self.cfg = envsetup.load_config()
        if not IS_WINDOWS:
            self.cfg["backend"] = config.BACKEND_LOCAL
        self.backend: Backend = make_backend(
            str(self.cfg.get("backend", config.BACKEND_WSL)),
            str(self.cfg.get("wsl_distro", "")))

        self._nav_buttons: Dict[str, ctk.CTkButton] = {}
        self._pages: Dict[str, ctk.CTkFrame] = {}
        self._poll_job = None
        self._build_ui()
        self._apply_config_to_ui()
        self._startup_env_detect()
        self._poll_job = self.after(100, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ==================================================================
    # 字体 / 主题 / 图标
    # ==================================================================
    def _setup_fonts(self) -> None:
        self.f_page = ctk.CTkFont(FONT_FAMILY, 22, "bold")      # 页面大标题
        self.f_sub = ctk.CTkFont(FONT_FAMILY, 12)               # 页面副标题
        self.f_card = ctk.CTkFont(FONT_FAMILY, 14, "bold")      # 卡片标题
        self.f_body = ctk.CTkFont(FONT_FAMILY, 13)              # 正文
        self.f_body_b = ctk.CTkFont(FONT_FAMILY, 13, "bold")
        self.f_small = ctk.CTkFont(FONT_FAMILY, 11)             # 辅助说明
        self.f_num = ctk.CTkFont(FONT_FAMILY, 26, "bold")       # 数据大字
        self.f_stat = ctk.CTkFont(FONT_FAMILY, 11)              # 统计卡标题
        self.f_nav = ctk.CTkFont(FONT_FAMILY, 14)               # 侧栏导航
        self.f_logo = ctk.CTkFont(FONT_FAMILY, 17, "bold")
        self.f_btn = ctk.CTkFont(FONT_FAMILY, 13, "bold")
        self.f_log = ctk.CTkFont(MONO_FAMILY, 12)

    def _restore_theme(self) -> None:
        theme = str((envsetup.load_config().get("theme") or "Light"))
        try:
            ctk.set_appearance_mode({"Light": "light", "Dark": "dark",
                                     "System": "system"}[theme])
        except (KeyError, tk.TclError):
            ctk.set_appearance_mode("light")
        self._theme = theme

    def _on_theme_changed(self, value: str) -> None:
        theme = {"跟随": "System", "浅色": "Light", "深色": "Dark"}.get(value, "Light")
        ctk.set_appearance_mode(
            {"Light": "light", "Dark": "dark", "System": "system"}[theme])
        self._theme = theme
        self.cfg["theme"] = theme
        try:
            envsetup.save_config(self.cfg)
        except OSError:
            pass
        self._setup_ttk_style()   # ttk 控件不随主题自动变，重刷
        self._tree_tag_refresh()

    def _setup_ttk_style(self) -> None:
        """为保留的 ttk.Treeview 配置与主题一致的样式。"""
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        is_dark = ctk.get_appearance_mode() == "Dark"
        bg = "#1b2030" if is_dark else "#ffffff"
        fg = "#e6e9f2" if is_dark else "#1f2937"
        head_bg = "#242b42" if is_dark else "#eef1f8"
        alt = "#20263a" if is_dark else "#f6f8fc"
        sel = "#2c3a5e" if is_dark else "#dbe7fd"
        style.configure("Md.Treeview", background=bg, foreground=fg,
                        fieldbackground=bg, bordercolor="#2a3148" if is_dark else "#e2e8f2",
                        rowheight=32, font=(FONT_FAMILY, 12), relief="flat",
                        borderwidth=0)
        style.configure("Md.Treeview.Heading", background=head_bg, foreground=fg,
                        font=(FONT_FAMILY, 12, "bold"), relief="flat", padding=(8, 9),
                        borderwidth=0)
        style.map("Md.Treeview", background=[("selected", sel)],
                  foreground=[("selected", fg)])
        self._ttk_alt = alt
        self._ttk_bg = bg

    def _tree_tag_refresh(self) -> None:
        tree = getattr(self, "tree_steps", None)
        if tree is not None:
            tree.tag_configure("row0", background=self._ttk_bg)
            tree.tag_configure("row1", background=self._ttk_alt)

    @staticmethod
    def _resource_path(name: str) -> Path:
        base = getattr(sys, "_MEIPASS", None)
        if base:
            return Path(base) / name
        return Path(__file__).resolve().parent.parent / name

    @staticmethod
    def _open_path(p) -> None:
        import subprocess
        try:
            if IS_WINDOWS:
                os.startfile(str(p))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p)])
        except Exception as exc:
            messagebox.showerror("错误", f"无法打开: {exc}")

    def _setup_icon(self) -> None:
        try:
            ppm = self._resource_path("md_icon_64.ppm")
            if ppm.exists():
                img = tk.PhotoImage(data=ppm.read_bytes(), format="ppm")
                self._icon_img = img
                self.iconphoto(True, img)
                return
        except (tk.TclError, OSError):
            pass
        try:
            img = tk.PhotoImage(width=64, height=64)
            img.put("#3563e9", to=(0, 0, 63, 63))

            def px(x: int, y: int) -> None:
                img.put("#ffffff", to=(x, y, x, y))

            def circle(cx: int, cy: int, r: int) -> None:
                for x in range(cx - r, cx + r + 1):
                    for y in range(cy - r, cy + r + 1):
                        if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                            px(x, y)
            for cx, cy, r in ((18, 20, 9), (46, 20, 9), (32, 46, 9)):
                circle(cx, cy, r)
            self._icon_img = img
            self.iconphoto(True, img)
        except tk.TclError:
            pass

    # ==================================================================
    # 通用小组件
    # ==================================================================
    def _card(self, parent, title: str = "", desc: str = "") -> ctk.CTkFrame:
        """白底圆角卡片；返回卡片 Frame（内部放内容）。"""
        card = ctk.CTkFrame(parent, corner_radius=12, fg_color=C_CARD,
                            border_width=1, border_color=C_BORDER)
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=20, pady=16)
        if title:
            ctk.CTkLabel(inner, text=title, font=self.f_card,
                         text_color=C_TEXT, anchor="w").pack(fill="x")
        if desc:
            ctk.CTkLabel(inner, text=desc, font=self.f_small, anchor="w",
                         justify="left", text_color=C_MUTED).pack(fill="x",
                                                                  pady=(2, 6))
        elif title:
            ctk.CTkFrame(inner, height=6, fg_color="transparent").pack(fill="x")
        card._inner = inner  # type: ignore[attr-defined]
        return card

    def _btn(self, parent, text: str, command, primary: bool = False,
             width: int = 0) -> ctk.CTkButton:
        kw = dict(font=self.f_btn, height=34, corner_radius=8, text=text,
                  command=command)
        if width:
            kw["width"] = width
        if primary:
            kw.update(fg_color=C_PRIMARY, hover_color=C_PRIMARY_HOVER,
                      text_color="#ffffff")
        else:
            kw.update(fg_color=C_GHOST, hover_color="#e2e8f4",
                      text_color=C_TEXT, border_width=1, border_color=C_BORDER)
        return ctk.CTkButton(parent, **kw)

    def _chip(self, parent, text: str) -> ctk.CTkLabel:
        return ctk.CTkLabel(parent, text=text, font=self.f_small,
                            fg_color=C_CARD_SUB, corner_radius=20,
                            border_width=1, border_color=C_BORDER,
                            text_color=C_MUTED, padx=12, pady=5)

    # ==================================================================
    # 总框架：侧栏 + 内容 + 状态栏
    # ==================================================================
    def _build_ui(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # ---- 侧栏 ----
        side = ctk.CTkFrame(self, width=212, corner_radius=0, fg_color=C_SIDE)
        side.grid(row=0, column=0, rowspan=2, sticky="ns")
        side.grid_propagate(False)

        logo = ctk.CTkFrame(side, fg_color="transparent")
        logo.pack(fill="x", padx=18, pady=(22, 18))
        ctk.CTkLabel(logo, text="MD", font=ctk.CTkFont(FONT_FAMILY, 18, "bold"),
                     text_color="#ffffff", fg_color=C_PRIMARY, corner_radius=8,
                     width=36, height=36).pack(side="left")
        tbox = ctk.CTkFrame(logo, fg_color="transparent")
        tbox.pack(side="left", padx=(10, 0))
        ctk.CTkLabel(tbox, text="MD Workflow", font=self.f_logo,
                     text_color="#ffffff", anchor="w").pack(fill="x", anchor="w")
        ctk.CTkLabel(tbox, text=f"Manager v{config.APP_VERSION}",
                     font=self.f_small, text_color=C_SIDE_TEXT,
                     anchor="w").pack(fill="x", anchor="w")

        nav_titles = [("env", "环境配置"), ("input", "输入与检测"),
                      ("params", "模拟参数"), ("run", "运行与日志")]
        for key, name in nav_titles:
            num = PAGE_ICON[key]
            btn = ctk.CTkButton(side, text=f"   {num}    {name}",
                                font=self.f_nav, height=42, corner_radius=8,
                                anchor="w", fg_color="transparent",
                                hover_color=C_SIDE_HOVER, text_color=C_SIDE_TEXT,
                                border_width=0,
                                command=lambda k=key: self._show_page(k))
            btn.pack(fill="x", padx=12, pady=3)
            self._nav_buttons[key] = btn

        ctk.CTkFrame(side, height=1, fg_color=C_SIDE_HOVER).pack(fill="x",
                                                                 padx=16, pady=14)

        foot = ctk.CTkFrame(side, fg_color="transparent")
        foot.pack(side="bottom", fill="x", padx=16, pady=16)
        ctk.CTkLabel(foot, text="外观", font=self.f_small,
                     text_color=C_SIDE_TEXT, anchor="w").pack(fill="x")
        cur = {"Light": "浅色", "Dark": "深色", "System": "跟随"}.get(self._theme, "浅色")
        seg = ctk.CTkSegmentedButton(
            foot, values=["跟随", "浅色", "深色"], font=self.f_small,
            height=28, corner_radius=8, selected_color=C_PRIMARY,
            selected_hover_color=C_PRIMARY_HOVER, unselected_color=C_SIDE_HOVER,
            unselected_hover_color="#2c3552", text_color="#c8cfdd",
            command=self._on_theme_changed)
        seg.set(cur)
        seg.pack(fill="x", pady=(4, 10))
        ctk.CTkLabel(foot, text="西电超算 · 分子动力学", font=ctk.CTkFont(FONT_FAMILY, 10),
                     text_color="#5b657f").pack()

        # ---- 内容列：标题区 + 页面容器 + 状态栏 ----
        content = ctk.CTkFrame(self, fg_color="transparent")
        content.grid(row=0, column=1, sticky="nsew")
        content.grid_rowconfigure(1, weight=1)
        content.grid_columnconfigure(0, weight=1)

        self._header = ctk.CTkFrame(content, fg_color="transparent")
        self._header.grid(row=0, column=0, sticky="ew", padx=28, pady=(24, 6))
        self._header.grid_columnconfigure(0, weight=1)
        self.lbl_page_title = ctk.CTkLabel(self._header, text="环境配置",
                                           font=self.f_page, text_color=C_TEXT,
                                           anchor="w")
        self.lbl_page_title.grid(row=0, column=0, sticky="w")
        self._chip_box = ctk.CTkFrame(self._header, fg_color="transparent")
        self._chip_box.grid(row=0, column=1, sticky="e")
        self.chip_backend = self._chip(self._chip_box, "后端: —")
        self.chip_backend.pack(side="right", padx=(8, 0))
        self.chip_gmx = self._chip(self._chip_box, "gmx: 检测中…")
        self.chip_gmx.pack(side="right")
        self.lbl_page_desc = ctk.CTkLabel(self._header, text="", font=self.f_sub,
                                          text_color=C_MUTED, anchor="w")
        self.lbl_page_desc.grid(row=1, column=0, columnspan=2, sticky="w",
                                pady=(3, 0))

        pages_host = ctk.CTkFrame(content, fg_color="transparent")
        pages_host.grid(row=1, column=0, sticky="nsew", padx=20, pady=(6, 6))
        pages_host.grid_rowconfigure(0, weight=1)
        pages_host.grid_columnconfigure(0, weight=1)

        self._build_env_tab(pages_host)
        self._build_input_tab(pages_host)
        self._build_params_tab(pages_host)
        self._build_run_tab(pages_host)

        # ---- 状态栏 ----
        sbar = ctk.CTkFrame(self, height=34, corner_radius=0,
                            fg_color=C_CARD, border_width=0)
        sbar.grid(row=1, column=1, sticky="ew")
        ctk.CTkFrame(sbar, height=1, fg_color=C_BORDER).pack(fill="x")
        sb = ctk.CTkFrame(sbar, fg_color="transparent")
        sb.pack(fill="x", padx=16, pady=(6, 4))
        self.lbl_status_left = ctk.CTkLabel(sb, text="就绪", font=self.f_small,
                                            text_color=C_MUTED, anchor="w")
        self.lbl_status_left.pack(side="left")
        self.lbl_jobdir = ctk.CTkLabel(sb, text="", font=self.f_small,
                                       text_color=C_MUTED, anchor="e")
        self.lbl_jobdir.pack(side="right")

        self._show_page("env")

    def _show_page(self, key: str) -> None:
        titles = {"env": ("环境配置", "选择计算后端，检测或自动安装 GROMACS 环境"),
                  "input": ("输入与检测", "导入 CHARMM-GUI 输出目录或压缩包，自动识别体系"),
                  "params": ("模拟参数", "按阶段微调参数，支持保存/加载配置"),
                  "run": ("运行与日志", "启动模拟，实时监控进度、阶段状态与日志")}
        self.lbl_page_title.configure(text=titles[key][0])
        self.lbl_page_desc.configure(text=titles[key][1])
        for k, btn in self._nav_buttons.items():
            if k == key:
                btn.configure(fg_color=C_PRIMARY, text_color="#ffffff",
                              hover_color=C_PRIMARY_HOVER)
            else:
                btn.configure(fg_color="transparent", text_color=C_SIDE_TEXT,
                              hover_color=C_SIDE_HOVER)
        for k, page in self._pages.items():
            if k == key:
                # CTk 6.0 的 CTkScrollableFrame 是包装器：真正参与父容器堆叠
                # 的是其内部 _parent_frame，tkraise 必须作用在它上面。
                pf = getattr(page, "_parent_frame", page)
                pf.tkraise()

    # ---------------- 页面骨架 ----------------
    def _page(self, parent, key: str) -> ctk.CTkScrollableFrame:
        page = ctk.CTkScrollableFrame(parent, fg_color="transparent",
                                      scrollbar_button_color=C_BORDER,
                                      scrollbar_button_hover_color=C_MUTED)
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_columnconfigure(0, weight=1)
        self._pages[key] = page
        return page

    # ---------------- 页 1：环境配置 ----------------
    def _build_env_tab(self, parent) -> None:
        page = self._page(parent, "env")

        card = self._card(page, "运行后端（计算在哪里跑）",
                          "推荐 WSL2：界面留在 Windows，模拟在 Linux 环境运行（GROMACS 原生支持最好）")
        card.grid(row=0, column=0, sticky="ew", pady=(4, 14))
        inner = card._inner  # type: ignore[attr-defined]
        self.var_backend = tk.StringVar(
            value=config.BACKEND_LOCAL if not IS_WINDOWS else config.BACKEND_WSL)
        if IS_WINDOWS:
            ctk.CTkRadioButton(inner, text="WSL2 Linux（推荐）", font=self.f_body,
                               variable=self.var_backend, value=config.BACKEND_WSL,
                               command=self._on_backend_changed,
                               fg_color=C_PRIMARY, hover_color=C_PRIMARY_HOVER,
                               text_color=C_TEXT).pack(anchor="w", pady=3)
            ctk.CTkRadioButton(inner, text="本机直接运行（gmx 与软件同机）",
                               font=self.f_body, variable=self.var_backend,
                               value=config.BACKEND_LOCAL,
                               command=self._on_backend_changed,
                               fg_color=C_PRIMARY, hover_color=C_PRIMARY_HOVER,
                               text_color=C_TEXT).pack(anchor="w", pady=3)

            row = ctk.CTkFrame(inner, fg_color="transparent")
            row.pack(fill="x", pady=(10, 2))
            ctk.CTkLabel(row, text="WSL 状态:", font=self.f_body,
                         text_color=C_MUTED).pack(side="left")
            self.lbl_wsl = ctk.CTkLabel(row, text="检测中...", font=self.f_body,
                                        text_color=C_TEXT)
            self.lbl_wsl.pack(side="left", padx=8)
            self._btn(row, "检测 WSL", self._detect_env_async,
                      width=110).pack(side="right")

            row = ctk.CTkFrame(inner, fg_color="transparent")
            row.pack(fill="x", pady=4)
            ctk.CTkLabel(row, text="WSL 发行版:", font=self.f_body,
                         text_color=C_MUTED).pack(side="left")
            self.var_distro = tk.StringVar()
            self.cmb_distro = Combo(row, textvariable=self.var_distro, width=200,
                                    font=self.f_body, height=32, corner_radius=8,
                                    border_color=C_BORDER, fg_color=C_CARD,
                                    button_color=C_GHOST, button_hover_color="#dde4f1",
                                    dropdown_hover_color=C_PRIMARY,
                                    text_color=C_TEXT, dropdown_text_color=C_TEXT,
                                    command=lambda _v: self._on_distro_changed())
            self.cmb_distro.pack(side="left", padx=8)
            self._btn(row, "安装 / 配置 WSL 发行版", self._install_wsl,
                      width=200).pack(side="right")
        else:
            ctk.CTkLabel(inner, text="本机直接运行（Linux 版：gmx 在本机运行）",
                         font=self.f_body_b, text_color=C_TEXT).pack(anchor="w")
            self.var_backend.set(config.BACKEND_LOCAL)
            self.lbl_wsl = ctk.CTkLabel(inner, text="")

        card2 = self._card(page, "GROMACS（gmx）",
                           "检测不到时点「自动安装」——软件自动下载 micromamba 并安装 conda-forge 版 GROMACS，无需管理员权限")
        card2.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        i2 = card2._inner  # type: ignore[attr-defined]
        row = ctk.CTkFrame(i2, fg_color="transparent")
        row.pack(fill="x", pady=3)
        ctk.CTkLabel(row, text="状态:", font=self.f_body,
                     text_color=C_MUTED, width=60, anchor="w").pack(side="left")
        self.lbl_gmx = ctk.CTkLabel(row, text="检测中...", font=self.f_body,
                                    text_color=C_TEXT)
        self.lbl_gmx.pack(side="left", padx=8)
        btns = ctk.CTkFrame(row, fg_color="transparent")
        btns.pack(side="right")
        self._btn(btns, "环境自测", self._self_test_env, width=100).pack(side="left", padx=4)
        self._btn(btns, "检测 GROMACS", self._check_gmx_now,
                  width=120).pack(side="left", padx=4)
        self._btn(btns, "自动安装 GROMACS", self._install_gmx,
                  primary=True, width=160).pack(side="left", padx=4)

        def _field_row(label, var, width, hint="", hint_color=C_MUTED):
            r = ctk.CTkFrame(i2, fg_color="transparent")
            r.pack(fill="x", pady=4)
            ctk.CTkLabel(r, text=label, font=self.f_body, text_color=C_MUTED,
                         width=190, anchor="w").pack(side="left")
            ctk.CTkEntry(r, textvariable=var, width=width, font=self.f_body,
                         height=32, corner_radius=8, border_color=C_BORDER,
                         fg_color=C_CARD, text_color=C_TEXT).pack(side="left")
            if hint:
                ctk.CTkLabel(r, text=hint, font=self.f_small,
                             text_color=hint_color).pack(side="left", padx=10)

        self.var_gmx = tk.StringVar(value=config.GMX_EXE_DEFAULT)
        _field_row("gmx 命令（可手动指定）", self.var_gmx, 420)
        self.var_gmx_version = tk.StringVar(value="")
        _field_row("GROMACS 版本（留空=最新）", self.var_gmx_version, 120,
                   "Martini v2 体系建议填 2023.3", C_WARN)
        self.var_channels = tk.StringVar(value=",".join(config.CONDA_CHANNELS_DEFAULT))
        _field_row("conda 通道（逗号分隔）", self.var_channels, 420,
                   "国内加速: https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud")

        self.env_progress = Progress(i2, height=8, corner_radius=4,
                                     progress_color=C_PRIMARY)
        self.env_progress.pack(fill="x", pady=(12, 4))
        ctk.CTkLabel(i2, font=self.f_small, text_color=C_MUTED, justify="left",
                     wraplength=860, anchor="w",
                     text="提示：安装输出会实时显示在「运行与日志」页面；装好后 gmx 路径自动回填。"
                     ).pack(fill="x")

    # ---------------- 页 2：输入与检测 ----------------
    def _build_input_tab(self, nb) -> None:
        page = self._page(nb, "input")

        card = self._card(page, "CHARMM-GUI 输出",
                          "选择 GROMACS 输出文件夹（或其父目录）；也可直接导入 tgz / zip 压缩包，自动解压定位")
        card.grid(row=0, column=0, sticky="ew", pady=(4, 14))
        inner = card._inner  # type: ignore[attr-defined]
        row = ctk.CTkFrame(inner, fg_color="transparent")
        row.pack(fill="x", pady=4)
        self.var_input_dir = tk.StringVar()
        ctk.CTkEntry(row, textvariable=self.var_input_dir, font=self.f_body,
                     height=36, corner_radius=8, border_color=C_BORDER,
                     fg_color=C_CARD, text_color=C_TEXT,
                     placeholder_text="例如 D:/md_projects/charmm-gui-xxxx").pack(
            side="left", fill="x", expand=True, padx=(0, 8))
        self._btn(row, "浏览...", self._pick_input_dir, width=90).pack(side="left", padx=4)
        self._btn(row, "导入压缩包", self._import_zip, width=110).pack(side="left", padx=4)
        self._btn(row, "开始检测", self._do_detect, primary=True,
                  width=110).pack(side="left", padx=4)

        row2 = ctk.CTkFrame(inner, fg_color="transparent")
        row2.pack(fill="x", pady=(8, 0))
        self.var_dry = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(row2, text="模拟运行 (dry-run)：不调用 gmx，体验完整流程",
                        variable=self.var_dry, font=self.f_body,
                        fg_color=C_PRIMARY, hover_color=C_PRIMARY_HOVER,
                        text_color=C_TEXT).pack(side="left")
        ctk.CTkLabel(row2, text="gmx 环境请在「环境配置」页检测 / 安装",
                     font=self.f_small, text_color=C_MUTED).pack(side="left",
                                                                 padx=16)

        card2 = self._card(page, "检测结果")
        card2.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        i2 = card2._inner  # type: ignore[attr-defined]
        g = ctk.CTkFrame(i2, fg_color="transparent")
        g.pack(fill="x")
        g.grid_columnconfigure(1, weight=1)

        def _kv(r, k):
            ctk.CTkLabel(g, text=k, font=self.f_body, text_color=C_MUTED,
                         width=110, anchor="w").grid(row=r, column=0, sticky="w",
                                                     pady=4)
            box = ctk.CTkFrame(g, fg_color="transparent")
            box.grid(row=r, column=1, sticky="w", padx=8)
            return box

        b = _kv(0, "力场")
        self.lbl_ff = ctk.CTkLabel(b, text="—", font=self.f_body_b, text_color=C_TEXT)
        self.lbl_ff.pack(side="left")
        b = _kv(1, "模拟类型")
        self.var_sim_type = tk.StringVar(value=AUTO_LABEL)
        self.cmb_sim_type = Combo(b, textvariable=self.var_sim_type, width=280,
                                  font=self.f_body, height=32, corner_radius=8,
                                  values=[AUTO_LABEL] + list(config.SIM_TYPE_NAMES.values()),
                                  border_color=C_BORDER, fg_color=C_CARD,
                                  button_color=C_GHOST, button_hover_color="#dde4f1",
                                  dropdown_hover_color=C_PRIMARY, text_color=C_TEXT,
                                  dropdown_text_color=C_TEXT)
        self.cmb_sim_type.pack(side="left")
        b = _kv(2, "原子数")
        self.lbl_atoms = ctk.CTkLabel(b, text="—", font=self.f_body_b, text_color=C_TEXT)
        self.lbl_atoms.pack(side="left")
        b = _kv(3, "发现阶段")
        self.lbl_stages = ctk.CTkLabel(b, text="—", font=self.f_body,
                                       text_color=C_TEXT, wraplength=760,
                                       justify="left", anchor="w")
        self.lbl_stages.pack(side="left")

        self.lbl_warn = ctk.CTkLabel(page, text="", font=self.f_body,
                                     text_color=C_WARN, wraplength=940,
                                     justify="left", anchor="w")
        self.lbl_warn.grid(row=2, column=0, sticky="w", padx=4)

    # ---------------- 页 3：模拟参数 ----------------
    def _build_params_tab(self, nb) -> None:
        page = self._page(nb, "params")

        card = self._card(page, "运行设置",
                          "输出目录将创建每次运行的作业文件夹（含各阶段输入与结果）")
        card.grid(row=0, column=0, sticky="ew", pady=(4, 14))
        inner = card._inner  # type: ignore[attr-defined]
        row = ctk.CTkFrame(inner, fg_color="transparent")
        row.pack(fill="x", pady=3)
        ctk.CTkLabel(row, text="输出目录", font=self.f_body, text_color=C_MUTED,
                     width=110, anchor="w").pack(side="left")
        self.var_output_dir = tk.StringVar()
        ctk.CTkEntry(row, textvariable=self.var_output_dir, font=self.f_body,
                     height=34, corner_radius=8, border_color=C_BORDER,
                     fg_color=C_CARD, text_color=C_TEXT).pack(
            side="left", fill="x", expand=True, padx=8)
        self._btn(row, "浏览...", self._pick_output_dir, width=90).pack(side="left")

        row = ctk.CTkFrame(inner, fg_color="transparent")
        row.pack(fill="x", pady=(10, 3))
        ctk.CTkLabel(row, text="并行线程数 ntomp", font=self.f_body,
                     text_color=C_MUTED, width=140, anchor="w").pack(side="left")
        self.var_ntomp = tk.StringVar(value="0")
        ctk.CTkEntry(row, textvariable=self.var_ntomp, width=80, font=self.f_body,
                     height=34, corner_radius=8, border_color=C_BORDER,
                     fg_color=C_CARD, text_color=C_TEXT).pack(side="left", padx=8)
        ctk.CTkLabel(row, text="(0 = 自动)", font=self.f_small,
                     text_color=C_MUTED).pack(side="left")
        self.var_gpu = tk.BooleanVar(value=False)
        self.chk_gpu = ctk.CTkCheckBox(row, text="GPU 加速 (-nb gpu，出错自动回退 CPU)",
                                       variable=self.var_gpu, font=self.f_body,
                                       fg_color=C_PRIMARY, hover_color=C_PRIMARY_HOVER,
                                       text_color=C_TEXT)
        self.chk_gpu.pack(side="left", padx=28)
        self.lbl_gpu_hint = ctk.CTkLabel(row, text="", font=self.f_small,
                                         text_color=C_WARN)
        self.lbl_gpu_hint.pack(side="left")

        card2 = self._card(page, "阶段参数",
                           "选择阶段逐项配置；「恢复默认」回到 CHARMM-GUI 原值")
        card2.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        i2 = card2._inner  # type: ignore[attr-defined]
        row = ctk.CTkFrame(i2, fg_color="transparent")
        row.pack(fill="x")
        ctk.CTkLabel(row, text="阶段", font=self.f_body, text_color=C_MUTED,
                     width=50, anchor="w").pack(side="left")
        self.var_stage_sel = tk.StringVar()
        self.cmb_stage = Combo(row, textvariable=self.var_stage_sel, width=320,
                               font=self.f_body, height=34, corner_radius=8,
                               border_color=C_BORDER, fg_color=C_CARD,
                               button_color=C_GHOST, button_hover_color="#dde4f1",
                               dropdown_hover_color=C_PRIMARY, text_color=C_TEXT,
                               dropdown_text_color=C_TEXT,
                               command=lambda _v: self._on_stage_selected())
        self.cmb_stage.pack(side="left", padx=8)
        btns = ctk.CTkFrame(row, fg_color="transparent")
        btns.pack(side="right")
        self._btn(btns, "加载参数…", self._load_params, width=110).pack(side="left", padx=4)
        self._btn(btns, "保存参数…", self._save_params, width=110).pack(side="left", padx=4)
        self._btn(btns, "恢复默认", self._restore_defaults, width=100).pack(side="left", padx=4)

        self.stage_form = ctk.CTkFrame(i2, fg_color=C_CARD_SUB, corner_radius=10,
                                       border_width=1, border_color=C_BORDER)
        self.stage_form.pack(fill="both", expand=True, pady=(14, 2))
        self._form_pad = ctk.CTkFrame(self.stage_form, fg_color="transparent")
        self._form_pad.pack(fill="both", expand=True, padx=16, pady=12)

    # ---------------- 页 4：运行与日志 ----------------
    def _build_run_tab(self, nb) -> None:
        page = self._page(nb, "run")

        # 顶部工具条
        bar = ctk.CTkFrame(page, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", pady=(2, 12))
        self.btn_start = self._btn(bar, "▶  启动模拟", lambda: self._start(False),
                                   primary=True, width=140)
        self.btn_start.pack(side="left")
        self.btn_resume = self._btn(bar, "从失败处继续", lambda: self._start(True),
                                    width=120)
        self.btn_resume.pack(side="left", padx=6)
        self.btn_cancel = self._btn(bar, "取消运行", self._cancel_run, width=90)
        self.btn_cancel.pack(side="left", padx=6)
        self.btn_open = self._btn(bar, "打开输出目录", self._open_output_dir,
                                  width=120)
        self.btn_open.pack(side="left", padx=6)
        self._btn(bar, "作业历史", self._show_history, width=90).pack(side="left")
        self.lbl_action = ctk.CTkLabel(bar, text="尚未开始运行", font=self.f_body_b,
                                       text_color=C_MUTED, anchor="e")
        self.lbl_action.pack(side="right")

        # 统计卡片行
        stats = ctk.CTkFrame(page, fg_color="transparent")
        stats.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        stats.grid_columnconfigure((0, 1, 2, 3), weight=1, uniform="s")

        def _stat_col(col):
            c = ctk.CTkFrame(stats, corner_radius=12, fg_color=C_CARD,
                             border_width=1, border_color=C_BORDER)
            c.grid(row=0, column=col, sticky="nsew", padx=(0, 12) if col < 3 else 0)
            ctk.CTkLabel(c, text="", font=self.f_stat, text_color=C_MUTED,
                         anchor="w").pack(anchor="w", padx=16, pady=(10, 0))
            val = ctk.CTkLabel(c, text="—", font=self.f_num, text_color=C_TEXT,
                               anchor="w")
            val.pack(anchor="w", padx=16)
            return c, val

        col_over, self.lbl_overall = _stat_col(0)
        ctk.CTkLabel(col_over, text="总体进度", font=self.f_stat,
                     text_color=C_MUTED, anchor="w").pack_forget()
        ctk.CTkLabel(col_over, text="总体进度", font=self.f_stat,
                     text_color=C_MUTED, anchor="w").pack(anchor="w", padx=16,
                                                          pady=(10, 0))
        self.lbl_overall.configure(text="0%")
        self.pb_overall = Progress(col_over, height=8, corner_radius=4,
                                   progress_color=C_PRIMARY)
        self.pb_overall.pack(fill="x", padx=16, pady=(4, 14))

        col_st, self.lbl_stage_pct = _stat_col(1)
        ctk.CTkLabel(col_st, text="当前阶段进度", font=self.f_stat,
                     text_color=C_MUTED, anchor="w").pack_forget()
        ctk.CTkLabel(col_st, text="当前阶段进度", font=self.f_stat,
                     text_color=C_MUTED, anchor="w").pack(anchor="w", padx=16,
                                                          pady=(10, 0))
        self.lbl_stage_pct.configure(text="0%")
        self.pb_stage = Progress(col_st, height=8, corner_radius=4,
                                 progress_color="#22c55e")
        self.pb_stage.pack(fill="x", padx=16, pady=(4, 14))

        col_eta, self.lbl_eta_val = _stat_col(2)
        ctk.CTkLabel(col_eta, text="预计剩余", font=self.f_stat,
                     text_color=C_MUTED, anchor="w").pack_forget()
        ctk.CTkLabel(col_eta, text="预计剩余", font=self.f_stat,
                     text_color=C_MUTED, anchor="w").pack(anchor="w", padx=16,
                                                          pady=(10, 0))
        ctk.CTkLabel(col_eta, text="—", font=self.f_stat, text_color=C_MUTED,
                     anchor="w").pack(anchor="w", padx=16, pady=(0, 14))
        col_el, self.lbl_elapsed_val = _stat_col(3)
        ctk.CTkLabel(col_el, text="已运行", font=self.f_stat,
                     text_color=C_MUTED, anchor="w").pack_forget()
        ctk.CTkLabel(col_el, text="已运行", font=self.f_stat,
                     text_color=C_MUTED, anchor="w").pack(anchor="w", padx=16,
                                                          pady=(10, 0))
        ctk.CTkLabel(col_el, text="—", font=self.f_stat, text_color=C_MUTED,
                     anchor="w").pack(anchor="w", padx=16, pady=(0, 14))

        # 阶段列表
        card2 = self._card(page, "阶段状态")
        card2.grid(row=2, column=0, sticky="ew", pady=(0, 14))
        i2 = card2._inner  # type: ignore[attr-defined]
        self.tree_steps = ttk.Treeview(i2, columns=("name", "status"),
                                       show="tree headings", height=7,
                                       selectmode="none", style="Md.Treeview")
        self.tree_steps.heading("#0", text="阶段")
        self.tree_steps.heading("name", text="类型")
        self.tree_steps.heading("status", text="状态")
        self.tree_steps.column("#0", width=280, anchor="w")
        self.tree_steps.column("name", width=150, anchor="center")
        self.tree_steps.column("status", width=130, anchor="center")
        self.tree_steps.pack(fill="x")
        self.tree_steps.tag_configure("ok", foreground="#16a34a")
        self.tree_steps.tag_configure("run", foreground="#3563e9")
        self.tree_steps.tag_configure("fail", foreground="#dc2626")
        self.tree_steps.tag_configure("row0", background=self._ttk_bg)
        self.tree_steps.tag_configure("row1", background=self._ttk_alt)

        # 日志
        card3 = self._card(page, "运行日志")
        card3.grid(row=3, column=0, sticky="nsew")
        page.grid_rowconfigure(3, weight=1)
        i3 = card3._inner  # type: ignore[attr-defined]
        self.log_text = ctk.CTkTextbox(i3, font=self.f_log, corner_radius=8,
                                       height=220,
                                       fg_color=("#0f1420", "#0b0e17"),
                                       text_color="#d5dbe8", wrap="word",
                                       border_width=1, border_color=C_BORDER)
        self.log_text.pack(fill="both", expand=True)
        _tb = self.log_text._textbox  # 内部 tk.Text：tag / state / see 由它承担
        _tb.configure(state="disabled")
        for tag, color in TAG_COLORS.items():
            _tb.tag_configure(tag, foreground=color[0] if not isinstance(color, tuple)
                              else (color[1] if ctk.get_appearance_mode() == "Dark"
                                    else color[0]))

    # ==================================================================
    # 页 2 逻辑（输入与检测）—— 与旧版一致
    # ==================================================================
    def _pick_input_dir(self) -> None:
        d = filedialog.askdirectory(title="选择 CHARMM-GUI 输出目录")
        if d:
            self.var_input_dir.set(d)

    def _import_zip(self) -> None:
        f = filedialog.askopenfilename(
            title="选择 CHARMM-GUI 输出的压缩包",
            filetypes=[("压缩包 (zip/tgz/tar.gz)", "*.zip *.tgz *.tar.gz *.tar"),
                       ("所有文件", "*.*")])
        if not f:
            return
        self.logger.info(f"正在解压: {f}")
        try:
            res = detect.extract_charmmgui_archive(f)
        except Exception as exc:
            messagebox.showerror("解压失败", f"无法解压压缩包:\n{exc}")
            return
        if not res.ok or res.gromacs_dir is None:
            self.logger.warn("压缩包未找到输出，诊断信息:\n" + res.hint)
            messagebox.showwarning("未找到输出", res.hint)
            return
        self.var_input_dir.set(str(res.gromacs_dir))
        self.logger.info(f"压缩包已解压并定位到: {res.gromacs_dir}")
        self._do_detect()

    # ==================================================================
    # 页 1 逻辑（环境）—— 与旧版一致，仅颜色接口替换
    # ==================================================================
    def _apply_config_to_ui(self) -> None:
        backend_default = config.BACKEND_LOCAL if not IS_WINDOWS else config.BACKEND_WSL
        self.var_backend.set(str(self.cfg.get("backend", backend_default))
                             if IS_WINDOWS else config.BACKEND_LOCAL)
        self.var_distro.set(str(self.cfg.get("wsl_distro", "")))
        self.var_gmx.set(str(self.cfg.get("gmx_path", config.GMX_EXE_DEFAULT)))
        self.var_gmx_version.set(str(self.cfg.get("gmx_version", "")))
        ch = self.cfg.get("channels") or config.CONDA_CHANNELS_DEFAULT
        self.var_channels.set(",".join(str(c) for c in ch))

    def _persist_ui_config(self) -> None:
        self.cfg["backend"] = self.var_backend.get()
        self.cfg["wsl_distro"] = self.var_distro.get().strip()
        self.cfg["gmx_path"] = self.var_gmx.get().strip() or config.GMX_EXE_DEFAULT
        self.cfg["gmx_version"] = self.var_gmx_version.get().strip()
        ch = [c.strip() for c in self.var_channels.get().split(",") if c.strip()]
        self.cfg["channels"] = ch or list(config.CONDA_CHANNELS_DEFAULT)
        self.cfg["theme"] = self._theme
        try:
            envsetup.save_config(self.cfg)
        except OSError as exc:
            self.logger.warn(f"保存配置失败: {exc}")

    def _channels_list(self):
        ch = [c.strip() for c in self.var_channels.get().split(",") if c.strip()]
        return ch or list(config.CONDA_CHANNELS_DEFAULT)

    def _on_backend_changed(self) -> None:
        self.backend = make_backend(self.var_backend.get(),
                                    self.var_distro.get().strip())
        self._persist_ui_config()
        self.chip_backend.configure(text="后端: " + self.backend.display)
        self.logger.info(f"运行后端已切换为: {self.backend.display}")

    def _on_distro_changed(self) -> None:
        self.backend = make_backend(self.var_backend.get(),
                                    self.var_distro.get().strip())
        self._persist_ui_config()
        self._check_gmx_now()

    def _startup_env_detect(self) -> None:
        if IS_WINDOWS:
            self._detect_env_async()
        else:
            self._check_gmx_now()
        self.chip_backend.configure(text="后端: " + self.backend.display)

    def _detect_env_async(self) -> None:
        self.lbl_wsl.configure(text="检测中...")
        self.lbl_gmx.configure(text="检测中...")
        gmx_exe = self.var_gmx.get().strip() or config.GMX_EXE_DEFAULT
        kind = self.var_backend.get()
        distro = self.var_distro.get().strip()
        threading.Thread(target=self._detect_env_worker,
                         args=(kind, distro, gmx_exe), daemon=True).start()

    def _detect_env_worker(self, kind: str, distro: str, gmx_exe: str) -> None:
        wsl = envsetup.detect_wsl()
        if kind == config.BACKEND_WSL and not distro and wsl.get("distros"):
            distro = str(wsl.get("default") or wsl["distros"][0])
        backend = make_backend(kind, distro)
        info = envsetup.probe_gmx_info(backend, gmx_exe)
        self.event_q.put({"type": "env_detect", "wsl": wsl, "distro": distro,
                          "gmx_ok": info.get("ok"), "gmx_msg": info.get("msg", ""),
                          "gmx_info": info})

    def _check_gmx_now(self) -> None:
        self.lbl_gmx.configure(text="检测中...")
        gmx_exe = self.var_gmx.get().strip() or config.GMX_EXE_DEFAULT
        kind = self.var_backend.get()
        distro = self.var_distro.get().strip()
        threading.Thread(target=self._check_gmx_worker,
                         args=(kind, distro, gmx_exe), daemon=True).start()

    def _check_gmx_worker(self, kind: str, distro: str, gmx_exe: str) -> None:
        backend = make_backend(kind, distro)
        info = envsetup.probe_gmx_info(backend, gmx_exe)
        self.event_q.put({"type": "gmx_check", "ok": info.get("ok"),
                          "msg": info.get("msg", ""), "gmx_info": info})

    def _install_gmx(self) -> None:
        self._persist_ui_config()
        kind = self.var_backend.get()
        distro = self.var_distro.get().strip()
        if kind == config.BACKEND_WSL:
            wsl = envsetup.detect_wsl()
            if not wsl.get("available"):
                messagebox.showwarning(
                    "WSL 不可用",
                    "本机未检测到 WSL2。请先点击“安装/配置 WSL 发行版”完成 WSL 安装并重启，"
                    "然后再自动安装 GROMACS。")
                return
            if not distro:
                if not wsl.get("distros"):
                    messagebox.showwarning("WSL", "WSL 可用但未安装发行版，请先安装发行版。")
                    return
                distro = str(wsl.get("default") or wsl["distros"][0])
                self.var_distro.set(distro)
        self.env_progress.start(12)
        threading.Thread(target=self._install_gmx_worker,
                         args=(kind, distro), daemon=True).start()

    def _install_gmx_worker(self, kind: str, distro: str) -> None:
        def on_output(line: str) -> None:
            self.logger.info("[安装] " + line)
        ver = self.var_gmx_version.get().strip() or None
        ok, msg = envsetup.install_gmx(kind, distro, self._channels_list(),
                                       on_output, gmx_version=ver)
        self.event_q.put({"type": "install_done", "ok": ok, "msg": msg,
                          "kind": kind, "distro": distro})

    def _install_wsl(self) -> None:
        if messagebox.askyesno(
                "安装 WSL 发行版",
                "将执行: wsl --install -d Ubuntu\n\n"
                "首次安装 WSL 需要管理员权限，Windows 会弹出 UAC 确认，"
                "部分情况需要重启电脑。是否继续？"):
            self.env_progress.start(12)
            threading.Thread(target=self._install_wsl_worker, daemon=True).start()

    def _install_wsl_worker(self) -> None:
        def on_output(line: str) -> None:
            self.logger.info("[WSL安装] " + line)
        ok, msg = envsetup.install_wsl_distro("Ubuntu", on_output)
        self.event_q.put({"type": "install_done", "ok": ok, "msg": msg,
                          "kind": "wsl_install", "distro": ""})

    def _show_gmx_status(self, ok, msg: str) -> None:
        if ok:
            self.lbl_gmx.configure(text="已就绪: " + msg, text_color=C_OK)
            self.chip_gmx.configure(text="gmx: 已就绪")
        else:
            self.lbl_gmx.configure(
                text="不可用: " + msg + "（可点击“自动安装 GROMACS”）",
                text_color=C_ERR)
            self.chip_gmx.configure(text="gmx: 未安装")

    def _apply_gmx_info(self, info) -> None:
        if not isinstance(info, dict) or not info.get("ok"):
            return
        self.gmx_info = info
        gpu = bool(info.get("gpu"))
        if gpu:
            self.chk_gpu.configure(state="normal")
            self.lbl_gpu_hint.configure(text="")
        else:
            self.chk_gpu.configure(state="disabled")
            self.var_gpu.set(False)
            self.lbl_gpu_hint.configure(text="当前 gmx 为 CPU 版，不支持 GPU 加速")

    def _self_test_env(self) -> None:
        self._persist_ui_config()
        backend = make_backend(self.var_backend.get(), self.var_distro.get().strip())
        gmx_exe = self.var_gmx.get().strip() or config.GMX_EXE_DEFAULT
        self.env_progress.start(12)
        threading.Thread(target=self._self_test_worker,
                         args=(backend, gmx_exe), daemon=True).start()

    def _self_test_worker(self, backend: Backend, gmx_exe: str) -> None:
        def on_output(line: str) -> None:
            self.logger.info("[环境自测] " + line)
        work = Path(tempfile.mkdtemp(prefix="mdwf_envtest_"))
        ok, msg = envsetup.self_test(backend, gmx_exe, on_output, work)
        self.event_q.put({"type": "self_test_done", "ok": ok, "msg": msg})

    # ==================================================================
    # 检测 / 阶段填充 / 参数表单 —— 逻辑与旧版一致
    # ==================================================================
    def _do_detect(self) -> None:
        sel = self.var_input_dir.get().strip()
        if not sel:
            messagebox.showwarning("提示", "请先选择 CHARMM-GUI 输出目录。")
            return
        res = detect.detect(sel)
        self.detect_result = res
        self.stage_cfgs = {}
        self._failed_index = None
        if not res.ok:
            messagebox.showerror("检测失败", res.error)
            return
        self.lbl_ff.configure(text=res.ff_name or "未知（请手动选择类型）")
        self.cmb_sim_type.set(AUTO_LABEL)
        self.lbl_atoms.configure(text=str(res.atoms) if res.atoms is not None else "未知")
        self.lbl_stages.configure(text="、".join(
            f"{s.key}({config.KIND_NAMES.get(s.kind, '')})" for s in res.stages))
        warn = res.warnings
        if res.sim_type:
            self.logger.info(f"检测成功: 力场={res.ff_name}, 类型={res.sim_type_name}, "
                             f"阶段数={len(res.stages)}")
            self.lbl_warn.configure(text=("注意: " + "；".join(warn)) if warn else "")
        else:
            self.lbl_warn.configure(text="注意: " + "；".join(
                warn or ["未识别到力场，请在模拟类型中手动选择。"]))
        if res.structure_path is None:
            self.lbl_warn.configure(text="注意: 未找到结构文件（step5_input.gro/.pdb），"
                                         "真实运行将无法进行，仅支持 dry-run 体验。")
        if not self.var_output_dir.get().strip():
            self.var_output_dir.set(self._default_output_root())
        self._populate_stages()

    def _populate_stages(self) -> None:
        res = self.detect_result
        if not res:
            return
        self.cmb_stage.set("")
        self.cmb_stage["values"] = [f"{i + 1:02d}. {s.name}" for i, s in enumerate(res.stages)]
        if res.stages:
            self.cmb_stage.current(0)
        self._cur_stage_key = None
        self._on_stage_selected()
        for item in self.tree_steps.get_children():
            self.tree_steps.delete(item)
        self._stage_items = {}
        self._stage_stripes = {}
        for i, s in enumerate(res.stages):
            stripe = f"row{i % 2}"
            item = self.tree_steps.insert("", "end",
                                          text=f"{i + 1:02d}. {s.name}",
                                          values=(config.KIND_NAMES.get(s.kind, ""), "待运行"),
                                          tags=(stripe,))
            self._stage_items[s.key] = item
            self._stage_stripes[s.key] = stripe

    def _set_stage_status(self, key: str, status: str) -> None:
        item = self._stage_items.get(key)
        if not item:
            return
        color = {"完成": "ok", "运行中": "run", "失败": "fail",
                 "已取消": "fail"}.get(status)
        stripe = self._stage_stripes.get(key, "row0")
        tags = (stripe, color) if color else (stripe,)
        values = self.tree_steps.item(item, "values")
        self.tree_steps.item(item, values=(values[0], status), tags=tags)

    def _pick_output_dir(self) -> None:
        d = filedialog.askdirectory(title="选择输出目录")
        if d:
            self.var_output_dir.set(d)

    def _default_output_root(self) -> str:
        home = Path.home()
        for cand in (home / "Desktop", home):
            if cand.exists():
                return str(cand / "MD_sim")
        return str(home)

    def _stage_by_key(self, key: str) -> Optional[detect.Stage]:
        if not self.detect_result:
            return None
        for s in self.detect_result.stages:
            if s.key == key:
                return s
        return None

    def _on_stage_selected(self) -> None:
        self._save_current_form()
        if not self.detect_result:
            return
        idx = self.cmb_stage.current()
        if idx < 0:
            return
        stage = self.detect_result.stages[idx]
        self._cur_stage_key = stage.key
        self._build_stage_form(stage)

    def _stage_defaults(self, stage: detect.Stage) -> dict:
        p = stage.parsed
        fb = config.FALLBACK_PARAMS[self._resolved_sim_type() or config.ALL_ATOM]
        is_prod = stage.kind == config.KIND_PRODUCTION
        dur_ps = mdp_mod.stage_duration_ps(p)
        dur_str = ""
        if dur_ps and dur_ps > 0:
            dur_str = f"{dur_ps / 1000.0:.4g}" if is_prod else f"{dur_ps:.3g}"
        defaults = {
            "duration": dur_str,
            "unit": "ns" if is_prod else "ps",
            "temp": mdp_mod.first_float(p, "ref_t", float(fb["temperature_k"])),
            "press": mdp_mod.first_float(p, "ref_p", float(fb["pressure_bar"])),
            "emtol": mdp_mod.to_float(p, "emtol", 1000.0),
            "restraints": "-DPOSRES" in str(mdp_mod.get(p, "define", "")).upper(),
            "nstxout": mdp_mod.to_int(p, "nstxout-compressed",
                                      int(fb["nstxout_compressed"])),
            "nstenergy": mdp_mod.to_int(p, "nstenergy", int(fb["nstenergy"])),
            "dt": mdp_mod.to_float(p, "dt", float(fb["dt_ps"])),
            "tcoupl": mdp_mod.get(p, "tcoupl", "v-rescale"),
            "pcoupl": mdp_mod.get(p, "pcoupl", "no"),
        }
        cfg = self.stage_cfgs.get(stage.key, {})
        if "duration_ps" in cfg:
            v = cfg["duration_ps"] / (1000.0 if defaults["unit"] == "ns" else 1.0)
            defaults["duration"] = f"{v:.4g}"
        for k_cfg, k_def in (("temperature_k", "temp"), ("pressure_bar", "press"),
                             ("nstxout_compressed", "nstxout"), ("nstenergy", "nstenergy"),
                             ("dt_ps", "dt"), ("tcoupl", "tcoupl"), ("pcoupl", "pcoupl")):
            if k_cfg in cfg:
                defaults[k_def] = cfg[k_cfg]
        if "restraints" in cfg:
            defaults["restraints"] = cfg["restraints"]
        return defaults

    def _entry(self, parent, var, width=110):
        return ctk.CTkEntry(parent, textvariable=var, width=width,
                            font=self.f_body, height=32, corner_radius=8,
                            border_color=C_BORDER, fg_color=C_CARD,
                            text_color=C_TEXT)

    def _build_stage_form(self, stage: detect.Stage) -> None:
        for w in self.stage_form.winfo_children():
            w.destroy()
        self._form = {}
        pad = ctk.CTkFrame(self.stage_form, fg_color="transparent")
        pad.pack(fill="both", expand=True, padx=18, pady=14)
        self._form_pad = pad
        d = self._stage_defaults(stage)
        kind = stage.kind
        grid = ctk.CTkFrame(pad, fg_color="transparent")
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)
        r = 0

        def _lbl(row, text, **kw):
            ctk.CTkLabel(grid, text=text, font=self.f_body, text_color=C_MUTED,
                         width=170, anchor="w", **kw).grid(row=row, column=0,
                                                           sticky="w", pady=4)

        # 时长
        _lbl(r, "阶段时长")
        f = ctk.CTkFrame(grid, fg_color="transparent")
        f.grid(row=r, column=1, sticky="w", padx=8)
        self._form["duration"] = tk.StringVar(value=str(d["duration"]))
        self._entry(f, self._form["duration"]).pack(side="left")
        self._form["unit"] = tk.StringVar(value=d["unit"])
        Combo(f, textvariable=self._form["unit"], values=["ps", "ns"], width=72,
              font=self.f_body, height=32, corner_radius=8, border_color=C_BORDER,
              fg_color=C_CARD, button_color=C_GHOST, text_color=C_TEXT,
              dropdown_text_color=C_TEXT).pack(side="left", padx=6)
        r += 1

        if kind == config.KIND_EM:
            _lbl(r, "力收敛阈值 emtol")
            self._form["emtol"] = tk.StringVar(value=f"{d['emtol']:.4g}")
            self._entry(grid, self._form["emtol"]).grid(row=r, column=1,
                                                        sticky="w", padx=8)
            r += 1
            ctk.CTkLabel(grid, font=self.f_small, text_color=C_MUTED, anchor="w",
                         justify="left",
                         text="说明: 最小化步数由「阶段时长」换算（nsteps = 时长/步长）；"
                              "位置限制默认不启用。"
                         ).grid(row=r, column=0, columnspan=2, sticky="w", pady=6)
            r += 1

        if kind == config.KIND_EQUIL:
            _lbl(r, "温度 (K)")
            self._form["temp"] = tk.StringVar(value=f"{d['temp']:.2f}")
            self._entry(grid, self._form["temp"]).grid(row=r, column=1, sticky="w",
                                                       padx=8)
            r += 1
            if mdp_mod.get(stage.parsed, "pcoupl", "").lower() not in ("", "no"):
                _lbl(r, "压力 (bar)")
                self._form["press"] = tk.StringVar(value=f"{d['press']:.2f}")
                self._entry(grid, self._form["press"]).grid(row=r, column=1,
                                                            sticky="w", padx=8)
                r += 1
            self._form["restraints"] = tk.BooleanVar(value=bool(d["restraints"]))
            ctk.CTkCheckBox(grid, text="启用位置限制 (define=-DPOSRES，平衡早期推荐)",
                            variable=self._form["restraints"], font=self.f_body,
                            fg_color=C_PRIMARY, hover_color=C_PRIMARY_HOVER,
                            text_color=C_TEXT).grid(row=r, column=0, columnspan=2,
                                                    sticky="w", pady=5)
            r += 1

        if kind == config.KIND_PRODUCTION:
            _lbl(r, "温度 (K)")
            self._form["temp"] = tk.StringVar(value=f"{d['temp']:.2f}")
            self._entry(grid, self._form["temp"]).grid(row=r, column=1, sticky="w",
                                                       padx=8)
            r += 1
            _lbl(r, "压力 (bar)")
            self._form["press"] = tk.StringVar(value=f"{d['press']:.2f}")
            self._entry(grid, self._form["press"]).grid(row=r, column=1, sticky="w",
                                                        padx=8)
            r += 1
            _lbl(r, "轨迹输出频率 (步)")
            self._form["nstxout"] = tk.StringVar(value=str(d["nstxout"]))
            self._entry(grid, self._form["nstxout"]).grid(row=r, column=1,
                                                          sticky="w", padx=8)
            r += 1
            _lbl(r, "能量输出频率 (步)")
            self._form["nstenergy"] = tk.StringVar(value=str(d["nstenergy"]))
            self._entry(grid, self._form["nstenergy"]).grid(row=r, column=1,
                                                            sticky="w", padx=8)
            r += 1
            ctk.CTkLabel(grid, font=self.f_small, text_color=C_MUTED, anchor="w",
                         text="说明: 生产阶段会自动移除位置限制。"
                         ).grid(row=r, column=0, columnspan=2, sticky="w", pady=6)
            r += 1

        # 高级选项
        adv = ctk.CTkFrame(pad, fg_color="transparent")
        adv.pack(fill="x", pady=(14, 0))
        ctk.CTkLabel(adv, text="高级选项", font=self.f_body_b,
                     text_color=C_TEXT).pack(anchor="w", pady=(0, 6))
        g = ctk.CTkFrame(adv, fg_color="transparent")
        g.pack(fill="x")
        ctk.CTkLabel(g, text="步长 dt (ps)", font=self.f_body, text_color=C_MUTED,
                     width=170, anchor="w").grid(row=0, column=0, sticky="w", pady=4)
        self._form["dt"] = tk.StringVar(value=f"{d['dt']:.6f}")
        self._entry(g, self._form["dt"]).grid(row=0, column=1, sticky="w", padx=8)
        ctk.CTkLabel(g, text="恒温器 tcoupl", font=self.f_body, text_color=C_MUTED,
                     width=170, anchor="w").grid(row=1, column=0, sticky="w", pady=4)
        self._form["tcoupl"] = tk.StringVar(value=str(d["tcoupl"]))
        Combo(g, textvariable=self._form["tcoupl"], values=TCOUPL_CHOICES, width=200,
              font=self.f_body, height=32, corner_radius=8, border_color=C_BORDER,
              fg_color=C_CARD, button_color=C_GHOST, text_color=C_TEXT,
              dropdown_text_color=C_TEXT).grid(row=1, column=1, sticky="w", padx=8)
        ctk.CTkLabel(g, text="恒压器 pcoupl", font=self.f_body, text_color=C_MUTED,
                     width=170, anchor="w").grid(row=2, column=0, sticky="w", pady=4)
        self._form["pcoupl"] = tk.StringVar(value=str(d["pcoupl"]))
        Combo(g, textvariable=self._form["pcoupl"], values=PCOUPL_CHOICES, width=200,
              font=self.f_body, height=32, corner_radius=8, border_color=C_BORDER,
              fg_color=C_CARD, button_color=C_GHOST, text_color=C_TEXT,
              dropdown_text_color=C_TEXT).grid(row=2, column=1, sticky="w", padx=8)

    def _restore_defaults(self) -> None:
        self._save_current_form()
        stage = self._stage_by_key(self._cur_stage_key or "")
        if stage:
            self.stage_cfgs.pop(stage.key, None)
            self._build_stage_form(stage)

    def _save_params(self) -> None:
        self._save_current_form()
        f = filedialog.asksaveasfilename(
            title="保存模拟参数", defaultextension=".json",
            filetypes=[("JSON 文件", "*.json")],
            initialfile="md_params.json")
        if not f:
            return
        data = {
            "version": config.APP_VERSION,
            "sim_type": self.var_sim_type.get(),
            "output_dir": self.var_output_dir.get(),
            "ntomp": self.var_ntomp.get(),
            "gpu": bool(self.var_gpu.get()),
            "dry_run": bool(self.var_dry.get()),
            "stage_cfgs": self.stage_cfgs,
        }
        try:
            Path(f).write_text(json.dumps(data, ensure_ascii=False, indent=2),
                               encoding="utf-8")
            self.logger.info(f"参数已保存: {f}")
            messagebox.showinfo("保存成功", f"参数已保存到:\n{f}")
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc))

    def _load_params(self) -> None:
        f = filedialog.askopenfilename(title="加载模拟参数",
                                       filetypes=[("JSON 文件", "*.json")])
        if not f:
            return
        try:
            data = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception as exc:
            messagebox.showerror("加载失败", f"无法读取参数文件:\n{exc}")
            return
        if not isinstance(data.get("stage_cfgs"), dict):
            messagebox.showwarning("提示", "文件中没有有效的 stage_cfgs 参数。")
            return
        self.stage_cfgs = {str(k): dict(v) for k, v in data["stage_cfgs"].items()}
        if data.get("sim_type") in [AUTO_LABEL] + list(config.SIM_TYPE_NAMES.values()):
            self.var_sim_type.set(data["sim_type"])
        if data.get("output_dir"):
            self.var_output_dir.set(str(data["output_dir"]))
        if str(data.get("ntomp", "")).isdigit():
            self.var_ntomp.set(str(data["ntomp"]))
        if "gpu" in data:
            self.var_gpu.set(bool(data["gpu"]))
        if "dry_run" in data:
            self.var_dry.set(bool(data["dry_run"]))
        self._cur_stage_key = None
        self._on_stage_selected()
        self.logger.info(f"参数已加载: {f}（{len(self.stage_cfgs)} 个阶段）")
        messagebox.showinfo("加载成功", f"已加载 {len(self.stage_cfgs)} 个阶段的参数。\n"
                                        "请确认“模拟类型”与当前输入一致。")

    def _show_history(self) -> None:
        hist = envsetup.load_history()
        if not hist:
            messagebox.showinfo("作业历史", "暂无历史记录。运行过模拟后会显示在这里。")
            return
        win = ctk.CTkToplevel(self)
        win.title("作业历史")
        win.geometry("780x440")
        win.transient(self)
        win.after(150, win.grab_set)
        head = ctk.CTkFrame(win, fg_color="transparent")
        head.pack(fill="x", padx=18, pady=(16, 8))
        ctk.CTkLabel(head, text="作业历史", font=self.f_card,
                     text_color=C_TEXT).pack(side="left")
        tree = ttk.Treeview(win, columns=("time", "type", "status", "mode", "dir"),
                            show="headings", style="Md.Treeview")
        for col, txt, w in (("time", "时间", 150), ("type", "类型", 150),
                            ("status", "状态", 60), ("mode", "模式", 70),
                            ("dir", "输出目录", 300)):
            tree.heading(col, text=txt)
            tree.column(col, width=w, anchor="w")
        tree.pack(fill="both", expand=True, padx=18, pady=(0, 8))
        for h in hist:
            status = "成功" if h.get("ok") else ("取消" if h.get("canceled") else "失败")
            tree.insert("", "end", values=(h.get("time", ""), h.get("sim_type", ""),
                                           status, h.get("mode", ""),
                                           h.get("output_dir", "")))
        row = ctk.CTkFrame(win, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=(0, 16))

        def selected_dir():
            sel = tree.selection()
            if not sel:
                messagebox.showinfo("提示", "请先选择一条记录", parent=win)
                return None
            return hist[tree.index(sel[0])].get("output_dir")

        def open_dir():
            d = selected_dir()
            if d and os.path.isdir(str(d)):
                self._open_path(d)
            else:
                messagebox.showinfo("提示", "该作业的输出目录已不存在", parent=win)

        def open_summary():
            d = selected_dir()
            if d and (Path(str(d)) / "run_summary.txt").exists():
                self._open_path(Path(str(d)) / "run_summary.txt")
            else:
                messagebox.showinfo("提示", "该作业的 run_summary.txt 不存在", parent=win)

        self._btn(row, "打开目录", open_dir, width=100).pack(side="left", padx=(0, 6))
        self._btn(row, "查看运行汇总", open_summary, width=120).pack(side="left")

    def _save_current_form(self) -> None:
        if not self._cur_stage_key or not self._form:
            return
        cfg: dict = {}
        dur = str(self._form["duration"].get()).strip()
        if dur:
            try:
                val = float(dur)
            except ValueError:
                val = 0.0
            unit = str(self._form["unit"].get())
            cfg["duration_ps"] = val * 1000.0 if unit == "ns" else val
        for field, key in (("temp", "temperature_k"), ("press", "pressure_bar"),
                           ("emtol", "emtol")):
            w = self._form.get(field)
            if w is None:
                continue
            s = str(w.get()).strip()
            if s:
                try:
                    cfg[key] = float(s)
                except ValueError:
                    pass
        w = self._form.get("restraints")
        if w is not None:
            cfg["restraints"] = bool(w.get())
        for field, key in (("nstxout", "nstxout_compressed"),
                           ("nstenergy", "nstenergy")):
            w = self._form.get(field)
            if w is None:
                continue
            s = str(w.get()).strip()
            if s:
                try:
                    cfg[key] = int(s)
                except ValueError:
                    pass
        w = self._form.get("dt")
        if w is not None:
            s = str(w.get()).strip()
            if s:
                try:
                    cfg["dt_ps"] = float(s)
                except ValueError:
                    pass
        for field, key in (("tcoupl", "tcoupl"), ("pcoupl", "pcoupl")):
            w = self._form.get(field)
            if w is None:
                continue
            v = str(w.get())
            if v and v.lower() != "auto":
                cfg[key] = v
        if cfg:
            self.stage_cfgs[self._cur_stage_key] = cfg
        else:
            self.stage_cfgs.pop(self._cur_stage_key, None)

    def _resolved_sim_type(self) -> Optional[str]:
        choice = self.var_sim_type.get()
        if choice == AUTO_LABEL:
            return self.detect_result.sim_type if self.detect_result else None
        for t, name in config.SIM_TYPE_NAMES.items():
            if choice == name:
                return t
        return None

    # ==================================================================
    # 启动 / 取消（业务逻辑与旧版一致）
    # ==================================================================
    def _start(self, resume: bool) -> None:
        self._save_current_form()
        if not self.detect_result or not self.detect_result.ok:
            messagebox.showwarning("提示", "请先在「输入与检测」页面选择目录并完成检测。")
            return
        sim_type = self._resolved_sim_type()
        if not sim_type:
            messagebox.showwarning("提示", "无法确定模拟类型，请在“模拟类型”中手动选择（全原子/粗粒化）。")
            return
        dry = bool(self.var_dry.get())
        if not dry and self.detect_result.structure_path is None:
            messagebox.showwarning("缺少结构文件",
                                   "未找到结构文件（step5_input.gro/.pdb）。\n"
                                   "请检查 CHARMM-GUI 输出是否完整。")
            return
        if not dry and self.detect_result.topol_path is None:
            messagebox.showwarning(
                "缺少拓扑文件",
                "未找到拓扑文件（topol.top / system.top）。\n\n"
                "这个 CHARMM-GUI 包需要用 gen_gromacstop.sh 生成拓扑：\n"
                "  1. 把压缩包解压，进入 charmm-gui-xxxx 目录\n"
                "  2. 运行: bash gen_gromacstop.sh（需安装 CHARMM 或相关工具）\n"
                "  3. 生成的 system.top 会在 gromacs/ 目录里\n"
                "  4. 回到本软件重新检测\n\n"
                "也可以先用“模拟运行 (dry-run)”体验完整流程。")
            return
        for key, cfg in self.stage_cfgs.items():
            dur = cfg.get("duration_ps")
            if dur is not None and dur <= 0:
                messagebox.showwarning("参数错误",
                                       f"阶段 {key} 的时长为 {dur} ps，必须大于 0。")
                return
        out_root = self.var_output_dir.get().strip() or self._default_output_root()
        try:
            Path(out_root).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("错误", f"无法创建输出目录 {out_root}\n{exc}")
            return
        gmx_exe = self.var_gmx.get().strip() or config.GMX_EXE_DEFAULT
        self._last_sim_type = sim_type
        self._last_dry = dry
        if not dry:
            backend = make_backend(self.var_backend.get(),
                                   self.var_distro.get().strip())
            info = envsetup.probe_gmx_info(backend, gmx_exe)
            ok = bool(info.get("ok"))
            msg = str(info.get("msg", ""))
            if not ok:
                if messagebox.askyesno(
                        "未找到 GROMACS",
                        f"{msg}\n\n是否现在自动安装 GROMACS？\n"
                        "（软件将自动下载 micromamba 并在相应环境安装 conda-forge 版 GROMACS，"
                        "需要几分钟。安装完成后请重新点击“启动”。）"):
                    self._install_gmx()
                return
            self.logger.info(f"gmx 检查通过: {msg}")
            self._apply_gmx_info(info)
            if self.var_gpu.get() and not bool(info.get("gpu")):
                self.var_gpu.set(False)
                messagebox.showwarning(
                    "GPU 不支持",
                    "当前 gmx 为 CPU 版，不支持 GPU 加速，已自动取消 GPU 选项。")
            if (self.detect_result.martini_v2
                    and envsetup.martini_v2_incompatible(info.get("version_tuple"), True)):
                ver = str(info.get("version") or "?")
                if messagebox.askyesno(
                        "GROMACS 版本不兼容",
                        f"当前体系使用 Martini v2 力场，检测到的 GROMACS 是 {ver}，"
                        "与其不兼容（2023.4 及更新版本会报 reaction-field 错误，"
                        "2023.3 已实测可用）。\n\n"
                        "是否自动安装兼容版本 GROMACS 2023.3？\n"
                        "（软件将创建独立 conda 环境，不影响现有 GROMACS，需要几分钟。"
                        "安装完成后 gmx 命令会自动切换，再点“启动”即可。）"):
                    self.var_gmx_version.set("2023.3")
                    self._persist_ui_config()
                    self._install_gmx()
                    return
                self.logger.warn(
                    f"选择继续使用 GROMACS {ver} 运行 Martini v2 体系，可能会报 "
                    "reaction-field 错误")

        try:
            ntomp = int(self.var_ntomp.get() or "0")
        except ValueError:
            ntomp = 0

        backend = make_backend(self.var_backend.get(),
                               self.var_distro.get().strip())
        if resume and self.workflow is not None:
            wf = self.workflow
            wf.dry_run = dry
            wf.gmx_exe = gmx_exe
            wf.ntomp = ntomp
            wf.use_gpu = bool(self.var_gpu.get())
            wf.backend = backend
            wf.stage_configs = dict(self.stage_cfgs)
            start_from = self._failed_index or 0
            self.logger.info(f"从阶段 {start_from + 1} 继续运行（后端: {backend.display}）")
        else:
            wf = workflow.Workflow(
                detect=self.detect_result, sim_type=sim_type,
                output_root=out_root, gmx_exe=gmx_exe, ntomp=ntomp,
                use_gpu=bool(self.var_gpu.get()), dry_run=dry,
                backend=backend,
                stage_configs=dict(self.stage_cfgs),
                logger=self.logger, emit=self.event_q.put)
            self.workflow = wf
            start_from = 0
            self._last_overall = 0.0

        self._set_running(True)
        self._failed_index = None
        self.btn_resume.configure(state="disabled")
        for key, item in self._stage_items.items():
            values = self.tree_steps.item(item, "values")
            self.tree_steps.item(item, values=(values[0], "待运行"),
                                 tags=(self._stage_stripes.get(key, "row0"),))
        self.worker = threading.Thread(target=self._run_worker,
                                       args=(wf, start_from), daemon=True)
        self.worker.start()

    def _run_worker(self, wf: workflow.Workflow, start_from: int) -> None:
        try:
            wf.run(start_from=start_from)
        except Exception as exc:
            import traceback
            detail = f"内部错误: {exc}"
            self.event_q.put({"type": "error", "message": detail + "\n"
                              + traceback.format_exc()})
            self.event_q.put({"type": "worker_crashed", "message": detail})

    def _cancel_run(self) -> None:
        if self.workflow is not None:
            self.workflow.runner.request_cancel()
            self.logger.warn("已请求取消，正在终止当前进程…")

    def _set_running(self, running: bool) -> None:
        self._running = running
        self.btn_start.configure(state="disabled" if running else "normal")
        self.btn_cancel.configure(state="normal" if running else "disabled")
        if running:
            self.lbl_action.configure(text="正在准备运行…", text_color=C_PRIMARY)
        else:
            self.lbl_action.configure(text_color=C_MUTED)

    def _open_output_dir(self) -> None:
        if self.workflow and self.workflow.job_dir:
            d = str(self.workflow.job_dir)
        else:
            d = self.var_output_dir.get().strip() or self._default_output_root()
        if os.path.isdir(d):
            self._open_path(d)
        else:
            messagebox.showinfo("提示", f"目录尚未创建: {d}")

    # ==================================================================
    # 日志 / 事件轮询
    # ==================================================================
    def _on_log(self, level: str, msg: str) -> None:
        if level == "DEBUG" and STEP_NOISE_RE.search(msg):
            return
        self.event_q.put({"type": "log", "level": level, "msg": msg})

    def _append_log(self, level: str, msg: str) -> None:
        try:
            tb = self.log_text._textbox
            tag = level.lower() if level.lower() in TAG_COLORS else "info"
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            tb.configure(state="normal")
            tb.insert("end", f"[{ts}] [{level}] {msg}\n", (tag,))
            if float(tb.index("end-1c").split(".")[0]) > 3000:
                tb.delete("1.0", "500.0")
            tb.configure(state="disabled")
            tb.see("end")
        except tk.TclError:
            pass

    def _poll(self) -> None:
        try:
            while True:
                ev = self.event_q.get_nowait()
                self._handle_event(ev)
        except queue.Empty:
            pass
        except tk.TclError:
            pass
        try:
            self._poll_job = self.after(100, self._poll)
        except tk.TclError:
            pass

    def _handle_event(self, ev: dict) -> None:
        t = ev.get("type")
        if t == "log":
            self._append_log(ev["level"], ev["msg"])
        elif t == "env_detect":
            wsl = ev.get("wsl") or {}
            if wsl.get("available"):
                distros = wsl.get("distros") or []
                txt = "WSL 可用" if distros else "WSL 可用（未安装发行版）"
                if distros:
                    txt += f"，发行版: {', '.join(str(d) for d in distros)}"
                self.lbl_wsl.configure(text=txt, text_color=C_OK)
                if distros and not self.var_distro.get():
                    default = str(wsl.get("default") or distros[0])
                    self.var_distro.set(default)
                    self.cfg["wsl_distro"] = default
            else:
                self.lbl_wsl.configure(text="不可用: " + str(wsl.get("error", "WSL 不可用")),
                                       text_color=C_ERR)
            self.cmb_distro["values"] = [str(d) for d in (wsl.get("distros") or [])]
            self.backend = make_backend(self.var_backend.get(),
                                        self.var_distro.get().strip())
            self._show_gmx_status(ev.get("gmx_ok"), ev.get("gmx_msg", ""))
            self._apply_gmx_info(ev.get("gmx_info"))
        elif t == "gmx_check":
            self._show_gmx_status(ev.get("ok"), ev.get("msg", ""))
            self._apply_gmx_info(ev.get("gmx_info"))
        elif t == "install_done":
            self.env_progress.stop()
            if ev.get("ok"):
                msg = str(ev.get("msg", ""))
                self.lbl_gmx.configure(text="已就绪: " + msg, text_color=C_OK)
                self.logger.info(f"GROMACS 安装完成: {msg}")
                if msg and (msg.startswith("~") or msg.startswith("/") or msg.endswith("gmx")):
                    self.var_gmx.set(msg)
                    self.cfg["gmx_path"] = msg
                    envsetup.save_config(self.cfg)
                    self._check_gmx_now()
                messagebox.showinfo("安装完成", f"GROMACS 安装成功！\n{msg}\n\n"
                                                "现在可以返回“输入与检测”页面开始模拟了。")
            else:
                self.lbl_gmx.configure(text="安装失败", text_color=C_ERR)
                messagebox.showerror("安装失败", str(ev.get("msg", "未知错误")) +
                                     "\n\n详情见“运行与日志”页面。")
        elif t == "self_test_done":
            self.env_progress.stop()
            if ev.get("ok"):
                self.lbl_gmx.configure(text="环境自测通过", text_color=C_OK)
                messagebox.showinfo("环境自测", str(ev.get("msg", "通过")))
            else:
                self.lbl_gmx.configure(text="环境自测失败", text_color=C_ERR)
                messagebox.showerror("环境自测失败",
                                     str(ev.get("msg", "未知错误")) +
                                     "\n\n详细输出见“运行与日志”页面，"
                                     "可尝试点击“自动安装 GROMACS”。")
        elif t == "stage_start":
            idx, key, name = ev["index"], ev["key"], ev["name"]
            self.lbl_action.configure(text=f"正在运行: {name}（阶段 {idx + 1}/{ev['count']}）",
                                      text_color=C_PRIMARY)
            self.pb_stage["value"] = 0
            self.lbl_stage_pct.configure(text="0%")
            self.lbl_eta_val.configure(text="计算中")
            self.lbl_elapsed_val.configure(text="0 秒")
            self._set_stage_status(key, "运行中")
            if self.workflow and self.workflow.job_dir:
                self.lbl_jobdir.configure(text=str(self.workflow.job_dir))
                self.btn_open.configure(state="normal")
        elif t == "progress":
            self.pb_stage["value"] = ev["fraction"] * 100
            self.lbl_stage_pct.configure(text=f"{ev['fraction'] * 100:.0f}%")
            self.pb_overall["value"] = ev["overall"] * 100
            self.lbl_overall.configure(text=f"{ev['overall'] * 100:.0f}%")
            self.lbl_eta_val.configure(text=str(ev["eta"]))
            self.lbl_elapsed_val.configure(text=f"{ev['elapsed']:.0f} 秒")
            self.lbl_status_left.configure(
                text=f"运行中 · {ev['name']} · 总体 {ev['overall'] * 100:.1f}%")
        elif t == "stage_done":
            self._set_stage_status(ev["key"], "完成")
            self.logger.info(f"阶段完成: {ev['name']}")
        elif t == "stage_fail":
            self._set_stage_status(ev["key"], "已取消" if ev.get("canceled") else "失败")
        elif t == "error":
            self._append_log("error", "ERROR: " + ev["message"])
        elif t == "worker_crashed":
            self._set_running(False)
            messagebox.showerror("运行错误",
                                 str(ev.get("message", "未知错误")) +
                                 "\n\n可修复问题后重新启动。")
        elif t == "canceled":
            self.lbl_action.configure(text="运行已取消", text_color=C_WARN)
        elif t == "finished":
            self._set_running(False)
            ok = ev.get("ok", True)
            canceled = bool(ev.get("canceled"))
            envsetup.add_history({
                "sim_type": config.SIM_TYPE_NAMES.get(self._last_sim_type, self._last_sim_type),
                "ok": ok, "canceled": canceled,
                "mode": "dry-run" if self._last_dry else "真实",
                "output_dir": str(ev.get("output_dir", "")),
            })
            if canceled:
                self.lbl_action.configure(text="运行已取消", text_color=C_WARN)
                self.lbl_status_left.configure(text="运行已取消")
            elif ok:
                self.lbl_action.configure(text="全部阶段完成！", text_color=C_OK)
                self.pb_overall["value"] = 100
                self.lbl_overall.configure(text="100%")
                self.btn_open.configure(state="normal")
                self.lbl_jobdir.configure(text=str(ev.get("output_dir", "")))
                self.lbl_status_left.configure(text="运行完成")
                messagebox.showinfo("完成", f"MD 模拟全部完成！\n输出目录:\n{ev.get('output_dir', '')}")
            else:
                self.lbl_action.configure(text="运行失败，可修复参数后“从失败处继续”",
                                          text_color=C_ERR)
                self.lbl_status_left.configure(text="运行失败")
                self.btn_resume.configure(state="normal")
                self._failed_index = self._find_failed_index()
                messagebox.showerror(
                    "运行失败",
                    "某个阶段运行失败，详见日志与 md_sim.log。\n"
                    "可修改参数后点击“从失败处继续”。")

    def _find_failed_index(self) -> Optional[int]:
        if not self.detect_result:
            return None
        for i, s in enumerate(self.detect_result.stages):
            item = self._stage_items.get(s.key)
            if item and "失败" in str(self.tree_steps.item(item, "values")[1]):
                return i
        return None

    def _on_close(self) -> None:
        if self._running:
            if not messagebox.askyesno("确认退出",
                                       "模拟仍在运行，确定要退出吗？（将终止当前进程）"):
                return
            if self.workflow is not None:
                self.workflow.runner.request_cancel()
        if getattr(self, "_poll_job", None):
            try:
                self.after_cancel(self._poll_job)
            except tk.TclError:
                pass
        self.logger.close()
        self.destroy()


def main() -> None:
    app = MDApp()
    app.mainloop()


if __name__ == "__main__":
    main()
