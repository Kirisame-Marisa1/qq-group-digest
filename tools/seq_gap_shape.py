#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""seq 缺口结构体检 —— 只读。判断"缺的消息"是「大段连续」还是「细碎散落」。

为什么重要：
  大段连续  = 客户端不在线那段时间（离线窗口）→ 靠"保持在线"能改善
  细碎散落  = 客户端在线时也没收全（按 seq 漏条）→ 只能靠"按 seq 主动翻页"补

用法：
    python tools/seq_gap_shape.py --group <群号> --days 7
    python tools/seq_gap_shape.py --group <群号> --days 7 --top 10
"""

import argparse
import datetime as dt
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'scripts'))
try:
    from ntqq_core import INDEX_DB as DEFAULT_DB
except Exception:
    DEFAULT_DB = r'D:\QQChatCache\data\index.db'

TZ = dt.timezone(dt.timedelta(hours=8))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=DEFAULT_DB)
    ap.add_argument('--group', type=int, required=True)
    ap.add_argument('--days', type=int, default=7)
    ap.add_argument('--top', type=int, default=8)
    a = ap.parse_args()

    con = sqlite3.connect(f'file:{a.db}?mode=ro', uri=True)
    mx = con.execute('select max(ts) from messages').fetchone()[0]
    if mx is None:
        print('索引中暂无消息记录')
        con.close()
        return 0
    lo = mx - a.days * 86400
    name = con.execute('select group_name from messages where group_code=? limit 1', (a.group,)).fetchone()

    rows = list(con.execute(
        'select seq, ts from messages where group_code=? and ts>=? and seq is not null order by seq', (a.group, lo)))
    if not rows:
        print('该窗口没有数据')
        con.close()
        return 0

    s0, s1 = rows[0][0], rows[-1][0]
    span = s1 - s0 + 1
    have = len({s for s, _ in rows})
    print('群 %s  %s' % (a.group, (name[0] if name else '')[:30]))
    print('窗口: 近 %d 天；seq %d..%d  跨度 %d  本地不同 seq %d  覆盖率 %.1f%%'
          % (a.days, s0, s1, span, have, 100.0 * have / max(1, span)))

    gaps = []
    uniq = sorted({s for s, _ in rows})
    tmap = {}
    for s, t in rows:
        tmap.setdefault(s, t)
    for i in range(1, len(uniq)):
        d = uniq[i] - uniq[i - 1]
        if d > 1:
            gaps.append((d - 1, uniq[i - 1], uniq[i], tmap[uniq[i - 1]], tmap[uniq[i]]))

    miss = sum(g[0] for g in gaps)
    print('\n缺口: %d 个，共缺 %d 条（占跨度 %.1f%%）' % (len(gaps), miss, 100.0 * miss / max(1, span)))
    if gaps:
        sizes = sorted((g[0] for g in gaps), reverse=True)
        print('缺口大小: 最大 %d  中位 %d  最小 %d' % (sizes[0], sizes[len(sizes) // 2], sizes[-1]))
        big = sum(s for s in sizes if s >= 50)
        print('其中 ≥50 条的大缺口贡献 %d 条 = 全部缺口 %.0f%%；'
              '剩余 %d 个碎缺口贡献 %d 条'
              % (big, 100.0 * big / max(1, miss), sum(1 for s in sizes if s < 50),
                 miss - big))

        print('\n最大的 %d 个缺口：' % a.top)
        for n, before, after, tb, ta in sorted(gaps, key=lambda g: -g[0])[:a.top]:
            fb = dt.datetime.fromtimestamp(tb, TZ).strftime('%m-%d %H:%M')
            fa = dt.datetime.fromtimestamp(ta, TZ).strftime('%m-%d %H:%M')
            mins = (ta - tb) / 60
            print('  缺 %5d 条   %s → %s   (%.0f 分钟，%.1f 条/分)'
                  % (n, fb, fa, mins, n / max(1e-9, mins)))
    con.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
