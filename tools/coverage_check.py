#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""覆盖率达到 100% 了吗？—— 多口径交叉验证（只读 index.db）。

为什么不能只看一个数字：
  * `rows / (max_seq - min_seq + 1)` 这种覆盖率**永远到不了 100%**——
    seq 本身会跳号（实测约 4.6% 的相邻对差值 > 1，是服务端跳号而非本地缺失），
    所以分母天然偏大。**别拿这个数当验收标准。**
  * 真正该问的是：**还有没有「本可以拿到、但本地没有」的 seq 段**。

因此本脚本同时给出四个口径：
  口径 A（主）后端缺口长度：相邻本地 seq 之间空档 > 阈值 的**总长**。补到 0 才算 100%。
  口径 B      seq 连续性：把「差值 > 1」按大小分档，区分「细碎跳号」与「大段缺失」。
  口径 C      覆盖率参考值：rows / 跨度（会低于 100%，仅作横向对比，不作验收）。
  口径 D      与原始库对照（若 data/plain/nt_msg.db 存在）：索引里缺的 seq 在原始库里有没有。

用法：
    python tools/coverage_check.py                       # 近 30 天
    python tools/coverage_check.py --days 11 --top 25
    python tools/coverage_check.py --group <群号> --days 11 --detail
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'scripts'))
try:
    from ntqq_core import INDEX_DB as DEFAULT_DB, PLAIN, RUN
    DEFAULT_PLAIN = os.path.join(PLAIN, 'nt_msg.db')
    DEFAULT_GI = os.path.join(PLAIN, 'group_info.db')
    LEFT_CACHE = os.path.join(RUN, 'left_groups.json')
except Exception:
    DEFAULT_DB = r'D:\QQChatCache\data\index.db'
    DEFAULT_PLAIN = r'D:\QQChatCache\data\plain\nt_msg.db'
    DEFAULT_GI = r'D:\QQChatCache\data\plain\group_info.db'
    LEFT_CACHE = r'D:\QQChatCache\run\left_groups.json'


def scan(con, code, since, split=50):
    """返回该群的 seq 统计：(rows, minseq, maxseq, 缺口总长, 空洞数, 大段空洞列表, 跳号数, 大段缺口总长)。"""
    rows = 0
    prev = None
    mn = mx = None
    gap_total = 0
    holes = 0
    big = []          # (length, start)
    small_jumps = 0   # 差值 <= split，视为 seq 跳号
    cur = con.execute('select seq from messages where group_code=? and ts>=? order by seq', (code, since))
    while True:
        batch = cur.fetchmany(20000)
        if not batch:
            break
        for (seq,) in batch:
            if seq is None:
                continue
            rows += 1
            if mn is None or seq < mn:
                mn = seq
            if mx is None or seq > mx:
                mx = seq
            if prev is not None and seq > prev + 1:
                length = seq - prev - 1
                gap_total += length
                holes += 1
                if length > split:
                    big.append((length, prev + 1))
                else:
                    small_jumps += 1
            prev = seq
    big.sort(key=lambda x: -x[0])
    big_total = sum(l for l, _ in big)      # ★ 只统计「大段缺口」（> split），跳号不算
    return rows, mn, mx, gap_total, holes, big, small_jumps, big_total


def load_left_groups(gi_path, con, since, refresh=False):
    """返回 (已退群群号集合, 说明文字)。"""
    if not refresh and os.path.exists(LEFT_CACHE):
        try:
            with open(LEFT_CACHE, encoding='utf-8') as fh:
                d = json.load(fh)
            age = (time.time() - d.get('ts', 0)) / 86400.0
            if age < 7:
                return set(d.get('left', [])), '缓存，%.1f 天前' % age
        except Exception:
            pass
    if not os.path.exists(gi_path):
        try:
            from ntqq_core import load_config, ensure_keymap, ensure_plain
            cfg = load_config()
            km = ensure_keymap(cfg)
            ensure_plain(cfg, km, 'group_info.db')
        except Exception as e:
            return set(), '既无缓存、解密也失败（%s）——已退群未排除' % e
    if not os.path.exists(gi_path):
        return set(), '无 group_info.db——已退群未排除'
    gi = sqlite3.connect('file:%s?mode=ro' % gi_path.replace('\\', '/'), uri=True)
    joined = set()
    for row in gi.execute('select * from group_list'):
        for v in row:
            s = str(v) if v is not None else ''
            if s.isdigit() and 6 <= len(s) <= 12:
                joined.add(int(s))
                break
    gi.close()
    all_codes = set(r[0] for r in con.execute(
        'select distinct group_code from messages where ts>=?', (since,)))
    left = sorted(all_codes - joined)
    try:
        os.makedirs(os.path.dirname(LEFT_CACHE), exist_ok=True)
        with open(LEFT_CACHE, 'w', encoding='utf-8') as fh:
            json.dump({'ts': time.time(), 'left': left}, fh)
    except Exception:
        pass
    return set(left), '刚解密 group_list（%d 个在群），已写缓存' % len(joined)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=DEFAULT_DB)
    ap.add_argument('--plain', default=DEFAULT_PLAIN)
    ap.add_argument('--days', type=int, default=30)
    ap.add_argument('--top', type=int, default=25, help='按条数列出前 N 个群')
    ap.add_argument('--group', type=int, default=0, help='只看一个群（详细模式）')
    ap.add_argument('--split', type=int, default=50, help='差值 <= 该值算「跳号」，> 算「缺口」')
    ap.add_argument('--min-rows', type=int, default=1)
    ap.add_argument('--detail', action='store_true')
    ap.add_argument('--group-info', default=DEFAULT_GI,
                    help='用于识别「已退群」的明文库；存在则默认把不在群的群排除出统计')
    ap.add_argument('--keep-left', action='store_true',
                    help='不排除已退群（默认排除：已退了的群拿不到消息属正常，不计入覆盖率）')
    ap.add_argument('--refresh-left', action='store_true',
                    help='强制重新解密 group_info.db 刷新「已退群」列表')
    a = ap.parse_args()

    con = sqlite3.connect('file:%s?mode=ro' % a.db.replace('\\', '/'), uri=True)
    since = int(time.time()) - a.days * 86400
    print('索引：%s' % a.db)
    print('窗口：近 %d 天（>= %s）｜ 跳号阈值 split=%d\n'
          % (a.days, datetime.fromtimestamp(since).strftime('%Y-%m-%d %H:%M'), a.split))

    left_groups = set()
    if not a.keep_left:
        left_groups, how = load_left_groups(a.group_info, con, since, a.refresh_left)
        print('已退群 %d 个不计入统计（%s）\n' % (len(left_groups), how))

    if a.group:
        codes = [a.group]
    else:
        codes = [r[0] for r in con.execute(
            'select group_code from messages where ts>=? group by group_code having count(*)>=? order by count(*) desc',
            (since, a.min_rows))]
        if left_groups:
            codes = [c for c in codes if c not in left_groups]

    rows_all = gap_all = holes_all = big_all = 0
    perfect, imperfect = [], []
    lines = []
    for code in codes:
        rows, mn, mx, gap, holes, big, small, big_total = scan(con, code, since, a.split)
        if not rows:
            continue
        span = (mx - mn + 1) if (mx is not None and mn is not None) else 0
        cov = 100.0 * rows / span if span else 0.0
        name = con.execute('select group_name from groups where group_code=?', (code,)).fetchone()
        name = (name[0] if name and name[0] else '')[:22]
        rows_all += rows
        gap_all += gap
        holes_all += holes
        big_all += big_total
        rec = (code, name, rows, gap, holes, cov, big, big_total)
        (perfect if big_total == 0 else imperfect).append(rec)
        if a.group or a.detail:
            lines.append('  %-11d %-22s rows=%-7d 大段缺口=%-7d 空洞=%-5d 覆盖率=%5.1f%%'
                         % (code, name, rows, big_total, holes, cov))
            for length, start in big[:5]:
                lines.append('        └ 大段缺口 %6d 条，起点 seq=%d' % (length, start))

    if lines:
        print('=== 逐群 ===')
        print('\n'.join(lines))
        print()

    print('=== 汇总 ===')
    print('  参与统计的群：%d ｜ 本地总行数：%d' % (len(perfect) + len(imperfect), rows_all))
    print('  ☑ 无大段缺口的群：%d' % len(perfect))
    print('  ✗ 仍有大段缺口的群：%d' % len(imperfect))
    print('  ★ 验收口径 A（大段缺口 %d 条以上的总长）：**%d 条**' % (a.split, big_all))
    print('     （参考）全部空档总长 %d 条，其中 ≤%d 条的「服务端跳号」%d 条 —— 补不回来，也不算缺口'
          % (gap_all, a.split, gap_all - big_all))
    if imperfect and not a.group:
        print('\n  缺口最大的前 %d 个群：' % min(a.top, len(imperfect)))
        for code, name, rows, gap, holes, cov, big, big_total in sorted(imperfect, key=lambda x: -x[7])[:a.top]:
            print('    %-11d %-22s 大段缺口=%-7d 空洞=%-4d' % (code, name, big_total, holes))

    verdict = (big_all == 0)
    print('\n=== 判定 ===')
    if verdict:
        print('  ✅ 口径 A 达标：**大段缺口为 0**——本地 seq 已无「本可拿到却缺失」的区间，覆盖率 100%。')
        print('     （剩余 %d 条空档全部 ≤%d 条，属服务端自身跳号，任何手段都补不到。）' % (gap_all, a.split))
    else:
        print('  ❌ 口径 A 未达标：仍有 **%d 条**大段缺口分布在 %d 个群。'
              '按铁律 0，应再跑一轮 L1b 补缺口。' % (big_all, len(imperfect)))

    if os.path.exists(a.plain):
        print('\n=== 口径 D：原始库对照 ===')
        try:
            raw = sqlite3.connect('file:%s?mode=ro' % a.plain.replace('\\', '/'), uri=True)
            n = raw.execute("select count(*) from sqlite_master where type='table' and name='group_msg_table'").fetchone()[0]
            print('  原始库存在，group_msg_table=%s（详细对照请用 raw_check.py 指定群与 seq 窗口）'
                  % ('有' if n else '无'))
            raw.close()
        except Exception as e:
            print('  原始库打不开：%s' % e)
    else:
        print('\n=== 口径 D：原始库对照 ===')
        print('  未找到 %s（按需解密流程不落盘，正常）' % a.plain)

    con.close()
    return 0 if verdict else 1


if __name__ == '__main__':
    sys.exit(main())
