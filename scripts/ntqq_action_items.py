"""
ntqq_action_items.py - 待办事项与决议雷达提取器
从群聊消息中自动抽取付诸行动的任务（Action Items、责任人、截止时间）与达成的共识/决议（Decisions）。
兼容 dict 与 sqlite3.Row。
"""

import re
from datetime import datetime

# 责任人匹配
MENTION_RE = re.compile(r'@([^\s@|]{1,16})')
GROUP_ASSIGNEES = ('大家', '各位', '所有人', '全体成员', '全员', '同学们', '组员')

# 截止时间匹配
DEADLINE_RE = re.compile(
    r'((?:今天|明天|后天|大后天|本周|这周|下周|周[一二三四五六日天]|星期[一二三四五六日天]|\d{1,2}月\d{1,2}[日号]?)(?:上午|下午|晚上|中午|下班|底|\d{1,2}点|\d{1,2}:\d{2})*前|'
    r'今晚前?|明早前?|明晚前?|下班前|月底前?|周[一二三四五六日天]前|尽快|下周[一二三四五六日天]前)'
)

# 强行动动词
ACTION_VERB_RE = re.compile(
    r'(?:请|务必|记得|负责|需要|需|麻烦|安排)?\s*'
    r'(提交|填写|统计|核对|跟进|处理|修改|确认|发布|上线|汇报|反馈|准备|汇总|报备|组织|参加|完成|录入|复查|自查)'
    r'([^，。！？；\n]{2,30})'
)

# 弱干扰词与已完成夸奖（避免日常闲聊与事后夸奖误报）
CASUAL_IGNORE_RE = re.compile(
    r'(我记得|不记得|还记得|记得挺|记得好|记得有|记得很|吃|玩|喝|天气|睡|哈哈|笑死|好玩|好看|'
    r'完成得很棒|做得好|干得漂亮|已经完成|已经提交|已经发了|都完成了|都提交了|已提交|搞定了|做完了)'
)

# 决议与共识关键词
DECISION_RE = re.compile(
    r'(?:经讨论[，,]?|大家讨论[，,]?|会议讨论[，,]?)?(?:方案确定(?:为|采用)?|确定采用|统一使用|达成一致|最终决定|'
    r'定在|定于|决定是|公告[：:]|通知[：:]|会议结论[：:])'
    r'([^。！？\n]{3,60})'
)


def _get_val(row, keys, default=None):
    """同时兼容 dict 和 sqlite3.Row，并支持字段别名回退。"""
    if isinstance(keys, str):
        keys = [keys]
    for k in keys:
        if isinstance(row, dict):
            if k in row and row[k] is not None:
                return row[k]
        else:
            try:
                val = row[k]
                if val is not None:
                    return val
            except (KeyError, IndexError, TypeError):
                pass
    return default


def extract_action_items_and_decisions(messages: list) -> dict:
    """
    分析消息流，提取具有明确行动意向的待办事项与决议。
    兼容 dict 与 sqlite3.Row。
    """
    actions = []
    decisions = []
    seen_action_signatures = set()
    seen_decision_signatures = set()

    for msg in messages:
        text = (_get_val(msg, ["text", "msg_text"], "") or "").strip()
        if not text or len(text) < 4:
            continue

        ts = int(_get_val(msg, ["ts", "timestamp"], 0) or 0)
        seq = int(_get_val(msg, ["seq", "msg_seq"], 0) or 0)
        sender_name = _get_val(msg, ["sender_name", "name", "disp_name"], "未知群友")
        time_str = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else ""

        # --- 1. 决议 / 共识抽取 ---
        d_match = DECISION_RE.search(text)
        if d_match:
            decision_body = d_match.group(0).strip()
            sig = re.sub(r'\s+', '', decision_body)
            if sig not in seen_decision_signatures:
                seen_decision_signatures.add(sig)
                decisions.append({
                    "decision": decision_body,
                    "announcer": sender_name,
                    "time_str": time_str,
                    "timestamp": ts,
                    "seq": seq,
                })

        # --- 2. 待办事项抽取 ---
        # 排除典型闲聊与已完成夸奖
        if CASUAL_IGNORE_RE.search(text) and not any(k in text for k in ("请于", "务必", "截止", "记得在", "前提交", "前完成", "前填写")):
            continue

        deadline_match = DEADLINE_RE.search(text)
        action_match = ACTION_VERB_RE.search(text)

        mentions = MENTION_RE.findall(text)
        assignee = None
        if mentions:
            assignee = "@" + " @".join(mentions[:3])
        else:
            for ga in GROUP_ASSIGNEES:
                if ga in text:
                    assignee = ga
                    break

        is_action = False
        task_desc = ""
        deadline = deadline_match.group(1) if deadline_match else "未明确"

        if action_match:
            verb = action_match.group(1)
            obj = action_match.group(2).strip()
            task_desc = verb + obj
            # 排除已完成的过去时描述（如 "完成得很棒"）
            if any(p in task_desc for p in ("完成得很棒", "做得很好", "已搞定")):
                is_action = False
            elif assignee or deadline != "未明确":
                is_action = True
        elif deadline != "未明确" and ("提交" in text or "完成" in text or "填写" in text or "发我" in text or "发群里" in text):
            task_desc = text[:40]
            is_action = True

        if is_action:
            assignee = assignee or "群内成员"
            task_desc = re.sub(r'^[请于在把将]+\s*', '', task_desc)
            act_sig = f"{assignee}_{deadline}_{task_desc[:12]}"
            if act_sig not in seen_action_signatures:
                seen_action_signatures.add(act_sig)
                actions.append({
                    "assignee": assignee,
                    "task": task_desc,
                    "deadline": deadline,
                    "sender_name": sender_name,
                    "time_str": time_str,
                    "timestamp": ts,
                    "seq": seq,
                    "raw_text": text[:120],
                })

    actions.sort(key=lambda x: x["timestamp"])
    decisions.sort(key=lambda x: x["timestamp"])

    return {
        "actions": actions,
        "decisions": decisions,
    }


def format_action_items_markdown(data: dict) -> str:
    """将待办事项与决议格式化为 Markdown 报告板块"""
    actions = data.get("actions", [])
    decisions = data.get("decisions", [])

    if not actions and not decisions:
        return ""

    lines = ["## 📌 待办事项与决议雷达\n"]

    if decisions:
        lines.append(f"### 📢 决议与重要公告 ({len(decisions)})\n")
        for d in decisions:
            time_part = f"`[{d['time_str']}]` " if d.get("time_str") else ""
            seq_part = f" `[Ref: #{d['seq']}]`" if d.get("seq") else ""
            lines.append(f"- 🎯 {time_part}**{d['decision']}** (发布人: {d['announcer']}){seq_part}")
        lines.append("")

    if actions:
        lines.append(f"### 📋 行动清单与截止时间 ({len(actions)})\n")
        for a in actions:
            time_part = f"`[{a['time_str']}]` " if a.get("time_str") else ""
            seq_part = f" `[Ref: #{a['seq']}]`" if a.get("seq") else ""
            ddl_part = f" ⏳ **截止: {a['deadline']}**" if a.get("deadline") and a["deadline"] != "未明确" else ""
            lines.append(f"- [ ] {time_part}**{a['assignee']}** : {a['task']}{ddl_part} (由 {a['sender_name']} 布置){seq_part}")
        lines.append("")

    return "\n".join(lines)
