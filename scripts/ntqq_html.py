#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""零外部依赖的本地交互式 HTML 可视化简报渲染器 (ntqq_html.py)。

特性：
  1. 100% 本地离线单文件（内联现代 CSS + 原生 JS，不含任何外部 CDN/URL）
  2. 支持多种精美视图一键切换：
     - ✨ Claude 视窗（Anthropic 双轨流式速读、Artifact 设计语言）
     - 🌸 莫奈纸雕视图（参考 BaxterL/maison-des-mots 法式洛可可纸雕与手账拼贴美学，模切白边、右缘书签、信笺与火漆印章）
     - 🍱 Bento 交互看板（多群筛选、实时搜索、时段话题聚类、可展开 QQ 气泡、群友画像榜）
     - 📰 纸上日报视图（参考 TraceMemo paperdaily 报纸排版，适合沉浸速读与打印）
     - 📄 Markdown 原文视图（支持一键复制 Markdown）
  3. 支持全群/分群简报、群友发言专报（--member）、关键词检索（--search）与自然语言提问（--ask）
  4. 严格继承 --anonymize 脱敏规则，并使用安全 JSON 注入防止任何聊天内容触发 XSS 或破坏排版
"""
import os
import sys
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

    sanitized_rows_for_extract = []
    for _, _, code, _, r_list, _ in prepared:
        gm = anon_map.get(code, {})
        use_anon = is_anon or bool(gm)
        for r in r_list:
            r_dict = dict(r) if hasattr(r, 'keys') else (r if isinstance(r, dict) else {})
            sname = r_dict.get('sender_name') or ''
            txt = r_dict.get('text') or ''
            if use_anon and anon_unknown_fn:
                sname = anon_unknown_fn(sname, gm)
            if use_anon and mask_names_fn:
                txt = mask_names_fn(txt, gm)
            if is_anon and scrub_fn:
                txt = scrub_fn(txt)
            sanitized_rows_for_extract.append({
                'text': txt,
                'sender_name': sname,
                'sender': sname if use_anon else (r_dict.get('sender') or r_dict.get('sender_qq')),
                'timestamp': r_dict.get('ts') or r_dict.get('timestamp') or 0,
                'seq': r_dict.get('msg_seq') or r_dict.get('seq') or 0,
            })

    try:
        from ntqq_links import extract_links_and_resources
        links_data = extract_links_and_resources(sanitized_rows_for_extract)
    except Exception:
        links_data = []

    try:
        from ntqq_action_items import extract_action_items_and_decisions
        actions_data = extract_action_items_and_decisions(sanitized_rows_for_extract)
    except Exception:
        actions_data = {'actions': [], 'decisions': []}

    try:
        from ntqq_social import build_social_graph
        soc_graph = build_social_graph(sanitized_rows_for_extract)
    except Exception:
        soc_graph = {'nodes': [], 'edges': [], 'top_dyads': []}

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
        'links': links_data,
        'actions': actions_data,
        'social_graph': soc_graph,
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



_CACHED_TEMPLATE = None


def get_html_template() -> str:
    """Load offline HTML template with in-memory caching."""
    global _CACHED_TEMPLATE
    if _CACHED_TEMPLATE is not None:
        return _CACHED_TEMPLATE

    curr_dir = os.path.dirname(os.path.abspath(__file__))
    cand1 = os.path.join(curr_dir, "templates", "digest_report.html")
    bundle_dir = getattr(sys, "_MEIPASS", None)
    cand2 = (
        os.path.join(bundle_dir, "scripts", "templates", "digest_report.html")
        if bundle_dir
        else None
    )

    for cand in (cand1, cand2):
        if cand and os.path.exists(cand):
            with open(cand, "r", encoding="utf-8") as f:
                _CACHED_TEMPLATE = f.read()
                return _CACHED_TEMPLATE

    raise FileNotFoundError(
        f"digest_report.html not found. Checked: {cand1}"
        + (f" and {cand2}" if cand2 else "")
    )


def render_html_report(payload):
    """将结构化 payload 渲染为独立的离线 HTML 字符串。"""
    if payload.get('mode') == 'wrapped':
        from ntqq_wrapped import render_wrapped_html
        return render_wrapped_html(payload.get('extra', {}))
    title = (payload.get('meta') or {}).get('title') or 'QQ 群聊可视化简报'
    safe_title = (
        str(title)
        .replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
        .replace('"', '&quot;')
    )
    json_str = _safe_json_for_script(payload)
    template = get_html_template()
    return template.replace('__PAGE_TITLE__', safe_title).replace('__DIGEST_JSON__', json_str)


def __getattr__(name):
    if name == "HTML_TEMPLATE":
        return get_html_template()
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
