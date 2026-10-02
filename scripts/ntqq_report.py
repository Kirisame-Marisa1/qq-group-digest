#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 cached/index.db 生成报告：单群 / 多群汇总 / 关键词检索。

用法示例：
  ntqq_report.py --active --hours 6 --brief
  ntqq_report.py --group 123456789 --hours 48
  ntqq_report.py --search 东方 --days 7
"""
import argparse, json, os, re, sqlite3, sys
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ntqq_core import open_index, now_ts, fmt, parse_when, log, APP, OUTPUT, ensure_dirs

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


def focus_block(rows, title='重点速览（通知 / 公告 / 长文 / 转发）', top=40):
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
        L.append('- **%s** [%s] %s (%s)' % (fmt(ts, '%m-%d %H:%M'), r['group_name'] or r['group_code'],
                                            r['sender_name'], '重要度 %d' % s))
        L.append('  > ' + t)
    L.append('')
    return chr(10).join(L) + chr(10)


def block(code, name, rows, max_msgs, show_media=False, prev_n=None, brief=False):
    conv = [r for r in rows if r['kind'] not in SYS_KINDS]
    ts = [r['ts'] for r in rows]
    if not ts:
        return '', 0
    t0, t1 = min(ts), max(ts)
    people = Counter(r['sender_name'] for r in conv if r['sender_name'] not in (None, '', '未知', '系统'))
    day = Counter(r['day'] for r in rows)
    hour = Counter(r['hour'] for r in rows if r['hour'] >= 0)
    bucket = set(r['ts'] // 600 for r in rows)
    kinds = Counter(r['kind'] for r in conv)
    ats = Counter()
    for r in conv:
        for m in re.findall(r'@([^\s@|]{1,16})', r['text'] or ''):
            ats[m] += 1
    L = []
    L.append('## %s (%s)' % (name or '未知群', code))
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
    kws = keywords([r['text'] for r in conv if r['text']])
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
            t = (r['text'] or '').replace('\n', ' ⏎ ')
            if len(t) > 300:
                t = t[:300] + '…'
            if not t:
                continue
            if r['kind'] != 'text':
                t = '[' + r['kind'] + '] ' + t
            L.append('%s %s: %s' % (fmt(r['ts'], '%m-%d %H:%M'), r['sender_name'], t))
        L.append('')
    return '\n'.join(L), len(conv)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--active', action='store_true')
    ap.add_argument('--group', action='append', default=[])
    ap.add_argument('--search', default=None)
    ap.add_argument('--list-groups', action='store_true')
    ap.add_argument('--since'); ap.add_argument('--until')
    ap.add_argument('--hours', type=float); ap.add_argument('--days', type=float)
    ap.add_argument('--max-msgs', type=int, default=150)
    ap.add_argument('--max-chars', type=int, default=200000)
    ap.add_argument('--min-conv', type=int, default=1)
    ap.add_argument('--show-media', action='store_true', help='正文里也列出图片/表情/语音')
    ap.add_argument('--brief', action='store_true', help='每群只输出最近的原文，适合多群速览')
    ap.add_argument('--out', default=None, help='把报告写入文件')
    ap.add_argument('--json', default=None)
    ap.add_argument('--class-mode', action='store_true', help='班群模式：重点速览 + 不限篇幅逐条')
    ap.add_argument('--no-focus', action='store_true', help='不输出重点速览')
    ap.add_argument('--media-list', default=None, help='把窗口内图片候选写成 TSV')
    a = ap.parse_args()

    idx = open_index(); idx.row_factory = sqlite3.Row
    def meta(k, d='0'):
        r = idx.execute('SELECT value FROM meta WHERE key=?', (k,)).fetchone()
        return r[0] if r else d
    cutoff = int(meta('cutoff_ts') or 0)
    built = int(meta('built_at') or 0)
    scan_rows = meta('scan_rows'); errs = meta('read_errors')
    bad_days = json.loads(meta('bad_days', '[]') or '[]')

    if a.list_groups:
        for g in idx.execute('SELECT * FROM groups ORDER BY n DESC'):
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
    out.append('# QQ 群聊报告')
    out.append('')
    out.append('> **数据截止 %s**（索引构建于 %s，距现在 %d 分钟；本次扫描 %s 行，读取错误 %s 次）'
               % (fmt(cutoff, '%Y-%m-%d %H:%M:%S'), fmt(built, '%Y-%m-%d %H:%M:%S'),
                  int((now - cutoff) / 60) if cutoff else -1, scan_rows, errs))
    if bad_days:
        out.append('> ⚠ 数据缺口：有 %d 个「群-日」读取失败被跳过：%s'
                   % (len(bad_days), ', '.join('%s@%s' % (g, d) for g, d in bad_days[:12])))
    out.append('')
    out.append('- 查询窗口: %s ~ %s' % (fmt(t0, '%Y-%m-%d %H:%M'), fmt(t1, '%Y-%m-%d %H:%M')))
    cur_n = idx.execute('SELECT COUNT(*) FROM messages WHERE ts>=? AND ts<=?', (t0, t1)).fetchone()[0]
    prev_n = idx.execute('SELECT COUNT(*) FROM messages WHERE ts>=? AND ts<?', (t0 - span, t0)).fetchone()[0]
    out.append('- 窗口内总消息 %d 条；上一个等长窗口 %d 条；趋势 %s（%+d）'
               % (cur_n, prev_n, '↑' if cur_n > prev_n else ('↓' if cur_n < prev_n else '→'), cur_n - prev_n))
    out.append('')

    result = {'cutoff_ts': cutoff, 'window': [t0, t1], 'groups': []}

    if a.media_list:
        mrows = idx.execute('SELECT * FROM media WHERE ts>=? AND ts<=? ORDER BY ts', (t0, t1)).fetchall()
        want = None
        if not a.group and a.class_mode:
            cfgp = os.path.join(APP, 'config.json')
            want = set(int(x) for x in json.load(open(cfgp, encoding='utf-8')).get('class_groups', []))
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

    if a.search:
        rows = idx.execute('SELECT * FROM messages WHERE ts>=? AND ts<=? AND text LIKE ? ORDER BY ts',
                           (t0, t1, '%' + a.search + '%')).fetchall()
        conv = [r for r in rows if r['kind'] not in SYS_KINDS]
        out.append('## 关键词命中: %r —— %d 条（有效对话 %d 条）' % (a.search, len(rows), len(conv)))
        out.append('')
        names = {r['group_code']: r['group_name'] for r in rows}
        byg = Counter(r['group_code'] for r in rows)
        out.append('- 分布: ' + ', '.join('%s(%s)=%d' % (names.get(c) or '-', c, v) for c, v in byg.most_common(20)))
        out.append('')
        for r in rows[:a.max_msgs]:
            out.append('%s [%s] %s: %s' % (fmt(r['ts'], '%m-%d %H:%M'), names.get(r['group_code']) or r['group_code'],
                                           r['sender_name'], (r['text'] or '')[:200]))
        result['n'] = len(rows)
    else:
        if a.class_mode:
            cfg = json.load(open(os.path.join(APP, 'config.json'), encoding='utf-8'))
            cg = [int(x) for x in cfg.get('class_groups', [])]
            if not a.group and cg:
                a.group = [str(x) for x in cg]
        if a.group:
            allg = idx.execute('SELECT group_code, group_name FROM groups ORDER BY group_code').fetchall()
            targets = []
            for s in a.group:
                s = s.strip()
                for r in allg:
                    if str(r['group_code']) == s or (r['group_name'] and s in r['group_name']) or s in str(r['group_code']):
                        if (r['group_code'], r['group_name']) not in targets:
                            targets.append((r['group_code'], r['group_name']))
            if not targets:
                log('未找到匹配的群，用 --list-groups 查看。'); return
        else:
            targets = [(r['group_code'], r['group_name']) for r in idx.execute(
                'SELECT group_code, group_name FROM groups WHERE last_ts>=? ORDER BY n DESC', (t0,))]

        prepared = []
        for code, name in targets:
            rows = idx.execute('SELECT * FROM messages WHERE group_code=? AND ts>=? AND ts<=? ORDER BY ts',
                               (code, t0, t1)).fetchall()
            if not rows:
                continue
            nconv = sum(1 for r in rows if r['kind'] not in SYS_KINDS)
            if nconv < a.min_conv:
                continue
            prev = idx.execute('SELECT COUNT(*) FROM messages WHERE group_code=? AND ts>=? AND ts<?',
                               (code, t0 - span, t0)).fetchone()[0]
            prepared.append((nconv, len(rows), code, name, rows, prev))
        prepared.sort(key=lambda x: -x[0])
        out.append('- 有实际对话的群 **%d 个**' % len(prepared))
        out.append('')
        if prepared:
            out.append('| 群 | 有效对话 | 总消息 | 参与 | 最后一条 |')
            out.append('|---|---|---|---|---|')
            for nconv, ntot, code, name, rows, _ in prepared:
                np = len(set(r['sender_name'] for r in rows if r['sender_name'] not in ('系统', '未知')))
                out.append('| %s (%s) | %d | %d | %d | %s |' % (name or '-', code, nconv, ntot, np,
                                                           fmt(max(r['ts'] for r in rows), '%m-%d %H:%M')))
            out.append('')
        if not a.no_focus:
            allrows = []
            for _, _, _, _, rows, _ in prepared:
                allrows.extend(rows)
            fb = focus_block(allrows, top=(60 if a.class_mode else 25))
            if fb:
                out.append(fb)
                if a.class_mode:
                    out.append('> 上表只列「重要度 >= 2」的消息；下面是逐群详述。')
                    out.append('')
        for nconv, ntot, code, name, rows, prev in prepared:
            txt, _ = block(code, name, rows, a.max_msgs, a.show_media, prev, a.brief)
            out.append(txt)
            result['groups'].append({'code': code, 'name': name, 'conv': nconv, 'n': ntot,
                                     'first': min(r['ts'] for r in rows), 'last': max(r['ts'] for r in rows)})

    text = '\n'.join(out)
    if len(text) > a.max_chars:
        text = text[:a.max_chars] + '\n\n...[已按 --max-chars 截断]\n'
    if a.out:
        with open(a.out, 'w', encoding='utf-8') as f:
            f.write(text)
        log('[out] %s (%d 字符, %d 行)' % (a.out, len(text), text.count(chr(10))))
    else:
        print(text)
    if a.json:
        result['text'] = text
        with open(a.json, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)


if __name__ == '__main__':
    main()
