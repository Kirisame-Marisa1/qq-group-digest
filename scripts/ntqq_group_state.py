#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""群变更登记 —— 自动识别「新入群 / 已退群 / 退群后回归 / 群改名」。

只做一件事：拿本机 `group_info.db` 的当前群列表与上一次的登记表比对，报出差量。
**不猜任何服务端状态。** 早期版本试过推断「收进群助手」，排查结论是那个状态
客户端根本不落盘（group_list / group_detail_info_ver1 / group_ext_list 逐列看过、
hidden_session_storage_table_v1 与 service_assistant_contact 是空表、
nt_qq 下六万多个文件搜关键词零命中），做不成自动识别，**该功能已整体移除**。

产物两个，都在 run\ 下：
  group_registry.json  长期登记表：每个群一条记录（首见时间、是否在群、退群时间、名字）
  group_snapshot.json  每次扫描的现状快照（便于人工核对与回滚）
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ntqq_core import PLAIN, RUN, TZ, ensure_dirs, load_schema, open_ro, log, fmt  # noqa: E402

REGISTRY = os.path.join(RUN, 'group_registry.json')
SNAPSHOT = os.path.join(RUN, 'group_snapshot.json')


def read_live_groups():
    """读当前「已加入的群」。返回 {群号int: {...}}；库不存在时返回 None。"""
    p = os.path.join(PLAIN, 'group_info.db')
    if not os.path.exists(p):
        return None
    sc = load_schema()
    ns = sc['name_sources']['group_list']
    gl = sc['name_sources']['group_detail']
    con = open_ro(p)
    live = {}
    try:
        sql = 'SELECT "%s","%s" FROM "%s"' % (ns['code'], ns['name'], ns['table'])
        for code, name in con.execute(sql):
            if not code:
                continue
            live[int(code)] = {'name': (name or '').strip(),
                               'in_member_list': True, 'in_detail': False,
                               'last_msg_ts': 0, 'msgs_30d': 0}
    except sqlite3.DatabaseError as e:
        log('[warn] 读 group_list 失败: %s' % e)
    try:
        sql = 'SELECT "%s","%s" FROM "%s"' % (gl['code'], gl['name'], gl['table'])
        for code, name in con.execute(sql):
            if not code:
                continue
            c = int(code)
            if c in live:
                live[c]['in_detail'] = True
                if not live[c]['name'] and name:
                    live[c]['name'] = str(name).strip()
            else:
                # 只在 detail 表里、不在 group_list：历史群（多半已退）
                live[c] = {'name': (name or '').strip(), 'in_member_list': False,
                           'in_detail': True, 'last_msg_ts': 0, 'msgs_30d': 0}
    except sqlite3.DatabaseError as e:
        log('[warn] 读 group_detail_info_ver1 失败: %s' % e)
    con.close()
    return live


def attach_activity(live, window_days=30):
    """从滚动索引补「最后一条消息时间 / 窗口内条数」。索引不存在则跳过。"""
    idxp = os.path.join(os.path.dirname(PLAIN), 'index.db')
    if not os.path.exists(idxp) or live is None:
        return live
    try:
        con = open_ro(idxp)
        for code, n, last in con.execute('SELECT group_code, n, last_ts FROM groups'):
            c = int(code)
            if c in live:
                live[c]['msgs_30d'] = int(n or 0)
                live[c]['last_msg_ts'] = int(last or 0)
        con.close()
    except sqlite3.DatabaseError as e:
        log('[warn] 读索引补活动度失败: %s' % e)
    return live


def load_registry():
    try:
        with open(REGISTRY, encoding='utf-8') as fh:
            data = json.load(fh)
    except Exception:
        data = {}
    data.setdefault('version', 1)
    data.setdefault('groups', {})
    data.setdefault('last_scan_ts', 0)
    if not isinstance(data.get('groups'), dict):
        data['groups'] = {}
    return data


def save_registry(reg):
    ensure_dirs()
    tmp = REGISTRY + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(reg, fh, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, REGISTRY)


def scan(persist=True, window_days=30):
    """读本地库 -> 与登记表比对 -> 返回本次差量。"""
    now = int(time.time())
    live = read_live_groups()
    if live is None:
        return {'ok': False, 'reason': 'group_info.db 不在 %s，先跑 ntqq_build.py 解密' % PLAIN}
    attach_activity(live, window_days)
    reg = load_registry()
    R = reg['groups']
    first_ever = not R

    res = {'ok': True, 'live': live, 'new': [], 'left': [], 'returned': [],
           'renamed': [], 'now': now, 'first_ever': first_ever,
           'suspect_snapshot': False}

    for code, info in sorted(live.items()):
        key = str(code)
        rec = R.get(key)
        if rec is None:
            rec = {'code': code, 'name': info['name'], 'first_seen_ts': now,
                   'in_member_list': info['in_member_list'], 'left_ts': 0,
                   'last_live_ts': now}
            R[key] = rec
            if info['in_member_list'] and not first_ever:
                # 首次建基线时不算「新入群」，否则一上来就把已有群全报一遍
                res['new'].append(dict(rec, info=info))
            elif not info['in_member_list']:
                # 只在 detail 表里的历史群，不算「新加群」
                rec['left_ts'] = rec['left_ts'] or now
            continue

        was_in = bool(rec.get('in_member_list'))
        rec['code'] = code
        rec['last_live_ts'] = now
        if info['name'] and info['name'] != rec.get('name'):
            old = rec.get('name')
            rec['name'] = info['name']
            if old:
                res['renamed'].append((code, old, info['name']))

        if info['in_member_list']:
            if not was_in:
                rec['left_ts'] = 0
                res['returned'].append(dict(rec, info=info))
            rec['in_member_list'] = True
        else:
            if was_in:
                rec['in_member_list'] = False
                rec['left_ts'] = now
                res['left'].append(dict(rec, info=info))

    # 本次本地库里完全不见了的群（两张表都没有）——「退群」最强的一条证据。
    # 防呆：一次扫描掉了一大半群，更可能是 group_info.db 读失败/快照损坏，
    #       这时不判退群（宁可漏报，也不要几百条假「退群」）。
    was_in = [r for r in R.values() if r.get('in_member_list')]
    suspect = bool(was_in) and len(live) < 0.5 * len(was_in)
    res['suspect_snapshot'] = suspect
    if suspect:
        log('[warn] 本次只读到 %d 个群，而登记表里有 %d 个在群 —— '
            '疑似 group_info.db 未读出，跳过退群判定' % (len(live), len(was_in)))
    else:
        for key, rec in R.items():
            if int(rec['code']) in live:
                continue
            if rec.get('in_member_list'):
                rec['in_member_list'] = False
                rec['left_ts'] = now
                res['left'].append(dict(rec, info={}))
            rec['last_absent_ts'] = now

    reg['last_scan_ts'] = now
    res['registry'] = reg
    if persist:
        save_registry(reg)
        snap = {'scanned_at': now,
                'scanned_at_str': fmt(now, '%Y-%m-%d %H:%M:%S'),
                'live_count': len(live),
                'in_group': sum(1 for r in R.values() if r.get('in_member_list')),
                'groups': {str(c): v for c, v in sorted(live.items())}}
        try:
            with open(SNAPSHOT, 'w', encoding='utf-8') as fh:
                json.dump(snap, fh, ensure_ascii=False, indent=2)
        except OSError as e:
            log('[warn] 写快照失败: %s' % e)
    return res


def resolve(reg, s):
    """按群号 / 群名子串找群，返回 [(code, rec)]。"""
    out = []
    s = s.strip()
    for key, rec in reg['groups'].items():
        if str(rec.get('code')) == s or (rec.get('name') and s in rec['name']) or s in str(key):
            out.append((int(rec['code']), rec))
    return out


def mark_left(reg, s):
    hits = resolve(reg, s)
    if not hits:
        return None
    now = int(time.time())
    for code, rec in hits:
        rec['in_member_list'] = False
        rec['left_ts'] = rec.get('left_ts') or now
    save_registry(reg)
    return hits


def summarize(res):
    L = []
    if not res.get('ok'):
        return ['⚠ 无法扫描：%s' % res.get('reason')]
    R = res['registry']['groups']
    L.append('数据截止 %s' % fmt(res['now'], '%Y-%m-%d %H:%M:%S'))
    if res.get('first_ever'):
        L.append('（本次为首次基线：只记录现状，不报「新入群」）')
    L.append('在群 %d 个；历史登记 %d 个（含已退）'
             % (sum(1 for r in R.values() if r.get('in_member_list')), len(R)))
    if res.get('suspect_snapshot'):
        L.append('⚠ 本次读到的群数异常偏少，已挂起退群判定（疑似 group_info.db 未读出）')
    if res['new']:
        L.append('')
        L.append('🆕 新入群 %d 个:' % len(res['new']))
        for r in res['new']:
            L.append('   %-11s %s' % (r['code'], r['name'] or '(无群名)'))
    if res['returned']:
        L.append('')
        L.append('↩ 退群后又回来 %d 个:' % len(res['returned']))
        for r in res['returned']:
            L.append('   %-11s %s' % (r['code'], r['name'] or '(无群名)'))
    if res['left']:
        L.append('')
        L.append('🚪 本次判定已退群 %d 个:' % len(res['left']))
        for r in res['left']:
            L.append('   %-11s %s' % (r['code'], r['name'] or '(无群名)'))
    if res['renamed']:
        L.append('')
        L.append('✏ 群名变更 %d 个:' % len(res['renamed']))
        for code, old, new in res['renamed']:
            L.append('   %-11s %s → %s' % (code, old, new))
    return L


def main():
    ap = argparse.ArgumentParser(description='QQ 群变更登记（新入群 / 已退群 / 改名）')
    ap.add_argument('--scan', action='store_true', help='扫描本地库并与登记表比对')
    ap.add_argument('--list', action='store_true', help='列出登记表里的全部群')
    ap.add_argument('--in-group', action='store_true', help='配合 --list：只看当前在群的')
    ap.add_argument('--left', action='append', default=[], help='把某个群手工标记为已退群（群号或群名子串）')
    a = ap.parse_args()

    if a.left:
        reg = load_registry()
        for who in a.left:
            hits = mark_left(reg, who)
            if not hits:
                print('没找到群: %s' % who)
                continue
            for code, rec in hits:
                print('%-11s %s -> 已退群' % (code, rec.get('name') or ''))
        return 0

    if a.list:
        reg = load_registry()
        rows = sorted(reg['groups'].values(),
                      key=lambda r: (not r.get('in_member_list'), -int(r.get('last_live_ts') or 0)))
        for r in rows:
            if a.in_group and not r.get('in_member_list'):
                continue
            print('  %-11s %-40s %s' % (r['code'], (r.get('name') or '(无群名)')[:40],
                                        '在群' if r.get('in_member_list') else '不在成员列表'))
        return 0

    res = scan()
    for line in summarize(res):
        print(line)
    return 0 if res.get('ok') else 1


if __name__ == '__main__':
    sys.exit(main())
