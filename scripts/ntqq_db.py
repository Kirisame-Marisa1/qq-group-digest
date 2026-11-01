#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NTQQ SQLite 数据库连接、索引 Schema、版本迁移与 FTS5 全文索引管理 (ntqq_db)。

职责：
1. 数据库只读安全连接与行迭代器 (open_ro, iter_rows_desc)；
2. 聚合索引数据库结构定义 (INDEX_SCHEMA)；
3. 数据库版本演化与迁移序列 (INDEX_MIGRATIONS, run_index_migrations)；
4. FTS5 全文检索引擎维护与增量同步 (rebuild_fts, sync_fts_incremental)；
5. 高性能连接初始化 (open_index, PRAGMA 调优)。
"""

import os
import sqlite3
import sys
import time
from typing import Any, Callable, Dict, Generator, List, Optional, Tuple


def _get_core():
    """按需延迟导入 ntqq_core 避免循环依赖。"""
    import ntqq_core
    return ntqq_core


INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS messages(
  msg_id INTEGER PRIMARY KEY,
  ts INTEGER, day TEXT, hour INTEGER,
  group_code INTEGER, group_name TEXT,
  sender_uid TEXT, sender_qq INTEGER, sender_name TEXT,
  direction INTEGER, msg_type INTEGER, subtype INTEGER,
  kind TEXT, text TEXT, reply_seq INTEGER, seq INTEGER
);
CREATE INDEX IF NOT EXISTS idx_m_ts ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_m_group_ts ON messages(group_code, ts);
CREATE INDEX IF NOT EXISTS idx_m_sender ON messages(sender_qq, ts);
CREATE TABLE IF NOT EXISTS groups(
  group_code INTEGER PRIMARY KEY, group_name TEXT,
  first_ts INTEGER, last_ts INTEGER, n INTEGER
);
CREATE TABLE IF NOT EXISTS media(
  msg_id INTEGER PRIMARY KEY,
  ts INTEGER, day TEXT, group_code INTEGER, group_name TEXT,
  sender_name TEXT, md5 TEXT, declared_size INTEGER,
  host TEXT, path TEXT
);
CREATE INDEX IF NOT EXISTS idx_media_ts ON media(ts);
CREATE INDEX IF NOT EXISTS idx_media_group ON media(group_code, ts);
CREATE TABLE IF NOT EXISTS c2c_messages(
  msg_id INTEGER PRIMARY KEY,
  ts INTEGER, day TEXT, hour INTEGER,
  group_code INTEGER, group_name TEXT,
  sender_uid TEXT, sender_qq INTEGER, sender_name TEXT,
  direction INTEGER, msg_type INTEGER, subtype INTEGER,
  kind TEXT, text TEXT, reply_seq INTEGER
);
CREATE INDEX IF NOT EXISTS idx_c2c_ts ON c2c_messages(ts);
CREATE INDEX IF NOT EXISTS idx_c2c_group_ts ON c2c_messages(group_code, ts);
CREATE TABLE IF NOT EXISTS c2c_groups(
  group_code INTEGER PRIMARY KEY, group_name TEXT,
  first_ts INTEGER, last_ts INTEGER, n INTEGER
);
CREATE TABLE IF NOT EXISTS group_cursors(
  group_code INTEGER PRIMARY KEY,
  last_processed_msg_seq INTEGER DEFAULT 0,
  last_msg_time INTEGER DEFAULT 0,
  updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS micro_digests(
  cluster_id TEXT PRIMARY KEY,
  group_code INTEGER,
  start_seq INTEGER,
  end_seq INTEGER,
  start_ts INTEGER,
  end_ts INTEGER,
  title TEXT,
  summary TEXT,
  entities TEXT,
  participants TEXT,
  msg_count INTEGER,
  created_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_md_group_ts ON micro_digests(group_code, start_ts);
"""


def _mig_1_media_host_path(con: sqlite3.Connection) -> None:
    """v1: 为老版本 media 表补齐多媒体 CDN 原图 host/path 列。"""
    cols = {r[1] for r in con.execute('PRAGMA table_info(media)')}
    for c in ('host', 'path'):
        if c not in cols:
            con.execute('ALTER TABLE media ADD COLUMN %s TEXT' % c)


def _mig_2_c2c_tables(con: sqlite3.Connection) -> None:
    """v2: 确保私聊独立索引表 c2c_messages / c2c_groups 存在。"""
    con.executescript("""
    CREATE TABLE IF NOT EXISTS c2c_messages(
      msg_id INTEGER PRIMARY KEY,
      ts INTEGER, day TEXT, hour INTEGER,
      group_code INTEGER, group_name TEXT,
      sender_uid TEXT, sender_qq INTEGER, sender_name TEXT,
      direction INTEGER, msg_type INTEGER, subtype INTEGER,
      kind TEXT, text TEXT, reply_seq INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_c2c_ts ON c2c_messages(ts);
    CREATE INDEX IF NOT EXISTS idx_c2c_group_ts ON c2c_messages(group_code, ts);
    CREATE TABLE IF NOT EXISTS c2c_groups(
      group_code INTEGER PRIMARY KEY, group_name TEXT,
      first_ts INTEGER, last_ts INTEGER, n INTEGER
    );
    """)


def _mig_3_fts5(con: sqlite3.Connection) -> None:
    """v3: 建立基于 messages 外部内容的 FTS5 全文检索虚拟表。"""
    core = _get_core()
    try:
        con.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
          text, content='messages', content_rowid='msg_id', tokenize='unicode61'
        )
        """)
    except sqlite3.OperationalError as e:
        core.log_debug('SQLite 环境不支持 FTS5，跳过全文索引创建: %s' % e)


def _mig_4_messages_seq(con: sqlite3.Connection) -> None:
    """v4: 为老版本 messages 表补齐 seq 列（群内连续消息序号 40003）。"""
    cols = {r[1] for r in con.execute('PRAGMA table_info(messages)')}
    if 'seq' not in cols:
        con.execute('ALTER TABLE messages ADD COLUMN seq INTEGER')


def _mig_5_hierarchical_index(con: sqlite3.Connection) -> None:
    """v5: 建立群级别增量游标 group_cursors 与分层微摘要表 micro_digests。"""
    con.executescript("""
    CREATE TABLE IF NOT EXISTS group_cursors (
      group_code INTEGER PRIMARY KEY,
      last_processed_msg_seq INTEGER DEFAULT 0,
      last_msg_time INTEGER DEFAULT 0,
      updated_at INTEGER
    );
    CREATE TABLE IF NOT EXISTS micro_digests (
      cluster_id TEXT PRIMARY KEY,
      group_code INTEGER,
      start_seq INTEGER,
      end_seq INTEGER,
      start_ts INTEGER,
      end_ts INTEGER,
      title TEXT,
      summary TEXT,
      entities TEXT,
      participants TEXT,
      msg_count INTEGER,
      created_at INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_md_group_ts ON micro_digests(group_code, start_ts);
    """)


INDEX_MIGRATIONS: List[Tuple[int, str, Callable[[sqlite3.Connection], None]]] = [
    (1, 'media_host_path', _mig_1_media_host_path),
    (2, 'c2c_tables', _mig_2_c2c_tables),
    (3, 'messages_fts5', _mig_3_fts5),
    (4, 'messages_seq_col', _mig_4_messages_seq),
    (5, 'hierarchical_index', _mig_5_hierarchical_index),
]


def run_index_migrations(con: sqlite3.Connection) -> None:
    """按版本号顺序执行 index.db 结构迁移。"""
    core = _get_core()
    r = con.execute("SELECT value FROM meta WHERE key='db_schema_version'").fetchone()
    cur_ver = int(r[0]) if (r and str(r[0]).isdigit()) else 0
    # 即使版本号已最新，也做一次幂等列检查以兼容未写入 db_schema_version 的旧库
    _mig_1_media_host_path(con)
    _mig_4_messages_seq(con)
    for ver, name, fn in INDEX_MIGRATIONS:
        if cur_ver < ver:
            core.log_debug('[db-migrate] 执行索引迁移 v%d (%s)' % (ver, name))
            fn(con)
            con.execute("INSERT OR REPLACE INTO meta VALUES ('db_schema_version', ?)", (str(ver),))
            cur_ver = ver
    con.commit()


def rebuild_fts(con: sqlite3.Connection) -> None:
    """同步重建 messages_fts 全文索引。"""
    core = _get_core()
    try:
        con.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
        con.commit()
    except sqlite3.OperationalError as e:
        core.log_debug('FTS5 rebuild 跳过: %s' % e)


def sync_fts_incremental(con: sqlite3.Connection, rows: List[Any]) -> None:
    """增量同步 FTS5 虚拟表，避免全表盲目全量重建。"""
    core = _get_core()
    fts_data = [(r[0], r[13]) for r in rows if len(r) > 13 and r[13]]
    if fts_data:
        try:
            con.executemany("INSERT OR REPLACE INTO messages_fts(rowid, text) VALUES (?, ?)", fts_data)
        except sqlite3.DatabaseError as e:
            core.log_debug('FTS5 增量同步异常: %s' % e)


def open_ro(path: str) -> sqlite3.Connection:
    """以只读 URI 模式打开 SQLite 数据库，避免占用或写入。"""
    ap = os.path.abspath(path).replace('\\', '/')
    return sqlite3.connect('file:%s?mode=ro' % ap, uri=True)


def open_index(index_path: Optional[str] = None) -> sqlite3.Connection:
    """初始化并打开聚合索引库 index.db，配置高性能 PRAGMA 选项并自动运行结构迁移。"""
    core = _get_core()
    core.ensure_dirs()
    target_path = (
        index_path
        or getattr(sys.modules.get('scripts.ntqq_core'), 'INDEX_DB', None)
        or getattr(core, 'INDEX_DB', 'data/index.db')
    )
    con = sqlite3.connect(target_path)
    try:
        con.execute("PRAGMA journal_mode = WAL;")
        con.execute("PRAGMA synchronous = NORMAL;")
        con.execute("PRAGMA cache_size = -64000;")     # 64MB 内存页缓存
        con.execute("PRAGMA temp_store = MEMORY;")     # 临时表与排序走内存
        con.execute("PRAGMA mmap_size = 268435456;")   # 256MB 内存映射 I/O
    except sqlite3.DatabaseError as e:
        core.log_debug('SQLite PRAGMA 设置跳过: %s' % e)
    con.executescript(INDEX_SCHEMA)
    run_index_migrations(con)
    return con


def iter_rows_desc(path: str, select_cols: List[str], table: str, where: str = '', params: Tuple[Any, ...] = (), batch: int = 2000,
                   max_rows: int = 0) -> Generator[List[Any], None, None]:
    """倒序游标流式读 SQLite 表。即使页面有轻微损坏，跳过错误行继续读。"""
    core = _get_core()
    con = open_ro(path)
    cur = con.cursor()
    sql = 'SELECT %s FROM %s' % (','.join(select_cols), table)
    if where:
        sql += ' WHERE ' + where
    sql += ' ORDER BY rowid DESC'
    cur.execute(sql, params)
    read_n = 0
    errors = 0
    while True:
        try:
            rows = cur.fetchmany(batch)
        except sqlite3.DatabaseError as e:
            errors += 1
            if errors > 100:
                core.log_warn('  [warn] 遇到过多损坏页，提前停止读取 %s' % table)
                break
            continue
        if not rows:
            break
        for r in rows:
            yield r
            read_n += 1
            if max_rows and read_n >= max_rows:
                con.close()
                return
    con.close()
    if errors:
        core.log_warn('  [warn] 跳过 %d 次损坏读取' % errors)
