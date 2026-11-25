# -*- coding: utf-8 -*-
"""Regression tests covering security, correctness, and architecture fixes from code audit."""

import contextlib
import json
import os
import re
import sqlite3
import subprocess
import sys

import pytest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(ROOT_DIR, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import ntqq_core
import ntqq_db
import ntqq_snapshot
import ntqq_parser
import ntqq_slice
import ntqq_media
import ntqq_ocr
import ntqq_links
import ntqq_action_items
import ntqq_social
import ntqq_obsidian
import ntqq_report
import ntqq_report_schema
import ntqq_html
from gui.service import QQGuiService


def test_core_validation_safe_local_and_cdn_allowlist(tmp_path):
    # Bool index_days must be rejected
    ok, errs = ntqq_core.validate_config({"index_days": True, "decrypt": ["nt_msg.db"]})
    assert not ok
    assert any("index_days" in e for e in errs)

    # Small timestamps on Windows must not raise OSError
    assert ntqq_core.safe_local_dt(0) is None
    assert ntqq_core.safe_local_dt(100).year == 1970
    assert ntqq_core.local(0).year == 1970
    assert ntqq_core.local(100).strftime("%Y-%m-%d %H:%M:%S") == "1970-01-01 08:01:40"

    # SQL identifier guard
    assert ntqq_core.validate_sql_ident("messages") == "messages"
    with pytest.raises(ValueError):
        ntqq_core.validate_sql_ident("messages; DROP TABLE users")

    # CDN host allowlist and leading slash check
    assert ntqq_core.is_allowed_cdn_host("multimedia.nt.qq.com.cn")
    assert not ntqq_core.is_allowed_cdn_host("evil-qq.com.cn")
    assert not ntqq_core.is_allowed_cdn_host("attacker.example")
    with pytest.raises(ValueError):
        ntqq_core.db_image_url("https://evil.example/steal?x=1", "/download?a=1", "rk")
    with pytest.raises(ValueError):
        ntqq_core.db_image_url("multimedia.nt.qq.com.cn", "noslash", "rk")
    assert (
        ntqq_core.db_image_url("multimedia.nt.qq.com.cn", "/download?appid=1407", "r123")
        == "https://multimedia.nt.qq.com.cn/download?appid=1407&rkey=r123"
    )

    # Atomic JSON write cleans up tmp artifacts
    target = tmp_path / "nested" / "cfg.json"
    ntqq_core.atomic_write_json(str(target), {"hello": "world"})
    assert json.loads(target.read_text(encoding="utf-8")) == {"hello": "world"}


def _insert_or_update_msg(con, row):
    con.execute(
        """
        INSERT INTO messages(
            msg_id, seq, ts, day, hour, group_code, group_name, sender_uid,
            sender_qq, sender_name, direction, msg_type, subtype, kind, text, reply_seq
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(msg_id) DO UPDATE SET text=excluded.text, sender_name=excluded.sender_name
        """,
        row,
    )
    con.commit()


def test_fts5_triggers_sync_update_delete_without_double_count(tmp_path):
    db_file = tmp_path / "fts_sync.db"
    con = ntqq_db.open_index(str(db_file))
    try:
        assert con.execute("PRAGMA recursive_triggers").fetchone()[0] == 1
        _insert_or_update_msg(
            con,
            (101, 1, 1700000000, "2026-11-01", 10, 999, "测试群", "u1", 10001, "张三", 0, 1, 0, "text", "旧话题 AlphaZero", 0),
        )
        # Trigger already indexed row 101; incremental helper should remain idempotent
        ntqq_db.sync_fts_incremental(con, [101])
        cnt_before = con.execute("SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'AlphaZero'").fetchone()[0]
        assert cnt_before == 1

        # ON CONFLICT update removes stale token and indexes new token
        _insert_or_update_msg(
            con,
            (101, 1, 1700000000, "2026-11-01", 10, 999, "测试群", "u1", 10001, "张三", 0, 1, 0, "text", "新话题 BetaZero", 0),
        )
        assert con.execute("SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'AlphaZero'").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'BetaZero'").fetchone()[0] == 1

        # Delete removes from FTS
        con.execute("DELETE FROM messages WHERE msg_id=101")
        con.commit()
        assert con.execute("SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'BetaZero'").fetchone()[0] == 0
    finally:
        con.close()


def test_snapshot_clamps_retries_and_cleans_up(tmp_path):
    missing_db = str(tmp_path / "does_not_exist.db")
    snap = ntqq_snapshot.SafeDBSnapshot(missing_db, max_retries=0, retry_delay=0.0)
    assert snap.max_retries == 1
    with pytest.raises(FileNotFoundError):
        snap.create_snapshot()
    assert snap.snapshot_db_path is None
    assert snap._temp_dir_obj is None


def test_parser_xml_entities_and_slice_signed_ints(tmp_path):
    xml = "<msg brief='[卡片 &amp; 分享]'><meta><news title='Hello &lt;World&gt;'/></meta></msg>"
    info = ntqq_parser.extract_card_info(xml)
    assert info is not None
    assert info.title == "Hello <World>"

    # Replacement char shouldn't drop surrounding readable text
    extracted = ntqq_parser.MessageParser.fallback_extract_strings("你好世界\ufffd明天开会".encode("utf-8"))
    assert "你好世界" in extracted
    assert "明天开会" in extracted

    # Verify 24-bit and 48-bit negative big-endian integers in ntqq_slice
    assert ntqq_slice.stype_val((-5).to_bytes(3, "big", signed=True), 0, 3) == -5
    assert ntqq_slice.stype_val((-123456789012).to_bytes(6, "big", signed=True), 0, 5) == -123456789012

    # Verify OrderedDict LRU + context manager on Src with real AES-CBC pages
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = os.urandom(32)
    fake_db = tmp_path / "fake_enc.db"
    chunks = [b"\x00" * 1024]
    for pno in range(1, 5):
        iv = bytes([pno]) * 16
        plain = bytes([pno]) * 4048
        enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor().update(plain)
        raw = enc + iv + b"\x00" * 32
        chunks.append((b"\x00" * 16 + raw[16:]) if pno == 1 else raw)
    fake_db.write_bytes(b"".join(chunks))

    with ntqq_slice.Src(str(fake_db), key, cache=2) as src:
        _ = src.page(2)
        _ = src.page(3)
        _ = src.page(2)  # refresh page 2 MRU (cache key 1)
        _ = src.page(4)  # evicts page 3 (key 2), retains page 2 (key 1) and 4 (key 3)
        assert 2 not in src.cache
        assert 1 in src.cache and 3 in src.cache
    assert src.f.closed


def test_media_limit_and_ocr_cli_safety(tmp_path):
    class _FakeResp:
        def __init__(self, payload: bytes):
            self._buf = payload
            self._pos = 0

        def read(self, n: int = -1) -> bytes:
            if n < 0:
                chunk = self._buf[self._pos :]
                self._pos = len(self._buf)
                return chunk
            chunk = self._buf[self._pos : self._pos + n]
            self._pos += len(chunk)
            return chunk

    with pytest.raises(ValueError, match="超过"):
        ntqq_media._read_limited(_FakeResp(b"x" * 20), max_bytes=10)
    assert ntqq_media._read_limited(_FakeResp(b"ok"), max_bytes=10) == b"ok"

    # LIKE wildcard escaping + recursive image walk in ntqq_ocr
    db_path = tmp_path / "ocr.db"
    media_root = tmp_path / "media"
    sub = media_root / "ab"
    sub.mkdir(parents=True)
    img1 = sub / "ab123456789012345678901234567890.png"
    img1.write_bytes(b"fake")

    con = sqlite3.connect(str(db_path))
    ntqq_ocr.init_ocr_table(con)
    ntqq_ocr.record_ocr_result(con, "ab123456789012345678901234567890", "进度 100% 完成")
    ntqq_ocr.record_ocr_result(con, "deadbeef" * 4, "进度 100 完成")
    # Literal '%' must match only '100%', not '100 完成'
    hits = ntqq_ocr.search_ocr_text(con, "100%")
    assert len(hits) == 1
    assert hits[0]["md5"] == "ab123456789012345678901234567890"
    con.close()

    # CLI help must work
    help_res = subprocess.run([sys.executable, os.path.join(SCRIPTS_DIR, "ntqq_ocr.py"), "--help"], capture_output=True, text=True)
    assert help_res.returncode == 0
    assert "--batch-size" in help_res.stdout


def test_links_actions_social_and_obsidian_hardening(tmp_path):
    # Lookalike domain must not match github.com category
    rows = [
        {"group_code": 1, "seq": 1, "ts": 100, "sender_name": "Alice", "text": "check https://notgithub.com/evil and HTTPS://GitHub.com/foo/bar)"},
        {"group_code": 1, "seq": 2, "ts": 120, "sender_name": "Alice", "text": "[引用 Bob: test]\n@Charlie 记得周三前提交测试报告，好厉害！"},
        {"group_code": 2, "seq": 1, "ts": 125, "sender_name": "Dave", "text": "另一群的人不应和前一分钟的 Alice 产生轮转边"},
    ]
    links = ntqq_links.extract_links_and_resources(rows)
    by_url = {l["clean_url"]: l for l in links}
    assert by_url["https://notgithub.com/evil"]["category"] == "other"
    assert by_url["https://github.com/foo/bar"]["category"] == "code"

    acts = ntqq_action_items.extract_action_items_and_decisions(rows)
    assert any("Charlie" in a["assignee"] and "提交测试报告" in a["task"] for a in acts["actions"])

    graph = ntqq_social.build_social_graph(rows)
    pair_ids = {tuple(sorted((e["source"], e["target"]))) for e in graph["edges"]}
    assert tuple(sorted(("Alice", "Dave"))) not in pair_ids

    # Obsidian path traversal, reserved name, YAML escaping & dynamic fence
    vault = tmp_path / "vault"
    out = ntqq_obsidian.export_to_obsidian(
        {
            "title": "../../../../etc/passwd",
            "group_id": "CON",
            "group_name": "群\n换行\"测试",
            "date_str": "2026-11-04",
            "dialogues": [{"sender": "A", "time": "10:00", "text": "内含三反引号\n```\ncode\n```"}],
        },
        str(vault),
    )
    assert os.path.commonpath([os.path.abspath(str(vault)), os.path.abspath(out)]) == os.path.abspath(str(vault))
    content = open(out, encoding="utf-8").read()
    assert "````chat-qq" in content
    assert "group_name: '群 换行\"测试'" in content


def test_report_ask_structured_anonymize_and_html_no_inline_onclick(tmp_path):
    db_file = tmp_path / "report_test.db"
    con = ntqq_db.open_index(str(db_file))
    now = ntqq_core.now_ts()
    _insert_or_update_msg(
        con,
        (1, 10, now - 600, "2026-11-04", 10, 888888, "研发交流群", "u1", 100001, "真实张三", 0, 1, 0, "text", "大家好，周会改到明天下午三点", 0),
    )
    _insert_or_update_msg(
        con,
        (2, 11, now - 500, "2026-11-04", 10, 888888, "研发交流群", "u2", 100002, "真实李四", 0, 1, 0, "text", "收到，课程设计方案已确认采用方案B，记得带 @真实张三 看 12345678", 0),
    )
    con.row_factory = sqlite3.Row

    # ask() + _run_report_with_idx(--ask --anonymize) must not crash and must scrub names/QQ
    import argparse
    con.execute("INSERT OR IGNORE INTO groups VALUES (888888, '研发交流群', ?, ?, 2)", (now - 600, now))
    con.commit()
    ask_ns = argparse.Namespace(
        list_groups=False, hours=None, days=None, since=None, until=None,
        no_group_state=True, c2c=False, member=None, anonymize=True, class_mode=False,
        ask="研发交流群 今天聊 课程设计 了吗", group=["888888"], max_msgs=40, ctx=2,
        search=None, no_html=True, html=None, out=None, brief=False, structured=False,
        links=False, actions=False, social=False, wrapped=False, export=None,
        obsidian_vault=None, min_msgs=1, max_chars=50000, verbose=False,
    )
    md_out = tmp_path / "ask_anon.md"
    ntqq_report._run_report_with_idx(ask_ns, con, "messages", "groups", False, str(md_out), None)
    out = md_out.read_text(encoding="utf-8")
    assert "命中消息" in out
    assert "真实张三" not in out
    assert "真实李四" not in out
    assert "888888" not in out
    assert "12345678" not in out

    # generate_structured_digest() must handle sqlite3.Row, and _run_report_with_idx(structured=True, anonymize=True) must scrub PII
    rows = con.execute("SELECT * FROM messages ORDER BY ts ASC").fetchall()
    md = ntqq_report.generate_structured_digest(888888, rows=rows, group_name="研发交流群")
    assert "方案B" in md

    struct_ns = ntqq_report.build_argument_parser().parse_args(
        ["--group", "888888", "--hours", "24", "--structured", "--anonymize", "--no-group-state", "--no-html"]
    )
    struct_md_out = tmp_path / "struct_anon.md"
    ntqq_report._run_report_with_idx(struct_ns, con, "messages", "groups", False, str(struct_md_out), None)
    con.close()
    struct_out = struct_md_out.read_text(encoding="utf-8")
    assert "真实张三" not in struct_out
    assert "真实李四" not in struct_out
    assert "12345678" not in struct_out

    # digest_report.html must avoid user-data inline onclick injection and pass node syntax check
    payload = ntqq_html.build_digest_payload(
        meta_info={
            "title": "安全验证",
            "is_c2c": False,
            "anonymized": False,
            "brief": False,
            "class_mode": False,
            "cutoff_str": "2026-11-04 12:00:00",
            "window_str": "今天",
            "cur_n": 1,
            "prev_n": 0,
            "delta": 1,
        },
        prepared=[(1, 1, 888888, "<b>群名</b>", [dict(r) for r in rows[:1]], 0)],
        focus_rows=[],
        fmt_fn=ntqq_core.fmt,
        kw_fn=ntqq_report.keywords,
    )
    html_text = ntqq_html.render_html_report(payload)
    tpl_js = re.findall(r"<script.*?>([\s\S]*?)</script>", html_text)[1]
    assert re.search(r'onclick="[^\"]*\'\s*\+', tpl_js) is None
    assert re.search(r"onclick='[^']*\"\s*\+", tpl_js) is None
    assert "data-action" in tpl_js
    assert "safeUrl(" in tpl_js
    js_path = tmp_path / "check_tpl.js"
    js_path.write_text(tpl_js, encoding="utf-8")
    node_res = subprocess.run(["node", "-c", str(js_path)], capture_output=True, text=True)
    assert node_res.returncode == 0, node_res.stderr


def test_gui_service_helpers_and_script_launcher(tmp_path):
    bad_cfg = tmp_path / "bad.json"
    bad_cfg.write_text("{corrupt", encoding="utf-8")
    assert QQGuiService.load_config(str(bad_cfg)) == {}

    txt = QQGuiService.format_database_summary(
        {
            "total_msgs": 5,
            "total_groups": 1,
            "min_day": "2026-11-01",
            "max_day": "2026-11-02",
            "built_at": "1700000000",
            "window_days": "30",
            "top_groups": [("测试群", 5)],
            "kind_stats": [("text", 5)],
        }
    )
    assert "1700000000" not in txt
    assert "2023-" in txt

    # Ensure scripts/gui_app.py no longer hits infinite/wrong self-import
    res = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, 'scripts'); import gui_app; assert callable(gui_app.main)",
        ],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
