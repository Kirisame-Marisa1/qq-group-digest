#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NTQQ SQLCipher 流式解密。
关键：page1 明文里前 16 字节就是 SQLite 文件头本身，必须原样保留
（尤其 offset 20 = reserved space = 80，写错会让 SQLite 误判可用页大小）。
格式常量取自 NapNeko/qq_dump_db (MIT)，已逐行审计。"""
import argparse, json, os, struct, sys, time
from contextlib import nullcontext
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

EXT_HEADER, PAGE_SIZE, SALT_SIZE = 1024, 4096, 16
IV_SIZE, RESERVE = 16, 48
KEY = None


def plain(body, key=None):
    k = key if key is not None else KEY
    enc = body[:len(body) - RESERVE]
    iv = body[len(body) - RESERVE:len(body) - RESERVE + IV_SIZE]
    d = Cipher(algorithms.AES(k), modes.CBC(iv)).decryptor()
    return d.update(enc) + d.finalize()


def decrypt_db(key_map_path, db_name, out_path, max_mb=0, use_snapshot=True):
    """流式解密指定 NTQQ SQLCipher 数据库到 out_path，支持 DPAPI 加密与明文 key_map。"""
    global KEY
    for _s in (sys.stdout, sys.stderr):          # 控制台可能是 GBK，别让中文输出崩掉
        try:
            _s.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from ntqq_core import unwrap_key_hex, log

    with open(key_map_path, encoding='utf-8') as fh:
        km = json.load(fh)
    src = None
    key_bytes = None
    for sh, files in km['salt_files'].items():
        for f in files:
            if os.path.basename(f) == db_name:
                src = f
                key_bytes = bytes.fromhex(unwrap_key_hex(km['key_map'][sh]))
    if src is None or key_bytes is None:
        log('ERROR: %s not in key_map' % db_name)
        raise SystemExit(1)
    KEY = key_bytes

    # 使用 SafeDBSnapshot 进行安全热拷贝
    snapshot_ctx = nullcontext()
    if use_snapshot:
        try:
            from ntqq_snapshot import SafeDBSnapshot
            snapshot_ctx = SafeDBSnapshot(src)
        except Exception as e:
            log('warn: 创建快照失败，回退直接读取: %s' % e)

    with snapshot_ctx as snap_src:
        actual_src = str(snap_src) if snap_src else src
        total = os.path.getsize(actual_src)
        pages = ((min(total, int(max_mb * 1048576)) if max_mb else total) - EXT_HEADER) // PAGE_SIZE
        d = os.path.dirname(os.path.abspath(out_path))
        if d:
            os.makedirs(d, exist_ok=True)
        log('src=%s size=%.2fGB pages=%d' % (actual_src, total / 1073741824.0, pages))
        t0 = time.time()
        with open(actual_src, 'rb') as fi, open(out_path, 'wb') as fo:
            fi.seek(EXT_HEADER)
            for i in range(pages):
                raw = fi.read(PAGE_SIZE)
                if len(raw) < PAGE_SIZE:
                    break
                if i == 0:
                    pt = plain(raw[SALT_SIZE:], key_bytes)          # pt[0:16] 就是原始 SQLite 头
                    page = bytes(b'SQLite format 3\x00' + pt)
                else:
                    page = plain(raw, key_bytes)
                if len(page) < PAGE_SIZE:
                    page += b'\x00' * (PAGE_SIZE - len(page))
                fo.write(page)
                if (i & 8191) == 0 or i + 1 >= pages:
                    print('@@PROGRESS|{"stage":"decrypt","file":"%s","done":%d,"total":%d,"pct":%.1f}'
                          % (db_name, i + 1, pages, (i + 1) * 100.0 / max(1, pages)), flush=True)

    sz = os.path.getsize(out_path)
    log('done %.1fs out=%.2fGB' % (time.time() - t0, sz / 1073741824.0))
    with open(out_path, 'rb') as f:
        h = f.read(32)
    if len(h) >= 32:
        log('   header: page_size=%d reserved=%d db_size_pages=%d' % (
            int.from_bytes(h[16:18], 'big'), h[20], int.from_bytes(h[28:32], 'big')))
    return out_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--key-map', required=True)
    ap.add_argument('--db', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--max-mb', type=float, default=0)
    ap.add_argument('--no-snapshot', action='store_true', default=False, help='禁用 WAL 安全热快照拷贝')
    a = ap.parse_args()
    decrypt_db(key_map_path=a.key_map, db_name=a.db, out_path=a.out, max_mb=a.max_mb, use_snapshot=not a.no_snapshot)


if __name__ == '__main__':
    main()

