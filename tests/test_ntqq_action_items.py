import pytest
from scripts.ntqq_action_items import extract_action_items_and_decisions, format_action_items_markdown


def test_extract_action_items_and_decisions():
    messages = [
        {
            "text": "@张三 请于周五前提交系统架构测试报告，辛苦了",
            "sender": "1001",
            "sender_name": "组长Alice",
            "timestamp": 1700000100,
            "seq": 201,
        },
        {
            "text": "大家记得今晚前填写团建统计在线表格",
            "sender": "1002",
            "sender_name": "委员Bob",
            "timestamp": 1700000200,
            "seq": 202,
        },
        {
            "text": "经讨论，技术方案确定采用方案B（Rust重构核心）",
            "sender": "1001",
            "sender_name": "组长Alice",
            "timestamp": 1700000300,
            "seq": 203,
        },
        {
            "text": "公告：下周一上午10点在大会议室召开季度总结会",
            "sender": "1003",
            "sender_name": "行政Charlie",
            "timestamp": 1700000400,
            "seq": 204,
        },
        # Casual talks that should NOT be detected as action items
        {
            "text": "今天下午吃了汉堡，记得味道挺不错的哈哈",
            "sender": "1004",
            "sender_name": "Dave",
            "timestamp": 1700000500,
            "seq": 205,
        },
        {
            "text": "周五的天气预报说有大雨，出门带伞哦",
            "sender": "1005",
            "sender_name": "Eve",
            "timestamp": 1700000600,
            "seq": 206,
        },
    ]

    result = extract_action_items_and_decisions(messages)
    actions = result["actions"]
    decisions = result["decisions"]

    assert len(actions) == 2
    assert len(decisions) == 2

    # Check action 1
    act1 = next(a for a in actions if "张三" in a["assignee"])
    assert "周五前" in act1["deadline"]
    assert "提交" in act1["task"]
    assert act1["sender_name"] == "组长Alice"
    assert act1["seq"] == 201

    # Check action 2
    act2 = next(a for a in actions if a["assignee"] in ("大家", "全体成员", "各位"))
    assert "今晚前" in act2["deadline"]
    assert "填写" in act2["task"]

    # Check decisions
    assert any("方案B" in d["decision"] for d in decisions)
    assert any("季度总结会" in d["decision"] for d in decisions)


def test_format_action_items_markdown():
    data = {
        "actions": [
            {
                "assignee": "张三",
                "task": "提交系统架构测试报告",
                "deadline": "周五前",
                "sender_name": "组长Alice",
                "time_str": "11-04 14:00",
                "seq": 201,
            }
        ],
        "decisions": [
            {
                "decision": "技术方案确定采用方案B",
                "announcer": "组长Alice",
                "time_str": "11-04 15:00",
                "seq": 203,
            }
        ],
    }

    md = format_action_items_markdown(data)
    assert "📌 待办事项与决议雷达" in md
    assert "张三" in md
    assert "周五前" in md
    assert "方案B" in md

