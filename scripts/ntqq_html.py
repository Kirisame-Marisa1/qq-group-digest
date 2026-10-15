#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""零外部依赖的本地交互式 HTML 可视化简报渲染器 (ntqq_html.py)。

特性：
  1. 100% 本地离线单文件（内联现代 CSS + 原生 JS，不含任何外部 CDN/URL）
  2. 支持三种视图一键切换：
     - 🍱 Bento 交互看板（多群筛选、实时搜索、时段话题聚类、可展开 QQ 气泡、群友画像榜）
     - 📰 纸上日报视图（参考 TraceMemo paperdaily 报纸排版，适合沉浸速读与打印）
     - 📄 Markdown 原文视图（支持一键复制 Markdown）
  3. 支持全群/分群简报、群友发言专报（--member）、关键词检索（--search）与自然语言提问（--ask）
  4. 严格继承 --anonymize 脱敏规则，并使用安全 JSON 注入防止任何聊天内容触发 XSS 或破坏排版
"""
import json
import re
from collections import Counter
from datetime import datetime

SYS_KINDS = ('system', 'empty')
MEDIA_KINDS = ('image', 'sticker', 'audio')
NOTICE_RE = re.compile(
    r'通知|公告|全体成员|@全体|报名|截止|考试|安排|开会|班会|答辩|选课|放假|调休|'
    r'地点|时间|务必|重要|请大家|记得|提交|作业|补考|重修|会议|签到|统计|填表|接龙'
)
CLASS_HINT = re.compile(r'班|通知群|学习|学院|年级|课程|教务')
AT_RE = re.compile(r'@([^\s@|]{1,16})')


def _safe_json_for_script(data):
    """将数据序列化为可安全内嵌进 <script type="application/json"> 的字符串。"""
    raw = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    return (
        raw.replace('&', '\\u0026')
        .replace('<', '\\u003c')
        .replace('>', '\\u003e')
        .replace('\u2028', '\\u2028')
        .replace('\u2029', '\\u2029')
    )


def _period_label(hour):
    if 6 <= hour < 11:
        return '🌅 早间档'
    if 11 <= hour < 14:
        return '☀️ 午间档'
    if 14 <= hour < 18:
        return '☕ 下午档'
    if 18 <= hour < 23:
        return '🌆 晚间黄金档'
    return '🌙 深夜档'


def _compute_persona(name, rank, count, total_conv, user_rows, mentioned_count):
    """根据群友在窗口内的真实发言特征生成趣味画像标签。"""
    share = count * 100.0 / max(1, total_conv)
    if rank == 0 and count >= 3 and share >= 12.0:
        return '👑 榜一担当', 'amber'
    night_n = sum(1 for r in user_rows if r['hour'] in (0, 1, 2, 3, 4, 5))
    if count >= 3 and night_n * 1.0 / count >= 0.35:
        return '🌙 深夜夜猫子', 'purple'
    fwd_n = sum(1 for r in user_rows if r['kind'] == 'forward' or '[转发聊天记录' in (r['text'] or ''))
    if count >= 2 and fwd_n * 1.0 / count >= 0.25:
        return '📢 情报转发员', 'cyan'
    texts = [len((r['text'] or '').strip()) for r in user_rows if (r['text'] or '').strip()]
    avg_len = (sum(texts) / len(texts)) if texts else 0
    if count >= 2 and avg_len >= 48:
        return '📝 长文输出王', 'emerald'
    reply_n = sum(1 for r in user_rows if r['kind'] == 'reply' or '[引用' in (r['text'] or '') or '@' in (r['text'] or ''))
    if count >= 4 and reply_n * 1.0 / count >= 0.35:
        return '💬 接梗互动王', 'blue'
    if mentioned_count >= 2:
        return '🎯 话题焦点', 'rose'
    if rank < 3 and count >= 3:
        return '🔥 核心活跃', 'amber'
    return '⚡ 活跃群友', 'slate'


def _extract_focus_items(allrows, class_groups, fmt_fn, kw_fn, anon_map=None,
                         anon_unknown_fn=None, mask_names_fn=None, code_label_fn=None,
                         is_anon=False, top=40):
    """提取结构化的重点速览条目（用于顶部重点雷达卡片）。"""
    anon_map = anon_map or {}
    class_groups = set(class_groups or ())
    scored = []
    for r in allrows:
        t = (r['text'] or '').strip()
        if not t:
            continue
        s = 0
        tags = []
        gcode = r['group_code']
        gname = r['group_name'] or str(gcode)
        is_cls = (gcode in class_groups) or bool(CLASS_HINT.search(gname))
        if is_cls:
            tags.append('🎓 班群')
            s += 1
        if '@全体' in t or '全体成员' in t:
            s += 3
            tags.append('🚨 @全体')
        if NOTICE_RE.search(t):
            s += 3
            tags.append('📢 通知/重要')
        if '[转发聊天记录' in t or r['kind'] == 'forward':
            s += 2
            tags.append('📦 合并转发')
        if len(t) >= 200:
            s += 4
            tags.append('📝 长文')
        elif len(t) >= 80:
            s += 2
            tags.append('📝 详述')
        if s <= 1:
            continue
        scored.append((s, r['ts'], r, is_cls, tags))
    scored.sort(key=lambda x: (-x[0], -x[1]))

    items = []
    use_anon = is_anon or bool(anon_map)
    for s, ts, r, is_cls, tags in scored[:top]:
        gcode = r['group_code']
        gm = anon_map.get(gcode, {})
        who = (anon_unknown_fn(r['sender_name'] or '', gm) if (use_anon and anon_unknown_fn) else r['sender_name']) or '未知'
        txt = (mask_names_fn(r['text'] or '', gm) if (use_anon and mask_names_fn) else (r['text'] or '')).strip()
        if len(txt) > 500:
            txt = txt[:500] + '…'
        glabel = str(code_label_fn(gcode) if code_label_fn else gcode)
        gname = r['group_name'] or glabel
        if use_anon and mask_names_fn:
            gname = mask_names_fn(gname, gm)
        items.append({
            'score': s,
            'ts': ts,
            'time': fmt_fn(ts, '%m-%d %H:%M'),
            'group_code': glabel,
            'group_name': gname,
            'is_class': is_cls,
            'sender': who,
            'kind': r['kind'] or 'text',
            'text': txt,
            'tags': tags[:4],
        })
    return items


def _build_threads(conv_rows, fmt_fn, kw_fn, nm_fn, mk_fn, show_media=False, max_threads=10):
    """将群内对话按时间静默间隔（25分钟）或篇幅自动切分为「时段话题卡片 + 可展开气泡」。"""
    valid = []
    for r in conv_rows:
        if not show_media and r['kind'] in MEDIA_KINDS:
            continue
        txt = mk_fn(r['text'] or '').strip()
        if not txt:
            continue
        valid.append((r, txt))
    if not valid:
        return []

    raw_bursts = []
    cur = []
    last_ts = None
    for r, txt in valid:
        ts = r['ts']
        if cur and ((ts - last_ts > 1500) or len(cur) >= 42):
            raw_bursts.append(cur)
            cur = []
        cur.append((r, txt))
        last_ts = ts
    if cur:
        raw_bursts.append(cur)

    # 合并过碎的相邻单条消息段（若间隔 < 45 分钟且总段数较多）
    merged = []
    for b in raw_bursts:
        if merged and len(merged[-1]) <= 2 and len(b) <= 2:
            gap = b[0][0]['ts'] - merged[-1][-1][0]['ts']
            if gap < 2700:
                merged[-1].extend(b)
                continue
        merged.append(b)

    # 给每个时段段打分（消息数 + 通知权重 + 字数），挑出最有信息量的 max_threads 段，再按时间正序排列
    scored_bursts = []
    for idx_b, b in enumerate(merged):
        notice_hits = sum(1 for _, t in b if NOTICE_RE.search(t) or '@全体' in t)
        char_sum = sum(min(len(t), 120) for _, t in b)
        weight = len(b) * 12 + notice_hits * 25 + char_sum * 0.15
        scored_bursts.append((weight, idx_b, b))

    if len(scored_bursts) > max_threads:
        top_picked = sorted(scored_bursts, key=lambda x: (-x[0], -x[1]))[:max_threads]
        top_picked.sort(key=lambda x: x[1])
        selected = [b for _, _, b in top_picked]
    else:
        selected = [b for _, _, b in scored_bursts]

    threads = []
    for i, b in enumerate(selected, 1):
        t0 = b[0][0]['ts']
        t1 = b[-1][0]['ts']
        h0 = b[0][0]['hour'] if b[0][0]['hour'] >= 0 else 12
        start_str = fmt_fn(t0, '%m-%d %H:%M')
        end_str = fmt_fn(t1, '%H:%M') if fmt_fn(t0, '%m-%d') == fmt_fn(t1, '%m-%d') else fmt_fn(t1, '%m-%d %H:%M')

        p_cnt = Counter(
            nm_fn(r['sender_name']) for r, _ in b
            if r['sender_name'] not in (None, '', '未知', '系统')
        )
        texts = [t for _, t in b]
        tkws = [w for w, _ in kw_fn(texts, min_count=2, topk=5)]
        if not tkws:
            tkws = [w for w, _ in kw_fn(texts, min_count=1, topk=3)]

        has_notice = any(NOTICE_RE.search(t) or '@全体' in t for t in texts)
        if has_notice:
            if tkws:
                title = '📢 重点通知 · ' + ' / '.join(tkws[:3])
            else:
                first_notice = next((t for t in texts if NOTICE_RE.search(t) or '@全体' in t), texts[0])
                clean_n = first_notice.replace('\n', ' ')[:22]
                title = '📢 ' + clean_n + ('…' if len(first_notice) > 22 else '')
        elif tkws:
            title = ' / '.join(tkws[:3]) + (' 讨论' if len(b) >= 3 else ' 动态')
        elif p_cnt:
            top_u = p_cnt.most_common(1)[0][0]
            title = '%s 等人的互动交流' % top_u
        else:
            title = '时段对话记录 #%d' % i

        # 挑出 2~3 条最具代表性的摘要预览
        hl_cands = []
        for idx_m, (r, t) in enumerate(b):
            sc = min(len(t), 160)
            if NOTICE_RE.search(t) or '@全体' in t:
                sc += 200
            if idx_m == 0:
                sc += 35
            hl_cands.append((sc, idx_m, r, t))
        hl_cands.sort(key=lambda x: -x[0])
        chosen_hl = sorted(hl_cands[:3], key=lambda x: x[1])
        highlights = []
        for _, _, r, t in chosen_hl:
            one_line = t.replace('\n', ' ⏎ ')
            if len(one_line) > 150:
                one_line = one_line[:150] + '…'
            highlights.append({
                'time': fmt_fn(r['ts'], '%H:%M'),
                'sender': nm_fn(r['sender_name']) or '未知',
                'text': one_line,
            })

        bubbles = []
        for r, t in b[:40]:
            clipped = t if len(t) <= 450 else (t[:450] + '…')
            bubbles.append({
                'time': fmt_fn(r['ts'], '%m-%d %H:%M'),
                'sender': nm_fn(r['sender_name']) or '未知',
                'kind': r['kind'] or 'text',
                'text': clipped,
                'is_notice': bool(NOTICE_RE.search(t) or '@全体' in t),
            })

        threads.append({
            'idx': i,
            'period': _period_label(h0),
            'start_time': start_str,
            'end_time': end_str,
            'msg_count': len(b),
            'people_count': len(p_cnt),
            'top_people': [{'name': n, 'count': c} for n, c in p_cnt.most_common(5)],
            'keywords': tkws[:5],
            'has_notice': has_notice,
            'title': title,
            'highlights': highlights,
            'bubbles': bubbles,
            'more_count': max(0, len(b) - len(bubbles)),
        })
    return threads


def build_digest_payload(meta_info, prepared, focus_rows, fmt_fn, kw_fn,
                         anon_map=None, anon_unknown_fn=None, mask_names_fn=None,
                         code_label_fn=None, scrub_fn=None, class_groups=None,
                         max_msgs=150, show_media=False, brief=False, markdown_text=''):
    """为标准群聊/私聊简报构建完整的可视化数据包。"""
    anon_map = anon_map or {}
    class_groups = set(class_groups or ())

    is_anon = bool(meta_info.get('anonymized')) or bool(anon_map)

    def _code_lbl(c):
        return str(code_label_fn(c) if code_label_fn else c)

    focus_items = _extract_focus_items(
        focus_rows, class_groups, fmt_fn, kw_fn,
        anon_map=anon_map, anon_unknown_fn=anon_unknown_fn,
        mask_names_fn=mask_names_fn, code_label_fn=code_label_fn,
        is_anon=is_anon,
        top=(60 if meta_info.get('class_mode') else 30),
    )

    global_hours = Counter()
    global_days = Counter()
    global_people = Counter()
    global_user_rows = {}
    global_mentions = Counter()
    all_conv_texts = []
    total_conv = 0
    total_msgs_in_groups = 0

    groups_data = []
    for nconv, ntot, code, raw_name, rows, prev in prepared:
        gm = anon_map.get(code, {})
        use_anon = is_anon or bool(gm)

        def nm(x, _gm=gm, _ua=use_anon):
            if not x:
                return '未知'
            return anon_unknown_fn(x, _gm) if (_ua and anon_unknown_fn) else x

        def mk(text, _gm=gm, _ua=use_anon):
            if not text:
                return ''
            return mask_names_fn(text, _gm) if (_ua and mask_names_fn) else text

        g_label = _code_lbl(code)
        if meta_info.get('anonymized'):
            g_name = g_label
        else:
            g_name = raw_name or g_label

        is_cls = (code in class_groups) or bool(CLASS_HINT.search(raw_name or ''))
        conv = [r for r in rows if r['kind'] not in SYS_KINDS]
        total_conv += len(conv)
        total_msgs_in_groups += len(rows)

        ts_list = [r['ts'] for r in rows]
        t0_g, t1_g = (min(ts_list), max(ts_list)) if ts_list else (0, 0)
        people = Counter()
        u_rows = {}
        for r in conv:
            sname = r['sender_name']
            if sname not in (None, '', '未知', '系统'):
                disp = nm(sname)
                people[disp] += 1
                u_rows.setdefault(disp, []).append(r)
                global_people[disp] += 1
                global_user_rows.setdefault(disp, []).append(r)

        day_cnt = Counter(r['day'] for r in rows if r['day'])
        hour_cnt = Counter(r['hour'] for r in rows if r['hour'] is not None and r['hour'] >= 0)
        for d, c in day_cnt.items():
            global_days[d] += c
        for h, c in hour_cnt.items():
            global_hours[h] += c

        bucket = set(r['ts'] // 600 for r in rows)
        kinds = Counter(r['kind'] for r in conv)
        sys_cnt = sum(1 for r in rows if r['kind'] in SYS_KINDS)

        ats = Counter()
        g_texts = []
        for r in conv:
            mt = mk(r['text'] or '')
            if mt:
                g_texts.append(mt)
                if len(all_conv_texts) < 15000:
                    all_conv_texts.append(mt)
                for m in AT_RE.findall(mt):
                    ats[m] += 1
                    global_mentions[m] += 1

        kws = kw_fn(g_texts, min_count=2 if len(conv) < 30 else 3, topk=18)

        speakers = []
        for rank_idx, (uname, ucnt) in enumerate(people.most_common(12)):
            persona_lbl, persona_clr = _compute_persona(
                uname, rank_idx, ucnt, len(conv), u_rows.get(uname, []), ats.get(uname, 0)
            )
            speakers.append({
                'name': uname,
                'count': ucnt,
                'pct': round(ucnt * 100.0 / max(1, len(conv)), 1),
                'persona': persona_lbl,
                'color': persona_clr,
            })

        # 提取本群高光金句 / 重点发言 (4~5 条)
        quote_cands = []
        for r in conv:
            mt = mk(r['text'] or '').strip()
            if len(mt) < 12 or mt.startswith('[图片]') or mt.startswith('[表情]'):
                continue
            q_score = 0
            if 20 <= len(mt) <= 220:
                q_score += 3
            elif len(mt) > 220:
                q_score += 2
            if NOTICE_RE.search(mt) or '@全体' in mt:
                q_score += 5
            if r['kind'] == 'reply':
                q_score += 1
            quote_cands.append((q_score, r['ts'], r, mt))
        quote_cands.sort(key=lambda x: (-x[0], -x[1]))
        quotes = []
        seen_q = set()
        for q_sc, q_ts, r, mt in quote_cands:
            short_q = mt.replace('\n', ' ')[:180] + ('…' if len(mt) > 180 else '')
            if short_q in seen_q:
                continue
            seen_q.add(short_q)
            quotes.append({
                'time': fmt_fn(q_ts, '%m-%d %H:%M'),
                'sender': nm(r['sender_name']),
                'text': short_q,
                'is_notice': bool(NOTICE_RE.search(mt) or '@全体' in mt),
            })
            if len(quotes) >= 5:
                break

        threads = _build_threads(conv, fmt_fn, kw_fn, nm, mk, show_media=show_media, max_threads=10)

        show_rows = [r for r in conv if show_media or r['kind'] not in MEDIA_KINDS]
        omitted = len(conv) - len(show_rows)
        if brief:
            show_rows = show_rows[-min(max_msgs, len(show_rows)):]
        else:
            show_rows = show_rows[:max_msgs]

        stream = []
        for r in show_rows:
            mt = mk(r['text'] or '').strip()
            if not mt:
                continue
            if len(mt) > 360:
                mt = mt[:360] + '…'
            stream.append({
                'time': fmt_fn(r['ts'], '%m-%d %H:%M'),
                'sender': nm(r['sender_name']),
                'kind': r['kind'] or 'text',
                'text': mt,
            })

        hours_24 = [hour_cnt.get(h, 0) for h in range(24)]
        peak_h = max(range(24), key=lambda h: hours_24[h]) if any(hours_24) else 0

        groups_data.append({
            'code': g_label,
            'name': g_name,
            'is_class': is_cls,
            'ntot': ntot,
            'nconv': len(conv),
            'prev': prev or 0,
            'delta': ntot - (prev or 0),
            'people_count': len(people),
            'first_time': fmt_fn(t0_g, '%m-%d %H:%M') if t0_g else '-',
            'last_time': fmt_fn(t1_g, '%m-%d %H:%M') if t1_g else '-',
            'span_hours': round((t1_g - t0_g) / 3600.0, 1) if t1_g > t0_g else 0.0,
            'active_mins': len(bucket) * 10,
            'sys_count': sys_cnt,
            'kinds': [{'kind': k, 'count': v} for k, v in kinds.most_common()],
            'hours_24': hours_24,
            'peak_hour': peak_h,
            'days_trend': [{'day': d[5:], 'full_day': d, 'count': day_cnt[d]} for d in sorted(day_cnt)],
            'speakers': speakers,
            'ats': [{'name': n, 'count': c} for n, c in ats.most_common(8)],
            'keywords': [{'word': w, 'count': c} for w, c in kws],
            'quotes': quotes,
            'threads': threads,
            'stream': stream,
            'omitted_media': omitted,
        })

    g_hours_24 = [global_hours.get(h, 0) for h in range(24)]
    g_peak_h = max(range(24), key=lambda h: g_hours_24[h]) if any(g_hours_24) else 0
    g_peak_cnt = g_hours_24[g_peak_h] if any(g_hours_24) else 0

    global_speakers = []
    for rank_idx, (uname, ucnt) in enumerate(global_people.most_common(15)):
        p_lbl, p_clr = _compute_persona(
            uname, rank_idx, ucnt, max(1, total_conv),
            global_user_rows.get(uname, []), global_mentions.get(uname, 0)
        )
        global_speakers.append({
            'name': uname,
            'count': ucnt,
            'pct': round(ucnt * 100.0 / max(1, total_conv), 1),
            'persona': p_lbl,
            'color': p_clr,
        })

    global_kws = [{'word': w, 'count': c} for w, c in kw_fn(all_conv_texts, min_count=3, topk=24)]
    top_grp = groups_data[0] if groups_data else None
    top_spk = global_speakers[0] if global_speakers else None

    return {
        'mode': 'digest',
        'meta': meta_info,
        'kpis': {
            'total_msgs': meta_info.get('cur_n', total_msgs_in_groups),
            'prev_msgs': meta_info.get('prev_n', 0),
            'delta_msgs': meta_info.get('delta', 0),
            'total_conv': total_conv,
            'group_count': len(groups_data),
            'people_count': len(global_people),
            'peak_hour': g_peak_h,
            'peak_hour_str': '%02d:00 - %02d:59' % (g_peak_h, g_peak_h),
            'peak_hour_count': g_peak_cnt,
            'focus_count': len(focus_items),
            'hours_24': g_hours_24,
            'days_trend': [{'day': d[5:], 'full_day': d, 'count': global_days[d]} for d in sorted(global_days)],
            'top_group': {'name': top_grp['name'], 'conv': top_grp['nconv'], 'ntot': top_grp['ntot']} if top_grp else None,
            'top_speaker': {'name': top_spk['name'], 'count': top_spk['count'], 'persona': top_spk['persona']} if top_spk else None,
            'global_speakers': global_speakers,
            'global_keywords': global_kws,
        },
        'focus_items': focus_items,
        'groups': groups_data,
        'markdown': markdown_text,
    }


def build_generic_payload(mode, meta_info, markdown_text, extra=None):
    """为 --member / --search / --ask 模式构建可视化数据包（自动将 Markdown 结构化解析为卡片与时间轴）。"""
    payload = {
        'mode': mode,
        'meta': meta_info,
        'extra': extra or {},
        'markdown': markdown_text,
    }
    return payload


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__PAGE_TITLE__</title>
<style>
:root[data-theme="dark"] {
  --bg: #0b0f17;
  --bg-elev: #111827;
  --card: #151e2e;
  --card-hover: #1b263b;
  --card-alt: #0f1623;
  --border: rgba(148, 163, 184, 0.14);
  --border-strong: rgba(148, 163, 184, 0.26);
  --text: #f1f5f9;
  --text-sub: #94a3b8;
  --text-muted: #64748b;
  --accent: #38bdf8;
  --accent-soft: rgba(56, 189, 248, 0.14);
  --amber: #f59e0b;
  --amber-soft: rgba(245, 158, 11, 0.14);
  --emerald: #10b981;
  --emerald-soft: rgba(16, 185, 129, 0.14);
  --rose: #f43f5e;
  --rose-soft: rgba(244, 63, 94, 0.14);
  --purple: #a855f7;
  --purple-soft: rgba(168, 85, 247, 0.14);
  --cyan: #06b6d4;
  --cyan-soft: rgba(6, 182, 212, 0.14);
  --bubble-bg: #1e293b;
  --bubble-notice: rgba(245, 158, 11, 0.12);
  --shadow: 0 10px 30px -10px rgba(0, 0, 0, 0.55);
  --paper-bg: #141413;
  --paper-card: #1c1b18;
  --paper-ink: #e7e5e4;
  --paper-rule: #44403c;
  --claude-bg: #141413;
  --claude-surface: #1e1d1b;
  --claude-surface-warm: #252421;
  --claude-border: #33312e;
  --claude-border-subtle: #292724;
  --claude-text: #ece8e1;
  --claude-text-sub: #a39e93;
  --claude-text-muted: #78736b;
  --claude-terracotta: #e28567;
  --claude-terracotta-hover: #f0987d;
  --claude-terracotta-soft: rgba(226, 133, 103, 0.16);
  --claude-blue: #88b4de;
  --claude-blue-soft: rgba(136, 180, 222, 0.16);
  --claude-green: #8fa671;
  --claude-green-soft: rgba(143, 166, 113, 0.16);
  --claude-shadow: 0 4px 24px -2px rgba(0, 0, 0, 0.45);
}
:root[data-theme="light"] {
  --bg: #f1f5f9;
  --bg-elev: #ffffff;
  --card: #ffffff;
  --card-hover: #f8fafc;
  --card-alt: #f8fafc;
  --border: rgba(15, 23, 42, 0.09);
  --border-strong: rgba(15, 23, 42, 0.18);
  --text: #0f172a;
  --text-sub: #475569;
  --text-muted: #64748b;
  --accent: #0284c7;
  --accent-soft: rgba(2, 132, 199, 0.10);
  --amber: #d97706;
  --amber-soft: rgba(217, 119, 6, 0.10);
  --emerald: #059669;
  --emerald-soft: rgba(5, 150, 105, 0.10);
  --rose: #e11d48;
  --rose-soft: rgba(225, 29, 72, 0.10);
  --purple: #7e22ce;
  --purple-soft: rgba(126, 34, 206, 0.10);
  --cyan: #0891b2;
  --cyan-soft: rgba(8, 145, 178, 0.10);
  --bubble-bg: #f1f5f9;
  --bubble-notice: #fffbeb;
  --shadow: 0 8px 24px -8px rgba(15, 23, 42, 0.08);
  --paper-bg: #f7f4eb;
  --paper-card: #fdfbf7;
  --paper-ink: #1c1917;
  --paper-rule: #292524;
  --claude-bg: #faf9f5;
  --claude-surface: #ffffff;
  --claude-surface-warm: #f7f5ee;
  --claude-border: #e8e6dc;
  --claude-border-subtle: #f0ede5;
  --claude-text: #141413;
  --claude-text-sub: #6e665e;
  --claude-text-muted: #87867f;
  --claude-terracotta: #d97757;
  --claude-terracotta-hover: #c96442;
  --claude-terracotta-soft: rgba(217, 119, 87, 0.12);
  --claude-blue: #6a9bcc;
  --claude-blue-soft: rgba(106, 155, 204, 0.12);
  --claude-green: #788c5d;
  --claude-green-soft: rgba(120, 140, 93, 0.12);
  --claude-shadow: 0 4px 20px -2px rgba(20, 20, 19, 0.05), 0 2px 6px -1px rgba(20, 20, 19, 0.03);
}
:root {
  --claude-serif: "Lora", "Georgia", "Songti SC", "STSong", "Source Han Serif CN", "Noto Serif CJK SC", "SimSun", serif;
  --claude-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  --claude-mono: "SF Mono", "Fira Code", "Consolas", monospace;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
  padding-bottom: 60px;
}
a { color: var(--accent); text-decoration: none; }
button, input, select { font-family: inherit; }

/* Top Navbar */
.topbar {
  position: sticky;
  top: 0;
  z-index: 100;
  background: color-mix(in srgb, var(--bg-elev) 88%, transparent);
  backdrop-filter: blur(14px);
  border-bottom: 1px solid var(--border);
  padding: 12px 24px;
}
.topbar-inner {
  max-width: 1440px;
  margin: 0 auto;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.brand {
  display: flex;
  align-items: center;
  gap: 12px;
}
.brand-logo {
  width: 38px;
  height: 38px;
  border-radius: 10px;
  background: linear-gradient(135deg, #0ea5e9, #6366f1);
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-weight: 800;
  font-size: 18px;
  box-shadow: 0 4px 12px rgba(14, 165, 233, 0.35);
}
.brand-title { font-size: 17px; font-weight: 700; letter-spacing: -0.01em; }
.brand-sub { font-size: 12px; color: var(--text-sub); display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.status-dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: var(--emerald);
  display: inline-block;
  box-shadow: 0 0 8px var(--emerald);
}

.toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.mode-switch {
  display: inline-flex;
  background: var(--card-alt);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 3px;
}
.mode-btn {
  border: none;
  background: transparent;
  color: var(--text-sub);
  padding: 6px 12px;
  border-radius: 7px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.15s;
}
.mode-btn.active {
  background: var(--card);
  color: var(--text);
  box-shadow: 0 2px 6px rgba(0,0,0,0.18);
}
.icon-btn {
  border: 1px solid var(--border);
  background: var(--card);
  color: var(--text);
  padding: 6px 12px;
  border-radius: 9px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.15s;
}
.icon-btn:hover { border-color: var(--accent); color: var(--accent); }

.container {
  max-width: 1440px;
  margin: 0 auto;
  padding: 20px 24px;
}

/* Integrity & Alerts */
.alert-bar {
  background: var(--card);
  border: 1px solid var(--border);
  border-left: 4px solid var(--accent);
  border-radius: 12px;
  padding: 12px 16px;
  margin-bottom: 16px;
  font-size: 13px;
  color: var(--text-sub);
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.alert-bar.warn { border-left-color: var(--amber); }

/* KPI 5-Grid */
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 14px;
  margin-bottom: 20px;
}
.kpi-card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 16px 18px;
  box-shadow: var(--shadow);
  position: relative;
  overflow: hidden;
}
.kpi-label {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-sub);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  margin-bottom: 6px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.kpi-val {
  font-size: 28px;
  font-weight: 800;
  letter-spacing: -0.02em;
  line-height: 1.2;
}
.kpi-foot {
  font-size: 12px;
  color: var(--text-sub);
  margin-top: 8px;
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
  border: 1px solid transparent;
}
.pill-accent { background: var(--accent-soft); color: var(--accent); border-color: rgba(56,189,248,0.25); }
.pill-amber { background: var(--amber-soft); color: var(--amber); border-color: rgba(245,158,11,0.25); }
.pill-emerald { background: var(--emerald-soft); color: var(--emerald); border-color: rgba(16,185,129,0.25); }
.pill-rose { background: var(--rose-soft); color: var(--rose); border-color: rgba(244,63,94,0.25); }
.pill-purple { background: var(--purple-soft); color: var(--purple); border-color: rgba(168,85,247,0.25); }
.pill-cyan { background: var(--cyan-soft); color: var(--cyan); border-color: rgba(6,182,212,0.25); }
.pill-slate { background: rgba(148,163,184,0.12); color: var(--text-sub); border-color: var(--border); }

/* Priority Radar */
.section-card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 18px 20px;
  margin-bottom: 20px;
  box-shadow: var(--shadow);
}
.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 14px;
}
.section-title {
  font-size: 16px;
  font-weight: 700;
  display: flex;
  align-items: center;
  gap: 8px;
}
.radar-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
  gap: 12px;
  max-height: 390px;
  overflow-y: auto;
  padding-right: 4px;
}
.radar-item {
  background: var(--card-alt);
  border: 1px solid var(--border);
  border-left: 3px solid var(--amber);
  border-radius: 11px;
  padding: 12px 14px;
  cursor: pointer;
  transition: all 0.15s;
}
.radar-item:hover {
  border-color: var(--amber);
  transform: translateY(-1px);
}
.radar-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  font-size: 12px;
  color: var(--text-sub);
  margin-bottom: 6px;
}
.radar-text {
  font-size: 13px;
  color: var(--text);
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
  word-break: break-word;
}

/* Group Selector & Filter Bar */
.filter-bar {
  position: sticky;
  top: 63px;
  z-index: 90;
  background: color-mix(in srgb, var(--bg) 92%, transparent);
  backdrop-filter: blur(12px);
  padding: 10px 0 14px 0;
  margin-bottom: 16px;
  border-bottom: 1px solid var(--border);
}
.filter-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.search-input {
  flex: 1;
  min-width: 260px;
  background: var(--card);
  border: 1px solid var(--border-strong);
  color: var(--text);
  padding: 9px 14px;
  border-radius: 10px;
  font-size: 13px;
  outline: none;
}
.search-input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
.group-tabs {
  display: flex;
  gap: 8px;
  overflow-x: auto;
  padding-bottom: 4px;
  scrollbar-width: thin;
}
.group-tab {
  border: 1px solid var(--border);
  background: var(--card);
  color: var(--text-sub);
  padding: 7px 13px;
  border-radius: 999px;
  font-size: 13px;
  font-weight: 600;
  white-space: nowrap;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  transition: all 0.15s;
}
.group-tab:hover { border-color: var(--accent); color: var(--text); }
.group-tab.active {
  background: var(--accent);
  color: #fff;
  border-color: var(--accent);
}
.group-tab.active .tab-cnt {
  background: rgba(255,255,255,0.22);
  color: #fff;
}
.tab-cnt {
  background: var(--card-alt);
  color: var(--text-sub);
  padding: 1px 7px;
  border-radius: 999px;
  font-size: 11px;
}

/* Bento Main 2-Column Layout */
.bento-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 370px;
  gap: 20px;
  align-items: start;
}
@media (max-width: 1080px) {
  .bento-layout { grid-template-columns: 1fr; }
}

/* Group Overview Cards Grid (All Groups View) */
.group-cards-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 14px;
}
.grp-overview-card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 16px;
  cursor: pointer;
  transition: all 0.16s;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  gap: 10px;
}
.grp-overview-card:hover {
  border-color: var(--accent);
  transform: translateY(-2px);
  box-shadow: var(--shadow);
}

/* Thread / Topic Cluster Card */
.thread-card {
  background: var(--card-alt);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 16px 18px;
  margin-bottom: 14px;
  transition: border-color 0.15s;
}
.thread-card:hover { border-color: var(--border-strong); }
.thread-card.notice-thread { border-left: 4px solid var(--amber); }
.thread-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.thread-title {
  font-size: 15px;
  font-weight: 700;
  color: var(--text);
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.thread-highlights {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 10px 12px;
  margin: 10px 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.hl-row {
  font-size: 13px;
  color: var(--text-sub);
  word-break: break-word;
}
.hl-row strong { color: var(--text); font-weight: 600; }

/* Chat Bubbles */
.bubble-drawer {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px dashed var(--border-strong);
  display: none;
  flex-direction: column;
  gap: 10px;
  max-height: 520px;
  overflow-y: auto;
  padding-right: 6px;
}
.bubble-drawer.open { display: flex; }
.chat-item {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}
.avatar {
  width: 32px;
  height: 32px;
  border-radius: 9px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-weight: 700;
  font-size: 13px;
  user-select: none;
}
.bubble-wrap {
  flex: 1;
  min-width: 0;
}
.bubble-meta {
  font-size: 11px;
  color: var(--text-muted);
  margin-bottom: 3px;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.bubble-sender { font-weight: 600; color: var(--text-sub); font-size: 12px; }
.bubble-body {
  display: inline-block;
  max-width: 100%;
  background: var(--bubble-bg);
  border: 1px solid var(--border);
  border-radius: 4px 12px 12px 12px;
  padding: 8px 12px;
  font-size: 13.5px;
  color: var(--text);
  white-space: pre-wrap;
  word-break: break-word;
}
.bubble-body.is-notice {
  background: var(--bubble-notice);
  border-color: rgba(245, 158, 11, 0.35);
}

/* 24h Bar Chart */
.chart-24h {
  display: grid;
  grid-template-columns: repeat(24, 1fr);
  gap: 4px;
  align-items: end;
  height: 96px;
  padding-top: 18px;
  margin-top: 8px;
}
.bar-col {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: flex-end;
  height: 100%;
  gap: 4px;
}
.bar-fill {
  width: 100%;
  min-height: 3px;
  border-radius: 4px 4px 2px 2px;
  background: var(--accent);
  opacity: 0.78;
  transition: all 0.2s;
}
.bar-fill.peak {
  background: var(--amber);
  opacity: 1;
  box-shadow: 0 0 10px rgba(245, 158, 11, 0.45);
}
.bar-col:hover .bar-fill { opacity: 1; transform: scaleY(1.04); }
.bar-lbl { font-size: 10px; color: var(--text-muted); font-family: monospace; }

/* Speaker Leaderboard */
.speaker-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 7px 8px;
  border-radius: 9px;
  cursor: pointer;
  transition: background 0.15s;
}
.speaker-row:hover { background: var(--card-alt); }
.speaker-info { flex: 1; min-width: 0; }
.speaker-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  font-size: 13px;
  margin-bottom: 4px;
}
.speaker-name {
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.progress-track {
  height: 5px;
  background: var(--card-alt);
  border-radius: 999px;
  overflow: hidden;
}
.progress-bar {
  height: 100%;
  background: linear-gradient(90deg, var(--accent), #818cf8);
  border-radius: 999px;
}

/* Pull Quotes */
.quote-card {
  background: var(--card-alt);
  border: 1px solid var(--border);
  border-left: 3px solid var(--accent);
  border-radius: 10px;
  padding: 10px 12px;
  margin-bottom: 10px;
  font-size: 13px;
}
.quote-card.notice { border-left-color: var(--amber); }

/* Editorial Paper Daily View (TraceMemo paperdaily style) */
.paper-view {
  background: var(--paper-bg);
  color: var(--paper-ink);
  border: 1px solid var(--border-strong);
  border-radius: 12px;
  padding: 36px 40px;
  max-width: 1260px;
  margin: 0 auto;
  font-family: "Georgia", "Songti SC", "SimSun", "STSong", serif;
  box-shadow: var(--shadow);
}
.paper-masthead {
  border-bottom: 3px double var(--paper-rule);
  padding-bottom: 16px;
  margin-bottom: 24px;
  text-align: center;
}
.paper-title {
  font-size: 40px;
  font-weight: 900;
  letter-spacing: 0.08em;
  margin-bottom: 8px;
}
.paper-meta-strip {
  border-top: 1px solid var(--paper-rule);
  border-bottom: 1px solid var(--paper-rule);
  padding: 6px 12px;
  font-size: 13px;
  display: flex;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px;
  font-family: sans-serif;
}
.paper-columns {
  column-count: 2;
  column-gap: 36px;
  column-rule: 1px solid var(--border-strong);
}
@media (max-width: 900px) {
  .paper-columns { column-count: 1; }
  .paper-view { padding: 20px 18px; }
}
.paper-article {
  break-inside: avoid;
  margin-bottom: 26px;
  padding-bottom: 18px;
  border-bottom: 1px solid var(--border);
}
.paper-article h3 {
  font-size: 20px;
  font-weight: 800;
  margin-bottom: 6px;
}
.paper-byline {
  font-size: 12px;
  color: var(--text-sub);
  font-family: sans-serif;
  margin-bottom: 10px;
}

/* Markdown Raw Viewer */
.md-box {
  background: var(--card-alt);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 18px;
  font-family: "Consolas", "JetBrains Mono", monospace;
  font-size: 13px;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 78vh;
  overflow-y: auto;
}
mark.hl {
  background: rgba(245, 158, 11, 0.35);
  color: inherit;
  padding: 0 2px;
  border-radius: 3px;
}

/* Claude / Anthropic Reading Experience */
.claude-layout {
  display: flex;
  gap: 24px;
  align-items: flex-start;
  max-width: 1440px;
  margin: 0 auto;
}
.claude-sidebar {
  width: 320px;
  flex-shrink: 0;
  position: sticky;
  top: 76px;
  max-height: calc(100vh - 96px);
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding-right: 4px;
}
.claude-sidebar::-webkit-scrollbar {
  width: 5px;
}
.claude-sidebar::-webkit-scrollbar-thumb {
  background: var(--claude-border);
  border-radius: 999px;
}
.claude-sidebar-card {
  background: var(--claude-surface);
  border: 1px solid var(--claude-border);
  border-radius: 12px;
  padding: 16px;
  box-shadow: var(--claude-shadow);
}
.claude-search-box {
  position: relative;
  margin-bottom: 8px;
}
.claude-search-input {
  width: 100%;
  padding: 8px 12px 8px 32px;
  background: var(--claude-surface-warm);
  border: 1px solid var(--claude-border);
  border-radius: 8px;
  font-size: 13px;
  color: var(--claude-text);
  outline: none;
  transition: all 0.15s;
}
.claude-search-input:focus {
  border-color: var(--claude-terracotta);
  box-shadow: 0 0 0 3px var(--claude-terracotta-soft);
  background: var(--claude-surface);
}
.claude-search-icon {
  position: absolute;
  left: 10px;
  top: 50%;
  transform: translateY(-50%);
  color: var(--claude-text-muted);
  font-size: 13px;
  pointer-events: none;
}
.claude-group-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 48vh;
  overflow-y: auto;
}
.claude-group-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 9px 12px;
  border-radius: 8px;
  border: 1px solid transparent;
  background: transparent;
  color: var(--claude-text);
  cursor: pointer;
  text-align: left;
  font-size: 13px;
  transition: all 0.15s;
  width: 100%;
  text-decoration: none;
}
.claude-group-item:hover {
  background: var(--claude-surface-warm);
  border-color: var(--claude-border-subtle);
}
.claude-group-item.active {
  background: var(--claude-surface-warm);
  border-color: var(--claude-border);
  border-left: 3.5px solid var(--claude-terracotta);
  font-weight: 600;
}
.claude-group-badge {
  font-size: 11px;
  padding: 2px 7px;
  border-radius: 999px;
  background: var(--claude-surface);
  border: 1px solid var(--claude-border);
  color: var(--claude-text-sub);
  font-family: var(--claude-mono);
  flex-shrink: 0;
}
.claude-group-item.active .claude-group-badge {
  background: var(--claude-terracotta-soft);
  border-color: transparent;
  color: var(--claude-terracotta);
  font-weight: 700;
}

.claude-main {
  flex: 1;
  min-width: 0;
}
.claude-artifact-card {
  background: var(--claude-surface);
  border: 1px solid var(--claude-border);
  border-radius: 14px;
  box-shadow: var(--claude-shadow);
  overflow: hidden;
  margin-bottom: 24px;
}
.claude-artifact-header {
  padding: 16px 24px;
  border-bottom: 1px solid var(--claude-border);
  background: var(--claude-surface-warm);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}
.claude-artifact-title-box {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.claude-sparkle {
  color: var(--claude-terracotta);
  font-size: 18px;
  line-height: 1;
}
.claude-artifact-badge {
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--claude-terracotta);
  background: var(--claude-terracotta-soft);
  border: 1px solid var(--claude-terracotta);
  padding: 2px 8px;
  border-radius: 6px;
}
.claude-artifact-title {
  font-family: var(--claude-serif);
  font-size: 18px;
  font-weight: 700;
  color: var(--claude-text);
  letter-spacing: -0.01em;
}
.claude-artifact-tools {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.claude-tool-btn {
  padding: 6px 12px;
  font-size: 12px;
  border-radius: 7px;
  border: 1px solid var(--claude-border);
  background: var(--claude-surface);
  color: var(--claude-text);
  cursor: pointer;
  transition: all 0.15s;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.claude-tool-btn:hover {
  border-color: var(--claude-terracotta);
  color: var(--claude-terracotta);
}

.claude-article-body {
  padding: 28px 36px;
}
@media (max-width: 900px) {
  .claude-layout { flex-direction: column; }
  .claude-sidebar { width: 100%; position: static; max-height: none; }
  .claude-article-body { padding: 18px; }
}

.claude-lead-note {
  border-left: 3.5px solid var(--claude-terracotta);
  background: var(--claude-surface-warm);
  border-radius: 0 10px 10px 0;
  padding: 16px 20px;
  margin-bottom: 24px;
  font-family: var(--claude-serif);
  font-size: 15px;
  color: var(--claude-text);
  line-height: 1.7;
}

.claude-chapter-title {
  font-family: var(--claude-serif);
  font-size: 18px;
  font-weight: 700;
  color: var(--claude-text);
  margin: 28px 0 14px 0;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--claude-border-subtle);
  display: flex;
  align-items: center;
  gap: 10px;
}
.claude-chapter-diamond {
  color: var(--claude-terracotta);
  font-size: 14px;
}

.claude-thread-card {
  border: 1px solid var(--claude-border);
  border-radius: 12px;
  background: var(--claude-surface);
  margin-bottom: 18px;
  overflow: hidden;
}
.claude-thread-header {
  padding: 12px 18px;
  background: var(--claude-surface-warm);
  border-bottom: 1px solid var(--claude-border-subtle);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.claude-thread-title {
  font-family: var(--claude-serif);
  font-size: 15.5px;
  font-weight: 700;
  color: var(--claude-text);
}
.claude-thread-meta {
  font-size: 12px;
  color: var(--claude-text-sub);
  display: flex;
  align-items: center;
  gap: 8px;
}
.claude-thread-body {
  padding: 16px 18px;
}
.claude-thread-takeaways {
  list-style: none;
  margin-bottom: 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.claude-takeaway-item {
  font-size: 14px;
  color: var(--claude-text);
  line-height: 1.6;
  display: flex;
  align-items: flex-start;
  gap: 8px;
}
.claude-takeaway-dot {
  color: var(--claude-terracotta);
  font-weight: bold;
  line-height: 1.6;
}

.claude-turn {
  display: flex;
  gap: 12px;
  padding: 11px 14px;
  border-radius: 10px;
  background: var(--claude-surface-warm);
  border: 1px solid var(--claude-border-subtle);
  margin-bottom: 8px;
}
.claude-turn.is-notice {
  background: var(--claude-terracotta-soft);
  border-color: var(--claude-terracotta);
}
.claude-avatar {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-weight: 700;
  font-size: 13px;
  flex-shrink: 0;
}
.claude-turn-content {
  flex: 1;
  min-width: 0;
}
.claude-turn-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
  font-size: 12px;
  color: var(--claude-text-sub);
}
.claude-turn-sender {
  font-weight: 600;
  color: var(--claude-text);
}
.claude-turn-text {
  font-size: 13.5px;
  line-height: 1.65;
  color: var(--claude-text);
  word-break: break-word;
}

.claude-blockquote {
  margin: 14px 0;
  padding: 12px 16px;
  border-left: 3px solid var(--claude-terracotta);
  background: var(--claude-surface-warm);
  border-radius: 0 8px 8px 0;
  font-family: var(--claude-serif);
  font-size: 14px;
  line-height: 1.65;
  color: var(--claude-text);
}
.claude-blockquote-sender {
  margin-top: 6px;
  font-family: var(--claude-sans);
  font-size: 12px;
  color: var(--claude-text-sub);
}

.claude-persona-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 10px;
  margin-top: 14px;
}
.claude-persona-card {
  padding: 10px 14px;
  border: 1px solid var(--claude-border);
  border-radius: 8px;
  background: var(--claude-surface);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.claude-persona-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--claude-text);
}
.claude-persona-tag {
  font-size: 11px;
  padding: 2px 7px;
  border-radius: 999px;
  font-weight: 600;
}

@media print {
  .topbar, .filter-bar, .no-print, .claude-sidebar { display: none !important; }
  body { background: #fff; color: #000; padding: 0; }
  .container { max-width: 100%; padding: 0; }
  .section-card, .kpi-card, .thread-card, .claude-artifact-card { box-shadow: none; border-color: #ccc; break-inside: avoid; }
  .bubble-drawer { display: flex !important; max-height: none !important; }
}
</style>
</head>
<body>

<header class="topbar">
  <div class="topbar-inner">
    <div class="brand">
      <div class="brand-logo" style="background:linear-gradient(135deg, #d97757, #6a9bcc)">✦</div>
      <div>
        <div class="brand-title" id="hdr-title">QQ 群聊可视化简报</div>
        <div class="brand-sub" id="hdr-sub"></div>
      </div>
    </div>
    <div class="toolbar">
      <div class="mode-switch" id="view-switcher">
        <button class="mode-btn active" data-view="claude" onclick="switchView('claude')">✨ Claude 视窗</button>
        <button class="mode-btn" data-view="bento" onclick="switchView('bento')">🍱 Bento 交互看板</button>
        <button class="mode-btn" data-view="paper" onclick="switchView('paper')">📰 纸上日报视图</button>
        <button class="mode-btn" data-view="markdown" onclick="switchView('markdown')">📄 Markdown 原文</button>
      </div>
      <button class="icon-btn" onclick="toggleTheme()" id="theme-btn">☀️ 浅色</button>
      <button class="icon-btn" onclick="window.print()">🖨️ 打印 / PDF</button>
    </div>
  </div>
</header>

<main class="container" id="app-root">
  <noscript>请启用浏览器 JavaScript 以查看交互式可视化简报。</noscript>
</main>

<script id="digest-data" type="application/json">__DIGEST_JSON__</script>
<script>
(function() {
  const rawJson = document.getElementById('digest-data').textContent;
  const DATA = JSON.parse(rawJson);

  const urlParamView = (new URLSearchParams(window.location.search).get('view') || window.location.hash.replace('#', ''));
  let defaultView = 'claude';
  if (['claude', 'bento', 'paper', 'markdown'].includes(urlParamView)) {
    defaultView = urlParamView;
  }

  let state = {
    view: defaultView,          // 'claude' | 'bento' | 'paper' | 'markdown'
    activeGroup: '__ALL__',     // '__ALL__' or group.code
    claudeActiveGroup: '__ALL__', // '__ALL__' or group.code
    groupSubTab: 'threads',     // 'threads' | 'stream'
    radarFilter: 'ALL',         // 'ALL' | 'CLASS' | 'AT' | 'FWD'
    searchQuery: '',
    claudeSearchQuery: '',
    speakerFilter: '',
    expandedThreads: {},
    claudeExpandedThreads: {},
  };

  const AVATAR_COLORS = [
    '#0ea5e9', '#6366f1', '#10b981', '#f59e0b',
    '#ec4899', '#8b5cf6', '#14b8a6', '#f43f5e'
  ];

  function hashColor(name) {
    let h = 0;
    const s = String(name || '匿名');
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return AVATAR_COLORS[h % AVATAR_COLORS.length];
  }

  function avatarChar(name) {
    const s = String(name || '?').replace(/^[@\s]+/, '');
    return s ? s.slice(0, 1).toUpperCase() : '?';
  }

  function esc(str) {
    if (str === null || str === undefined) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function highlightText(str, q) {
    const safe = esc(str);
    if (!q) return safe;
    const cleanQ = q.trim();
    if (!cleanQ) return safe;
    const escapedRe = cleanQ.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    try {
      const re = new RegExp('(' + escapedRe + ')', 'gi');
      return safe.replace(re, '<mark class="hl">$1</mark>');
    } catch (e) {
      return safe;
    }
  }

  function pillClass(clr) {
    const map = {
      amber: 'pill-amber',
      purple: 'pill-purple',
      cyan: 'pill-cyan',
      emerald: 'pill-emerald',
      blue: 'pill-accent',
      rose: 'pill-rose',
      slate: 'pill-slate'
    };
    return map[clr] || 'pill-slate';
  }

  window.switchView = function(v) {
    state.view = v;
    document.querySelectorAll('#view-switcher .mode-btn').forEach(btn => {
      btn.classList.toggle('active', btn.getAttribute('data-view') === v);
    });
    render();
  };

  window.toggleTheme = function() {
    const root = document.documentElement;
    const cur = root.getAttribute('data-theme') || 'dark';
    const next = cur === 'dark' ? 'light' : 'dark';
    root.setAttribute('data-theme', next);
    const btn = document.getElementById('theme-btn');
    if (btn) btn.textContent = next === 'dark' ? '☀️ 浅色' : '🌙 深色';
  };

  window.selectGroup = function(code) {
    state.activeGroup = code;
    state.speakerFilter = '';
    render();
    const fb = document.querySelector('.filter-bar');
    if (fb && code !== '__ALL__') {
      fb.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  };

  window.setGroupSubTab = function(tab) {
    state.groupSubTab = tab;
    render();
  };

  window.setRadarFilter = function(f) {
    state.radarFilter = f;
    render();
  };

  window.onSearchInput = function(val) {
    state.searchQuery = val || '';
    renderContentArea();
  };

  window.filterByKeyword = function(word) {
    state.searchQuery = word;
    if (state.activeGroup !== '__ALL__') {
      state.groupSubTab = 'stream';
    }
    render();
    const inp = document.getElementById('global-search-input');
    if (inp) inp.value = word;
  };

  window.filterBySpeaker = function(name) {
    if (state.speakerFilter === name) {
      state.speakerFilter = '';
    } else {
      state.speakerFilter = name;
      if (state.activeGroup !== '__ALL__') {
        state.groupSubTab = 'stream';
      }
    }
    render();
  };

  window.toggleThread = function(tid) {
    state.expandedThreads[tid] = !state.expandedThreads[tid];
    const drawer = document.getElementById('drawer-' + tid);
    const btn = document.getElementById('btn-' + tid);
    if (drawer) {
      const open = !!state.expandedThreads[tid];
      drawer.classList.toggle('open', open);
      if (btn) {
        const cnt = btn.getAttribute('data-cnt') || '';
        btn.innerHTML = open ? '🔼 收起对话气泡' : ('💬 展开对话气泡 (' + cnt + ' 条)');
      }
    }
  };

  window.toggleAllThreads = function(expand) {
    const grp = (DATA.groups || []).find(g => g.code === state.activeGroup);
    if (!grp) return;
    (grp.threads || []).forEach(t => {
      const tid = grp.code + '-' + t.idx;
      state.expandedThreads[tid] = expand;
    });
    renderContentArea();
  };

  window.copyMarkdown = function() {
    const md = DATA.markdown || '';
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(md).then(() => {
        const b = document.getElementById('copy-md-btn');
        if (b) { b.textContent = '✅ 已复制到剪贴板'; setTimeout(() => { b.textContent = '📋 复制全部 Markdown'; }, 2000); }
      });
    }
  };

  function renderHeader() {
    const m = DATA.meta || {};
    document.getElementById('hdr-title').textContent = m.title || 'QQ 群聊可视化简报';
    const subParts = [];
    if (m.cutoff_str) {
      subParts.push('<span class="status-dot"></span><span>数据截止 <strong>' + esc(m.cutoff_str) + '</strong></span>');
    }
    if (m.window_str) {
      subParts.push('<span>｜窗口 ' + esc(m.window_str) + '</span>');
    }
    if (m.src_mode) {
      subParts.push('<span class="pill pill-slate">' + esc(m.src_mode) + '</span>');
    }
    if (m.anonymized) {
      subParts.push('<span class="pill pill-emerald">🛡️ 已脱敏</span>');
    }
    document.getElementById('hdr-sub').innerHTML = subParts.join(' ');
  }

  function render24hChart(hours24, peakHour) {
    const arr = hours24 || new Array(24).fill(0);
    const mx = Math.max(1, ...arr);
    let html = '<div class="chart-24h">';
    for (let h = 0; h < 24; h++) {
      const v = arr[h] || 0;
      const pct = v === 0 ? 4 : Math.max(8, Math.round((v / mx) * 100));
      const isPeak = (v > 0 && h === peakHour);
      html += '<div class="bar-col" title="' + (h < 10 ? '0' + h : h) + ':00 - ' + v + ' 条消息">' +
        '<div class="bar-fill' + (isPeak ? ' peak' : '') + '" style="height:' + pct + '%"></div>' +
        '<span class="bar-lbl">' + (h % 2 === 0 ? (h < 10 ? '0' + h : h) : '') + '</span>' +
      '</div>';
    }
    html += '</div>';
    return html;
  }

  function renderDaysTrend(daysTrend) {
    if (!daysTrend || !daysTrend.length) return '';
    const mx = Math.max(1, ...daysTrend.map(d => d.count));
    let html = '<div style="display:flex;flex-direction:column;gap:6px;margin-top:12px">';
    daysTrend.forEach(d => {
      const w = Math.max(2, Math.round((d.count / mx) * 100));
      html += '<div style="display:flex;align-items:center;gap:8px;font-size:12px">' +
        '<span style="width:46px;font-family:monospace;color:var(--text-sub)">' + esc(d.day) + '</span>' +
        '<div style="flex:1;height:7px;background:var(--card-alt);border-radius:999px;overflow:hidden">' +
          '<div style="width:' + w + '%;height:100%;background:var(--accent);border-radius:999px"></div>' +
        '</div>' +
        '<span style="width:48px;text-align:right;font-family:monospace;color:var(--text-sub)">' + d.count + '</span>' +
      '</div>';
    });
    html += '</div>';
    return html;
  }

  function renderBubble(b, q) {
    const color = hashColor(b.sender);
    const ch = avatarChar(b.sender);
    const kindBadge = (b.kind && b.kind !== 'text')
      ? '<span class="pill pill-slate">[' + esc(b.kind) + ']</span>'
      : '';
    return '<div class="chat-item">' +
      '<div class="avatar" style="background:' + color + '">' + esc(ch) + '</div>' +
      '<div class="bubble-wrap">' +
        '<div class="bubble-meta">' +
          '<span class="bubble-sender">' + highlightText(b.sender, q) + '</span>' +
          '<span>' + esc(b.time) + '</span>' +
          kindBadge +
          (b.is_notice ? '<span class="pill pill-amber">📢 重点</span>' : '') +
        '</div>' +
        '<div class="bubble-body' + (b.is_notice ? ' is-notice' : '') + '">' +
          highlightText(b.text, q) +
        '</div>' +
      '</div>' +
    '</div>';
  }

  function renderAllGroupsOverview(q) {
    const kpis = DATA.kpis || {};
    const groups = DATA.groups || [];
    const qLow = (q || '').trim().toLowerCase();

    const filteredGroups = groups.filter(g => {
      if (!qLow) return true;
      if ((g.name || '').toLowerCase().includes(qLow) || (g.code || '').toLowerCase().includes(qLow)) return true;
      if ((g.keywords || []).some(k => k.word.toLowerCase().includes(qLow))) return true;
      if ((g.threads || []).some(t => t.title.toLowerCase().includes(qLow) || (t.bubbles || []).some(b => b.text.toLowerCase().includes(qLow) || b.sender.toLowerCase().includes(qLow)))) return true;
      return false;
    });

    let leftHtml = '<div class="section-card">' +
      '<div class="section-head">' +
        '<div class="section-title">🗂️ 活跃群组全景 (' + filteredGroups.length + ' / ' + groups.length + ' 个群)</div>' +
        '<span style="font-size:12px;color:var(--text-sub)">点击任意群卡片可进入该群专属深度看板與对话气泡</span>' +
      '</div>';

    if (!filteredGroups.length) {
      leftHtml += '<div style="padding:32px;text-align:center;color:var(--text-sub)">未找到匹配「' + esc(q) + '」的群组或话题。</div>';
    } else {
      leftHtml += '<div class="group-cards-grid">';
      filteredGroups.forEach(g => {
        const deltaStr = g.delta > 0 ? ('↑ +' + g.delta) : (g.delta < 0 ? ('↓ ' + g.delta) : '→ 0');
        const deltaCls = g.delta > 0 ? 'pill-emerald' : (g.delta < 0 ? 'pill-rose' : 'pill-slate');
        const topKws = (g.keywords || []).slice(0, 5).map(k => '<span class="pill pill-slate">' + highlightText(k.word, q) + ' (' + k.count + ')</span>').join(' ');
        const topThread = (g.threads && g.threads[0]) ? g.threads[0] : null;
        leftHtml += '<div class="grp-overview-card" onclick="selectGroup(\'' + esc(g.code) + '\')">' +
          '<div>' +
            '<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:6px">' +
              '<div style="font-weight:700;font-size:15px;display:flex;align-items:center;gap:6px;min-width:0">' +
                (g.is_class ? '<span class="pill pill-amber">🎓 班群</span>' : '') +
                '<span style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + highlightText(g.name, q) + '</span>' +
              '</div>' +
              '<span class="pill ' + deltaCls + '">' + esc(deltaStr) + '</span>' +
            '</div>' +
            '<div style="font-size:12px;color:var(--text-sub);margin-bottom:10px">' +
              '有效对话 <strong>' + g.nconv + '</strong> 条（总 ' + g.ntot + '）｜参与 <strong>' + g.people_count + '</strong> 人｜末条 ' + esc(g.last_time) +
            '</div>' +
            (topThread ? (
              '<div style="background:var(--card-alt);border:1px solid var(--border);border-radius:9px;padding:8px 10px;margin-bottom:8px;font-size:12.5px">' +
                '<div style="font-weight:600;color:var(--accent);margin-bottom:3px">🔥 ' + highlightText(topThread.title, q) + '</div>' +
                (topThread.highlights && topThread.highlights[0]
                  ? '<div style="color:var(--text-sub);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' +
                      '<strong>' + esc(topThread.highlights[0].sender) + ':</strong> ' + highlightText(topThread.highlights[0].text, q) +
                    '</div>'
                  : '') +
              '</div>'
            ) : '') +
          '</div>' +
          '<div style="display:flex;flex-wrap:wrap;gap:4px">' + topKws + '</div>' +
        '</div>';
      });
      leftHtml += '</div>';
    }
    leftHtml += '</div>';

    // Right sidebar: Global 24h & Daily Chart + Global Persona Leaderboard + Global Hot Keywords
    let rightHtml = '<div class="section-card">' +
      '<div class="section-title" style="margin-bottom:6px">⏰ 全群 24 小时活跃分布</div>' +
      '<div style="font-size:12px;color:var(--text-sub)">峰值时段：<strong style="color:var(--amber)">' + esc(kpis.peak_hour_str || '-') + '</strong>（' + (kpis.peak_hour_count || 0) + ' 条）</div>' +
      render24hChart(kpis.hours_24, kpis.peak_hour) +
      renderDaysTrend(kpis.days_trend) +
    '</div>';

    if (kpis.global_speakers && kpis.global_speakers.length) {
      const mxSpk = Math.max(1, kpis.global_speakers[0].count);
      rightHtml += '<div class="section-card">' +
        '<div class="section-title" style="margin-bottom:10px">🏆 全群活跃群友画像榜 Top ' + kpis.global_speakers.length + '</div>' +
        '<div style="display:flex;flex-direction:column;gap:4px">';
      kpis.global_speakers.forEach((s, idx) => {
        const w = Math.max(4, Math.round((s.count / mxSpk) * 100));
        rightHtml += '<div class="speaker-row" onclick="filterByKeyword(\'' + esc(s.name) + '\')">' +
          '<div class="avatar" style="background:' + hashColor(s.name) + ';width:28px;height:28px;font-size:12px">' + esc(avatarChar(s.name)) + '</div>' +
          '<div class="speaker-info">' +
            '<div class="speaker-top">' +
              '<span class="speaker-name">' + (idx + 1) + '. ' + esc(s.name) + '</span>' +
              '<span class="pill ' + pillClass(s.color) + '">' + esc(s.persona) + '</span>' +
            '</div>' +
            '<div class="progress-track"><div class="progress-bar" style="width:' + w + '%"></div></div>' +
          '</div>' +
          '<div style="font-size:12px;font-family:monospace;color:var(--text-sub);min-width:46px;text-align:right">' + s.count + '条</div>' +
        '</div>';
      });
      rightHtml += '</div></div>';
    }

    if (kpis.global_keywords && kpis.global_keywords.length) {
      rightHtml += '<div class="section-card">' +
        '<div class="section-title" style="margin-bottom:10px">🏷️ 全群高频热词胶囊</div>' +
        '<div style="display:flex;flex-wrap:wrap;gap:6px">';
      kpis.global_keywords.forEach((k, idx) => {
        const cls = idx < 4 ? 'pill-amber' : (idx < 10 ? 'pill-accent' : 'pill-slate');
        rightHtml += '<button class="pill ' + cls + '" style="cursor:pointer;padding:4px 10px;font-size:12px" onclick="filterByKeyword(\'' + esc(k.word) + '\')">' +
          esc(k.word) + ' <span style="opacity:0.75">×' + k.count + '</span>' +
        '</button>';
      });
      rightHtml += '</div></div>';
    }

    return '<div class="bento-layout"><div>' + leftHtml + '</div><div>' + rightHtml + '</div></div>';
  }

  function renderSingleGroupBento(grp, q) {
    const qLow = (q || '').trim().toLowerCase();
    const spkFilter = state.speakerFilter;
    const deltaStr = grp.delta > 0 ? ('↑ +' + grp.delta + ' 条') : (grp.delta < 0 ? ('↓ ' + grp.delta + ' 条') : '→ 持平');
    const deltaCls = grp.delta > 0 ? 'pill-emerald' : (grp.delta < 0 ? 'pill-rose' : 'pill-slate');

    const kindsPills = (grp.kinds || []).map(k => '<span class="pill pill-slate">' + esc(k.kind) + '=' + k.count + '</span>').join(' ');

    let leftHtml = '<div class="section-card">' +
      '<div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:12px">' +
        '<div>' +
          '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">' +
            '<button class="icon-btn" style="padding:4px 10px;font-size:12px" onclick="selectGroup(\'__ALL__\')">← 返回全群</button>' +
            (grp.is_class ? '<span class="pill pill-amber">🎓 班群</span>' : '') +
            '<h2 style="font-size:20px;font-weight:800">' + esc(grp.name) + '</h2>' +
            '<span style="font-size:13px;color:var(--text-muted)">(' + esc(grp.code) + ')</span>' +
          '</div>' +
          '<div style="font-size:13px;color:var(--text-sub);margin-top:6px">' +
            '共 <strong>' + grp.ntot + '</strong> 条消息｜有效对话 <strong>' + grp.nconv + '</strong> 条｜参与 <strong>' + grp.people_count + '</strong> 人｜时段 ' + esc(grp.first_time) + ' ~ ' + esc(grp.last_time) + '（活跃约 ' + grp.active_mins + ' 分钟）' +
          '</div>' +
        '</div>' +
        '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">' +
          '<span class="pill ' + deltaCls + '">环比上窗口 ' + esc(deltaStr) + '</span>' +
          kindsPills +
        '</div>' +
      '</div>' +
      '<div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;padding-top:12px;border-top:1px solid var(--border)">' +
        '<div class="mode-switch">' +
          '<button class="mode-btn' + (state.groupSubTab === 'threads' ? ' active' : '') + '" onclick="setGroupSubTab(\'threads\')">🔥 核心讨论脉络 (' + (grp.threads || []).length + ' 个时段话题)</button>' +
          '<button class="mode-btn' + (state.groupSubTab === 'stream' ? ' active' : '') + '" onclick="setGroupSubTab(\'stream\')">💬 完整对话流 (' + (grp.stream || []).length + ' 条)</button>' +
        '</div>' +
        (state.groupSubTab === 'threads' ? (
          '<div style="display:flex;gap:6px">' +
            '<button class="icon-btn" style="font-size:12px;padding:4px 10px" onclick="toggleAllThreads(true)">全部展开气泡</button>' +
            '<button class="icon-btn" style="font-size:12px;padding:4px 10px" onclick="toggleAllThreads(false)">全部收起</button>' +
          '</div>'
        ) : (
          spkFilter ? '<button class="pill pill-accent" style="cursor:pointer;padding:5px 10px" onclick="filterBySpeaker(\'' + esc(spkFilter) + '\')">正在筛选发言人: ' + esc(spkFilter) + ' ✕</button>' : ''
        )) +
      '</div>' +
    '</div>';

    if (state.groupSubTab === 'threads') {
      const threads = (grp.threads || []).filter(t => {
        if (!qLow) return true;
        if (t.title.toLowerCase().includes(qLow)) return true;
        if ((t.keywords || []).some(k => k.toLowerCase().includes(qLow))) return true;
        return (t.bubbles || []).some(b => b.text.toLowerCase().includes(qLow) || b.sender.toLowerCase().includes(qLow));
      });
      if (!threads.length) {
        leftHtml += '<div class="section-card" style="text-align:center;color:var(--text-sub);padding:32px">没有匹配的讨论时段话题。</div>';
      } else {
        threads.forEach(t => {
          const tid = grp.code + '-' + t.idx;
          const isOpen = !!state.expandedThreads[tid] || !!qLow;
          const kwBadges = (t.keywords || []).map(k => '<span class="pill pill-accent" style="cursor:pointer" onclick="filterByKeyword(\'' + esc(k) + '\')">#' + highlightText(k, q) + '</span>').join(' ');
          const pplBadges = (t.top_people || []).map(p =>
            '<span class="pill pill-slate" style="cursor:pointer" onclick="filterBySpeaker(\'' + esc(p.name) + '\')">' +
              esc(p.name) + '(' + p.count + ')' +
            '</span>'
          ).join(' ');

          leftHtml += '<div class="thread-card' + (t.has_notice ? ' notice-thread' : '') + '">' +
            '<div class="thread-head">' +
              '<div class="thread-title">' +
                '<span class="pill pill-amber">' + esc(t.period) + ' · ' + esc(t.start_time) + ' ~ ' + esc(t.end_time) + '</span>' +
                '<span>' + highlightText(t.title, q) + '</span>' +
              '</div>' +
              '<span style="font-size:12px;color:var(--text-sub)">' + t.msg_count + ' 条对话 · ' + t.people_count + ' 人参与</span>' +
            '</div>' +
            '<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px">' + kwBadges + ' ' + pplBadges + '</div>' +
            '<div class="thread-highlights">' +
              (t.highlights || []).map(h =>
                '<div class="hl-row"><span style="font-family:monospace;font-size:11px;color:var(--text-muted)">' + esc(h.time) + '</span> <strong>' + highlightText(h.sender, q) + ':</strong> ' + highlightText(h.text, q) + '</div>'
              ).join('') +
            '</div>' +
            '<div style="display:flex;align-items:center;justify-content:space-between;margin-top:8px">' +
              '<button class="icon-btn" id="btn-' + esc(tid) + '" data-cnt="' + (t.bubbles || []).length + '" style="font-size:12px;padding:5px 12px" onclick="toggleThread(\'' + esc(tid) + '\')">' +
                (isOpen ? '🔼 收起对话气泡' : ('💬 展开对话气泡 (' + (t.bubbles || []).length + ' 条)')) +
              '</button>' +
              (t.more_count > 0 ? '<span style="font-size:12px;color:var(--text-muted)">另有 ' + t.more_count + ' 条见完整对话流</span>' : '') +
            '</div>' +
            '<div class="bubble-drawer' + (isOpen ? ' open' : '') + '" id="drawer-' + esc(tid) + '">' +
              (t.bubbles || []).map(b => renderBubble(b, q)).join('') +
            '</div>' +
          '</div>';
        });
      }
    } else {
      const rows = (grp.stream || []).filter(b => {
        if (spkFilter && b.sender !== spkFilter) return false;
        if (!qLow) return true;
        return b.text.toLowerCase().includes(qLow) || b.sender.toLowerCase().includes(qLow);
      });
      leftHtml += '<div class="section-card">' +
        '<div class="section-head">' +
          '<div class="section-title">💬 对话原文气泡流（显示 ' + rows.length + ' 条' + (grp.omitted_media > 0 ? '，已省略纯图片/表情 ' + grp.omitted_media + ' 条' : '') + '）</div>' +
        '</div>' +
        '<div style="display:flex;flex-direction:column;gap:10px;max-height:780px;overflow-y:auto;padding-right:6px">' +
          (rows.length ? rows.map(b => renderBubble(b, q)).join('') : '<div style="padding:24px;text-align:center;color:var(--text-sub)">没有匹配的对话消息。</div>') +
        '</div>' +
      '</div>';
    }

    // Right column for single group
    let rightHtml = '<div class="section-card">' +
      '<div class="section-title" style="margin-bottom:4px">⏰ 本群 24 小时活跃分布</div>' +
      '<div style="font-size:12px;color:var(--text-sub)">最活跃时段：<strong style="color:var(--amber)">' + (grp.peak_hour < 10 ? '0' + grp.peak_hour : grp.peak_hour) + ':00</strong></div>' +
      render24hChart(grp.hours_24, grp.peak_hour) +
      renderDaysTrend(grp.days_trend) +
    '</div>';

    if (grp.quotes && grp.quotes.length) {
      rightHtml += '<div class="section-card">' +
        '<div class="section-title" style="margin-bottom:10px">💬 本群高光金句 / 重点发言</div>';
      grp.quotes.forEach(qt => {
        rightHtml += '<div class="quote-card' + (qt.is_notice ? ' notice' : '') + '">' +
          '<div style="font-size:11px;color:var(--text-sub);margin-bottom:4px;display:flex;justify-content:space-between">' +
            '<strong>' + esc(qt.sender) + '</strong><span>' + esc(qt.time) + '</span>' +
          '</div>' +
          '<div>“' + highlightText(qt.text, q) + '”</div>' +
        '</div>';
      });
      rightHtml += '</div>';
    }

    if (grp.speakers && grp.speakers.length) {
      const mx = Math.max(1, grp.speakers[0].count);
      rightHtml += '<div class="section-card">' +
        '<div class="section-head" style="margin-bottom:8px">' +
          '<div class="section-title">🏆 本群发言画像榜</div>' +
          '<span style="font-size:11px;color:var(--text-muted)">点击按群友筛选</span>' +
        '</div>' +
        '<div style="display:flex;flex-direction:column;gap:4px">';
      grp.speakers.forEach((s, idx) => {
        const w = Math.max(4, Math.round((s.count / mx) * 100));
        const isSel = state.speakerFilter === s.name;
        rightHtml += '<div class="speaker-row" style="' + (isSel ? 'background:var(--accent-soft);border:1px solid var(--accent)' : '') + '" onclick="filterBySpeaker(\'' + esc(s.name) + '\')">' +
          '<div class="avatar" style="background:' + hashColor(s.name) + ';width:28px;height:28px;font-size:12px">' + esc(avatarChar(s.name)) + '</div>' +
          '<div class="speaker-info">' +
            '<div class="speaker-top">' +
              '<span class="speaker-name">' + (idx + 1) + '. ' + esc(s.name) + '</span>' +
              '<span class="pill ' + pillClass(s.color) + '">' + esc(s.persona) + '</span>' +
            '</div>' +
            '<div class="progress-track"><div class="progress-bar" style="width:' + w + '%"></div></div>' +
          '</div>' +
          '<div style="font-size:11px;font-family:monospace;color:var(--text-sub);min-width:64px;text-align:right">' + s.count + '条 (' + s.pct + '%)</div>' +
        '</div>';
      });
      rightHtml += '</div></div>';
    }

    if ((grp.keywords && grp.keywords.length) || (grp.ats && grp.ats.length)) {
      rightHtml += '<div class="section-card">';
      if (grp.keywords && grp.keywords.length) {
        rightHtml += '<div class="section-title" style="margin-bottom:10px">🏷️ 本群高频词（点击筛选）</div>' +
          '<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:14px">';
        grp.keywords.forEach((k, idx) => {
          const cls = idx < 3 ? 'pill-amber' : (idx < 8 ? 'pill-accent' : 'pill-slate');
          rightHtml += '<button class="pill ' + cls + '" style="cursor:pointer;padding:4px 10px;font-size:12px" onclick="filterByKeyword(\'' + esc(k.word) + '\')">' +
            esc(k.word) + ' ×' + k.count +
          '</button>';
        });
        rightHtml += '</div>';
      }
      if (grp.ats && grp.ats.length) {
        rightHtml += '<div class="section-title" style="margin-bottom:8px;font-size:14px">🎯 被 @ 最多</div>' +
          '<div style="display:flex;flex-wrap:wrap;gap:6px">';
        grp.ats.forEach(a => {
          rightHtml += '<span class="pill pill-purple">@' + esc(a.name) + ' (' + a.count + ')</span>';
        });
        rightHtml += '</div>';
      }
      rightHtml += '</div>';
    }

    return '<div class="bento-layout"><div>' + leftHtml + '</div><div>' + rightHtml + '</div></div>';
  }

  function renderContentArea() {
    const slot = document.getElementById('bento-content-slot');
    if (!slot) return;
    const q = state.searchQuery;
    if (state.activeGroup === '__ALL__') {
      slot.innerHTML = renderAllGroupsOverview(q);
    } else {
      const grp = (DATA.groups || []).find(g => g.code === state.activeGroup);
      if (grp) {
        slot.innerHTML = renderSingleGroupBento(grp, q);
      } else {
        state.activeGroup = '__ALL__';
        slot.innerHTML = renderAllGroupsOverview(q);
      }
    }
  }

  function renderDigestBento() {
    const m = DATA.meta || {};
    const k = DATA.kpis || {};
    const focus = DATA.focus_items || [];
    const groups = DATA.groups || [];

    let html = '';

    // 1. State lines / bad_days alert bar
    const alerts = [];
    if (m.state_lines && m.state_lines.length) {
      m.state_lines.forEach(ln => alerts.push('<div>' + esc(ln) + '</div>'));
    }
    if (m.bad_days && m.bad_days.length) {
      alerts.push('<div style="color:var(--amber)">⚠️ 数据缺口提示：有 ' + m.bad_days.length + ' 个「群-日」因快照繁忙跳过：' + esc(m.bad_days.join(', ')) + '</div>');
    }
    if (alerts.length) {
      html += '<div class="alert-bar' + (m.bad_days && m.bad_days.length ? ' warn' : '') + '">' + alerts.join('') + '</div>';
    }

    // 2. 5-Grid KPI Cards
    const dMsgs = k.delta_msgs || 0;
    const dBadge = dMsgs > 0 ? '<span class="pill pill-emerald">↑ +' + dMsgs + '</span>' : (dMsgs < 0 ? '<span class="pill pill-rose">↓ ' + dMsgs + '</span>' : '<span class="pill pill-slate">→ 0</span>');

    html += '<div class="kpi-grid">' +
      '<div class="kpi-card">' +
        '<div class="kpi-label"><span>📊 窗口消息总量</span>' + dBadge + '</div>' +
        '<div class="kpi-val">' + (k.total_msgs || 0).toLocaleString() + '</div>' +
        '<div class="kpi-foot">有效对话 <strong>' + (k.total_conv || 0).toLocaleString() + '</strong> 条｜上窗口 ' + (k.prev_msgs || 0).toLocaleString() + ' 条</div>' +
      '</div>' +
      '<div class="kpi-card">' +
        '<div class="kpi-label"><span>👥 活跃群组 & 群友</span><span class="pill pill-accent">全景覆盖</span></div>' +
        '<div class="kpi-val">' + (k.group_count || 0) + ' <span style="font-size:15px;font-weight:600;color:var(--text-sub)">个群</span></div>' +
        '<div class="kpi-foot">共 <strong>' + (k.people_count || 0) + '</strong> 位群友参与发言互动</div>' +
      '</div>' +
      '<div class="kpi-card">' +
        '<div class="kpi-label"><span>🔥 峰值活跃时段</span><span class="pill pill-amber">黄金档</span></div>' +
        '<div class="kpi-val" style="color:var(--amber)">' + esc(k.peak_hour_str || '-') + '</div>' +
        '<div class="kpi-foot">该小时段共产生 <strong>' + (k.peak_hour_count || 0) + '</strong> 条消息</div>' +
      '</div>' +
      '<div class="kpi-card">' +
        '<div class="kpi-label"><span>📌 重点雷达命中</span><span class="pill pill-rose">通知/公告</span></div>' +
        '<div class="kpi-val">' + (k.focus_count || 0) + ' <span style="font-size:15px;font-weight:600;color:var(--text-sub)">条重点</span></div>' +
        '<div class="kpi-foot">自动识别 @全体 / 通知 / 截止 / 班群 / 长文</div>' +
      '</div>' +
      '<div class="kpi-card">' +
        '<div class="kpi-label"><span>🏆 榜首群 & 龙王</span><span class="pill pill-purple">TOP 1</span></div>' +
        '<div class="kpi-val" style="font-size:19px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' +
          esc(k.top_group ? k.top_group.name : '-') +
        '</div>' +
        '<div class="kpi-foot">' +
          (k.top_speaker ? ('话痨榜首: <strong>' + esc(k.top_speaker.name) + '</strong> (' + k.top_speaker.count + '条)') : '暂无发言统计') +
        '</div>' +
      '</div>' +
    '</div>';

    // 3. Priority Radar (if focus_items exist)
    if (focus.length) {
      const fItems = focus.filter(it => {
        if (state.radarFilter === 'CLASS') return it.is_class || (it.tags || []).some(t => t.includes('通知'));
        if (state.radarFilter === 'AT') return (it.tags || []).some(t => t.includes('@全体'));
        if (state.radarFilter === 'FWD') return (it.tags || []).some(t => t.includes('转发') || t.includes('长文'));
        return true;
      });
      html += '<div class="section-card">' +
        '<div class="section-head">' +
          '<div class="section-title">📌 重点雷达 · 通知 / @全体 / 班群 / 转发 (' + fItems.length + ' 条)</div>' +
          '<div class="mode-switch">' +
            '<button class="mode-btn' + (state.radarFilter === 'ALL' ? ' active' : '') + '" onclick="setRadarFilter(\'ALL\')">全部 (' + focus.length + ')</button>' +
            '<button class="mode-btn' + (state.radarFilter === 'CLASS' ? ' active' : '') + '" onclick="setRadarFilter(\'CLASS\')">🎓 班群/通知</button>' +
            '<button class="mode-btn' + (state.radarFilter === 'AT' ? ' active' : '') + '" onclick="setRadarFilter(\'AT\')">🚨 @全体</button>' +
            '<button class="mode-btn' + (state.radarFilter === 'FWD' ? ' active' : '') + '" onclick="setRadarFilter(\'FWD\')">📦 转发/长文</button>' +
          '</div>' +
        '</div>' +
        '<div class="radar-grid">';
      fItems.slice(0, 24).forEach(it => {
        const tagPills = (it.tags || []).map(t => {
          const c = t.includes('@全体') ? 'pill-rose' : (t.includes('班群') ? 'pill-amber' : 'pill-accent');
          return '<span class="pill ' + c + '">' + esc(t) + '</span>';
        }).join(' ');
        html += '<div class="radar-item" onclick="selectGroup(\'' + esc(it.group_code) + '\')">' +
          '<div class="radar-meta">' +
            '<div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;min-width:0">' +
              tagPills +
              '<strong style="color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">[' + esc(it.group_name) + ']</strong>' +
              '<span>' + esc(it.sender) + '</span>' +
            '</div>' +
            '<span style="font-family:monospace;flex-shrink:0">' + esc(it.time) + '</span>' +
          '</div>' +
          '<div class="radar-text">' + esc(it.text) + '</div>' +
        '</div>';
      });
      html += '</div></div>';
    }

    // 4. Sticky Group Filter Tabs + Global Search Input
    html += '<div class="filter-bar">' +
      '<div class="filter-top">' +
        '<input type="search" id="global-search-input" class="search-input" placeholder="🔍 实时搜索群名、话题关键词、群友昵称或聊天原文..." value="' + esc(state.searchQuery) + '" oninput="onSearchInput(this.value)">' +
        (state.searchQuery ? '<button class="icon-btn" onclick="filterByKeyword(\'\')">清除搜索 ✕</button>' : '') +
      '</div>' +
      '<div class="group-tabs">' +
        '<button class="group-tab' + (state.activeGroup === '__ALL__' ? ' active' : '') + '" onclick="selectGroup(\'__ALL__\')">' +
          '🌐 全部群概览 <span class="tab-cnt">' + groups.length + '</span>' +
        '</button>';
    groups.forEach(g => {
      const act = state.activeGroup === g.code ? ' active' : '';
      html += '<button class="group-tab' + act + '" onclick="selectGroup(\'' + esc(g.code) + '\')">' +
        (g.is_class ? '🎓 ' : '') + esc(g.name) +
        ' <span class="tab-cnt">' + g.nconv + '</span>' +
      '</button>';
    });
    html += '</div></div>';

    // 5. Dynamic Bento Content Slot
    html += '<div id="bento-content-slot"></div>';
    return html;
  }

  function renderPaperDailyView() {
    const m = DATA.meta || {};
    const k = DATA.kpis || {};
    const focus = DATA.focus_items || [];
    const groups = DATA.groups || [];

    if (DATA.mode !== 'digest') {
      return '<div class="paper-view">' +
        '<div class="paper-masthead">' +
          '<div class="paper-title">' + esc(m.title || 'QQ 群聊日报') + '</div>' +
          '<div class="paper-meta-strip"><span>' + esc(m.window_str || '') + '</span><span>数据截止：' + esc(m.cutoff_str || '') + '</span></div>' +
        '</div>' +
        '<div class="md-box" style="background:transparent;border:none;max-height:none;font-family:inherit;font-size:15px">' + esc(DATA.markdown || '') + '</div>' +
      '</div>';
    }

    let html = '<div class="paper-view">' +
      '<div class="paper-masthead">' +
        '<div style="font-size:12px;letter-spacing:0.2em;text-transform:uppercase;color:var(--text-sub);margin-bottom:4px">LOCAL INTELLIGENCE EDITION · ZERO-UPLOAD DIGEST</div>' +
        '<div class="paper-title">QQ 群聊每日观察报</div>' +
        '<div class="paper-meta-strip">' +
          '<span>📅 统计窗口：' + esc(m.window_str || '-') + '</span>' +
          '<span>📊 总消息 ' + (k.total_msgs || 0) + ' 条（有效对话 ' + (k.total_conv || 0) + ' 条 / ' + (k.group_count || 0) + ' 个群）</span>' +
          '<span>⏱️ 数据截止：' + esc(m.cutoff_str || '-') + '</span>' +
        '</div>' +
      '</div>';

    if (focus.length) {
      html += '<div style="margin-bottom:28px;padding-bottom:20px;border-bottom:2px solid var(--paper-rule)">' +
        '<h2 style="font-size:22px;margin-bottom:12px">📌 头版要闻 · 重点通知与公告</h2>' +
        '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px">';
      focus.slice(0, 6).forEach(it => {
        html += '<div style="padding:12px 14px;border:1px solid var(--border-strong);border-radius:6px">' +
          '<div style="font-size:12px;color:var(--text-sub);font-family:sans-serif;margin-bottom:4px">' +
            '<strong>[' + esc(it.group_name) + ']</strong> ' + esc(it.sender) + ' · ' + esc(it.time) +
          '</div>' +
          '<div style="font-size:14px;line-height:1.65">' + esc(it.text) + '</div>' +
        '</div>';
      });
      html += '</div></div>';
    }

    html += '<div class="paper-columns">';
    groups.forEach((g, idx) => {
      const topSpk = (g.speakers || []).slice(0, 5).map(s => s.name + '(' + s.count + ')').join('、');
      const topKws = (g.keywords || []).slice(0, 8).map(kw => kw.word).join(' · ');
      html += '<article class="paper-article">' +
        '<h3>' + (idx + 1) + '. ' + esc(g.name) + '</h3>' +
        '<div class="paper-byline">有效对话 ' + g.nconv + ' 条 ｜ 参与 ' + g.people_count + ' 人 ｜ 高峰 ' + (g.peak_hour < 10 ? '0' + g.peak_hour : g.peak_hour) + ':00</div>' +
        (topKws ? '<div style="font-size:12.5px;margin-bottom:8px;color:var(--text-sub)"><strong>热议词：</strong>' + esc(topKws) + '</div>' : '') +
        (topSpk ? '<div style="font-size:12.5px;margin-bottom:10px;color:var(--text-sub)"><strong>主要发言：</strong>' + esc(topSpk) + '</div>' : '');

      (g.threads || []).slice(0, 4).forEach(t => {
        html += '<div style="margin-bottom:10px">' +
          '<div style="font-weight:700;font-size:14.5px">▪ [' + esc(t.start_time) + '] ' + esc(t.title) + '（' + t.msg_count + '条）</div>';
        (t.highlights || []).slice(0, 2).forEach(h => {
          html += '<div style="font-size:13.5px;padding-left:12px;color:var(--text-sub)">“<strong>' + esc(h.sender) + '</strong>：' + esc(h.text) + '”</div>';
        });
        html += '</div>';
      });
      html += '</article>';
    });
    html += '</div></div>';
    return html;
  }

  function renderGenericBento() {
    const m = DATA.meta || {};
    const ex = DATA.extra || {};
    let html = '<div class="section-card">' +
      '<div class="section-head">' +
        '<div class="section-title">📊 ' + esc(m.title || '专项报告视图') + '</div>' +
        '<button class="icon-btn" id="copy-md-btn" onclick="copyMarkdown()">📋 复制全部 Markdown</button>' +
      '</div>' +
      '<div style="font-size:13px;color:var(--text-sub);margin-bottom:14px">' +
        '查询窗口：<strong>' + esc(m.window_str || '-') + '</strong> ｜ 数据截止：<strong>' + esc(m.cutoff_str || '-') + '</strong>' +
      '</div>';

    if (ex.summary_cards && ex.summary_cards.length) {
      html += '<div class="kpi-grid" style="margin-bottom:16px">';
      ex.summary_cards.forEach(c => {
        html += '<div class="kpi-card">' +
          '<div class="kpi-label">' + esc(c.label) + '</div>' +
          '<div class="kpi-val" style="font-size:22px">' + esc(c.value) + '</div>' +
          (c.sub ? '<div class="kpi-foot">' + esc(c.sub) + '</div>' : '') +
        '</div>';
      });
      html += '</div>';
    }

    if (ex.items && ex.items.length) {
      html += '<div style="display:flex;flex-direction:column;gap:10px;margin-bottom:18px">';
      ex.items.forEach(it => {
        html += renderBubble(it, state.searchQuery);
      });
      html += '</div>';
    }

    html += '<div class="md-box">' + esc(DATA.markdown || '') + '</div></div>';
    return html;
  }

  window.onClaudeSearch = function(val) {
    state.claudeSearchQuery = val || '';
    renderClaudeSidebar();
    renderClaudeContent();
  };

  window.selectClaudeGroup = function(code) {
    state.claudeActiveGroup = code;
    renderClaudeSidebar();
    renderClaudeContent();
    const mainSlot = document.getElementById('claude-main-slot');
    if (mainSlot) mainSlot.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };

  window.toggleClaudeThread = function(tid) {
    state.claudeExpandedThreads[tid] = !state.claudeExpandedThreads[tid];
    const drawer = document.getElementById('claude-drawer-' + tid);
    const btn = document.getElementById('claude-btn-' + tid);
    if (drawer) {
      const open = !!state.claudeExpandedThreads[tid];
      drawer.style.display = open ? 'flex' : 'none';
      if (btn) {
        const cnt = btn.getAttribute('data-cnt') || '';
        btn.innerHTML = open ? '🔼 收起全部消息' : ('💬 展开全部消息 (' + cnt + ' 条)');
      }
    }
  };

  window.copyClaudeGroupMarkdown = function(code) {
    let textToCopy = '';
    if (code === '__ALL__') {
      textToCopy = DATA.markdown || '';
    } else {
      const g = (DATA.groups || []).find(x => x.code === code);
      if (g) {
        const lines = (DATA.markdown || '').split('\n');
        let inSection = false;
        let gLines = [];
        for (let line of lines) {
          if (line.startsWith('## ') || line.startsWith('### ')) {
            if (line.includes(g.name) || line.includes(String(g.code))) {
              inSection = true;
              gLines.push(line);
              continue;
            } else if (inSection && line.startsWith('## ')) {
              break;
            }
          }
          if (inSection) gLines.push(line);
        }
        textToCopy = gLines.length ? gLines.join('\n') : (DATA.markdown || '');
      } else {
        textToCopy = DATA.markdown || '';
      }
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(textToCopy).then(() => {
        const b = document.getElementById('claude-copy-btn');
        if (b) {
          const old = b.innerHTML;
          b.innerHTML = '✅ 已复制本群简报';
          setTimeout(() => { b.innerHTML = old; }, 2000);
        }
      });
    }
  };

  function renderClaudeTurn(b, q) {
    const color = hashColor(b.sender);
    const ch = avatarChar(b.sender);
    const kindBadge = (b.kind && b.kind !== 'text')
      ? '<span class="pill pill-slate">[' + esc(b.kind) + ']</span>'
      : '';
    return '<div class="claude-turn' + (b.is_notice ? ' is-notice' : '') + '">' +
      '<div class="claude-avatar" style="background:' + color + '">' + esc(ch) + '</div>' +
      '<div class="claude-turn-content">' +
        '<div class="claude-turn-header">' +
          '<span class="claude-turn-sender">' + highlightText(b.sender, q) + '</span>' +
          '<span>' + esc(b.time) + '</span>' +
          kindBadge +
          (b.is_notice ? '<span class="pill pill-amber" style="background:var(--claude-terracotta-soft);color:var(--claude-terracotta);border-color:var(--claude-terracotta)">📢 重点关注</span>' : '') +
        '</div>' +
        '<div class="claude-turn-text">' + highlightText(b.text, q) + '</div>' +
      '</div>' +
    '</div>';
  }

  function renderClaudeView() {
    return '<div class="claude-layout">' +
      '<aside class="claude-sidebar" id="claude-sidebar-slot"></aside>' +
      '<main class="claude-main" id="claude-main-slot"></main>' +
    '</div>';
  }

  function renderClaudeSidebar() {
    const slot = document.getElementById('claude-sidebar-slot');
    if (!slot) return;
    const groups = DATA.groups || [];
    const q = (state.claudeSearchQuery || '').trim().toLowerCase();

    let html = '<div class="claude-sidebar-card">' +
      '<div style="display:flex;align-items:center;gap:10px;margin-bottom:14px">' +
        '<div class="claude-sparkle" style="font-size:22px">✦</div>' +
        '<div>' +
          '<div style="font-family:var(--claude-serif);font-weight:700;font-size:15px;color:var(--claude-text)">Claude 阅览室</div>' +
          '<div style="font-size:11.5px;color:var(--claude-text-sub)">Anthropic 美学双轨阅览</div>' +
        '</div>' +
      '</div>' +
      '<div class="claude-search-box">' +
        '<span class="claude-search-icon">🔍</span>' +
        '<input type="search" class="claude-search-input" placeholder="过滤群聊、话题、金句..." value="' + esc(state.claudeSearchQuery) + '" oninput="onClaudeSearch(this.value)">' +
      '</div>';

    html += '<div style="font-size:11px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;color:var(--claude-text-sub);margin:14px 0 8px 4px">群组列表 (GROUPS)</div>' +
      '<div class="claude-group-list">' +
        '<button class="claude-group-item' + (state.claudeActiveGroup === '__ALL__' ? ' active' : '') + '" onclick="selectClaudeGroup(\'__ALL__\')">' +
          '<span style="display:flex;align-items:center;gap:8px">' +
            '<span>🌐</span><span>全部群速览</span>' +
          '</span>' +
          '<span class="claude-group-badge">' + groups.length + ' 群</span>' +
        '</button>';

    groups.forEach(g => {
      if (q) {
        const matchName = (g.name || '').toLowerCase().includes(q);
        const matchKws = (g.keywords || []).some(k => k.word.toLowerCase().includes(q));
        const matchThreads = (g.threads || []).some(t => (t.title || '').toLowerCase().includes(q));
        const matchQuotes = (g.quotes || []).some(quo => (quo.text || '').toLowerCase().includes(q));
        if (!matchName && !matchKws && !matchThreads && !matchQuotes) return;
      }
      const act = state.claudeActiveGroup === g.code ? ' active' : '';
      html += '<button class="claude-group-item' + act + '" onclick="selectClaudeGroup(\'' + esc(g.code) + '\')">' +
        '<span style="display:flex;align-items:center;gap:8px;min-width:0;overflow:hidden">' +
          '<span>' + (g.is_class ? '🎓' : '💬') + '</span>' +
          '<span style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + esc(g.name) + '</span>' +
        '</span>' +
        '<span class="claude-group-badge">' + g.nconv + '</span>' +
      '</button>';
    });
    html += '</div></div>';

    const curGroup = groups.find(g => g.code === state.claudeActiveGroup);
    if (curGroup && (curGroup.threads || []).length) {
      html += '<div class="claude-sidebar-card">' +
        '<div style="font-size:11px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;color:var(--claude-text-sub);margin-bottom:10px">本群核心议题 (TOPICS)</div>' +
        '<div style="display:flex;flex-direction:column;gap:6px">';
      curGroup.threads.forEach(t => {
        html += '<a href="#claude-topic-' + curGroup.code + '-' + t.idx + '" class="claude-group-item" style="padding:6px 10px;font-size:12.5px;line-height:1.4">' +
          '<span style="color:var(--claude-terracotta);margin-right:6px">◈</span>' +
          '<span style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + esc(t.title) + '</span>' +
        '</a>';
      });
      html += '</div></div>';
    }

    const speakers = curGroup ? (curGroup.speakers || []).slice(0, 6) : ((DATA.kpis || {}).global_speakers || []).slice(0, 6);
    if (speakers.length) {
      html += '<div class="claude-sidebar-card">' +
        '<div style="font-size:11px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;color:var(--claude-text-sub);margin-bottom:10px">' + (curGroup ? '本群核心群友' : '全群核心群友') + '</div>' +
        '<div style="display:flex;flex-wrap:wrap;gap:6px">';
      speakers.forEach(s => {
        html += '<span class="claude-persona-tag" style="background:var(--claude-surface-warm);border:1px solid var(--claude-border);color:var(--claude-text);padding:3px 8px;font-size:11.5px">' +
          esc(s.name) + ' <strong style="color:var(--claude-terracotta)">' + s.count + '</strong>' +
        '</span>';
      });
      html += '</div></div>';
    }

    slot.innerHTML = html;
  }

  function renderClaudeContent() {
    const slot = document.getElementById('claude-main-slot');
    if (!slot) return;
    const m = DATA.meta || {};
    const k = DATA.kpis || {};
    const groups = DATA.groups || [];
    const focus = DATA.focus_items || [];
    const q = (state.claudeSearchQuery || '').trim();

    if (DATA.mode !== 'digest') {
      const ex = DATA.extra || {};
      let html = '<div class="claude-artifact-card">' +
        '<div class="claude-artifact-header">' +
          '<div class="claude-artifact-title-box">' +
            '<span class="claude-sparkle">✦</span>' +
            '<span class="claude-artifact-badge">Artifact · Query Report</span>' +
            '<span class="claude-artifact-title">' + esc(m.title || '专项查询提炼简报') + '</span>' +
          '</div>' +
          '<div class="claude-artifact-tools">' +
            '<button class="claude-tool-btn" id="claude-copy-btn" onclick="copyMarkdown()">📋 复制 Markdown</button>' +
          '</div>' +
        '</div>' +
        '<div class="claude-article-body">' +
          '<div class="claude-lead-note">' +
            '💬 本专项分析由私有分析引擎实时提炼。窗口范围：<strong>' + esc(m.window_str || '-') + '</strong> ｜ 数据截止：<strong>' + esc(m.cutoff_str || '-') + '</strong>' +
          '</div>';

      if (ex.summary_cards && ex.summary_cards.length) {
        html += '<div class="claude-persona-grid" style="margin-bottom:20px">';
        ex.summary_cards.forEach(c => {
          html += '<div class="claude-persona-card" style="padding:12px 16px">' +
            '<div>' +
              '<div style="font-size:11.5px;color:var(--claude-text-sub)">' + esc(c.label) + '</div>' +
              '<div style="font-size:18px;font-weight:700;color:var(--claude-terracotta);margin-top:2px">' + esc(c.value) + '</div>' +
            '</div>' +
            (c.sub ? '<div style="font-size:11px;color:var(--claude-text-muted)">' + esc(c.sub) + '</div>' : '') +
          '</div>';
        });
        html += '</div>';
      }

      if (ex.items && ex.items.length) {
        html += '<div style="margin-bottom:20px">' +
          '<div class="claude-chapter-title"><span class="claude-chapter-diamond">◈</span> 命中对话记录 (' + ex.items.length + ' 条)</div>' +
          '<div style="display:flex;flex-direction:column;gap:8px">';
        ex.items.forEach(it => {
          html += renderClaudeTurn(it, q);
        });
        html += '</div></div>';
      }

      html += '<div class="claude-chapter-title"><span class="claude-chapter-diamond">◈</span> Markdown 原文</div>' +
        '<div class="md-box" style="background:var(--claude-surface-warm);border-color:var(--claude-border)">' + esc(DATA.markdown || '') + '</div>' +
      '</div></div>';
      slot.innerHTML = html;
      return;
    }

    if (state.claudeActiveGroup === '__ALL__') {
      let html = '<div class="claude-artifact-card">' +
        '<div class="claude-artifact-header">' +
          '<div class="claude-artifact-title-box">' +
            '<span class="claude-sparkle">✦</span>' +
            '<span class="claude-artifact-badge">Artifact · Executive Overview</span>' +
            '<span class="claude-artifact-title">QQ 群聊每日观察 · 总览</span>' +
            '<span class="pill pill-slate" style="font-family:var(--claude-mono)">约 ' + (k.total_conv || 0) + ' 条有效对话 · ' + groups.length + ' 个活跃群</span>' +
          '</div>' +
          '<div class="claude-artifact-tools">' +
            '<button class="claude-tool-btn" id="claude-copy-btn" onclick="copyClaudeGroupMarkdown(\'__ALL__\')">📋 复制全部 Markdown</button>' +
            '<button class="claude-tool-btn" onclick="window.print()">🖨️ 打印简报</button>' +
          '</div>' +
        '</div>' +
        '<div class="claude-article-body">' +
          '<div class="claude-lead-note">' +
            '💬 本期群聊日报由本地私有引擎基于本地 QQ 缓存实时提炼，覆盖 <strong>' + groups.length + '</strong> 个活跃群组与 <strong>' + (k.total_conv || 0) + '</strong> 条有效对话。' +
            '所有数据均在本地安全计算，不上传任何隐私。以下为全群核心议题与重要动态。点击左侧群聊或下方卡片可直接进入逐群深度阅读。' +
          '</div>';

      html += '<div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(210px, 1fr));gap:12px;margin-bottom:24px">' +
        '<div class="claude-persona-card" style="padding:14px 18px;flex-direction:column;align-items:flex-start">' +
          '<div style="font-size:11.5px;color:var(--claude-text-sub);text-transform:uppercase;letter-spacing:0.04em">📊 消息吞吐总量</div>' +
          '<div style="font-size:22px;font-weight:700;color:var(--claude-text);margin-top:4px">' + (k.total_msgs || 0) + ' 条</div>' +
          '<div style="font-size:11px;color:var(--claude-text-muted);margin-top:2px">' + (k.delta_msgs > 0 ? ('较前增量 +' + k.delta_msgs + ' 条') : '全量覆盖') + '</div>' +
        '</div>' +
        '<div class="claude-persona-card" style="padding:14px 18px;flex-direction:column;align-items:flex-start">' +
          '<div style="font-size:11.5px;color:var(--claude-text-sub);text-transform:uppercase;letter-spacing:0.04em">💬 有效对话数</div>' +
          '<div style="font-size:22px;font-weight:700;color:var(--claude-terracotta);margin-top:4px">' + (k.total_conv || 0) + ' 条</div>' +
          '<div style="font-size:11px;color:var(--claude-text-muted);margin-top:2px">已自动剔除纯表情/系统消息</div>' +
        '</div>' +
        '<div class="claude-persona-card" style="padding:14px 18px;flex-direction:column;align-items:flex-start">' +
          '<div style="font-size:11.5px;color:var(--claude-text-sub);text-transform:uppercase;letter-spacing:0.04em">👥 活跃交流群组</div>' +
          '<div style="font-size:22px;font-weight:700;color:var(--claude-blue);margin-top:4px">' + (k.group_count || 0) + ' 个群</div>' +
          '<div style="font-size:11px;color:var(--claude-text-muted);margin-top:2px">涵盖 ' + (k.people_count || 0) + ' 位群友参与</div>' +
        '</div>' +
        '<div class="claude-persona-card" style="padding:14px 18px;flex-direction:column;align-items:flex-start">' +
          '<div style="font-size:11.5px;color:var(--claude-text-sub);text-transform:uppercase;letter-spacing:0.04em">⏰ 黄金讨论档</div>' +
          '<div style="font-size:22px;font-weight:700;color:var(--claude-green);margin-top:4px">' + esc(k.peak_hour_str || '-') + '</div>' +
          '<div style="font-size:11px;color:var(--claude-text-muted);margin-top:2px">单小时峰值 ' + (k.peak_hour_count || 0) + ' 条</div>' +
        '</div>' +
      '</div>';

      if (focus.length) {
        html += '<div class="claude-chapter-title"><span class="claude-chapter-diamond">◈</span> 头版雷达 · 重点通知与高优发言 (' + focus.length + ' 条)</div>' +
          '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px;margin-bottom:28px">';
        focus.slice(0, 8).forEach(it => {
          html += '<div class="claude-thread-card" style="cursor:pointer" onclick="selectClaudeGroup(\'' + esc(it.group_code) + '\')">' +
            '<div class="claude-thread-header">' +
              '<span style="font-weight:600;font-size:13px;color:var(--claude-terracotta)">[' + esc(it.group_name) + ']</span>' +
              '<span style="font-size:11.5px;color:var(--claude-text-sub)">' + esc(it.sender) + ' · ' + esc(it.time) + '</span>' +
            '</div>' +
            '<div class="claude-thread-body" style="font-size:13.5px;line-height:1.6">' +
              esc(it.text) +
            '</div>' +
          '</div>';
        });
        html += '</div>';
      }

      html += '<div class="claude-chapter-title"><span class="claude-chapter-diamond">◈</span> 各群纵览与快速直达</div>' +
        '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px;margin-bottom:28px">';
      groups.forEach((g) => {
        const topKws = (g.keywords || []).slice(0, 5).map(kw => '<span class="claude-persona-tag" style="background:var(--claude-surface-warm);border:1px solid var(--claude-border);color:var(--claude-text);padding:2px 6px;font-size:11px">' + esc(kw.word) + '</span>').join(' ');
        const firstQuote = (g.quotes || [])[0];
        html += '<div class="claude-thread-card" style="display:flex;flex-direction:column;justify-content:space-between">' +
          '<div>' +
            '<div class="claude-thread-header">' +
              '<div style="font-family:var(--claude-serif);font-size:15px;font-weight:700;color:var(--claude-text)">' +
                (g.is_class ? '🎓 ' : '') + esc(g.name) +
              '</div>' +
              '<span class="claude-group-badge">' + g.nconv + ' 条对话</span>' +
            '</div>' +
            '<div class="claude-thread-body">' +
              '<div style="font-size:12px;color:var(--claude-text-sub);margin-bottom:8px">' +
                '参与群友 ' + g.people_count + ' 人 ｜ 活跃峰值 ' + (g.peak_hour < 10 ? '0' + g.peak_hour : g.peak_hour) + ':00 ｜ 覆盖 ' + g.span_hours + ' 小时' +
              '</div>' +
              (topKws ? '<div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:12px">' + topKws + '</div>' : '') +
              (firstQuote ? '<div class="claude-blockquote" style="margin:8px 0;font-size:13px;padding:8px 12px">“' + esc(firstQuote.text) + '”<div class="claude-blockquote-sender">—— ' + esc(firstQuote.sender) + '</div></div>' : '') +
            '</div>' +
          '</div>' +
          '<div style="padding:10px 18px;background:var(--claude-surface-warm);border-top:1px solid var(--claude-border-subtle);display:flex;justify-content:flex-end">' +
            '<button class="claude-tool-btn" onclick="selectClaudeGroup(\'' + esc(g.code) + '\')">进入深度阅读 →</button>' +
          '</div>' +
        '</div>';
      });
      html += '</div>';

      html += '<div class="claude-chapter-title"><span class="claude-chapter-diamond">◈</span> 全天讨论活跃度时序分布 (24-Hour Pulse)</div>' +
        '<div style="background:var(--claude-surface);border:1px solid var(--claude-border);border-radius:12px;padding:18px;margin-bottom:28px">' +
          render24hChart(k.hours_24, k.peak_hour) +
          '<div style="font-size:12px;color:var(--claude-text-sub);margin-top:12px;text-align:right">橙色高亮柱标示为今日讨论高峰时段 (' + esc(k.peak_hour_str || '') + ')</div>' +
        '</div>';

      if ((k.global_speakers || []).length) {
        html += '<div class="claude-chapter-title"><span class="claude-chapter-diamond">◈</span> 活跃群友画像名人堂 (Top Contributors)</div>' +
          '<div class="claude-persona-grid" style="margin-bottom:28px">';
        (k.global_speakers || []).slice(0, 12).forEach(spk => {
          html += '<div class="claude-persona-card">' +
            '<div>' +
              '<div class="claude-persona-name">' + esc(spk.name) + '</div>' +
              '<div style="font-size:11.5px;color:var(--claude-text-muted)">发言 ' + spk.count + ' 条 ｜ 占比 ' + spk.pct + '%</div>' +
            '</div>' +
            '<span class="claude-persona-tag ' + pillClass(spk.color) + '">' + esc(spk.persona) + '</span>' +
          '</div>';
        });
        html += '</div>';
      }

      html += '</div></div>';
      slot.innerHTML = html;
      return;
    }

    const curGroup = groups.find(g => g.code === state.claudeActiveGroup) || groups[0];
    if (!curGroup) {
      slot.innerHTML = '<div class="claude-lead-note">未找到指定群组记录。</div>';
      return;
    }

    const curIdx = groups.indexOf(curGroup);
    const prevGroup = curIdx > 0 ? groups[curIdx - 1] : null;
    const nextGroup = curIdx < groups.length - 1 ? groups[curIdx + 1] : null;

    let html = '<div class="claude-artifact-card">' +
      '<div class="claude-artifact-header">' +
        '<div class="claude-artifact-title-box">' +
          '<span class="claude-sparkle">✦</span>' +
          '<span class="claude-artifact-badge">Artifact · Deep Read</span>' +
          '<span class="claude-artifact-title">' + (curGroup.is_class ? '🎓 ' : '') + esc(curGroup.name) + '</span>' +
          '<span class="pill pill-slate" style="font-family:var(--claude-mono)">有效对话 ' + curGroup.nconv + ' 条 ｜ 参与 ' + curGroup.people_count + ' 人</span>' +
        '</div>' +
        '<div class="claude-artifact-tools">' +
          '<button class="claude-tool-btn" id="claude-copy-btn" onclick="copyClaudeGroupMarkdown(\'' + esc(curGroup.code) + '\')">📋 复制本群 Markdown</button>' +
          '<button class="claude-tool-btn" onclick="switchView(\'bento\'); selectGroup(\'' + esc(curGroup.code) + '\')">🍱 Bento 看板</button>' +
          '<button class="claude-tool-btn" onclick="window.print()">🖨️ 打印</button>' +
        '</div>' +
      '</div>' +
      '<div class="claude-article-body">' +
        '<div class="claude-lead-note">' +
          '💬 本群在统计窗口内共产生有效对话 <strong>' + curGroup.nconv + '</strong> 条（总消息 ' + curGroup.ntot + ' 条），' +
          '共有 <strong>' + curGroup.people_count + '</strong> 位群友发言。讨论黄金峰值出现在 <strong>' + (curGroup.peak_hour < 10 ? '0' + curGroup.peak_hour : curGroup.peak_hour) + ':00</strong> 前后。' +
          '首末发言跨度约 <strong>' + curGroup.span_hours + '</strong> 小时。' +
        '</div>';

    const groupNotices = (curGroup.quotes || []).filter(quo => quo.is_notice);
    if (groupNotices.length) {
      html += '<div class="claude-chapter-title"><span class="claude-chapter-diamond">◈</span> 群内重点通知与通告 (' + groupNotices.length + ' 条)</div>' +
        '<div style="display:flex;flex-direction:column;gap:10px;margin-bottom:24px">';
      groupNotices.forEach(n => {
        html += '<div class="claude-turn is-notice">' +
          '<div class="claude-avatar" style="background:var(--claude-terracotta)">📢</div>' +
          '<div class="claude-turn-content">' +
            '<div class="claude-turn-header">' +
              '<span class="claude-turn-sender">' + highlightText(n.sender, q) + '</span>' +
              '<span>' + esc(n.time) + '</span>' +
              '<span class="pill pill-amber" style="background:var(--claude-terracotta-soft);color:var(--claude-terracotta);border-color:var(--claude-terracotta)">📢 重点通告</span>' +
            '</div>' +
            '<div class="claude-turn-text">' + highlightText(n.text, q) + '</div>' +
          '</div>' +
        '</div>';
      });
      html += '</div>';
    }

    const threads = curGroup.threads || [];
    if (threads.length) {
      html += '<div class="claude-chapter-title"><span class="claude-chapter-diamond">◈</span> 话题脉络深度拆解 (' + threads.length + ' 个讨论阶段)</div>';
      threads.forEach(t => {
        const tid = curGroup.code + '-' + t.idx;
        const isOpen = !!state.claudeExpandedThreads[tid];
        html += '<div class="claude-thread-card" id="claude-topic-' + tid + '">' +
          '<div class="claude-thread-header">' +
            '<div class="claude-thread-title">◈ [' + esc(t.start_time) + ' ~ ' + esc(t.end_time) + '] ' + esc(t.title) + '</div>' +
            '<div class="claude-thread-meta">' +
              '<span>💬 ' + t.msg_count + ' 条讨论</span>' +
              '<span>👥 ' + t.people_count + ' 人参与</span>' +
            '</div>' +
          '</div>' +
          '<div class="claude-thread-body">';

        if (t.highlights && t.highlights.length) {
          html += '<div style="margin-bottom:12px;font-size:12.5px;font-weight:700;color:var(--claude-text-sub);letter-spacing:0.04em">核心观点与发言摘要:</div>' +
            '<div style="display:flex;flex-direction:column;gap:8px">';
          t.highlights.forEach(h => {
            html += renderClaudeTurn(h, q);
          });
          html += '</div>';
        }

        if (t.bubbles && t.bubbles.length) {
          html += '<div style="margin-top:14px;display:flex;justify-content:flex-end">' +
            '<button class="claude-tool-btn" id="claude-btn-' + tid + '" data-cnt="' + t.bubbles.length + '" onclick="toggleClaudeThread(\'' + tid + '\')">' +
              (isOpen ? '🔼 收起全部消息' : ('💬 展开全部消息 (' + t.bubbles.length + ' 条)')) +
            '</button>' +
          '</div>' +
          '<div id="claude-drawer-' + tid + '" style="display:' + (isOpen ? 'flex' : 'none') + ';flex-direction:column;gap:8px;margin-top:12px;border-top:1px solid var(--claude-border-subtle);padding-top:12px">';
          t.bubbles.forEach(b => {
            html += renderClaudeTurn(b, q);
          });
          html += '</div>';
        }

        html += '</div></div>';
      });
    }

    const quotes = curGroup.quotes || [];
    if (quotes.length) {
      html += '<div class="claude-chapter-title"><span class="claude-chapter-diamond">◈</span> 群聊高光金句辑录</div>' +
        '<div style="display:flex;flex-direction:column;gap:10px;margin-bottom:28px">';
      quotes.forEach(quo => {
        html += '<div class="claude-blockquote">' +
          '“' + highlightText(quo.text, q) + '”' +
          '<div class="claude-blockquote-sender">—— ' + esc(quo.sender) + ' · ' + esc(quo.time) + '</div>' +
        '</div>';
      });
      html += '</div>';
    }

    const speakers = curGroup.speakers || [];
    if (speakers.length) {
      html += '<div class="claude-chapter-title"><span class="claude-chapter-diamond">◈</span> 群友画像与发言分布榜</div>' +
        '<div class="claude-persona-grid" style="margin-bottom:32px">';
      speakers.forEach(spk => {
        html += '<div class="claude-persona-card">' +
          '<div>' +
            '<div class="claude-persona-name">' + esc(spk.name) + '</div>' +
            '<div style="font-size:11.5px;color:var(--claude-text-muted)">发言 ' + spk.count + ' 条 ｜ 占比 ' + spk.pct + '%</div>' +
          '</div>' +
          '<span class="claude-persona-tag ' + pillClass(spk.color) + '">' + esc(spk.persona) + '</span>' +
        '</div>';
      });
      html += '</div>';
    }

    html += '<div style="display:flex;align-items:center;justify-content:space-between;gap:12px;padding-top:20px;border-top:1px solid var(--claude-border);margin-top:20px;flex-wrap:wrap">' +
      (prevGroup ? '<button class="claude-tool-btn" onclick="selectClaudeGroup(\'' + esc(prevGroup.code) + '\')">← 上一个群：' + esc(prevGroup.name) + '</button>' : '<div></div>') +
      '<button class="claude-tool-btn" onclick="selectClaudeGroup(\'__ALL__\')">🌐 返回全群总览</button>' +
      (nextGroup ? '<button class="claude-tool-btn" onclick="selectClaudeGroup(\'' + esc(nextGroup.code) + '\')">下一个群：' + esc(nextGroup.name) + ' →</button>' : '<div></div>') +
    '</div>';

    html += '</div></div>';
    slot.innerHTML = html;
  }

  function renderMarkdownView() {
    return '<div class="section-card">' +
      '<div class="section-head">' +
        '<div class="section-title">📄 Markdown 完整报告原文</div>' +
        '<button class="icon-btn" id="copy-md-btn" onclick="copyMarkdown()">📋 复制全部 Markdown</button>' +
      '</div>' +
      '<div class="md-box">' + esc(DATA.markdown || '') + '</div>' +
    '</div>';
  }

  function render() {
    renderHeader();
    const root = document.getElementById('app-root');
    if (!root) return;
    if (state.view === 'claude') {
      root.innerHTML = renderClaudeView();
      renderClaudeSidebar();
      renderClaudeContent();
    } else if (state.view === 'markdown') {
      root.innerHTML = renderMarkdownView();
    } else if (state.view === 'paper') {
      root.innerHTML = renderPaperDailyView();
    } else {
      if (DATA.mode === 'digest') {
        root.innerHTML = renderDigestBento();
        renderContentArea();
      } else {
        root.innerHTML = renderGenericBento();
      }
    }
  }

  render();
})();
</script>
</body>
</html>
"""


def render_html_report(payload):
    """将结构化 payload 渲染为独立的离线 HTML 字符串。"""
    title = (payload.get('meta') or {}).get('title') or 'QQ 群聊可视化简报'
    safe_title = (
        str(title)
        .replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
        .replace('"', '&quot;')
    )
    json_str = _safe_json_for_script(payload)
    return HTML_TEMPLATE.replace('__PAGE_TITLE__', safe_title).replace('__DIGEST_JSON__', json_str)
