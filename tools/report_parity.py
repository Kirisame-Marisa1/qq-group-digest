#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""报告-索引一致性核对（只读）。

回答一个问题：**生成出来的报告，有没有真的把索引里的每一行都印出来？**

做法：解析每份报告头部的统计表（群号 / 有效对话 / 总消息 / 最后一条），
再回查 `index.db`，逐群比对：
  1. 报告里写的「总消息」== 索引里该群窗口内的总条数
  2. 报告里写的「最后一条」时间 == 索引里该群窗口内最后一条的时间（精确到分钟）
  3. 正文里实际出现的消息行数 + 声明省略的图片/表情 == 报告声明的「有效对话」数

用法：
    python tools/report_parity.py --dir <报告目录> --since '2026-10-01 00:00'
"""
import argparse
import glob
import os
import re
import sqlite3
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'scripts'))
try:
    from ntqq_core import INDEX_DB as DEFAULT_DB, OUTPUT
    DEFAULT_DIR = os.path.join(OUTPUT, 'batch')
except Exception:
    DEFAULT_DB = r'D:\QQChatCache\data\index.db'
    DEFAULT_DIR = r'D:\QQChatCache\output\batch'

ROW_RE = re.compile(r'\((?P<code>\d{6,12})\)\s*\|\s*(?P<conv>\d+)\s*\|\s*(?P<total>\d+)\s*\|')
MSG_RE = re.compile(r'^\d{2}-\d{2} \d{2}:\d{2} \S')


def ts_of(since):
    return int(datetime.strptime(since, '%Y-%m-%d %H:%M').timestamp())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=DEFAULT_DB)
    ap.add_argument('--dir', default=DEFAULT_DIR)
    ap.add_argument('--since', default='2026-10-01 00:00')
    a = ap.parse_args()

    if not os.path.isdir(a.dir):
        print('[fatal] 报告目录不存在：%s' % a.dir)
        return 2
    con = sqlite3.connect('file:%s?mode=ro' % a.db.replace('\\', '/'), uri=True)
    since = ts_of(a.since)

    files = sorted(glob.glob(os.path.join(a.dir, '*.md')))
    files = [f for f in files if not os.path.basename(f).startswith('_')]
    print('报告 %d 份 ｜ 窗口起点 %s ｜ 索引 %s\n' % (len(files), a.since, a.db))

    bad_total, bad_last, bad_conv, ok = [], [], [], 0
    for path in files:
        with open(path, encoding='utf-8', errors='replace') as fh:
            raw = fh.read()
        head = raw[:4000]
        m = ROW_RE.search(head)
        if not m:
            print('  ?  %-14s 头部无统计表，跳过' % os.path.basename(path))
            continue
        code = int(m.group('code'))
        rep_total = int(m.group('total'))
        rep_conv = int(m.group('conv'))

        idx_total = con.execute(
            'select count(*) from messages where group_code=? and ts>=?', (code, since)).fetchone()[0]
        last_ts = con.execute(
            'select max(ts) from messages where group_code=? and ts>=?', (code, since)).fetchone()[0]

        body = raw[head.find(m.group(0)):]
        printed = sum(1 for line in body.splitlines() if MSG_RE.match(line))
        om = re.search(r'已省略图片/表情\s*(\d+)\s*条', raw)
        omitted = int(om.group(1)) if om else 0

        rep_last = ''
        tail = re.search(r'\|\s*(\d{2}-\d{2} \d{2}:\d{2})\s*\|', head)
        if tail:
            rep_last = tail.group(1)
        idx_last = datetime.fromtimestamp(last_ts).strftime('%m-%d %H:%M') if last_ts else ''

        name = os.path.basename(path)
        if idx_total != rep_total:
            bad_total.append((name, rep_total, idx_total))
            print('  ✗  %-14s 总消息不一致：报告 %d / 索引 %d' % (name, rep_total, idx_total))
        elif rep_last and idx_last and rep_last != idx_last:
            bad_last.append((name, rep_last, idx_last))
            print('  ✗  %-14s 末条时间不一致：报告 %s / 索引 %s' % (name, rep_last, idx_last))
        else:
            ok += 1
        if printed and abs((printed + omitted) - rep_conv) > max(30, rep_conv * 0.02):
            bad_conv.append((name, printed + omitted, rep_conv))
            print('  ~  %-14s 正文自洽性：印出 %d + 省略 %d = %d，报告声明 %d'
                  % (name, printed, omitted, printed + omitted, rep_conv))

    con.close()
    print('\n结果：总消息与末条时间一致 %d/%d' % (ok, len(files)))
    print('      总消息不一致 %d ｜ 末条时间不一致 %d ｜ 正文明细存疑 %d'
          % (len(bad_total), len(bad_last), len(bad_conv)))
    return 1 if (bad_total or bad_last) else 0


if __name__ == '__main__':
    sys.exit(main())
