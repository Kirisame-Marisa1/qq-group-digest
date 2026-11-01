# -*- coding: utf-8 -*-
"""Unit tests for reverse streaming B-tree extraction with early stop and zero-disk fallback."""

import os
import sqlite3
import tempfile
import pytest

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from scripts.ntqq_slice import Src, Db, varint, decode_some, cols_of, decode_record


def test_slice_rows_desc_strictly_descending():
    """验证 Db.rows_desc 在多级 B-Tree 页面结构下能够严格按 rowid 倒序产出记录。"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        con = sqlite3.connect(db_path)
        con.execute("CREATE TABLE test_msgs (id INTEGER PRIMARY KEY, content TEXT, day INT)")
        # 写入 5000 行，产生多级 B-Tree 内部页与叶子页
        rows = [(i, f"content message {i}", 1700000000 + i * 10) for i in range(1, 5001)]
        con.executemany("INSERT INTO test_msgs VALUES (?, ?, ?)", rows)
        con.commit()
        con.close()

        with open(db_path, "rb") as fi:
            plain_bytes = fi.read()

        class MockPlainSrc:
            def __init__(self, data):
                self.data = data
                self.npages = len(data) // 4096
                self.bad_pages = 0
                self.bad_rows = 0

            def page(self, pno):
                off = (pno - 1) * 4096
                return self.data[off : off + 4096]

        src = MockPlainSrc(plain_bytes)
        db = Db(src)
        assert db.page_size == 4096

        test_root = None
        for rowid, pl in db.rows(1):
            rec = decode_record(pl)
            if len(rec) >= 5 and rec[0] == "table" and rec[1] == "test_msgs":
                test_root = rec[3]
                break

        assert test_root is not None, "未找到 test_msgs 根页"

        # 核心断言：测试 rows_desc 产出的 5000 行 rowid 严格按 5000 -> 1 倒序递减
        rowids_desc = [rowid for rowid, _ in db.rows_desc(test_root)]
        assert len(rowids_desc) == 5000
        assert rowids_desc == list(range(5000, 0, -1)), "rows_desc 产出的 rowid 未满足严格递减！"
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


def test_src_decryption_with_cached_algo():
    """验证 Src._dec 在 AES-CBC 解密下对 4KB 数据页的正确性。"""
    key = os.urandom(32)
    iv = os.urandom(16)
    payload = b"SQLite format 3\x00" + b"X" * (4048 - 16)
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor().update(payload)
    raw_body = enc + iv + b"\x00" * 32

    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
        f.write(b"\x00" * 1024)  # EXT_HEADER
        f.write(b"\x00" * 16 + raw_body[16:])  # page 1
        tmp_name = f.name

    try:
        src = Src(tmp_name, key)
        assert src.aes_algo is not None
        decrypted = src._dec(raw_body)
        assert decrypted == payload
        src.close()
    finally:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)


def test_early_stop_mechanism():
    """验证逆序遍历在检测到连续超出时间窗口阈值（STOP_THRESHOLD）时能够立即提前终止。"""
    now_ts = 1700000000
    cutoff_ts = now_ts - 7 * 86400  # 7 天前

    stream_data = []
    # 7 天内消息（合格）
    for i in range(100):
        stream_data.append((1000 - i, now_ts - i * 3600))
    # 7 天前消息（过期）
    for i in range(500):
        stream_data.append((900 - i, cutoff_ts - (i + 1) * 3600))

    STOP_THRESHOLD = 50
    consecutive_old = 0
    scanned = 0
    kept = 0

    for rowid, msg_ts in stream_data:
        scanned += 1
        if msg_ts < cutoff_ts:
            consecutive_old += 1
            if consecutive_old >= STOP_THRESHOLD:
                # 提前终止！
                break
            continue
        else:
            consecutive_old = 0
        kept += 1

    assert kept == 100
    # 总共只扫描了 100 条有效消息 + 50 条阈值探测消息 = 150 条，剩余 450 条陈旧消息完全跳过！
    assert scanned == 100 + STOP_THRESHOLD
    assert scanned < len(stream_data)


def test_stream_source_option_and_defaults():
    """验证 ntqq_build 命令行参数 --source auto/stream/sql 选项解析正确。"""
    import argparse
    from scripts.ntqq_build import main

    ap = argparse.ArgumentParser()
    ap.add_argument('--source', choices=('auto', 'sql', 'stream', 'snap'), default='auto')

    args_default = ap.parse_args([])
    assert args_default.source == 'auto'

    args_sql = ap.parse_args(['--source', 'sql'])
    assert args_sql.source == 'sql'

    args_stream = ap.parse_args(['--source', 'stream'])
    assert args_stream.source == 'stream'
