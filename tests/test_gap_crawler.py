#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单元测试：L1a/L1b 缺口规划、碎片合并、副本幂等导入、去重压缩、子集校验与覆盖率验收 (test_gap_crawler.py)"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(HERE, '..', 'scripts')
TOOLS_DIR = os.path.join(HERE, '..', 'tools')
sys.path.insert(0, SCRIPTS_DIR)
sys.path.insert(0, TOOLS_DIR)

from ntqq_core import INDEX_SCHEMA  # noqa: E402
from gap_plan import merge_holes, scan_group  # noqa: E402
from import_filled import load as load_filled, import_records  # noqa: E402
from compact_filled import compact_file  # noqa: E402
from purge_filled import verify_subset  # noqa: E402
from coverage_check import scan as scan_coverage  # noqa: E402


class TestSelfloaderGapPipeline(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.idx_path = os.path.join(self.tmp_dir.name, 'mock_index.db')
        self.jsonl_path = os.path.join(self.tmp_dir.name, 'filled-messages.jsonl')

        con = sqlite3.connect(self.idx_path)
        con.executescript(INDEX_SCHEMA)
        con.execute(
            'INSERT INTO groups (group_code, group_name, first_ts, last_ts, n) VALUES (?, ?, ?, ?, ?)',
            (10001, '测试群A', 1760000000, 1760003600, 4),
        )
        # 插入 seq: 100, 101, 120 (缺 102..119 共 18 条), 125 (缺 121..124 共 4 条 < min_gap=5 跳号), 200 (缺 126..199 共 74 条)
        rows = [
            (1, 1760000100, '2026-10-09', 10, 10001, '测试群A', 'u1', 111, '群友甲', 0, 2, 0, 'text', '消息100', 0, 100),
            (2, 1760000200, '2026-10-09', 10, 10001, '测试群A', 'u1', 111, '群友甲', 0, 2, 0, 'text', '消息101', 0, 101),
            (3, 1760000300, '2026-10-09', 10, 10001, '测试群A', 'u2', 222, '群友乙', 0, 2, 0, 'text', '消息120', 0, 120),
            (4, 1760000400, '2026-10-09', 10, 10001, '测试群A', 'u2', 222, '群友乙', 0, 2, 0, 'text', '消息125', 0, 125),
            (5, 1760000500, '2026-10-09', 10, 10001, '测试群A', 'u3', 333, '群友丙', 0, 2, 0, 'text', '消息200', 0, 200),
        ]
        con.executemany(
            'INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            rows,
        )
        con.commit()
        con.close()

    def tearDown(self):
        import gc
        gc.collect()
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass

    def test_merge_holes_and_scan_group(self):
        """测试缺口扫描：过滤 < min_gap 跳号噪声，并将邻近碎片合并为带 holes 子区间的扫描段"""
        con = sqlite3.connect(self.idx_path)
        # 当 merge_gap=10 时，缺口 102..119 (长 18) 与 126..199 (长 74) 之间仅隔 120..125 (6 条 <= 10)，应合并为 1 段
        picked_merged, total, nholes = scan_group(con, 10001, 1760000000, min_gap=5, per_group=0, merge_gap=10)
        self.assertEqual(nholes, 2)
        self.assertEqual(total, 18 + 74)
        self.assertEqual(len(picked_merged), 1)
        seg = picked_merged[0]
        self.assertEqual(seg['start'], '102')
        self.assertEqual(seg['end'], '199')
        self.assertEqual(seg['gap_len'], 92)
        self.assertEqual(seg['holes'], [['102', '119'], ['126', '199']])

        # 当 merge_gap=2 时，间距 6 > 2，不合并，保持 2 段且按缺口长度降序排列
        picked_unmerged, _, _ = scan_group(con, 10001, 1760000000, min_gap=5, per_group=0, merge_gap=2)
        self.assertEqual(len(picked_unmerged), 2)
        self.assertEqual(picked_unmerged[0]['start'], '126')
        self.assertEqual(picked_unmerged[1]['start'], '102')
        con.close()

    def test_import_compact_and_purge_pipeline(self):
        """测试 filled-messages.jsonl 去重压缩、确定性负整数主键幂等导入与子集安全删除校验"""
        # 写入含重复键和已存在键的 JSONL
        records = [
            {'group': 10001, 'seq': '100', 'ts': 1760000100, 'sender_uid': 'u1', 'sender_name': '群友甲', 'kind': 'text', 'text': '已有消息100'},
            {'group': 10001, 'seq': '102', 'ts': 1760000210, 'sender_uid': 'u2', 'sender_name': '群友乙', 'kind': 'text', 'text': '补回消息102旧'},
            {'group': 10001, 'seq': '102', 'ts': 1760000210, 'sender_uid': 'u2', 'sender_name': '群友乙', 'kind': 'text', 'text': '补回消息102新'},
            {'group': 10001, 'seq': '103', 'ts': 1760000220, 'sender_uid': 'u3', 'sender_name': '群友丙', 'kind': 'image', 'text': '[图片]'},
        ]
        with open(self.jsonl_path, 'w', encoding='utf-8-sig') as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + '\n')

        # 1. 导入前校验：102、103 只在副本中，verify_subset 应检测到 missing 2 条
        _, _, missing_before = verify_subset(self.idx_path, self.jsonl_path)
        self.assertEqual(missing_before, {(10001, 102), (10001, 103)})

        # 2. 执行 compact_file 去重
        n_total, n_uniq, n_bad = compact_file(self.jsonl_path)
        self.assertEqual(n_total, 4)
        self.assertEqual(n_uniq, 3)
        self.assertEqual(n_bad, 0)

        # 3. 执行 import_records 幂等导入
        recs, bad, _ = load_filled(self.jsonl_path)
        self.assertEqual(bad, 0)
        inserted, skipped, touched = import_records(self.idx_path, recs, dry_run=False)
        self.assertEqual(inserted, 2)   # 102, 103 新增
        self.assertEqual(skipped, 1)    # 100 已存在跳过
        self.assertEqual(touched, 1)

        # 验证负整数主键 -(group * 1e9 + seq)
        con = sqlite3.connect(self.idx_path)
        row_102 = con.execute('SELECT msg_id, text FROM messages WHERE group_code=10001 AND seq=102').fetchone()
        self.assertIsNotNone(row_102)
        self.assertEqual(row_102[0], -(10001 * 1000000000 + 102))
        self.assertEqual(row_102[1], '补回消息102新')
        con.close()

        # 4. 再次导入验证幂等性（新增 0，跳过 3）
        inserted2, skipped2, _ = import_records(self.idx_path, recs, dry_run=False)
        self.assertEqual(inserted2, 0)
        self.assertEqual(skipped2, 3)

        # 5. 导入后校验：副本 ⊆ 索引，missing 应为空集
        _, _, missing_after = verify_subset(self.idx_path, self.jsonl_path)
        self.assertEqual(missing_after, set())

    def test_coverage_check_scan(self):
        """测试四口径覆盖率扫描（区分 <= split 的跳号与 > split 的大段缺口）"""
        con = sqlite3.connect(self.idx_path)
        rows, mn, mx, gap_total, holes, big, small_jumps, big_total = scan_coverage(
            con, 10001, 1760000000, split=50
        )
        self.assertEqual(rows, 5)
        self.assertEqual(mn, 100)
        self.assertEqual(mx, 200)
        # 只有 126..199 (长 74) > split=50 计入大段缺口
        self.assertEqual(big_total, 74)
        self.assertEqual(len(big), 1)
        self.assertEqual(big[0], (74, 126))
        self.assertEqual(small_jumps, 2)  # 18 和 4 均 <= 50
        con.close()


if __name__ == '__main__':
    unittest.main()
