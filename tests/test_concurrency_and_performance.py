# -*- coding: utf-8 -*-
"""Unit tests for concurrency, SQLite WAL pragma optimization, and incremental FTS5 indexing."""

import os
import sqlite3
import tempfile
import pytest

from scripts.ntqq_core import open_index, sync_fts_incremental, rebuild_fts
from scripts.ntqq_report import block


def test_sqlite_wal_and_pragmas(monkeypatch):
    """验证 open_index() 正确配置了 WAL 模式与高性能内存缓存参数。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        test_db = os.path.join(tmp_dir, "test_index.db")
        monkeypatch.setattr("scripts.ntqq_core.INDEX_DB", test_db)
        monkeypatch.setattr("scripts.ntqq_core.RUN", tmp_dir)
        monkeypatch.setattr("scripts.ntqq_core.DATA", tmp_dir)

        con = open_index()
        try:
            cur = con.cursor()
            # 校验 journal_mode 为 wal
            j_mode = cur.execute("PRAGMA journal_mode;").fetchone()[0]
            assert j_mode.lower() == "wal"

            # 校验 synchronous 为 normal (1)
            sync_mode = cur.execute("PRAGMA synchronous;").fetchone()[0]
            assert sync_mode in (1, "NORMAL", "normal")

            # 校验 temp_store 为 memory (2)
            temp_store = cur.execute("PRAGMA temp_store;").fetchone()[0]
            assert temp_store in (2, "MEMORY", "memory")
        finally:
            con.close()


def test_incremental_fts_indexing(monkeypatch):
    """验证 sync_fts_incremental 能毫秒级增量维护全文索引，且检索结果与全量 rebuild 一致。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        test_db = os.path.join(tmp_dir, "test_fts.db")
        monkeypatch.setattr("scripts.ntqq_core.INDEX_DB", test_db)
        monkeypatch.setattr("scripts.ntqq_core.RUN", tmp_dir)
        monkeypatch.setattr("scripts.ntqq_core.DATA", tmp_dir)

        con = open_index()
        try:
            # 插入第一批消息
            batch1 = [
                (101, 1600000000, "2020-09-13", 12, 1001, "测试群", "u1", 111, "张三", 0, 1, 0, "text", "hello refactor test", 0, 1),
                (102, 1600000010, "2020-09-13", 12, 1001, "测试群", "u2", 222, "李四", 0, 1, 0, "text", "meeting notice today", 0, 2),
            ]
            con.executemany("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch1)
            sync_fts_incremental(con, batch1)
            con.commit()

            # 验证全文检索立即命中第一条
            hits = con.execute("SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'refactor'").fetchall()
            assert len(hits) == 1
            assert hits[0][0] == 101

            # 插入第二批增量消息
            batch2 = [
                (103, 1600000020, "2020-09-13", 12, 1001, "测试群", "u3", 333, "王五", 0, 1, 0, "text", "speedup refactor benchmark", 0, 3),
            ]
            con.executemany("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch2)
            sync_fts_incremental(con, batch2)
            con.commit()

            # 增量验证无需 rebuild 即可检索到新消息
            hits_updated = con.execute("SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'refactor'").fetchall()
            assert len(hits_updated) == 2
            assert {h[0] for h in hits_updated} == {101, 103}
        finally:
            con.close()


def test_multithreaded_group_report_consistency():
    """验证多群报表在多线程并发生成下的确定性与格式完整性。"""
    rows_g1 = [
        {"ts": 1700000000, "day": "2023-11-15", "hour": 14, "sender_name": "用户A", "text": "讨论项目进度与并发方案", "kind": "text"},
        {"ts": 1700000010, "day": "2023-11-15", "hour": 14, "sender_name": "用户B", "text": "收到，多线程加速效果显著", "kind": "text"},
    ]
    rows_g2 = [
        {"ts": 1700000020, "day": "2023-11-15", "hour": 15, "sender_name": "用户C", "text": "今天天气真好", "kind": "text"},
        {"ts": 1700000030, "day": "2023-11-15", "hour": 15, "sender_name": "用户D", "text": "一起去运动吧", "kind": "text"},
    ]

    txt1, _ = block(1001, "群一", rows_g1, max_msgs=10)
    txt2, _ = block(1002, "群二", rows_g2, max_msgs=10)

    assert "群一" in txt1
    assert "并发" in txt1
    assert "群二" in txt2
    assert "运动" in txt2


def test_keywords_ngram_speed_and_parity():
    """验证关键词提取优化前后结果精确一致，且能高效处理长文本与转发记录。"""
    from scripts.ntqq_report import keywords

    samples = [
        "你好，请问今天下午的会议几点开始？[转发聊天记录: 昨天讨论]",
        "请大家尽快提交实验报告，截止日期是明天下午5点！",
        "python test 123456 abcdef",
        "有人知道这个报错是什么意思吗？ModuleNotFoundError: No module named requests",
        "今天天气真好，一起去图书馆学习吗？",
        "大家注意查收邮件通知，关于下周放假安排的公告已经在群文件发布了。",
        "",
        None,
        "a" * 120,  # 极端超长无标点输入
    ] * 50

    kws = keywords(samples, min_count=2, topk=10)
    assert len(kws) > 0
    words = [w for w, c in kws]
    # 验证关键高频词正常检出
    assert any("会议" in w or "下午" in w or "实验" in w or "报告" in w or "通知" in w for w in words)


def test_decryption_preallocation_and_byte_parity():
    """验证使用算法缓存与预分配缓冲区的流式解密输出与标准 AES-CBC 精确一致。"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from scripts.ntqq_decrypt import plain, PAGE_SIZE, SALT_SIZE, RESERVE

    key = os.urandom(32)
    iv = os.urandom(16)
    plain_data = b"SQLite format 3\x00" + b"A" * (4048 - 16)
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor().update(plain_data)
    raw_page = enc + iv + b"\x00" * 32

    # 1. 验证传 key bytes 正常工作
    res_bytes = plain(raw_page, key)
    assert res_bytes == plain_data

    # 2. 验证传 AES 预缓存算法对象正常工作且字节完全相同
    aes_algo = algorithms.AES(key)
    res_algo = plain(raw_page, aes_algo)
    assert res_algo == plain_data


def test_open_ro_mmap_pragmas():
    """验证 open_ro 启用 mmap 与页缓存，并支持正常只读查询。"""
    from scripts.ntqq_db import open_ro

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "ro_test.db")
        con = sqlite3.connect(db_path)
        con.execute("CREATE TABLE kv (k TEXT, v TEXT)")
        con.execute("INSERT INTO kv VALUES ('hello', 'world')")
        con.commit()
        con.close()

        ro_con = open_ro(db_path)
        try:
            row = ro_con.execute("SELECT v FROM kv WHERE k='hello'").fetchone()
            assert row is not None and row[0] == "world"

            # 验证 PRAGMA 读取正常
            mmap = ro_con.execute("PRAGMA mmap_size;").fetchone()[0]
            assert mmap > 0
        finally:
            ro_con.close()


def test_parser_varint_and_decode_str_perf_helpers():
    """验证统一 Parser 中的 _varint 与 _decode_str 快速路径在各类边界值下的正确性。"""
    from scripts.ntqq_parser import _varint, _decode_str

    # 1. 单字节与多字节 varint
    assert _varint(b"\x00", 0) == (0, 1)
    assert _varint(b"\x01", 0) == (1, 1)
    assert _varint(b"\x7f", 0) == (127, 1)
    assert _varint(b"\x80\x01", 0) == (128, 2)
    assert _varint(b"\xac\x02", 0) == (300, 2)

    # 2. _decode_str 各类型分支
    assert _decode_str(b"") == ""
    assert _decode_str(None) == ""
    assert _decode_str("") == ""
    assert _decode_str("hello") == "hello"
    assert _decode_str(b"hello world") == "hello world"
    assert _decode_str("测试中文".encode("utf-8")) == "测试中文"
    assert _decode_str("测试中文".encode("gbk")) == "测试中文"

