#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQ 群聊简报 · 智能分析台启动入口 (Claude & Monet Edition)."""

import os
import sys

from gui import (
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
    STAGE_LABELS,
    DEFAULT_WORKSPACE,
    ClaudeReaderWindow,
    QQToolGUI,
)
from gui.themes import FROZEN, BUNDLE_DIR, APP_DIR, CONFIG_PATH, SCRIPTS_DIR


def _report(msg: str) -> None:
    try:
        with open(os.path.join(APP_DIR, "smoke_result.txt"), "w", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass
    print(msg)


def main() -> None:
    """Application entrypoint with test runner support."""
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
