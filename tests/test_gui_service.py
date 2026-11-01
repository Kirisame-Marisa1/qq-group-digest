# -*- coding: utf-8 -*-
"""Headless unit tests for QQGuiService (business logic decoupled from Tkinter)."""

import os
import sqlite3
import tempfile
import pytest

from gui.service import QQGuiService


def test_python_exe_resolution():
    """验证 Python 解释器在不同环境下的解析逻辑。"""
    # 非冻结模式下返回当前 sys.executable
    exe = QQGuiService.get_python_exe(frozen=False)
    assert exe is not None
    assert os.path.isfile(exe)

    # 冻结模式下，如果传入不存在的路径，安全返回 None
    fake_exe = "Z:/fake/path/gui_app.exe"
    assert QQGuiService.get_python_exe(frozen=True, executable=fake_exe) is None


def test_config_load_and_save():
    """验证配置文件的无头读写与字典合并能力。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cfg_path = os.path.join(tmp_dir, "test_config.json")
        # 初始文件不存在时安全返回空字典
        assert QQGuiService.load_config(cfg_path) == {}

        # 写入部分配置
        data = {"account": "123456", "index_days": 30}
        saved = QQGuiService.save_config(data, config_path=cfg_path)
        assert saved is True

        loaded = QQGuiService.load_config(cfg_path)
        assert loaded["account"] == "123456"
        assert loaded["index_days"] == 30

        # 合并保存增量配置
        QQGuiService.save_config({"output_dir": "D:/output"}, config_path=cfg_path)
        updated = QQGuiService.load_config(cfg_path)
        assert updated["account"] == "123456"
        assert updated["output_dir"] == "D:/output"


def test_database_summary_queries():
    """验证在无界面环境下针对 SQLite 数据库的状态统计提取与报告生成。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "test_digest.db")

        # 1. 针对空库测试
        assert QQGuiService.query_database_summary("non_existent.db") is None

        # 2. 构建模拟测试库
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        cur.execute(
            "CREATE TABLE messages (day TEXT, group_code TEXT, group_name TEXT, kind TEXT)"
        )
        cur.execute("CREATE TABLE groups (group_code TEXT, group_name TEXT)")
        cur.execute("CREATE TABLE meta (key TEXT, value TEXT)")

        # 插入元数据
        cur.execute("INSERT INTO meta VALUES ('built_at', '2026-11-01 10:00:00')")
        cur.execute("INSERT INTO meta VALUES ('window_days', '30')")

        # 插入模拟群与消息
        cur.execute("INSERT INTO groups VALUES ('1001', '测试群A')")
        cur.execute("INSERT INTO groups VALUES ('1002', '测试群B')")
        cur.execute(
            "INSERT INTO messages VALUES ('2026-10-15', '1001', '测试群A', 'text')"
        )
        cur.execute(
            "INSERT INTO messages VALUES ('2026-10-16', '1001', '测试群A', 'text')"
        )
        cur.execute(
            "INSERT INTO messages VALUES ('2026-10-20', '1002', '测试群B', 'image')"
        )
        con.commit()
        con.close()

        # 3. 统计查询验证
        stats = QQGuiService.query_database_summary(db_path)
        assert stats is not None
        assert stats["total_msgs"] == 3
        assert stats["total_groups"] == 2
        assert stats["min_day"] == "2026-10-15"
        assert stats["max_day"] == "2026-10-20"
        assert stats["window_days"] == "30"
        assert len(stats["top_groups"]) == 2
        assert stats["top_groups"][0] == ("测试群A", 2)

        # 4. 报告字符串渲染验证
        report_text = QQGuiService.format_database_summary(stats)
        assert "已处理消息总数: 3 条" in report_text
        assert "涉及群组数量: 2 个" in report_text
        assert "测试群A: 2 条消息" in report_text
        assert "text: 2 条" in report_text
        assert "image: 1 条" in report_text


def test_format_empty_database_summary():
    """验证空数据报告格式化。"""
    assert "尚无消息记录" in QQGuiService.format_database_summary({"total_msgs": 0})
    assert "尚无消息记录" in QQGuiService.format_database_summary({})


def test_detect_running_qq_ctypes_fallback(monkeypatch):
    """验证当 psutil 不可用时，detect_running_qq 能安全走 ctypes 降级分支而不报错。"""
    import gui.service as svc
    monkeypatch.setattr(svc, "_HAS_PSUTIL", False)
    monkeypatch.setattr(svc, "psutil", None)

    qq_exe, candidates = QQGuiService.detect_running_qq()
    assert isinstance(candidates, list)
    if qq_exe is not None:
        assert isinstance(qq_exe, str)
        assert len(qq_exe) > 0

