#!/usr/bin/env pythonw
# -*- coding: utf-8 -*-
"""Windowless launcher for GUI desktop application."""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
for _p in (_PARENT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from gui_app import main
except ImportError:
    from gui import QQToolGUI
    def main():
        app = QQToolGUI()
        app.mainloop()

if __name__ == "__main__":
    main()

