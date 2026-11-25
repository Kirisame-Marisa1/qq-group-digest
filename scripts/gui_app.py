#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Command-line launcher for GUI desktop application."""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
for _p in (_HERE, _PARENT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from gui import *  # noqa: F401,F403
    from gui.app import main
except ImportError:
    from gui import QQToolGUI  # noqa: F401

    def main() -> None:
        app = QQToolGUI()
        app.mainloop()

if __name__ == "__main__":
    main()
