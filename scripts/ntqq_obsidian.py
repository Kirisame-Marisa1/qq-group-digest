"""
ntqq_obsidian.py - Obsidian / 个人第二大脑知识库导出适配器
将群聊分析报告转化为符合 Obsidian 语法规范的双链笔记：
1. 规范 YAML Frontmatter 元数据（单引号转义防御）
2. 现代 Obsidian Callouts（[!summary], [!todo], [!quote]）
3. 严格清洗双向链接 [[关键词]] 与 [[群友]] 避免字符冲突
4. 兼容 obsidian-chat-view-qq 原生聊天气泡代码块 (```chat-qq)
"""

import os
import re
from datetime import datetime


_WIN_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_filename(name: str, max_len: int = 120) -> str:
    """清理文件名中 Windows/Linux 系统的非法字符、保留设备名与超长文件名。"""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(name or '')).strip(' ._')
    if not cleaned:
        cleaned = 'unnamed'
    if cleaned.upper() in _WIN_RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned[:max_len].rstrip(' ._') or 'unnamed'


def sanitize_wikilink(text: str) -> str:
    """清理 Wikilink 中的特殊保留字符 [ ] | # ^，避免破坏 Obsidian 语法。"""
    return re.sub(r'[\[\]|#^]', '', str(text)).strip()


def _yaml_escape(val: str) -> str:
    """转义 YAML 单引号标量中的单引号并折叠换行符，防止 Frontmatter 注入。"""
    return re.sub(r'[\r\n]+', ' ', str(val)).replace("'", "''")


def _callout_lines(text: str, prefix: str = "> ") -> list[str]:
    """将多行文本每行均加上 Obsidian Callout 前缀 `> `。"""
    return [f"{prefix}{line}" for line in str(text or "").splitlines() or [""]]


def generate_obsidian_markdown(data: dict, options: dict = None) -> str:
    """
    根据聚合报告数据，生成完整的 Obsidian 格式 Markdown 笔记正文。
    """
    options = options or {}
    raw_title = data.get("title") or f"QQ群聊简报 - {data.get('group_name', '未命名群')}"
    title = _yaml_escape(raw_title)
    group_id = _yaml_escape(str(data.get("group_id") or "0"))
    group_name = _yaml_escape(data.get("group_name") or f"群_{group_id}")
    date_str = _yaml_escape(data.get("date_str") or datetime.now().strftime("%Y-%m-%d"))
    period_str = _yaml_escape(data.get("period_str") or date_str)
    tot_msgs = int(data.get("total_messages") or 0)
    act_mems = int(data.get("active_members") or 0)
    summary = (data.get("summary") or "").strip()
    keywords = data.get("keywords") or []
    actions = data.get("actions") or []
    decisions = data.get("decisions") or []
    quotes = data.get("quotes") or []
    dialogues = data.get("dialogues") or []
    links = data.get("links") or []

    lines = [
        "---",
        f"title: '{title}'",
        f"date: {date_str}",
        f"group_id: '{group_id}'",
        f"group_name: '{group_name}'",
        f"period: '{period_str}'",
        f"message_count: {tot_msgs}",
        f"active_members: {act_mems}",
        "tags:",
        "  - qq-digest",
        "  - group-chat",
        "  - obsidian-second-brain",
        "---",
        "",
        f"# {raw_title}",
        "",
        f"> 📅 **统计周期**：`{period_str}` ｜ 👥 **群成员**：`{act_mems} 人活跃` ｜ 💬 **消息量**：`{tot_msgs} 条`",
        "",
    ]

    # --- 1. 核心速览与关键词（带双向链接） ---
    if summary or keywords:
        lines.append("> [!summary] 📋 今日要点速览")
        if summary:
            lines.extend(_callout_lines(summary))
        if keywords:
            kw_links = " ".join(f"[[{sanitize_wikilink(kw)}]]" for kw in keywords if sanitize_wikilink(kw))
            lines.append(f"> \n> **相关主题标签**：{kw_links}")
        lines.append("")

    # --- 2. 待办事项 Callout ---
    if actions:
        lines.append("> [!todo] 📌 待办行动清单")
        for a in actions:
            raw_assignee = a.get("assignee") or "成员"
            assignee = sanitize_wikilink(raw_assignee) or "成员"
            task = re.sub(r'\s+', ' ', str(a.get("task") or "")).strip()
            ddl = f" (⏳ 截止: {a['deadline']})" if a.get("deadline") and a["deadline"] != "未明确" else ""
            lines.append(f"> - [ ] [[{assignee}]] : {task}{ddl}")
        lines.append("")

    # --- 3. 决议与公告 Callout ---
    if decisions:
        lines.append("> [!tip] 📢 决议与重要公告")
        for d in decisions:
            raw_ann = d.get("announcer") or "群公告"
            announcer = sanitize_wikilink(raw_ann) or "群公告"
            dec = re.sub(r'\s+', ' ', str(d.get("decision") or "")).strip()
            lines.append(f"> - 🎯 **{dec}** (由 [[{announcer}]] 确认)")
        lines.append("")

    # --- 4. 高光金句 Callout ---
    if quotes:
        lines.append("> [!quote] 💬 高光金句与锐评")
        for q in quotes:
            raw_spk = q.get("sender") or "群友"
            sender = sanitize_wikilink(raw_spk) or "群友"
            text = re.sub(r'\s+', ' ', str(q.get("text") or "")).strip()
            lines.append(f"> - [[{sender}]] : \"{text}\"")
        lines.append("")

    # --- 5. 沉淀资源合辑 ---
    if links:
        lines.append("## 📦 沉淀资源与链接")
        lines.append("")
        for l in links:
            url = l.get("clean_url") or l.get("url") or ""
            domain = l.get("domain") or "Link"
            ctx = f" — _{l['context']}_" if l.get("context") else ""
            lines.append(f"- [{domain}]({url}){ctx}")
        lines.append("")

    # --- 6. 原生聊天气泡代码块（obsidian-chat-view-qq 兼容） ---
    if dialogues:
        lines.append("## 💬 代表性时段对话")
        lines.append("")
        chat_lines = []
        for d in dialogues:
            sender = re.sub(r'[\r\n]+', ' ', str(d.get("sender") or "未知")).strip()
            t_str = f" ({d['time']})" if d.get("time") else ""
            txt = re.sub(r'[\r\n]+', ' ', str(d.get("text") or ""))
            chat_lines.append(f"{sender}{t_str}: {txt}")
        fence = "````" if any("```" in cl for cl in chat_lines) else "```"
        lines.append(f"{fence}chat-qq")
        lines.extend(chat_lines)
        lines.append(fence)
        lines.append("")

    return "\n".join(lines)


def export_to_obsidian(data: dict, output_dir: str, options: dict = None) -> str:
    """
    将报告数据导出为 Obsidian Vault 笔记文件并返回写入的绝对路径。
    """
    abs_out = os.path.abspath(output_dir)
    os.makedirs(abs_out, exist_ok=True)
    date_str = sanitize_filename(data.get("date_str") or datetime.now().strftime("%Y-%m-%d"), max_len=32)
    group_id = sanitize_filename(str(data.get("group_id") or "0"), max_len=32)
    group_name = sanitize_filename(data.get("group_name") or f"群_{group_id}", max_len=80)

    filename = f"{date_str}-{group_name}-{group_id}.md"
    abs_file = os.path.abspath(os.path.join(abs_out, filename))
    if os.path.commonpath([abs_out, abs_file]) != abs_out:
        raise ValueError(f"非法导出文件路径: {abs_file}")

    content = generate_obsidian_markdown(data, options)
    with open(abs_file, "w", encoding="utf-8") as f:
        f.write(content)

    return abs_file
