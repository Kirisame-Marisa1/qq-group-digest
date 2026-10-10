#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单元测试：Protobuf wire 解码、消息类型分类与嵌套转发消息展开 (test_protobuf.py)"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(HERE, '..', 'scripts')
sys.path.insert(0, SCRIPTS_DIR)

from ntqq_core import (
    _varint, pb_fields, classify, decode_body,
    forward_text, decode_forward, format_forward, load_schema
)


def encode_varint(n):
    """编码单个無符号整数为 Protobuf varint 字节串。"""
    out = bytearray()
    while n > 0x7F:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n & 0x7F)
    return bytes(out)


def encode_field_varint(field_number, value):
    """编码 wire_type=0 (varint) 字段。"""
    tag = (field_number << 3) | 0
    return encode_varint(tag) + encode_varint(value)


def encode_field_bytes(field_number, data):
    """编码 wire_type=2 (length-delimited) 字段。"""
    if isinstance(data, str):
        data = data.encode('utf-8')
    tag = (field_number << 3) | 2
    return encode_varint(tag) + encode_varint(len(data)) + data


class TestProtobufDecoder(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.sc = load_schema()

    def test_varint_and_pb_fields(self):
        """测试基础 varint 编解码与 pb_fields 解析。"""
        raw = encode_field_varint(45002, 1) + encode_field_bytes(45101, '测试文本')
        fields = pb_fields(raw)
        self.assertEqual(len(fields), 2)
        self.assertEqual(fields[0], (45002, 0, 1))
        self.assertEqual(fields[1], (45101, 2, '测试文本'.encode('utf-8')))

    def test_pb_fields_truncated_raises(self):
        """测试截断的 protobuf 数据能被安全捕获而不崩溃。"""
        bad_blob = encode_varint((40800 << 3) | 2) + encode_varint(100) + b'short'
        with self.assertRaises(ValueError):
            pb_fields(bad_blob)
        # decode_body 面对截断数据应平滑返回 ('other', '')
        kind, txt = decode_body(bad_blob, self.sc)
        self.assertEqual(kind, 'other')
        self.assertEqual(txt, '')

    def test_decode_body_empty(self):
        """测试空字节流解码。"""
        self.assertEqual(decode_body(b'', self.sc), ('empty', ''))
        self.assertEqual(decode_body(None, self.sc), ('empty', ''))

    def test_decode_body_text(self):
        """测试纯文本消息 (content_type=1, text=45101)。"""
        seg = encode_field_varint(45002, 1) + encode_field_bytes(45101, '你好，世界！')
        blob = encode_field_bytes(40800, seg)
        kind, text = decode_body(blob, self.sc)
        self.assertEqual(kind, 'text')
        self.assertEqual(text, '你好，世界！')

    def test_decode_body_image(self):
        """测试图片消息 (content_type=2, filename=45402)。"""
        seg = encode_field_varint(45002, 2) + encode_field_bytes(45402, 'cat.jpg')
        blob = encode_field_bytes(40800, seg)
        kind, text = decode_body(blob, self.sc)
        self.assertEqual(kind, 'image')
        self.assertEqual(text, 'cat.jpg')

    def test_decode_body_file_and_video(self):
        """测试文件消息与视频扩展名自动识别 (content_type=3)。"""
        seg_file = encode_field_varint(45002, 3) + encode_field_bytes(45402, 'report.pdf')
        kind_f, text_f = decode_body(encode_field_bytes(40800, seg_file), self.sc)
        self.assertEqual(kind_f, 'file')
        self.assertEqual(text_f, 'report.pdf')

        seg_video = encode_field_varint(45002, 3) + encode_field_bytes(45402, 'demo.mp4')
        kind_v, text_v = decode_body(encode_field_bytes(40800, seg_video), self.sc)
        self.assertEqual(kind_v, 'video')
        self.assertEqual(text_v, 'demo.mp4')

    def test_decode_body_reply_with_text(self):
        """测试带引用段和正文段的消息：类型优先级应为 text，且保留两段文本。"""
        seg_reply = encode_field_varint(45002, 7) + encode_field_bytes(47413, '原消息摘要')
        seg_text = encode_field_varint(45002, 1) + encode_field_bytes(45101, '这是我的回复')
        blob = encode_field_bytes(40800, seg_reply) + encode_field_bytes(40800, seg_text)
        kind, text = decode_body(blob, self.sc)
        self.assertEqual(kind, 'text')
        self.assertIn('原消息摘要', text)
        self.assertIn('这是我的回复', text)

    def test_decode_body_system(self):
        """测试系统通知消息 (content_type=8, sys_content=80900)。"""
        seg = encode_field_varint(45002, 8) + encode_field_bytes(80900, '管理员撤回了一条消息')
        blob = encode_field_bytes(40800, seg)
        kind, text = decode_body(blob, self.sc)
        self.assertEqual(kind, 'system')
        self.assertEqual(text, '管理员撤回了一条消息')

    def test_decode_forward_multi_leaf_segments(self):
        """测试 40900 转发记录中多个 40800 叶子段（引用段 + 正文段）不丢失正文。"""
        # 构造一条含「引用段 + 文本段」的转发子消息
        leaf_reply = encode_field_varint(45002, 7) + encode_field_bytes(47413, '被引用内容')
        leaf_text = encode_field_varint(45002, 1) + encode_field_bytes(45101, '转发里的真正长文通知内容')
        child_msg = (
            encode_field_bytes(40020, 'u_sender_1') +
            encode_field_varint(40033, 12345678) +
            encode_field_varint(40050, 1760000000) +
            encode_field_bytes(40093, '张三') +
            encode_field_bytes(40800, leaf_reply) +
            encode_field_bytes(40800, leaf_text)
        )
        fwd_blob = encode_field_bytes(40900, child_msg)
        items = decode_forward(fwd_blob, self.sc)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['name'], '张三')
        self.assertEqual(items[0]['qq'], 12345678)
        self.assertEqual(items[0]['kind'], 'text')
        self.assertIn('转发里的真正长文通知内容', items[0]['text'])

        formatted = format_forward(items)
        self.assertIn('[转发聊天记录 1 条]', formatted)
        self.assertIn('张三', formatted)
        self.assertIn('转发里的真正长文通知内容', formatted)


if __name__ == '__main__':
    unittest.main()
