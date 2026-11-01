#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Command-line launcher for GUI desktop application."""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
for _p in (_PARENT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from gui import *

try:
    from gui_app import *
except ImportError:
    pass

if __name__ == "__main__":
    main()
