#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用完就删：把加载器的取回副本（filled-messages.jsonl）清理掉。

副本的**唯一用途**是跨「注入 → 重建索引 → 导入」这个窗口保住取回的数据。
一旦 `import_filled.py` 确认导入完成（新增 0 / 已存在 N），它的使命就结束了。

**删除前必须校验「副本 ⊆ 索引」**——差集不为空说明有数据只存在于副本（QQ 没落盘），
此时删掉就是丢数据，工具会拒绝并提示先跑 `import_filled.py`。

用法：
    python tools/purge_filled.py              # 校验通过就删（jsonl + .gz）
    python tools/purge_filled.py --dry-run    # 只报告，不删
    python tools/purge_filled.py --keep-gz    # 保留 .gz，只删 jsonl
    python tools/purge_filled.py --force      # 跳过校验强制删（确认数据另有备份时才用）
"""
import argparse
import gzip
import json
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'scripts'))
try:
    from ntqq_core import INDEX_DB as DEFAULT_DB
except Exception:
    DEFAULT_DB = r'D:\QQChatCache\data\index.db'


def _default_inj_out():
    for rel in (('..', 'injector', 'out'),
                ('..', '..', 'injector', 'out')):
        cand = os.path.normpath(os.path.join(HERE, *rel))
        if os.path.isdir(os.path.dirname(cand)):
            return cand
    return os.path.normpath(os.path.join(HERE, '..', 'injector', 'out'))


_INJ_OUT = _default_inj_out()
DEFAULT_JSONL = os.path.join(_INJ_OUT, 'filled-messages.jsonl')
PURGE_LOG = os.path.join(_INJ_OUT, 'purge-log.txt')


def iter_keys(path):
    op = gzip.open if path.endswith('.gz') else open
    with op(path, 'rt', encoding='utf-8-sig', errors='replace') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                yield (int(r['group']), int(r['seq']))
            except Exception:
                continue


def verify_subset(db_path, jsonl_path):
    """返回 (keys_in_jsonl, keys_in_db, missing_keys)。"""
    con = sqlite3.connect('file:%s?mode=ro' % db_path.replace('\\', '/'), uri=True)
    have = set((int(g), int(s)) for g, s in con.execute('select group_code, seq from messages where seq is not null'))
    con.close()
    keys = set(iter_keys(jsonl_path))
    return keys, have, keys - have


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--jsonl', default=DEFAULT_JSONL)
    ap.add_argument('--db', default=DEFAULT_DB)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--keep-gz', action='store_true')
    ap.add_argument('--force', action='store_true')
    a = ap.parse_args()

    targets = [a.jsonl, a.jsonl + '.gz']
    existing = [t for t in targets if os.path.exists(t)]
    if not existing:
        print('副本不存在，无需清理：%s' % a.jsonl)
        return 0
    total_mb = sum(os.path.getsize(t) for t in existing) / 1048576.0
    print('待清理：%s（%.2f MB）' % (', '.join(os.path.basename(t) for t in existing), total_mb))

    if not a.force and os.path.exists(a.jsonl):
        keys, have, missing = verify_subset(a.db, a.jsonl)
        print('  副本唯一键 %d ｜ 索引键 %d ｜ 只在副本里 %d' % (len(keys), len(have), len(missing)))
        if missing:
            print('  ❌ 拒绝删除：有 %d 条只存在于副本（QQ 未落盘）。先跑：' % len(missing))
            print('       python tools/import_filled.py')
            for k in list(missing)[:5]:
                print('         %s' % (k,))
            return 2
        print('  ✅ 副本 ⊆ 索引，删除不丢数据')

    if a.dry_run:
        print('  （--dry-run，未删除）')
        return 0

    removed = []
    for t in existing:
        if a.keep_gz and t.endswith('.gz'):
            continue
        os.remove(t)
        removed.append(os.path.basename(t))
    try:
        os.makedirs(os.path.dirname(PURGE_LOG), exist_ok=True)
        with open(PURGE_LOG, 'a', encoding='utf-8') as f:
            f.write('%s  删除 %s（%.2f MB）\n'
                    % (time.strftime('%Y-%m-%d %H:%M:%S'), ', '.join(removed), total_mb))
    except Exception:
        pass
    print('  已删除：%s（释放 %.2f MB）' % (', '.join(removed), total_mb))
    print('  记录：%s' % PURGE_LOG)
    return 0


if __name__ == '__main__':
    sys.exit(main())
