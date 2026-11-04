"""
ntqq_links.py - 群聊资源与链接归集提取器
从群消息流中自动提取 URL、清洗追踪参数、自动分类（代码/文章/视频/网盘/文档）并生成结构化合辑。
"""

import re
import urllib.parse
from datetime import datetime

# 匹配 URL 的正则（避开中文标点与常见括弧）
URL_REGEX = re.compile(
    r'(https?://[^\s<>"\'，。！？；、“”‘’《》【】]+)',
    re.IGNORECASE
)

TRAILING_PUNCT = ".,!?:;)]}>'\">"

# 常见追踪参数（清洗后可精确去重）
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "spm_id_from", "from_source", "ref", "chksm", "scene", "subscene",
    "share_source", "share_medium", "vd_source", "share_token", "share_id",
    "isappinstalled", "nsukey", "pass_ticket", "wx_header"
}

CATEGORY_MAP = {
    "code": ["github.com", "gitee.com", "gitlab.com", "huggingface.co", "npmjs.com", "pypi.org", "crates.io"],
    "article": ["mp.weixin.qq.com", "zhihu.com", "zhuanlan.zhihu.com", "juejin.cn", "csdn.net", "medium.com", "sspai.com", "infoq.cn", "cnblogs.com", "v2ex.com", "arxiv.org"],
    "video": ["bilibili.com", "b23.tv", "youtube.com", "youtu.be", "douyin.com", "kuaishou.com"],
    "drive": ["pan.baidu.com", "aliyundrive.com", "quark.cn", "123pan.com", "lanzou", "lanzoui.com", "mega.nz", "drive.google.com"],
    "doc": ["docs.qq.com", "feishu.cn", "kdocs.cn", "notion.so", "yuque.com", "wolai.com", "docs.google.com"],
}

CATEGORY_LABELS = {
    "code": "💻 开源与代码仓库",
    "article": "📰 技术文章与资讯",
    "video": "🎬 影音与视频分享",
    "drive": "💾 网盘与资源下载",
    "doc": "📝 在线协作与文档",
    "other": "🔗 其他通用链接",
}


def _get_val(row, keys, default=None):
    """同时兼容 dict 和 sqlite3.Row，并支持字段别名回退。"""
    if isinstance(keys, str):
        keys = [keys]
    for k in keys:
        if isinstance(row, dict):
            if k in row and row[k] is not None:
                return row[k]
        else:
            try:
                val = row[k]
                if val is not None:
                    return val
            except (KeyError, IndexError, TypeError):
                pass
    return default


def clean_url(raw_url: str) -> str:
    """去除 URL 中的跟踪参数，末尾斜杠及标点归一化"""
    try:
        url_stripped = raw_url.rstrip(TRAILING_PUNCT).rstrip("/?#&")
        parsed = urllib.parse.urlparse(url_stripped)
        qs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        cleaned_qs = []
        for k, v in qs:
            kl = k.lower()
            if kl in TRACKING_PARAMS:
                continue
            if kl == "from" and v.lower() in ("singlemessage", "timeline", "groupmessage", "appmessage"):
                continue
            cleaned_qs.append((k, v))
        new_query = urllib.parse.urlencode(cleaned_qs)
        cleaned = urllib.parse.urlunparse((
            parsed.scheme,
            parsed.netloc.lower(),
            parsed.path.rstrip("/") if parsed.path != "/" else parsed.path,
            parsed.params,
            new_query,
            parsed.fragment
        ))
        return cleaned.rstrip("?")
    except Exception:
        return raw_url


def categorize_url(url: str) -> str:
    """根据域名分类"""
    domain = urllib.parse.urlparse(url).netloc.lower()
    for cat, domains in CATEGORY_MAP.items():
        if any(d in domain for d in domains):
            return cat
    return "other"


def extract_links_and_resources(messages: list) -> list[dict]:
    """
    从一组消息中提取资源链接并按 clean_url 去重聚合。
    兼容 dict 与 sqlite3.Row。
    """
    url_map: dict[str, dict] = {}

    for msg in messages:
        text = _get_val(msg, ["text", "msg_text"], "") or ""
        if not text or ("http://" not in text and "https://" not in text):
            continue

        matches = URL_REGEX.findall(text)
        if not matches:
            continue

        sender_name = _get_val(msg, ["sender_name", "name", "disp_name"], "未知群友")
        sender_qq = str(_get_val(msg, ["sender_qq", "sender", "sender_uid"], ""))
        ts = int(_get_val(msg, ["ts", "timestamp"], 0) or 0)
        seq = int(_get_val(msg, ["seq", "msg_seq"], 0) or 0)

        for raw_url in matches:
            cleaned = clean_url(raw_url)
            domain = urllib.parse.urlparse(cleaned).netloc
            cat = categorize_url(cleaned)

            # 提取周边说明文字作为 context
            desc = text.replace(raw_url, "").strip()
            desc = re.sub(r'\s+', ' ', desc)
            if len(desc) > 80:
                desc = desc[:77] + "..."

            if cleaned not in url_map:
                url_map[cleaned] = {
                    "clean_url": cleaned,
                    "raw_url": raw_url,
                    "domain": domain,
                    "category": cat,
                    "senders": [sender_name],
                    "sender_qqs": [sender_qq],
                    "contexts": [desc] if desc else [],
                    "share_count": 1,
                    "first_timestamp": ts,
                    "last_timestamp": ts,
                    "seq": seq,
                }
            else:
                entry = url_map[cleaned]
                entry["share_count"] += 1
                if sender_name not in entry["senders"]:
                    entry["senders"].append(sender_name)
                if sender_qq and sender_qq not in entry["sender_qqs"]:
                    entry["sender_qqs"].append(sender_qq)
                if desc and desc not in entry["contexts"]:
                    entry["contexts"].append(desc)
                if ts > entry["last_timestamp"]:
                    entry["last_timestamp"] = ts

    results = list(url_map.values())
    results.sort(key=lambda x: (x["share_count"], x["last_timestamp"]), reverse=True)

    for item in results:
        best_ctx = max(item["contexts"], key=len) if item["contexts"] else ""
        item["context"] = best_ctx
        item["last_time"] = datetime.fromtimestamp(item["last_timestamp"]).strftime("%m-%d %H:%M") if item["last_timestamp"] else ""

    return results


def format_links_markdown(links: list[dict]) -> str:
    """将归集的链接渲染为 Markdown 板块"""
    if not links:
        return ""

    lines = ["## 📦 群内资源与链接合辑\n"]
    lines.append(f"> 共沉淀 **{len(links)}** 个高价值链接与资源条目。\n")

    by_cat: dict[str, list[dict]] = {}
    for item in links:
        by_cat.setdefault(item["category"], []).append(item)

    preferred_order = ["code", "article", "doc", "drive", "video", "other"]
    for cat in preferred_order:
        items = by_cat.get(cat)
        if not items:
            continue
        title = CATEGORY_LABELS.get(cat, cat)
        lines.append(f"### {title} ({len(items)})\n")
        for it in items:
            senders_str = "、".join(it["senders"][:3])
            count_str = f" `🔥 {it['share_count']}次分享`" if it["share_count"] > 1 else ""
            ctx_str = f" - _{it['context']}_" if it.get("context") else ""
            time_str = f" `[{it['last_time']}]`" if it.get("last_time") else ""
            lines.append(f"- [{it['domain']}]({it['clean_url']}){count_str}{time_str} by **{senders_str}**{ctx_str}")
        lines.append("")

    return "\n".join(lines)
