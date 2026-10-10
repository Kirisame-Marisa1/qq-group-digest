#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单元测试：一键初始化向导、增量索引元信息与明文库清理 (test_init_and_build.py)"""

import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(HERE, '..', 'scripts')
sys.path.insert(0, SCRIPTS_DIR)

import ntqq_core
import ntqq_build
import ntqq_init


class TestInitAndBuild(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = self.tmp_dir.name

    def tearDown(self):
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass

    def test_discover_qq_accounts_and_init_project(self):
        """测试自动扫描 Tencent Files 账号目录并生成合法 config.json。"""
        fake_tf = os.path.join(self.root, 'Tencent Files')
        db_dir = os.path.join(fake_tf, '123456789', 'nt_qq', 'nt_db')
        os.makedirs(db_dir, exist_ok=True)
        with open(os.path.join(db_dir, 'nt_msg.db'), 'wb') as f:
            f.write(b'SQLite header 3\x00' + b'\x00' * 100)

        accounts = ntqq_init.discover_qq_accounts(base_dir=fake_tf)
        self.assertEqual(accounts, ['123456789'])

        old_cfg_path = ntqq_core.CONFIG
        tmp_cfg_path = os.path.join(self.root, 'config.json')
        ntqq_core.CONFIG = tmp_cfg_path
        try:
            data_root = os.path.join(self.root, 'cache_root')
            cfg = ntqq_init.init_project(
                qq=None, root=data_root, include_c2c=True,
                base_dir=fake_tf, non_interactive=True
            )
            self.assertEqual(cfg['account'], '123456789')
            self.assertTrue(cfg['include_c2c'])
            ok, errs = ntqq_core.validate_config(cfg)
            self.assertTrue(ok, errs)
            self.assertTrue(os.path.exists(tmp_cfg_path))
        finally:
            ntqq_core.CONFIG = old_cfg_path

    def test_clean_plain_dbs_and_meta_get(self):
        """测试 _meta_get 与 clean_plain_dbs 明文缓存清理。"""
        old_plain = ntqq_build.PLAIN
        plain_dir = os.path.join(self.root, 'plain')
        os.makedirs(plain_dir, exist_ok=True)
        ntqq_build.PLAIN = plain_dir
        try:
            dummy_db = os.path.join(plain_dir, 'nt_msg.db')
            with open(dummy_db, 'wb') as f:
                f.write(b'dummy')
            self.assertTrue(os.path.exists(dummy_db))
            ntqq_build.clean_plain_dbs({'decrypt': ['nt_msg.db', 'group_info.db']})
            self.assertFalse(os.path.exists(dummy_db))

            idx_path = os.path.join(self.root, 'idx.db')
            con = sqlite3.connect(idx_path)
            con.executescript(ntqq_core.INDEX_SCHEMA)
            con.execute("INSERT INTO meta VALUES ('cutoff_ts', '1760000000')")
            con.commit()
            self.assertEqual(ntqq_build._meta_get(con, 'cutoff_ts'), '1760000000')
            self.assertEqual(ntqq_build._meta_get(con, 'nonexistent', '42'), '42')
            con.close()
        finally:
            ntqq_build.PLAIN = old_plain


if __name__ == '__main__':
    unittest.main()

