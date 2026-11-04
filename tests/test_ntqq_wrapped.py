import pytest
from scripts.ntqq_wrapped import build_chat_wrapped, format_wrapped_markdown


def test_build_chat_wrapped():
    messages = [
        # Night owl message at 02:30 AM
        {"sender_name": "Alice", "text": "深夜还在调模型...", "timestamp": 1700015400, "day": "2026-11-04", "hour": 2, "kind": "text"},
        # Early bird message at 07:15 AM
        {"sender_name": "Bob", "text": "大家早上好！开始新一天", "timestamp": 1700032500, "day": "2026-11-04", "hour": 7, "kind": "text"},
        # Long message
        {"sender_name": "Charlie", "text": "今天给大家做个完整的技术架构复盘：" + "详细内容" * 30, "timestamp": 1700040000, "day": "2026-11-04", "hour": 10, "kind": "text"},
        {"sender_name": "Alice", "text": "赞同Charlie的观点", "timestamp": 1700040100, "day": "2026-11-04", "hour": 10, "kind": "text"},
    ]

    wrapped = build_chat_wrapped(messages, period_name="2026年度时光机")
    
    assert wrapped["total_messages"] == 4
    assert wrapped["unique_speakers"] == 3
    assert wrapped["night_owl_speaker"] == "Alice"
    assert wrapped["early_bird_speaker"] == "Bob"
    assert wrapped["longest_speaker"] == "Charlie"
    assert len(wrapped["badges"]) >= 1

    md = format_wrapped_markdown(wrapped)
    assert "时光机" in md
    assert "Charlie" in md
    assert "Alice" in md

