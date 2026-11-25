#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQ 群聊简报 · 智能分析台启动入口 (Claude & Monet Edition)."""

import os
import sys

from gui import (
    CLAUDE_BG,
    CLAUDE_CARD,
    CLAUDE_BORDER,
    CLAUDE_TERRACOTTA,
    MONET_BG,
    MONET_CARD,
    MONET_BORDER,
    MONET_INK,
    MONET_LOTUS,
    MONET_SILVER_LIGHT,
    ClaudeReaderWindow,
    QQToolGUI,
)
from gui.themes import FROZEN, BUNDLE_DIR, APP_DIR, CONFIG_PATH, SCRIPTS_DIR

__all__ = [
    "CLAUDE_BG",
    "CLAUDE_CARD",
    "CLAUDE_BORDER",
    "CLAUDE_TERRACOTTA",
    "MONET_BG",
    "MONET_CARD",
    "MONET_BORDER",
    "MONET_INK",
    "MONET_LOTUS",
    "MONET_SILVER_LIGHT",
    "ClaudeReaderWindow",
    "QQToolGUI",
    "main",
]


def _report(msg: str) -> None:
    try:
        with open(os.path.join(APP_DIR, "smoke_result.txt"), "w", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass
    print(msg)


def _maybe_hide_console() -> None:
    """If launched on Windows via python.exe without CLI arguments, hide redundant console window."""
    if sys.platform == "win32" and not FROZEN:
        if len(sys.argv) > 1:
            return
        try:
            import ctypes
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd:
                pids = (ctypes.c_uint * 2)()
                count = ctypes.windll.kernel32.GetConsoleProcessList(pids, 2)
                if count <= 1:
                    ctypes.windll.user32.ShowWindow(hwnd, 0)
        except Exception:
            pass


def main() -> None:
    """Application entrypoint with test runner support."""
    _maybe_hide_console()
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


if __name__ == "__main__":
    main()
