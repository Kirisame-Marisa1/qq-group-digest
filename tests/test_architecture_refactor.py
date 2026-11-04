# -*- coding: utf-8 -*-
"""架构规范治理与模块解耦专项测试套件 (test_architecture_refactor.py)。

验证：
1. ntqq_core Facade 门面模式 100% 再导出兼容性；
2. ntqq_parser Protobuf wire 编解码单一真实来源 (SSOT)；
3. ntqq_crypto 凭据与 DPAPI 加解密独立性；
4. ntqq_db 数据库连接与结构迁移独立性；
5. ntqq_report 与 ntqq_build 拆解后的独立 handler 与 helper 可执行性。
"""

import argparse
import os
import sqlite3
import tempfile
import pytest

import ntqq_core
import ntqq_crypto
import ntqq_db
import ntqq_parser
import ntqq_build
import ntqq_report


def test_core_facade_reexports():
    """验证 ntqq_core 作为 Facade，完整再导出了 ntqq_crypto, ntqq_db, ntqq_parser 的全部公共成员。"""
    # 1. 来自 ntqq_crypto 的成员
    crypto_symbols = [
        "dpapi_encrypt",
        "dpapi_decrypt",
        "protect_key_hex",
        "unwrap_key_hex",
        "load_rkey",
        "save_rkey",
        "verify_keymap",
        "ensure_keymap",
        "ensure_plain",
        "cleanup_plain",
    ]
    for sym in crypto_symbols:
        assert hasattr(ntqq_core, sym), f"ntqq_core missing crypto symbol: {sym}"
        assert getattr(ntqq_core, sym) is getattr(ntqq_crypto, sym) or callable(getattr(ntqq_core, sym))

    # 2. 来自 ntqq_db 的成员
    db_symbols = [
        "INDEX_SCHEMA",
        "INDEX_MIGRATIONS",
        "run_index_migrations",
        "rebuild_fts",
        "sync_fts_incremental",
        "open_ro",
        "open_index",
    ]
    for sym in db_symbols:
        assert hasattr(ntqq_core, sym), f"ntqq_core missing db symbol: {sym}"

    # 3. 来自 ntqq_parser 的成员
    parser_symbols = [
        "_varint",
        "pb_fields",
        "_decode_str",
        "decode_body",
        "CT_KIND",
        "classify",
        "FWD_FIELD",
        "forward_text",
        "decode_forward",
        "format_forward",
        "MessageParser",
        "ParsedMessage",
        "AtTarget",
        "ReplyInfo",
        "CardInfo",
    ]
    for sym in parser_symbols:
        assert hasattr(ntqq_core, sym), f"ntqq_core missing parser symbol: {sym}"
        assert getattr(ntqq_core, sym) is getattr(ntqq_parser, sym)


def test_parser_unification():
    """验证 ntqq_parser 是 Protobuf 编解码与消息归一化的单一事实来源。"""
    # 验证 varint 编解码
    data = b"\x96\x01"  # 150 in varint
    val, idx = ntqq_parser._varint(data, 0)
    assert val == 150
    assert idx == 2

    # 验证 pb_fields wire 解析
    raw_field = b"\x08\x96\x01"  # field 1, wire type 0, value 150
    fields = ntqq_parser.pb_fields(raw_field)
    assert fields == [(1, 0, 150)]

    # 验证分类与文本抽取
    dummy_sc = {"body_40800": {"content_type": 45002, "text": 45101}}
    kind = ntqq_parser.classify(1, {45101: [b"hello world"]}, dummy_sc)
    assert kind == "text"


def test_crypto_module_standalone():
    """验证 ntqq_crypto 凭据与加密层独立可用且加解密对称。"""
    raw_hex = "a" * 64
    wrapped = ntqq_crypto.protect_key_hex(raw_hex, use_dpapi=True)
    assert isinstance(wrapped, str)
    unwrapped = ntqq_crypto.unwrap_key_hex(wrapped)
    assert unwrapped.lower() == raw_hex.lower()


def test_db_module_standalone():
    """验证 ntqq_db 存储层在隔离环境下能独立初始化库并执行迁移。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "standalone_test.db")
        con = ntqq_db.open_index(db_path)
        try:
            # 验证所有核心表均已成功建立
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            expected_tables = {
                "meta",
                "messages",
                "groups",
                "media",
                "c2c_messages",
                "c2c_groups",
                "group_cursors",
                "micro_digests",
            }
            assert expected_tables.issubset(tables)

            # 验证迁移版本标记已写入 meta 表
            ver_row = con.execute("SELECT value FROM meta WHERE key='db_schema_version'").fetchone()
            assert ver_row is not None
            assert int(ver_row[0]) >= 5
        finally:
            con.close()


def test_report_decoupled_handlers():
    """验证 ntqq_report 拆解出的参数解析器与时间窗计算 helper。"""
    parser = ntqq_report.build_argument_parser()
    assert isinstance(parser, argparse.ArgumentParser)

    args = parser.parse_args(["--days", "7", "--brief"])
    assert args.days == 7.0
    assert args.brief is True

    now = 1700000000
    t0, t1, span = ntqq_report.resolve_report_time_window(args, now)
    assert t1 == now
    assert t0 == now - int(7 * 86400)
    assert span == t1 - t0


def test_build_decoupled_helpers():
    """验证 ntqq_build 拆解出的增量窗口判定 helper。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "build_test.db")
        con = ntqq_db.open_index(db_path)
        try:
            a = argparse.Namespace(rebuild=False, only_groups=None)
            # 全量场景
            scan_day0, is_inc = ntqq_build._calculate_incremental_window(
                con, days=30, cut=1000, day0=1000, a=a, stream=False, codes_count=10, wlabel="近 30 天"
            )
            assert scan_day0 == 1000
            assert is_inc is False
        finally:
            con.close()

def test_normalize_qq_data_dir_handles_tencent_files_and_accounts():
    from ntqq_core import _normalize_qq_data_dir
    res = _normalize_qq_data_dir("D:/QQNT/Tencent Files", "123456789")
    assert res == "D:/QQNT/Tencent Files"
    res = _normalize_qq_data_dir("D:/QQNT/Tencent Files/123456789", "123456789")
    assert res == "D:/QQNT/Tencent Files"
    res = _normalize_qq_data_dir("D:/QQNT/Tencent Files/123456789/nt_qq/nt_db", "123456789")
    assert res == "D:/QQNT/Tencent Files"
    res = _normalize_qq_data_dir("D:/QQNT/Tencent Files/123456789/nt_qq/nt_db", "YOUR_QQ")
    assert res == "D:/QQNT/Tencent Files"

