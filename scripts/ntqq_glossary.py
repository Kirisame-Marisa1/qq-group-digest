#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""群聊关键词库：扫描候选词 -> 查使用场景 -> 存释义 -> 渲染成可读文档。

用法：
  ntqq_glossary.py --scan --days 60 --top 80        找候选词（群内黑话 / 网络流行语）
  ntqq_glossary.py --context X --limit 15      看某个词在历史里怎么用的
  ntqq_glossary.py --apply draft.json               把释义合并进库
  ntqq_glossary.py --render                         重新生成 关键词库.md
  ntqq_glossary.py --list                           已收录词
"""
import argparse, json, os, re, sys, time
from collections import Counter, defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ntqq_core import open_index, ensure_dirs, GLOSSARY, now_ts, fmt, log

STORE = os.path.join(GLOSSARY, 'terms.json')
DOC = os.path.join(GLOSSARY, '关键词库.md')

NOISE = set('的了是我你他她它们这那有和就不都也还很太吧呢吗啊哦嗯么什怎为以所以但而且如果因为可以没有一个是 在会要能对上下来去个之与及等把被让给')
HEXONLY = re.compile(r'^[0-9A-Fa-f]{2,8}[A-Za-z0-9]{0,4}$')
FILELIKE = re.compile(r'\.(jpg|jpeg|png|gif|webp|bmp|mp4|mp3|amr|silk|zip|rar|7z|pdf|docx?|xlsx?|txt|doc)$', re.I)
COMMON = set('这个 那个 什么 怎么 就是 不是 可以 没有 我们 你们 他们 一个 现在 知道 因为 所以 但是 然后 还是 已经 时候 感觉 真的 应该 可能 一样 出来 起来 一下 这样 那样 自己 大家 同学 老师 今天 明天 昨天 早上 晚上 上午 下午 东西 事情 问题 时间 地方 有点 表示 直接 完全 突然'.split())


def load_store():
    if os.path.exists(STORE):
        try:
            return json.load(open(STORE, encoding='utf-8'))
        except Exception:
            pass
    return {'terms': {}, 'meta': {}}


def save_store(st):
    ensure_dirs()
    st.setdefault('meta', {})['updated'] = time.strftime('%Y-%m-%d %H:%M:%S')
    with open(STORE, 'w', encoding='utf-8') as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def iter_texts(days, groups=None):
    idx = open_index(); idx.row_factory = __import__('sqlite3').Row
    cut = now_ts() - int(days * 86400)
    if groups:
        q = 'SELECT ts,group_code,group_name,sender_name,text FROM messages WHERE ts>=? AND group_code IN (%s)' % ','.join('?' * len(groups))
        rows = idx.execute(q, [cut] + list(groups))
    else:
        rows = idx.execute('SELECT ts,group_code,group_name,sender_name,text FROM messages WHERE ts>=?', (cut,))
    for r in rows:
        t = re.sub(r'\[转发聊天记录[^\]]*\]', ' ', r['text'] or '')
        if t.strip():
            yield r['ts'], r['group_code'], r['group_name'], r['sender_name'], t


def ngrams(text, nmin=2, nmax=5):
    for run in re.findall(r'[\u4e00-\u9fffA-Za-z0-9]{2,}', text):
        if FILELIKE.search(run):
            continue
        L = len(run)
        for n in range(nmin, min(nmax, L) + 1):
            for i in range(L - n + 1):
                g = run[i:i + n]
                if g in COMMON:
                    continue
                if all(c in NOISE for c in g):
                    continue
                has_cjk = any('\u4e00' <= c <= '\u9fff' for c in g)
                if not has_cjk:
                    if HEXONLY.match(g) or g.isdigit() or len(g) < 3:
                        continue
                yield g


def scan(days, top, min_count, groups=None):
    gcnt = defaultdict(Counter)
    first = {}; last = {}
    for ts, gc, gn, sn, txt in iter_texts(days, groups):
        seen = set()
        for g in ngrams(txt):
            if g in seen:
                continue
            seen.add(g)
            gcnt[g][gc] += 1
            if g not in first or ts < first[g]:
                first[g] = ts
            if g not in last or ts > last[g]:
                last[g] = ts
    out = []
    for g, c in gcnt.items():
        total = sum(c.values())
        if total < min_count:
            continue
        if len(g) == 2 and total < min_count * 3:
            continue
        top_g, top_n = c.most_common(1)[0]
        out.append({'term': g, 'total': total, 'groups': len(c), 'top_group': top_g,
                    'top_n': top_n, 'spec': round(top_n / float(total), 3),
                    'first': first[g], 'last': last[g]})
    out.sort(key=lambda x: (-x['total'], -len(x['term'])))
    keep = []
    for it in out:
        t = it['term']
        if any(t in k['term'] and k['total'] >= it['total'] for k in keep):
            continue
        keep.append(it)
        if len(keep) >= top * 3:
            break
    return keep


def show_scan(keep, top):
    idx = open_index()
    names = {r[0]: r[1] for r in idx.execute('SELECT group_code, MAX(group_name) FROM messages GROUP BY group_code')}
    spec = [x for x in keep if x['spec'] >= 0.7 and x['total'] >= 8]
    glob = [x for x in keep if x['spec'] < 0.7 and x['groups'] >= 5 and x['total'] >= 30]
    print('=' * 78)
    print('A. 群内黑话候选（高频集中在一个群）  %d 个' % len(spec))
    print('=' * 78)
    for x in spec[:top]:
        print('  %-14s 共%4d次  %-22s 占%3d%%  首见 %s' % (
            x['term'], x['total'], (names.get(x['top_group']) or str(x['top_group']))[:22],
            int(x['spec'] * 100), fmt(x['first'], '%m-%d')))
    print()
    print('=' * 78)
    print('B. 跨群通用/网络流行语候选  %d 个' % len(glob))
    print('=' * 78)
    for x in glob[:top]:
        print('  %-14s 共%4d次  出现在%2d个群  首见 %s' % (x['term'], x['total'], x['groups'], fmt(x['first'], '%m-%d')))


def context(term, limit, days=90, groups=None):
    idx = open_index(); idx.row_factory = __import__('sqlite3').Row
    cut = now_ts() - int(days * 86400)
    if groups:
        q = ("SELECT ts,group_name,sender_name,text FROM messages WHERE ts>=? AND text LIKE ? AND group_code IN (%s) ORDER BY ts"
             % ','.join('?' * len(groups)))
        rows = idx.execute(q, [cut, '%' + term + '%'] + list(groups)).fetchall()
    else:
        rows = idx.execute("SELECT ts,group_name,sender_name,text FROM messages WHERE ts>=? AND text LIKE ? ORDER BY ts",
                           (cut, '%' + term + '%')).fetchall()
    print('=== 「%s」近 %d 天出现 %d 次，前 %d 条 ===' % (term, days, len(rows), limit))
    from collections import Counter
    print('  按群分布: ' + ', '.join('%s=%d' % (g or '-', c) for g, c in Counter(r['group_name'] for r in rows).most_common(8)))
    for r in rows[:limit]:
        t = (r['text'] or '').replace(chr(10), ' ')
        i = t.find(term)
        print('  %s [%s] %s: ...%s...' % (fmt(r['ts'], '%m-%d %H:%M'), (r['group_name'] or '')[:14], r['sender_name'], t[max(0, i - 55): i + 75]))
    if not rows:
        print('  （没有命中）')


def apply_draft(path):
    st = load_store()
    draft = json.load(open(path, encoding='utf-8'))
    n = 0
    for e in draft:
        k = e['term']
        old = st['terms'].get(k, {})
        old.update(e)
        old['updated'] = time.strftime('%Y-%m-%d')
        st['terms'][k] = old
        n += 1
    save_store(st)
    log('[glossary] 合并 %d 条 -> %s' % (n, STORE))
    render()


def render():
    st = load_store()
    terms = st['terms']
    by = defaultdict(list)
    for k, v in terms.items():
        by[v.get('type', '未分类')].append((k, v))
    L = ['# 群聊关键词库', '', '> 自动维护。新增词直接追加。',
         '> 更新时间：%s ｜ 收录 %d 条' % (st.get('meta', {}).get('updated', '-'), len(terms)), '']
    for typ in sorted(by):
        L.append('## %s（%d）' % (typ, len(by[typ])))
        L.append('')
        for k, v in sorted(by[typ], key=lambda x: -(x[1].get('total') or 0)):
            multi = bool(v.get('meanings'))
            L.append('### %s%s' % (k, '   ⚠️ 一词多义' if multi else ''))
            if multi:
                L.append('> 这个词在不同群里意思不同，**必须结合所在群和上下文判断**。')
                L.append('')
                for m in v['meanings']:
                    L.append('- **[%s]** %s' % (m.get('scope', '通用'), m.get('meaning', '')))
                    if m.get('origin'):
                        L.append('  - 来源：%s' % m['origin'])
                    if m.get('groups'):
                        L.append('  - 出现群：%s' % '、'.join(str(g) for g in m['groups']))
                    for e in (m.get('evidence') or [])[:4]:
                        L.append('  - 例：%s' % e)
                    L.append('')
            else:
                L.append('- 含义：%s' % (v.get('meaning') or '（待补）'))
                if v.get('origin'):
                    L.append('- 来源/考证：%s' % v['origin'])
                if v.get('groups'):
                    L.append('- 出现群：%s' % '、'.join(str(g) for g in v['groups']))
                if v.get('evidence'):
                    L.append('- 用法举例：')
                    for e in v['evidence'][:4]:
                        L.append('  - %s' % e)
                L.append('')
    ensure_dirs()
    with open(DOC, 'w', encoding='utf-8') as f:
        f.write(chr(10).join(L))
    log('[glossary] 文档 -> %s' % DOC)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--scan', action='store_true')
    ap.add_argument('--days', type=float, default=60)
    ap.add_argument('--top', type=int, default=60)
    ap.add_argument('--min-count', type=int, default=6)
    ap.add_argument('--groups', default=None)
    ap.add_argument('--context', default=None)
    ap.add_argument('--limit', type=int, default=15)
    ap.add_argument('--apply', default=None)
    ap.add_argument('--render', action='store_true')
    ap.add_argument('--list', action='store_true')
    a = ap.parse_args()
    ensure_dirs()
    if a.apply:
        apply_draft(a.apply); return
    if a.render:
        render(); return
    if a.context:
        gs2 = [int(x) for x in a.groups.split(',')] if a.groups else None
        context(a.context, a.limit, a.days, gs2); return
    if a.list:
        st = load_store()
        for k, v in sorted(st['terms'].items()):
            print('  %-16s %-8s %s' % (k, v.get('type', '?'), (v.get('meaning') or '')[:70]))
        print('共 %d 条' % len(st['terms'])); return
    gs = [int(x) for x in a.groups.split(',')] if a.groups else None
    keep = scan(a.days, a.top, a.min_count, gs)
    show_scan(keep, a.top)


if __name__ == '__main__':
    main()
