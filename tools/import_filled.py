#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把自建加载器落盘的取回消息（filled-messages.jsonl）合并进 index.db。

**为什么需要它**：QQ 是否把服务端取回的消息写进它自己的 `nt_msg.db`，**不受我们控制**。
实测一轮取回 11.3 万条，本地库只长了约 3.7 万行——覆盖率因此被 QQ 的落盘策略卡住。
所以加载器现在**自己也存一份**（`injector\\out\\filled-messages.jsonl`），本脚本把它并进索引，
覆盖率就不再取决于 QQ 愿不愿意写库。

**什么时候跑**：每次 `ntqq_build.py` **之后**（build 会重建 messages 表，把导入的数据冲掉；
外部 JSONL 是持久的，重跑本脚本即可恢复）。这就是 `refill_finish.ps1` 里的收尾顺序。

用法：
    python tools/import_filled.py                  # 合并 + 报统计
    python tools/import_filled.py --dry-run        # 只报会新增多少条，不写
    python tools/import_filled.py --jsonl <路径>
    python tools/import_filled.py --report-only    # 只统计 JSONL 本身（不碰索引）
"""
import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'scripts'))
try:
    from ntqq_core import INDEX_DB as DEFAULT_DB
except Exception:
    DEFAULT_DB = r'D:\QQChatCache\data\index.db'


def _default_jsonl():
    for rel in (('..', 'injector', 'out', 'filled-messages.jsonl'),
                ('..', '..', 'injector', 'out', 'filled-messages.jsonl')):
        cand = os.path.normpath(os.path.join(HERE, *rel))
        if os.path.isdir(os.path.dirname(os.path.dirname(cand))):
            return cand
    return os.path.normpath(os.path.join(HERE, '..', 'injector', 'out', 'filled-messages.jsonl'))


DEFAULT_JSONL = _default_jsonl()


def load(path):
    """读 JSONL，(group, seq) -> 记录（同键后者覆盖前者）。坏行计数返回。"""
    recs = {}
    bad = 0
    total = 0
    if not os.path.exists(path):
        return recs, 0, 0
    # utf-8-sig: PowerShell 的 Set-Content -Encoding UTF8 会写 BOM，带 BOM 的首行会解析失败
    with open(path, encoding='utf-8-sig', errors='replace') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            total += 1
            try:
                r = json.loads(line)
            except Exception:
                bad += 1
                continue
            try:
                key = (int(r['group']), int(r['seq']))
            except Exception:
                bad += 1
                continue
            if not r.get('ts'):
                bad += 1
                continue
            recs[key] = r
    return recs, bad, total


def import_records(db_path, recs, dry_run=False):
    """将去重后的 recs 字典 ((group, seq) -> dict) 幂等写入 index.db，返回 (inserted, skipped, touched_groups)。"""
    by_group = defaultdict(int)
    for (g, _s) in recs:
        by_group[g] += 1

    con = sqlite3.connect(db_path, timeout=60)
    con.execute('PRAGMA journal_mode=WAL')

    gname = {r[0]: (r[1] or '') for r in con.execute('select group_code, group_name from groups')}

    inserted = skipped = 0
    touched = defaultdict(lambda: [0, None, None])       # group -> [新增数, first_ts, last_ts]
    for g in by_group:
        have = set(r[0] for r in con.execute('select seq from messages where group_code=?', (g,)))
        rows = []
        for (gg, seq), r in recs.items():
            if gg != g:
                continue
            if seq in have:
                skipped += 1
                continue
            ts = int(r['ts'])
            dt = datetime.fromtimestamp(ts)
            # msg_id 是 INTEGER PRIMARY KEY（QQ 用的是 19 位正数）。
            # 用**负整数**自己造一个：-(群号 × 1e9 + seq) —— 与 QQ 的正数完全隔离，
            # 同一 (群,seq) 永远得到同一个 id，所以重复导入是幂等的。
            rows.append((
                -(g * 1000000000 + seq),
                ts, dt.strftime('%Y-%m-%d'), dt.hour,
                g, gname.get(g, ''),
                r.get('sender_uid', ''), 0, r.get('sender_name', ''),
                1, 0, 0, r.get('kind', 'other'), r.get('text', ''), None, seq,
            ))
        if rows:
            inserted += len(rows)
            touched[g][0] = len(rows)
            ts_all = [x[1] for x in rows]
            touched[g][1] = min(ts_all)
            touched[g][2] = max(ts_all)
            if not dry_run:
                con.executemany(
                    'insert into messages (msg_id, ts, day, hour, group_code, group_name, '
                    'sender_uid, sender_qq, sender_name, direction, msg_type, subtype, kind, '
                    'text, reply_seq, seq) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', rows)

    if not dry_run and inserted:
        for g, (n, lo, hi) in touched.items():
            cur = con.execute('select n, first_ts, last_ts from groups where group_code=?', (g,)).fetchone()
            if cur is None:
                con.execute('insert into groups (group_code, group_name, first_ts, last_ts, n) values (?,?,?,?,?)',
                            (g, gname.get(g, ''), lo, hi, n))
            else:
                con.execute('update groups set n=?, first_ts=min(coalesce(first_ts,?), ?), '
                            'last_ts=max(coalesce(last_ts,?), ?) where group_code=?',
                            (cur[0] + n, lo, lo, hi, hi, g))
        con.commit()
    con.close()
    return inserted, skipped, len(touched)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=DEFAULT_DB)
    ap.add_argument('--jsonl', default=DEFAULT_JSONL)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--report-only', action='store_true')
    a = ap.parse_args()

    recs, bad, total = load(a.jsonl)
    print('JSONL：%s' % a.jsonl)
    print('  原始行 %d ｜ 去重后唯一 (群,seq) %d ｜ 坏行 %d' % (total, len(recs), bad))
    if not recs:
        print('  没有可导入的记录（加载器还没落盘，或文件被清空）')
        return 0
    by_group = defaultdict(int)
    for (g, _s) in recs:
        by_group[g] += 1
    print('  涉及 %d 个群，最大的 5 个：%s'
          % (len(by_group), ', '.join('%d(%d)' % (g, n) for g, n in
                                      sorted(by_group.items(), key=lambda x: -x[1])[:5])))
    print('  时间范围：%s .. %s'
          % (datetime.fromtimestamp(min(r['ts'] for r in recs.values())).strftime('%Y-%m-%d %H:%M'),
             datetime.fromtimestamp(max(r['ts'] for r in recs.values())).strftime('%Y-%m-%d %H:%M')))
    if a.report_only:
        return 0

    inserted, skipped, n_touched = import_records(a.db, recs, dry_run=a.dry_run)
    print('\n%s：新增 %d 条 ｜ 已存在跳过 %d 条 ｜ 涉及 %d 个群'
          % ('[dry-run] 将会' if a.dry_run else '已', inserted, skipped, n_touched))
    if not a.dry_run and inserted:
        print('下一步：跑 coverage_check.py 验收覆盖率')
    return 0


if __name__ == '__main__':
    sys.exit(main())
