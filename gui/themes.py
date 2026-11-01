# -*- coding: utf-8 -*-
"""Design system constants and paths for qq-group-digest GUI."""

import os
import sys

FROZEN = bool(getattr(sys, "frozen", False))
BUNDLE_DIR = getattr(sys, "_MEIPASS", None)

if FROZEN:
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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

# Monet / Maison des Mots Design System Constants
MONET_BG = "#F5F3EC"             # Canvas stone / warm background
MONET_CARD = "#FBF9F4"           # Paper light / parchment
MONET_CARD_WARM = "#EEF0F5"      # Canvas deep / fog blue tint
MONET_BORDER = "#BFC8E0"         # Silver blue border
MONET_BORDER_SUBTLE = "#DCE2F1"  # Mist blue hairline
MONET_INK = "#1E2748"            # Deep ink blue
MONET_INK_SOFT = "#46536F"       # Soft ink
MONET_FOG = "#5C6884"            # Fog
MONET_LOTUS = "#E8C9D0"          # Lotus pink / rose seal
MONET_LOTUS_HOVER = "#DCB5BE"
MONET_LOTUS_SOFT = "#F7EFF1"
MONET_SILVER_BTN = "#DCE2F1"
MONET_SILVER_HOVER = "#BFC8E0"
MONET_SILVER_LIGHT = "#E8EDF8"
