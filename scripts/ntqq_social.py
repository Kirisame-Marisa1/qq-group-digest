"""
ntqq_social.py - 群聊社交互动关系网络分析器
分析群聊中成员间的 @ 提及、引用回复与短时轮转对话，构建群内社交拓扑关系有向图。
"""

import re
from collections import Counter

MENTION_RE = re.compile(r'@([^\s@|]{1,16})')
QUOTE_RE = re.compile(r'\[引用\s+([^\s:]+):')


def build_social_graph(messages: list[dict], top_n: int = 30) -> dict:
    """
    根据消息流构建群成员社交互动图谱。
    """
    speaker_counts = Counter()
    edges_map = {}  # (source, target) -> weight
    last_speaker = None
    last_ts = 0

    valid_speakers = set()

    for msg in messages:
        sender = msg.get("sender_name")
        if not sender or sender in ("系统", "未知", "系统消息"):
            continue
        speaker_counts[sender] += 1
        valid_speakers.add(sender)

    # 仅保留发言最多的前 top_n 位核心成员进行图谱连线，避免过大爆炸
    top_speakers = {s for s, _ in speaker_counts.most_common(top_n)}

    for msg in messages:
        sender = msg.get("sender_name")
        if not sender or sender not in top_speakers:
            continue
        text = msg.get("text") or ""
        ts = int(msg.get("timestamp") or 0)
        kind = msg.get("kind") or "text"

        # 1. 显式 @ 提及 (权重 3)
        mentions = MENTION_RE.findall(text)
        for m in mentions:
            # 找到匹配的群友
            target = next((s for s in top_speakers if s == m or (len(m) >= 2 and m in s)), None)
            if target and target != sender:
                edges_map[(sender, target)] = edges_map.get((sender, target), 0) + 3

        # 2. 引用回复 (权重 2)
        quote_match = QUOTE_RE.search(text)
        if quote_match:
            q_target = quote_match.group(1).strip()
            target = next((s for s in top_speakers if s == q_target or (len(q_target) >= 2 and q_target in s)), None)
            if target and target != sender:
                edges_map[(sender, target)] = edges_map.get((sender, target), 0) + 2

        # 3. 对话轮转与上下文接梗 (60秒内不同人连续发言，权重 1)
        if last_speaker and last_speaker != sender and last_speaker in top_speakers:
            if 0 < (ts - last_ts) <= 60:
                edges_map[(sender, last_speaker)] = edges_map.get((sender, last_speaker), 0) + 1

        last_speaker = sender
        last_ts = ts

    # 节点列表与指标
    in_degree = Counter()
    out_degree = Counter()
    for (src, tgt), w in edges_map.items():
        out_degree[src] += w
        in_degree[tgt] += w

    nodes = []
    for s in top_speakers:
        nodes.append({
            "id": s,
            "label": s,
            "message_count": speaker_counts[s],
            "in_degree": in_degree[s],
            "out_degree": out_degree[s],
            "total_degree": in_degree[s] + out_degree[s],
        })
    nodes.sort(key=lambda x: (x["total_degree"], x["message_count"]), reverse=True)

    # 边列表
    edges = []
    for (src, tgt), w in edges_map.items():
        edges.append({
            "source": src,
            "target": tgt,
            "weight": w,
        })
    edges.sort(key=lambda x: x["weight"], reverse=True)

    # 最强互动双人搭档 (Dyads)
    dyads_map = {}
    for (src, tgt), w in edges_map.items():
        pair_key = tuple(sorted([src, tgt]))
        dyads_map[pair_key] = dyads_map.get(pair_key, 0) + w

    top_dyads = []
    for (p1, p2), w in sorted(dyads_map.items(), key=lambda x: x[1], reverse=True)[:5]:
        top_dyads.append({
            "pair": f"{p1} ⇄ {p2}",
            "weight": w,
            "member_a": p1,
            "member_b": p2,
        })

    return {
        "nodes": nodes,
        "edges": edges,
        "top_dyads": top_dyads,
        "total_interactions": sum(edges_map.values()),
    }


def format_social_graph_markdown(graph: dict) -> str:
    """将社交互动网络渲染为 Markdown 章节。"""
    nodes = graph.get("nodes", [])
    top_dyads = graph.get("top_dyads", [])
    if not nodes:
        return ""

    lines = ["## 🕸️ 群聊社交互动网络分析\n"]
    lines.append(f"> 识别出核心互动节点 **{len(nodes)} 人**，总交互强度指数 **{graph.get('total_interactions', 0)}**。\n")

    if top_dyads:
        lines.append("### 🤝 最亲密互动搭子 Top 5\n")
        for idx, d in enumerate(top_dyads, 1):
            lines.append(f"{idx}. **{d['pair']}** `(互动热度: {d['weight']})`")
        lines.append("")

    lines.append("### 🌟 社交中心活跃榜\n")
    lines.append("| 群友 | 发言数 | 被提及/回复热度 | 主动互动度 | 综合社交指数 |")
    lines.append("|---|---|---|---|---|")
    for n in nodes[:10]:
        lines.append(f"| **{n['label']}** | {n['message_count']} | {n['in_degree']} | {n['out_degree']} | {n['total_degree']} |")
    lines.append("")

    return "\n".join(lines)
