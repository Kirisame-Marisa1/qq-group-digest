import os
import sqlite3
import pytest
from scripts.ntqq_ocr import (
    init_ocr_table,
    record_ocr_result,
    search_ocr_text,
    ocr_image,
    index_media_ocr,
)


def test_init_and_record_ocr_table():
    conn = sqlite3.connect(":memory:")
    init_ocr_table(conn)

    # Idempotent call
    init_ocr_table(conn)

    record_ocr_result(conn, "md5_sample_123", "这是测试通知图片里的文字内容：明天放假通知", "zh-Hans")
    
    # Verify search
    results = search_ocr_text(conn, "放假通知")
    assert len(results) == 1
    assert results[0]["md5"] == "md5_sample_123"
    assert "明天放假通知" in results[0]["ocr_text"]

    # Verify not matching
    no_results = search_ocr_text(conn, "不存在的关键词")
    assert len(no_results) == 0


def test_ocr_image_missing_file():
    # Should gracefully return empty string instead of throwing exception
    res = ocr_image("non_existent_image_file.jpg")
    assert res == ""


def test_index_media_ocr(tmp_path):
    db_file = tmp_path / "test_index.db"
    media_dir = tmp_path / "media"
    media_dir.mkdir()

    # Create dummy file with fake md5
    dummy_img = media_dir / "abcde12345.jpg"
    dummy_img.write_text("not real image")

    conn = sqlite3.connect(str(db_file))
    init_ocr_table(conn)
    conn.close()

    # Test indexing handles corrupted/dummy files gracefully without breaking
    indexed_count = index_media_ocr(str(db_file), str(media_dir))
    assert indexed_count >= 0

