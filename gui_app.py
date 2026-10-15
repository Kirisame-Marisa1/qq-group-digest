import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox
import json
import os
import re
import subprocess
import threading
import sys
import psutil
import sqlite3
import time

ctk.set_appearance_mode("Light")
ctk.set_default_color_theme("blue")

FROZEN = bool(getattr(sys, "frozen", False))
BUNDLE_DIR = getattr(sys, "_MEIPASS", None)

APP_DIR = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")


def _pick(*paths):
    for p in paths:
        if p and os.path.exists(p):
            return p
    return paths[-1]


SCRIPTS_DIR = _pick(
    os.path.join(APP_DIR, "scripts"),
    os.path.join(BUNDLE_DIR, "scripts") if BUNDLE_DIR else None,
)
DEFAULT_WORKSPACE = "E:/QQChatCache"

STAGE_LABELS = {
    "key": "提取密钥",
    "decrypt": "解密数据库",
    "names": "读取群名/昵称",
    "index": "构建索引",
    "groups": "群变更登记",
    "done": "完成",
}

# Anthropic / Claude Design System Constants
CLAUDE_BG = "#FAF9F5"          # Warm parchment ivory canvas
CLAUDE_CARD = "#FFFFFF"        # Clean surface card
CLAUDE_CARD_WARM = "#F5F3EC"   # Warm stone card
CLAUDE_BORDER = "#E8E6DC"      # Subtle hairline border
CLAUDE_BORDER_SUBTLE = "#F0EDE5"
CLAUDE_TEXT = "#141413"        # Deep espresso charcoal
CLAUDE_TEXT_SUB = "#6E665E"    # Warm muted gray
CLAUDE_TEXT_MUTED = "#87867F"
CLAUDE_TERRACOTTA = "#D97757"  # Anthropic signature terracotta coral
CLAUDE_TERRACOTTA_HOVER = "#C96442"
CLAUDE_TERRACOTTA_SOFT = "#F7ECE6"
CLAUDE_BLUE = "#6A9BCC"        # Anthropic soft slate blue
CLAUDE_BLUE_HOVER = "#5888B8"
CLAUDE_GREEN = "#788C5D"       # Anthropic sage / olive green
CLAUDE_GREEN_HOVER = "#687A50"
CLAUDE_MUTED_BTN = "#F4F1EA"
CLAUDE_MUTED_HOVER = "#E8E3DA"


class ClaudeReaderWindow(ctk.CTkToplevel):
    """Claude / Anthropic 风格独立沉浸式阅读视窗。
    遵循 Anthropic 品牌色彩体系、双轨布局与典雅排版。
    """
    def __init__(self, parent):
        super().__init__(parent)
        self.parent = parent
        self.title("✦ Claude 阅读视窗 · QQ 群聊每日简报")
        self.geometry("1100x820")
        self.configure(fg_color=CLAUDE_BG)

        # Center on screen
        self.update_idletasks()
        x = max(20, (self.winfo_screenwidth() - 1100) // 2)
        y = max(20, (self.winfo_screenheight() - 820) // 2)
        self.geometry(f"+{x}+{y}")

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self.report_path = ""
        self.html_path = ""
        self.full_markdown = ""
        self.groups = []
        self.active_group_idx = -1

        # 1. Top Navbar
        nav = ctk.CTkFrame(self, fg_color=CLAUDE_CARD, corner_radius=0, border_color=CLAUDE_BORDER, border_width=1)
        nav.grid(row=0, column=0, columnspan=2, sticky="ew")

        left_nav = ctk.CTkFrame(nav, fg_color="transparent")
        left_nav.pack(side="left", padx=16, pady=10)

        ctk.CTkLabel(
            left_nav,
            text="✦",
            font=ctk.CTkFont(family="Georgia", size=20, weight="bold"),
            text_color=CLAUDE_TERRACOTTA,
        ).pack(side="left", padx=(0, 8))

        ctk.CTkLabel(
            left_nav,
            text="Claude 阅读视窗",
            font=ctk.CTkFont(family="Georgia", size=17, weight="bold"),
            text_color=CLAUDE_TEXT,
        ).pack(side="left", padx=(0, 10))

        self.badge_lbl = ctk.CTkLabel(
            left_nav,
            text="Artifact · 沉浸速读",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=CLAUDE_TERRACOTTA,
            fg_color=CLAUDE_TERRACOTTA_SOFT,
            corner_radius=6,
            padx=8,
            pady=2,
        )
        self.badge_lbl.pack(side="left")

        right_nav = ctk.CTkFrame(nav, fg_color="transparent")
        right_nav.pack(side="right", padx=16, pady=10)

        tool_style = {
            "corner_radius": 7,
            "height": 30,
            "font": ctk.CTkFont(size=12, weight="bold"),
        }

        ctk.CTkButton(
            right_nav,
            text="🌐 浏览器沉浸视窗",
            command=self.open_in_browser,
            fg_color=CLAUDE_TERRACOTTA,
            hover_color=CLAUDE_TERRACOTTA_HOVER,
            text_color="#FFFFFF",
            **tool_style,
        ).pack(side="left", padx=5)

        ctk.CTkButton(
            right_nav,
            text="📋 复制当前文本",
            command=self.copy_current_text,
            fg_color=CLAUDE_MUTED_BTN,
            hover_color=CLAUDE_MUTED_HOVER,
            text_color=CLAUDE_TEXT,
            border_color=CLAUDE_BORDER,
            border_width=1,
            **tool_style,
        ).pack(side="left", padx=5)

        ctk.CTkButton(
            right_nav,
            text="📁 打开所在目录",
            command=self.open_output_dir,
            fg_color=CLAUDE_MUTED_BTN,
            hover_color=CLAUDE_MUTED_HOVER,
            text_color=CLAUDE_TEXT,
            border_color=CLAUDE_BORDER,
            border_width=1,
            **tool_style,
        ).pack(side="left", padx=5)

        # 2. Left Sidebar (Navigation Rail)
        self.sidebar = ctk.CTkFrame(
            self,
            fg_color=CLAUDE_CARD,
            border_color=CLAUDE_BORDER,
            border_width=1,
            corner_radius=12,
            width=280,
        )
        self.sidebar.grid(row=1, column=0, padx=(16, 8), pady=16, sticky="nsew")
        self.sidebar.grid_propagate(False)
        self.sidebar.grid_columnconfigure(0, weight=1)
        self.sidebar.grid_rowconfigure(2, weight=1)

        search_frame = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        search_frame.grid(row=0, column=0, padx=12, pady=(12, 6), sticky="ew")
        self.search_entry = ctk.CTkEntry(
            search_frame,
            placeholder_text="🔍 搜索群聊 / 话题...",
            fg_color="#FAF9F5",
            border_color=CLAUDE_BORDER,
            text_color=CLAUDE_TEXT,
            height=32,
            corner_radius=8,
        )
        self.search_entry.pack(fill="x")
        self.search_entry.bind("<KeyRelease>", lambda e: self.filter_groups())

        ctk.CTkLabel(
            self.sidebar,
            text="群组目录 (GROUPS)",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=CLAUDE_TEXT_SUB,
            anchor="w",
        ).grid(row=1, column=0, padx=16, pady=(6, 4), sticky="w")

        self.group_scroll = ctk.CTkScrollableFrame(
            self.sidebar,
            fg_color="transparent",
            corner_radius=0,
        )
        self.group_scroll.grid(row=2, column=0, padx=6, pady=4, sticky="nsew")

        self.stats_lbl = ctk.CTkLabel(
            self.sidebar,
            text="就绪",
            font=ctk.CTkFont(size=11),
            text_color=CLAUDE_TEXT_MUTED,
            anchor="w",
        )
        self.stats_lbl.grid(row=3, column=0, padx=14, pady=10, sticky="w")

        # 3. Right Reading Canvas
        self.canvas_card = ctk.CTkFrame(
            self,
            fg_color=CLAUDE_CARD,
            border_color=CLAUDE_BORDER,
            border_width=1,
            corner_radius=12,
        )
        self.canvas_card.grid(row=1, column=1, padx=(8, 16), pady=16, sticky="nsew")
        self.canvas_card.grid_columnconfigure(0, weight=1)
        self.canvas_card.grid_rowconfigure(1, weight=1)

        self.canvas_header = ctk.CTkFrame(self.canvas_card, fg_color=CLAUDE_CARD_WARM, corner_radius=0)
        self.canvas_header.grid(row=0, column=0, sticky="ew")

        self.reading_title = ctk.CTkLabel(
            self.canvas_header,
            text="正在载入简报...",
            font=ctk.CTkFont(family="Georgia", size=17, weight="bold"),
            text_color=CLAUDE_TEXT,
            anchor="w",
        )
        self.reading_title.pack(side="left", padx=16, pady=12)

        self.reading_sub = ctk.CTkLabel(
            self.canvas_header,
            text="",
            font=ctk.CTkFont(size=12),
            text_color=CLAUDE_TEXT_SUB,
            anchor="e",
        )
        self.reading_sub.pack(side="right", padx=16, pady=12)

        self.reading_box = ctk.CTkTextbox(
            self.canvas_card,
            wrap="word",
            fg_color="#FDFBF7",
            text_color=CLAUDE_TEXT,
            font=ctk.CTkFont(family="Consolas", size=13),
            border_color=CLAUDE_BORDER,
            border_width=1,
            corner_radius=8,
        )
        self.reading_box.grid(row=1, column=0, padx=16, pady=16, sticky="nsew")
        self.reading_box.configure(state="disabled")

        self.load_latest_report()

    def load_latest_report(self):
        out_dir = self.parent.output_entry.get().replace("\\", "/")
        work_dir = self.parent.work_entry.get().replace("\\", "/")

        cands_md = []
        cands_html = []
        for d in (out_dir, work_dir, os.path.join(out_dir, "output")):
            if os.path.isdir(d):
                for fn in os.listdir(d):
                    p = os.path.join(d, fn)
                    if fn.lower().endswith(".md") and ("简报" in fn or fn.startswith("群友发言报告")):
                        cands_md.append(p)
                    elif fn.lower().endswith(".html") and ("简报" in fn or fn.startswith("群友发言报告")):
                        cands_html.append(p)

        cands_md.sort(key=lambda x: os.path.getmtime(x), reverse=True)
        cands_html.sort(key=lambda x: os.path.getmtime(x), reverse=True)

        if not cands_md:
            self.reading_title.configure(text="尚未检测到简报文件")
            self.reading_box.configure(state="normal")
            self.reading_box.delete("1.0", "end")
            self.reading_box.insert(
                "1.0",
                "✦ 欢迎来到 Claude 阅读视窗\n\n"
                "当前尚未找到生成的群聊简报文件。\n\n"
                "👉 操作建议：\n"
                "1. 请在主界面点击「3️⃣ 生成简报」或「🚀 一键全自动」。\n"
                "2. 简报生成后，系统将在此自动呈现 Anthropic 美学沉浸阅读！\n"
                "3. 您也可以点击顶部的「🌐 浏览器沉浸视窗」在浏览器中体验双轨交互与气泡展开。\n"
            )
            self.reading_box.configure(state="disabled")
            return

        self.report_path = cands_md[0]
        if cands_html:
            self.html_path = cands_html[0]

        try:
            with open(self.report_path, "r", encoding="utf-8", errors="replace") as f:
                self.full_markdown = f.read()
            self.parse_report()
            self.render_group_buttons()
            self.select_group(-1)
        except Exception as e:
            self.reading_title.configure(text="读取简报出错")
            self.reading_box.configure(state="normal")
            self.reading_box.insert("1.0", f"读取出错: {e}")
            self.reading_box.configure(state="disabled")

    def parse_report(self):
        self.groups = []
        lines = self.full_markdown.splitlines()

        preamble = []
        cur_group = None
        cur_lines = []

        grp_re = re.compile(r"^##\s+(\d+)\.\s+(.+?)(?:\s+\((\d+)\))?$")
        for line in lines:
            m = grp_re.match(line)
            if m:
                if cur_group:
                    cur_group["text"] = "\n".join(cur_lines).strip()
                    self.groups.append(cur_group)
                cur_group = {
                    "idx": int(m.group(1)),
                    "name": m.group(2).strip(),
                    "code": m.group(3) or "",
                    "title": line,
                }
                cur_lines = [line]
            else:
                if cur_group:
                    cur_lines.append(line)
                else:
                    preamble.append(line)

        if cur_group:
            cur_group["text"] = "\n".join(cur_lines).strip()
            self.groups.append(cur_group)

        self.preamble_text = "\n".join(preamble).strip()
        self.stats_lbl.configure(text=f"已解析 {len(self.groups)} 个群组 ｜ 共 {len(lines)} 行")

    def render_group_buttons(self, filter_q=""):
        for w in self.group_scroll.winfo_children():
            w.destroy()

        btn_all = ctk.CTkButton(
            self.group_scroll,
            text="🌐 全群宏观总览",
            anchor="w",
            fg_color=CLAUDE_CARD_WARM if self.active_group_idx == -1 else "transparent",
            hover_color=CLAUDE_MUTED_HOVER,
            text_color=CLAUDE_TERRACOTTA if self.active_group_idx == -1 else CLAUDE_TEXT,
            corner_radius=8,
            height=32,
            font=ctk.CTkFont(size=12, weight="bold" if self.active_group_idx == -1 else "normal"),
            command=lambda: self.select_group(-1),
        )
        btn_all.pack(fill="x", pady=2)

        q = filter_q.strip().lower()
        for idx, g in enumerate(self.groups):
            if q and (q not in g["name"].lower() and q not in g["code"] and q not in g["text"].lower()):
                continue
            is_act = (self.active_group_idx == idx)
            btn = ctk.CTkButton(
                self.group_scroll,
                text=f"{g['idx']}. {g['name']}",
                anchor="w",
                fg_color=CLAUDE_CARD_WARM if is_act else "transparent",
                hover_color=CLAUDE_MUTED_HOVER,
                text_color=CLAUDE_TERRACOTTA if is_act else CLAUDE_TEXT,
                corner_radius=8,
                height=32,
                font=ctk.CTkFont(size=12, weight="bold" if is_act else "normal"),
                command=lambda i=idx: self.select_group(i),
            )
            btn.pack(fill="x", pady=2)

    def filter_groups(self):
        val = self.search_entry.get()
        self.render_group_buttons(val)

    def select_group(self, idx):
        self.active_group_idx = idx
        self.render_group_buttons(self.search_entry.get())

        self.reading_box.configure(state="normal")
        self.reading_box.delete("1.0", "end")

        if idx == -1:
            self.reading_title.configure(text="QQ 群聊每日观察 · 全群总览")
            self.reading_sub.configure(text=f"覆盖 {len(self.groups)} 个群组")
            display_text = self.preamble_text if self.preamble_text else self.full_markdown
            self.reading_box.insert("1.0", display_text)
        else:
            g = self.groups[idx]
            self.reading_title.configure(text=f"✦ {g['name']}")
            self.reading_sub.configure(text=f"群号: {g['code'] or '-'}")
            self.reading_box.insert("1.0", g["text"])

        self.reading_box.configure(state="disabled")

    def copy_current_text(self):
        if self.active_group_idx == -1:
            t = self.full_markdown
        else:
            t = self.groups[self.active_group_idx]["text"]
        self.clipboard_clear()
        self.clipboard_append(t)
        messagebox.showinfo("提示", "已复制到剪贴板！")

    def open_in_browser(self):
        if self.html_path and os.path.exists(self.html_path):
            try:
                os.startfile(os.path.normpath(self.html_path))
            except Exception as e:
                messagebox.showwarning("打开失败", f"无法打开浏览器: {e}")
        else:
            self.parent.open_latest_html()

    def open_output_dir(self):
        out_dir = self.parent.output_entry.get()
        if os.path.isdir(out_dir):
            try:
                os.startfile(os.path.normpath(out_dir))
            except Exception as e:
                messagebox.showwarning("打开失败", f"无法打开目录: {e}")


class QQToolGUI(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("✦ QQ 群聊简报 · 智能分析台 (Claude Edition)")
        self.geometry("980x920")
        self.configure(fg_color=CLAUDE_BG)

        self.update_idletasks()
        x = (self.winfo_screenwidth() - 980) // 2
        y = (self.winfo_screenheight() - 920) // 2
        self.geometry(f"+{x}+{y}")

        self.current_process = None
        self.busy = False
        self.last_html_path = None
        self.claude_win = None

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)

        # Header Frame
        header_frame = ctk.CTkFrame(self, fg_color="transparent")
        header_frame.grid(row=0, column=0, padx=20, pady=(20, 10), sticky="ew")

        left_header = ctk.CTkFrame(header_frame, fg_color="transparent")
        left_header.pack(side="left")

        ctk.CTkLabel(
            left_header,
            text="✦",
            font=ctk.CTkFont(family="Georgia", size=22, weight="bold"),
            text_color=CLAUDE_TERRACOTTA,
        ).pack(side="left", padx=(0, 10))

        title_box = ctk.CTkFrame(left_header, fg_color="transparent")
        title_box.pack(side="left")

        ctk.CTkLabel(
            title_box,
            text="QQ 群聊简报 · 智能分析台",
            font=ctk.CTkFont(family="Georgia", size=20, weight="bold"),
            text_color=CLAUDE_TEXT,
            anchor="w",
        ).pack(anchor="w")

        ctk.CTkLabel(
            title_box,
            text="Claude 极简美学 · 本地离线按需解密",
            font=ctk.CTkFont(size=12),
            text_color=CLAUDE_TEXT_SUB,
            anchor="w",
        ).pack(anchor="w")

        ctk.CTkButton(
            header_frame,
            text="🔄 自动检测运行中的 QQ",
            command=self.auto_detect_qq,
            fg_color=CLAUDE_CARD,
            hover_color=CLAUDE_MUTED_HOVER,
            text_color=CLAUDE_TEXT,
            border_color=CLAUDE_BORDER,
            border_width=1,
            corner_radius=8,
            height=34,
            font=ctk.CTkFont(size=12, weight="bold"),
        ).pack(side="right")

        # Config Frame
        config_frame = ctk.CTkFrame(
            self,
            corner_radius=12,
            fg_color=CLAUDE_CARD,
            border_color=CLAUDE_BORDER,
            border_width=1,
        )
        config_frame.grid(row=1, column=0, padx=20, pady=10, sticky="ew")
        config_frame.grid_columnconfigure((1, 3), weight=1)

        field_font = ctk.CTkFont(size=13, weight="bold")
        entry_kwargs = {
            "fg_color": "#FAF9F5",
            "border_color": CLAUDE_BORDER,
            "text_color": CLAUDE_TEXT,
            "corner_radius": 8,
            "height": 32,
        }

        ctk.CTkLabel(config_frame, text="QQ号:", font=field_font, text_color=CLAUDE_TEXT).grid(
            row=0, column=0, padx=15, pady=12, sticky="e"
        )
        self.acc_entry = ctk.CTkEntry(config_frame, placeholder_text="请输入QQ号", **entry_kwargs)
        self.acc_entry.grid(row=0, column=1, padx=15, pady=12, sticky="ew")

        ctk.CTkLabel(config_frame, text="索引范围:", font=field_font, text_color=CLAUDE_TEXT).grid(
            row=0, column=2, padx=15, pady=12, sticky="e"
        )
        self.days_combo = ctk.CTkOptionMenu(
            config_frame,
            values=["最近 7 天", "最近 30 天", "最近 90 天", "最近 180 天", "最近 365 天", "全部历史"],
            fg_color="#FAF9F5",
            button_color=CLAUDE_MUTED_BTN,
            button_hover_color=CLAUDE_MUTED_HOVER,
            text_color=CLAUDE_TEXT,
            corner_radius=8,
            height=32,
        )
        self.days_combo.set("最近 7 天")
        self.days_combo.grid(row=0, column=3, padx=15, pady=12, sticky="w")

        ctk.CTkLabel(config_frame, text="数据根目录:", font=field_font, text_color=CLAUDE_TEXT).grid(
            row=1, column=0, padx=15, pady=(0, 10), sticky="e"
        )
        self.data_root_entry = ctk.CTkEntry(config_frame, **entry_kwargs)
        self.data_root_entry.grid(row=1, column=1, columnspan=2, padx=15, pady=(0, 10), sticky="ew")
        ctk.CTkButton(
            config_frame,
            text="浏览...",
            width=60,
            command=lambda: self.browse_dir(self.data_root_entry),
            fg_color=CLAUDE_MUTED_BTN,
            hover_color=CLAUDE_MUTED_HOVER,
            text_color=CLAUDE_TEXT,
            border_color=CLAUDE_BORDER,
            border_width=1,
            corner_radius=8,
            height=32,
        ).grid(row=1, column=3, padx=15, pady=(0, 10), sticky="w")

        ctk.CTkLabel(config_frame, text="工作目录:", font=field_font, text_color=CLAUDE_TEXT).grid(
            row=2, column=0, padx=15, pady=(0, 10), sticky="e"
        )
        self.work_entry = ctk.CTkEntry(config_frame, **entry_kwargs)
        self.work_entry.grid(row=2, column=1, columnspan=2, padx=15, pady=(0, 10), sticky="ew")
        ctk.CTkButton(
            config_frame,
            text="浏览...",
            width=60,
            command=lambda: self.browse_dir(self.work_entry),
            fg_color=CLAUDE_MUTED_BTN,
            hover_color=CLAUDE_MUTED_HOVER,
            text_color=CLAUDE_TEXT,
            border_color=CLAUDE_BORDER,
            border_width=1,
            corner_radius=8,
            height=32,
        ).grid(row=2, column=3, padx=15, pady=(0, 10), sticky="w")

        ctk.CTkLabel(config_frame, text="输出目录:", font=field_font, text_color=CLAUDE_TEXT).grid(
            row=3, column=0, padx=15, pady=(0, 10), sticky="e"
        )
        self.output_entry = ctk.CTkEntry(config_frame, **entry_kwargs)
        self.output_entry.grid(row=3, column=1, columnspan=2, padx=15, pady=(0, 10), sticky="ew")
        ctk.CTkButton(
            config_frame,
            text="浏览...",
            width=60,
            command=lambda: self.browse_dir(self.output_entry),
            fg_color=CLAUDE_MUTED_BTN,
            hover_color=CLAUDE_MUTED_HOVER,
            text_color=CLAUDE_TEXT,
            border_color=CLAUDE_BORDER,
            border_width=1,
            corner_radius=8,
            height=32,
        ).grid(row=3, column=3, padx=15, pady=(0, 10), sticky="w")

        ctk.CTkLabel(config_frame, text="QQ.exe路径:", font=field_font, text_color=CLAUDE_TEXT).grid(
            row=4, column=0, padx=15, pady=(0, 10), sticky="e"
        )
        self.exe_entry = ctk.CTkEntry(config_frame, **entry_kwargs)
        self.exe_entry.grid(row=4, column=1, columnspan=3, padx=15, pady=(0, 10), sticky="ew")

        ctk.CTkLabel(config_frame, text="群友筛选(可选):", font=field_font, text_color=CLAUDE_TEXT).grid(
            row=5, column=0, padx=15, pady=(0, 10), sticky="e"
        )
        self.member_entry = ctk.CTkEntry(
            config_frame,
            placeholder_text="留空生成全群简报；填昵称/群名片/QQ号可生成该群友专属报告",
            **entry_kwargs,
        )
        self.member_entry.grid(row=5, column=1, columnspan=3, padx=15, pady=(0, 10), sticky="ew")

        self.keep_plain_var = ctk.BooleanVar(value=False)
        self.keep_plain_box = ctk.CTkCheckBox(
            config_frame,
            variable=self.keep_plain_var,
            text="保留明文缓存 nt_msg.db（约 13GB，加速下次刷新；不勾选则构建完成后自动清理）",
            fg_color=CLAUDE_TERRACOTTA,
            hover_color=CLAUDE_TERRACOTTA_HOVER,
            text_color=CLAUDE_TEXT,
        )
        self.keep_plain_box.grid(row=6, column=0, columnspan=4, padx=15, pady=(0, 10), sticky="w")

        info_label = ctk.CTkLabel(
            config_frame,
            text="💡 全程无需关闭 QQ：密钥从内存只读提取，数据库按需解密（仅 nt_msg.db / group_info.db / profile_info.db），QQ 活跃写入导致的坏页会自动换快照重试。",
            text_color=CLAUDE_GREEN,
            font=ctk.CTkFont(size=12),
        )
        info_label.grid(row=7, column=0, columnspan=4, padx=15, pady=(0, 12), sticky="w")

        # Buttons
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.grid(row=2, column=0, padx=20, pady=10, sticky="ew")
        btn_style = {
            "corner_radius": 8,
            "height": 36,
            "font": ctk.CTkFont(size=13, weight="bold"),
        }

        row_a = ctk.CTkFrame(btn_frame, fg_color="transparent")
        row_a.pack(fill="x")
        ctk.CTkButton(
            row_a,
            text="💾 保存配置",
            command=self.save_config,
            fg_color=CLAUDE_MUTED_BTN,
            hover_color=CLAUDE_MUTED_HOVER,
            text_color=CLAUDE_TEXT,
            border_color=CLAUDE_BORDER,
            border_width=1,
            **btn_style,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            row_a,
            text="1️⃣ 提取密钥",
            command=self.run_key_extract,
            fg_color=CLAUDE_CARD,
            hover_color=CLAUDE_MUTED_HOVER,
            text_color=CLAUDE_TEXT,
            border_color=CLAUDE_BORDER,
            border_width=1,
            **btn_style,
        ).pack(side="left", padx=6)
        ctk.CTkButton(
            row_a,
            text="2️⃣ 构建/刷新索引",
            command=self.run_build_index,
            fg_color=CLAUDE_CARD,
            hover_color=CLAUDE_MUTED_HOVER,
            text_color=CLAUDE_TEXT,
            border_color=CLAUDE_BORDER,
            border_width=1,
            **btn_style,
        ).pack(side="left", padx=6)
        ctk.CTkButton(
            row_a,
            text="3️⃣ 生成简报",
            command=self.run_report,
            fg_color=CLAUDE_CARD,
            hover_color=CLAUDE_MUTED_HOVER,
            text_color=CLAUDE_TEXT,
            border_color=CLAUDE_BORDER,
            border_width=1,
            **btn_style,
        ).pack(side="left", padx=6)
        ctk.CTkButton(
            row_a,
            text="📖 Claude 风格阅读视窗",
            command=self.open_claude_reader,
            fg_color=CLAUDE_TERRACOTTA,
            hover_color=CLAUDE_TERRACOTTA_HOVER,
            text_color="#FFFFFF",
            **btn_style,
        ).pack(side="left", padx=8)
        ctk.CTkButton(
            row_a,
            text="🌐 浏览器看板 (HTML)",
            command=self.open_latest_html,
            fg_color=CLAUDE_BLUE,
            hover_color=CLAUDE_BLUE_HOVER,
            text_color="#FFFFFF",
            **btn_style,
        ).pack(side="left", padx=6)

        row_b = ctk.CTkFrame(btn_frame, fg_color="transparent")
        row_b.pack(fill="x", pady=(10, 0))
        ctk.CTkButton(
            row_b,
            text="🚀 一键全自动（密钥→索引→简报）",
            command=self.run_auto_all,
            fg_color=CLAUDE_GREEN,
            hover_color=CLAUDE_GREEN_HOVER,
            text_color="#FFFFFF",
            **btn_style,
        ).pack(side="left")
        ctk.CTkButton(
            row_b,
            text="📁 打开输出目录",
            command=self.open_output_dir_main,
            fg_color=CLAUDE_MUTED_BTN,
            hover_color=CLAUDE_MUTED_HOVER,
            text_color=CLAUDE_TEXT,
            border_color=CLAUDE_BORDER,
            border_width=1,
            **btn_style,
        ).pack(side="left", padx=10)
        ctk.CTkButton(
            row_b,
            text="⏹️ 终止并总结（不影响 QQ）",
            command=self.stop_and_summarize,
            fg_color=CLAUDE_BG,
            hover_color="#FDE8E8",
            text_color="#C42B1C",
            border_color="#F8B4B4",
            border_width=1,
            **btn_style,
        ).pack(side="right")

        # Status Bar
        status_frame = ctk.CTkFrame(self, fg_color="transparent")
        status_frame.grid(row=3, column=0, padx=20, pady=(10, 0), sticky="ew")
        status_frame.grid_columnconfigure(1, weight=1)

        self.state_label = ctk.CTkLabel(
            status_frame, text="状态: 就绪", text_color=CLAUDE_TEXT_SUB, width=150, anchor="w"
        )
        self.state_label.grid(row=0, column=0, padx=(0, 10), sticky="w")
        self.progress_bar = ctk.CTkProgressBar(status_frame, mode="determinate", progress_color=CLAUDE_TERRACOTTA)
        self.progress_bar.grid(row=0, column=1, sticky="ew")
        self.progress_bar.set(0)
        self.pct_label = ctk.CTkLabel(status_frame, text="", text_color=CLAUDE_TEXT_SUB, width=60, anchor="e")
        self.pct_label.grid(row=0, column=2, padx=(10, 0), sticky="e")

        # Log Frame
        log_frame = ctk.CTkFrame(
            self,
            corner_radius=12,
            fg_color=CLAUDE_CARD,
            border_color=CLAUDE_BORDER,
            border_width=1,
        )
        log_frame.grid(row=4, column=0, padx=20, pady=(10, 20), sticky="nsew")

        self.log_text = ctk.CTkTextbox(
            log_frame,
            wrap="word",
            font=ctk.CTkFont(family="Consolas", size=12),
            fg_color="#FDFBF7",
            text_color=CLAUDE_TEXT,
            border_color=CLAUDE_BORDER,
            border_width=1,
            corner_radius=8,
        )
        self.log_text.pack(fill="both", expand=True, padx=10, pady=10)
        self.log_text.configure(state="disabled")

        self.load_config()

    def open_claude_reader(self):
        if self.claude_win and self.claude_win.winfo_exists():
            self.claude_win.lift()
            self.claude_win.focus_force()
            self.claude_win.load_latest_report()
        else:
            self.claude_win = ClaudeReaderWindow(self)

    def open_output_dir_main(self):
        out_dir = self.output_entry.get()
        if os.path.isdir(out_dir):
            try:
                os.startfile(os.path.normpath(out_dir))
            except Exception as e:
                messagebox.showwarning("打开失败", f"无法打开目录: {e}")

    def log(self, message):
        self.after(0, self._append_log, message)

    def _append_log(self, message):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", str(message) + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _apply_begin(self, title):
        self.progress_bar.stop()
        self.progress_bar.configure(mode="indeterminate")
        self.progress_bar.set(0)
        self.progress_bar.start()
        self.pct_label.configure(text="")
        self.state_label.configure(text="状态: %s" % title, text_color="#0078D4")

    def _apply_progress(self, d):
        stage = str(d.get("stage") or "")
        pct = d.get("pct")
        fname = str(d.get("file") or "")
        detail = str(d.get("detail") or "")
        label = STAGE_LABELS.get(stage, stage or "运行中")
        if stage == "decrypt" and fname:
            label = "解密 %s" % fname
        elif stage == "index":
            done, total = d.get("done"), d.get("total")
            if done is not None and total:
                label = f"构建索引 {done}/{total} 群"
        if pct is not None:
            try:
                p = float(pct)
                self.progress_bar.stop()
                self.progress_bar.configure(mode="determinate")
                self.progress_bar.set(min(1.0, max(0.0, p / 100.0)))
                self.pct_label.configure(text="%.0f%%" % p)
            except Exception:
                pass
        text = ("状态: " + label + (" " + detail if detail else "")).strip()
        self.state_label.configure(text=text, text_color="#0078D4")

    def _apply_end(self, ok):
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate")
        self.progress_bar.set(1.0 if ok else 0.0)
        self.pct_label.configure(text="100%" if ok else "")
        self.state_label.configure(
            text="状态: 完成" if ok else "状态: 失败/已终止",
            text_color="#107C10" if ok else "#C42B1C",
        )

    def _on_progress(self, raw):
        try:
            d = json.loads(raw)
        except Exception:
            return
        self.after(0, self._apply_progress, d)

    def browse_dir(self, entry_widget):
        path = filedialog.askdirectory()
        if path:
            entry_widget.delete(0, "end")
            entry_widget.insert(0, path.replace("\\", "/"))

    def qq_running(self):
        for proc in psutil.process_iter(["name"]):
            if proc.info["name"] and proc.info["name"].lower() == "qq.exe":
                return True
        return False

    @staticmethod
    def _discover_qq_base_dirs():
        """Discover candidate Tencent Files directories via UserDataInfo.ini + standard paths."""
        candidates = []
        ini = r"C:\Users\Public\Documents\Tencent\QQ\UserDataInfo.ini"
        if os.path.isfile(ini):
            try:
                with open(ini, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read()
                m = re.search(r"^UserDataSavePath\s*=\s*(.+)$", text, re.M)
                if m:
                    p = m.group(1).strip()
                    if p and os.path.isdir(p):
                        candidates.append(p)
            except Exception:
                pass
        doc_tf = os.path.expanduser("~\\Documents\\Tencent Files")
        if os.path.isdir(doc_tf) and doc_tf not in candidates:
            candidates.append(doc_tf)
        for drv in "CDEFG":
            for sub in ("Tencent Files", "QQNT\\Tencent Files", "ntqq\\Tencent Files", "QQ\\Tencent Files"):
                p = f"{drv}:\\{sub}"
                if os.path.isdir(p) and p not in candidates:
                    candidates.append(p)
        return candidates

    def auto_detect_qq(self):
        self.log("🔍 正在检测运行中的 QQ...")
        qq_exe = None
        for proc in psutil.process_iter(["name", "exe"]):
            if (
                proc.info["name"]
                and proc.info["name"].lower() == "qq.exe"
                and proc.info["exe"]
            ):
                qq_exe = proc.info["exe"]
                break
        if not qq_exe:
            messagebox.showwarning("提示", "未检测到正在运行的 QQ，请先登录 QQ！")
            self.log("❌ 未检测到 QQ 进程。")
            return
        self.exe_entry.delete(0, "end")
        self.exe_entry.insert(0, qq_exe.replace("\\", "/"))
        self.log(f"✅ 找到 QQ.exe: {qq_exe}")

        base_dirs = self._discover_qq_base_dirs()
        matched_Candidates = []  # (mtime, qq, data_dir)
        for base_dir in base_dirs:
            try:
                for f in os.listdir(base_dir):
                    full = os.path.join(base_dir, f)
                    if os.path.isdir(full) and re.match(r"^\d+$", f):
                        msg_db = os.path.join(full, "nt_qq", "nt_db", "nt_msg.db")
                        wal_db = msg_db + "-wal"
                        if os.path.isfile(wal_db):
                            mtime = os.path.getmtime(wal_db) + 1e9
                        elif os.path.isfile(msg_db):
                            mtime = os.path.getmtime(msg_db) + 1e6
                        else:
                            mtime = os.path.getmtime(full)
                        matched_Candidates.append((mtime, f, full))
            except Exception:
                pass

        if matched_Candidates:
            matched_Candidates.sort(key=lambda x: x[0], reverse=True)
            _, target_qq, data_dir = matched_Candidates[0]
            self.data_root_entry.delete(0, "end")
            self.data_root_entry.insert(0, data_dir.replace("\\", "/"))
            self.acc_entry.delete(0, "end")
            self.acc_entry.insert(0, target_qq)
            self.log(f"✅ 自动匹配到数据目录: {data_dir} (QQ号: {target_qq})")
            if not self.work_entry.get().strip():
                self.work_entry.insert(0, DEFAULT_WORKSPACE)
            if not self.output_entry.get().strip():
                self.output_entry.insert(0, DEFAULT_WORKSPACE + "/output")
        elif base_dirs:
            self.log("⚠️ 未找到数字QQ号文件夹。")
        else:
            self.log("⚠️ 未找到 Tencent Files 数据目录（请手动浏览选择）。")
        self.log("✅ QQ 正在运行：全程（提取密钥/解密/索引）都无需关闭 QQ。")

    def get_selected_days(self):
        val = self.days_combo.get()
        for d in (7, 30, 90, 180, 365):
            if str(d) in val:
                return d
        return 36500

    @staticmethod
    def day_label(days):
        return {
            7: "7天",
            30: "30天",
            90: "90天",
            180: "180天",
            365: "365天",
            36500: "全部",
        }.get(days, "%d天" % days)

    def load_config(self):
        if os.path.exists(CONFIG_PATH):
            try:
                try:
                    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                        cfg = json.load(f)
                except UnicodeDecodeError:
                    with open(CONFIG_PATH, "r", encoding="gbk") as f:
                        cfg = json.load(f)
                self.acc_entry.insert(0, cfg.get("account", ""))
                self.exe_entry.insert(0, cfg.get("qq_exe", ""))
                self.output_entry.insert(
                    0, cfg.get("output_dir") or DEFAULT_WORKSPACE + "/output"
                )

                workspace = cfg.get("root", "") or ""
                account_root = cfg.get("account_root", "") or ""
                if not account_root and workspace:
                    base = os.path.basename(workspace.replace("\\", "/").rstrip("/"))
                    if re.match(r"^\d+$", base):
                        account_root, workspace = workspace, ""
                self.work_entry.insert(0, workspace or DEFAULT_WORKSPACE)
                self.data_root_entry.insert(0, account_root)

                days = cfg.get("index_days", 7)
                if days <= 7:
                    self.days_combo.set("最近 7 天")
                elif days <= 30:
                    self.days_combo.set("最近 30 天")
                elif days <= 90:
                    self.days_combo.set("最近 90 天")
                elif days <= 180:
                    self.days_combo.set("最近 180 天")
                elif days <= 365:
                    self.days_combo.set("最近 365 天")
                else:
                    self.days_combo.set("全部历史")
                self.keep_plain_var.set(bool(cfg.get("keep_plain", False)))
                self.log("✅ 成功加载 config.json")
            except Exception as e:
                self.log(f"⚠️ 加载配置失败: {e}")
        else:
            self.work_entry.insert(0, DEFAULT_WORKSPACE)
            self.output_entry.insert(0, DEFAULT_WORKSPACE + "/output")

    def save_config(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                old = json.load(f)
        except Exception:
            old = {}

        cfg = {
            "root": self.work_entry.get().replace("\\", "/"),
            "account_root": self.data_root_entry.get().replace("\\", "/"),
            "output_dir": self.output_entry.get().replace("\\", "/"),
            "account": self.acc_entry.get().strip(),
            "qq_exe": self.exe_entry.get().replace("\\", "/"),
            "index_days": self.get_selected_days(),
            "decrypt": old.get("decrypt")
            or ["nt_msg.db", "group_info.db", "profile_info.db"],
            "keep_plain": bool(self.keep_plain_var.get()),
            "class_groups": old.get("class_groups", []),
            "class_group_names": old.get("class_group_names", {}),
            "glossary_autoupdate": old.get("glossary_autoupdate", True),
            "_note": "由GUI自动生成 - 按需解密版",
        }
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
            self.log("✅ 配置已保存")
            return True
        except Exception as e:
            self.log(f"❌ 保存失败: {e}")
            return False

    def get_python_exe(self):
        if not FROZEN:
            return sys.executable
        exe_dir = os.path.dirname(sys.executable)
        for cand in (
            os.path.join(exe_dir, "python", "Scripts", "python.exe"),
            os.path.join(exe_dir, "python", "python.exe"),
            os.path.join(exe_dir, "runtime", "python.exe"),
            os.path.join(exe_dir, "python.exe"),
        ):
            if os.path.exists(cand):
                return cand
        return None

    def _bg(self, fn, *args):
        threading.Thread(target=fn, args=args, daemon=True).start()

    def run_command(self, cmd, label=None):
        if self.busy:
            self.log("⚠️ 已有任务在运行，请等待完成或先点击「终止并总结」。")
            return -2
        py = self.get_python_exe()
        if not py:
            self.log("❌ 找不到随包分发的 Python 运行时。")
            self.log(
                "👉 请把完整的 venv 目录复制到 exe 同级并重命名为 python\\（必须保留 python\\pyvenv.cfg 和 python\\Lib\\site-packages）。"
            )
            return -3
        self.busy = True
        title = label or "执行任务"
        self.log(
            "\n>>> "
            + (
                title
                if label
                else "执行命令: " + " ".join(os.path.basename(c) for c in cmd)
            )
        )
        self.after(0, self._apply_begin, title)
        code = 1
        try:
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["NTQQ_CONFIG"] = CONFIG_PATH
            self.current_process = subprocess.Popen(
                [py] + cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                bufsize=1,
                cwd=SCRIPTS_DIR,
            )
            for line in self.current_process.stdout:
                line = line.rstrip("\r\n")
                if line.startswith("@@PROGRESS|"):
                    self._on_progress(line[len("@@PROGRESS|") :])
                    continue
                if line.strip():
                    self.log(line)
            self.current_process.stdout.close()
            code = self.current_process.wait()
        except Exception as e:
            self.log(f"❌ 执行出错: {e}")
            code = 1
        finally:
            self.current_process = None
            self.busy = False
            self.after(0, self._apply_end, code == 0)
        if code == 0:
            self.log(f"✅ {title} 完成")
        else:
            self.log(f"❌ {title} 失败（退出码 {code}）")
        return code

    def run_key_extract(self):
        acc = self.acc_entry.get().strip()
        if not acc:
            messagebox.showwarning("提示", "请先填写 QQ 号！")
            return
        if not self.qq_running():
            messagebox.showwarning(
                "提示",
                "未检测到正在运行的 QQ。提取密钥需要 QQ 登录在线（只读扫描内存，不修改 QQ）。",
            )
            return
        keys_dir = os.path.join(self.work_entry.get().replace("\\", "/"), "keys")
        os.makedirs(keys_dir, exist_ok=True)
        out = os.path.join(keys_dir, "key_map.json")
        cmd = [os.path.join(SCRIPTS_DIR, "ntqq_key.py"), "--qq", acc, "-o", out]
        self._bg(self.run_command, cmd, "提取密钥")

    def run_build_index(self):
        if not self.acc_entry.get().strip():
            messagebox.showwarning("提示", "请先填写 QQ 号并保存配置！")
            return
        if not self.save_config():
            return
        days = self.get_selected_days()
        cmd = [os.path.join(SCRIPTS_DIR, "ntqq_build.py"), "--days", str(days)]
        self._bg(
            self.run_command, cmd, "构建索引（最近 %s）" % self.day_label(days)
        )

    def open_latest_html(self, path=None, silent=False):
        target = path or self.last_html_path
        out_dir = self.output_entry.get().replace("\\", "/")
        if (not target or not os.path.exists(target)) and os.path.isdir(out_dir):
            htmls = [
                os.path.join(out_dir, fn)
                for fn in os.listdir(out_dir)
                if fn.lower().endswith(".html")
            ]
            if htmls:
                htmls.sort(key=lambda p: os.path.getmtime(p), reverse=True)
                target = htmls[0]
        if not target or not os.path.exists(target):
            if not silent:
                messagebox.showinfo(
                    "提示",
                    "尚未找到可视化简报 (.html)，请先点击「3️⃣ 生成简报」或「🚀 一键全自动」。",
                )
            return False
        self.last_html_path = target
        try:
            if hasattr(os, "startfile"):
                os.startfile(os.path.normpath(target))
            self.log(f"🌐 已在浏览器中打开可视化简报: {target}")
            return True
        except Exception as e:
            self.log(f"⚠️ 打开 HTML 简报失败: {e}")
            return False

    def run_report(self):
        if not self.acc_entry.get().strip():
            messagebox.showwarning("提示", "请先填写 QQ 号并保存配置！")
            return
        if not self.save_config():
            return
        days = self.get_selected_days()
        label_days = self.day_label(days)
        out_dir = self.output_entry.get().replace("\\", "/")
        os.makedirs(out_dir, exist_ok=True)
        member = self.member_entry.get().strip() if hasattr(self, "member_entry") else ""
        if member:
            safe_m = re.sub(r'[\\/:*?"<>|]+', "_", member)
            out_file = os.path.join(out_dir, f"群友发言报告_{safe_m}_{label_days}.md")
            title = f"生成群友报告（{member} / {label_days}）"
            cmd = [
                os.path.join(SCRIPTS_DIR, "ntqq_report.py"),
                "--member",
                member,
                "--hours",
                str(days * 24),
                "--out",
                out_file,
            ]
        else:
            out_file = os.path.join(out_dir, "我的群聊简报_%s.md" % label_days)
            title = "生成简报（%s）" % label_days
            cmd = [
                os.path.join(SCRIPTS_DIR, "ntqq_report.py"),
                "--active",
                "--hours",
                str(days * 24),
                "--brief",
                "--out",
                out_file,
            ]
        html_file = os.path.splitext(out_file)[0] + ".html"

        def _task():
            code = self.run_command(cmd, title)
            if code == 0 and os.path.exists(html_file):
                self.last_html_path = html_file
                self.open_latest_html(html_file, silent=True)
                if hasattr(self, "claude_win") and self.claude_win and self.claude_win.winfo_exists():
                    self.after(0, self.claude_win.load_latest_report)

        self._bg(_task)

    def run_auto_all(self):
        if not self.save_config():
            return
        acc = self.acc_entry.get().strip()
        if not acc:
            messagebox.showwarning("提示", "请先填写 QQ 号！")
            return
        days = self.get_selected_days()
        label_days = self.day_label(days)

        def sequence():
            self.log(
                "\n========== 一键全自动开始（QQ 全程保持运行，无需关闭） =========="
            )
            keys_dir = os.path.join(self.work_entry.get().replace("\\", "/"), "keys")
            os.makedirs(keys_dir, exist_ok=True)
            c1 = self.run_command(
                [
                    os.path.join(SCRIPTS_DIR, "ntqq_key.py"),
                    "--qq",
                    acc,
                    "-o",
                    os.path.join(keys_dir, "key_map.json"),
                ],
                "步骤 1/3 提取密钥",
            )
            if c1 != 0:
                self.log("❌ 密钥提取失败：请确认 QQ 已登录且正在运行后重试。")
                return
            c2 = self.run_command(
                [os.path.join(SCRIPTS_DIR, "ntqq_build.py"), "--days", str(days)],
                "步骤 2/3 构建索引（最近 %s）" % label_days,
            )
            if c2 != 0:
                self.log("❌ 索引构建失败，流程终止。")
                return
            out_dir = self.output_entry.get().replace("\\", "/")
            os.makedirs(out_dir, exist_ok=True)
            out_file = os.path.join(out_dir, "我的群聊简报_%s.md" % label_days)
            html_file = os.path.splitext(out_file)[0] + ".html"
            c3 = self.run_command(
                [
                    os.path.join(SCRIPTS_DIR, "ntqq_report.py"),
                    "--active",
                    "--hours",
                    str(days * 24),
                    "--brief",
                    "--out",
                    out_file,
                ],
                "步骤 3/3 生成简报（%s）" % label_days,
            )
            if c3 == 0 and os.path.exists(html_file):
                self.last_html_path = html_file
                self.open_latest_html(html_file, silent=True)
                if hasattr(self, "claude_win") and self.claude_win and self.claude_win.winfo_exists():
                    self.after(0, self.claude_win.load_latest_report)
            self.log("========== 一键全自动完毕 ==========")

        self._bg(sequence)

    def stop_and_summarize(self):
        if self.current_process and self.current_process.poll() is None:
            self.log("\n🛑 正在终止当前任务进程树（不影响 QQ）...")
            try:
                parent = psutil.Process(self.current_process.pid)
                for child in parent.children(recursive=True):
                    child.kill()
                parent.kill()
                self.log("✅ 任务进程已终止。")
            except Exception as e:
                self.log(f"⚠️ 终止进程时出错: {e}")
            self.current_process = None
        else:
            self.log("\n⚠️ 当前没有正在运行的任务进程。")
        self.after(0, self._apply_end, False)
        self.log("\n========== 开始总结已提取的结果 ==========")
        self.generate_summary_report()

    def generate_summary_report(self):
        root = self.work_entry.get().replace("\\", "/")
        out_dir = self.output_entry.get().replace("\\", "/")
        possible_paths = [
            os.path.join(root, "data", "index.db"),
            os.path.join(root, "cached", "index.db"),
            os.path.join(out_dir, "cached", "index.db"),
            os.path.join(out_dir, "index.db"),
        ]
        db_path = next((p for p in possible_paths if os.path.exists(p)), None)
        if not db_path:
            self.log("❌ 在工作目录/输出目录中都找不到 index.db 文件。")
            self.log("👉 请先运行「2️⃣ 构建/刷新索引」。")
            return
        self.log(f"📁 找到索引数据库: {db_path}")
        try:
            con = sqlite3.connect(db_path)
            cur = con.cursor()
            cur.execute("SELECT COUNT(*) FROM messages")
            total_msgs = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM groups")
            total_groups = cur.fetchone()[0]
            if total_msgs == 0:
                self.log("⚠️ 数据库中尚无消息记录。")
                con.close()
                return
            cur.execute("SELECT MIN(day), MAX(day) FROM messages")
            min_day, max_day = cur.fetchone()
            meta = {}
            try:
                meta = {
                    k: v for k, v in cur.execute("SELECT key, value FROM meta")
                }
            except sqlite3.DatabaseError:
                pass
            cur.execute(
                "SELECT group_name, COUNT(*) FROM messages GROUP BY group_code ORDER BY COUNT(*) DESC LIMIT 5"
            )
            top_groups = cur.fetchall()
            cur.execute(
                "SELECT kind, COUNT(*) FROM messages GROUP BY kind ORDER BY COUNT(*) DESC"
            )
            kind_stats = cur.fetchall()
            report = (
                "\n================ 阶段结果总结报告 ================\n"
                "🕒 生成时间: %s\n"
                "📊 已处理消息总数: %d 条\n"
                "👥 涉及群组数量: %d 个\n"
                "📅 覆盖时间范围: %s 至 %s\n"
                "⏱ 索引构建时间: %s（窗口 %s 天）\n\n"
                "🔥 最活跃群组 Top 5:\n"
            ) % (
                time.strftime("%Y-%m-%d %H:%M:%S"),
                total_msgs,
                total_groups,
                min_day,
                max_day,
                meta.get("built_at", "-"),
                meta.get("window_days", "-"),
            )
            for g, c in top_groups:
                report += "  - %s: %d 条消息\n" % (g if g else "未知群", c)
            report += "\n📎 消息类型分布:\n"
            for k, c in kind_stats:
                report += "  - %s: %d 条\n" % (k, c)
            report += "=================================================="
            self.log(report)
            con.close()
        except Exception as e:
            self.log(f"❌ 总结时出错: {e}")


def _report(msg):
    try:
        with open(os.path.join(APP_DIR, "smoke_result.txt"), "w", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass
    print(msg)


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        app = QQToolGUI()
        py = app.get_python_exe()
        app.after(2500, app.destroy)
        app.mainloop()
        _report(
            f"SELF-TEST OK\nFROZEN={FROZEN}\nAPP_DIR={APP_DIR}\nSCRIPTS_DIR={SCRIPTS_DIR}\nCONFIG_PATH={CONFIG_PATH}\nPYTHON={py}"
        )
    elif "--engine-test" in sys.argv:
        app = QQToolGUI()
        app.update()
        py = app.get_python_exe()
        acc = ""
        if "--qq" in sys.argv:
            try:
                acc = sys.argv[sys.argv.index("--qq") + 1].strip()
            except IndexError:
                acc = ""
        acc = acc or app.acc_entry.get().strip()
        if not acc:
            _report(
                f"ENGINE-TEST SKIPPED: 未配置 QQ 号，跳过真实引擎自检\nFROZEN={FROZEN}\nAPP_DIR={APP_DIR}\nSCRIPTS_DIR={SCRIPTS_DIR}\nCONFIG_PATH={CONFIG_PATH}\nPYTHON={py}"
            )
            app.destroy()
        else:
            out = os.path.join(
                app.work_entry.get().replace("\\", "/"), "keys", "engine_test.json"
            )
            code = app.run_command(
                [os.path.join(SCRIPTS_DIR, "ntqq_key.py"), "--qq", acc, "-o", out]
            )
            app.update()
            tail = app.log_text.get("1.0", "end")[-3000:]
            try:
                if os.path.exists(out):
                    os.remove(out)
            except Exception:
                pass
            _report(
                f"ENGINE-TEST EXIT={code}\nFROZEN={FROZEN}\nAPP_DIR={APP_DIR}\nSCRIPTS_DIR={SCRIPTS_DIR}\nCONFIG_PATH={CONFIG_PATH}\nPYTHON={py}\n--- log tail ---\n{tail}"
            )
            app.destroy()
    else:
        app = QQToolGUI()
        app.mainloop()
