"""
ntqq_ocr.py - 零重依赖的 Windows 原生 WinRT 本地识图与全文检索模块
利用 Windows 10/11 内置的 Windows.Media.Ocr.OcrEngine 进行本地离线识图，
将图片文字索引写入 index.db 中的 media_ocr 表，并在 --search 时联动检索。
"""

import argparse
import contextlib
import os
import re
import sqlite3
import subprocess
import sys
import time
from typing import Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PS1_OCR_SCRIPT = os.path.join(SCRIPT_DIR, "winrt_ocr.ps1")


def _normalize_ocr_text(text: str) -> str:
    """去除 WinRT OCR 在中文字符间插入的多余空格，保留英文单词间空格。"""
    s = (text or "").strip()
    if not s:
        return ""
    return re.sub(r'(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])', '', s)


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


def record_ocr_result(conn: sqlite3.Connection, md5: str, ocr_text: str, language: str = "zh-Hans", commit: bool = True) -> None:
    """记录一条图片的 OCR 识别结果。"""
    now = int(time.time())
    conn.execute("""
        INSERT OR REPLACE INTO media_ocr (md5, ocr_text, language, updated_at)
        VALUES (?, ?, ?, ?)
    """, (md5, ocr_text, language, now))
    if commit:
        conn.commit()


def search_ocr_text(conn: sqlite3.Connection, query: str) -> list[dict]:
    """在 media_ocr 表中模糊检索匹配的图片 MD5（转义 LIKE 通配符）。"""
    if not query:
        return []
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT md5, ocr_text, language, updated_at
        FROM media_ocr
        WHERE ocr_text LIKE ? ESCAPE '\\'
        LIMIT 50
    """, (f"%{escaped}%",))
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


def ocr_image_result(image_path: str) -> Optional[str]:
    """使用 Windows 原生 OCR 识别单个本地图片；失败或非 Windows 返回 None，成功返回识别文本。"""
    if not image_path or not os.path.isfile(image_path):
        return None

    if sys.platform != "win32" or not os.path.isfile(PS1_OCR_SCRIPT):
        return None

    try:
        cmd = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", PS1_OCR_SCRIPT,
            "-ImagePath", os.path.abspath(image_path)
        ]
        sp_kwargs = {
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": 20
        }
        if sys.platform == "win32":
            sp_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

        res = subprocess.run(cmd, **sp_kwargs)
        if res.returncode == 0:
            return _normalize_ocr_text(res.stdout)
        return None
    except Exception:
        return None


def ocr_image(image_path: str) -> str:
    """
    使用 Windows 原生 OCR 识别单个本地图片。
    如果文件不存在、损坏或处于非 Windows 环境，静默降级返回空字符串。
    在 Windows 下使用 CREATE_NO_WINDOW 隐藏控制台弹窗。
    """
    res = ocr_image_result(image_path)
    return res if res is not None else ""


_DEFAULT_OCR_IMAGE_FN = ocr_image


def index_media_ocr(db_path: str, media_dir: str, limit: int = 50) -> int:
    """
    递归扫描 media_dir 目录及子目录下的图片，对尚未识图的图片运行 OCR 并存库。
    """
    if not media_dir or not os.path.exists(media_dir):
        return 0

    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        init_ocr_table(conn)

        cursor = conn.cursor()
        cursor.execute("SELECT md5 FROM media_ocr")
        existing_md5s = {row[0] for row in cursor.fetchall()}

        valid_exts = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
        candidates = []
        seen_md5s = set(existing_md5s)
        for root, _, files in os.walk(media_dir):
            for fname in sorted(files):
                stem, ext = os.path.splitext(fname)
                if ext.lower() in valid_exts:
                    md5_val = stem.lower()
                    if md5_val not in seen_md5s:
                        seen_md5s.add(md5_val)
                        candidates.append((md5_val, os.path.join(root, fname)))

        count = 0
        use_raw = (ocr_image is _DEFAULT_OCR_IMAGE_FN)
        for md5_val, fpath in candidates[:max(0, int(limit))]:
            if use_raw:
                text = ocr_image_result(fpath)
                if text is None:
                    continue
            else:
                text = ocr_image(fpath)
            record_ocr_result(conn, md5_val, text, commit=False)
            count += 1
        if count:
            conn.commit()
        return count


def main() -> int:
    if SCRIPT_DIR not in sys.path:
        sys.path.insert(0, SCRIPT_DIR)
    import ntqq_core
    ap = argparse.ArgumentParser(description="Windows 原生 WinRT 图片 OCR 索引构建")
    ap.add_argument("--batch-size", type=int, default=50, help="本次最多处理的图片数量（默认 50）")
    ap.add_argument("--db", default=None, help="index.db 路径（默认读取配置）")
    ap.add_argument("--media-dir", default=None, help="图片缓存目录（默认读取配置）")
    ap.add_argument("--verbose", action="store_true", help="显示调试日志")
    args = ap.parse_args()
    if args.verbose:
        ntqq_core.set_log_level(True)

    cfg = ntqq_core.load_config()
    db_path = args.db or ntqq_core.INDEX_DB
    media_dir = args.media_dir or cfg.get("media_cache_dir") or ntqq_core.MEDIA
    ntqq_core.ensure_dirs()
    if not os.path.exists(media_dir):
        ntqq_core.log("[ocr] 图片缓存目录不存在，跳过: %s" % media_dir)
        return 0
    cnt = index_media_ocr(db_path, media_dir, limit=args.batch_size)
    ntqq_core.log("[ocr] 本地 OCR 索引完成：新增识别 %d 张图片" % cnt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
