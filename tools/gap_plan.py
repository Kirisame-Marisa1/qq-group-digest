#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""缺口表生成器 —— 为「按 seq 从服务端补缺口」产出 fill-plan.json（只读 index.db）。

它是 L1b 的第一半：把「哪里缺、缺多少」变成自建加载器 (selfloader.mjs) 能吃的一张表。
第二半是 injector\\run_selfloader.ps1 + injector\\selfloader.mjs。

用法：
    python tools/gap_plan.py                                  # 近 30 天，全部缺口 + 合并扫描段 + 自动预算
    python tools/gap_plan.py --days 10 --per-group 8 --min-gap 30
    python tools/gap_plan.py --groups 100000001,100000002     # 只做这几个群
    python tools/gap_plan.py --print-only                     # 只看摘要，不写文件

口径：
    缺口 = 群内相邻两个「本地已有 seq」之间的空档（cur - prev - 1 > 0）。
    seq 是群内逐条 +1 的计数（字段 40003），实测约 95% 的相邻对差值恰为 1，
    所以差值 > 1 基本是真缺口，不是跳号噪声。
    **每个起点都是缺口的最左端**，且带上 gap_len（缺口总长）；
    加载器会从该起点按 seq 连续翻页（每次 320–799 条）直到把这一整段补完或超时——
    这是「覆盖率做到 100%」的前提，别再改回「起点随机落在缺口内部」。
"""
import argparse
import json
import math
import os
import sqlite3
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'scripts'))
try:
    from ntqq_core import INDEX_DB as DEFAULT_DB
except Exception:
    DEFAULT_DB = r'D:\QQChatCache\data\index.db'


def _default_out():
    for rel in (('..', 'injector', 'out', 'fill-plan.json'),
                ('..', '..', 'injector', 'out', 'fill-plan.json')):
        cand = os.path.normpath(os.path.join(HERE, *rel))
        if os.path.isdir(os.path.dirname( os.path.dirname(cand))):
            return cand
    return os.path.normpath(os.path.join(HERE, '..', 'injector', 'out', 'fill-plan.json'))


DEFAULT_OUT = _default_out()


def group_name(con, code):
    try:
        r = con.execute('select group_name from groups where group_code=?', (code,)).fetchone()
        return (r[0] if r and r[0] else '')[:20]
    except Exception:
        return ''


def merge_holes(holes, merge_gap):
    """把相邻的碎片缺口合并成「扫描段」。

    为什么必须合并：实测缺口高度碎片化（数千段、平均每段仅几十条）。
    若「一段一请求」，数千段就要数千次请求（数小时）——不可接受。
    而 `getMsgsBySeqAndCount` 取的是**连续**区间，所以只要两段缺口之间的「本地已有数据」
    不超过 merge_gap 条，就可以用一条扫描段把它们一次覆盖掉（中间已有的消息会重复取回，
    本地落库时自然覆盖，无副作用）。

    返回 [(start, end, gap_len, [(s, e), ...])]，其中 end = 段内最后一个缺口的右端，
    最后一维是**段内的缺口子区间**——加载器用它过滤落盘，只把真正缺的消息写进 JSONL，
    避免把段内「本地已有」的重复消息也存一遍（实测那是 94% 的体积浪费）。
    """
    if not holes:
        return []
    holes = sorted(holes, key=lambda x: x[1])          # 按起点升序
    segs = []
    cur = None
    for length, start in holes:
        end = start + length - 1
        if cur is None:
            cur = [start, end, length, [(start, end)]]
            continue
        spacing = start - cur[1] - 1                    # 两段之间「本地已有」的条数
        if spacing <= merge_gap:
            cur[1] = end
            cur[2] += length
            cur[3].append((start, end))
        else:
            segs.append(cur)
            cur = [start, end, length, [(start, end)]]
    if cur:
        segs.append(cur)
    return segs


def scan_group(con, code, since, min_gap, per_group, merge_gap):
    """返回该群的缺口起点列表（长缺口优先，起点为缺口最左端）。"""
    holes = []
    prev = None
    cur = con.execute('select seq from messages where group_code=? and ts>=? order by seq', (code, since))
    while True:
        rows = cur.fetchmany(20000)
        if not rows:
            break
        for (seq,) in rows:
            if seq is None:
                continue
            if prev is not None and seq > prev + 1:
                length = seq - prev - 1
                if length >= min_gap:
                    holes.append((length, prev + 1))
            prev = seq
    holes.sort(key=lambda x: -x[0])
    segs = merge_holes(holes, merge_gap)
    segs.sort(key=lambda s: -s[2])
    limit = per_group if (per_group and per_group > 0) else len(segs)
    picked = [{'start': str(s), 'end': str(e), 'gap_len': g, 'span': e - s + 1,
               'holes': [[str(a), str(b)] for a, b in subs]}
              for s, e, g, subs in segs[:limit]]
    total = sum(h[0] for h in holes)
    return picked, total, len(holes)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=DEFAULT_DB)
    ap.add_argument('--days', type=int, default=30, help='滚动窗口天数（默认 30）')
    ap.add_argument('--groups', default='', help='逗号分隔群号；默认自动取消息量最大的若干群')
    ap.add_argument('--top-groups', type=int, default=12, help='自动模式下取前多少个群')
    ap.add_argument('--min-rows', type=int, default=200, help='自动模式下的最小本地行数')
    ap.add_argument('--per-group', type=int, default=0,
                    help='每群最多几个起点；**0 = 不限（默认，把所有缺口都列进去）**。')
    ap.add_argument('--min-gap', type=int, default=5,
                    help='只统计长度 >= 该值的缺口（< 该值按 seq 跳号噪声处理，服务端也没有）')
    ap.add_argument('--merge-gap', type=int, default=600,
                    help='相邻缺口之间「本地已有」的条数 <= 该值时，合并成一条扫描段（默认 600）')
    ap.add_argument('--budget-ms', type=int, default=0,
                    help='加载器总时间预算；**0 = 自动按缺口总量估算（默认）**')
    ap.add_argument('--out', default=DEFAULT_OUT)
    ap.add_argument('--print-only', action='store_true')
    a = ap.parse_args()

    if not os.path.exists(a.db):
        print('[fatal] index.db 不存在：%s' % a.db)
        return 2
    con = sqlite3.connect('file:%s?mode=ro' % a.db.replace('\\', '/'), uri=True)
    since = int(time.time()) - a.days * 86400

    if a.groups:
        codes = [int(x) for x in a.groups.split(',') if x.strip()]
    else:
        codes = [r[0] for r in con.execute(
            'select group_code from messages where ts>=? group by group_code '
            'having count(*)>? order by count(*) desc limit ?', (since, a.min_rows, a.top_groups))]

    print('窗口：近 %d 天（>= %s）' % (a.days, datetime.fromtimestamp(since).strftime('%Y-%m-%d %H:%M')))
    print('目标群：%d 个' % len(codes))
    groups, grand_total, grand_points = [], 0, 0
    for code in codes:
        picked, total, nholes = scan_group(con, code, since, a.min_gap, a.per_group, a.merge_gap)
        grand_total += total
        grand_points += len(picked)
        if not picked:
            print('  %-11d %-20s 无 >=%d 的缺口' % (code, group_name(con, code), a.min_gap))
            continue
        print('  %-11d %-20s 缺口合计 %-8d 起点 %d 个：%s'
              % (code, group_name(con, code), total, len(picked),
                 ', '.join('%s(+%d)' % (p['start'], p['gap_len']) for p in picked)))
        groups.append({'group': code, 'group_name': group_name(con, code),
                       'gap_total': total, 'holes': nholes, 'points': picked})
    con.close()

    print('合计：缺口 %d 条，起点 %d 个（每请求最多 799 条 -> 一轮预计取回 %d 条量级）'
          % (grand_total, grand_points, grand_points * 500))

    if a.budget_ms and a.budget_ms > 0:
        budget = a.budget_ms
        budget_src = '手工指定'
    else:
        req_est = sum(math.ceil(p['span'] / 550.0) for grp in groups for p in grp['points'])
        budget = int(max(60000, math.ceil(req_est * 7000 * 1.35)))
        budget_src = '自动估算（%d 次请求 x 7s x 1.35）' % req_est
    print('预算：%d ms（%.0f 分钟，%s）' % (budget, budget / 60000.0, budget_src))

    if a.print_only:
        return 0

    plan = {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'window_days': a.days,
        'min_gap': a.min_gap,
        'budget_ms': budget,
        'request_timeout_ms': 25000,
        'count_range': [320, 799],
        'interval_range_ms': [2000, 9000],
        'note': '由 tools/gap_plan.py 生成；selfloader.mjs 按此表按 seq 取回服务端消息，'
                '取回的消息由 QQ 自己落进本地 nt_msg.db 并存副本 filled-messages.jsonl。只读，不发消息。',
        'groups': groups,
    }
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, 'w', encoding='utf-8') as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    print('已写出：%s' % a.out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
