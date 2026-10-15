#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可溯源结构化报告生成器与数据契约 (Traceable Report Schema & Generator)。

职责：
1. 模块 5.1 引用溯源机制 (Citation & Grounding)：
   使用 Pydantic 强约束报告结构，强制每个关键总结要点附带精确消息锚点：
   `[Ref: #seq_xxx - #seq_yyy, HH:MM @User]`；
2. 模块 5.2 多维度标准化报告模板：
   - 🔥 今日热点话题 Top N（标题、参与核心成员、争议/结论、溯源锚点）
   - 📌 重要资源与链接归档（GitHub、技术文档、网盘链接、卡片分享）
   - 💬 金句与群友高光时刻（高回复互动、复读高光、重要通知）
   - 📊 群聊活跃度微画像（纯本地统计指标，零 LLM 消耗）
"""

import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

_CUR_DIR = os.path.dirname(os.path.abspath(__file__))
if _CUR_DIR not in sys.path:
    sys.path.insert(0, _CUR_DIR)

from pydantic import BaseModel, Field
from ntqq_segmenter import TopicCluster

TZ = timezone(timedelta(hours=8))


class HotTopicItem(BaseModel):
    title: str = Field(description="话题核心标题")
    participants: List[str] = Field(default_factory=list, description="参与讨论的核心群友")
    conclusion: str = Field(description="讨论核心结论或争议要点")
    ref_anchor: str = Field(description="溯源锚点，格式如 [Ref: #seq_1024 - #seq_1058, 14:20 @Alice]")
    msg_count: int = Field(default=0, description="话题包含消息数")


class ResourceItem(BaseModel):
    category: str = Field(description="资源类型，如 GitHub、技术文章、文档、网盘等")
    title: str = Field(description="资源名称或说明")
    url: str = Field(description="资源链接或标识")
    sender: str = Field(description="分享者昵称")
    ref_anchor: str = Field(description="溯源锚点")


class HighlightItem(BaseModel):
    sender: str = Field(description="发言人")
    quote: str = Field(description="金句或高光内容")
    interaction_info: str = Field(description="高光依据，如高回复数、多人复读、重点通知等")
    ref_anchor: str = Field(description="溯源锚点")


class ActivityProfile(BaseModel):
    total_messages: int = Field(default=0, description="总消息量")
    active_members: int = Field(default=0, description="活跃参与人数")
    hourly_distribution: Dict[int, int] = Field(default_factory=dict, description="24 小时活跃时段分布")
    top_chatters: List[Tuple[str, int]] = Field(default_factory=list, description="发言量前 N 名水群王")
    repeat_stats: List[Tuple[str, int]] = Field(default_factory=list, description="高频复读统计")


class StructuredDigestReport(BaseModel):
    data_cutoff: str = Field(description="数据截止时间声明")
    group_name: str = Field(description="群名称")
    group_code: int = Field(description="群号")
    hot_topics: List[HotTopicItem] = Field(default_factory=list, description="今日热点话题")
    resources: List[ResourceItem] = Field(default_factory=list, description="重要资源与链接归档")
    highlights: List[HighlightItem] = Field(default_factory=list, description="金句与高光时刻")
    activity_profile: Optional[ActivityProfile] = Field(default=None, description="群聊活跃度微画像")


# 正则匹配各类常见分享链接
RE_GITHUB = re.compile(r'https?://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+')
RE_DOCS = re.compile(r'https?://(?:docs\.qq\.com|feishu\.cn|yuque\.com|notion\.so)/\S+')
RE_PAN = re.compile(r'https?://(?:pan\.baidu\.com|aliyundrive\.com|123pan\.com|lanzou\w*\.com)/\S+')
RE_URL = re.compile(r'https?://[^\s<>"\'{}|\\^`]+')


def _format_msg_anchor(msg: Dict[str, Any]) -> str:
    ts = msg.get("ts", 0)
    dt = datetime.fromtimestamp(ts, TZ) if ts else datetime.now(TZ)
    time_str = dt.strftime("%H:%M")
    who = msg.get("sender_name", "") or f"QQ{msg.get('sender_qq', '')}"
    seq = msg.get("seq", 0)
    if seq:
        return f"[Ref: #seq_{seq}, {time_str} @{who}]"
    return f"[Ref: {time_str} @{who}]"


def build_structured_report(
    rows: List[Dict[str, Any]],
    clusters: List[TopicCluster],
    group_name: str,
    group_code: int,
    data_cutoff: str = ""
) -> StructuredDigestReport:
    """基于清洗与切片后的数据，生成符合 4 大标准化板块的结构化报告对象。"""
    if not data_cutoff:
        max_ts = max((r.get("ts", 0) for r in rows), default=int(datetime.now().timestamp()))
        data_cutoff = datetime.fromtimestamp(max_ts, TZ).strftime("%Y-%m-%d %H:%M")

    # ── 板块一：今日热点话题 Top N ──────────────────────────────────────
    hot_topics: List[HotTopicItem] = []
    # 过滤消息数 >= 2 的有效话题簇，按热度（消息数）降序
    valid_clusters = [c for c in clusters if c.message_count >= 2]
    valid_clusters.sort(key=lambda c: -c.message_count)

    for c in valid_clusters[:8]:
        # 提取核心议题与结论
        title = ""
        conclusion = ""
        msgs = c.messages

        for m in msgs:
            txt = m.get("text", "").replace("\n", " ").strip()
            if any(k in txt for k in ("通知", "讨论", "问题", "建议", "关于", "总结", "决定")):
                title = txt[:40]
                break
        if not title and msgs:
            title = msgs[0].get("text", "").replace("\n", " ").strip()[:35]

        # 结论抽取
        sample_conclusions = []
        for m in msgs[-3:]:
            st = m.get("text", "").replace("\n", " ").strip()
            if st:
                sample_conclusions.append(f"{m.get('sender_name', '群友')}: {st[:45]}")
        conclusion = "；".join(sample_conclusions) if sample_conclusions else "多位成员参与了简短探讨"

        hot_topics.append(HotTopicItem(
            title=title or "日常深度讨论",
            participants=c.participants[:5],
            conclusion=conclusion,
            ref_anchor=c.ref_anchor,
            msg_count=c.message_count
        ))

    # ── 板块二：重要资源与链接归档 ────────────────────────────────────
    resources: List[ResourceItem] = []
    seen_urls = set()

    for r in rows:
        t = r.get("text", "")
        sender = r.get("sender_name", "") or f"QQ{r.get('sender_qq', '')}"
        anchor = _format_msg_anchor(r)

        # 检查 GitHub
        for m in RE_GITHUB.findall(t):
            if m not in seen_urls:
                seen_urls.add(m)
                repo_name = m.replace("https://github.com/", "")
                resources.append(ResourceItem(
                    category="GitHub 仓库",
                    title=f"开源项目: {repo_name}",
                    url=m,
                    sender=sender,
                    ref_anchor=anchor
                ))

        # 检查在线文档
        for m in RE_DOCS.findall(t):
            if m not in seen_urls:
                seen_urls.add(m)
                resources.append(ResourceItem(
                    category="在线文档",
                    title="共享协同文档",
                    url=m,
                    sender=sender,
                    ref_anchor=anchor
                ))

        # 检查网盘分享
        for m in RE_PAN.findall(t):
            if m not in seen_urls:
                seen_urls.add(m)
                resources.append(ResourceItem(
                    category="网盘资源",
                    title="文件下载分享",
                    url=m,
                    sender=sender,
                    ref_anchor=anchor
                ))

        # 检查分享卡片
        if r.get("kind") == "card" or "[分享卡片:" in t:
            m_card = re.search(r'\[分享卡片:\s*([^\]]+)\](?:\s*\((https?://\S+)\))?', t)
            if m_card:
                ctitle = m_card.group(1).strip()
                curl = m_card.group(2) or ""
                if curl not in seen_urls:
                    seen_urls.add(curl)
                    resources.append(ResourceItem(
                        category="应用卡片",
                        title=ctitle,
                        url=curl or "(内置小程序/富文本分享)",
                        sender=sender,
                        ref_anchor=anchor
                    ))

    # ── 板块三：金句与群友高光时刻 ────────────────────────────────────
    highlights: List[HighlightItem] = []

    # 1. 统计被引用回复次数最多的消息 (High Quotes)
    quoted_counts = Counter(r.get("reply_seq", 0) for r in rows if r.get("reply_seq", 0) > 0)
    for reply_seq, count in quoted_counts.most_common(3):
        if count >= 1:
            # 找到被引用的原始消息
            orig_msg = next((r for r in rows if r.get("seq", 0) == reply_seq), None)
            if orig_msg:
                highlights.append(HighlightItem(
                    sender=orig_msg.get("sender_name", ""),
                    quote=orig_msg.get("text", "").replace("\n", " ")[:120],
                    interaction_info=f"引发热烈反响，被引用回复 {count} 次",
                    ref_anchor=_format_msg_anchor(orig_msg)
                ))

    # 2. 统计高频复读消息
    for r in rows:
        t = r.get("text", "")
        if "人复读了:" in t:
            highlights.append(HighlightItem(
                sender=r.get("sender_name", ""),
                quote=t,
                interaction_info="群友高频一致复读",
                ref_anchor=_format_msg_anchor(r)
            ))
            if len(highlights) >= 6:
                break

    # ── 板块四：群聊活跃度微画像 ────────────────────────────────────
    hourly = Counter()
    senders = Counter()
    repeat_cnt = Counter()

    for r in rows:
        ts = r.get("ts", 0)
        if ts:
            hour = datetime.fromtimestamp(ts, TZ).hour
            hourly[hour] += 1
        name = r.get("sender_name", "") or f"QQ{r.get('sender_qq', '')}"
        if name and name not in ("系统", "未知"):
            senders[name] += 1
        txt = r.get("text", "").strip()
        if len(txt) <= 20 and txt:
            repeat_cnt[txt] += 1

    profile = ActivityProfile(
        total_messages=len(rows),
        active_members=len(senders),
        hourly_distribution=dict(hourly),
        top_chatters=senders.most_common(5),
        repeat_stats=[(k, v) for k, v in repeat_cnt.most_common(5) if v >= 2]
    )

    return StructuredDigestReport(
        data_cutoff=data_cutoff,
        group_name=group_name or f"群_{group_code}",
        group_code=group_code,
        hot_topics=hot_topics[:6],
        resources=resources[:10],
        highlights=highlights[:5],
        activity_profile=profile
    )


def render_markdown(report: StructuredDigestReport) -> str:
    """将 StructuredDigestReport 对象渲染为 GitHub 风格的专业 Markdown 简报。"""
    lines = []
    lines.append(f"# 【群聊总结简报】{report.group_name}")
    lines.append(f"> **数据截止到 {report.data_cutoff}** | 统计范围包含群内全部有效发言与多媒体流\n")

    # 1. 今日热点话题
    lines.append("## 🔥 今日热点话题 Top N")
    if not report.hot_topics:
        lines.append("- *暂无明显话题聚焦*\n")
    else:
        for idx, item in enumerate(report.hot_topics, 1):
            members_str = "、".join(item.participants) if item.participants else "群友"
            lines.append(f"### {idx}. {item.title}")
            lines.append(f"- **讨论成员**: {members_str} (共 {item.msg_count} 条发言)")
            lines.append(f"- **核心脉络**: {item.conclusion}")
            lines.append(f"- **消息锚点**: `{item.ref_anchor}`\n")

    # 2. 重要资源与链接归档
    lines.append("## 📌 重要资源与链接归档")
    if not report.resources:
        lines.append("- *本周期内未检测到外部技术仓库或文档链接分享*\n")
    else:
        for r in report.resources:
            lines.append(f"- **[{r.category}]** [{r.title}]({r.url}) — *由 @{r.sender} 分享* `{r.ref_anchor}`")
        lines.append("")

    # 3. 金句与群友高光时刻
    lines.append("## 💬 金句与群友高光时刻")
    if not report.highlights:
        lines.append("- *暂无显著高互动记录*\n")
    else:
        for h in report.highlights:
            lines.append(f"- **@{h.sender}** ({h.interaction_info}) `{h.ref_anchor}`:")
            lines.append(f"  > *\"{h.quote}\"*\n")

    # 4. 群聊活跃度微画像
    lines.append("## 📊 群聊活跃度微画像 (纯本地统计)")
    if report.activity_profile:
        p = report.activity_profile
        lines.append(f"- **消息总量**: {p.total_messages} 条 | **活跃人数**: {p.active_members} 人")

        # 水群榜
        chatters_str = "，".join(f"**{name}** ({count}条)" for name, count in p.top_chatters)
        lines.append(f"- **🏆 水群先锋榜**: {chatters_str}")

        # 高频复读
        if p.repeat_stats:
            repeats_str = "，".join(f"\"{txt}\" ({c}次)" for txt, c in p.repeat_stats)
            lines.append(f"- **🔁 高频复读梗**: {repeats_str}")

        # 活跃时段直方图
        if p.hourly_distribution:
            lines.append("- **🕒 24 小时发言峰值分布**:")
            max_h = max(p.hourly_distribution.values()) if p.hourly_distribution else 1
            for h in sorted(p.hourly_distribution.keys()):
                val = p.hourly_distribution[h]
                bar_len = max(1, int(val / max_h * 15))
                bar_str = "█" * bar_len
                lines.append(f"  - `{h:02d}:00`: {bar_str} ({val})")
        lines.append("")

    return "\n".join(lines)

