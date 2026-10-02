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


def plain(body):
    enc = body[:len(body) - RESERVE]
    iv = body[len(body) - RESERVE:len(body) - RESERVE + IV_SIZE]
    d = Cipher(algorithms.AES(KEY), modes.CBC(iv)).decryptor()
    return d.update(enc) + d.finalize()


def main():
    global KEY
    ap = argparse.ArgumentParser()
    ap.add_argument('--key-map', required=True)
    ap.add_argument('--db', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--max-mb', type=float, default=0)
    a = ap.parse_args()

    km = json.load(open(a.key_map, encoding='utf-8'))
    src = None
    for sh, files in km['salt_files'].items():
        for f in files:
            if os.path.basename(f) == a.db:
                src = f; KEY = bytes.fromhex(km['key_map'][sh])
    if src is None:
        print('ERROR: %s not in key_map' % a.db); sys.exit(1)

    total = os.path.getsize(src)
    pages = ((min(total, int(a.max_mb * 1048576)) if a.max_mb else total) - EXT_HEADER) // PAGE_SIZE
    d = os.path.dirname(os.path.abspath(a.out))
    if d: os.makedirs(d, exist_ok=True)
    print('src=%s size=%.2fGB pages=%d' % (src, total / 1073741824.0, pages))
    t0 = time.time()
    with open(src, 'rb') as fi, open(a.out, 'wb') as fo:
        fi.seek(EXT_HEADER)
        for i in range(pages):
            raw = fi.read(PAGE_SIZE)
            if len(raw) < PAGE_SIZE: break
            if i == 0:
                pt = plain(raw[SALT_SIZE:])          # pt[0:16] 就是原始 SQLite 头
                page = bytes(b'SQLite format 3\x00' + pt)
            else:
                page = plain(raw)
            if len(page) < PAGE_SIZE:
                page += b'\x00' * (PAGE_SIZE - len(page))
            fo.write(page)
    sz = os.path.getsize(a.out)
    print('done %.1fs out=%.2fGB' % (time.time() - t0, sz / 1073741824.0))
    with open(a.out, 'rb') as f:
        h = f.read(32)
    print('   header: page_size=%d reserved=%d db_size_pages=%d' % (
        int.from_bytes(h[16:18], 'big'), h[20], int.from_bytes(h[28:32], 'big')))


if __name__ == '__main__':
    main()
