#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核对：覆盖率高的群，到底是"真完整"还是"根本没收到几条"（小跨度假高）。

只看原始库（plain\\nt_msg.db），不经索引，避免构建侧口径干扰。

⚠️ 必须走 (group_key, day) 索引：直接 `max(time)` 全表扫会撞上已知的溢出页损坏
（`database disk image is malformed`）。所以过滤一律用 day。
"""

import argparse
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'scripts'))
from ntqq_core import find_schema, PLAIN, open_ro, INDEX_DB  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--group', type=int)
    ap.add_argument('--days', type=int, default=7)
    ap.add_argument('--top', type=int, default=10)
    ap.add_argument('--quiet', action='store_true')
    A = ap.parse_args()

    path = os.path.join(PLAIN, 'nt_msg.db')
    if not os.path.exists(path):
        print('!! plain\\nt_msg.db 不存在')
        return 2

    with open(find_schema(), encoding='utf-8') as fh:
        sc = json.load(fh)
    G = sc['group_msg_table']
    seqcol, gcol, dcol = G['seq'], G['group_key'], G['day']

    idx = sqlite3.connect('file:%s?mode=ro' % INDEX_DB.replace('\\', '/'), uri=True)
    mx = idx.execute('select max(ts) from messages').fetchone()[0]
    if A.group:
        groups = [A.group]
        WIN = (A.days,)
    else:
        groups = [r[0] for r in idx.execute(
            'select group_code from messages group by group_code order by count(*) desc limit ?', (A.top,))]
        WIN = (7, 30)
    idx.close()

    cl = open_ro(path)

    def days_for(g):
        return [r[0] for r in cl.execute(
            'select distinct "%s" from group_msg_table where "%s"=? order by "%s" desc limit 40'
            % (dcol, gcol, dcol), (g,))]

    print('参考最新 ts: %s' % mx)
    if not A.quiet and groups:
        print('day 字段样例（群 %d 最近 3 个）: %r' % (groups[0], days_for(groups[0])[:3]))
    print()
    print('%-12s %10s %12s %10s' % ('群号', '行数', 'seq 跨度', '覆盖率'))

    for g in groups:
        ds = days_for(g)
        if not ds:
            print('%-12d  （原始库里没有该群）' % g)
            continue

        def cutoff(days):
            return int(((mx - days * 86400) // 86400) * 86400)

        res = {}
        for days in WIN:
            try:
                res[days] = cl.execute(
                    'select count(*), count(distinct "%s"), min("%s"), max("%s") from group_msg_table '
                    'where "%s"=? and "%s">=?' % (seqcol, seqcol, seqcol, gcol, dcol),
                    (g, cutoff(days))).fetchone()
            except sqlite3.DatabaseError as e:
                res[days] = (None, None, None, None)
                print('   (群 %d %d 天查询失败: %s)' % (g, days, e))

        cells = []
        for days in WIN:
            n, d, lo, hi = res[days]
            if n is None or lo is None or hi is None or hi <= lo:
                cells.append('%12s' % '-')
            else:
                cells.append('%11.1f%%' % (100.0 * d / (hi - lo + 1)))
        n0 = res[WIN[0]][0] or 0
        span0 = (res[WIN[0]][3] - res[WIN[0]][2] + 1) if res[WIN[0]][2] is not None else 0
        print('%-12d %10d %12d %s' % (g, n0, span0, ''.join(cells)))
    cl.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
