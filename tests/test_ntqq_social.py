import pytest
from scripts.ntqq_social import build_social_graph, format_social_graph_markdown


def test_build_social_graph():
    messages = [
        # Alice mentions Bob
        {"sender_name": "Alice", "text": "@Bob 你看下这个方案", "timestamp": 1700000000, "kind": "text"},
        # Bob replies to Alice within 30s
        {"sender_name": "Bob", "text": "收到，马上看", "timestamp": 1700000030, "kind": "text"},
        # Bob mentions Alice
        {"sender_name": "Bob", "text": "@Alice 方案我看完了，很赞", "timestamp": 1700000100, "kind": "text"},
        # Charlie quotes/replies to Bob
        {"sender_name": "Charlie", "text": "[引用 Bob: 方案我看完了] 确实很赞", "timestamp": 1700000150, "kind": "reply"},
        # Dave speaks independently
        {"sender_name": "Dave", "text": "大家今天好啊", "timestamp": 1700000500, "kind": "text"},
    ]

    graph = build_social_graph(messages, top_n=10)
    nodes = graph["nodes"]
    edges = graph["edges"]
    top_dyads = graph["top_dyads"]

    assert len(nodes) == 4  # Alice, Bob, Charlie, Dave
    assert len(edges) >= 2

    # Check Alice <-> Bob dyad
    alice_bob_dyad = next((d for d in top_dyads if ("Alice" in d["pair"] and "Bob" in d["pair"])), None)
    assert alice_bob_dyad is not None
    assert alice_bob_dyad["weight"] >= 2

    # Check Markdown output
    md = format_social_graph_markdown(graph)
    assert "群聊社交互动网络" in md
    assert "Alice" in md and "Bob" in md

