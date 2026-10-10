#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单元测试：缺口检测算法与爬虫入库调度 (test_gap_crawler.py)"""

import os
import sys
import json
import sqlite3
import tempfile
import unittest

# 将 scripts 加入环境变量
HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(HERE, '..', 'scripts')
sys.path.insert(0, SCRIPTS_DIR)

from ntqq_gap import detect_gaps_from_db, detect_gaps_from_index, format_gap_report
from ntqq_crawler import extract_raw_text, ingest_messages_into_index, run_crawler
from ntqq_core import INDEX_SCHEMA


class TestGapCrawler(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, 'mock_nt_msg.db')
        self.idx_path = os.path.join(self.tmp_dir.name, 'mock_index.db')

    def tearDown(self):
        import gc
        gc.collect()
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass

    def test_detect_gaps_from_db(self):
        """测试从 SQLite group_msg_table 中检测精确序号断层与头部缺口"""
        con = sqlite3.connect(self.db_path)
        # 模拟 group_msg_table 结构 (40003=seq, 40050=time, 40027=group_key)
        con.execute('''
            CREATE TABLE group_msg_table (
                "40001" INTEGER PRIMARY KEY,
                "40003" INTEGER,
                "40027" INTEGER,
                "40050" INTEGER,
                "40058" INTEGER
            )
        ''')
        # 模拟数据：
        # 群 12345，窗口从 100000 开始
        # 第一条消息 seq=50, time=180000 (距窗口起点缺约 80000 秒，应产生 head_gap)
        # seq 序列：50, 51, 52, 60 (缺失 53~59 共 7 条，应产生 seq_gap)
        records = [
            (1, 50, 12345, 180000, 20261001),
            (2, 51, 12345, 180100, 20261001),
            (3, 52, 12345, 180200, 20261001),
            (4, 60, 12345, 181000, 20261001),
        ]
        con.executemany('INSERT INTO group_msg_table VALUES (?, ?, ?, ?, ?)', records)
        con.commit()
        con.close()

        schema = {
            'group_msg_table': {
                'rowid': '40001',
                'seq': '40003',
                'group_key': '40027',
                'time': '40050',
                'day': '40058'
            }
        }

        cut_ts = 90000
        gaps = detect_gaps_from_db(self.db_path, schema, cut_ts, group_filter=12345, group_names={12345: '测试群'})

        # 应该检测出 2 个缺口：1 个 head_gap，1 个 seq_gap
        self.assertEqual(len(gaps), 2)

        head_gap = next((g for g in gaps if g['type'] == 'head_gap'), None)
        self.assertIsNotNone(head_gap)
        self.assertEqual(head_gap['group_code'], 12345)
        self.assertEqual(head_gap['end_seq'], 49)

        seq_gap = next((g for g in gaps if g['type'] == 'seq_gap'), None)
        self.assertIsNotNone(seq_gap)
        self.assertEqual(seq_gap['start_seq'], 53)
        self.assertEqual(seq_gap['end_seq'], 59)
        self.assertEqual(seq_gap['count'], 7)

        # 验证报告生成排版
        rep = format_gap_report(gaps)
        self.assertIn('测试群', rep)
        self.assertIn('53 ~ 59', rep)

    def test_extract_raw_text(self):
        """测试从 RawMessage 元素结构中提取文本与类型的逻辑"""
        # 测试文本消息
        msg1 = {
            'elements': [
                {'elementType': 1, 'textElement': {'content': '你好世界'}},
                {'elementType': 1, 'textElement': {'content': '第二段'}}
            ]
        }
        k1, t1, r1 = extract_raw_text(msg1)
        self.assertEqual(k1, 'text')
        self.assertEqual(t1, '你好世界 第二段')

        # 测试带引用的图片消息
        msg2 = {
            'elements': [
                {'elementType': 7, 'replyElement': {'replayMsgSeq': 1234}},
                {'elementType': 2, 'imageElement': {}}
            ]
        }
        k2, t2, r2 = extract_raw_text(msg2)
        self.assertEqual(k2, 'reply')
        self.assertEqual(t2, '[图片]')
        self.assertEqual(r2, 1234)

    def test_ingest_messages_into_index(self):
        """测试消息直接写入 index.db 的完整性与幂等性"""
        idx = sqlite3.connect(self.idx_path)
        idx.executescript(INDEX_SCHEMA)

        raw_msgs = [
            {
                'msgId': '1001',
                'msgTime': 1760000000,
                'senderUid': 'u100',
                'senderUin': 98765432,
                'sendNickName': '测试张三',
                'elements': [{'elementType': 1, 'textElement': {'content': '测试消息 1'}}]
            },
            {
                'msgId': '1002',
                'msgTime': 1760000060,
                'senderUid': 'u101',
                'senderUin': 12345678,
                'sendNickName': '测试李四',
                'elements': [{'elementType': 1, 'textElement': {'content': '测试消息 2'}}]
            }
        ]

        n1 = ingest_messages_into_index(idx, 88888, '测试群888', raw_msgs)
        self.assertEqual(n1, 2)

        # 验证数据库内容
        cur = idx.execute('SELECT msg_id, sender_qq, text FROM messages WHERE group_code = 88888 ORDER BY msg_id ASC')
        rows = cur.fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][1], 98765432)
        self.assertEqual(rows[0][2], '测试消息 1')

        # 再次执行相同数据，验证 INSERT OR IGNORE 幂等性
        n2 = ingest_messages_into_index(idx, 88888, '测试群888', raw_msgs)
        self.assertEqual(n2, 2)
        total_rows = idx.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
        self.assertEqual(total_rows, 2) # 未重复增加

        idx.close()

    def test_mock_crawler_run(self):
        """测试脱机模拟爬虫调度全流程"""
        cfg = {
            'root': self.tmp_dir.name,
            'crawler': {
                'bridge_port': 48920,
                'batch_size': 10,
                'request_delay_ms': 10
            }
        }
        gaps = [
            {
                'group_code': 99999,
                'group_name': '模拟群',
                'type': 'seq_gap',
                'start_seq': 101,
                'end_seq': 120,
                'count': 20
            }
        ]

        # 临时将全局 INDEX_DB 指向临时数据库
        import ntqq_core
        old_idx = ntqq_core.INDEX_DB
        old_run = ntqq_core.RUN
        ntqq_core.INDEX_DB = self.idx_path
        ntqq_core.RUN = self.tmp_dir.name

        try:
            pulled = run_crawler(cfg, gaps, bridge_port=48920, batch_size=10, delay_ms=10, mock_mode=True)
            self.assertEqual(pulled, 20)

            # 验证写入的数据
            idx = sqlite3.connect(self.idx_path)
            c = idx.execute('SELECT COUNT(*) FROM messages WHERE group_code = 99999').fetchone()[0]
            self.assertEqual(c, 20)
            idx.close()
        finally:
            ntqq_core.INDEX_DB = old_idx
            ntqq_core.RUN = old_run


if __name__ == '__main__':
    unittest.main()
