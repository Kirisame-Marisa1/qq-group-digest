#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 cached/index.db 生成报告：单群 / 多群汇总 / 关键词检索 / 群友发言专报。

用法示例：
  ntqq_report.py --active --hours 6 --brief
  ntqq_report.py --group <群号> --hours 48
  ntqq_report.py --search <关键词> --days 7
  ntqq_report.py --member <群友昵称> --days 30
"""
import argparse, bisect, json, math, os, re, sqlite3, sys
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ntqq_core import (open_index, now_ts, fmt, parse_when, log, log_debug, log_warn, set_log_level,
                       APP, OUTPUT, ensure_dirs, KNOW, GLOSSARY, TZ, MEDIA)
from datetime import datetime, timedelta
import sqlite3 as _sq3

SYS_KINDS = ('system', 'empty')
MEDIA_KINDS = ('image', 'sticker', 'audio')
NOISE_CHARS = set('的了是我你他她它们这那有和就不都也还很太吧呢吗啊哦嗯么什怎为以所以但而且如果因为可以没有一个是 在会要能对上下来去个之与及等把被让给')
BARS = '▁▂▃▄▅▆▇█'
KW_SAMPLE_CAP = 20000
HEXONLY = re.compile(r'^[0-9A-Fa-f]{2,8}[A-Za-z0-9]{0,4}$')
FILELIKE = re.compile(r'\.(jpg|jpeg|png|gif|webp|bmp|mp4|mp3|amr|silk|zip|rar|7z|pdf|docx?|xlsx?|txt)$', re.I)


def bar(v, mx, width=10):
    if not mx or not v:
        return ''
    return '█' * max(1, int(round(v / mx * width)))


def keywords(texts, min_count=3, topk=20):
    texts = texts[:KW_SAMPLE_CAP]
    cnt = Counter()
    for t in texts:
        t = re.sub(r'\[转发聊天记录[^\]]*\]', ' ', t or '')
        for run in re.findall(r'[\u4e00-\u9fffA-Za-z0-9]{2,}', t):
            if FILELIKE.search(run):
                continue
            L = len(run)
            for n in (2, 3, 4, 5, 6):
                if n > L:
                    break
                for i in range(L - n + 1):
                    g = run[i:i + n]
                    if all(c in NOISE_CHARS for c in g):
                        continue
                    has_cjk = any('\u4e00' <= c <= '\u9fff' for c in g)
                    if not has_cjk:
                        if HEXONLY.match(g) or g.isdigit():
                            continue
                        if n < 3:
                            continue
                    cnt[g] += 1
    cand = sorted(((g, c) for g, c in cnt.items() if c >= min_count),
                  key=lambda x: (-x[1] * (1 + 0.45 * (len(x[0]) - 2)), -len(x[0]), x[0]))
    kept = []
    for g, c in cand:
        if any(g in k and cnt[k] >= c for k, _ in kept):
            continue
        kept.append((g, c))
        if len(kept) >= topk:
            break
    return kept


NOTICE_RE = re.compile(r'通知|公告|全体成员|@全体|报名|截止|考试|安排|开会|班会|答辩|选课|放假|调休|'
                         r'地点|时间|务必|重要|请大家|记得|提交|作业|补考|重修|会议|签到|统计|填表|接龙')
CLASS_HINT = re.compile(r'班|通知群|学习|学院|年级')


def focus_block(rows, title='重点速览（通知 / 公告 / 长文 / 转发）', top=40, anon=None):
    """挑出最像「通知、公告、需要知道的事」的消息。"""
    scored = []
    for r in rows:
        t = (r['text'] or '').strip()
        if not t:
            continue
        s = 0
        if len(t) >= 80:
            s += 2
        if len(t) >= 200:
            s += 2
        if NOTICE_RE.search(t):
            s += 3
        if '@全体' in t or '全体成员' in t:
            s += 3
        if '[转发聊天记录' in t:
            s += 2
        if r['kind'] == 'forward':
            s += 1
        if s <= 1:
            continue
        scored.append((s, r['ts'], r))
    if not scored:
        return ''
    scored.sort(key=lambda x: (-x[0], -x[1]))
    L = ['## ' + title, '']
    for s, ts, r in scored[:top]:
        t = (r['text'] or '').replace(chr(10), ' ⏎ ')
        if len(t) > 600:
            t = t[:600] + '…'
        gm = (anon or {}).get(r['group_code'], {})
        if gm:
            who = anon_unknown(r['sender_name'], gm)
            t = mask_names(t, gm)
        else:
            who = r['sender_name']
        L.append('- **%s** [%s] %s (%s)' % (fmt(ts, '%m-%d %H:%M'), r['group_name'] or r['group_code'],
                                            who, '重要度 %d' % s))
        L.append('  > ' + t)
    L.append('')
    return chr(10).join(L) + chr(10)


def block(code, name, rows, max_msgs, show_media=False, prev_n=None, brief=False, anon=None):
    anon = anon or {}
    def nm(x):
        return anon_unknown(x, anon) if anon else x
    def mk(text):
        return mask_names(text, anon) if anon else text
    conv = [r for r in rows if r['kind'] not in SYS_KINDS]
    ts = [r['ts'] for r in rows]
    if not ts:
        return '', 0
    t0, t1 = min(ts), max(ts)
    people = Counter(nm(r['sender_name']) for r in conv if r['sender_name'] not in (None, '', '未知', '系统'))
    day = Counter(r['day'] for r in rows)
    hour = Counter(r['hour'] for r in rows if r['hour'] >= 0)
    bucket = set(r['ts'] // 600 for r in rows)
    kinds = Counter(r['kind'] for r in conv)
    ats = Counter()
    for r in conv:
        for m in re.findall(r'@([^\s@|]{1,16})', mk(r['text']) or ''):
            ats[m] += 1
    L = []
    L.append('## %s (%s)' % (name or '未知群', code_label(code)))
    L.append('')
    L.append('- 共 %d 条消息，其中有效对话 **%d 条**，参与 %d 人，时段 %s ~ %s'
             % (len(rows), len(conv), len(people), fmt(t0, '%m-%d %H:%M'), fmt(t1, '%m-%d %H:%M')))
    L.append('- 时间跨度 %.1f 小时，其中有消息的时间约 %d 分钟'
             % ((t1 - t0) / 3600.0, len(bucket) * 10))
    if prev_n is not None:
        d = len(rows) - prev_n
        L.append('- 与上一个等长窗口相比 %s（%+d 条）' % ('↑' if d > 0 else ('↓' if d < 0 else '→'), d))
    if kinds:
        L.append('- 内容构成: %s' % ', '.join('%s=%d' % (k, v) for k, v in kinds.most_common()))
    src = sum(1 for r in rows if r['kind'] in SYS_KINDS)
    if src:
        L.append('- 另有系统/通知 %d 条（未计入）' % src)
    ds = sorted(day)
    L.append('- 逐日: ' + '  '.join('%s %s%d' % (d[5:], bar(day[d], max(day.values()), 6), day[d]) for d in ds))
    L.append('- 逐时: ' + ' '.join('%02d' % h for h in range(24)))
    L.append('        ' + ' '.join(bar(hour.get(h, 0), max(hour.values()) if hour else 1, 1) or '·' for h in range(24)))
    if people:
        L.append('- 发言最多: ' + ', '.join('%s(%d)' % (n, c) for n, c in people.most_common(15)))
    if ats:
        L.append('- 被 @ 最多: ' + ', '.join('%s(%d)' % (n, c) for n, c in ats.most_common(8)))
    kws = keywords([mk(r['text']) for r in conv if r['text']])
    if kws:
        L.append('- 高频词: ' + ', '.join('%s(%d)' % (g, c) for g, c in kws))
    L.append('')

    show = [r for r in conv if show_media or r['kind'] not in MEDIA_KINDS]
    omitted = len(conv) - len(show)
    if show:
        if brief:
            show = show[-min(max_msgs, len(show)):]
        else:
            show = show[:max_msgs]
        L.append('### 对话原文（%s共 %d 条有效对话%s）'
                 % ('最近 ' if brief else '', len(conv),
                    '，已省略图片/表情 %d 条' % omitted if omitted > 0 else ''))
        L.append('')
        for r in show:
            t = mk(r['text'] or '').replace('\n', ' ⏎ ')
            if len(t) > 300:
                t = t[:300] + '…'
            if not t:
                continue
            if r['kind'] != 'text':
                t = '[' + r['kind'] + '] ' + t
            L.append('%s %s: %s' % (fmt(r['ts'], '%m-%d %H:%M'), nm(r['sender_name']), t))
        L.append('')
    return '\n'.join(L), len(conv)


# ── 自然语言提问（关键词检索） ─────────────────────────────────────────────
# 只在「结构助词/代词」处断句，绝不切 不/过/来 —— 否则「看不过来」会碎成「看」「来」
PARTICLE = set('的了是吗呢吧啊哦嗯呀嘛着我你他她它们这那有在')
COMMON2 = set('''这个 那个 什么 怎么 就是 不是 可以 没有 我们 你们 他们 有人 一个 现在 知道 因为 所以
但是 然后 还是 已经 时候 感觉 真的 应该 可能 一样 出来 起来 一下 这样 那样 自己 大家 同学 老师 今天 明天 昨天
早上 晚上 上午 下午 东西 事情 问题 时间 地方 有点 表示 直接 完全 突然 是不是 有没有 抱怨 什么 最近 刚刚'''.split())
ASK_STOP = ['是不是', '有没有', '有没有人', '刚刚', '刚才', '今天', '昨天', '最近', '现在', '这几天',
            '这个', '那个', '有人', '请问', '帮我', '查一下', '看看', '谁', '什么', '怎么', '为啥',
            '为什么', '是不是有人', '在不在', '聊过', '聊了', '说过', '提到', '关于', '一下', '吗', '呢', '吧']
ANON_DIR = os.path.join(KNOW, 'anonymize')


def parse_time_hint(q, default_days=7):
    now = now_ts()
    if any(k in q for k in ('刚刚', '刚才', '方才')):
        return now - 2 * 3600, '刚刚（近 2 小时）'
    if '今天' in q or '今日' in q:
        d = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        return int(d.timestamp()), '今天'
    if '昨天' in q or '昨日' in q:
        d = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        return int(d.timestamp()), '昨天起'
    if '这周' in q or '本周' in q or '这个星期' in q:
        d = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        return int((d - timedelta(days=d.weekday())).timestamp()), '本周'
    if '最近' in q or '这几天' in q:
        return now - int(default_days * 86400), '最近 %d 天' % default_days
    return now - int(default_days * 86400), '最近 %d 天（未指定时间）' % default_days


def _lcs_len(a, b):
    """最长公共子串长度（群名匹配用）。"""
    if not a or not b:
        return 0
    best = 0
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def load_aliases():
    try:
        with open(os.path.join(APP, 'config.json'), encoding='utf-8') as fh:
            return json.load(fh).get('group_aliases', {})
    except Exception:
        return {}


GENERIC = set('群 的 了 是 有 在 和 与 或 东方 同好 交流 会 社 们 大家')


def resolve_group(idx, q, topn=4, grp_table='groups'):
    """从问句猜群：先做别名展开，再按最长公共子串打分。"""
    aliases = load_aliases()
    qq = q
    for k, v in aliases.items():
        if k in qq:
            qq = qq.replace(k, v)
    rows = idx.execute('SELECT group_code, group_name FROM %s WHERE group_name IS NOT NULL' % grp_table).fetchall()
    scored = []
    for code, name in rows:
        if not name:
            continue
        cjk_q = ''.join(c for c in qq if '\u4e00' <= c <= '\u9fff')
        cjk_n = ''.join(c for c in name if '\u4e00' <= c <= '\u9fff')
        a = _lcs_len(cjk_q, cjk_n)          # 中文连续命中
        b = _lcs_len(qq, name) - a          # 英文/数字连续命中
        # 中文权重高：靠 "tho" 蒙对不算数；「东方」「同好」这类通用词也要求连续 3 字以上才算
        s = a * 2 + max(0, b)
        if a >= 3:
            s += 2
        if s > 0:
            scored.append((s, code, name))
    scored.sort(reverse=True)
    return scored[:topn]


def extract_keywords(q):
    known = []
    try:
        with open(os.path.join(GLOSSARY, 'terms.json'), encoding='utf-8') as fh:
            store = json.load(fh)
        for t in store.get('terms', {}):
            if t.lower() in q.lower():
                known.append(t)
    except Exception as e:
        log_debug('读取词库 terms.json 失败: %r' % e)
    clean = q
    for k, v in load_aliases().items():
        clean = clean.replace(k, v)
    for s in ASK_STOP:
        clean = clean.replace(s, ' ')
    # 在结构助词处断开，再在每段内取 2/3/4 元组
    clean = ''.join((' ' if c in PARTICLE else c) for c in clean)
    grams = set()
    for run in re.findall(r'[\u4e00-\u9fff]{2,}', clean):
        for L in (2, 3, 4):
            for i in range(len(run) - L + 1):
                grams.add(run[i:i + L])
    for run in re.findall(r'[A-Za-z0-9]{2,}', clean):
        if not run.isdigit():
            grams.add(run.lower())
    return known, sorted(grams, key=lambda x: -len(x))


def search_messages(idx, query, t0, t1, msg_table='messages'):
    """检索消息：对群消息优先尝试 FTS5 全文检索（支持布尔/多词查询），再与多关键词 LIKE 合并去重。"""
    q = (query or '').strip()
    if not q:
        return []
    seen_ids = set()
    out = []

    # 1. 若检索群消息，先尝试 FTS5 全文匹配（对英文词、数字、布尔表达式极快）
    if msg_table == 'messages':
        try:
            fts_sql = ('SELECT m.* FROM messages_fts f JOIN messages m ON f.rowid = m.msg_id '
                       'WHERE messages_fts MATCH ? AND m.ts>=? AND m.ts<=? ORDER BY m.ts')
            for r in idx.execute(fts_sql, (q, t0, t1)).fetchall():
                mid = r['msg_id']
                if mid not in seen_ids:
                    seen_ids.add(mid)
                    out.append(r)
        except sqlite3.OperationalError as e:
            log_debug('FTS5 查询未命中或语法回退 LIKE: %s' % e)

    # 2. 中文子串/多词 AND LIKE 匹配（无需分词库即可命中任意连续汉字片段）
    terms = [t for t in q.split() if t.upper() not in ('AND', 'OR', 'NOT')]
    if not terms:
        terms = [q]
    like_clauses = ' AND '.join('text LIKE ?' for _ in terms)
    params = [t0, t1] + ['%' + t.strip('"') + '%' for t in terms]
    like_sql = 'SELECT * FROM %s WHERE ts>=? AND ts<=? AND %s ORDER BY ts' % (msg_table, like_clauses)
    for r in idx.execute(like_sql, params).fetchall():
        mid = r['msg_id']
        if mid not in seen_ids:
            seen_ids.add(mid)
            out.append(r)

    out.sort(key=lambda r: r['ts'])
    return out


def ask(idx, q, t0, t1, group_codes=None, limit=40, msg_table='messages', grp_table='groups'):
    known, grams = extract_keywords(q)
    kws = list(dict.fromkeys(known + grams))
    # 群名本身不算检索词（转发块里每条都带群名，会把结果全污染）
    excl = []
    if group_codes:
        for c in group_codes:
            r = idx.execute('SELECT group_name FROM %s WHERE group_code=?' % grp_table, (c,)).fetchone()
            if r and r[0]:
                excl.append(r[0])
    if excl:
        kws = [k for k in kws if not any(k in n for n in excl)]
    where = 'ts>=? AND ts<=?'
    params = [t0, t1]
    if group_codes:
        where += ' AND group_code IN (%s)' % ','.join('?' * len(group_codes))
        params += list(group_codes)
    rows = idx.execute('SELECT * FROM %s WHERE %s ORDER BY group_code, ts' % (msg_table, where), params).fetchall()
    n = max(1, len(rows))
    def clean_meta(t):
        t = re.sub(r'\[转发聊天记录[^\]]*\]', ' ', t or '')
        for n in excl:
            t = t.replace(n, ' ')
        return t.lower()
    texts = [clean_meta(r['text']) for r in rows]
    df = {}
    for k in kws:
        kl = k.lower()
        df[k] = sum(1 for t in texts if kl in t)
    # IDF 加权：越罕见的词权重越高；「tho」「东方」这类背景词自动变轻
    idf = {k: math.log((n + 1.0) / (df[k] + 1.0)) for k in kws}
    live = {k: idf[k] for k in kws if df[k] > 0}
    tot = sum(live.values()) or 1.0
    dropped = [k for k in kws if df[k] >= n * 0.5]
    scored = []
    for i, (r, tl) in enumerate(zip(rows, texts)):
        hit = [k for k in live if k.lower() in tl]
        if not hit:
            continue
        low = set(h.lower() for h in hit)
        if len(low) < 2:
            continue
        w = sum(idf[k] for k in hit)
        scored.append((round(w / tot, 3), len(low), r['ts'], r, hit, i))
    scored.sort(key=lambda x: (-x[0], -x[2]))
    return kws, known, scored, dropped, n, rows


def anon_code(n):
    s = ''
    while n > 0:
        n, rem = divmod(n - 1, 26)
        s = chr(65 + rem) + s
    return s


def build_anon(idx, codes=None, msg_table='messages'):
    """昵称 -> 稳定代号（按发言量排序，存本地，跨次一致）。"""
    os.makedirs(ANON_DIR, exist_ok=True)
    out = {}
    q = 'SELECT DISTINCT group_code FROM %s' % msg_table
    for (code,) in idx.execute(q):
        if codes and code not in codes:
            continue
        p = os.path.join(ANON_DIR, '%s.json' % code)
        m = {}
        if os.path.exists(p):
            try:
                with open(p, encoding='utf-8') as fh:
                    m = json.load(fh)
            except Exception as e:
                log_debug('读脱敏映射 %s 失败: %r' % (p, e))
                m = {}
        used = set(m.values())
        n = 1
        sql = ("SELECT sender_name, COUNT(*) c FROM %s WHERE group_code=? "
               "AND sender_name NOT IN ('系统','未知','') GROUP BY sender_name ORDER BY c DESC" % msg_table)
        for name, c in idx.execute(sql, (code,)):
            if name and name not in m:
                while ('用户' + anon_code(n)) in used:
                    n += 1
                m[name] = '用户' + anon_code(n)
                used.add(m[name])
        if m:
            with open(p, 'w', encoding='utf-8') as fh:
                json.dump(m, fh, ensure_ascii=False, indent=1)
        out[code] = m
    return out


QQNUM = re.compile(r'(?<!\d)\d{6,11}(?!\d)')
FWD_NAME = re.compile(r'(\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\]\s*)([^:\n]{1,24})(:)')
AT_NAME = re.compile(r'@([^\s@|]{1,16})')
_ANON_EXTRA = {}
_ANON_CTR = [0]
ANON_LABELS = {}          # 群号 -> 群N（脱敏时用）


def code_label(code):
    """脱敏模式下把群号换成「群N」，否则原样返回。"""
    return ANON_LABELS.get(code, code)


def anon_unknown(name, gm):
    """本群映射里没有的名字（多见于转发的原作者）也发一个代号。"""
    name = name.strip()
    if not name or name.startswith('用户') or name.startswith('匿名'):
        return name
    if gm.get(name):
        return gm[name]
    if name not in _ANON_EXTRA:
        _ANON_CTR[0] += 1
        _ANON_EXTRA[name] = '匿名%d' % _ANON_CTR[0]
    return _ANON_EXTRA[name]


def mask_names(text, gm):
    if not text:
        return text
    for k, v in gm.items():
        if k and k in text:
            text = text.replace(k, v)
    text = FWD_NAME.sub(lambda m: m.group(1) + anon_unknown(m.group(2), gm) + m.group(3), text)
    text = AT_NAME.sub(lambda m: '@' + anon_unknown(m.group(1), gm), text)
    return scrub(text)


def scrub(text):
    if not text:
        return text
    return QQNUM.sub('【QQ号】', text)


# ── 群友个人发言专报 ────────────────────────────────────────────────────────
AT_RE = re.compile(r'@([^\s@|]{1,24})')


def _clip(s, n=1200):
    s = s or ''
    return s if len(s) <= n else s[:n] + '…[+%d字]' % (len(s) - n)


def member_report(idx, out, who, t0, t1, group_spec=None, exact=False, ctx=2,
                  max_lines=0, detail_top=5, all_matches=False,
                  msg_table='messages', grp_table='groups'):
    """某位群友的发言专报：概览 + 逐条原文（正文不截断，可选带上文）。

    who        : 昵称（默认子串匹配，exact=True 时完全相等）
    group_spec : 限定群（群号或群名片段）的列表；None = 全部群
    ctx        : 每条发言前带几条其他人的上文（0 = 不带）
    max_lines  : 最多印多少条自己的发言（0 = 不限）
    """
    out.append('## 群友发言：%s' % who)
    out.append('')
    where = 'sender_name %s ? AND ts>=? AND ts<=?' % ('=' if exact else 'LIKE')
    params = [who if exact else '%' + who + '%', t0, t1]

    codes = None
    if group_spec:
        codes = []
        for g in idx.execute('SELECT group_code, group_name FROM %s' % grp_table):
            for s in group_spec:
                s = (s or '').strip()
                if not s:
                    continue
                if str(g['group_code']) == s or (g['group_name'] and s in g['group_name']) or s in str(g['group_code']):
                    if g['group_code'] not in codes:
                        codes.append(g['group_code'])
        if not codes:
            out.append('- ⚠ 没匹配到群 %s；用 `--list-groups` 看群名/群号。' % group_spec)
            return {'matched': 0, 'n': 0}
        where += ' AND group_code IN (%s)' % ','.join('?' * len(codes))
        params += codes

    rows = idx.execute('SELECT * FROM %s WHERE %s ORDER BY ts' % (msg_table, where), params).fetchall()
    out.append('- 查询窗口: %s ~ %s%s'
               % (fmt(t0, '%Y-%m-%d %H:%M'), fmt(t1, '%Y-%m-%d %H:%M'),
                  ('；限定群 %s' % '、'.join(str(c) for c in codes)) if codes else '（全部群）'))
    if not rows:
        out.append('')
        out.append('> 窗口内没有该昵称的发言。放宽窗口（`--days`/`--since`）或换昵称片段再试。')
        return {'matched': 0, 'n': 0}

    by = {}
    for r in rows:
        by.setdefault(r['sender_name'], []).append(r)
    out.append('- 命中 **%d 个昵称**，合计 **%d 条**' % (len(by), len(rows)))
    out.append('')
    if len(by) > 1:
        out.append('| 昵称 | 条数 | 首发 | 末次 | 主要群 |')
        out.append('|---|---|---|---|---|')
        for nm, rs in sorted(by.items(), key=lambda x: -len(x[1])):
            gl = Counter((r['group_name'] or str(r['group_code'])) for r in rs)
            out.append('| %s | %d | %s | %s | %s |' % (
                nm, len(rs), fmt(min(r['ts'] for r in rs), '%m-%d %H:%M'),
                fmt(max(r['ts'] for r in rs), '%m-%d %H:%M'),
                '、'.join('%s(%d)' % (k, v) for k, v in gl.most_common(3))))
        out.append('')
        out.append('> 命中多个昵称说明它们**不是同一个人**，下面逐个给出概览与原文。')
        out.append('')

    order = sorted(by.items(), key=lambda x: -len(x[1]))
    if len(order) > detail_top and not all_matches:
        out.append('> 命中 %d 个昵称，默认只展开条数最多的前 %d 个；要全部展开加 --all-matches，'
                   '用更长的昵称片段或 --exact 可以一次命中一个。' % (len(order), detail_top))
        out.append('')
        order = order[:detail_top]

    for nm, rs in order:
        conv = [r for r in rs if r['kind'] not in SYS_KINDS]
        days = sorted({r['day'] for r in rs})
        out.append('### %s · 概览' % nm)
        out.append('')
        out.append('- 发言 **%d 条**（有效对话 %d），覆盖 **%d 天**（%s）'
                   % (len(rs), len(conv), len(days), '、'.join(days[:24])))
        out.append('- 内容构成: ' + ', '.join('%s=%d' % (k, v)
                   for k, v in Counter(r['kind'] for r in rs).most_common()))
        hd = Counter(r['hour'] for r in rs)
        mx = max(hd.values()) if hd else 1
        out.append('- 活跃时段（时:条数）:')
        for h in range(24):
            if hd.get(h):
                out.append('    %02d时 %-12s %d' % (h, bar(hd[h], mx, 12), hd[h]))
        ats = Counter()
        for r in conv:
            for m in AT_RE.findall(r['text'] or ''):
                ats[m.strip()] += 1
        if ats:
            out.append('- @ 过的人: ' + '、'.join('%s(%d)' % (k, v) for k, v in ats.most_common(10)))
        safe = nm.replace('%', '').replace('_', '')
        if safe:
            gcodes = sorted({r['group_code'] for r in rs})
            q = 'SELECT * FROM %s WHERE ts>=? AND ts<=? AND sender_name<>? AND text LIKE ?' % msg_table
            pp = [t0, t1, nm, '%' + safe + '%']
            if gcodes:
                q += ' AND group_code IN (%s)' % ','.join('?' * len(gcodes))
                pp += gcodes
            be = idx.execute(q, pp).fetchall()
            if be:
                out.append('- 被别人 @/提到 **%d 次**，主要来自: %s' % (len(be),
                           '、'.join('%s(%d)' % (k, v)
                                     for k, v in Counter(r['sender_name'] for r in be).most_common(8))))
        kw = keywords([r['text'] for r in conv if r['text']], min_count=3, topk=15)
        if kw:
            out.append('- 高频词: ' + ', '.join('%s(%d)' % (k, v) for k, v in kw))
        out.append('')

        out.append('### %s · 逐条原文（正文不截断%s）'
                   % (nm, ('，每条带 %d 条上文' % ctx) if ctx else ''))
        out.append('')
        cache, shown, curday, last_ctx = {}, 0, None, set()
        for r in rs:
            if r['day'] != curday:
                curday = r['day']
                out.append('**%s**' % curday)
                out.append('')
            if ctx:
                code = r['group_code']
                if code not in cache:
                    cache[code] = idx.execute(
                        'SELECT ts, sender_name, text, kind FROM %s WHERE group_code=? AND ts>=? AND ts<=? ORDER BY ts'
                        % msg_table,
                        (code, t0 - 3600, t1)).fetchall()
                arr = cache[code]
                tss = [x['ts'] for x in arr]
                i = bisect.bisect_left(tss, r['ts'])
                cur_ctx = [p for p in arr[max(0, i - ctx):i]
                           if (p['text'] or '').strip() and p['kind'] not in SYS_KINDS]
                for p in cur_ctx:
                    # 连续两条自己的发言，上文大量重叠——只印上一轮没印过的那几条
                    if (p['ts'], p['sender_name']) in last_ctx:
                        continue
                    out.append('    ↳ %s %s: %s'
                               % (fmt(p['ts'], '%H:%M'), p['sender_name'], _clip(p['text'], 200)))
                last_ctx = {(p['ts'], p['sender_name']) for p in cur_ctx}
            loc = '' if (len(by) == 1 and not codes) else '[%s] ' % (r['group_name'] or r['group_code'])
            body = _clip(r['text']) if (r['text'] or '').strip() else '[%s]' % r['kind']
            out.append('%s %s%s: %s' % (fmt(r['ts'], '%m-%d %H:%M'), loc, nm, body))
            shown += 1
            if max_lines and shown >= max_lines:
                out.append('')
                out.append('> 已达 --max-msgs 上限（%d 条），后面的没印；调大 --max-msgs 或收窄窗口。' % max_lines)
                break
        out.append('')

    return {'matched': len(by), 'n': len(rows),
            'names': [{'name': nm, 'n': len(rs)} for nm, rs in sorted(by.items(), key=lambda x: -len(x[1]))]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ask', default=None, help='自然语言提问，例如「某群今天聊某话题了吗」')
    ap.add_argument('--ctx', type=int, default=3, help='--ask / --member 时每条命中前带几条上下文（默认 3；--member 传 0 可关掉）')
    ap.add_argument('--anonymize', action='store_true', help='昵称->代号，并抹掉 QQ 号（外传用）')
    ap.add_argument('--active', action='store_true')
    ap.add_argument('--group', action='append', default=[])
    ap.add_argument('--c2c', action='store_true', help='查询私聊记录（与群聊完全隔离，需先 ntqq_build.py --include-c2c）')
    ap.add_argument('--contact', action='append', default=[], help='指定私聊联系人 QQ 号或昵称片段（自动开启 --c2c）')
    ap.add_argument('--search', default=None)
    ap.add_argument('--member', default=None,
                    help='某位群友的昵称（默认子串匹配）。输出「概览 + 逐条原文」，正文不截断；'
                         '配合 --group 限定群、--exact 精确匹配、--days/--since 限定窗口')
    ap.add_argument('--exact', action='store_true', help='--member 时要求昵称完全相等')
    ap.add_argument('--all-matches', action='store_true',
                    help='--member 命中多个昵称时全部展开（默认只展开条数最多的前 5 个）')
    ap.add_argument('--list-groups', action='store_true')
    ap.add_argument('--since'); ap.add_argument('--until')
    ap.add_argument('--hours', type=float); ap.add_argument('--days', type=float)
    ap.add_argument('--max-msgs', type=int, default=None,
                    help='最多印多少条（群报告默认 150；--member 模式默认为不限，只在显式指定时才截断）')
    ap.add_argument('--max-chars', type=int, default=200000,
                    help='整份报告的全局字符上限（不是每行！）。超出即把整篇从尾巴截断并附一行标记；默认 %(default)s。')
    ap.add_argument('--min-conv', type=int, default=1)
    ap.add_argument('--show-media', action='store_true', help='正文里也列出图片/表情/语音')
    ap.add_argument('--brief', action='store_true', help='每群只输出最近的原文，适合多群速览')
    ap.add_argument('--out', default=None, help='把报告写入文件（默认同时生成同名 .html 交互式可视化简报）')
    ap.add_argument('--html', default=None, help='把交互式可视化简报写入指定 HTML 路径')
    ap.add_argument('--no-html', action='store_true', help='传 --out 时不自动生成同名 .html 可视化简报')
    ap.add_argument('--json', default=None)
    ap.add_argument('--class-mode', action='store_true', help='班群模式：重点速览 + 不限篇幅逐条')
    ap.add_argument('--no-focus', action='store_true', help='不输出重点速览')
    ap.add_argument('--media-list', default=None, help='把窗口内图片候选写成 TSV')
    ap.add_argument('--no-group-state', action='store_true',
                    help='不在报告里输出「群变更」（新入群/退群/改名）段落')
    ap.add_argument('--verbose', action='store_true', help='显示调试日志')
    a = ap.parse_args()

    if a.verbose:
        set_log_level(True)
    if a.contact:
        a.c2c = True
        a.group.extend(a.contact)
    msgs_given = a.max_msgs is not None
    if a.max_msgs is None:
        a.max_msgs = 150

    md_out = a.out
    html_out = a.html
    if a.out and a.out.lower().endswith(('.html', '.htm')):
        html_out = a.html or a.out
        md_out = None
    elif a.out and not a.no_html and not html_out:
        html_out = os.path.splitext(a.out)[0] + '.html'

    msg_table = 'c2c_messages' if a.c2c else 'messages'
    grp_table = 'c2c_groups' if a.c2c else 'groups'

    idx = open_index(); idx.row_factory = sqlite3.Row
    def meta(k, d='0'):
        r = idx.execute('SELECT value FROM meta WHERE key=?', (k,)).fetchone()
        return r[0] if r else d
    cutoff = int(meta('cutoff_ts') or 0)
    built = int(meta('built_at') or 0)
    scan_rows = meta('scan_rows'); errs = meta('read_errors')
    src_mode = meta('source_mode', '')
    bad_days = json.loads(meta('bad_days', '[]') or '[]')

    if a.list_groups:
        for g in idx.execute('SELECT * FROM %s ORDER BY n DESC' % grp_table):
            print('%s  %-30s %6d 条  %s ~ %s' % (g['group_code'], (g['group_name'] or '-')[:30], g['n'],
                  fmt(g['first_ts'], '%m-%d %H:%M'), fmt(g['last_ts'], '%m-%d %H:%M')))
        return

    now = now_ts()
    if a.since:
        t0 = parse_when(a.since)
    elif a.hours:
        t0 = now - int(a.hours * 3600)
    elif a.days:
        t0 = now - int(a.days * 86400)
    else:
        t0 = now - 6 * 3600
    t1 = parse_when(a.until) if a.until else now
    span = max(60, t1 - t0)

    out = []
    # ── 群变更提示（新入群 / 退群 / 回归 / 改名）─────────────────────────────
    state_lines = []
    if not a.no_group_state and not a.c2c:
        try:
            import ntqq_group_state as GS
            sres = GS.scan()
            if not sres.get('ok'):
                state_lines.append('> ⚠ 群变更未生效：%s' % sres.get('reason'))
            else:
                if sres.get('suspect_snapshot'):
                    state_lines.append('> ⚠ 本次读到的群数异常偏少，已挂起退群判定（疑似 group_info.db 未读出）')
                if sres['new']:
                    state_lines.append('> 🆕 **新入群 %d 个**：%s'
                                       % (len(sres['new']),
                                          '、'.join('%s(%s)' % (r.get('name') or '?', r['code']) for r in sres['new'][:10])))
                if sres['returned']:
                    state_lines.append('> ↩ 退群后又回来 %d 个：%s'
                                       % (len(sres['returned']),
                                          '、'.join('%s(%s)' % (r.get('name') or '?', r['code']) for r in sres['returned'][:10])))
                if sres['left']:
                    state_lines.append('> 🚪 本次判定已退群 %d 个：%s'
                                       % (len(sres['left']),
                                          '、'.join('%s(%s)' % (r.get('name') or '?', r['code']) for r in sres['left'][:10])))
                if sres['renamed']:
                    state_lines.append('> ✏ 群名变更 %d 个：%s'
                                       % (len(sres['renamed']),
                                          '、'.join('%s→%s' % (a_, b_) for _c, a_, b_ in sres['renamed'][:6])))
        except Exception as e:
            state_lines.append('> ⚠ 群变更读取失败：%r' % e)

    if not a.no_group_state and not a.c2c:
        for ln in state_lines:
            out.append(ln)
        if state_lines:
            out.append('')

    src_mode_label = {'sql': '全盘解密（默认/快）', 'stream': '按需解密（备选/慢）', 'snap': '一致快照'}.get(src_mode, src_mode or '未记录')
    if a.member:
        out.append('# QQ 群友发言专报 · %s' % a.member)
    else:
        out.append('# QQ 私聊报告' if a.c2c else '# QQ 群聊报告')
    out.append('')
    out.append('> **数据截止 %s**（索引构建于 %s，距现在 %d 分钟；本次扫描 %s 行，读取错误 %s 次；取数方案：%s）'
               % (fmt(cutoff, '%Y-%m-%d %H:%M:%S'), fmt(built, '%Y-%m-%d %H:%M:%S'),
                  int((now - cutoff) / 60) if cutoff else -1, scan_rows, errs, src_mode_label))
    if bad_days and not a.c2c:
        out.append('> ⚠ 数据缺口：有 %d 个「群-日」读取失败被跳过：%s'
                   % (len(bad_days), ', '.join('%s@%s' % (g, d) for g, d in bad_days[:12])))
    out.append('')
    out.append('- 查询窗口: %s ~ %s' % (fmt(t0, '%Y-%m-%d %H:%M'), fmt(t1, '%Y-%m-%d %H:%M')))
    cur_n = prev_n = 0
    if not a.member:                     # --member 模式下全库总量统计没有意义，不印
        cur_n = idx.execute('SELECT COUNT(*) FROM %s WHERE ts>=? AND ts<=?' % msg_table, (t0, t1)).fetchone()[0]
        prev_n = idx.execute('SELECT COUNT(*) FROM %s WHERE ts>=? AND ts<?' % msg_table, (t0 - span, t0)).fetchone()[0]
        out.append('- 窗口内总消息 %d 条；上一个等长窗口 %d 条；趋势 %s（%+d）'
                   % (cur_n, prev_n, '↑' if cur_n > prev_n else ('↓' if cur_n < prev_n else '→'), cur_n - prev_n))
    out.append('')
    result = {'cutoff_ts': cutoff, 'window': [t0, t1], 'groups': []}

    meta_info = {
        'title': ('QQ 群友发言专报 · %s' % a.member) if a.member else (
            ('QQ 聊天提问 · %s' % a.ask) if a.ask else (
                ('QQ 关键词检索 · %s' % a.search) if a.search else (
                    'QQ 私聊可视化简报' if a.c2c else 'QQ 群聊可视化简报'
                )
            )
        ),
        'is_c2c': bool(a.c2c),
        'anonymized': bool(a.anonymize),
        'brief': bool(a.brief),
        'class_mode': bool(a.class_mode),
        'cutoff_str': fmt(cutoff, '%Y-%m-%d %H:%M:%S'),
        'built_str': fmt(built, '%Y-%m-%d %H:%M:%S'),
        'age_min': int((now - cutoff) / 60) if cutoff else -1,
        'scan_rows': str(scan_rows),
        'read_errors': str(errs),
        'src_mode': src_mode_label,
        'window_str': '%s ~ %s' % (fmt(t0, '%Y-%m-%d %H:%M'), fmt(t1, '%Y-%m-%d %H:%M')),
        'cur_n': cur_n,
        'prev_n': prev_n,
        'delta': cur_n - prev_n,
        'bad_days': [(scrub('%s@%s' % (g, d)) if a.anonymize else ('%s@%s' % (g, d))) for g, d in bad_days[:12]] if (bad_days and not a.c2c) else [],
        'state_lines': [(scrub(ln.lstrip('> ').strip()) if a.anonymize else ln.lstrip('> ').strip()) for ln in state_lines],
    }

    if a.ask:
        a_t0, label = parse_time_hint(a.ask, a.days or 7)
        if a.hours:
            a_t0 = now - int(a.hours * 3600); label = '近 %g 小时' % a.hours
        if a.days:
            a_t0 = now - int(a.days * 86400); label = '近 %g 天' % a.days
        cand = resolve_group(idx, a.ask, grp_table=grp_table)
        gcodes = None; gdesc = '全部私聊' if a.c2c else '全部群'
        if a.group:
            picked = []
            for s in a.group:
                for r in idx.execute('SELECT group_code, group_name FROM %s' % grp_table):
                    if str(r[0]) == s or (r[1] and s in r[1]):
                        picked.append(r[0])
            if picked:
                gcodes = list(dict.fromkeys(picked))
                gdesc = '、'.join(str(c) for c in gcodes)
        elif cand and cand[0][0] >= 5 and (len(cand) == 1 or cand[0][0] >= cand[1][0] + 2):
            gcodes = [cand[0][1]]; gdesc = cand[0][2]
        kws, known, scored, dropped, nwin, arows = ask(
            idx, a.ask, a_t0, t1, gcodes, msg_table=msg_table, grp_table=grp_table)
        meta_map = {k: v for k, v in idx.execute('SELECT key, value FROM meta')}
        built = int(meta_map.get('built_at') or 0)
        age = now - built if built else 0
        if any(k in a.ask for k in ('刚刚', '刚才', '现在', '今天')) and age > 600:
            out.append('> ⚠️ **索引是 %d 分钟前建的，不是实时的。**'
                       '你问的是「刚刚/今天」，最新消息可能还没进索引——'
                       '**先重跑 ntqq_build.py 再回答**，否则可能漏掉。' % (age // 60))
            out.append('')
        out.append('## 提问: %s' % a.ask)
        out.append('')
        out.append('- 解析出的时间范围: **%s**（%s ~ %s）' % (label, fmt(a_t0, '%m-%d %H:%M'), fmt(t1, '%m-%d %H:%M')))
        out.append('- 解析出的对象: **%s**' % gdesc if a.c2c else '- 解析出的群: **%s**' % gdesc)
        if cand:
            out.append('- 候选（分数）: %s' % '、'.join('%s(%d)' % (n, s) for s, c, n in cand))
        out.append('- 窗口内消息 %d 条，检索词 %d 个: %s' % (nwin, len(kws), ('、'.join(kws) if kws else '（无）')))
        if dropped:
            out.append('- 被当背景噪音剔除的高频词: %s' % '、'.join(dropped[:12]))
        if known:
            out.append('- 词库里认识的词: %s' % '、'.join(known))
        out.append('')
        strong = [x for x in scored if x[0] >= 0.45 and x[1] >= 2]
        if strong:
            out.append('**结论：是。命中 %d 条，其中强相关 %d 条。**' % (len(scored), len(strong)))
        elif scored:
            out.append('**结论：有沾边的 %d 条，但没有强相关的（可能不是你要问的那件事）。**' % len(scored))
        else:
            out.append('**结论：没有。**在解析出的时间范围和群里，**一条相关消息都没找到。**')
        out.append('')
        if gcodes and not strong:
            _, _, other, _, _, _ = ask(idx, a.ask, a_t0, t1, None, msg_table=msg_table, grp_table=grp_table)
            other = [x for x in other if x[3]['group_code'] not in gcodes and x[0] >= 0.30 and x[1] >= 2][:10]
            if other:
                out.append('> ⚠️ 但**其他群**里有 %d 条内容相近，可能是你记错群了：' % len(other))
                out.append('')
                for cov, kn, tsx, r, hit, _i in other[:10]:
                    t = (r['text'] or '').replace(chr(10), ' ⏎ ')
                    if len(t) > 180:
                        t = t[:180] + '…'
                    out.append('- **%s** [%s] %s (相关度 %.2f)' % (fmt(tsx, '%m-%d %H:%M'),
                                                               (r['group_name'] or r['group_code']), r['sender_name'], cov))
                    out.append('  > ' + t)
                out.append('')
        shown = (strong or scored)[:a.max_msgs]
        ask_items = []
        for cov, known_n, tsx, r, hit, idx_at in shown:
            out.append('- **%s** [%s] %s (相关度 %.2f) ｜命中 %s' % (fmt(tsx, '%m-%d %H:%M'),
                                                                 (r['group_name'] or r['group_code']),
                                                                 r['sender_name'], cov, '/'.join(hit[:6])))
            ask_items.append({
                'time': fmt(tsx, '%m-%d %H:%M'),
                'sender': '%s [%s]' % (r['sender_name'], r['group_name'] or r['group_code']),
                'kind': '相关度 %.2f' % cov,
                'text': (r['text'] or '')[:400],
                'is_notice': cov >= 0.45,
            })
            lo = max(0, idx_at - a.ctx)
            hi = min(len(arows), idx_at + a.ctx + 1)
            if a.ctx > 0 and (hi - lo) > 1:
                out.append('  > **上下文（前后各 %d 条，★ 为命中的那条）:**' % a.ctx)
                for j in range(lo, hi):
                    rr = arows[j]
                    tt = (rr['text'] or '').replace(chr(10), ' ⏎ ')
                    if len(tt) > 220:
                        tt = tt[:220] + '…'
                    if not tt:
                        continue
                    out.append('  > %s %s %s: %s' % ('★' if j == idx_at else '·',
                                                     fmt(rr['ts'], '%m-%d %H:%M'),
                                                     rr['sender_name'], tt))
            else:
                tt = (r['text'] or '').replace(chr(10), ' ⏎ ')
                out.append('  > ' + (tt[:260] + '…' if len(tt) > 260 else tt))
        dist = Counter(x[3]['group_name'] for x in scored)
        if dist:
            out.append('')
            out.append('- 命中分布: ' + ', '.join('%s=%d' % (g or '-', c) for g, c in dist.most_common(12)))
        result['ask'] = a.ask
        result['ask_hits'] = len(scored)
        text = chr(10).join(out)
        if a.anonymize:
            text = scrub(text)
        if md_out:
            with open(md_out, 'w', encoding='utf-8') as fh:
                fh.write(text)
            log('[out] %s' % md_out)
        elif not html_out:
            print(text)
        if html_out:
            from ntqq_html import build_generic_payload, render_html_report
            payload = build_generic_payload('ask', meta_info, text, extra={
                'summary_cards': [
                    {'label': '提问内容', 'value': a.ask, 'sub': '解析范围: ' + label},
                    {'label': '目标范围', 'value': gdesc, 'sub': '窗口内 %d 条消息' % nwin},
                    {'label': '命中结果', 'value': '%d 条' % len(scored), 'sub': '强相关 %d 条' % len(strong)},
                ],
                'items': ask_items,
            })
            html_str = render_html_report(payload)
            with open(html_out, 'w', encoding='utf-8') as fh:
                fh.write(html_str)
            log('[html] %s (%.1f KB)' % (html_out, len(html_str.encode('utf-8')) / 1024.0))
        return

    if a.media_list:
        mrows = idx.execute('SELECT * FROM media WHERE ts>=? AND ts<=? ORDER BY ts', (t0, t1)).fetchall()
        want = None
        if not a.group and a.class_mode:
            cfgp = os.path.join(APP, 'config.json')
            with open(cfgp, encoding='utf-8') as fh:
                want = set(int(x) for x in json.load(fh).get('class_groups', []))
        elif a.group:
            want = set()
            allg = [r[0] for r in idx.execute('SELECT group_code FROM groups')]
            for s in a.group:
                for gcode in allg:
                    if str(gcode) == s or s in str(gcode):
                        want.add(gcode)
        if want is not None:
            mrows = [m for m in mrows if m['group_code'] in want]
        T = chr(9)
        with open(a.media_list, 'w', encoding='utf-8') as fh:
            fh.write(T.join(['msg_id', 'ts', 'group_code', 'group_name', 'sender', 'md5', 'size']) + chr(10))
            for m in mrows:
                fh.write(T.join([str(m['msg_id']), str(m['ts']), str(m['group_code']),
                                 (m['group_name'] or ''), (m['sender_name'] or ''),
                                 (m['md5'] or ''), str(m['declared_size'] or 0)]) + chr(10))
        log('[media] %d 张候选 -> %s' % (len(mrows), a.media_list))
        return

    html_mode = 'digest'
    html_extra = {}
    prepared = []
    allrows = []
    ANON = {}
    cfg_class_groups = set()
    try:
        with open(os.path.join(APP, 'config.json'), encoding='utf-8') as fh:
            cfg_class_groups = set(int(x) for x in json.load(fh).get('class_groups', []) if str(x).isdigit())
    except Exception:
        cfg_class_groups = set()

    if a.search:
        html_mode = 'search'
        rows = search_messages(idx, a.search, t0, t1, msg_table=msg_table)
        conv = [r for r in rows if r['kind'] not in SYS_KINDS]
        out.append('## 关键词命中: %r —— %d 条（有效对话 %d 条）' % (a.search, len(rows), len(conv)))
        out.append('')
        names = {r['group_code']: r['group_name'] for r in rows}
        byg = Counter(r['group_code'] for r in rows)
        out.append('- 分布: ' + ', '.join('%s(%s)=%d' % (names.get(c) or '-', c, v) for c, v in byg.most_common(20)))
        out.append('')
        s_items = []
        for r in rows[:a.max_msgs]:
            g_disp = names.get(r['group_code']) or r['group_code']
            out.append('%s [%s] %s: %s' % (fmt(r['ts'], '%m-%d %H:%M'), g_disp,
                                           r['sender_name'], (r['text'] or '')[:200]))
            s_items.append({
                'time': fmt(r['ts'], '%m-%d %H:%M'),
                'sender': '%s [%s]' % (r['sender_name'], g_disp),
                'kind': r['kind'] or 'text',
                'text': (r['text'] or '')[:360],
                'is_notice': False,
            })
        result['n'] = len(rows)
        html_extra = {
            'summary_cards': [
                {'label': '检索关键词', 'value': a.search, 'sub': 'FTS5 全文 + 多词子串匹配'},
                {'label': '命中消息数', 'value': '%d 条' % len(rows), 'sub': '有效对话 %d 条' % len(conv)},
                {'label': '涉及群组数', 'value': '%d 个' % len(byg), 'sub': ''},
            ],
            'items': s_items,
        }
    elif a.member:
        html_mode = 'member'
        mres = member_report(idx, out, a.member, t0, t1,
                             group_spec=a.group, exact=a.exact, ctx=a.ctx,
                             max_lines=(a.max_msgs if msgs_given else 0),
                             all_matches=a.all_matches,
                             msg_table=msg_table, grp_table=grp_table)
        result['member'] = mres
        html_extra = {
            'summary_cards': [
                {'label': '目标群友', 'value': a.member, 'sub': '精确匹配' if a.exact else '昵称子串匹配'},
                {'label': '命中昵称数', 'value': '%d 个' % mres.get('matched', 0), 'sub': ''},
                {'label': '累计发言量', 'value': '%d 条' % mres.get('n', 0), 'sub': ''},
            ],
            'items': [],
        }
    else:
        if a.class_mode and not a.c2c:
            cg = sorted(cfg_class_groups)
            if not a.group and cg:
                a.group = [str(x) for x in cg]
        if a.group:
            # 显式点名的群尊重用户意图，不过滤
            allg = idx.execute('SELECT group_code, group_name FROM %s ORDER BY group_code' % grp_table).fetchall()
            targets = []
            for s in a.group:
                s = s.strip()
                for r in allg:
                    if str(r['group_code']) == s or (r['group_name'] and s in r['group_name']) or s in str(r['group_code']):
                        if (r['group_code'], r['group_name']) not in targets:
                            targets.append((r['group_code'], r['group_name']))
            if not targets:
                log('未找到匹配的对象，用 --list-groups 查看。'); return
        else:
            targets = [(r['group_code'], r['group_name']) for r in idx.execute(
                'SELECT group_code, group_name FROM %s WHERE last_ts>=? ORDER BY n DESC' % grp_table, (t0,))]

        ANON = build_anon(idx, set(c for c, _ in targets), msg_table=msg_table) if a.anonymize else {}
        if a.anonymize:
            for i, (c, _n) in enumerate(targets, 1):
                ANON_LABELS[c] = ('私聊%d' if a.c2c else '群%d') % i

        for code, name in targets:
            rows = idx.execute('SELECT * FROM %s WHERE group_code=? AND ts>=? AND ts<=? ORDER BY ts' % msg_table,
                               (code, t0, t1)).fetchall()
            if not rows:
                continue
            nconv = sum(1 for r in rows if r['kind'] not in SYS_KINDS)
            if nconv < a.min_conv:
                continue
            prev = idx.execute('SELECT COUNT(*) FROM %s WHERE group_code=? AND ts>=? AND ts<?' % msg_table,
                               (code, t0 - span, t0)).fetchone()[0]
            prepared.append((nconv, len(rows), code, name, rows, prev))
        prepared.sort(key=lambda x: -x[0])
        out.append('- 有实际对话的%s **%d 个**' % ('联系人' if a.c2c else '群', len(prepared)))
        out.append('')
        if prepared:
            out.append('| %s | 有效对话 | 总消息 | 参与 | 最后一条 |' % ('联系人' if a.c2c else '群'))
            out.append('|---|---|---|---|---|')
            for nconv, ntot, code, name, rows, _ in prepared:
                np = len(set(r['sender_name'] for r in rows if r['sender_name'] not in ('系统', '未知')))
                disp_name = code_label(code) if a.anonymize else (name or '-')
                out.append('| %s (%s) | %d | %d | %d | %s |' % (disp_name, code_label(code), nconv, ntot, np,
                                                           fmt(max(r['ts'] for r in rows), '%m-%d %H:%M')))
            out.append('')
        if not a.no_focus and not a.c2c:
            for _, _, _, _, rows, _ in prepared:
                allrows.extend(rows)
            fb = focus_block(allrows, top=(60 if a.class_mode else 25), anon=ANON)
            if fb:
                out.append(fb)
                if a.class_mode:
                    out.append('> 上表只列「重要度 >= 2」的消息；下面是逐群详述。')
                    out.append('')
        for nconv, ntot, code, name, rows, prev in prepared:
            disp_name = code_label(code) if a.anonymize else name
            txt, _ = block(code, disp_name, rows, a.max_msgs, a.show_media, prev, a.brief, anon=ANON.get(code))
            out.append(txt)
            result['groups'].append({'code': code_label(code) if a.anonymize else code,
                                     'name': disp_name, 'conv': nconv, 'n': ntot,
                                     'first': min(r['ts'] for r in rows), 'last': max(r['ts'] for r in rows)})

    text = '\n'.join(out)
    if a.anonymize:
        text = scrub(text)
    if len(text) > a.max_chars:
        text = text[:a.max_chars] + '\n\n...[已按 --max-chars 截断]\n'
    if md_out:
        with open(md_out, 'w', encoding='utf-8') as f:
            f.write(text)
        log('[out] %s (%d 字符, %d 行)' % (md_out, len(text), text.count(chr(10))))
    elif not html_out:
        print(text)

    if html_out:
        try:
            from ntqq_html import build_digest_payload, build_generic_payload, render_html_report
            if html_mode == 'digest':
                payload = build_digest_payload(
                    meta_info=meta_info,
                    prepared=prepared,
                    focus_rows=allrows,
                    fmt_fn=fmt,
                    kw_fn=keywords,
                    anon_map=ANON,
                    anon_unknown_fn=anon_unknown,
                    mask_names_fn=mask_names,
                    code_label_fn=code_label,
                    scrub_fn=scrub,
                    class_groups=cfg_class_groups,
                    max_msgs=a.max_msgs,
                    show_media=a.show_media,
                    brief=a.brief,
                    markdown_text=text,
                )
            else:
                payload = build_generic_payload(html_mode, meta_info, text, extra=html_extra)
            html_str = render_html_report(payload)
            with open(html_out, 'w', encoding='utf-8') as f:
                f.write(html_str)
            log('[html] %s (%.1f KB)' % (html_out, len(html_str.encode('utf-8')) / 1024.0))
        except Exception as e:
            log_warn('[html] 生成可视化 HTML 简报失败: %r' % e)

    if a.json:
        result['text'] = text
        if html_out:
            result['html'] = html_out
        with open(a.json, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)


if __name__ == '__main__':
    main()
