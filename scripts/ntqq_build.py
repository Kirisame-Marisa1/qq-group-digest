#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""建立 / 刷新滚动索引 cached/index.db。

流程：取密钥 -> 解密(按需) -> schema 版本漂移检查 -> 逐群按 (群,日期) 索引抽取 -> 写入索引。

重要：nt_msg.db 里 rowid(40001) 与时间不是严格同序（实测同一时刻的消息 rowid 可相差上千万亿），
      所以不能靠 rowid 倒序扫描，必须用 group_msg_table_idx40027_40058 索引按「群 + 日期」取。
"""
import argparse, json, os, sqlite3, sys, time
from datetime import datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ntqq_core import (load_config, load_schema, find_schema, ensure_dirs, ensure_keymap,
                       ensure_plain, open_ro, build_names, open_index, decode_body,
                       decode_forward, format_forward, pb_fields,
                       now_ts, local, fmt, TZ, RUN, PLAIN, CACHE, log)


def flush_media(idx, mbuf):
    if mbuf:
        idx.executemany('INSERT OR REPLACE INTO media VALUES (%s)' % ','.join('?' * 8), mbuf)
        mbuf.clear()

KEYS = ['seq', 'direction', 'sender_uid', 'group_code_str', 'group_key',
        'sender_qq', 'time', 'card', 'nick', 'msg_type', 'subtype', 'body', 'reply_seq', 'forward']


def group_codes(sc):
    """候选群号：group_info 里的群 + 最近会话里的群。"""
    codes = set()
    gi = os.path.join(PLAIN, 'group_info.db')
    if os.path.exists(gi):
        con = open_ro(gi)
        for t in ('group_list', 'group_detail_info_ver1'):
            try:
                for (c,) in con.execute('SELECT "60001" FROM "%s"' % t):
                    if c:
                        codes.add(int(c))
            except sqlite3.DatabaseError:
                pass
        con.close()
    nm = os.path.join(PLAIN, 'nt_msg.db')
    if os.path.exists(nm):
        con = open_ro(nm)
        try:
            for (c,) in con.execute('SELECT DISTINCT "40027" FROM recent_contact_v3_table'):
                if c:
                    codes.add(int(c))
        except sqlite3.DatabaseError:
            pass
        con.close()
    return sorted(codes)


def schema_gate(con, sc, schema_path):
    from ntqq_core import live_structure
    live = live_structure(con)
    recorded = sc.get('structure')
    if not recorded:
        sc['structure'] = live
        with open(schema_path, 'w', encoding='utf-8') as f:
            json.dump(sc, f, indent=2, ensure_ascii=False)
        log('[schema] 首次记录库结构（%d 张表）' % len(live))
        return True, []
    problems = []
    for t, cols in recorded.items():
        if t not in live:
            problems.append('表缺失: %s' % t)
            continue
        miss = [c for c in cols if c not in live[t]]
        if miss:
            problems.append('表 %s 缺列: %s' % (t, ','.join(miss)))
    for t in live:
        if t not in recorded:
            problems.append('新增表: %s' % t)
    if not problems:
        return True, []
    rep = os.path.join(RUN, 'schema_report.md')
    with open(rep, 'w', encoding='utf-8') as f:
        f.write('# Schema 漂移报告\n\n基线版本: %s\n检测时间: %s\n\n'
                % (sc.get('qq_version'), time.strftime('%Y-%m-%d %H:%M:%S')))
        for p in problems:
            f.write('- %s\n' % p)
    return False, problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=None)
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--rebuild', action='store_true')
    ap.add_argument('--only-groups', default=None, help='逗号分隔的群号，仅重建这些群')
    a = ap.parse_args()

    t0 = time.time()
    cfg = load_config()
    ensure_dirs()
    sc = load_schema()
    schema_path = find_schema()
    G = sc['group_msg_table']
    days = a.days or cfg['index_days']
    cut = now_ts() - days * 86400
    day0 = int(datetime.fromtimestamp(cut, TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())

    km = ensure_keymap(cfg, force=a.force)
    for name in cfg['decrypt']:
        log('[decrypt] %s' % name)
        ensure_plain(cfg, km, name, force=a.force)

    # QQ 是活跃写入的库，解密快照可能正好落在一次 checkpoint 中间，读 sqlite_master 会报 malformed。
    # 这种情况重新解密一次即可，最多重试 3 轮。
    con = None
    for attempt in range(3):
        con = open_ro(os.path.join(PLAIN, 'nt_msg.db'))
        try:
            ok, probs = schema_gate(con, sc, schema_path)
            break
        except sqlite3.DatabaseError as e:
            try:
                con.close()
            except Exception:
                pass
            con = None
            log('[warn] 解密快照不一致（%s），重新解密后重试 %d/3' % (e, attempt + 1))
            ensure_plain(cfg, km, 'nt_msg.db', force=True)
    if con is None:
        raise SystemExit('连续 3 次解密后仍读不出库结构。QQ 可能正在大量写入，稍后重试即可。')
    if not ok:
        log('[schema] !! 检测到库结构变化，已写出 run/schema_report.md')
        for p in probs[:20]:
            log('   - %s' % p)
        log('[schema] 已停止。请按 SKILL.md 的「版本漂移重解析」流程处理后重跑。')
        con.close(); sys.exit(3)

    log('[names] 读取群名/昵称 ...')
    group_names, member_names, global_names = build_names(cfg)
    log('  群名 %d, 群成员名片 %d, 全局昵称 %d'
        % (len(group_names), len(member_names), len(global_names)))
    con.close()

    codes = group_codes(sc)
    if a.only_groups:
        want = set(int(x) for x in a.only_groups.split(','))
        codes = [c for c in codes if c in want]
    log('[index] 候选群 %d 个，窗口 %d 天（>= %s）' % (len(codes), days, fmt(day0, '%Y-%m-%d')))

    idx = open_index()
    if a.rebuild:
        idx.execute('DELETE FROM messages'); idx.execute('DELETE FROM groups'); idx.commit()

    I = {k: i + 1 for i, k in enumerate(KEYS)}
    G = dict(G)
    G['forward'] = '40900'          # 40900 不在 schema 的 group_msg_table 映射里，单独补
    cols = ','.join('"%s"' % G[k] for k in KEYS)
    PATHS = os.path.join(PLAIN, 'nt_msg.db')
    sql = ('SELECT rowid,%s FROM group_msg_table WHERE "%s"=? AND "%s">=? ORDER BY "%s"'
           % (cols, G['group_key'], G['day'], G['day']))
    sql_day = ('SELECT rowid,%s FROM group_msg_table WHERE "%s"=? AND "%s">=? AND "%s"<? ORDER BY "%s"'
               % (cols, G['group_key'], G['day'], G['day'], G['day']))

    con = open_ro(PATHS)
    buf = []
    mbuf = []
    n = 0
    nerr = 0
    ngroup = 0
    max_ts = 0
    bad_days = []
    byday = {}

    def run(sqltext, params):
        nonlocal con, nerr
        for attempt in (0, 1):
            try:
                return con.execute(sqltext, params).fetchall()
            except sqlite3.DatabaseError:
                nerr += 1
                try:
                    con.close()
                except Exception:
                    pass
                con = open_ro(PATHS)
        return None

    def take(g, rows):
        """把某群取到的行写入缓冲。"""
        nonlocal n, ngroup, max_ts
        if not rows:
            return
        ngroup += 1
        for r in rows:
            ts = r[I['time']] or 0
            if ts < cut:
                continue
            max_ts = max(max_ts, ts)
            uid = r[I['sender_uid']] or ''
            sqq = r[I['sender_qq']] or 0
            name = member_names.get((g, uid)) or global_names.get(uid) or ''
            if not name and sqq:
                name = 'QQ%d' % sqq
            if not name:
                name = '系统'
            try:
                kind, text = decode_body(r[I['body']], sc)
            except Exception:
                kind, text = 'error', ''
            # 图片消息：登记 md5，供「不点开也能下原图」使用
            if r[I['body']]:
                try:
                    for a, b, c in pb_fields(r[I['body']]):
                        if a != 40800 or b != 2:
                            continue
                        sf = {}
                        for x, y, z in pb_fields(c):
                            sf.setdefault(x, []).append(z)
                        if sf.get(45002, [0])[0] == 2:
                            mm = sf.get(45406, [b''])[0]
                            if isinstance(mm, (bytes, bytearray)) and len(mm) == 16:
                                mbuf.append((r[0], ts, d.strftime('%Y-%m-%d') if d else '',
                                             g, group_names.get(g, ''), name, mm.hex(),
                                             sf.get(45405, [0])[0] or 0))
                except Exception:
                    pass
            # 转发：把 40900 里的原始消息展开，正文接在后面
            try:
                fwd = decode_forward(r[I['forward']], sc)
                if fwd:
                    block = format_forward(fwd)
                    text = (text + chr(10) if text else '') + block
                    kind = 'forward'
            except Exception:
                pass
            d = local(ts) if ts else None
            buf.append((r[0], ts, d.strftime('%Y-%m-%d') if d else '', d.hour if d else -1,
                        g, group_names.get(g, ''), uid, sqq, name,
                        r[I['direction']] or 0, r[I['msg_type']] or 0, r[I['subtype']] or 0,
                        kind, text, r[I['reply_seq']] or 0))
            while len(buf) >= 4000:
                idx.executemany('INSERT OR REPLACE INTO messages VALUES (%s)' % ','.join('?' * 15), buf[:4000])
                del buf[:4000]
                idx.commit()
            if len(mbuf) >= 4000:
                flush_media(idx, mbuf)
                idx.commit()
        n += len(rows)

    # 分轮读取：整段失败就先「重新解密一份新快照」再重试。
    # 原因：QQ 是活跃写入的库，某次解密快照可能正好落在一次 checkpoint 中间，
    # 损坏页落在哪个群上每次都不一样，换个快照通常就好了。
    pending = list(codes)
    for round_no in range(3):
        failed = []
        for g in pending:
            rows = run(sql, (g, day0))
            if rows is None:
                failed.append(g)
            else:
                take(g, rows)
        if not failed:
            break
        log('[warn] %d 个群整段读取失败，重新解密快照后重试（第 %d 轮）' % (len(failed), round_no + 1))
        try:
            con.close()          # 必须先关掉连接，否则 Windows 不允许覆盖文件
        except Exception:
            pass
        ensure_plain(cfg, km, 'nt_msg.db', force=True)
        con = open_ro(PATHS)
        pending = failed
    else:
        # 三轮都没救回来：退化为按天取，坏日跳过并记账
        for g in pending:
            rows = []
            for k in range(days + 1):
                d = day0 + k * 86400
                part = run(sql_day, (g, d, d + 86400))
                if part is None:
                    bad_days.append((g, fmt(d, '%Y-%m-%d')))
                else:
                    rows.extend(part)
            log('  [warn] 群 %s 三轮仍失败，按天补取后缺 %d 天'
                % (g, sum(1 for x in bad_days if x[0] == g)))
            take(g, rows)
    if buf:
        idx.executemany('INSERT OR REPLACE INTO messages VALUES (%s)' % ','.join('?' * 15), buf)
        idx.commit()
    flush_media(idx, mbuf)
    idx.commit()
    try:
        con.close()
    except Exception:
        pass

    idx.execute('DELETE FROM messages WHERE ts < ?', (cut,))
    idx.execute('DELETE FROM media WHERE ts < ?', (cut,))
    idx.execute('DELETE FROM groups')
    idx.execute('''INSERT INTO groups
        SELECT group_code, MAX(group_name), MIN(ts), MAX(ts), COUNT(*)
        FROM messages GROUP BY group_code''')
    for k, v in (('built_at', str(now_ts())), ('cutoff_ts', str(max_ts)),
                 ('window_days', str(days)), ('schema_version', sc.get('qq_version', '')),
                 ('scan_rows', str(n)), ('groups_scanned', str(ngroup)),
                 ('read_errors', str(nerr)),
                 ('bad_days', json.dumps(bad_days[:50], ensure_ascii=False))):
        idx.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (k, v))
    idx.commit()
    tot = idx.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
    grp = idx.execute('SELECT COUNT(*) FROM groups').fetchone()[0]
    nmedia = idx.execute('SELECT COUNT(*) FROM media').fetchone()[0]
    for d, c in idx.execute("SELECT day, COUNT(*) FROM messages GROUP BY day ORDER BY day DESC LIMIT 7"):
        byday[d] = c
    log('[index] 完成：扫描 %d 行 / 入索引 %d 条 / %d 个群 / 读取错误 %d 次' % (n, tot, grp, nerr))
    log('[index] 图片登记 %d 条（可走直连下载，无需点开）' % nmedia)
    log('[index] 数据截止 %s（耗时 %.1fs）' % (fmt(max_ts, '%Y-%m-%d %H:%M:%S'), time.time() - t0))
    log('[index] 近期每天: %s' % ', '.join('%s=%d' % (d[5:], c) for d, c in byday.items()))
    if bad_days:
        log('[index] !! 有 %d 个「群-日」读不出来（已跳过）: %s'
            % (len(bad_days), ', '.join('%s@%s' % x for x in bad_days[:10])))
    idx.close()


if __name__ == '__main__':
    main()
