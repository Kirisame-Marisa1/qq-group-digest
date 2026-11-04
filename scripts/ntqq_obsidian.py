"""
ntqq_obsidian.py - Obsidian / 个人第二大脑知识库导出适配器
将群聊分析报告转化为符合 Obsidian 语法规范的双链笔记：
1. 规范 YAML Frontmatter 元数据
2. 现代 Obsidian Callouts（[!summary], [!todo], [!quote]）
3. 自动构建双向链接 [[关键词]] 与 [[群友]]
4. 兼容 obsidian-chat-view-qq 原生聊天气泡代码块 (```chat-qq)
"""

import os
import re
from datetime import datetime


def sanitize_filename(name: str) -> str:
    """清理文件名中 Windows/Linux 系统的非法字符。"""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(' ._') or 'unnamed'


def generate_obsidian_markdown(data: dict, options: dict = None) -> str:
    """
    根据聚合报告数据，生成完整的 Obsidian 格式 Markdown 笔记正文。
    """
    options = options or {}
    title = data.get("title") or f"QQ群聊简报 - {data.get('group_name', '未命名群')}"
    group_id = str(data.get("group_id") or "0")
    group_name = data.get("group_name") or f"群_{group_id}"
    date_str = data.get("date_str") or datetime.now().strftime("%Y-%m-%d")
    period_str = data.get("period_str") or date_str
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
        f"# {title}",
        "",
        f"> 📅 **统计周期**：`{period_str}` ｜ 👥 **群成员**：`{act_mems} 人活跃` ｜ 💬 **消息量**：`{tot_msgs} 条`",
        "",
    ]

    # --- 1. 核心速览与关键词（带双向链接） ---
    if summary or keywords:
        lines.append("> [!summary] 📋 今日要点速览")
        if summary:
            lines.append(f"> {summary}")
        if keywords:
            kw_links = " ".join(f"[[{kw}]]" for kw in keywords)
            lines.append(f"> \n> **相关主题标签**：{kw_links}")
        lines.append("")

    # --- 2. 待办事项 Callout ---
    if actions:
        lines.append("> [!todo] 📌 待办行动清单")
        for a in actions:
            assignee = a.get("assignee") or "成员"
            task = a.get("task") or ""
            ddl = f" (⏳ 截止: {a['deadline']})" if a.get("deadline") and a["deadline"] != "未明确" else ""
            lines.append(f"> - [ ] [[{assignee}]] : {task}{ddl}")
        lines.append("")

    # --- 3. 决议与公告 Callout ---
    if decisions:
        lines.append("> [!tip] 📢 决议与重要公告")
        for d in decisions:
            announcer = d.get("announcer") or "群公告"
            dec = d.get("decision") or ""
            lines.append(f"> - 🎯 **{dec}** (由 [[{announcer}]] 确认)")
        lines.append("")

    # --- 4. 高光金句 Callout ---
    if quotes:
        lines.append("> [!quote] 💬 高光金句与锐评")
        for q in quotes:
            sender = q.get("sender") or "群友"
            text = q.get("text") or ""
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
        lines.append("```chat-qq")
        for d in dialogues:
            sender = d.get("sender") or "未知"
            t_str = f" ({d['time']})" if d.get("time") else ""
            txt = (d.get("text") or "").replace("\n", " ")
            lines.append(f"{sender}{t_str}: {txt}")
        lines.append("```")
        lines.append("")

    return "\n".join(lines)


def export_to_obsidian(data: dict, output_dir: str, options: dict = None) -> str:
    """
    将报告数据导出为 Obsidian Vault 笔记文件并返回写入的绝对路径。
    """
    os.makedirs(output_dir, exist_ok=True)
    date_str = data.get("date_str") or datetime.now().strftime("%Y-%m-%d")
    group_id = str(data.get("group_id") or "0")
    group_name = sanitize_filename(data.get("group_name") or f"群_{group_id}")
    
    filename = f"{date_str}-{group_name}-{group_id}.md"
    file_path = os.path.join(output_dir, filename)

    content = generate_obsidian_markdown(data, options)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)

    return os.path.abspath(file_path)
