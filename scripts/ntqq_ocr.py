"""
ntqq_ocr.py - 零重依赖的 Windows 原生 WinRT 本地识图与全文检索模块
利用 Windows 10/11 内置的 Windows.Media.Ocr.OcrEngine 进行本地离线识图，
将图片文字索引写入 index.db 中的 media_ocr 表，并在 --search 时联动检索。
"""

import os
import sys
import time
import sqlite3
import subprocess

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PS1_OCR_SCRIPT = os.path.join(SCRIPT_DIR, "winrt_ocr.ps1")


def init_ocr_table(conn: sqlite3.Connection) -> None:
    """初始化 media_ocr 数据表。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS media_ocr (
            md5 TEXT PRIMARY KEY,
            ocr_text TEXT,
            language TEXT,
            updated_at INTEGER
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_media_ocr_text ON media_ocr(ocr_text)")
    conn.commit()


def record_ocr_result(conn: sqlite3.Connection, md5: str, ocr_text: str, language: str = "zh-Hans") -> None:
    """记录一条图片的 OCR 识别结果。"""
    now = int(time.time())
    conn.execute("""
        INSERT OR REPLACE INTO media_ocr (md5, ocr_text, language, updated_at)
        VALUES (?, ?, ?, ?)
    """, (md5, ocr_text, language, now))
    conn.commit()


def search_ocr_text(conn: sqlite3.Connection, query: str) -> list[dict]:
    """在 media_ocr 表中模糊检索匹配的图片 MD5。"""
    if not query:
        return []
    cursor = conn.cursor()
    cursor.execute("""
        SELECT md5, ocr_text, language, updated_at
        FROM media_ocr
        WHERE ocr_text LIKE ?
        LIMIT 50
    """, (f"%{query}%",))
    rows = cursor.fetchall()
    return [
        {
            "md5": r[0],
            "ocr_text": r[1],
            "language": r[2],
            "updated_at": r[3],
        }
        for r in rows
    ]


def ocr_image(image_path: str) -> str:
    """
    使用 Windows 原生 OCR 识别单个本地图片。
    如果文件不存在、损坏或处于非 Windows 环境，静默降级返回空字符串。
    """
    if not image_path or not os.path.isfile(image_path):
        return ""

    if sys.platform != "win32" or not os.path.isfile(PS1_OCR_SCRIPT):
        return ""

    try:
        cmd = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", PS1_OCR_SCRIPT,
            "-ImagePath", os.path.abspath(image_path)
        ]
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=15
        )
        if res.returncode == 0:
            return res.stdout.strip()
        return ""
    except Exception:
        return ""


def index_media_ocr(db_path: str, media_dir: str, limit: int = 50) -> int:
    """
    扫描 media_dir 目录下的图片，对尚未识图的图片运行 OCR 并存库。
    """
    if not os.path.exists(media_dir):
        return 0

    conn = sqlite3.connect(db_path)
    init_ocr_table(conn)

    # 查出已存在的 md5
    cursor = conn.cursor()
    cursor.execute("SELECT md5 FROM media_ocr")
    existing_md5s = {row[0] for row in cursor.fetchall()}

    valid_exts = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
    candidates = []
    for fname in os.listdir(media_dir):
        stem, ext = os.path.splitext(fname)
        if ext.lower() in valid_exts:
            # 兼容 <md5>.<ext> 命名
            md5_val = stem.lower()
            if md5_val not in existing_md5s:
                candidates.append((md5_val, os.path.join(media_dir, fname)))

    count = 0
    for md5_val, fpath in candidates[:limit]:
        text = ocr_image(fpath)
        record_ocr_result(conn, md5_val, text)
        count += 1

    conn.close()
    return count
