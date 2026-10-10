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
from ntqq_core import (load_config, validate_config, load_schema, find_schema, ensure_dirs,
                       ensure_keymap, ensure_plain, open_ro, build_names, open_index,
                       decode_body, decode_forward, format_forward, pb_fields, rebuild_fts,
                       src_db, now_ts, local, fmt, TZ, RUN, PLAIN, CACHE,
                       log, log_debug, log_warn, log_error, set_log_level)


def flush_media(idx, mbuf):
    if mbuf:
        idx.executemany('INSERT OR REPLACE INTO media VALUES (%s)' % ','.join('?' * 10), mbuf)
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
            except sqlite3.DatabaseError as e:
                log_debug('读 %s 群号异常: %s' % (t, e))
        con.close()
    nm = os.path.join(PLAIN, 'nt_msg.db')
    if os.path.exists(nm):
        con = open_ro(nm)
        try:
            for (c,) in con.execute('SELECT DISTINCT "40027" FROM recent_contact_v3_table'):
                if c:
                    codes.add(int(c))
        except sqlite3.DatabaseError as e:
            log_debug('读 recent_contact_v3_table 群号异常: %s' % e)
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


def _meta_get(idx, key, default='0'):
    r = idx.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return r[0] if r else default


def clean_plain_dbs(cfg):
    """清理 data/plain/ 下的明文数据库副本，节省磁盘空间并降低明文暴露风险。"""
    removed = 0
    for name in cfg.get('decrypt', []):
        p = os.path.join(PLAIN, name)
        if os.path.exists(p):
            try:
                os.remove(p)
                removed += 1
            except OSError as e:
                log_warn('[clean] 无法删除明文库 %s: %s' % (p, e))
    if removed:
        log('[clean] 已自动清理 %d 个明文数据库缓存文件' % removed)


def build_once(a):
    t0 = time.time()
    cfg = load_config()
    ok_cfg, cfg_errs = validate_config(cfg, require_account=True)
    if not ok_cfg:
        for err in cfg_errs:
            log_error('[config] 错误: %s' % err)
        raise SystemExit('配置校验未通过，请先检查 config.json')

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
            except Exception as close_err:
                log_debug('关闭异常连接失败: %r' % close_err)
            con = None
            log_warn('[warn] 解密快照不一致（%s），重新解密后重试 %d/3' % (e, attempt + 1))
            ensure_plain(cfg, km, 'nt_msg.db', force=True)
    if con is None:
        raise SystemExit('连续 3 次解密后仍读不出库结构。QQ 可能正在大量写入，稍后重试即可。')
    if not ok:
        log_error('[schema] !! 检测到库结构变化，已写出 run/schema_report.md')
        for p in probs[:20]:
            log_error('   - %s' % p)
        log_error('[schema] 已停止。请按 SKILL.md 的「版本漂移重解析」流程处理后重跑。')
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

    idx = open_index()
    include_c2c = bool(getattr(a, 'include_c2c', False) or cfg.get('include_c2c', False))
    if a.rebuild:
        idx.execute('DELETE FROM messages'); idx.execute('DELETE FROM groups')
        idx.execute('DELETE FROM media')
        if include_c2c:
            idx.execute('DELETE FROM c2c_messages'); idx.execute('DELETE FROM c2c_groups')
        idx.commit()

    # ── 增量索引判定 ──────────────────────────────────────────────────────────
    prev_cutoff = int(_meta_get(idx, 'cutoff_ts', '0') or 0)
    prev_day0 = int(_meta_get(idx, 'window_day0', '0') or 0)
    scan_day0 = day0
    is_incremental = False
    if not a.rebuild and not a.only_groups and prev_cutoff > cut and 0 < prev_day0 <= day0:
        # 回退 1 天（86400s）以覆盖跨午夜或延迟落盘的同日消息
        cand_day0 = int(datetime.fromtimestamp(max(cut, prev_cutoff - 86400), TZ)
                        .replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        if cand_day0 > day0:
            scan_day0 = cand_day0
            is_incremental = True
            saved_days = (scan_day0 - day0) // 86400
            log('[index] 增量模式：候选群 %d 个，从 %s 起扫描（复用已有索引，跳过前 %d 天；--rebuild 可强制全量）'
                % (len(codes), fmt(scan_day0, '%Y-%m-%d'), saved_days))
    if not is_incremental:
        log('[index] 全量窗口模式：候选群 %d 个，窗口 %d 天（>= %s）'
            % (len(codes), days, fmt(day0, '%Y-%m-%d')))

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
    max_ts = prev_cutoff if is_incremental else 0
    bad_days = []
    byday = {}

    def run(sqltext, params):
        nonlocal con, nerr
        for attempt in (0, 1):
            try:
                return con.execute(sqltext, params).fetchall()
            except sqlite3.DatabaseError as e:
                nerr += 1
                log_debug('SQL 读取异常 (attempt=%d): %s' % (attempt, e))
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
            d = local(ts) if ts else None
            uid = r[I['sender_uid']] or ''
            sqq = r[I['sender_qq']] or 0
            name = member_names.get((g, uid)) or global_names.get(uid) or global_names.get('qq:%d' % sqq) or ''
            if not name and sqq:
                name = 'QQ%d' % sqq
            if not name:
                name = '系统'
            try:
                kind, text = decode_body(r[I['body']], sc)
            except Exception as e:
                log_debug('decode_body 异常 msg_id=%s: %r' % (r[0], e))
                kind, text = 'error', ''
            # 图片消息：登记 md5，供「不点开也能下原图」使用
            if r[I['body']]:
                try:
                    for a_f, b_f, c_f in pb_fields(r[I['body']]):
                        if a_f != 40800 or b_f != 2:
                            continue
                        sf = {}
                        for x, y, z in pb_fields(c_f):
                            sf.setdefault(x, []).append(z)
                        if sf.get(45002, [0])[0] == 2:
                            mm = sf.get(45406, [b''])[0]
                            if isinstance(mm, (bytes, bytearray)) and len(mm) == 16:
                                # 顺带记下多媒体 CDN 的 host 与 spec=0（原图）路径，
                                # 供 ntqq_media.py 配合内存里的 rkey 取原图。
                                hv = sf.get(45816, [b''])[0]
                                host = hv.decode('utf8', 'replace') if isinstance(hv, (bytes, bytearray)) else ''
                                p0 = ''
                                for fno in (45802, 45803, 45804):
                                    pv = sf.get(fno, [b''])[0]
                                    if isinstance(pv, (bytes, bytearray)) and b'spec=0' in pv:
                                        p0 = pv.decode('utf8', 'replace')
                                        break
                                mbuf.append((r[0], ts, d.strftime('%Y-%m-%d') if d else '',
                                             g, group_names.get(g, ''), name, mm.hex(),
                                             sf.get(45405, [0])[0] or 0, host, p0))
                except Exception as e:
                    log_debug('提取图片元数据异常 msg_id=%s: %r' % (r[0], e))
            # 转发：把 40900 里的原始消息展开，正文接在后面
            try:
                fwd = decode_forward(r[I['forward']], sc)
                if fwd:
                    block = format_forward(fwd)
                    text = (text + chr(10) if text else '') + block
                    kind = 'forward'
            except Exception as e:
                log_debug('decode_forward 异常 msg_id=%s: %r' % (r[0], e))
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
            rows = run(sql, (g, scan_day0))
            if rows is None:
                failed.append(g)
            else:
                take(g, rows)
        if not failed:
            break
        log_warn('[warn] %d 个群整段读取失败，重新解密快照后重试（第 %d 轮）' % (len(failed), round_no + 1))
        try:
            con.close()          # 必须先关掉连接，否则 Windows 不允许覆盖文件
        except Exception:
            pass
        ensure_plain(cfg, km, 'nt_msg.db', force=True)
        con = open_ro(PATHS)
        pending = failed
    else:
        # 三轮都没救回来：退化为按天取，坏日跳过并记账
        scan_days = max(1, (now_ts() - scan_day0) // 86400 + 1)
        for g in pending:
            rows = []
            for k in range(scan_days + 1):
                d = scan_day0 + k * 86400
                part = run(sql_day, (g, d, d + 86400))
                if part is None:
                    bad_days.append((g, fmt(d, '%Y-%m-%d')))
                else:
                    rows.extend(part)
            log_warn('  [warn] 群 %s 三轮仍失败，按天补取后缺 %d 天'
                     % (g, sum(1 for x in bad_days if x[0] == g)))
            take(g, rows)
    if buf:
        idx.executemany('INSERT OR REPLACE INTO messages VALUES (%s)' % ','.join('?' * 15), buf)
        idx.commit()
    flush_media(idx, mbuf)
    idx.commit()

    # ── 私聊消息抽取（可选 --include-c2c）────────────────────────────────────
    c2c_n = 0
    if include_c2c:
        log('[c2c] 正在扫描私聊消息表 c2c_msg_table ...')
        sql_c2c = ('SELECT rowid,%s FROM c2c_msg_table WHERE "%s">=? ORDER BY "%s"'
                   % (cols, G['day'], G['day']))
        c2c_rows = run(sql_c2c, (scan_day0,))
        if c2c_rows:
            cbuf = []
            for r in c2c_rows:
                ts = r[I['time']] or 0
                if ts < cut:
                    continue
                max_ts = max(max_ts, ts)
                d = local(ts) if ts else None
                uid = r[I['sender_uid']] or ''
                sqq = int(r[I['sender_qq']] or 0)
                peer_qq = int(r[I['group_key']] or sqq or 0)
                peer_uid = str(r[I['group_code_str']] or uid or '')
                if not peer_qq and peer_uid:
                    peer_qq = abs(hash(peer_uid)) % 10000000000
                peer_name = (global_names.get('qq:%d' % peer_qq)
                             or global_names.get(peer_uid)
                             or (r[I['card']] or r[I['nick']] or '').strip()
                             or ('QQ%d' % peer_qq if peer_qq else '私聊联系人'))
                sender_name = (global_names.get(uid)
                               or global_names.get('qq:%d' % sqq)
                               or (r[I['nick']] or r[I['card']] or '').strip()
                               or ('QQ%d' % sqq if sqq else peer_name))
                try:
                    kind, text = decode_body(r[I['body']], sc)
                except Exception:
                    kind, text = 'error', ''
                try:
                    fwd = decode_forward(r[I['forward']], sc)
                    if fwd:
                        block = format_forward(fwd)
                        text = (text + chr(10) if text else '') + block
                        kind = 'forward'
                except Exception:
                    pass
                cbuf.append((r[0], ts, d.strftime('%Y-%m-%d') if d else '', d.hour if d else -1,
                             peer_qq, peer_name, uid, sqq, sender_name,
                             r[I['direction']] or 0, r[I['msg_type']] or 0, r[I['subtype']] or 0,
                             kind, text, r[I['reply_seq']] or 0))
            if c2c_rows and cbuf:
                idx.executemany('INSERT OR REPLACE INTO c2c_messages VALUES (%s)' % ','.join('?' * 15), cbuf)
                idx.execute('DELETE FROM c2c_messages WHERE ts < ?', (cut,))
                idx.execute('DELETE FROM c2c_groups')
                idx.execute('''INSERT INTO c2c_groups
                    SELECT group_code, MAX(group_name), MIN(ts), MAX(ts), COUNT(*)
                    FROM c2c_messages GROUP BY group_code''')
                idx.commit()
                c2c_n = len(cbuf)
        log('[c2c] 私聊扫描新增/更新 %d 条' % c2c_n)

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
    db_max_ts = idx.execute('SELECT COALESCE(MAX(ts), 0) FROM messages').fetchone()[0]
    max_ts = max(max_ts, db_max_ts)
    for k, v in (('built_at', str(now_ts())), ('cutoff_ts', str(max_ts)),
                 ('window_days', str(days)), ('window_day0', str(day0)),
                 ('schema_version', sc.get('qq_version', '')),
                 ('scan_rows', str(n)), ('groups_scanned', str(ngroup)),
                 ('read_errors', str(nerr)),
                 ('bad_days', json.dumps(bad_days[:50], ensure_ascii=False))):
        idx.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (k, v))
    idx.commit()
    rebuild_fts(idx)

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
        log_warn('[index] !! 有 %d 个「群-日」读不出来（已跳过）: %s'
                 % (len(bad_days), ', '.join('%s@%s' % x for x in bad_days[:10])))

    # ── 群变更登记：自动识别新入群 / 已退群 / 退群后回归 / 改名 ────────────
    try:
        import ntqq_group_state as GS
        res = GS.scan()
        if not res.get('ok'):
            log('[groups] 跳过：%s' % res.get('reason'))
        else:
            R = res['registry']['groups']
            log('[groups] 在群 %d 个；历史登记 %d 个（含已退）'
                % (sum(1 for r in R.values() if r.get('in_member_list')), len(R)))
            if res.get('suspect_snapshot'):
                log_warn('[groups] [warn] 读到的群数异常偏少，已挂起退群判定')
            if res.get('first_ever'):
                log('[groups] 首次建立基线，本次不报新入群')
            for r in res['new']:
                log('[groups] 🆕 新入群: %s %s' % (r['code'], r.get('name') or ''))
            for r in res['returned']:
                log('[groups] ↩ 退群后又回来: %s %s' % (r['code'], r.get('name') or ''))
            for r in res['left']:
                log('[groups] 🚪 判定已退群: %s %s' % (r['code'], r.get('name') or ''))
            for code, old, new in res['renamed']:
                log('[groups] ✏ 改名: %s %s -> %s' % (code, old, new))
    except Exception as e:                                  # 登记表坏了不该拖垮建索引
        log_warn('[groups] [warn] 群变更扫描失败（不影响索引）: %r' % e)

    idx.close()
    if getattr(a, 'clean_plain', False) or cfg.get('clean_plain_after_build', False):
        clean_plain_dbs(cfg)


def _src_db_fingerprint(cfg):
    """获取源库 nt_msg.db 及其 -wal 文件的 (mtime, size) 指纹，供 --watch 判断是否有新消息落盘。"""
    fp = []
    base = src_db(cfg, 'nt_msg.db')
    for p in (base, base + '-wal'):
        try:
            st = os.stat(p)
            fp.append((p, int(st.st_mtime), st.st_size))
        except OSError:
            fp.append((p, 0, 0))
    return tuple(fp)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=None)
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--rebuild', action='store_true')
    ap.add_argument('--only-groups', default=None, help='逗号分隔的群号，仅重建这些群')
    ap.add_argument('--include-c2c', action='store_true', help='同时索引私聊消息（存入独立 c2c_messages 表）')
    ap.add_argument('--clean-plain', action='store_true', help='构建索引后自动删除 data/plain/ 下的明文库副本')
    ap.add_argument('--watch', action='store_true', help='持续监听模式：检测源库变动并自动增量刷新索引')
    ap.add_argument('--interval', type=int, default=300, help='--watch 模式的轮询间隔秒数（默认 300s）')
    ap.add_argument('--verbose', action='store_true', help='显示调试日志（同时写到控制台）')
    a = ap.parse_args()

    if a.verbose:
        set_log_level(True)

    if not a.watch:
        build_once(a)
        return

    cfg = load_config()
    interval = max(10, int(a.interval or 300))
    log('[watch] 已启动持续监听模式（轮询间隔 %ds，按 Ctrl+C 停止）' % interval)
    last_fp = None
    try:
        while True:
            cur_fp = _src_db_fingerprint(cfg)
            if cur_fp != last_fp:
                log('[watch] 检测到源库变动，开始增量刷新索引 ...')
                try:
                    build_once(a)
                    last_fp = cur_fp
                except SystemExit as e:
                    log_warn('[watch] 本轮构建未完成: %s' % e)
                except Exception as e:
                    log_error('[watch] 本轮构建异常: %r' % e)
                # 构建后仅用增量模式
                a.rebuild = False
                a.force = False
            else:
                log_debug('[watch] 源库无变化，跳过本轮')
            time.sleep(interval)
    except KeyboardInterrupt:
        log('\n[watch] 监听已停止。')


if __name__ == '__main__':
    main()

