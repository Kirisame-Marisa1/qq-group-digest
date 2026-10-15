#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视觉降噪漏斗与感知哈希去重 (ImageFilterPipeline)。

职责：
1. L1 元数据与尺寸过滤：跳过 <15KB、低分辨率 (<150x150) 或商城表情 (Market Face)；
2. L2 感知哈希 (dHash) 与表情包黑名单：基于 Pillow 实现 8x8 差异哈希，
   对群聊中重复出现超过 N 次的无意义高频图自动加入本地黑名单；
3. 为后续 L3 本地 OCR / 按需 VLM 调用提供轻量预筛底座。
"""

import io
import os
import sqlite3
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Tuple, Union

try:
    from PIL import Image
    _HAS_PILLOW = True
except ImportError:
    _HAS_PILLOW = False


class FilterTier(Enum):
    PASSED = "passed"
    L1_METADATA_DISCARD = "l1_discard"
    L2_STICKER_DISCARD = "l2_sticker"


@dataclass
class ImageFilterResult:
    decision: FilterTier
    reason: str
    dhash: Optional[str] = None
    width: int = 0
    height: int = 0
    filesize: int = 0


def compute_dhash(img: "Image.Image", hash_size: int = 8) -> str:
    """基于 Pillow 计算图像的差异哈希 (dHash)，返回 16 位 16 进制字符串。"""
    # 转为灰度图并缩放为 (hash_size + 1, hash_size)
    resized = img.convert("L").resize((hash_size + 1, hash_size), Image.Resampling.BILINEAR)
    pixels = list(resized.getdata())

    diff = []
    for row in range(hash_size):
        row_offset = row * (hash_size + 1)
        for col in range(hash_size):
            pixel_left = pixels[row_offset + col]
            pixel_right = pixels[row_offset + col + 1]
            diff.append(pixel_left > pixel_right)

    # 转化为 64-bit 整数后格式化为 hex
    decimal_val = 0
    for idx, bit in enumerate(diff):
        if bit:
            decimal_val |= 1 << idx

    return f"{decimal_val:016x}"


def hamming_distance(hash1: str, hash2: str) -> int:
    """计算两个十六进制哈希字符串之间的汉明距离。"""
    try:
        val1 = int(hash1, 16)
        val2 = int(hash2, 16)
        xor = val1 ^ val2
        return bin(xor).count("1")
    except Exception:
        return 64


class ImageFilterPipeline:
    """多级视觉降噪漏斗。"""

    def __init__(
        self,
        db_path: Optional[str] = None,
        min_filesize: int = 15360,     # 15 KB
        min_dimension: int = 150,      # 150x150
        repeat_threshold: int = 3       # 重复 3 次自动加入表情包黑名单
    ):
        self.min_filesize = min_filesize
        self.min_dimension = min_dimension
        self.repeat_threshold = repeat_threshold
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        """初始化表情包感知哈希黑名单 SQLite 表。"""
        if not self.db_path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        with sqlite3.connect(self.db_path) as con:
            con.execute("""
            CREATE TABLE IF NOT EXISTS sticker_blacklist (
                dhash TEXT PRIMARY KEY,
                count INTEGER DEFAULT 1,
                first_seen INTEGER,
                last_seen INTEGER,
                sample_md5 TEXT,
                is_blacklisted INTEGER DEFAULT 0
            )
            """)
            con.commit()

    def record_and_check_blacklist(self, dhash: str, md5: str = "") -> bool:
        """记录哈希出现频次，若超过阈值则标记为黑名单表情包。返回是否被过滤。"""
        if not self.db_path or not dhash:
            return False

        now = int(time.time())
        is_blocked = False
        with sqlite3.connect(self.db_path) as con:
            cur = con.execute("SELECT count, is_blacklisted FROM sticker_blacklist WHERE dhash=?", (dhash,))
            row = cur.fetchone()
            if row:
                count, blacklisted = row
                new_count = count + 1
                new_blacklisted = 1 if (blacklisted or new_count >= self.repeat_threshold) else 0
                con.execute(
                    "UPDATE sticker_blacklist SET count=?, last_seen=?, is_blacklisted=? WHERE dhash=?",
                    (new_count, now, new_blacklisted, dhash)
                )
                is_blocked = bool(new_blacklisted)
            else:
                con.execute(
                    "INSERT INTO sticker_blacklist (dhash, count, first_seen, last_seen, sample_md5, is_blacklisted) "
                    "VALUES (?, 1, ?, ?, ?, 0)",
                    (dhash, now, now, md5)
                )
                is_blocked = False
            con.commit()
        return is_blocked

    def evaluate_image(
        self,
        target: Union[str, Path, bytes],
        declared_size: int = 0,
        md5: str = "",
        is_market_face: bool = False
    ) -> ImageFilterResult:
        """对单张图片执行 L1 与 L2 漏斗检验。"""
        # 1. 商城表情直接 L1 过滤
        if is_market_face:
            return ImageFilterResult(
                decision=FilterTier.L1_METADATA_DISCARD,
                reason="标记为系统商城表情(Market Face)",
                filesize=declared_size
            )

        # 2. 检查文件大小
        actual_size = len(target) if isinstance(target, bytes) else (os.path.getsize(target) if os.path.exists(target) else 0)
        size = actual_size or declared_size
        if size < self.min_filesize:
            return ImageFilterResult(
                decision=FilterTier.L1_METADATA_DISCARD,
                reason=f"文件体积过小 ({size} 字节 < {self.min_filesize} 字节)",
                filesize=size
            )

        if not _HAS_PILLOW:
            # 环境无 Pillow 时平滑通过
            return ImageFilterResult(
                decision=FilterTier.PASSED,
                reason="Pillow 未安装，跳过分辨率与哈希检查",
                filesize=size
            )

        # 3. 尺寸与分辨率过滤 (L1) + dHash 计算 (L2)
        try:
            if isinstance(target, bytes):
                img = Image.open(io.BytesIO(target))
            else:
                img = Image.open(target)

            width, height = img.size
            if width < self.min_dimension or height < self.min_dimension:
                return ImageFilterResult(
                    decision=FilterTier.L1_METADATA_DISCARD,
                    reason=f"分辨率极低 ({width}x{height} < {self.min_dimension}x{self.min_dimension})",
                    width=width,
                    height=height,
                    filesize=size
                )

            # 计算 dHash
            h = compute_dhash(img)

            # L2 黑名单检查
            is_blacklisted = self.record_and_check_blacklist(h, md5=md5)
            if is_blacklisted:
                return ImageFilterResult(
                    decision=FilterTier.L2_STICKER_DISCARD,
                    reason=f"命中高频表情包哈希黑名单 (dHash={h})",
                    dhash=h,
                    width=width,
                    height=height,
                    filesize=size
                )

            return ImageFilterResult(
                decision=FilterTier.PASSED,
                reason="通过视觉漏斗",
                dhash=h,
                width=width,
                height=height,
                filesize=size
            )

        except Exception as e:
            # 图像损坏或未知格式直接作为低质数据过滤
            return ImageFilterResult(
                decision=FilterTier.L1_METADATA_DISCARD,
                reason=f"图像解析异常: {e}",
                filesize=size
            )

