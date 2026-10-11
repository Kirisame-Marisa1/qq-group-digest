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

ctk.set_appearance_mode("Dark")
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


class QQToolGUI(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("QQ 聊天记录提取工具 (Pro - 按需解密版)")
        self.geometry("960x900")

        self.update_idletasks()
        x = (self.winfo_screenwidth() - 960) // 2
        y = (self.winfo_screenheight() - 900) // 2
        self.geometry(f"+{x}+{y}")

        self.current_process = None
        self.busy = False
        self.last_html_path = None

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)

        # Header
        header_frame = ctk.CTkFrame(self, fg_color="transparent")
        header_frame.grid(row=0, column=0, padx=20, pady=(20, 10), sticky="ew")
        ctk.CTkLabel(
            header_frame,
            text="主页 / 按需解密（QQ 保持运行）",
            font=ctk.CTkFont(size=20, weight="bold"),
        ).pack(side="left")
        ctk.CTkButton(
            header_frame,
            text="🔄 自动检测运行中的 QQ",
            command=self.auto_detect_qq,
            corner_radius=8,
        ).pack(side="right")

        # Config Frame
        config_frame = ctk.CTkFrame(self, corner_radius=10)
        config_frame.grid(row=1, column=0, padx=20, pady=10, sticky="ew")
        config_frame.grid_columnconfigure((1, 3), weight=1)

        ctk.CTkLabel(config_frame, text="QQ号:", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, padx=15, pady=12, sticky="e"
        )
        self.acc_entry = ctk.CTkEntry(config_frame, placeholder_text="请输入QQ号")
        self.acc_entry.grid(row=0, column=1, padx=15, pady=12, sticky="ew")

        ctk.CTkLabel(config_frame, text="索引范围:", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=2, padx=15, pady=12, sticky="e"
        )
        self.days_combo = ctk.CTkOptionMenu(
            config_frame,
            values=["最近 7 天", "最近 30 天", "最近 90 天", "最近 180 天", "最近 365 天", "全部历史"],
        )
        self.days_combo.set("最近 7 天")
        self.days_combo.grid(row=0, column=3, padx=15, pady=12, sticky="w")

        ctk.CTkLabel(config_frame, text="数据根目录:", font=ctk.CTkFont(weight="bold")).grid(
            row=1, column=0, padx=15, pady=(0, 10), sticky="e"
        )
        self.data_root_entry = ctk.CTkEntry(config_frame)
        self.data_root_entry.grid(row=1, column=1, columnspan=2, padx=15, pady=(0, 10), sticky="ew")
        ctk.CTkButton(
            config_frame,
            text="浏览...",
            width=60,
            command=lambda: self.browse_dir(self.data_root_entry),
        ).grid(row=1, column=3, padx=15, pady=(0, 10), sticky="w")

        ctk.CTkLabel(config_frame, text="工作目录:", font=ctk.CTkFont(weight="bold")).grid(
            row=2, column=0, padx=15, pady=(0, 10), sticky="e"
        )
        self.work_entry = ctk.CTkEntry(config_frame)
        self.work_entry.grid(row=2, column=1, columnspan=2, padx=15, pady=(0, 10), sticky="ew")
        ctk.CTkButton(
            config_frame,
            text="浏览...",
            width=60,
            command=lambda: self.browse_dir(self.work_entry),
        ).grid(row=2, column=3, padx=15, pady=(0, 10), sticky="w")

        ctk.CTkLabel(config_frame, text="输出目录:", font=ctk.CTkFont(weight="bold")).grid(
            row=3, column=0, padx=15, pady=(0, 10), sticky="e"
        )
        self.output_entry = ctk.CTkEntry(config_frame)
        self.output_entry.grid(row=3, column=1, columnspan=2, padx=15, pady=(0, 10), sticky="ew")
        ctk.CTkButton(
            config_frame,
            text="浏览...",
            width=60,
            command=lambda: self.browse_dir(self.output_entry),
        ).grid(row=3, column=3, padx=15, pady=(0, 10), sticky="w")

        ctk.CTkLabel(config_frame, text="QQ.exe路径:", font=ctk.CTkFont(weight="bold")).grid(
            row=4, column=0, padx=15, pady=(0, 10), sticky="e"
        )
        self.exe_entry = ctk.CTkEntry(config_frame)
        self.exe_entry.grid(row=4, column=1, columnspan=3, padx=15, pady=(0, 10), sticky="ew")

        ctk.CTkLabel(config_frame, text="群友筛选(可选):", font=ctk.CTkFont(weight="bold")).grid(
            row=5, column=0, padx=15, pady=(0, 10), sticky="e"
        )
        self.member_entry = ctk.CTkEntry(
            config_frame, placeholder_text="留空生成全群简报；填昵称/群名片/QQ号可生成该群友专属报告"
        )
        self.member_entry.grid(row=5, column=1, columnspan=3, padx=15, pady=(0, 10), sticky="ew")

        self.keep_plain_var = ctk.BooleanVar(value=False)
        self.keep_plain_box = ctk.CTkCheckBox(
            config_frame,
            variable=self.keep_plain_var,
            text="保留明文缓存 nt_msg.db（约 13GB，加速下次刷新；不勾选则构建完成后自动清理）",
        )
        self.keep_plain_box.grid(row=6, column=0, columnspan=4, padx=15, pady=(0, 10), sticky="w")

        info_label = ctk.CTkLabel(
            config_frame,
            text="💡 全程无需关闭 QQ：密钥从内存只读提取，数据库按需解密（仅 nt_msg.db / group_info.db / profile_info.db），QQ 活跃写入导致的坏页会自动换快照重试。",
            text_color="#4EC9B0",
            font=ctk.CTkFont(size=12),
        )
        info_label.grid(row=7, column=0, columnspan=4, padx=15, pady=(0, 12), sticky="w")

        # Buttons
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.grid(row=2, column=0, padx=20, pady=10, sticky="ew")
        btn_style = {
            "corner_radius": 8,
            "height": 34,
            "font": ctk.CTkFont(size=13, weight="bold"),
        }

        row_a = ctk.CTkFrame(btn_frame, fg_color="transparent")
        row_a.pack(fill="x")
        ctk.CTkButton(
            row_a,
            text="💾 保存配置",
            command=self.save_config,
            fg_color="#444444",
            hover_color="#555555",
            **btn_style,
        ).pack(side="left", padx=(0, 10))
        ctk.CTkButton(
            row_a,
            text="1️⃣ 提取密钥",
            command=self.run_key_extract,
            fg_color="#0078D4",
            hover_color="#0063B1",
            **btn_style,
        ).pack(side="left", padx=10)
        ctk.CTkButton(
            row_a,
            text="2️⃣ 构建/刷新索引",
            command=self.run_build_index,
            fg_color="#0078D4",
            hover_color="#0063B1",
            **btn_style,
        ).pack(side="left", padx=10)
        ctk.CTkButton(
            row_a,
            text="3️⃣ 生成简报",
            command=self.run_report,
            fg_color="#0078D4",
            hover_color="#0063B1",
            **btn_style,
        ).pack(side="left", padx=10)
        ctk.CTkButton(
            row_a,
            text="🌐 打开可视化简报 (HTML)",
            command=self.open_latest_html,
            fg_color="#6B46C1",
            hover_color="#553C9A",
            **btn_style,
        ).pack(side="left", padx=10)

        row_b = ctk.CTkFrame(btn_frame, fg_color="transparent")
        row_b.pack(fill="x", pady=(10, 0))
        ctk.CTkButton(
            row_b,
            text="🚀 一键全自动（密钥→索引→简报）",
            command=self.run_auto_all,
            fg_color="#107C10",
            hover_color="#0B5A0B",
            **btn_style,
        ).pack(side="left")
        ctk.CTkButton(
            row_b,
            text="⏹️ 终止并总结（不影响 QQ）",
            command=self.stop_and_summarize,
            fg_color="#C42B1C",
            hover_color="#9E2216",
            **btn_style,
        ).pack(side="right")

        # Status Bar
        status_frame = ctk.CTkFrame(self, fg_color="transparent")
        status_frame.grid(row=3, column=0, padx=20, pady=(10, 0), sticky="ew")
        status_frame.grid_columnconfigure(1, weight=1)

        self.state_label = ctk.CTkLabel(
            status_frame, text="状态: 就绪", text_color="gray", width=150, anchor="w"
        )
        self.state_label.grid(row=0, column=0, padx=(0, 10), sticky="w")
        self.progress_bar = ctk.CTkProgressBar(status_frame, mode="determinate")
        self.progress_bar.grid(row=0, column=1, sticky="ew")
        self.progress_bar.set(0)
        self.pct_label = ctk.CTkLabel(status_frame, text="", width=60, anchor="e")
        self.pct_label.grid(row=0, column=2, padx=(10, 0), sticky="e")

        # Log Frame
        log_frame = ctk.CTkFrame(self, corner_radius=10)
        log_frame.grid(row=4, column=0, padx=20, pady=(10, 20), sticky="nsew")

        self.log_text = ctk.CTkTextbox(
            log_frame, wrap="word", font=ctk.CTkFont(family="Consolas", size=12)
        )
        self.log_text.pack(fill="both", expand=True, padx=10, pady=10)
        self.log_text.configure(state="disabled")

        self.load_config()

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
