#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分层滚动索引与微摘要树 (SummaryTree & GroupCursors)。

职责：
1. 模块 4.1 强一致性增量游标 (Incremental Cursor)：
   维护每个群的 last_processed_msg_seq 与 last_msg_time，支持毫秒级断点续传；
2. 模块 4.2 分层摘要树 (Hierarchical Rolling Summary)：
   - Level 1: Raw Messages（结构化原始消息）
   - Level 2: Micro-Digests（话题簇微摘要，包含核心实体、参与人、结论、起止 seq）
   - Level 3: Daily/Periodic Report（基于微摘要树聚合的最终可溯源报告）
"""

import json
import os
import re
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

_CUR_DIR = os.path.dirname(os.path.abspath(__file__))
if _CUR_DIR not in sys.path:
    sys.path.insert(0, _CUR_DIR)

from ntqq_segmenter import TopicCluster

_URL_RE = re.compile(r'https?://[^\s<>"\'\])]+')


def update_group_cursor(con: sqlite3.Connection, group_code: int, max_seq: int, max_ts: int, commit: bool = True) -> None:
    """更新单个群的增量游标（强一致幂等更新）。"""
    now = int(time.time())
    con.execute("""
    INSERT INTO group_cursors (group_code, last_processed_msg_seq, last_msg_time, updated_at)
    VALUES (?, ?, ?, ?)
    ON CONFLICT(group_code) DO UPDATE SET
        last_processed_msg_seq = MAX(last_processed_msg_seq, excluded.last_processed_msg_seq),
        last_msg_time = MAX(last_msg_time, excluded.last_msg_time),
        updated_at = excluded.updated_at
    """, (group_code, max_seq, max_ts, now))
    if commit:
        con.commit()


def get_group_cursor(con: sqlite3.Connection, group_code: int) -> Tuple[int, int]:
    """获取指定群的增量游标 (last_processed_msg_seq, last_msg_time)；未找到返回 (0, 0)。"""
    row = con.execute(
        "SELECT last_processed_msg_seq, last_msg_time FROM group_cursors WHERE group_code=?",
        (group_code,)
    ).fetchone()
    if row:
        return int(row[0] or 0), int(row[1] or 0)
    return 0, 0


def extract_cluster_title_and_summary(cluster: TopicCluster) -> Tuple[str, str, List[str]]:
    """纯本地快速抽取话题簇的核心标题、精炼微摘要与关键实体（零 LLM 开销）。"""
    msgs = cluster.messages
    if not msgs:
        return "闲聊话题", "无详细内容", []

    texts = [(m.get("text") or "").strip() for m in msgs if (m.get("text") or "").strip()]
    if not texts:
        return f"{cluster.first_sender} 发送的多媒体消息", "包含图片或文件分享", []

    # 1. 优先挑出长文或通知类消息作为标题线索
    title = ""
    for t in texts:
        clean_t = t.replace("\n", " ").strip()
        if any(kw in clean_t for kw in ("通知", "公告", "安排", "讨论", "关于", "项目", "问题", "建议")):
            title = clean_t[:35] + ("…" if len(clean_t) > 35 else "")
            break
    if not title:
        first_t = texts[0].replace("\n", " ").strip()
        title = first_t[:30] + ("…" if len(first_t) > 30 else "")

    # 2. 生成紧凑摘要：挑取前中后代表性发言
    summary_lines = []
    sample_msgs = msgs[:2] + (msgs[-2:] if len(msgs) > 2 else [])
    for m in sample_msgs:
        s_name = m.get("sender_name") or "群友"
        s_text = (m.get("text") or "").replace("\n", " ")
        if len(s_text) > 40:
            s_text = s_text[:40] + "…"
        summary_lines.append(f"{s_name}: {s_text}")
    summary = "；".join(summary_lines)

    # 3. 提取真实 URL / 域名实体与参与者
    entities = []
    seen_ent = set()
    for m in msgs:
        t = m.get("text") or ""
        for u in _URL_RE.findall(t):
            if u not in seen_ent:
                seen_ent.add(u)
                entities.append(u)
    for p in cluster.participants[:3]:
        if p and p not in seen_ent:
            seen_ent.add(p)
            entities.append(p)

    return title, summary, entities


def build_and_save_micro_digests(con: sqlite3.Connection, clusters: List[TopicCluster]) -> int:
    """将切片后的话题簇生成 Level 2 Micro-Digests 并批量写入 micro_digests 表。"""
    if not clusters:
        return 0

    now = int(time.time())
    records = []
    max_by_group: Dict[int, Tuple[int, int]] = {}
    for c in clusters:
        title, summary, entities = extract_cluster_title_and_summary(c)
        records.append((
            c.cluster_id,
            c.group_code,
            c.start_seq,
            c.end_seq,
            c.start_ts,
            c.end_ts,
            title,
            summary,
            json.dumps(entities, ensure_ascii=False),
            json.dumps(c.participants, ensure_ascii=False),
            c.message_count,
            now
        ))
        if c.end_seq and c.end_ts:
            prev_seq, prev_ts = max_by_group.get(c.group_code, (0, 0))
            max_by_group[c.group_code] = (max(prev_seq, c.end_seq), max(prev_ts, c.end_ts))

    con.executemany("""
    INSERT OR REPLACE INTO micro_digests (
        cluster_id, group_code, start_seq, end_seq, start_ts, end_ts,
        title, summary, entities, participants, msg_count, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, records)

    # 每群聚合一次更新游标，避免 N+1 提交
    for gc, (mseq, mts) in max_by_group.items():
        update_group_cursor(con, gc, mseq, mts, commit=False)

    con.commit()
    return len(records)


def load_micro_digests(con: sqlite3.Connection, group_code: int, since_ts: int = 0) -> List[Dict[str, Any]]:
    """查询指定群在时间窗口内的微摘要列表（不修改调用方连接的 row_factory）。"""
    cur = con.execute("""
    SELECT cluster_id, group_code, start_seq, end_seq, start_ts, end_ts,
           title, summary, entities, participants, msg_count
    FROM micro_digests
    WHERE group_code=? AND start_ts>=?
    ORDER BY start_ts DESC
    """, (group_code, since_ts))
    rows = cur.fetchall()
    results = []
    for r in rows:
        if hasattr(r, "keys"):
            cid, gc, sseq, eseq, sts, ets, title, summary, ent, part, mcnt = (
                r["cluster_id"], r["group_code"], r["start_seq"], r["end_seq"],
                r["start_ts"], r["end_ts"], r["title"], r["summary"],
                r["entities"], r["participants"], r["msg_count"]
            )
        else:
            cid, gc, sseq, eseq, sts, ets, title, summary, ent, part, mcnt = r[:11]
        results.append({
            "cluster_id": cid,
            "group_code": gc,
            "start_seq": sseq,
            "end_seq": eseq,
            "start_ts": sts,
            "end_ts": ets,
            "title": title,
            "summary": summary,
            "entities": json.loads(ent) if ent else [],
            "participants": json.loads(part) if part else [],
            "msg_count": mcnt,
        })
    return results

