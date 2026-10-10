#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单元测试：群状态变更登记（首次基线、新增/改名/退群、回归、快照防呆、检索与摘要）(test_group_state.py)"""

import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(HERE, '..', 'scripts')
sys.path.insert(0, SCRIPTS_DIR)

import ntqq_group_state as GS


class TestGroupState(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.plain_dir = os.path.join(self.tmp_dir.name, 'plain')
        self.run_dir = os.path.join(self.tmp_dir.name, 'run')
        os.makedirs(self.plain_dir, exist_ok=True)
        os.makedirs(self.run_dir, exist_ok=True)

        # 保存并替换 ntqq_group_state 的全局路径变量
        self._old_plain = GS.PLAIN
        self._old_registry = GS.REGISTRY
        self._old_snapshot = GS.SNAPSHOT
        self._old_ensure_dirs = GS.ensure_dirs

        GS.PLAIN = self.plain_dir
        GS.REGISTRY = os.path.join(self.run_dir, 'group_registry.json')
        GS.SNAPSHOT = os.path.join(self.run_dir, 'group_snapshot.json')
        GS.ensure_dirs = lambda: os.makedirs(self.run_dir, exist_ok=True)

        self.db_path = os.path.join(self.plain_dir, 'group_info.db')

    def tearDown(self):
        GS.PLAIN = self._old_plain
        GS.REGISTRY = self._old_registry
        GS.SNAPSHOT = self._old_snapshot
        GS.ensure_dirs = self._old_ensure_dirs
        import gc
        gc.collect()
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass

    def _write_group_info_db(self, group_list_rows, detail_rows=None):
        """辅助方法：创建或覆写模拟的 group_info.db（包含 group_list 与 group_detail_info_ver1）。"""
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        con = sqlite3.connect(self.db_path)
        try:
            con.execute('CREATE TABLE group_list ("60001" INTEGER, "60007" TEXT)')
            con.execute('CREATE TABLE group_detail_info_ver1 ("60001" INTEGER, "60007" TEXT)')
            con.executemany('INSERT INTO group_list VALUES (?, ?)', group_list_rows)
            if detail_rows is not None:
                con.executemany('INSERT INTO group_detail_info_ver1 VALUES (?, ?)', detail_rows)
            else:
                con.executemany('INSERT INTO group_detail_info_ver1 VALUES (?, ?)', group_list_rows)
            con.commit()
        finally:
            con.close()

    def test_group_state_lifecycle(self):
        """完整测试群变更的 5 个核心场景：基线、新入/改名/退群、回归、快照防呆、resolve/mark_left/summarize。"""
        # ── 场景 1：首次扫描建立基线（first_ever=True，new=[]） ──
        initial_groups = [
            (1001, '算法竞赛群'),
            (1002, '操作系统课程群'),
            (1003, '校园二手交易群'),
            (1004, '宿舍水群'),
        ]
        self._write_group_info_db(initial_groups)

        res1 = GS.scan()
        self.assertTrue(res1['ok'])
        self.assertTrue(res1['first_ever'])
        self.assertEqual(res1['new'], [])
        self.assertEqual(res1['left'], [])
        self.assertEqual(res1['returned'], [])
        self.assertEqual(res1['renamed'], [])
        self.assertFalse(res1['suspect_snapshot'])

        # ── 场景 2：新增一个群(1005)、改名一个群(1002)、退掉一个群(1003) ──
        second_groups = [
            (1001, '算法竞赛群'),
            (1002, '操作系统与内核实验群'),  # 改名
            # 1003 已退群（从 group_list 移除）
            (1004, '宿舍水群'),
            (1005, '秋招求职交流群'),        # 新增
        ]
        self._write_group_info_db(second_groups)

        res2 = GS.scan()
        self.assertTrue(res2['ok'])
        self.assertFalse(res2['first_ever'])
        self.assertEqual([r['code'] for r in res2['new']], [1005])
        self.assertEqual([r['code'] for r in res2['left']], [1003])
        self.assertEqual(res2['renamed'], [(1002, '操作系统课程群', '操作系统与内核实验群')])
        self.assertEqual(res2['returned'], [])

        # ── 场景 3：退掉的群(1003)重新出现在 group_list，验证识别为 returned ──
        third_groups = [
            (1001, '算法竞赛群'),
            (1002, '操作系统与内核实验群'),
            (1003, '校园二手交易群'),        # 重新加回
            (1004, '宿舍水群'),
            (1005, '秋招求职交流群'),
        ]
        self._write_group_info_db(third_groups)

        res3 = GS.scan()
        self.assertTrue(res3['ok'])
        self.assertEqual([r['code'] for r in res3['returned']], [1003])
        self.assertEqual(res3['new'], [])
        self.assertEqual(res3['left'], [])

        # ── 场景 4：快照损坏防呆（群数量从 5 个骤降到 1 个，低于一半，suspect_snapshot=True） ──
        corrupted_groups = [
            (1001, '算法竞赛群'),
        ]
        self._write_group_info_db(corrupted_groups)

        res4 = GS.scan()
        self.assertTrue(res4['ok'])
        self.assertTrue(res4['suspect_snapshot'])
        # 挂起退群判定，不误报退群
        self.assertEqual(res4['left'], [])

        # ── 场景 5：测试 resolve()、mark_left()、summarize() ──
        reg = GS.load_registry()
        # resolve 按群号或群名子串匹配
        hits_by_code = GS.resolve(reg, '1005')
        self.assertEqual(len(hits_by_code), 1)
        self.assertEqual(hits_by_code[0][0], 1005)

        hits_by_name = GS.resolve(reg, '内核实验')
        self.assertEqual(len(hits_by_name), 1)
        self.assertEqual(hits_by_name[0][0], 1002)

        # mark_left 手工标记退群
        marked = GS.mark_left(reg, '秋招求职')
        self.assertIsNotNone(marked)
        self.assertEqual(marked[0][0], 1005)
        reg_after = GS.load_registry()
        self.assertFalse(reg_after['groups']['1005']['in_member_list'])
        self.assertTrue(reg_after['groups']['1005']['left_ts'] > 0)

        # summarize 输出验证
        summary_lines_2 = '\n'.join(GS.summarize(res2))
        self.assertIn('新入群 1 个', summary_lines_2)
        self.assertIn('1005', summary_lines_2)
        self.assertIn('已退群 1 个', summary_lines_2)
        self.assertIn('1003', summary_lines_2)
        self.assertIn('群名变更 1 个', summary_lines_2)

        summary_lines_4 = '\n'.join(GS.summarize(res4))
        self.assertIn('群数异常偏少', summary_lines_4)


if __name__ == '__main__':
    unittest.main()
