import os
import re
import subprocess
import pytest
from scripts.ntqq_html import render_html_report, get_html_template
import gui.themes as themes
import gui_app


def test_html_monet_script_syntax(tmp_path):
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
    js_content = scripts[1]
    js_check_file = tmp_path / "tmp_check_monet.js"
    js_check_file.write_text(js_content, encoding="utf-8")
    res = subprocess.run(["node", "-c", str(js_check_file)], capture_output=True, text=True)
    assert res.returncode == 0, f"Node.js syntax error: {res.stderr}"


def test_html_template_file_integrity():
    """验证独立外部 HTML 模板文件的完整性与占位符匹配。"""
    template_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scripts",
        "templates",
        "digest_report.html",
    )
    assert os.path.isfile(template_path)
    assert os.path.getsize(template_path) > 50000

    content = get_html_template()
    assert "__PAGE_TITLE__" in content
    assert "__DIGEST_JSON__" in content
    assert "data-theme" in content


def test_monet_constants_defined():
    """验证 gui_app 与 gui.themes 中所有样式常量定义完备。"""
    for mod in (gui_app, themes):
        assert hasattr(mod, "MONET_BG")
        assert hasattr(mod, "MONET_CARD")
        assert hasattr(mod, "MONET_BORDER")
        assert hasattr(mod, "MONET_INK")
        assert hasattr(mod, "MONET_LOTUS")
        assert hasattr(mod, "MONET_SILVER_LIGHT")
        assert hasattr(mod, "CLAUDE_BG")
        assert hasattr(mod, "CLAUDE_CARD")
        assert hasattr(mod, "CLAUDE_BORDER")
        assert hasattr(mod, "CLAUDE_TERRACOTTA")
