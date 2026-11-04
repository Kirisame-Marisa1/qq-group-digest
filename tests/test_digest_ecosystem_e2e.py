"""
test_digest_ecosystem_e2e.py - 综合生态演进五大能力端到端集成测试
验证 Links、Actions、OCR、Obsidian 与 Social/Wrapped 在同一消息管道下的协同运作。
"""

import os
import sqlite3
import pytest
from scripts.ntqq_links import extract_links_and_resources, format_links_markdown
from scripts.ntqq_action_items import extract_action_items_and_decisions, format_action_items_markdown
from scripts.ntqq_ocr import init_ocr_table, record_ocr_result, search_ocr_text
from scripts.ntqq_obsidian import export_to_obsidian, generate_obsidian_markdown, sanitize_wikilink
from scripts.ntqq_social import build_social_graph, format_social_graph_markdown
from scripts.ntqq_wrapped import build_chat_wrapped, format_wrapped_markdown, render_wrapped_html
from scripts.ntqq_html import build_digest_payload, render_html_report
from ntqq_core import fmt


def test_ecosystem_full_pipeline_e2e(tmp_path):
    # 模拟一个丰富真实的群聊场景
    messages = [
        {
            "msg_id": 1,
            "seq": 1001,
            "ts": 1700010000,
            "day": "2026-11-04",
            "hour": 2,
            "sender": "10001",
            "sender_name": "组长Alice",
            "kind": "text",
            "text": "@Bob 请于周五前提交测试报告，项目地址: (https://github.com/org/repo?utm_source=qq) 辛苦！",
        },
        {
            "msg_id": 2,
            "seq": 1002,
            "ts": 1700010040,
            "day": "2026-11-04",
            "hour": 2,
            "sender": "10002",
            "sender_name": "开发Bob",
            "kind": "text",
            "text": "收到！正在看，资料参考B站教程 https://www.bilibili.com/video/BV1test123?spm_id_from=1",
        },
        {
            "msg_id": 3,
            "seq": 1003,
            "ts": 1700020000,
            "day": "2026-11-04",
            "hour": 8,
            "sender": "10003",
            "sender_name": "架构Charlie",
            "kind": "reply",
            "text": "[引用 组长Alice: 请于周五前提交测试报告] 经讨论，方案确定采用方案B，大家记得今晚前确认！",
        },
        {
            "msg_id": 4,
            "seq": 1004,
            "ts": 1700030000,
            "day": "2026-11-04",
            "hour": 14,
            "sender": "10001",
            "sender_name": "组长Alice",
            "kind": "image",
            "text": "[图片: 8f9b3c4d5e6a7b8c9d0e1f2a3b4c5d6e.jpg]",
        },
    ]

    # 1. 验证 Links 提取及标点符号清理
    links = extract_links_and_resources(messages)
    assert len(links) == 2
    gh_link = next(l for l in links if l["category"] == "code")
    assert gh_link["clean_url"] == "https://github.com/org/repo"
    assert not gh_link["clean_url"].endswith(")")  # Trailing parenthesis stripped!
    assert "utm_source" not in gh_link["clean_url"]
    links_md = format_links_markdown(links)
    assert "📦 群内资源与链接合辑" in links_md

    # 2. 验证 Actions 提取
    acts = extract_action_items_and_decisions(messages)
    assert len(acts["actions"]) >= 1
    assert any("Bob" in a["assignee"] and "周五前" in a["deadline"] for a in acts["actions"])
    assert any("方案B" in d["decision"] for d in acts["decisions"])
    actions_md = format_action_items_markdown(acts)
    assert "📌 待办事项与决议雷达" in actions_md

    # 3. 验证 OCR 数据库存取与搜索联动
    db_file = tmp_path / "index.db"
    conn = sqlite3.connect(str(db_file))
    init_ocr_table(conn)
    record_ocr_result(conn, "8f9b3c4d5e6a7b8c9d0e1f2a3b4c5d6e", "关于2026年第四季度架构升级的正式通知文件")
    ocr_res = search_ocr_text(conn, "架构升级")
    assert len(ocr_res) == 1
    assert ocr_res[0]["md5"] == "8f9b3c4d5e6a7b8c9d0e1f2a3b4c5d6e"
    conn.close()

    # 4. 验证 Obsidian 导出
    obs_data = {
        "title": "端到端测试群",
        "group_id": "99999",
        "group_name": "端到端测试群",
        "date_str": "2026-11-04",
        "total_messages": len(messages),
        "active_members": 3,
        "keywords": ["架构升级", "测试报告"],
        "actions": acts["actions"],
        "decisions": acts["decisions"],
        "links": links,
        "dialogues": [{"sender": m["sender_name"], "time": "12:00", "text": m["text"]} for m in messages[:2]],
    }
    obs_file = export_to_obsidian(obs_data, str(tmp_path / "obsidian"))
    assert os.path.exists(obs_file)
    obs_content = open(obs_file, "r", encoding="utf-8").read()
    assert "> [!todo]" in obs_content
    assert "> [!summary]" in obs_content
    assert "```chat-qq" in obs_content

    # 5. 验证社交关系图谱 (Social Graph)
    graph = build_social_graph(messages)
    assert len(graph["nodes"]) >= 2
    assert graph["total_interactions"] >= 1
    soc_md = format_social_graph_markdown(graph)
    assert "群聊社交互动网络" in soc_md

    # 6. 验证群聊时光机 (Chat Wrapped)
    wrapped = build_chat_wrapped(messages, period_name="测试时光机")
    assert wrapped["total_messages"] == 4
    assert wrapped["unique_speakers"] == 3
    wrapped_md = format_wrapped_markdown(wrapped)
    assert "测试时光机" in wrapped_md
    wrapped_html = render_wrapped_html(wrapped)
    assert "<!DOCTYPE html>" in wrapped_html
    assert "测试时光机" in wrapped_html


def test_sqlite3_row_compatibility_and_xss_prevention(tmp_path):
    """验证 sqlite3.Row 真实对象兼容性以及 HTML XSS 注入防护。"""
    db_file = tmp_path / "test_rows.db"
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE messages (
            msg_id INTEGER PRIMARY KEY,
            ts INTEGER,
            day TEXT,
            hour INTEGER,
            group_code INTEGER,
            group_name TEXT,
            sender_uid TEXT,
            sender_qq INTEGER,
            sender_name TEXT,
            direction INTEGER,
            msg_type INTEGER,
            subtype INTEGER,
            kind TEXT,
            text TEXT,
            reply_seq INTEGER
        )
    """)
    conn.execute("""
        INSERT INTO messages VALUES 
        (1, 1700010000, '2026-11-04', 2, 123456, '测试群', 'u1', 1001, '<script>alert(1)</script>', 0, 1, 0, 'text', '@Bob 请于周五前提交 https://github.com/foo/bar.', 0),
        (2, 1700010050, '2026-11-04', 2, 123456, '测试群', 'u2', 1002, '开发Bob', 0, 1, 0, 'text', '收到，昨天大家完成得很棒！', 0)
    """)
    conn.commit()

    rows = conn.execute("SELECT * FROM messages").fetchall()
    assert isinstance(rows[0], sqlite3.Row)

    # 1. 验证 sqlite3.Row 传入 extract_links_and_resources 不崩溃
    links = extract_links_and_resources(rows)
    assert len(links) == 1
    assert links[0]["clean_url"] == "https://github.com/foo/bar"
    assert not links[0]["clean_url"].endswith(".")

    # 2. 验证 sqlite3.Row 传入 extract_action_items_and_decisions 不崩溃，且过滤完成夸奖
    actions = extract_action_items_and_decisions(rows)
    assert len(actions["actions"]) == 1
    assert "Bob" in actions["actions"][0]["assignee"]
    # 确保 "昨天大家完成得很棒" 未被误报为待办任务
    assert not any("完成得很棒" in a["task"] for a in actions["actions"])

    # 3. 验证 sqlite3.Row 传入 build_social_graph 不崩溃
    soc = build_social_graph(rows)
    assert len(soc["nodes"]) >= 1

    # 4. 验证 sqlite3.Row 传入 build_chat_wrapped 不崩溃
    wrapped = build_chat_wrapped(rows, period_name="<XSS>时光机</XSS>")
    assert wrapped["total_messages"] == 2

    # 5. 验证 render_wrapped_html 严格转义 XSS
    html_out = render_wrapped_html(wrapped)
    assert "<script>" not in html_out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_out
    assert "&lt;XSS&gt;时光机&lt;/XSS&gt;" in html_out

    # 6. 验证 Obsidian wikilink 过滤
    raw_name = "[管理员] 张三 | 架构师 #1"
    clean_wiki = sanitize_wikilink(raw_name)
    assert clean_wiki == "管理员 张三  架构师 1"
    assert "[" not in clean_wiki and "]" not in clean_wiki and "|" not in clean_wiki and "#" not in clean_wiki

    # 7. 验证 build_digest_payload 传入 prepared 含 sqlite3.Row 时不抛 AttributeError
    prepared = [(2, 2, 123456, '测试群', rows, 0)]
    meta_info = {
        'title': '测试简报',
        'is_c2c': False,
        'anonymized': False,
        'brief': False,
        'class_mode': True,
        'cutoff_str': '2026-11-04 12:00:00',
        'cur_n': 2,
        'prev_n': 0,
        'delta': 2,
    }
    payload = build_digest_payload(
        meta_info=meta_info,
        prepared=prepared,
        focus_rows=rows,
        fmt_fn=fmt,
        kw_fn=lambda texts, **kwargs: [],
        class_groups={123456},
        markdown_text='# 测试',
    )
    assert payload['mode'] == 'digest'
    assert len(payload['links']) == 1
    assert len(payload['actions']['actions']) == 1

    conn.close()
