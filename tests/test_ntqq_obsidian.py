import os
import pytest
from scripts.ntqq_obsidian import generate_obsidian_markdown, export_to_obsidian


def test_generate_obsidian_markdown():
    report_data = {
        "title": "QQ群聊简报 - 深度学习交流群",
        "group_id": "12345678",
        "group_name": "深度学习交流群",
        "date_str": "2026-11-04",
        "period_str": "2026-11-03 00:00 ~ 2026-11-04 00:00",
        "total_messages": 350,
        "active_members": 28,
        "summary": "今日主要讨论了最新大模型微调方案与推理加速技巧。",
        "keywords": ["Transformer", "LoRA", "量化"],
        "actions": [
            {"assignee": "张三", "task": "整理量化评测对比表", "deadline": "周五前"}
        ],
        "quotes": [
            {"sender": "李四", "text": "量化到4bit精度基本无损，收益极大"}
        ],
        "dialogues": [
            {"sender": "张三", "time": "14:20", "text": "LoRA微调的 rank 设为多少合适？"},
            {"sender": "李四", "time": "14:22", "text": "通常 16 或 32 就足够了"}
        ],
        "links": [
            {"clean_url": "https://github.com/vllm-project/vllm", "domain": "github.com", "context": "推理加速框架"}
        ]
    }

    md = generate_obsidian_markdown(report_data)

    # Check YAML Frontmatter
    assert md.startswith("---")
    assert "group_id: '12345678'" in md or "group_id: 12345678" in md
    assert "tags:" in md
    assert "- qq-digest" in md

    # Check Obsidian Callouts
    assert "> [!summary]" in md
    assert "> [!todo]" in md
    assert "> [!quote]" in md

    # Check Wikilinks
    assert "[[Transformer]]" in md or "[[LoRA]]" in md

    # Check chat-qq code block
    assert "```chat-qq" in md
    assert "LoRA微调的 rank 设为多少合适？" in md
    assert "张三" in md


def test_export_to_obsidian_file(tmp_path):
    report_data = {
        "title": "测试群聊简报: 特殊字符/测试",
        "group_id": "888888",
        "group_name": "特殊*字符:测试群",
        "date_str": "2026-11-04",
        "summary": "测试内容",
    }

    out_file = export_to_obsidian(report_data, str(tmp_path))
    assert os.path.isfile(out_file)
    assert os.path.exists(out_file)
    content = open(out_file, "r", encoding="utf-8").read()
    assert "group_id" in content
