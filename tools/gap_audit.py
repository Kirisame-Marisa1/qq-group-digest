#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""index.db 缺口体检 —— 只读。

回答的问题：**「电脑/QQ 不在线那段时间的消息，现在本地到底有没有？」**

做法：客户端不在线 = 所有群同时收不到任何消息。所以
「全体群同时连续静默 ≥ N 小时」的窗口，就是**极可能的离线窗口**；
再看当前索引里这些窗口是不是空的 —— 空 = 那段数据现在确实不在本地。

用法：
    python tools/gap_audit.py [--min-hours 2] [--days 60] [--db <path>]
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


def loc(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, TZ).strftime('%Y-%m-%d %H:%M')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=DEFAULT_DB)
    ap.add_argument('--min-hours', type=int, default=2)
    ap.add_argument('--days', type=int, default=60)
    ap.add_argument('--top', type=int, default=15)
    a = ap.parse_args()

    con = sqlite3.connect(f'file:{a.db}?mode=ro', uri=True)
    cols = [r[1] for r in con.execute('pragma table_info(messages)')]
    print('messages 列:', cols)

    mn, mx = con.execute('select min(ts), max(ts) from messages').fetchone()
    if mn is None or mx is None:
        print('索引中暂无消息记录')
        con.close()
        return 0
    print('数据范围 : %s .. %s  (%.1f 天)' % (loc(mn), loc(mx), (mx - mn) / 86400))
    print('总行数   : %d' % con.execute('select count(*) from messages').fetchone()[0])

    per_hour = dict(con.execute('select ts/3600, count(*) from messages group by ts/3600'))
    h0, h1 = mn // 3600, mx // 3600

    spans, run = [], None
    for h in range(h0, h1 + 1):
        if per_hour.get(h, 0) == 0:
            if run is None:
                run = h
        elif run is not None:
            spans.append((run, h - 1))
            run = None
    if run is not None:
        spans.append((run, h1))

    big = sorted([s for s in spans if s[1] - s[0] + 1 >= a.min_hours],
                 key=lambda s: -(s[1] - s[0]))

    tot_gap_h = sum(b - x + 1 for x, b in spans)
    print('\n=== 全体群同时静默的窗口（≥%d 小时）: %d 个 ===' % (a.min_hours, len(big)))
    print('（覆盖范围 %.1f 天，其中静默合计 %.0f 小时 = %.1f%%，含 < %d 小时的碎片）'
          % ((mx - mn) / 86400, tot_gap_h, 100 * tot_gap_h / max(1, h1 - h0 + 1), a.min_hours))
    for x, b in big[:25]:
        print('  %s .. %s   %2d 小时' % (loc(x * 3600), loc(b * 3600 + 3599), b - x + 1))

    cut = mx - a.days * 86400
    print('\n=== 近 %d 天逐日总量（本地收到的条数）===' % a.days)
    rows = list(con.execute(
        'select ts/86400 as d, count(*), count(distinct group_code) '
        'from messages where ts >= ? group by d order by d', (cut,)))
    mx_daily = max(r[1] for r in rows) if rows else 1
    for d, n, g in rows:
        bar = '#' * max(1, int(40 * n / mx_daily))
        day = dt.datetime.fromtimestamp(d * 86400, TZ).strftime('%m-%d')
        flag = '   <-- 低' if n < 0.25 * mx_daily else ''
        print('  %s  %6d 条 / %3d 群  %s%s' % (day, n, g, bar, flag))

    print('\n=== 最空的三天：逐小时分布（0 = 该小时一条都没有）===')
    for d, n, _ in sorted(rows, key=lambda r: r[1])[:3]:
        day = dt.datetime.fromtimestamp(d * 86400, TZ).strftime('%Y-%m-%d')
        print('  %s  共 %d 条' % (day, n))
        hours = [0] * 24
        for hh, c in con.execute('select hour, count(*) from messages where ts>=? and ts<? group by hour',
                                 (d * 86400, (d + 1) * 86400)):
            hours[hh] = c
        for start in range(0, 24, 12):
            print('    %02d-%02d: %s' % (start, start + 11,
                  ' '.join(('%4d' % hours[x]) for x in range(start, start + 12))))

    print('\n=== 最活跃 %d 个群：本地覆盖与静默窗口的关系 ===' % a.top)
    tops = list(con.execute(
        'select group_code, count(*) c, min(ts), max(ts) from messages '
        'group by group_code order by c desc limit ?', (a.top,)))
    for code, c, gmn, gmx in tops:
        inside = sum(1 for x, b in spans
                     if con.execute('select 1 from messages where group_code=? and ts>=? and ts<=? limit 1',
                                    (code, x * 3600, b * 3600 + 3599)).fetchone())
        name = con.execute('select group_name from groups where group_code=? limit 1', (code,)).fetchone() \
            if _has(con, 'groups') else None
        print('  %s  %7d 条  跨 %d/%d 个静默窗口有数据   %s'
              % (code, c, inside, len(spans), (name[0] if name else '')[:22]))

    print('\n=== seq 覆盖率（本地行数 / seq 跨度），按时间窗口 ===')
    wins = [('全部', 0), ('近180天', 180), ('近30天', 30), ('近7天', 7)]
    hdr = '  %-14s' % '群号' + ''.join('%12s' % w for w, _ in wins)
    print(hdr)
    print('  ' + '-' * (len(hdr) + 2))
    for code, c, gmn, gmx in tops[:10]:
        cells = []
        for _, days in wins:
            lo = gmn if days == 0 else max(gmn, mx - days * 86400)
            r = con.execute('select count(*), count(distinct seq), min(seq), max(seq) '
                            'from messages where group_code=? and ts>=?', (code, lo)).fetchone()
            n, dseq, smin, smax = r
            if not n or smin is None or smax is None or smax == smin:
                cells.append('%12s' % '-')
            else:
                cells.append('%11.1f%%' % (100.0 * dseq / (smax - smin + 1)))
        print('  %-14s' % code + ''.join(cells))

    con.close()
    return 0


def _has(con, table: str) -> bool:
    return con.execute("select 1 from sqlite_master where type='table' and name=?", (table,)).fetchone() is not None


if __name__ == '__main__':
    raise SystemExit(main())
