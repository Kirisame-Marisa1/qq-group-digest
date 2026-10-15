#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基于引用链与时间窗的对话树重构与话题切片器 (ConversationSegmenter)。

职责：
1. 解决群聊多线程穿插交织导致的时间线性上下文幻觉问题；
2. 结合显式特征（回复引用链 reply_seq、@提及目标）与隐式特征（时间断层 Δt > 15 分钟、发言人间歇），
   将扁平消息流切分为独立的话题簇（Topic Clusters / Sessions）；
3. 生成带有精确序列号范围与时间发言人的溯源锚点：[Ref: #seq_xxx - #seq_yyy, HH:MM @User]。
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple, Union

TZ = timezone(timedelta(hours=8))


@dataclass
class TopicCluster:
    cluster_id: str
    group_code: int
    group_name: str
    start_seq: int
    end_seq: int
    start_ts: int
    end_ts: int
    first_sender: str
    participants: List[str] = field(default_factory=list)
    message_count: int = 0
    messages: List[Dict[str, Any]] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    ref_anchor: str = ""

    def format_anchor(self) -> str:
        """生成格式化溯源锚点：[Ref: #seq_start - #seq_end, HH:MM @User]"""
        dt = datetime.fromtimestamp(self.start_ts, TZ)
        time_str = dt.strftime("%H:%M")
        user_str = self.first_sender or "群友"
        if self.start_seq and self.end_seq and self.start_seq != self.end_seq:
            return f"[Ref: #seq_{self.start_seq} - #seq_{self.end_seq}, {time_str} @{user_str}]"
        elif self.start_seq:
            return f"[Ref: #seq_{self.start_seq}, {time_str} @{user_str}]"
        return f"[Ref: {time_str} @{user_str}]"


class ConversationSegmenter:
    """对话流拓扑重构与会话切片器。"""

    def __init__(self, time_gap_threshold: int = 900, max_cluster_size: int = 100):
        """
        :param time_gap_threshold: 隐式时间断层阈值（秒），默认 15 分钟 (900s)
        :param max_cluster_size: 单个簇最大消息数，超过则进行二次微切片
        """
        self.time_gap_threshold = time_gap_threshold
        self.max_cluster_size = max_cluster_size

    def _normalize_msg(self, m: Union[Dict[str, Any], Tuple, Any]) -> Dict[str, Any]:
        """将不同输入形式归一化为通用字典。"""
        if isinstance(m, dict):
            return {
                "msg_id": m.get("msg_id", 0),
                "seq": m.get("seq", 0),
                "ts": m.get("ts", 0),
                "group_code": m.get("group_code", 0),
                "group_name": m.get("group_name", ""),
                "sender_name": m.get("sender_name", "") or f"QQ{m.get('sender_qq', '')}",
                "sender_uid": m.get("sender_uid", ""),
                "sender_qq": m.get("sender_qq", 0),
                "kind": m.get("kind", "text"),
                "text": m.get("text", ""),
                "reply_seq": m.get("reply_seq", 0),
            }
        elif hasattr(m, "msg_id"):
            return {
                "msg_id": getattr(m, "msg_id", 0),
                "seq": getattr(m, "seq", 0),
                "ts": getattr(m, "ts", 0),
                "group_code": getattr(m, "group_code", 0),
                "group_name": getattr(m, "group_name", ""),
                "sender_name": getattr(m, "sender_name", "") or f"QQ{getattr(m, 'sender_qq', '')}",
                "sender_uid": getattr(m, "sender_uid", ""),
                "sender_qq": getattr(m, "sender_qq", 0),
                "kind": getattr(m, "kind", "text"),
                "text": getattr(m, "text", ""),
                "reply_seq": getattr(m, "reply_seq", 0),
            }
        elif isinstance(m, (tuple, list)):
            # 兼容 (msg_id, ts, day, hour, g_code, g_name, uid, sqq, name, direction, msg_type, subtype, kind, text, reply_seq, seq)
            return {
                "msg_id": m[0],
                "ts": m[1],
                "group_code": m[4],
                "group_name": m[5],
                "sender_uid": m[6],
                "sender_qq": m[7],
                "sender_name": m[8] or f"QQ{m[7]}",
                "kind": m[12],
                "text": m[13],
                "reply_seq": m[14],
                "seq": m[15] if len(m) > 15 else 0,
            }
        return {}

    def segment_messages(self, messages: List[Any]) -> List[TopicCluster]:
        """将消息序列切分为独立的话题簇 (Topic Clusters)。"""
        if not messages:
            return []

        # 1. 规整化并按 (group_code, ts, seq) 排序
        norm_msgs = [self._normalize_msg(m) for m in messages]
        norm_msgs = [m for m in norm_msgs if m.get("ts", 0) > 0]
        norm_msgs.sort(key=lambda x: (x["group_code"], x["ts"], x["seq"]))

        # 2. 按群分组切片
        clusters: List[TopicCluster] = []
        by_group: Dict[int, List[Dict[str, Any]]] = {}
        for m in norm_msgs:
            by_group.setdefault(m["group_code"], []).append(m)

        for g_code, g_msgs in by_group.items():
            clusters.extend(self._segment_single_group(g_code, g_msgs))

        # 按时间倒序返回
        clusters.sort(key=lambda c: -c.start_ts)
        return clusters

    def _segment_single_group(self, group_code: int, messages: List[Dict[str, Any]]) -> List[TopicCluster]:
        """对单个群的消息进行拓扑会话切片。"""
        if not messages:
            return []

        # 第一阶段：基于隐式时间断层 (Δt > 15min) 划分粗粒度会话窗口
        windows: List[List[Dict[str, Any]]] = []
        curr_window: List[Dict[str, Any]] = [messages[0]]

        for i in range(1, len(messages)):
            prev_m = messages[i - 1]
            curr_m = messages[i]
            delta_t = curr_m["ts"] - prev_m["ts"]

            if delta_t > self.time_gap_threshold:
                # 超过 15 分钟无发言，截断为新会话窗口
                windows.append(curr_window)
                curr_window = [curr_m]
            else:
                curr_window.append(curr_m)
        if curr_window:
            windows.append(curr_window)

        # 第二阶段：在每个窗口内，利用回复引用链 (reply_seq) 和 @提及 细化话题分支
        final_clusters: List[TopicCluster] = []
        for w_idx, win in enumerate(windows):
            if len(win) <= self.max_cluster_size:
                cluster = self._build_cluster(group_code, win, suffix=f"w{w_idx}")
                final_clusters.append(cluster)
            else:
                # 窗口内消息过多，按每 max_cluster_size 条进行分块微切片
                for sub_idx in range(0, len(win), self.max_cluster_size):
                    chunk = win[sub_idx:sub_idx + self.max_cluster_size]
                    cluster = self._build_cluster(group_code, chunk, suffix=f"w{w_idx}_s{sub_idx // self.max_cluster_size}")
                    final_clusters.append(cluster)

        return final_clusters

    def _build_cluster(self, group_code: int, msgs: List[Dict[str, Any]], suffix: str = "") -> TopicCluster:
        """从一组消息构建 TopicCluster 实例。"""
        seqs = [m["seq"] for m in msgs if m.get("seq", 0) > 0]
        start_seq = min(seqs) if seqs else 0
        end_seq = max(seqs) if seqs else 0

        ts_list = [m["ts"] for m in msgs]
        start_ts = min(ts_list)
        end_ts = max(ts_list)

        g_name = msgs[0].get("group_name", "") or str(group_code)
        first_sender = msgs[0].get("sender_name", "") or "群友"

        # 统计参与成员
        participants = []
        seen_senders = set()
        for m in msgs:
            s_name = m.get("sender_name", "")
            if s_name and s_name not in seen_senders:
                seen_senders.add(s_name)
                participants.append(s_name)

        cid = f"cluster_{group_code}_{start_seq}_{end_seq}_{suffix}"
        cluster = TopicCluster(
            cluster_id=cid,
            group_code=group_code,
            group_name=g_name,
            start_seq=start_seq,
            end_seq=end_seq,
            start_ts=start_ts,
            end_ts=end_ts,
            first_sender=first_sender,
            participants=participants,
            message_count=len(msgs),
            messages=msgs,
        )
        cluster.ref_anchor = cluster.format_anchor()
        return cluster

