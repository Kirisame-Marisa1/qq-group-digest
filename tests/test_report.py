#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单元测试：高频词提取、时间提示解析、脱敏、重点速览、全文检索与群聊/私聊隔离 (test_report.py)"""

import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(HERE, '..', 'scripts')
sys.path.insert(0, SCRIPTS_DIR)

from ntqq_core import INDEX_SCHEMA, run_index_migrations, rebuild_fts, now_ts
from ntqq_report import (
    keywords, parse_time_hint, anon_code, scrub, mask_names,
    focus_block, search_messages, ask, member_report
)
import ntqq_slice


class TestReport(unittest.TestCase):

    def test_keywords_extraction_and_noise_filter(self):
        """测试 keywords(texts)：验证高频词提取与纯噪音字过滤。"""
        texts = [
            '今天下午软件工程考试安排在三教',
            '提醒大家软件工程考试记得带好学生证',
            '关于软件工程考试的考场座位表已经发布',
            '的了是我你他她它们这那有和就不都也还很太吧呢吗啊',
            '的了是我你他她它们这那有和就不都也还很太吧呢吗啊',
            '的了是我你他她它们这那有和就不都也还很太吧呢吗啊',
            'cat.jpg dog.png 123456 00ff1a',
        ]
        kws = keywords(texts, min_count=3, topk=10)
        kw_dict = dict(kws)
        # 应成功提取高频实词「软件工程考试」
        self.assertIn('软件工程考试', kw_dict)
        self.assertEqual(kw_dict['软件工程考试'], 3)
        # 纯噪音组合、图片后缀、纯数字不应混入结果
        for word, _ in kws:
            self.assertNotIn(word, ('的了', '是我', '你他', 'jpg', 'png', '123456'))

    def test_parse_time_hint(self):
        """测试 parse_time_hint(q)：验证「刚刚」「今天」「昨天」「本周」「最近」时间窗口解析。"""
        now = now_ts()

        t_just, lbl_just = parse_time_hint('刚刚群里有人发通知吗')
        self.assertEqual(lbl_just, '刚刚（近 2 小时）')
        self.assertAlmostEqual(now - t_just, 2 * 3600, delta=10)

        t_today, lbl_today = parse_time_hint('今天有什么重要安排')
        self.assertEqual(lbl_today, '今天')
        self.assertTrue(0 <= now - t_today <= 86400 + 10)

        t_yest, lbl_yest = parse_time_hint('昨天老师说了什么')
        self.assertEqual(lbl_yest, '昨天起')
        self.assertAlmostEqual(t_today - t_yest, 86400, delta=10)

        t_week, lbl_week = parse_time_hint('本周的组会时间定了吗')
        self.assertEqual(lbl_week, '本周')
        self.assertTrue(t_week <= t_today)

        t_recent, lbl_recent = parse_time_hint('最近有什么作业要交', default_days=5)
        self.assertEqual(lbl_recent, '最近 5 天')
        self.assertAlmostEqual(now - t_recent, 5 * 86400, delta=10)

        t_default, lbl_default = parse_time_hint('请问期末答辩地点在哪', default_days=7)
        self.assertEqual(lbl_default, '最近 7 天（未指定时间）')
        self.assertAlmostEqual(now - t_default, 7 * 86400, delta=10)

    def test_anon_scrub_mask_and_focus_block(self):
        """测试 anon_code、scrub、mask_names 与 focus_block。"""
        # 1. anon_code
        self.assertEqual(anon_code(1), 'A')
        self.assertEqual(anon_code(26), 'Z')
        self.assertEqual(anon_code(27), 'AA')

        # 2. scrub
        scrubbed = scrub('请联系学委 QQ 123456789 或备用号 98765432，短号 123 不脱敏')
        self.assertIn('【QQ号】', scrubbed)
        self.assertNotIn('123456789', scrubbed)
        self.assertNotIn('98765432', scrubbed)
        self.assertIn('123', scrubbed)

        # 3. mask_names
        gm = {'张三': '用户A', '李四': '用户B'}
        masked = mask_names('张三 提醒 @李四 和 @王五 联系 123456789', gm)
        self.assertIn('用户A', masked)
        self.assertIn('@用户B', masked)
        self.assertNotIn('张三', masked)
        self.assertNotIn('李四', masked)
        self.assertNotIn('王五', masked)
        self.assertIn('【QQ号】', masked)

        # 4. focus_block
        rows = [
            {
                'msg_id': 1,
                'ts': 1760000100,
                'group_code': 10001,
                'group_name': '2026级软件工程通知群',
                'sender_name': '辅导员',
                'kind': 'text',
                'text': '@全体成员 重要通知：请大家务必在明天下午五点前提交实习报名统计表，逾期截止！'
            },
            {
                'msg_id': 2,
                'ts': 1760000200,
                'group_code': 10001,
                'group_name': '2026级软件工程通知群',
                'sender_name': '路人甲',
                'kind': 'text',
                'text': '收到'
            }
        ]
        fb = focus_block(rows)
        self.assertIn('重点速览', fb)
        self.assertIn('实习报名统计表', fb)
        self.assertIn('重要度', fb)
        self.assertNotIn('路人甲', fb)

    def test_search_and_ask_with_c2c_isolation(self):
        """测试 search_messages 与 ask：验证 FTS5/多词检索、IDF 打分，以及群聊与 c2c 私聊严格隔离。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, 'index.db')
            idx = sqlite3.connect(db_path)
            idx.row_factory = sqlite3.Row
            try:
                idx.executescript(INDEX_SCHEMA)
                run_index_migrations(idx)

                t0, t1 = 1760000000, 1760010000

                # 插入群信息与群消息（含第 16 列 seq）
                idx.execute(
                    "INSERT INTO groups VALUES (20001, '编译原理课程群', ?, ?, 3)",
                    (t0, t0 + 300)
                )
                group_msgs = [
                    (1, t0 + 10, '2026-10-01', 10, 20001, '编译原理课程群',
                     'u1', 111111, '张老师', 0, 1, 0, 'text',
                     '今天我们讲解词法分析器与正则表达式自动机转换', None, 1001),
                    (2, t0 + 120, '2026-10-01', 10, 20001, '编译原理课程群',
                     'u2', 222222, '助教', 0, 1, 0, 'text',
                     '实验一编译器前端语法分析大作业将在下周三截止提交', None, 1002),
                    (3, t0 + 240, '2026-10-01', 10, 20001, '编译原理课程群',
                     'u3', 333333, '同学甲', 0, 1, 0, 'text',
                     '请问食堂今天中午有什么好吃的推荐吗', None, 1003),
                ]
                idx.executemany(
                    "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    group_msgs
                )

                # 插入私聊联系人与私聊消息 (c2c_messages)
                idx.execute(
                    "INSERT INTO c2c_groups VALUES (888888, '私聊好友小王', ?, ?, 2)",
                    (t0, t0 + 300)
                )
                c2c_msgs = [
                    (101, t0 + 60, '2026-10-01', 10, 888888, '私聊好友小王',
                     'u_friend', 888888, '私聊好友小王', 0, 1, 0, 'text',
                     '私聊专属机密：服务器数据库密码已经重置为 token_xyz_987', None),
                    (102, t0 + 180, '2026-10-01', 10, 888888, '私聊好友小王',
                     'u_friend', 888888, '私聊好友小王', 0, 1, 0, 'text',
                     '周末去哪里打羽毛球', None),
                ]
                idx.executemany(
                    "INSERT INTO c2c_messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    c2c_msgs
                )
                idx.commit()
                rebuild_fts(idx)

                # 1. 验证群聊多关键词 search_messages 命中目标群消息
                hits_grp = search_messages(idx, '语法分析 大作业', t0, t1, msg_table='messages')
                self.assertEqual(len(hits_grp), 1)
                self.assertEqual(hits_grp[0]['msg_id'], 2)

                # 2. 验证群聊查询绝不会混入 c2c_messages 私聊记录
                hits_leak = search_messages(idx, 'token_xyz_987', t0, t1, msg_table='messages')
                self.assertEqual(len(hits_leak), 0)
                hits_leak2 = search_messages(idx, '私聊专属机密', t0, t1, msg_table='messages')
                self.assertEqual(len(hits_leak2), 0)

                # 3. 验证 msg_table='c2c_messages' 能准确查出私聊记录，且不混入群聊
                hits_c2c = search_messages(idx, '私聊专属机密 token_xyz_987', t0, t1, msg_table='c2c_messages')
                self.assertEqual(len(hits_c2c), 1)
                self.assertEqual(hits_c2c[0]['msg_id'], 101)
                hits_c2c_grp = search_messages(idx, '语法分析', t0, t1, msg_table='c2c_messages')
                self.assertEqual(len(hits_c2c_grp), 0)

                # 4. 验证 ask() 在群聊上的 IDF 打分命中与私聊隔离
                kws, known, scored, dropped, n, rows = ask(
                    idx, '请问语法分析大作业什么时候截止提交', t0, t1,
                    msg_table='messages', grp_table='groups'
                )
                self.assertEqual(n, 3)
                self.assertTrue(len(scored) >= 1)
                # 最高分应为 msg_id=2 的大作业通知
                self.assertEqual(scored[0][3]['msg_id'], 2)
                # 确保 ask(msg_table='messages') 的结果集完全不包含私聊消息
                all_msg_ids = {r['msg_id'] for r in rows}
                self.assertNotIn(101, all_msg_ids)
                self.assertNotIn(102, all_msg_ids)

                # 5. 验证 ask() 在私聊表 (msg_table='c2c_messages') 上能正常打分命中
                _, _, scored_c2c, _, n_c2c, _ = ask(
                    idx, '服务器数据库密码重置了吗', t0, t1,
                    msg_table='c2c_messages', grp_table='c2c_groups'
                )
                self.assertEqual(n_c2c, 2)
                self.assertEqual(len(scored_c2c), 1)
                self.assertEqual(scored_c2c[0][3]['msg_id'], 101)

                # 6. 验证 member_report() 群友个人发言专报
                out_lines = []
                mres = member_report(idx, out_lines, '助教', t0, t1, ctx=1)
                self.assertEqual(mres['matched'], 1)
                self.assertEqual(mres['n'], 1)
                report_txt = '\n'.join(out_lines)
                self.assertIn('### 助教 · 概览', report_txt)
                self.assertIn('实验一编译器前端语法分析大作业', report_txt)
            finally:
                idx.close()

    def test_ntqq_slice_helpers(self):
        """测试 ntqq_slice 按需解析辅助函数 cols_of 与 varint。"""
        sql = 'CREATE TABLE "group_msg_table"("40001" INT PRIMARY KEY, "40003" INT, "40800" BLOB, UNIQUE("40001"))'
        self.assertEqual(ntqq_slice.cols_of(sql), ['40001', '40003', '40800'])
        v, nxt = ntqq_slice.varint(b'\x81\x05', 0)
        self.assertEqual(v, 133)
        self.assertEqual(nxt, 2)


if __name__ == '__main__':
    unittest.main()
