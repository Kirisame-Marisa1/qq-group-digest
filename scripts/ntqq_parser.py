#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""细粒度 Protobuf 消息载荷解析器与容错降级引擎 (MessageParser)。

职责：
1. 解析富文本消息各段：@提及 (At)、回复引用 (Reply)、合并转发 (Forward)、JSON/XML 卡片消息；
2. 提取卡片核心元数据（Title、URL、Prompt）；
3. 容错降级策略 (Fallback)：当遭遇非预期 Protobuf wire type 或字节损坏时，
   自动平滑降级提取可见 UTF-8 文本块，防止单条畸形消息阻断索引批次。
"""

from datetime import datetime, timezone, timedelta
import html
import json
import re
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
    if i < len(d) and d[i] < 0x80:
        return d[i], i + 1
    v = 0
    s = 0
    while i < len(d):
        if s >= 70:
            raise ValueError("varint too long")
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
    if not v:
        return ""
    if type(v) is bytes or isinstance(v, (bytes, bytearray)):
        try:
            return v.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return v.decode("gbk", errors="ignore")
            except Exception:
                return ""
    return str(v)

_s = _decode_str

# 45002 = 消息段内容类型
CT_KIND = {
    1: 'text', 2: 'image', 3: 'file', 4: 'audio', 5: 'video',
    6: 'sticker', 7: 'reply', 8: 'system', 10: 'card', 16: 'forward'
}
FWD_FIELD = '40900'


def classify(ct: Optional[int], f: Dict[int, List[Any]], sc: Optional[dict] = None) -> str:
    """按 45002 判定类型；缺失时用结构字段兜底。"""
    ct_int = ct if isinstance(ct, int) else 0
    k = CT_KIND.get(ct_int)
    if k == 'file':
        name = (_decode_str(f.get(45402, [b''])[0]) + _decode_str(f.get(45419, [b''])[0])).lower()
        if name.endswith(('.mp4', '.mov', '.mkv', '.avi', '.wmv', '.flv', '.m4v')):
            return 'video'
        return 'file'
    if k:
        return k
    # ct 缺失/未知 -> 结构兜底
    if 45101 in f:
        return 'text'
    if 45402 in f or 45405 in f:
        return 'file'
    if 45526 in f or 45802 in f:
        return 'image'
    if 47601 in f or 47602 in f:
        return 'sticker'
    if 47402 in f or 47403 in f or 47413 in f:
        return 'reply'
    if 48601 in f or 48602 in f:
        return 'forward'
    if 48210 in f or 48501 in f or 48503 in f or 80900 in f:
        return 'system'
    if 48151 in f or 48153 in f:
        return 'call'
    return 'other'


def forward_text(f: Dict[int, List[Any]], sc: dict) -> Tuple[str, str]:
    """从转发子记录里取出正文与类型。"""
    B = sc.get('body_40800', {})
    parts: List[str] = []
    kinds: List[str] = []
    parsed_segs: List[Dict[int, List[Any]]] = []
    for seg in f.get(40800, []):
        try:
            s: Dict[int, List[Any]] = {}
            for a, b, c in pb_fields(seg):
                s.setdefault(a, []).append(c)
        except Exception:
            continue
        parsed_segs.append(s)
        ct_raw = s.get(B.get('content_type', 45002), [0])[0]
        ct = ct_raw if isinstance(ct_raw, int) else 0
        t = _decode_str(s.get(B.get('text', 45101), [b''])[0]).strip()
        if not t:
            for key in ('sys_content', 'reply_summary', 'video_text', 'call_desc', 'filename'):
                tag_no = B.get(key)
                if tag_no and tag_no in s:
                    cand = _decode_str(s[tag_no][0]).strip()
                    if cand:
                        t = cand
                        break
        k = classify(ct, s, sc)
        if k != 'reply':
            kinds.append(k)
        if t:
            parts.append(t)
    if not parts:
        rep_tag = B.get('reply_summary', 47413)
        for s_fb in parsed_segs:
            if rep_tag in s_fb:
                t = _decode_str(s_fb[rep_tag][0]).strip()
                if t:
                    parts.append('引用: ' + t)
                    break
    txt = ' | '.join(parts).strip()
    for pref in ('text', 'forward', 'file', 'video', 'image', 'sticker', 'audio', 'call', 'system'):
        if pref in kinds:
            return pref, txt
    return (kinds[0] if kinds else 'other'), txt


def decode_forward(blob: Optional[bytes], sc: dict, limit: int = 60) -> List[Dict[str, Any]]:
    """展开 40900 合并转发消息内容。"""
    if not blob:
        return []
    out: List[Dict[str, Any]] = []
    try:
        for n, w, v in pb_fields(blob):
            if n != 40900 or w != 2:
                continue
            try:
                f: Dict[int, List[Any]] = {}
                for a, b, c in pb_fields(v):
                    f.setdefault(a, []).append(c)
            except Exception:
                continue
            kind, text = forward_text(f, sc)
            out.append({
                'uid': _decode_str(f.get(40020, [b''])[0]),
                'qq': f.get(40033, [0])[0],
                'ts': f.get(40050, [0])[0],
                'kind': kind,
                'text': text,
                'name': (_decode_str(f.get(40094, [b''])[0]) or _decode_str(f.get(40093, [b''])[0])
                         or _decode_str(f.get(40090, [b''])[0])),
            })
            if len(out) >= limit:
                break
    except Exception:
        pass
    return out


TZ = timezone(timedelta(hours=8))
_EPOCH_TZ = datetime(1970, 1, 1, tzinfo=timezone.utc).astimezone(TZ)


def format_forward(items: List[Dict[str, Any]], per_line: int = 400) -> str:
    """把转发内容排成可读文本。"""
    if not items:
        return ''
    lines = ['[转发聊天记录 %d 条]' % len(items)]
    for m in items:
        t = (m.get('text') or '').replace('\n', ' ')
        if len(t) > per_line:
            t = t[:per_line] + '…'
        qq_val = m.get('qq')
        qq_int = qq_val if isinstance(qq_val, int) else 0
        who = m.get('name') or (('QQ%d' % qq_int) if qq_int else (str(m.get('uid') or '')[:10] or '?'))
        ts_raw = m.get('ts') or 0
        when = ''
        if isinstance(ts_raw, (int, float)) and ts_raw > 1e9:
            try:
                when = '[' + (_EPOCH_TZ + timedelta(seconds=float(ts_raw))).strftime('%Y-%m-%d %H:%M') + '] '
            except (OverflowError, ValueError):
                when = ''
        lines.append('  %s%s: %s' % (when, who, t))
    return '\n'.join(lines)


# 常见卡片/分享关键词与正则（兼容单引号与双引号）
_RE_XML_TITLE = re.compile(r'title=(?:"([^"]+)"|\'([^\']+)\')|<title>([^<]+)</title>', re.I)
_RE_XML_URL = re.compile(r'url=(?:"([^"]+)"|\'([^\']+)\')|<url>([^<]+)</url>|actionData=(?:"([^"]+)"|\'([^\']+)\')', re.I)
_RE_XML_SUMMARY = re.compile(r'summary=(?:"([^"]+)"|\'([^\']+)\')|<summary>([^<]+)</summary>', re.I)


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
                prompt = str(data.get("prompt") or "")
                app = str(data.get("app") or "")
                meta = data.get("meta")
                if not isinstance(meta, dict):
                    meta = {}
                title = ""
                desc = ""
                url = ""
                source_name = app

                # 遍历 meta 内部节点
                for _, v in meta.items():
                    if isinstance(v, dict):
                        title = title or str(v.get("title") or "")
                        desc = desc or str(v.get("desc") or "")
                        url = url or str(v.get("jumpUrl") or v.get("url") or v.get("qqdocurl") or "")
                        source_name = source_name or str(v.get("source") or v.get("tag") or "")

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
        title = html.unescape(next((g for g in title_m.groups() if g), "")) if title_m else ""
        url_m = _RE_XML_URL.search(raw)
        url = html.unescape(next((g for g in url_m.groups() if g), "")) if url_m else ""
        sum_m = _RE_XML_SUMMARY.search(raw)
        desc = html.unescape(next((g for g in sum_m.groups() if g), "")) if sum_m else ""

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
            j = i
            while j < n:
                b = blob[j]
                if (0x20 <= b <= 0x7E) or (0xC2 <= b <= 0xF4) or (0x80 <= b <= 0xBF):
                    j += 1
                else:
                    break

            chunk = blob[i:j]
            if len(chunk) >= min_len:
                s = chunk.decode("utf-8", errors="replace")
                for seg in s.split("\ufffd"):
                    cleaned = re.sub(r'[\x00-\x1f\x7f]', '', seg).strip()
                    has_cjk = any(('\u4e00' <= c <= '\u9fff') or ('\u3400' <= c <= '\u4dbf') or ('\uf900' <= c <= '\ufaff') for c in cleaned)
                    has_sentence = (' ' in cleaned and len(cleaned) >= 8) or len(cleaned) >= 12
                    if cleaned and (has_cjk or has_sentence):
                        extracted.append(cleaned)
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

            ct_raw = f.get(content_type_tag, [0])[0]
            ct = ct_raw if isinstance(ct_raw, int) else 0
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
                if isinstance(md5_bytes, (bytes, bytearray)) and len(md5_bytes) == 16:
                    md5_hex = md5_bytes.hex()
                elif isinstance(md5_bytes, (bytes, bytearray)) and len(md5_bytes) == 32:
                    s_md5 = _decode_str(md5_bytes).strip()
                    md5_hex = s_md5.lower() if re.fullmatch(r'[0-9a-fA-F]{32}', s_md5) else ""
                else:
                    md5_hex = ""
                declared_size_raw = f.get(45405, [0])[0] or 0
                declared_size = declared_size_raw if isinstance(declared_size_raw, int) else 0
                host_bytes = f.get(45816, [b""])[0]
                host = _decode_str(host_bytes)
                p0 = ""
                first_path = ""
                for fno in (45802, 45803, 45804):
                    pv = f.get(fno, [b""])[0]
                    if isinstance(pv, (bytes, bytearray)) and pv:
                        decoded_pv = _decode_str(pv)
                        if not first_path and decoded_pv.startswith("/"):
                            first_path = decoded_pv
                        if b"spec=0" in pv:
                            p0 = decoded_pv
                            break
                if not p0:
                    p0 = first_path

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

        # 兜底：仅当结构未识别（other/empty）且没有任何文本/媒体/引用/卡片时才降级提取文本，避免把图片CDN路径误判为 text
        if (not final_text and overall_kind in ("other", "empty")
                and not images and not ats and reply_info is None and card_info is None and len(blob) > 20):
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
