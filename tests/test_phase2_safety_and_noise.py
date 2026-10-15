#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单元测试：Phase 2 安全快照、Protobuf 容错解析、视觉降噪漏斗与复读机折叠 (test_phase2_safety_and_noise.py)"""

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(HERE, '..', 'scripts')
sys.path.insert(0, SCRIPTS_DIR)

from ntqq_snapshot import SafeDBSnapshot
from ntqq_parser import MessageParser, AtTarget, ReplyInfo, CardInfo, extract_card_info, decode_body
from ntqq_vision_filter import ImageFilterPipeline, FilterTier, compute_dhash, hamming_distance
from ntqq_normalizer import NoiseNormalizer, NormalizedMessage
from ntqq_core import load_schema


def encode_varint(n):
    out = bytearray()
    while n > 0x7F:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n & 0x7F)
    return bytes(out)


def encode_field_varint(field_number, value):
    tag = (field_number << 3) | 0
    return encode_varint(tag) + encode_varint(value)


def encode_field_bytes(field_number, data):
    if isinstance(data, str):
        data = data.encode('utf-8')
    tag = (field_number << 3) | 2
    return encode_varint(tag) + encode_varint(len(data)) + data


class TestSafeDBSnapshot(unittest.TestCase):
    """测试模块 1.1：WAL 快照热拷贝机制。"""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.src_dir = Path(self.tmp_dir.name)
        self.db_path = self.src_dir / "nt_msg.db"
        self.wal_path = self.src_dir / "nt_msg.db-wal"
        self.shm_path = self.src_dir / "nt_msg.db-shm"

        # 写入模拟数据
        self.db_path.write_bytes(b"SQLITE_MAIN_DATA" * 64)
        self.wal_path.write_bytes(b"WAL_UNCHECKPOINTED_DATA" * 32)
        self.shm_path.write_bytes(b"SHM_SHARED_MEMORY" * 16)

    def tearDown(self):
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass

    def test_snapshot_lifecycle_and_companions(self):
        """测试上下文管理器生命周期：主库与 wal/shm 伴随文件被完整安全拷贝，且退出后自动清理。"""
        snap_path = None
        with SafeDBSnapshot(str(self.db_path)) as snap:
            snap_path = snap
            self.assertTrue(snap.exists())
            self.assertEqual(snap.read_bytes(), self.db_path.read_bytes())

            snap_wal = snap.parent / "nt_msg.db-wal"
            snap_shm = snap.parent / "nt_msg.db-shm"
            self.assertTrue(snap_wal.exists())
            self.assertEqual(snap_wal.read_bytes(), self.wal_path.read_bytes())
            self.assertTrue(snap_shm.exists())
            self.assertEqual(snap_shm.read_bytes(), self.shm_path.read_bytes())

        # 退出上下文管理器后，临时目录和副本应已被清理
        self.assertFalse(snap_path.exists())

    def test_snapshot_nonexistent_file_raises(self):
        """测试源库不存在时抛出 FileNotFoundError。"""
        fake_path = self.src_dir / "nonexistent.db"
        snap_mgr = SafeDBSnapshot(str(fake_path))
        with self.assertRaises(FileNotFoundError):
            with snap_mgr:
                pass


class TestMessageParserAndFallback(unittest.TestCase):
    """测试模块 1.2：Protobuf 载荷解析、富文本卡片提取与容错降级。"""

    @classmethod
    def setUpClass(cls):
        cls.sc = load_schema()

    def test_parse_at_and_reply(self):
        """测试 @提及解析 与 回复引用 (reply_seq)。"""
        # 构造一条带 At段 (45300=2 为 @全体成员) 和 回复段 的 Protobuf 消息
        at_seg = (
            encode_field_varint(45002, 1) +
            encode_field_bytes(45101, '@全体成员') +
            encode_field_varint(45300, 2)
        )
        reply_seg = (
            encode_field_varint(45002, 7) +
            encode_field_varint(47402, 10086) +
            encode_field_bytes(47403, 'u_ref_user') +
            encode_field_bytes(47413, '被回复的上一条通知')
        )
        text_seg = (
            encode_field_varint(45002, 1) +
            encode_field_bytes(45101, '请大家准时参会')
        )

        blob = (
            encode_field_bytes(40800, at_seg) +
            encode_field_bytes(40800, reply_seg) +
            encode_field_bytes(40800, text_seg)
        )

        parsed = MessageParser.parse_body(blob, self.sc)
        self.assertFalse(parsed.is_fallback)
        self.assertIn('请大家准时参会', parsed.normalized_text)
        self.assertEqual(len(parsed.ats), 1)
        self.assertTrue(parsed.ats[0].is_all)
        self.assertIsNotNone(parsed.reply)
        self.assertEqual(parsed.reply.reply_seq, 10086)
        self.assertEqual(parsed.reply.source_summary, '被回复的上一条通知')

    def test_parse_json_and_xml_cards(self):
        """测试 JSON / XML 卡片分享标题与 URL 提取。"""
        # 1. JSON 小程序 / 链接分享卡片
        json_card_text = json.dumps({
            "app": "com.tencent.miniapp",
            "prompt": "[QQ小程序] 腾讯文档在线协作",
            "meta": {
                "detail_1": {
                    "title": "项目进度排期表",
                    "desc": "请各模块负责人更新进度",
                    "qqdocurl": "https://docs.qq.com/sheet/D123456"
                }
            }
        }, ensure_ascii=False)

        card_seg = encode_field_varint(45002, 1) + encode_field_bytes(45101, json_card_text)
        blob_json = encode_field_bytes(40800, card_seg)
        parsed_json = MessageParser.parse_body(blob_json, self.sc)

        self.assertEqual(parsed_json.raw_kind, 'card')
        self.assertIsNotNone(parsed_json.card)
        self.assertEqual(parsed_json.card.title, '项目进度排期表')
        self.assertEqual(parsed_json.card.url, 'https://docs.qq.com/sheet/D123456')
        self.assertIn('[分享卡片: 项目进度排期表]', parsed_json.normalized_text)

        # 2. XML 卡片
        xml_card_text = '<msg serviceID="1" templateID="1" action="web" url="https://github.com/test/repo" title="GitHub 热门仓库分享" summary="这是一个优秀的本地开源项目"></msg>'
        card_info = extract_card_info(xml_card_text)
        self.assertIsNotNone(card_info)
        self.assertEqual(card_info.card_type, 'xml')
        self.assertEqual(card_info.title, 'GitHub 热门仓库分享')
        self.assertEqual(card_info.url, 'https://github.com/test/repo')

    def test_corrupted_protobuf_utf8_fallback(self):
        """测试畸形或损坏 Protobuf 数据触发 UTF-8 容错降级提取。"""
        # 模拟 Protobuf 头部与 tag 损坏，但内部含有关键中文文本
        corrupted_blob = b'\xff\xfe\xca\xfe\x00\x00' + '关于明天项目答辩的重要通知：地点调整至教三401教室'.encode('utf-8') + b'\x00\xff'
        parsed = MessageParser.parse_body(corrupted_blob, self.sc)

        self.assertTrue(parsed.is_fallback)
        self.assertEqual(parsed.raw_kind, 'text')
        self.assertIn('关于明天项目答辩的重要通知', parsed.normalized_text)


class TestImageFilterPipeline(unittest.TestCase):
    """测试模块 2.2：L1 尺寸过滤与 L2 dHash 感知哈希表情包黑名单。"""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "stickers.db")
        self.pipeline = ImageFilterPipeline(db_path=self.db_path, min_filesize=15360, min_dimension=150, repeat_threshold=3)

    def tearDown(self):
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass

    def _create_test_image(self, width: int, height: int, color="blue") -> bytes:
        img = Image.new("RGB", (width, height), color=color)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def test_l1_filesize_filter(self):
        """测试 L1：文件体积过小 (<15KB) 判定为废弃。"""
        small_data = b"small_png_data" * 100  # < 15KB
        res = self.pipeline.evaluate_image(small_data, declared_size=len(small_data))
        self.assertEqual(res.decision, FilterTier.L1_METADATA_DISCARD)
        self.assertIn("文件体积过小", res.reason)

    def test_l1_dimension_filter(self):
        """测试 L1：分辨率极低 (<150x150) 判定为废弃。"""
        # 生成 80x80 但填充满假数据使其大于 15KB 的图像
        img_data = self._create_test_image(80, 80)
        padded_data = img_data + b"\x00" * 20000
        res = self.pipeline.evaluate_image(padded_data, declared_size=len(padded_data))
        self.assertEqual(res.decision, FilterTier.L1_METADATA_DISCARD)
        self.assertIn("分辨率极低", res.reason)

    def test_l2_dhash_and_sticker_blacklist(self):
        """测试 L2：dHash 差异哈希计算与重复超过 3 次自动加入表情包黑名单。"""
        # 创建一张 200x200 的有效图像
        img_bytes = self._create_test_image(200, 200, color="green")
        padded_bytes = img_bytes + b"\x00" * 20000

        # 第 1 次与第 2 次检验：正常通过
        res1 = self.pipeline.evaluate_image(padded_bytes, declared_size=len(padded_bytes), md5="md5_test_1")
        self.assertEqual(res1.decision, FilterTier.PASSED)
        self.assertIsNotNone(res1.dhash)

        res2 = self.pipeline.evaluate_image(padded_bytes, declared_size=len(padded_bytes), md5="md5_test_2")
        self.assertEqual(res2.decision, FilterTier.PASSED)

        # 第 3 次检验：达到 repeat_threshold=3，自动标记为黑名单表情包并过滤
        res3 = self.pipeline.evaluate_image(padded_bytes, declared_size=len(padded_bytes), md5="md5_test_3")
        self.assertEqual(res3.decision, FilterTier.L2_STICKER_DISCARD)
        self.assertIn("命中高频表情包哈希黑名单", res3.reason)


class TestNoiseNormalizer(unittest.TestCase):
    """测试模块 3.2：复读机折叠与娱乐机器人指令清洗。"""

    def setUp(self):
        self.normalizer = NoiseNormalizer(min_repeat_to_fold=2)

    def test_repeater_collapsing(self):
        """测试群内连续相同发言被自动折叠。"""
        messages = [
            # 群 1001 中连续 3 人发送 "+1"
            NormalizedMessage(msg_id=1, seq=101, ts=1760000100, day='2026-10-15', hour=10, group_code=1001,
                              group_name='测试群', sender_uid='u1', sender_qq=111, sender_name='张三',
                              direction=0, msg_type=2, subtype=0, kind='text', text='+1', reply_seq=0),
            NormalizedMessage(msg_id=2, seq=102, ts=1760000105, day='2026-10-15', hour=10, group_code=1001,
                              group_name='测试群', sender_uid='u2', sender_qq=222, sender_name='李四',
                              direction=0, msg_type=2, subtype=0, kind='text', text='+1', reply_seq=0),
            NormalizedMessage(msg_id=3, seq=103, ts=1760000110, day='2026-10-15', hour=10, group_code=1001,
                              group_name='测试群', sender_uid='u3', sender_qq=333, sender_name='王五',
                              direction=0, msg_type=2, subtype=0, kind='text', text='+1', reply_seq=0),
            # 群 1001 中接下来的正常发言
            NormalizedMessage(msg_id=4, seq=104, ts=1760000120, day='2026-10-15', hour=10, group_code=1001,
                              group_name='测试群', sender_uid='u4', sender_qq=444, sender_name='赵六',
                              direction=0, msg_type=2, subtype=0, kind='text', text='请问大家今天下午开会吗？', reply_seq=0),
        ]

        normalized = self.normalizer.normalize_batch(messages)
        # 前 3 条应折叠为 1 条，总条数变为 2
        self.assertEqual(len(normalized), 2)
        folded = normalized[0]
        self.assertTrue(folded.is_folded)
        self.assertEqual(folded.repeat_count, 3)
        self.assertEqual(folded.repeated_by, ['张三', '李四', '王五'])
        self.assertIn('复读了: "+1"', folded.text)

        # 正常发言保持原样
        self.assertFalse(normalized[1].is_folded)
        self.assertEqual(normalized[1].text, '请问大家今天下午开会吗？')

    def test_bot_command_filtering(self):
        """测试常见娱乐 Bot 指令过滤。"""
        commands = [
            "/help", "!签到", "#今日运势", "签到", "今日运势", "抽卡",
            "[CQ:at,qq=123456] 签到", ".rd20"
        ]
        for cmd in commands:
            self.assertTrue(self.normalizer.is_bot_command(cmd), f"未能识别 Bot 指令: {cmd}")

        normal_texts = [
            "签到表已经上传到群文件",
            "今天运气不错",
            "大家请在今天下午前完成打卡",
            "帮我看看这段代码为什么报错"
        ]
        for text in normal_texts:
            self.assertFalse(self.normalizer.is_bot_command(text), f"误判正常发言为 Bot 指令: {text}")


if __name__ == '__main__':
    unittest.main()

