#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NTQQ SQLCipher 流式解密。
关键：page1 明文里前 16 字节就是 SQLite 文件头本身，必须原样保留
（尤其 offset 20 = reserved space = 80，写错会让 SQLite 误判可用页大小）。
格式常量取自 NapNeko/qq_dump_db (MIT)，已逐行审计。"""
import argparse, json, os, struct, sys, time
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


def decrypt_db(key_map_path, db_name, out_path, max_mb=0):
    """流式解密指定 NTQQ SQLCipher 数据库到 out_path，支持 DPAPI 加密与明文 key_map。"""
    global KEY
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

    total = os.path.getsize(src)
    pages = ((min(total, int(max_mb * 1048576)) if max_mb else total) - EXT_HEADER) // PAGE_SIZE
    d = os.path.dirname(os.path.abspath(out_path))
    if d:
        os.makedirs(d, exist_ok=True)
    log('src=%s size=%.2fGB pages=%d' % (src, total / 1073741824.0, pages))
    t0 = time.time()
    with open(src, 'rb') as fi, open(out_path, 'wb') as fo:
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
    a = ap.parse_args()
    decrypt_db(key_map_path=a.key_map, db_name=a.db, out_path=a.out, max_mb=a.max_mb)


if __name__ == '__main__':
    main()

