#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单元测试：SQLCipher 密钥 HMAC 校验、Windows DPAPI 加解密、配置校验与索引迁移 (test_key_and_dpapi.py)"""

import hashlib
import hmac
import json
import os
import sqlite3
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(HERE, '..', 'scripts')
sys.path.insert(0, SCRIPTS_DIR)

from ntqq_key import (
    verify_key_hmac, EXT_HEADER, PAGE_SIZE, SALT_SIZE,
    KEY_SIZE, IV_SIZE, HMAC_SIZE, RESERVE, HMAC_MASK, FAST_ITER
)
from ntqq_core import (
    dpapi_encrypt, dpapi_decrypt, protect_key_hex, unwrap_key_hex,
    verify_keymap, validate_config, sniff_ext,
    INDEX_SCHEMA, run_index_migrations, rebuild_fts
)


def make_valid_page1(salt_bytes, enc_key_bytes):
    """构造一个合法的 4096 字节 SQLCipher page1：
    - 前 16 字节：salt
    - 中间 4032 字节：加密内容区 (content)
    - 接着 16 字节：IV
    - 接着 20 字节：HMAC-SHA1(hmac_key, content + iv + pack('<I', 1))
    - 最后 12 字节：padding
    """
    assert len(salt_bytes) == SALT_SIZE
    assert len(enc_key_bytes) == KEY_SIZE
    data_end = PAGE_SIZE - RESERVE
    content_len = data_end - SALT_SIZE  # 4032
    content = b'\xAA' * content_len
    iv = b'\x55' * IV_SIZE

    hmac_salt = bytes(b ^ HMAC_MASK for b in salt_bytes)
    hmac_key = hashlib.pbkdf2_hmac('sha512', enc_key_bytes, hmac_salt, FAST_ITER, KEY_SIZE)
    mac = hmac.new(hmac_key, content + iv + struct.pack('<I', 1), hashlib.sha1).digest()
    assert len(mac) == HMAC_SIZE

    padding = b'\x00' * (RESERVE - IV_SIZE - HMAC_SIZE)  # 12 bytes
    page1 = salt_bytes + content + iv + mac + padding
    assert len(page1) == PAGE_SIZE
    return page1


class TestKeyAndDpapi(unittest.TestCase):

    def test_verify_key_hmac(self):
        """测试 ntqq_key.verify_key_hmac：合法 page1 对正确密钥返回 True，错误密钥返回 False。"""
        salt = bytes.fromhex('0123456789abcdef0123456789abcdef')
        good_key = bytes.fromhex('11' * 32)
        bad_key = bytes.fromhex('22' * 32)

        page1 = make_valid_page1(salt, good_key)
        self.assertEqual(len(page1), 4096)
        self.assertTrue(verify_key_hmac(page1, good_key))
        self.assertFalse(verify_key_hmac(page1, bad_key))

        # 篡改 page1 中间内容后应校验失败
        tampered = bytearray(page1)
        tampered[100] ^= 0xFF
        self.assertFalse(verify_key_hmac(bytes(tampered), good_key))

    def test_dpapi_and_key_hex_wrappers(self):
        """测试 dpapi_encrypt/dpapi_decrypt 与 protect_key_hex/unwrap_key_hex 加解密及向后兼容性。"""
        raw_payload = b'ntqq-sqlcipher-test-secret-payload'
        enc_payload = dpapi_encrypt(raw_payload)
        if os.name == 'nt':
            self.assertNotEqual(enc_payload, raw_payload)
        self.assertEqual(dpapi_decrypt(enc_payload), raw_payload)

        raw_hex = 'a1b2c3d4e5f60718293a4b5c6d7e8f90' * 2  # 64 位 hex
        protected = protect_key_hex(raw_hex, use_dpapi=True)
        if os.name == 'nt':
            self.assertTrue(protected.startswith('dpapi:'))
            self.assertNotEqual(protected, raw_hex)
        # unwrap_key_hex 能精确还原 DPAPI 密文
        self.assertEqual(unwrap_key_hex(protected), raw_hex)
        # 向后兼容：传入明文 hex 原样返回
        self.assertEqual(unwrap_key_hex(raw_hex), raw_hex)
        # 禁用 DPAPI 时原样返回
        self.assertEqual(protect_key_hex(raw_hex, use_dpapi=False), raw_hex)
        # 空串边界处理
        self.assertEqual(unwrap_key_hex(''), '')

    def test_verify_keymap(self):
        """测试 verify_keymap：构造模拟加密库（1024 头 + 4096 page1）与 DPAPI 加密的 key_map.json。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            salt = bytes.fromhex('fedcba9876543210fedcba9876543210')
            key_hex = '3c' * 32
            page1 = make_valid_page1(salt, bytes.fromhex(key_hex))

            mock_db = os.path.join(tmpdir, 'nt_msg.db')
            ext_header = b'SQLite header 3\x00' + b'\x00' * (EXT_HEADER - 16)
            with open(mock_db, 'wb') as f:
                f.write(ext_header)
                f.write(page1)

            km_path = os.path.join(tmpdir, 'key_map.json')
            salt_hex = salt.hex()
            km_data = {
                'qq': '12345678',
                'pid': 1000,
                'dpapi_protected': (os.name == 'nt'),
                'key_map': {
                    salt_hex: protect_key_hex(key_hex, use_dpapi=True)
                },
                'salt_files': {
                    salt_hex: [mock_db]
                }
            }
            with open(km_path, 'w', encoding='utf-8') as f:
                json.dump(km_data, f)

            ok, msg = verify_keymap(km_path)
            self.assertTrue(ok)
            self.assertEqual(msg, 'OK (1 个库)')

            # 验证错误密钥时返回 False
            km_data['key_map'][salt_hex] = protect_key_hex('ff' * 32, use_dpapi=True)
            with open(km_path, 'w', encoding='utf-8') as f:
                json.dump(km_data, f)
            ok_bad, msg_bad = verify_keymap(km_path)
            self.assertFalse(ok_bad)
            self.assertIn('校验失败', msg_bad)

    def test_validate_config(self):
        """测试 validate_config：缺少 account 或为 YOUR_QQ 时报错，合法配置通过。"""
        ok1, errs1 = validate_config({})
        self.assertFalse(ok1)
        self.assertTrue(len(errs1) > 0)

        ok2, errs2 = validate_config({'account': 'YOUR_QQ', 'index_days': 30, 'decrypt': ['nt_msg.db']})
        self.assertFalse(ok2)
        self.assertTrue(any('account' in e for e in errs2))

        ok3, errs3 = validate_config({'account': 'not_digits', 'index_days': 30, 'decrypt': ['nt_msg.db']})
        self.assertFalse(ok3)
        self.assertTrue(any('纯数字' in e for e in errs3))

        ok_valid, errs_valid = validate_config({
            'account': '123456789',
            'index_days': 30,
            'decrypt': ['nt_msg.db', 'group_info.db']
        })
        self.assertTrue(ok_valid)
        self.assertEqual(errs_valid, [])

    def test_sniff_ext(self):
        """测试 sniff_ext：验证 jpg/png/gif/webp/bmp 魔数识别。"""
        self.assertEqual(sniff_ext(b'\xff\xd8\xff\xe0\x00\x10JFIF'), '.jpg')
        self.assertEqual(sniff_ext(b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR'), '.png')
        self.assertEqual(sniff_ext(b'GIF87a\x01\x00\x01\x00'), '.gif')
        self.assertEqual(sniff_ext(b'GIF89a\x01\x00\x01\x00'), '.gif')
        self.assertEqual(sniff_ext(b'RIFF\x24\x00\x00\x00WEBP'), '.webp')
        self.assertEqual(sniff_ext(b'BM\x36\x00\x00\x00'), '.bmp')
        self.assertIsNone(sniff_ext(b'\x00\x01\x02\x03random'))

    def test_run_index_migrations_and_rebuild_fts(self):
        """测试 run_index_migrations 与 rebuild_fts：验证迁移后版本号为 '4' 且各表、seq 列与 FTS5 可用。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, 'test_index.db')
            con = sqlite3.connect(db_path)
            try:
                con.executescript(INDEX_SCHEMA)
                run_index_migrations(con)

                ver_row = con.execute("SELECT value FROM meta WHERE key='db_schema_version'").fetchone()
                self.assertIsNotNone(ver_row)
                self.assertEqual(ver_row[0], '4')

                msg_cols = {r[1] for r in con.execute('PRAGMA table_info(messages)')}
                self.assertIn('seq', msg_cols)

                # 验证 c2c_messages、c2c_groups、messages_fts 均存在且可读写
                tables = {
                    r[0] for r in con.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                    )
                }
                self.assertIn('c2c_messages', tables)
                self.assertIn('c2c_groups', tables)
                self.assertIn('messages_fts', tables)

                # 插入测试消息并重建 FTS5 索引
                con.execute(
                    "INSERT INTO messages(msg_id, ts, day, hour, group_code, group_name, "
                    "sender_uid, sender_qq, sender_name, direction, msg_type, subtype, kind, text, reply_seq) "
                    "VALUES (101, 1760000000, '2026-10-01', 12, 10001, '测试群', "
                    "'u1', 111111, '张三', 0, 1, 0, 'text', 'Hello SQLCipher FTS5 World', NULL)"
                )
                con.commit()
                rebuild_fts(con)

                fts_hits = con.execute(
                    "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'SQLCipher'"
                ).fetchall()
                self.assertEqual(len(fts_hits), 1)
                self.assertEqual(fts_hits[0][0], 101)
            finally:
                con.close()


if __name__ == '__main__':
    unittest.main()
