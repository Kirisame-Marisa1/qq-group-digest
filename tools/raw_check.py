#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对照原始库：本地索引里"缺"的 seq，在**原始 nt_msg.db 里**到底有没有？

用来排除一个重要的替代解释：如果原始库里也没有 → 是真的没收到（数据缺失）；
如果原始库里有、只是索引里没有 → 那是构建侧的过滤，问题不在数据而在工具。

只读。需要 plain\\nt_msg.db 存在（build 收尾会清空它，要尽早跑）。
"""

import argparse
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'scripts'))
from ntqq_core import find_schema, PLAIN, INDEX_DB, open_ro  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--group', type=int, default=0, help='群号（默认自动取索引中消息最多的群）')
    ap.add_argument('--days', type=int, default=7)
    a = ap.parse_args()

    idx = sqlite3.connect('file:%s?mode=ro' % INDEX_DB.replace('\\', '/'), uri=True)
    mx = idx.execute('select max(ts) from messages').fetchone()[0]
    group = a.group
    if not group:
        r = idx.execute('select group_code from messages group by group_code order by count(*) desc limit 1').fetchone()
        group = r[0] if r else 0
    since = (mx or 0) - a.days * 86400
    idx_rows, idx_distinct, lo, hi = idx.execute(
        'select count(*), count(distinct seq), min(seq), max(seq) from messages where group_code=? and ts>=?',
        (group, since)).fetchone()
    idx.close()

    if not group or lo is None or hi is None:
        print('索引中未找到有效 seq 范围')
        return 1

    with open(find_schema(), encoding='utf-8') as fh:
        sc = json.load(fh)
    G = sc['group_msg_table']
    seqcol, gcol = G.get('seq'), G.get('group_key')
    print('群 %d | seq 列 = %s | group_key 列 = %s' % (group, seqcol, gcol))

    path = os.path.join(PLAIN, 'nt_msg.db')
    if not os.path.exists(path):
        print('!! plain\\nt_msg.db 不存在（build 收尾会清空）→ 这一步做不了')
        return 2
    print('原始库: %s (%.2f GB)' % (path, os.path.getsize(path) / 1024 ** 3))

    con = open_ro(path)
    q = ('select count(*), count(distinct "%s") from group_msg_table '
         'where "%s"=? and "%s" between ? and ?' % (seqcol, gcol, seqcol))
    raw_rows, raw_distinct = con.execute(q, (group, lo, hi)).fetchone()
    print()
    print('窗口 seq %d..%d（跨度 %d）' % (lo, hi, hi - lo + 1))
    print('  原始库 行数=%-8d 不同 seq=%-8d' % (raw_rows, raw_distinct))
    print('  索引里 行数=%-8d 不同 seq=%-8d' % (idx_rows, idx_distinct))
    con.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
