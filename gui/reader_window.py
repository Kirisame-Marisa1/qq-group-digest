# -*- coding: utf-8 -*-
"""Independent reading window with Claude & Monet (Maison des Mots) dual aesthetics."""

import os
import re
import subprocess
import tkinter as tk
from tkinter import messagebox
import customtkinter as ctk

from gui.themes import (
    CLAUDE_BG,
    CLAUDE_CARD,
    CLAUDE_CARD_WARM,
    CLAUDE_BORDER,
    CLAUDE_BORDER_SUBTLE,
    CLAUDE_TEXT,
    CLAUDE_TEXT_SUB,
    CLAUDE_TEXT_MUTED,
    CLAUDE_TERRACOTTA,
    CLAUDE_TERRACOTTA_HOVER,
    CLAUDE_TERRACOTTA_SOFT,
    CLAUDE_BLUE,
    CLAUDE_BLUE_HOVER,
    CLAUDE_GREEN,
    CLAUDE_GREEN_HOVER,
    CLAUDE_MUTED_BTN,
    CLAUDE_MUTED_HOVER,
    MONET_BG,
    MONET_CARD,
    MONET_CARD_WARM,
    MONET_BORDER,
    MONET_BORDER_SUBTLE,
    MONET_INK,
    MONET_INK_SOFT,
    MONET_FOG,
    MONET_LOTUS,
    MONET_LOTUS_HOVER,
    MONET_LOTUS_SOFT,
    MONET_SILVER_BTN,
    MONET_SILVER_HOVER,
    MONET_SILVER_LIGHT,
)


class ClaudeReaderWindow(ctk.CTkToplevel):
    """Claude (Anthropic) / 莫奈纸雕 (Maison des Mots) 双美学风格沉浸式独立阅读视窗。
    支持实时在 Claude 极简暖调与莫奈洛可可纸雕信笺风格间一键无缝切换。
    """
    def __init__(self, parent, initial_theme="claude"):
        super().__init__(parent)
        self.parent = parent
        self.current_theme = initial_theme
        self.geometry("1120x820")

        # Center on screen
        self.update_idletasks()
        x = max(20, (self.winfo_screenwidth() - 1120) // 2)
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
        self.nav = ctk.CTkFrame(self, corner_radius=0, border_width=1)
        self.nav.grid(row=0, column=0, columnspan=2, sticky="ew")

        left_nav = ctk.CTkFrame(self.nav, fg_color="transparent")
        left_nav.pack(side="left", padx=16, pady=10)

        self.brand_icon = ctk.CTkLabel(
            left_nav,
            text="✦",
            font=ctk.CTkFont(family="Georgia", size=20, weight="bold"),
        )
        self.brand_icon.pack(side="left", padx=(0, 8))

        self.brand_title = ctk.CTkLabel(
            left_nav,
            text="阅读视窗",
            font=ctk.CTkFont(family="Georgia", size=17, weight="bold"),
        )
        self.brand_title.pack(side="left", padx=(0, 10))

        self.badge_lbl = ctk.CTkLabel(
            left_nav,
            text="",
            font=ctk.CTkFont(size=11, weight="bold"),
            corner_radius=6,
            padx=8,
            pady=2,
        )
        self.badge_lbl.pack(side="left")

        right_nav = ctk.CTkFrame(self.nav, fg_color="transparent")
        right_nav.pack(side="right", padx=16, pady=10)

        self.tool_style = {
            "corner_radius": 7,
            "height": 30,
            "font": ctk.CTkFont(size=12, weight="bold"),
        }

        # Style Switcher Button
        self.theme_btn = ctk.CTkButton(
            right_nav,
            text="🌸 莫奈纸雕风格",
            command=self.toggle_theme,
            border_width=1,
            **self.tool_style,
        )
        self.theme_btn.pack(side="left", padx=5)

        self.browser_btn = ctk.CTkButton(
            right_nav,
            text="🌐 浏览器沉浸视窗",
            command=self.open_in_browser,
            **self.tool_style,
        )
        self.browser_btn.pack(side="left", padx=5)

        self.copy_btn = ctk.CTkButton(
            right_nav,
            text="📋 复制当前文本",
            command=self.copy_current_text,
            border_width=1,
            **self.tool_style,
        )
        self.copy_btn.pack(side="left", padx=5)

        self.dir_btn = ctk.CTkButton(
            right_nav,
            text="📁 打开所在目录",
            command=self.open_output_dir,
            border_width=1,
            **self.tool_style,
        )
        self.dir_btn.pack(side="left", padx=5)

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

        self.apply_theme(self.current_theme)
        self.load_latest_report()

    def apply_theme(self, theme_name):
        self.current_theme = theme_name
        is_monet = (theme_name == "monet")
        if is_monet:
            self.title("🌸 莫奈纸雕视窗 · 字里行间 · QQ 群聊每日简报")
            self.configure(fg_color=MONET_BG)
            self.nav.configure(fg_color=MONET_CARD, border_color=MONET_BORDER)
            self.brand_icon.configure(text="🌸", text_color=MONET_INK)
            self.brand_title.configure(text="莫奈纸雕视窗 · 字里行间", text_color=MONET_INK)
            self.badge_lbl.configure(
                text="🌸 莫奈纸雕 · Maison des Mots",
                fg_color=MONET_LOTUS,
                text_color=MONET_INK,
            )
            self.theme_btn.configure(
                text="✨ 切换为 Claude 风格",
                fg_color=MONET_CARD_WARM,
                hover_color=MONET_BORDER,
                text_color=MONET_INK,
                border_color=MONET_BORDER,
            )
            self.browser_btn.configure(
                fg_color=MONET_INK,
                hover_color="#2D3A63",
                text_color="#FFFFFF",
            )
            self.copy_btn.configure(
                fg_color=MONET_CARD_WARM,
                hover_color=MONET_BORDER,
                text_color=MONET_INK,
                border_color=MONET_BORDER,
            )
            self.dir_btn.configure(
                fg_color=MONET_CARD_WARM,
                hover_color=MONET_BORDER,
                text_color=MONET_INK,
                border_color=MONET_BORDER,
            )
            self.sidebar.configure(fg_color=MONET_CARD, border_color=MONET_BORDER)
            self.search_entry.configure(
                fg_color=MONET_CARD_WARM,
                border_color=MONET_BORDER,
                text_color=MONET_INK,
            )
            self.canvas_card.configure(fg_color=MONET_CARD, border_color=MONET_BORDER)
            self.canvas_header.configure(fg_color=MONET_CARD_WARM)
            self.reading_title.configure(text_color=MONET_INK)
            self.reading_sub.configure(text_color=MONET_INK_SOFT)
            self.reading_box.configure(
                fg_color=MONET_CARD,
                text_color=MONET_INK,
                border_color=MONET_BORDER,
                font=ctk.CTkFont(family="KaiTi", size=13),
            )
        else:
            self.title("✦ Claude 阅读视窗 · QQ 群聊每日简报")
            self.configure(fg_color=CLAUDE_BG)
            self.nav.configure(fg_color=CLAUDE_CARD, border_color=CLAUDE_BORDER)
            self.brand_icon.configure(text="✦", text_color=CLAUDE_TERRACOTTA)
            self.brand_title.configure(text="Claude 阅读视窗", text_color=CLAUDE_TEXT)
            self.badge_lbl.configure(
                text="Artifact · 沉浸速读",
                fg_color=CLAUDE_TERRACOTTA_SOFT,
                text_color=CLAUDE_TERRACOTTA,
            )
            self.theme_btn.configure(
                text="🌸 切换为莫奈纸雕",
                fg_color=MONET_LOTUS,
                hover_color=MONET_LOTUS_HOVER,
                text_color=MONET_INK,
                border_color=MONET_BORDER,
            )
            self.browser_btn.configure(
                fg_color=CLAUDE_TERRACOTTA,
                hover_color=CLAUDE_TERRACOTTA_HOVER,
                text_color="#FFFFFF",
            )
            self.copy_btn.configure(
                fg_color=CLAUDE_MUTED_BTN,
                hover_color=CLAUDE_MUTED_HOVER,
                text_color=CLAUDE_TEXT,
                border_color=CLAUDE_BORDER,
            )
            self.dir_btn.configure(
                fg_color=CLAUDE_MUTED_BTN,
                hover_color=CLAUDE_MUTED_HOVER,
                text_color=CLAUDE_TEXT,
                border_color=CLAUDE_BORDER,
            )
            self.sidebar.configure(fg_color=CLAUDE_CARD, border_color=CLAUDE_BORDER)
            self.search_entry.configure(
                fg_color="#FAF9F5",
                border_color=CLAUDE_BORDER,
                text_color=CLAUDE_TEXT,
            )
            self.canvas_card.configure(fg_color=CLAUDE_CARD, border_color=CLAUDE_BORDER)
            self.canvas_header.configure(fg_color=CLAUDE_CARD_WARM)
            self.reading_title.configure(text_color=CLAUDE_TEXT)
            self.reading_sub.configure(text_color=CLAUDE_TEXT_SUB)
            self.reading_box.configure(
                fg_color="#FDFBF7",
                text_color=CLAUDE_TEXT,
                border_color=CLAUDE_BORDER,
                font=ctk.CTkFont(family="Consolas", size=13),
            )
        if self.groups:
            self.render_group_buttons(self.search_entry.get())

    def toggle_theme(self):
        new_theme = "claude" if self.current_theme == "monet" else "monet"
        self.apply_theme(new_theme)

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

        grp_re = re.compile(r"^##\s+(?:(\d+)\.\s+)?(.+?)(?:\s+\((\d+)\))?$")
        for line in lines:
            m = grp_re.match(line.strip())
            if m:
                name = m.group(2).strip()
                if any(k in name for k in ["重点速览", "重点通知", "全群总览", "KPI", "概览"]):
                    if cur_group:
                        cur_lines.append(line)
                    else:
                        preamble.append(line)
                    continue
                if cur_group:
                    cur_group["text"] = "\n".join(cur_lines).strip()
                    self.groups.append(cur_group)
                idx = int(m.group(1)) if m.group(1) else (len(self.groups) + 1)
                cur_group = {
                    "idx": idx,
                    "name": name,
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

        is_monet = (self.current_theme == "monet")
        act_bg = MONET_SILVER_LIGHT if is_monet else CLAUDE_CARD_WARM
        act_fg = MONET_INK if is_monet else CLAUDE_TERRACOTTA
        norm_fg = MONET_INK if is_monet else CLAUDE_TEXT
        hov_bg = MONET_CARD_WARM if is_monet else CLAUDE_MUTED_HOVER

        btn_all = ctk.CTkButton(
            self.group_scroll,
            text="🌸 全卷信札总览" if is_monet else "🌐 全群宏观总览",
            anchor="w",
            fg_color=act_bg if self.active_group_idx == -1 else "transparent",
            hover_color=hov_bg,
            text_color=act_fg if self.active_group_idx == -1 else norm_fg,
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
            prefix = "✉️ " if is_monet else ""
            btn = ctk.CTkButton(
                self.group_scroll,
                text=f"{prefix}{g['idx']}. {g['name']}",
                anchor="w",
                fg_color=act_bg if is_act else "transparent",
                hover_color=hov_bg,
                text_color=act_fg if is_act else norm_fg,
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

    def open_in_browser(self, view=None):
        target_view = view or ("monet" if getattr(self, "current_theme", "claude") == "monet" else "claude")
        if self.html_path and os.path.exists(self.html_path):
            self.parent.open_latest_html(path=self.html_path, view=target_view)
        else:
            self.parent.open_latest_html(view=target_view)

    def open_output_dir(self):
        out_dir = self.parent.output_entry.get()
        if os.path.isdir(out_dir):
            try:
                os.startfile(os.path.normpath(out_dir))
            except Exception as e:
                messagebox.showwarning("打开失败", f"无法打开目录: {e}")


