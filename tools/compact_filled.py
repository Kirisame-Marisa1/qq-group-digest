#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""压缩 `filled-messages.jsonl`：按 (群, seq) 去重，可选 gzip。

为什么需要：加载器的落盘副本是**追加写**的，而多轮补缺口或重叠扫描段可能产生重复行。
去重后**行数会减少，但内容不丢**（同一 (群,seq) 的重复行内容一致）。

用法：
    python tools/compact_filled.py                 # 去重写回（保留最后一条）
    python tools/compact_filled.py --gzip          # 另存 .jsonl.gz（原文件保留）
    python tools/compact_filled.py --stats         # 只看统计，不写
"""
import argparse
import gzip
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _default_jsonl():
    for rel in (('..', 'injector', 'out', 'filled-messages.jsonl'),
                ('..', '..', 'injector', 'out', 'filled-messages.jsonl')):
        cand = os.path.normpath(os.path.join(HERE, *rel))
        if os.path.isdir(os.path.dirname(os.path.dirname(cand))):
            return cand
    return os.path.normpath(os.path.join(HERE, '..', 'injector', 'out', 'filled-messages.jsonl'))


DEFAULT = _default_jsonl()


def compact_file(jsonl_path, keep_text=True, do_gzip=False, stats_only=False):
    """对 jsonl_path 按 (group, seq) 去重写回，返回 (total_lines, unique_count, bad_lines)。"""
    if not os.path.exists(jsonl_path):
        return 0, 0, 0
    recs = {}
    n = bad = 0
    with open(jsonl_path, encoding='utf-8-sig', errors='replace') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            n += 1
            try:
                r = json.loads(line)
                key = (int(r['group']), int(r['seq']))
            except Exception:
                bad += 1
                continue
            recs[key] = r
    if stats_only:
        return n, len(recs), bad

    keys = ['seq', 'ts', 'sender_uid', 'sender_name', 'kind', 'text', 'group']
    if not keep_text:
        keys.remove('text')

    tmp = jsonl_path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as out:
        for r in recs.values():
            out.write(json.dumps({k: r.get(k, '') for k in keys}, ensure_ascii=False) + '\n')
    os.replace(tmp, jsonl_path)

    if do_gzip:
        gz = jsonl_path + '.gz'
        with open(jsonl_path, 'rb') as src, gzip.open(gz, 'wb', compresslevel=6) as dst:
            dst.write(src.read())
    return n, len(recs), bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--jsonl', default=DEFAULT)
    ap.add_argument('--gzip', action='store_true', help='另存 .gz（原文件保留）')
    ap.add_argument('--stats', action='store_true', help='只统计，不写回')
    ap.add_argument('--keep-text', action='store_true', default=True,
                    help='保留 text 字段（默认保留；仅在你想只留 seq 骨架时用 --no-text）')
    ap.add_argument('--no-text', dest='keep_text', action='store_false')
    a = ap.parse_args()

    if not os.path.exists(a.jsonl):
        print('没有 %s' % a.jsonl)
        return 0

    before = os.path.getsize(a.jsonl)
    n, uniq, bad = compact_file(a.jsonl, keep_text=a.keep_text, do_gzip=a.gzip, stats_only=a.stats)
    print('读入 %d 行 ｜ 唯一 (群,seq) %d ｜ 坏行 %d ｜ 原文件 %.1f MB'
          % (n, uniq, bad, before / 1048576))
    if a.stats:
        return 0

    after = os.path.getsize(a.jsonl)
    print('去重写回：%.1f MB -> %.1f MB（省 %.0f%%）'
          % (before / 1048576, after / 1048576, 100.0 * (before - after) / max(1, before)))

    if a.gzip and os.path.exists(a.jsonl + '.gz'):
        gsz = os.path.getsize(a.jsonl + '.gz')
        print('gzip 另存：%s（%.1f MB，再省 %.0f%%）' % (a.jsonl + '.gz', gsz / 1048576,
              100.0 * (after - gsz) / after if after else 0))
    return 0


if __name__ == '__main__':
    sys.exit(main())
