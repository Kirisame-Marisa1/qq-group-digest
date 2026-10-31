import os
import re
import subprocess
import pytest
from scripts.ntqq_html import render_html_report

def test_html_monet_script_syntax():
    """验证生成的 HTML 中包含的 JavaScript 在 Node 环境下无任何语法错误。"""
    sample_payload = {
        "mode": "digest",
        "meta": {"title": "测试简报", "window_str": "2026-10-25 ~ 2026-11-01"},
        "kpis": {"total_msgs": 100, "group_count": 2, "people_count": 10},
        "groups": [
            {"code": "123456", "name": "测试群A", "is_class": False, "nconv": 50, "people_count": 5, "peak_hour": 14, "threads": []},
            {"code": "789012", "name": "测试群B", "is_class": True, "nconv": 50, "people_count": 5, "peak_hour": 20, "threads": []}
        ],
        "focus_items": [
            {"time": "10-30 12:00", "group_name": "测试群A", "sender": "张三", "text": "重要通知：明日开会"}
        ],
        "markdown": "# 测试简报\n\n## 测试群A (123456)\n内容A\n\n## 测试群B (789012)\n内容B"
    }

    html = render_html_report(sample_payload)
    assert "🌸 莫奈纸雕" in html
    assert "renderMonetView" in html
    assert "selectMonetGroup" in html

    scripts = re.findall(r'<script.*?>([\s\S]*?)</script>', html)
    assert len(scripts) >= 2
    # Script 1 is the main logic
    js_content = scripts[1]
    tmp_path = "tmp_check_monet.js"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(js_content)
    try:
        res = subprocess.run(["node", "-c", tmp_path], capture_output=True, text=True)
        assert res.returncode == 0, f"Node.js syntax error: {res.stderr}"
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def test_monet_constants_defined():
    """验证 gui_app.py 中所有 MONET_ 常量已定义且无丢失。"""
    import gui_app
    assert hasattr(gui_app, "MONET_BG")
    assert hasattr(gui_app, "MONET_CARD")
    assert hasattr(gui_app, "MONET_BORDER")
    assert hasattr(gui_app, "MONET_INK")
    assert hasattr(gui_app, "MONET_LOTUS")
    assert hasattr(gui_app, "MONET_SILVER_LIGHT")

