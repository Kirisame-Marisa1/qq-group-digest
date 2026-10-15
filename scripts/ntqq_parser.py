#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""细粒度 Protobuf 消息载荷解析器与容错降级引擎 (MessageParser)。

职责：
1. 解析富文本消息各段：@提及 (At)、回复引用 (Reply)、合并转发 (Forward)、JSON/XML 卡片消息；
2. 提取卡片核心元数据（Title、URL、Prompt）；
3. 容错降级策略 (Fallback)：当遭遇非预期 Protobuf wire type 或字节损坏时，
   自动平滑降级提取可见 UTF-8 文本块，防止单条畸形消息阻断索引批次。
"""

import json
import re
import struct
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class AtTarget:
    uid: str = ""
    qq: int = 0
    display_name: str = ""
    is_all: bool = False


@dataclass
class ReplyInfo:
    reply_seq: int = 0
    source_sender_uid: str = ""
    source_summary: str = ""


@dataclass
class CardInfo:
    card_type: str = ""          # "json", "xml", "miniapp", "bilibili", "url_share"
    title: str = ""
    desc: str = ""
    url: str = ""
    source_name: str = ""


@dataclass
class ParsedMessage:
    raw_kind: str = "other"                                        # text, image, forward, file, reply, card, system, other
    normalized_text: str = ""                                      # 拼接规范后的展示文本
    ats: List[AtTarget] = field(default_factory=list)
    reply: Optional[ReplyInfo] = None
    card: Optional[CardInfo] = None
    raw_images: List[Dict[str, Any]] = field(default_factory=list)  # md5, host, path, size
    is_fallback: bool = False                                      # 是否触发了容错降级


def _varint(d: bytes, i: int) -> Tuple[int, int]:
    v = 0
    s = 0
    while i < len(d):
        b = d[i]
        i += 1
        v |= (b & 0x7F) << s
        if b < 0x80:
            return v, i
        s += 7
    raise ValueError("bad varint")


def pb_fields(d: bytes) -> List[Tuple[int, int, Any]]:
    """返回 [(field_number, wire_type, value)]，遇异常抛出 ValueError。"""
    out = []
    i = 0
    while i < len(d):
        k, i = _varint(d, i)
        num, wt = k >> 3, k & 7
        if wt == 0:
            v, i = _varint(d, i)
            out.append((num, 0, v))
        elif wt == 2:
            l, i = _varint(d, i)
            if i + l > len(d):
                raise ValueError("truncated")
            out.append((num, 2, d[i:i + l]))
            i += l
        elif wt == 5:
            if i + 4 > len(d):
                raise ValueError("truncated 32-bit")
            out.append((num, 5, d[i:i + 4]))
            i += 4
        elif wt == 1:
            if i + 8 > len(d):
                raise ValueError("truncated 64-bit")
            out.append((num, 1, d[i:i + 8]))
            i += 8
        else:
            raise ValueError(f"unknown wire type {wt}")
    return out


def _decode_str(v: Any) -> str:
    if isinstance(v, (bytes, bytearray)):
        try:
            return v.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return v.decode("gbk", errors="ignore")
            except Exception:
                return ""
    return str(v) if v is not None else ""


# 常见卡片/分享关键词与正则
_RE_XML_TITLE = re.compile(r'title="([^"]+)"|<title>([^<]+)</title>', re.I)
_RE_XML_URL = re.compile(r'url="([^"]+)"|<url>([^<]+)</url>|actionData="([^"]+)"', re.I)
_RE_XML_SUMMARY = re.compile(r'summary="([^"]+)"|<summary>([^<]+)</summary>', re.I)


def extract_card_info(raw_text: str) -> Optional[CardInfo]:
    """尝试将文本解析为 JSON/XML 卡片消息并提取结构化元数据。"""
    raw = (raw_text or "").strip()
    if not raw:
        return None

    # 1. 尝试 JSON 卡片
    if raw.startswith("{") and raw.endswith("}"):
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                # 常见 NTQQ 小程序 / 链接分享卡片结构
                prompt = data.get("prompt") or ""
                app = data.get("app") or ""
                meta = data.get("meta") or {}
                title = ""
                desc = ""
                url = ""
                source_name = app or ""

                # 遍历 meta 内部节点
                for k, v in meta.items():
                    if isinstance(v, dict):
                        title = title or v.get("title") or ""
                        desc = desc or v.get("desc") or ""
                        url = url or v.get("jumpUrl") or v.get("url") or v.get("qqdocurl") or ""
                        source_name = source_name or v.get("source") or v.get("tag") or ""

                if not title and prompt:
                    title = prompt

                if title or url:
                    card_type = "json"
                    if "bilibili" in (app + url).lower():
                        card_type = "bilibili"
                    elif "miniapp" in (app + url).lower() or app.startswith("com.tencent."):
                        card_type = "miniapp"
                    elif url:
                        card_type = "url_share"

                    return CardInfo(
                        card_type=card_type,
                        title=title.strip(),
                        desc=desc.strip(),
                        url=url.strip(),
                        source_name=source_name.strip()
                    )
        except Exception:
            pass

    # 2. 尝试 XML 卡片
    if raw.startswith("<?xml") or raw.startswith("<msg "):
        title_m = _RE_XML_TITLE.search(raw)
        title = (title_m.group(1) or title_m.group(2)) if title_m else ""
        url_m = _RE_XML_URL.search(raw)
        url = (url_m.group(1) or url_m.group(2) or url_m.group(3)) if url_m else ""
        sum_m = _RE_XML_SUMMARY.search(raw)
        desc = (sum_m.group(1) or sum_m.group(2)) if sum_m else ""

        if title or url:
            return CardInfo(
                card_type="xml",
                title=title.strip(),
                desc=desc.strip(),
                url=url.strip(),
                source_name="XML分享"
            )

    return None


class MessageParser:
    """细粒度 Protobuf 消息解析与容错降级处理器。"""

    CT_KIND = {
        1: "text", 2: "image", 3: "file", 4: "audio", 5: "video",
        6: "sticker", 7: "reply", 8: "system", 10: "card", 16: "forward"
    }

    @classmethod
    def fallback_extract_strings(cls, blob: bytes, min_len: int = 3) -> str:
        """从损坏或未知结构的 Protobuf 二进制中尽最大可能提取可读 UTF-8 文本块。"""
        if not blob:
            return ""

        extracted = []
        i = 0
        n = len(blob)

        while i < n:
            # 找到可能的 UTF-8 序列起始
            j = i
            while j < n:
                b = blob[j]
                # 接受可打印 ASCII (0x20..0x7E) 或 UTF-8 多字节序列头 (0xC2..0xF4)
                if (0x20 <= b <= 0x7E) or (0xC2 <= b <= 0xF4) or (0x80 <= b <= 0xBF):
                    j += 1
                else:
                    break

            chunk = blob[i:j]
            if len(chunk) >= min_len:
                try:
                    s = chunk.decode("utf-8")
                    # 过滤纯符号或过多控制字符的碎片
                    cleaned = re.sub(r'[\x00-\x1f\x7f]', '', s).strip()
                    has_cjk = any('\u4e00' <= c <= '\u9fff' for c in cleaned)
                    has_sentence = (' ' in cleaned and len(cleaned) >= 8) or len(cleaned) >= 12
                    if cleaned and (has_cjk or has_sentence):
                        extracted.append(cleaned)
                except UnicodeDecodeError:
                    pass
            i = j + 1

        return " ".join(extracted).strip()

    @classmethod
    def parse_body(cls, blob: Optional[bytes], schema: Optional[dict] = None) -> ParsedMessage:
        """解析 40800 body 载荷，若发生异常自动切换到容错降级提取。"""
        if not blob:
            return ParsedMessage(raw_kind="empty", normalized_text="")

        sc = schema or {}
        B = sc.get("body_40800", {})
        body_content_tag = B.get("body_content", 40800)
        content_type_tag = B.get("content_type", 45002)
        text_tag = B.get("text", 45101)

        try:
            top_fields = pb_fields(blob)
        except Exception:
            fallback_txt = cls.fallback_extract_strings(blob)
            return ParsedMessage(
                raw_kind="text" if fallback_txt else "other",
                normalized_text=fallback_txt,
                is_fallback=True
            )

        text_parts: List[str] = []
        kinds: List[str] = []
        ats: List[AtTarget] = []
        reply_info: Optional[ReplyInfo] = None
        card_info: Optional[CardInfo] = None
        images: List[Dict[str, Any]] = []

        for num, wt, val in top_fields:
            if num != body_content_tag or wt != 2:
                continue

            try:
                segs = pb_fields(val)
            except Exception:
                # 单个段损坏，提取其文本碎片继续
                frag = cls.fallback_extract_strings(val)
                if frag:
                    text_parts.append(frag)
                continue

            f: Dict[int, List[Any]] = {}
            for a, b, c in segs:
                f.setdefault(a, []).append(c)

            ct = f.get(content_type_tag, [0])[0]
            kind = cls.CT_KIND.get(ct, "other")
            if kind == "file":
                fname = (_decode_str(f.get(45402, [b""])[0]) + _decode_str(f.get(45419, [b""])[0])).lower()
                if fname.endswith(('.mp4', '.mov', '.mkv', '.avi', '.wmv', '.flv', '.m4v')):
                    kind = "video"
            elif kind == "other":
                if 45101 in f:
                    kind = "text"
                elif 45402 in f or 45405 in f:
                    fname = (_decode_str(f.get(45402, [b""])[0]) + _decode_str(f.get(45419, [b""])[0])).lower()
                    kind = "video" if fname.endswith(('.mp4', '.mov', '.mkv', '.avi', '.wmv', '.flv', '.m4v')) else "file"
                elif 45526 in f or 45802 in f or 45406 in f:
                    kind = "image"
                elif 47601 in f or 47602 in f:
                    kind = "sticker"
                elif 47402 in f or 47403 in f or 47413 in f:
                    kind = "reply"
                elif 48601 in f or 48602 in f:
                    kind = "forward"
                elif 80900 in f:
                    kind = "system"

            # 1. 提取 At 目标信息
            # NTQQ 中 At 目标可能存在于 45300 (is_all/type) 或 45301/45302 (uid/qq)
            if 45300 in f or 45301 in f or 45302 in f:
                at_flag = f.get(45300, [0])[0]
                is_all = (at_flag == 2)
                uid_bytes = f.get(45301, [b""])[0]
                target_uid = _decode_str(uid_bytes) if isinstance(uid_bytes, (bytes, bytearray)) else ""
                target_qq = f.get(45302, [0])[0]
                at_name = _decode_str(f.get(text_tag, [b""])[0]).lstrip("@")

                ats.append(AtTarget(
                    uid=target_uid,
                    qq=target_qq if isinstance(target_qq, int) else 0,
                    display_name=at_name,
                    is_all=is_all
                ))

            # 2. 提取文本内容
            t = _decode_str(f.get(text_tag, [b""])[0]).strip()
            if not t:
                for key in ("sys_content", "reply_summary", "video_text", "call_desc", "filename"):
                    tag_no = B.get(key)
                    if tag_no and tag_no in f:
                        cand = _decode_str(f[tag_no][0]).strip()
                        if cand:
                            t = cand
                            break

            # 3. 提取引用回复 (Reply)
            if kind == "reply" or 47413 in f or 47402 in f:
                summary = _decode_str(f.get(47413, [b""])[0]).strip()
                rseq = f.get(47402, [0])[0]
                ruid = _decode_str(f.get(47403, [b""])[0]).strip()
                if summary or rseq:
                    reply_info = ReplyInfo(
                        reply_seq=rseq if isinstance(rseq, int) else 0,
                        source_sender_uid=ruid,
                        source_summary=summary
                    )

            # 4. 提取图片元数据
            if kind == "image" or 45406 in f or 45802 in f:
                md5_bytes = f.get(45406, [b""])[0]
                md5_hex = md5_bytes.hex() if isinstance(md5_bytes, (bytes, bytearray)) and len(md5_bytes) == 16 else ""
                declared_size = f.get(45405, [0])[0] or 0
                host_bytes = f.get(45816, [b""])[0]
                host = _decode_str(host_bytes)
                p0 = ""
                for fno in (45802, 45803, 45804):
                    pv = f.get(fno, [b""])[0]
                    if isinstance(pv, (bytes, bytearray)) and b"spec=0" in pv:
                        p0 = _decode_str(pv)
                        break

                if md5_hex:
                    images.append({
                        "md5": md5_hex,
                        "size": declared_size,
                        "host": host,
                        "path": p0
                    })

            # 5. 提取卡片分享 (JSON/XML)
            card = extract_card_info(t)
            if card:
                card_info = card
                kind = "card"
                # 将卡片信息精简格式化为可读文本
                card_summary = f"[分享卡片: {card.title}]"
                if card.url:
                    card_summary += f" ({card.url})"
                t = card_summary

            if t:
                text_parts.append(t)
            kinds.append(kind)

        # 决定整体 kind 优先级：card > text > reply > forward > file > video > image > sticker > audio > system
        overall_kind = "other"
        for pref in ("card", "text", "reply", "forward", "file", "video", "image", "sticker", "audio", "system"):
            if pref in kinds:
                overall_kind = pref
                break
        if overall_kind == "other" and kinds:
            overall_kind = kinds[0]

        final_text = " | ".join(text_parts).strip()

        # 兜底：如果解析结果文本为空但 blob 相当大，尝试 fallback 提取
        if not final_text and len(blob) > 20:
            fb = cls.fallback_extract_strings(blob)
            if fb:
                final_text = fb
                overall_kind = "text"
                return ParsedMessage(
                    raw_kind=overall_kind,
                    normalized_text=final_text,
                    ats=ats,
                    reply=reply_info,
                    card=card_info,
                    raw_images=images,
                    is_fallback=True
                )

        return ParsedMessage(
            raw_kind=overall_kind,
            normalized_text=final_text,
            ats=ats,
            reply=reply_info,
            card=card_info,
            raw_images=images,
            is_fallback=False
        )


def decode_body(blob: Optional[bytes], sc: Optional[dict] = None) -> Tuple[str, str]:
    """向后兼容接口：返回 (kind, text)。"""
    parsed = MessageParser.parse_body(blob, sc)
    return parsed.raw_kind, parsed.normalized_text
