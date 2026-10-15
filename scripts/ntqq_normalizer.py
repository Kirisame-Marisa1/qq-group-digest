#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""群聊噪声清洗器 (NoiseNormalizer)。

职责：
1. 复读机折叠 (Repeater Collapser)：
   识别连续相同的文本或图片发言（如连续多人发送 "+1"、"收到" 或同一张图），
   将其折叠为单条元信息：`[群友 A, B, C 等 N 人复读了: "xxx"]`，
   防止多条无增量信息的复读刷屏占用宝贵的 LLM Context Window。
2. 机器人指令过滤 (Bot Command Filter)：
   识别并标记群内常见娱乐/功能性 Bot 的刷屏指令（签到、抽卡、运势、投骰子等）。
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union


# 常见机器人指令正则
DEFAULT_BOT_PATTERNS = [
    # 常用前缀符号指令: ! / # .
    r'^[!！/／#＃.][\w\u4e00-\u9fff]{1,16}',
    # 常见纯文本娱乐指令
    r'^(签到|打卡|今日运势|每日运势|抽签|抽卡|开盲盒|塔罗牌|占卜|今日人品|查成分|看壁纸|点歌|帮助|help|menu|菜单)$',
    # CQ 码或 @bot 指令
    r'^\[CQ:at,qq=\d+\]\s*(签到|打卡|抽卡|运势|今日运势|菜单|帮助|help)',
    # TRPG 骰子指令
    r'^[.#/／]r\d*',
]


@dataclass
class NormalizedMessage:
    msg_id: int
    seq: int
    ts: int
    day: str
    hour: int
    group_code: int
    group_name: str
    sender_uid: str
    sender_qq: int
    sender_name: str
    direction: int
    msg_type: int
    subtype: int
    kind: str
    text: str
    reply_seq: int
    # 扩展降噪元信息
    is_folded: bool = False
    repeat_count: int = 1
    repeated_by: List[str] = field(default_factory=list)
    is_bot_command: bool = False


class NoiseNormalizer:
    """群聊消息流噪声清洗与复读机折叠器。"""

    def __init__(self, bot_patterns: Optional[List[str]] = None, min_repeat_to_fold: int = 2):
        patterns = bot_patterns if bot_patterns is not None else DEFAULT_BOT_PATTERNS
        self.bot_regex = re.compile('|'.join(f'(?:{p})' for p in patterns), re.I)
        self.min_repeat_to_fold = min_repeat_to_fold

    def is_bot_command(self, text: str) -> bool:
        """判断某段文本是否为机器人指令。"""
        t = (text or '').strip()
        if not t:
            return False
        return bool(self.bot_regex.search(t))

    def format_repeat_summary(self, names: List[str], text: str, max_names: int = 3) -> str:
        """生成格式化复读折叠摘要。"""
        total = len(names)
        display_names = names[:max_names]
        name_str = "、".join(display_names)
        t_preview = text.replace('\n', ' ').strip()
        if len(t_preview) > 60:
            t_preview = t_preview[:60] + "…"

        if total > max_names:
            return f"[群友 {name_str} 等 {total} 人复读了: \"{t_preview}\"]"
        return f"[群友 {name_str} 共 {total} 人复读了: \"{t_preview}\"]"

    def normalize_batch(self, messages: List[Union[Dict[str, Any], Tuple, NormalizedMessage]]) -> List[NormalizedMessage]:
        """对批次消息执行复读折叠与机器人指令标记。"""
        if not messages:
            return []

        # 统一转为 NormalizedMessage
        norm_list: List[NormalizedMessage] = []
        for m in messages:
            if isinstance(m, NormalizedMessage):
                nm = m
            elif isinstance(m, dict):
                nm = NormalizedMessage(
                    msg_id=m.get('msg_id', 0),
                    seq=m.get('seq', 0),
                    ts=m.get('ts', 0),
                    day=m.get('day', ''),
                    hour=m.get('hour', -1),
                    group_code=m.get('group_code', 0),
                    group_name=m.get('group_name', ''),
                    sender_uid=m.get('sender_uid', ''),
                    sender_qq=m.get('sender_qq', 0),
                    sender_name=m.get('sender_name', ''),
                    direction=m.get('direction', 0),
                    msg_type=m.get('msg_type', 0),
                    subtype=m.get('subtype', 0),
                    kind=m.get('kind', 'text'),
                    text=m.get('text', ''),
                    reply_seq=m.get('reply_seq', 0),
                )
            elif isinstance(m, (tuple, list)):
                # 兼容 ntqq_build 内部 16 元组格式
                # (msg_id, ts, day, hour, g_code, g_name, uid, sqq, name, direction, msg_type, subtype, kind, text, reply_seq, seq)
                nm = NormalizedMessage(
                    msg_id=m[0],
                    ts=m[1],
                    day=m[2],
                    hour=m[3],
                    group_code=m[4],
                    group_name=m[5],
                    sender_uid=m[6],
                    sender_qq=m[7],
                    sender_name=m[8],
                    direction=m[9],
                    msg_type=m[10],
                    subtype=m[11],
                    kind=m[12],
                    text=m[13],
                    reply_seq=m[14],
                    seq=m[15] if len(m) > 15 else 0,
                )
            else:
                continue

            # 标记机器人指令
            nm.is_bot_command = self.is_bot_command(nm.text)
            norm_list.append(nm)

        # 执行连续复读折叠
        folded_results: List[NormalizedMessage] = []
        i = 0
        n = len(norm_list)

        while i < n:
            curr = norm_list[i]
            # 仅对有文本且非转发/文件的普通消息进行复读折叠
            curr_txt = curr.text.strip()
            if not curr_txt or curr.kind in ('forward', 'file', 'system'):
                folded_results.append(curr)
                i += 1
                continue

            j = i + 1
            repeat_senders = [curr.sender_name or f"QQ{curr.sender_qq}"]
            while j < n:
                nxt = norm_list[j]
                # 必须同群且文本完全一致
                if nxt.group_code == curr.group_code and nxt.text.strip() == curr_txt:
                    sender_label = nxt.sender_name or f"QQ{nxt.sender_qq}"
                    if sender_label not in repeat_senders:
                        repeat_senders.append(sender_label)
                    j += 1
                else:
                    break

            repeat_count = j - i
            if repeat_count >= self.min_repeat_to_fold:
                curr.is_folded = True
                curr.repeat_count = repeat_count
                curr.repeated_by = repeat_senders
                curr.text = self.format_repeat_summary(repeat_senders, curr_txt)
                folded_results.append(curr)
            else:
                folded_results.append(curr)

            # 跳过已折叠的重复项
            i = j

        return folded_results

