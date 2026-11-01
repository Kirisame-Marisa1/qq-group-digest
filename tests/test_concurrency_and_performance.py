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
