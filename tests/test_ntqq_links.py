import pytest
from scripts.ntqq_links import extract_links_and_resources, format_links_markdown


def test_extract_links_categorization_and_cleaning():
    messages = [
        {
            "text": "给大家安利一个开源库：https://github.com/foo/bar?utm_source=test 这个很赞！",
            "sender": "10001",
            "sender_name": "Alice",
            "timestamp": 1700000000,
            "seq": 101,
        },
        {
            "text": "看下这个B站视频 https://www.bilibili.com/video/BV1xx411c7mD?spm_id_from=333.788 讲解很透彻",
            "sender": "10002",
            "sender_name": "Bob",
            "timestamp": 1700000010,
            "seq": 102,
        },
        {
            "text": "资料在网盘：https://pan.baidu.com/s/1abcdef 提取码 1234",
            "sender": "10001",
            "sender_name": "Alice",
            "timestamp": 1700000020,
            "seq": 103,
        },
        {
            "text": "深度好文分享 https://mp.weixin.qq.com/s/xyz123?chksm=abc 推荐阅读",
            "sender": "10003",
            "sender_name": "Charlie",
            "timestamp": 1700000030,
            "seq": 104,
        },
        {
            "text": "在线文档收集表：https://docs.qq.com/sheet/abc123",
            "sender": "10004",
            "sender_name": "Dave",
            "timestamp": 1700000040,
            "seq": 105,
        },
        {
            "text": "这是重复分享 https://github.com/foo/bar 确实不错",
            "sender": "10002",
            "sender_name": "Bob",
            "timestamp": 1700000050,
            "seq": 106,
        },
    ]

    results = extract_links_and_resources(messages)
    assert len(results) == 5  # 5 unique clean URLs

    # Check GitHub repo item
    gh_item = next(r for r in results if "github.com/foo/bar" in r["clean_url"])
    assert gh_item["category"] == "code"
    assert gh_item["clean_url"] == "https://github.com/foo/bar"
    assert gh_item["share_count"] == 2
    assert "Alice" in gh_item["senders"] and "Bob" in gh_item["senders"]

    # Check Bilibili item
    bili_item = next(r for r in results if "bilibili.com" in r["clean_url"])
    assert bili_item["category"] == "video"
    assert "spm_id_from" not in bili_item["clean_url"]

    # Check Baidu Pan item
    pan_item = next(r for r in results if "pan.baidu.com" in r["clean_url"])
    assert pan_item["category"] == "drive"

    # Check WeChat item
    wx_item = next(r for r in results if "mp.weixin.qq.com" in r["clean_url"])
    assert wx_item["category"] == "article"

    # Check Doc item
    doc_item = next(r for r in results if "docs.qq.com" in r["clean_url"])
    assert doc_item["category"] == "doc"


def test_format_links_markdown():
    links = [
        {
            "clean_url": "https://github.com/foo/bar",
            "category": "code",
            "domain": "github.com",
            "context": "开源库",
            "senders": ["Alice", "Bob"],
            "share_count": 2,
            "last_time": "12:00",
        }
    ]
    md = format_links_markdown(links)
    assert "📦 群内资源与链接合辑" in md
    assert "https://github.com/foo/bar" in md
    assert "Alice" in md

