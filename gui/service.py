# -*- coding: utf-8 -*-
"""Business service layer for qq-group-digest GUI.

Decouples configuration persistence, process execution, QQ process detection,
and SQLite reporting from the Tkinter UI layer for headless automated testing.
"""

import json
import os
import re
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    psutil = None
    _HAS_PSUTIL = False

from gui.themes import CONFIG_PATH, DEFAULT_WORKSPACE, FROZEN, SCRIPTS_DIR


class QQGuiService:
    """Headless service managing background operations and data inspections."""

    @staticmethod
    def get_python_exe(frozen: bool = FROZEN, executable: Optional[str] = None) -> Optional[str]:
        """Resolve python interpreter path for both frozen and dev modes."""
        if not frozen:
            return sys.executable
        target_exe = executable or sys.executable
        exe_dir = os.path.dirname(target_exe)
        for cand in (
            os.path.join(exe_dir, "python", "Scripts", "python.exe"),
            os.path.join(exe_dir, "python", "python.exe"),
            os.path.join(exe_dir, "runtime", "python.exe"),
            os.path.join(exe_dir, "python.exe"),
        ):
            if os.path.exists(cand):
                return cand
        return None

    @staticmethod
    def load_config(config_path: str = CONFIG_PATH) -> Dict[str, Any]:
        """Load and return configuration dict, handling encoding gracefully."""
        if not os.path.exists(config_path):
            return {}
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except UnicodeDecodeError:
            with open(config_path, "r", encoding="gbk") as f:
                return json.load(f)

    @staticmethod
    def save_config(config_data: Dict[str, Any], config_path: str = CONFIG_PATH) -> bool:
        """Save configuration dictionary into json file with fallback merge."""
        existing = QQGuiService.load_config(config_path)
        merged = {**existing, **config_data}
        os.makedirs(os.path.dirname(os.path.abspath(config_path)), exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2, ensure_ascii=False)
        return True

    @staticmethod
    def discover_qq_base_dirs() -> List[str]:
        """Discover potential Tencent Files base directories."""
        candidates = []
        userprofile = os.environ.get("USERPROFILE", "")
        if userprofile:
            docs = os.path.join(userprofile, "Documents", "Tencent Files")
            if os.path.isdir(docs):
                candidates.append(docs)
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            root = f"{letter}:/"
            if not os.path.exists(root):
                continue
            for sub in ("Tencent Files", "QQNT/Tencent Files"):
                cand = os.path.join(root, sub)
                if os.path.isdir(cand) and cand not in candidates:
                    candidates.append(cand)
        return candidates

    @staticmethod
    def _detect_qq_exe_ctypes() -> Optional[str]:
        """Fallback to Windows Toolhelp32 API when psutil is unavailable."""
        if sys.platform != "win32":
            return None
        import ctypes
        import ctypes.wintypes as wt

        TH32CS_SNAPPROCESS = 0x00000002
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ('dwSize', wt.DWORD),
                ('cntUsage', wt.DWORD),
                ('th32ProcessID', wt.DWORD),
                ('th32DefaultHeapID', ctypes.POINTER(ctypes.c_ulong)),
                ('th32ModuleID', wt.DWORD),
                ('cntThreads', wt.DWORD),
                ('th32ParentProcessID', wt.DWORD),
                ('pcPriClassBase', ctypes.c_long),
                ('dwFlags', wt.DWORD),
                ('szExeFile', ctypes.c_char * 260),
            ]

        snap = ctypes.windll.kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap in (ctypes.c_void_p(-1).value, -1, 0):
            return None
        pe = PROCESSENTRY32()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
        qq_pid = None
        if ctypes.windll.kernel32.Process32First(snap, ctypes.byref(pe)):
            while True:
                exe_name = pe.szExeFile.decode('utf-8', 'replace').lower()
                if exe_name == 'qq.exe':
                    qq_pid = pe.th32ProcessID
                    break
                if not ctypes.windll.kernel32.Process32Next(snap, ctypes.byref(pe)):
                    break
        ctypes.windll.kernel32.CloseHandle(snap)

        if not qq_pid:
            return None

        h_proc = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, qq_pid)
        if not h_proc:
            return "QQ.exe"
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wt.DWORD(len(buf))
            if ctypes.windll.kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size)):
                return buf.value
            return "QQ.exe"
        finally:
            ctypes.windll.kernel32.CloseHandle(h_proc)

    @staticmethod
    def detect_running_qq() -> Tuple[Optional[str], List[Tuple[float, str, str]]]:
        """Detect running QQ.exe process and matching data directory candidates.

        Returns:
            Tuple of (qq_exe_path, list of (mtime, qq_number, data_dir))
        """
        qq_exe = None
        if _HAS_PSUTIL and psutil is not None:
            for proc in psutil.process_iter(["name", "exe"]):
                try:
                    name = proc.info.get("name")
                    exe = proc.info.get("exe")
                    if name and name.lower() == "qq.exe" and exe:
                        qq_exe = exe
                        break
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        else:
            qq_exe = QQGuiService._detect_qq_exe_ctypes()

        matched_candidates = []
        if qq_exe:
            base_dirs = QQGuiService.discover_qq_base_dirs()
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
                            matched_candidates.append((mtime, f, full))
                except Exception:
                    pass
            matched_candidates.sort(key=lambda x: x[0], reverse=True)

        return qq_exe, matched_candidates

    @staticmethod
    def query_database_summary(db_path: str) -> Optional[Dict[str, Any]]:
        """Query statistics from local digested SQLite database without Tkinter."""
        if not os.path.exists(db_path):
            return None
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        try:
            cur.execute("SELECT COUNT(*) FROM messages")
            total_msgs = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM groups")
            total_groups = cur.fetchone()[0]
            if total_msgs == 0:
                con.close()
                return {"total_msgs": 0, "total_groups": total_groups}

            cur.execute("SELECT MIN(day), MAX(day) FROM messages")
            min_day, max_day = cur.fetchone()

            meta = {}
            try:
                meta = {k: v for k, v in cur.execute("SELECT key, value FROM meta")}
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

            con.close()
            return {
                "total_msgs": total_msgs,
                "total_groups": total_groups,
                "min_day": min_day,
                "max_day": max_day,
                "built_at": meta.get("built_at", "-"),
                "window_days": meta.get("window_days", "-"),
                "top_groups": top_groups,
                "kind_stats": kind_stats,
            }
        except Exception:
            con.close()
            raise

    @staticmethod
    def format_database_summary(stats: Dict[str, Any]) -> str:
        """Format database summary statistics dict into readable text report."""
        if not stats or stats.get("total_msgs", 0) == 0:
            return "⚠️ 数据库中尚无消息记录。"

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
            stats["total_msgs"],
            stats["total_groups"],
            stats.get("min_day", "-"),
            stats.get("max_day", "-"),
            stats.get("built_at", "-"),
            stats.get("window_days", "-"),
        )
        for g, c in stats.get("top_groups", []):
            report += "  - %s: %d 条消息\n" % (g if g else "未知群", c)
        report += "\n📎 消息类型分布:\n"
        for k, c in stats.get("kind_stats", []):
            report += "  - %s: %d 条\n" % (k, c)
        report += "=================================================="
        return report

