#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安全数据库快照管理器 (SafeDBSnapshot)。

职责：
1. 解决 NTQQ 运行时对 nt_msg.db 高频写入（WAL 模式）导致的 'database is locked' 锁死问题；
2. 原子级拷贝 nt_msg.db 及其关联的 -wal 与 -shm 文件到系统临时目录；
3. 支持上下文管理器协议，解密与查询完毕后自动清理临时副本，保持零残留。
"""

import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional, List, Tuple


class SafeDBSnapshot:
    """SQLite/SQLCipher 活跃数据库的安全热拷贝快照管理器。"""

    def __init__(self, src_db_path: str, temp_dir: Optional[str] = None, max_retries: int = 3, retry_delay: float = 0.5):
        self.src_db_path = Path(src_db_path).resolve()
        self.temp_dir = temp_dir
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self._temp_dir_obj: Optional[tempfile.TemporaryDirectory] = None
        self.snapshot_db_path: Optional[Path] = None
        self.copied_files: List[Path] = []

    def __enter__(self) -> Path:
        return self.create_snapshot()

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.cleanup()

    def _copy_with_retry(self, src: Path, dst: Path) -> None:
        """带重试的文件复制，处理 Windows 下短暂的文件共享读锁问题。"""
        last_err = None
        for attempt in range(self.max_retries):
            try:
                # 使用二进制分块读取，避免一次性加载大文件到内存，并允许共享读取
                with open(src, 'rb') as f_in, open(dst, 'wb') as f_out:
                    shutil.copyfileobj(f_in, f_out, length=1024 * 1024)
                return
            except (OSError, PermissionError) as e:
                last_err = e
                time.sleep(self.retry_delay * (attempt + 1))
        raise OSError(f"无法安全拷贝文件 {src} 到 {dst}: {last_err}") from last_err

    def create_snapshot(self) -> Path:
        """创建数据库热快照，将主库及可能存在的 wal/shm 拷贝到临时目录。"""
        if not self.src_db_path.exists():
            raise FileNotFoundError(f"源数据库不存在: {self.src_db_path}")

        # 创建隔离的临时目录
        self._temp_dir_obj = tempfile.TemporaryDirectory(dir=self.temp_dir, prefix="ntqq_snap_")
        target_dir = Path(self._temp_dir_obj.name)

        db_name = self.src_db_path.name
        self.snapshot_db_path = target_dir / db_name

        # 检查主文件及 wal/shm 伴随文件
        src_parent = self.src_db_path.parent
        suffixes = ["", "-wal", "-shm"]

        for sfx in suffixes:
            companion_src = src_parent / f"{db_name}{sfx}"
            if companion_src.exists():
                companion_dst = target_dir / f"{db_name}{sfx}"
                self._copy_with_retry(companion_src, companion_dst)
                self.copied_files.append(companion_dst)

        return self.snapshot_db_path

    def cleanup(self) -> None:
        """清理临时快照目录。"""
        self.copied_files.clear()
        self.snapshot_db_path = None
        if self._temp_dir_obj is not None:
            try:
                self._temp_dir_obj.cleanup()
            except Exception:
                pass
            finally:
                self._temp_dir_obj = None


def make_safe_snapshot(src_db_path: str, temp_dir: Optional[str] = None) -> SafeDBSnapshot:
    """便捷工厂函数，构造 SafeDBSnapshot 上下文管理器。"""
    return SafeDBSnapshot(src_db_path=src_db_path, temp_dir=temp_dir)

