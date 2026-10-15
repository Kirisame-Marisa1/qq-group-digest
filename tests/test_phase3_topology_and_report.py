#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单元测试：Phase 3 对话拓扑重构、分层摘要树与可溯源标准化报告 (test_phase3_topology_and_report.py)"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(HERE, '..', 'scripts')
sys.path.insert(0, SCRIPTS_DIR)

from ntqq_core import INDEX_SCHEMA, run_index_migrations
from ntqq_segmenter import ConversationSegmenter, TopicCluster
from ntqq_summary_tree import (
    update_group_cursor, get_group_cursor,
    extract_cluster_title_and_summary,
    build_and_save_micro_digests, load_micro_digests
)
from ntqq_report_schema import (
    StructuredDigestReport, HotTopicItem, ResourceItem, HighlightItem, ActivityProfile,
    build_structured_report, render_markdown
)
from ntqq_report import generate_structured_digest


class TestConversationSegmenter(unittest.TestCase):
    """测试模块 3.1：基于引用与时间窗的对话树重构与话题切片。"""

    def setUp(self):
        self.segmenter = ConversationSegmenter(time_gap_threshold=900, max_cluster_size=50)

    def test_segment_by_time_gap(self):
        """测试时间断层 (Δt > 15分钟) 将扁平流切分为独立话题簇。"""
        # Session 1: 10:00:00, 10:02:00
        # Session 2: 10:20:00 (间隔 18 分钟 > 15 分钟)
        msgs = [
            {"msg_id": 1, "seq": 100, "ts": 1760000000, "group_code": 2001, "group_name": "技术交流群",
             "sender_name": "张三", "text": "今天测试环境部署好了吗？", "reply_seq": 0},
            {"msg_id": 2, "seq": 101, "ts": 1760000120, "group_code": 2001, "group_name": "技术交流群",
             "sender_name": "李四", "text": "已经部署完毕，正在跑流水线", "reply_seq": 100},
            {"msg_id": 3, "seq": 102, "ts": 1760001200, "group_code": 2001, "group_name": "技术交流群",
             "sender_name": "王五", "text": "大家中午一起去二食堂吃饭吗？", "reply_seq": 0},
        ]

        clusters = self.segmenter.segment_messages(msgs)
        self.assertEqual(len(clusters), 2)

        # 检查簇 1
        c1 = next(c for c in clusters if c.start_seq == 100)
        self.assertEqual(c1.message_count, 2)
        self.assertEqual(c1.start_seq, 100)
        self.assertEqual(c1.end_seq, 101)
        self.assertIn("张三", c1.participants)
        self.assertIn("李四", c1.participants)
        self.assertIn("[Ref: #seq_100 - #seq_101", c1.ref_anchor)

        # 检查簇 2
        c2 = next(c for c in clusters if c.start_seq == 102)
        self.assertEqual(c2.message_count, 1)
        self.assertEqual(c2.start_seq, 102)
        self.assertEqual(c2.first_sender, "王五")
        self.assertIn("[Ref: #seq_102", c2.ref_anchor)


class TestSummaryTreeAndGroupCursors(unittest.TestCase):
    """测试模块 4：增量游标与分层微摘要树。"""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "index.db")
        self.con = sqlite3.connect(self.db_path)
        self.con.executescript(INDEX_SCHEMA)
        run_index_migrations(self.con)

    def tearDown(self):
        self.con.close()
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass

    def test_group_cursor_persistence_and_monotonicity(self):
        """测试 group_cursors 表对群序列号与时间游标的强一致推进。"""
        # 初始应为 (0, 0)
        seq, ts = get_group_cursor(self.con, 2001)
        self.assertEqual((seq, ts), (0, 0))

        # 更新游标
        update_group_cursor(self.con, 2001, max_seq=150, max_ts=1760000000)
        seq1, ts1 = get_group_cursor(self.con, 2001)
        self.assertEqual(seq1, 150)
        self.assertEqual(ts1, 1760000000)

        # 尝试用更小或旧的值更新，应保持单调不回退
        update_group_cursor(self.con, 2001, max_seq=120, max_ts=1759000000)
        seq2, ts2 = get_group_cursor(self.con, 2001)
        self.assertEqual(seq2, 150)
        self.assertEqual(ts2, 1760000000)

        # 用更大值更新正常推移
        update_group_cursor(self.con, 2001, max_seq=200, max_ts=1760001000)
        seq3, ts3 = get_group_cursor(self.con, 2001)
        self.assertEqual(seq3, 200)
        self.assertEqual(ts3, 1760001000)

    def test_build_and_load_micro_digests(self):
        """测试话题簇微摘要入库与检索。"""
        msgs = [
            {"msg_id": 1, "seq": 10, "ts": 1760000000, "group_code": 2001, "group_name": "测试群",
             "sender_name": "张三", "text": "关于下周版本发布的通知安排", "reply_seq": 0},
            {"msg_id": 2, "seq": 11, "ts": 1760000060, "group_code": 2001, "group_name": "测试群",
             "sender_name": "李四", "text": "收到，代码已冻结", "reply_seq": 10},
        ]
        cluster = TopicCluster(
            cluster_id="cluster_2001_10_11_test",
            group_code=2001,
            group_name="测试群",
            start_seq=10,
            end_seq=11,
            start_ts=1760000000,
            end_ts=1760000060,
            first_sender="张三",
            participants=["张三", "李四"],
            message_count=2,
            messages=msgs,
            ref_anchor="[Ref: #seq_10 - #seq_11, 10:00 @张三]"
        )

        saved_count = build_and_save_micro_digests(self.con, [cluster])
        self.assertEqual(saved_count, 1)

        loaded = load_micro_digests(self.con, 2001, since_ts=1759000000)
        self.assertEqual(len(loaded), 1)
        item = loaded[0]
        self.assertEqual(item["cluster_id"], "cluster_2001_10_11_test")
        self.assertEqual(item["start_seq"], 10)
        self.assertEqual(item["end_seq"], 11)
        self.assertIn("通知安排", item["title"])
        self.assertIn("张三", item["participants"])


class TestTraceableStructuredReport(unittest.TestCase):
    """测试模块 5：带溯源锚点的 4 大标准化板块报告。"""

    def test_build_and_render_structured_report(self):
        """测试 4 大板块（热点、资源、金句、活跃度）完整生成并渲染为 Markdown。"""
        rows = [
            {"msg_id": 1, "seq": 101, "ts": 1760000000, "group_code": 3001, "group_name": "AI研讨群",
             "sender_name": "Alice", "text": "分享一个开源推理引擎 https://github.com/vllm-project/vllm 非常好用",
             "reply_seq": 0, "kind": "text"},
            {"msg_id": 2, "seq": 102, "ts": 1760000100, "group_code": 3001, "group_name": "AI研讨群",
             "sender_name": "Bob", "text": "支持多卡张量并行吗？", "reply_seq": 101, "kind": "text"},
            {"msg_id": 3, "seq": 103, "ts": 1760000200, "group_code": 3001, "group_name": "AI研讨群",
             "sender_name": "Charlie", "text": "支持的，而且吞吐提升很大！", "reply_seq": 102, "kind": "text"},
            {"msg_id": 4, "seq": 104, "ts": 1760000300, "group_code": 3001, "group_name": "AI研讨群",
             "sender_name": "Dave", "text": "[群友 Eve、Frank 等 3 人复读了: \"+1\"]", "reply_seq": 0, "kind": "text"},
        ]

        segmenter = ConversationSegmenter()
        clusters = segmenter.segment_messages(rows)

        report: StructuredDigestReport = build_structured_report(
            rows=rows,
            clusters=clusters,
            group_name="AI研讨群",
            group_code=3001,
            data_cutoff="2026-10-15 12:00"
        )

        # 1. 验证 Pydantic 模型合法性
        self.assertEqual(report.group_name, "AI研讨群")
        self.assertEqual(report.group_code, 3001)

        # 2. 验证板块一：热点话题
        self.assertTrue(len(report.hot_topics) >= 1)
        self.assertIn("[Ref: #seq_", report.hot_topics[0].ref_anchor)

        # 3. 验证板块二：资源链接归档（提取 GitHub 链接）
        self.assertTrue(len(report.resources) >= 1)
        res = report.resources[0]
        self.assertEqual(res.category, "GitHub 仓库")
        self.assertIn("vllm-project/vllm", res.url)
        self.assertIn("[Ref: #seq_101", res.ref_anchor)

        # 4. 验证板块三：金句高光（被引用或复读）
        self.assertTrue(len(report.highlights) >= 1)
        hl_anchor = report.highlights[0].ref_anchor
        self.assertIn("[Ref: #seq_", hl_anchor)

        # 5. 验证板块四：活跃度微画像
        self.assertIsNotNone(report.activity_profile)
        self.assertEqual(report.activity_profile.total_messages, 4)
        self.assertTrue(report.activity_profile.active_members >= 3)

        # 6. 验证 Markdown 渲染内容包含 4 大标准化板块标题与锚点
        md = render_markdown(report)
        self.assertIn("# 【群聊总结简报】AI研讨群", md)
        self.assertIn("## 🔥 今日热点话题 Top N", md)
        self.assertIn("## 📌 重要资源与链接归档", md)
        self.assertIn("## 💬 金句与群友高光时刻", md)
        self.assertIn("## 📊 群聊活跃度微画像 (纯本地统计)", md)
        self.assertIn("[Ref: #seq_", md)

        # 7. 测试便捷生成器接口
        helper_md = generate_structured_digest(3001, rows=rows, group_name="AI研讨群")
        self.assertIn("## 🔥 今日热点话题 Top N", helper_md)
        self.assertIn("vllm-project/vllm", helper_md)


if __name__ == '__main__':
    unittest.main()

