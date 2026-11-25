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
    """验证 ntqq_build.should_early_stop_stream 在检测到连续超出窗口阈值时触发提前终止。"""
    from scripts.ntqq_build import should_early_stop_stream

    scan_day0 = 20261025
    stream_days = [20261026] * 5 + [20261020] * 100
    consecutive_old = 0
    kept = 0
    scanned = 0

    for msg_day in stream_days:
        scanned += 1
        should_break, consecutive_old, skip_row = should_early_stop_stream(
            msg_day, scan_day0, consecutive_old, kept, stop_threshold=20
        )
        if should_break:
            break
        if not skip_row:
            kept += 1

    assert kept == 5
    assert scanned == 5 + 20
    assert scanned < len(stream_days)


def test_stream_source_option_and_defaults():
    """验证真实 ntqq_build.build_argument_parser 命令行参数解析。"""
    from scripts.ntqq_build import build_argument_parser

    ap = build_argument_parser()

    args_default = ap.parse_args([])
    assert args_default.source == 'auto'
    assert args_default.window is None
    assert args_default.only_groups is None

    args_sql = ap.parse_args(['--source', 'sql', '--only-groups', '123,456'])
    assert args_sql.source == 'sql'
    assert args_sql.only_groups == '123,456'

    args_stream = ap.parse_args(['--source', 'stream', '--window', '30d'])
    assert args_stream.source == 'stream'
    assert args_stream.window == '30d'
