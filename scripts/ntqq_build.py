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
                       ensure_keymap, ensure_plain, cleanup_plain, unwrap_key_hex, open_ro,
                       build_names, open_index, decode_body, decode_forward, format_forward,
                       pb_fields, rebuild_fts, src_db, now_ts, local, fmt,
                       TZ, RUN, PLAIN, CACHE, log, log_debug, log_warn, log_error, set_log_level)
try:
    from ntqq_normalizer import NoiseNormalizer
except ImportError:
    NoiseNormalizer = None


def flush_media(idx, mbuf):
    if mbuf:
        idx.executemany('INSERT OR REPLACE INTO media VALUES (%s)' % ','.join('?' * 10), mbuf)
        mbuf.clear()


def flush_buf(idx, buf, normalizer=None):
    if not buf:
        return
    rows = buf
    if normalizer is not None:
        try:
            norm = normalizer.normalize_batch(buf)
            rows = [
                (m.msg_id, m.ts, m.day, m.hour, m.group_code, m.group_name,
                 m.sender_uid, m.sender_qq, m.sender_name, m.direction,
                 m.msg_type, m.subtype, m.kind, m.text, m.reply_seq, m.seq)
                for m in norm
            ]
        except Exception as e:
            log_debug('复读折叠异常: %s' % e)
            rows = buf
    idx.executemany('INSERT OR REPLACE INTO messages VALUES (%s)' % ','.join('?' * 16), rows)
    buf.clear()

KEYS = ['seq', 'direction', 'sender_uid', 'group_code_str', 'group_key',
        'sender_qq', 'time', 'card', 'nick', 'msg_type', 'subtype', 'body', 'reply_seq', 'forward']

WINDOWS = {'7d': 7, '30d': 30, '90d': 90, '180d': 180, '365d': 365, 'all': None}


def src_db_path(cfg):
    """QQ 原始 nt_msg.db 的位置。"""
    try:
        return src_db(cfg, 'nt_msg.db')
    except Exception:
        return '(未找到)'


def resolve_window(cfg, a):
    """挡位 -> 天数（None 表示全部历史）。--window 优先于 --days，其次 config。"""
    w = (getattr(a, 'window', None) or '').strip().lower()
    if w:
        if w in WINDOWS:
            return WINDOWS[w]
        if w.endswith('d') and w[:-1].isdigit():
            return int(w[:-1])
        raise SystemExit('未知挡位 %r。可选：%s（也可写 45d 这种）' % (a.window, ' / '.join(WINDOWS)))
    if getattr(a, 'days', None):
        return a.days if a.days > 0 else None
    return cfg.get('index_days', 30)


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


def _core_cols_ok(live, sc):
    """检查 group_msg_table 的核心读取列是否完整。"""
    if 'group_msg_table' not in live:
        return False
    gm = sc.get('group_msg_table') or {}
    req = {gm[k] for k in KEYS if k in gm}
    if 'day' in gm:
        req.add(gm['day'])
    req.add('40900')
    have = set(live['group_msg_table'])
    return req.issubset(have)


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
    opt_tables = set(sc.get('added_tables_9936') or ())
    opt_cols = set(sc.get('added_columns_9936') or ('40722',))
    problems = []
    for t, cols in recorded.items():
        if t not in live:
            if t in opt_tables:
                continue
            problems.append('表缺失: %s' % t)
            continue
        miss = [c for c in cols if c not in live[t] and c not in opt_cols]
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
    if _core_cols_ok(live, sc):
        log_warn('[schema] 检测到非核心表/列差异 %d 项（核心消息列完整，继续构建；详见 run/schema_report.md）' % len(problems))
        return True, []
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
    ensure_dirs()
    sc = load_schema()
    schema_path = find_schema()
    G = sc['group_msg_table']
    days = resolve_window(cfg, a)
    if days:
        cut = now_ts() - days * 86400
        day0 = int(datetime.fromtimestamp(cut, TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        wlabel = '近 %d 天' % days
    else:
        cut = 0
        day0 = 0
        wlabel = '全部历史'

    if getattr(a, 'estimate', False):
        src = src_db_path(cfg)
        sz = os.path.getsize(src) / 1024 ** 3 if os.path.exists(src) else 0
        print('挡位：%s' % wlabel)
        print('源库：%s（%.2f GB）' % (src, sz))
        print('磁盘占用：约 %.2f GB 临时空间（默认 --source sql：全盘解密，建完索引即删）' % sz)
        print('           备选 --source stream：不落盘，但慢 2-3 倍')
        print('留存：data/index.db + 群名库（30d约95MB / 90d约195MB / all约1075MB）')
        print('提示：--window all 明显更慢（约 7 分钟）、索引更大；日常用 30d/90d 足够。')
        return

    ok_cfg, cfg_errs = validate_config(cfg, require_account=True)
    if not ok_cfg:
        for err in cfg_errs:
            log_error('[config] 错误: %s' % err)
        raise SystemExit('配置校验未通过，请先检查 config.json')

    print('@@PROGRESS|{"stage":"key","pct":0,"detail":"准备密钥"}', flush=True)
    km = ensure_keymap(cfg, force=a.force)
    src_mode = getattr(a, 'source', 'sql') or 'sql'
    stream = (src_mode == 'stream')
    snap = (src_mode == 'snap')
    for name in cfg['decrypt']:
        if stream and name == 'nt_msg.db':
            continue          # 主库不落地：按需解密直接抽行
        if snap and name == 'nt_msg.db':
            continue          # 主库由 ntqq_snapshot.py 提供，**绝不在这里重解密**
        log('[decrypt] %s' % name)
        ensure_plain(cfg, km, name, force=a.force)
    if snap and not os.path.exists(os.path.join(PLAIN, 'nt_msg.db')):
        raise SystemExit('--source snap 需要先跑 ntqq_snapshot.py 生成 data/plain/nt_msg.db')
    log('[source] 取数方案：%s' % ('按需解密（--source stream，备选；nt_msg.db 不落盘）' if stream
                                  else ('一致快照（--source snap，来自 ntqq_snapshot.py）' if snap
                                        else '全盘解密（--source sql，默认；明文库建完索引后删除）')))

    # QQ 是活跃写入的库，解密快照可能正好落在一次 checkpoint 中间，读 sqlite_master 会报 malformed。
    # 这种情况重新解密一次即可，最多重试 3 轮。
    if stream:
        import ntqq_slice as SL
        with open(km, encoding='utf-8') as fh:
            kmd = json.load(fh)
        keyhex = None
        for sh, files in kmd['salt_files'].items():
            for fp in files:
                if os.path.basename(fp) == 'nt_msg.db':
                    keyhex = unwrap_key_hex(kmd['key_map'][sh])
        if not keyhex:
            raise SystemExit('key_map 里没有 nt_msg.db 的密钥；先跑 ntqq_key.py')
        t_dec = time.time()
        ssrc = SL.Src(src_db_path(cfg), bytes.fromhex(keyhex))
        sdb = SL.Db(ssrc)
        SL.load_master(sdb)
        log('[source] 已解析加密库：%d 张表，%.1fs' % (len(sdb.tables), time.time() - t_dec))
        want_st = sc.get('structure')
        opt_tables = set(sc.get('added_tables_9936') or ())
        opt_cols = set(sc.get('added_columns_9936') or ('40722',))
        probs = []
        got = SL.structure_of(sdb)
        if want_st:
            for t, cs in want_st.items():
                if t not in got:
                    if t in opt_tables:
                        continue
                    probs.append('表缺失: %s' % t)
                else:
                    miss = [c for c in cs if c not in got[t] and c not in opt_cols]
                    if miss:
                        probs.append('表 %s 缺列: %s' % (t, ','.join(miss)))
        con = None
        ok = (not probs) or _core_cols_ok(got, sc)
        sroot, ssql = sdb.tables['group_msg_table']
        SCOLS = SL.cols_of(ssql)
    else:
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
                if snap:
                    raise SystemExit('一致快照本身读不出库结构——请重新跑 ntqq_snapshot.py')
                ensure_plain(cfg, km, 'nt_msg.db', force=True)
        if con is None:
            raise SystemExit('连续 3 次解密后仍读不出库结构。QQ 可能正在大量写入，稍后重试即可。')

    if not ok:
        log_error('[schema] !! 检测到库结构变化，已写出 run/schema_report.md')
        for p in probs[:20]:
            log_error('   - %s' % p)
        log_error('[schema] 已停止。请按 SKILL.md 的「版本漂移重解析」流程处理后重跑。')
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
        sys.exit(3)

    log('[names] 读取群名/昵称 ...')
    group_names, member_names, global_names = build_names(cfg)
    log('  群名 %d, 群成员名片 %d, 全局昵称 %d'
        % (len(group_names), len(member_names), len(global_names)))
    if con is not None:
        con.close()

    codes = group_codes(sc)
    if a.only_groups:
        want = set(int(x) for x in a.only_groups.split(','))
        codes = [c for c in codes if c in want]
    total_g = len(codes)
    processed = set()

    def report_progress():
        print('@@PROGRESS|{"stage":"index","done":%d,"total":%d,"pct":%.1f}'
              % (len(processed), total_g, len(processed) * 100.0 / max(1, total_g)), flush=True)

    def done_g(g_code):
        processed.add(g_code)
        report_progress()

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
    if not stream and days and not a.rebuild and not a.only_groups and prev_cutoff > cut and 0 < prev_day0 <= day0:
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
        log('[index] 候选群 %d 个 | 挡位：%s%s'
            % (len(codes), wlabel, ('（>= %s）' % fmt(day0, '%Y-%m-%d')) if days else ''))

    I = {k: i + 1 for i, k in enumerate(KEYS)}
    G = dict(G)
    G['forward'] = '40900'          # 40900 不在 schema 的 group_msg_table 映射里，单独补
    cols = ','.join('"%s"' % G[k] for k in KEYS)
    PATHS = os.path.join(PLAIN, 'nt_msg.db')
    sql = ('SELECT rowid,%s FROM group_msg_table WHERE "%s"=? AND "%s">=? ORDER BY "%s"'
           % (cols, G['group_key'], G['day'], G['day']))
    sql_day = ('SELECT rowid,%s FROM group_msg_table WHERE "%s"=? AND "%s">=? AND "%s"<? ORDER BY "%s"'
               % (cols, G['group_key'], G['day'], G['day'], G['day']))

    con = None if stream else open_ro(PATHS)
    normalizer = None if getattr(a, 'no_fold_repeats', False) or (NoiseNormalizer is None) else NoiseNormalizer()
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
        nonlocal ngroup
        if not rows:
            return
        ngroup += 1
        for r in rows:
            take_row(g, r)

    def take_row(g, r):
        nonlocal n, max_ts
        ts = r[I['time']] or 0
        if ts < cut:
            n += 1
            return
        # d 必须在图片登记之前算出来：登记分支（下面 mbuf.append）会用到它。
        d = local(ts) if ts else None
        max_ts = max(max_ts, ts)
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
                        decl_sz = sf.get(45405, [0])[0] or 0
                        # L1 过滤：跳过 < 15KB 的微小贴纸/表情（可通过 --no-filter-images 关闭）
                        if not getattr(a, 'no_filter_images', False) and 0 < decl_sz < 15360:
                            continue
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
                                         decl_sz, host, p0))
            except Exception as e:
                log_warn('[warn] 图片登记异常（msg_id=%s）: %s' % (r[0], str(e)[:120]))
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
                    kind, text, r[I['reply_seq']] or 0, r[I['seq']] or 0))
        while len(buf) >= 4000:
            flush_buf(idx, buf, normalizer)
            idx.commit()
        if len(mbuf) >= 4000:
            flush_media(idx, mbuf)
            idx.commit()
        n += 1

    # 分轮读取：整段失败就先「重新解密一份新快照」再重试。
    # 原因：QQ 是活跃写入的库，某次解密快照可能正好落在一次 checkpoint 中间，
    # 损坏页落在哪个群上每次都不一样，换个快照通常就好了。
    if stream:
        # ── 单遍扫描加密库：边解边丢，不产生 4GB 明文副本 ──
        pos_of = {c: i for i, c in enumerate(SCOLS)}
        need = {}
        for i, k in enumerate(KEYS):
            need[pos_of[G[k]]] = i + 1
        SLOT_DAY = len(KEYS) + 1
        SLOT_GK = len(KEYS) + 2
        need[pos_of[G['day']]] = SLOT_DAY
        need[pos_of[G['group_key']]] = SLOT_GK
        want_groups = set(codes)
        # 先只解「群 + 日」两个整数列做过滤（跳过 3GB blob 不解码），命中才解全部列
        GATE = {pos_of[G['group_key']]: 0, pos_of[G['day']]: 1}
        scanned = 0
        kept = 0
        seen = set()
        t_scan = time.time()
        for rowid, pl in sdb.rows(sroot):
            scanned += 1
            try:
                g = SL.decode_some(pl, GATE)
            except Exception:
                ssrc.bad_rows += 1
                continue
            gk = g[0]
            if gk not in want_groups or (g[1] or 0) < scan_day0:
                continue
            try:
                vals = SL.decode_some(pl, need)
            except Exception:
                ssrc.bad_rows += 1
                continue
            vals[0] = rowid
            seen.add(gk)
            kept += 1
            take_row(gk, tuple(vals[:len(KEYS) + 1]))
            if scanned % 1000000 == 0:
                log('  [scan] 已扫 %d 行 / 命中 %d 条 / %.0fs' % (scanned, kept, time.time() - t_scan))
        n = scanned
        ngroup = len(seen)
        try:
            ssrc.close()
        except Exception:
            pass
        log('[source] 扫描 %d 行，命中 %d 群 / %d 条，坏行 %d，耗时 %.1fs'
            % (scanned, ngroup, kept, ssrc.bad_rows, time.time() - t_scan))

    pending = [] if stream else list(codes)
    for round_no in range(3):
        if not pending:
            break
        failed = []
        for g in pending:
            rows = run(sql, (g, scan_day0))
            if rows is None:
                failed.append(g)
            else:
                take(g, rows)
                done_g(g)
        if not failed:
            break
        log_warn('[warn] %d 个群整段读取失败，重新打开快照后重试（第 %d 轮）' % (len(failed), round_no + 1))
        try:
            con.close()          # 必须先关掉连接，否则 Windows 不允许覆盖文件
        except Exception:
            pass
        if not snap:
            # snap 模式下这份明文库是「一致快照」，重解密只会把它换成撕裂版，绝不能做
            ensure_plain(cfg, km, 'nt_msg.db', force=True)
        con = open_ro(PATHS)
        pending = failed
    else:
        # 三轮都没救回来：退化为按天取，坏日跳过并记账
        for g in pending:
            rows = []
            dl = None
            try:
                dl = [r[0] for r in con.execute(
                    'SELECT DISTINCT "%s" FROM group_msg_table WHERE "%s"=? ORDER BY "%s"'
                    % (G['day'], G['group_key'], G['day']), (g,))]
                dl = [d for d in dl if d and d >= max(946684800, scan_day0)]
            except Exception:
                dl = None
            if not dl:
                scan_days = max(1, (now_ts() - scan_day0) // 86400 + 1) if scan_day0 else 400
                dl = [scan_day0 + k * 86400 for k in range(scan_days + 1)]
            for d in dl:
                part = run(sql_day, (g, d, d + 86400))
                if part is None:
                    bad_days.append((g, fmt(d, '%Y-%m-%d')))
                else:
                    rows.extend(part)
            log_warn('  [warn] 群 %s 三轮仍失败，按天补取后缺 %d 天'
                     % (g, sum(1 for x in bad_days if x[0] == g)))
            take(g, rows)
            done_g(g)
    if buf:
        flush_buf(idx, buf, normalizer)
        idx.commit()
    flush_media(idx, mbuf)
    idx.commit()

    # ── 私聊消息抽取（可选 --include-c2c）────────────────────────────────────
    c2c_n = 0
    if include_c2c and not stream and con is not None:
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

    if con is not None:
        try:
            con.close()
        except Exception:
            pass

    idx.execute('DELETE FROM messages WHERE ts < ?', (cut,))
    idx.execute('DELETE FROM media WHERE ts < ?', (cut,))
    idx.execute('DELETE FROM groups')
    idx.commit()
    # 切小挡位后文件不会自动收缩（SQLite 不回收已分配的页），VACUUM 一次把空间还给系统
    if cfg.get('vacuum', False):
        ix_path = os.path.join(CACHE, 'index.db')
        sz0 = os.path.getsize(ix_path) if os.path.exists(ix_path) else 0
        try:
            try:
                idx.autocommit = True
            except Exception:
                idx.isolation_level = None
            idx.execute('VACUUM')
            try:
                idx.autocommit = False
            except Exception:
                idx.isolation_level = ''
            sz1 = os.path.getsize(ix_path)
            log('[storage] 索引 %.1f MB%s' % (sz1 / 1048576.0,
                ('（已收缩，回收未用页 %.1f MB）' % ((sz0 - sz1) / 1048576.0)) if sz0 and sz1 < sz0 else ''))
        except Exception as e:
            log('[storage] VACUUM 跳过：%s' % e)
    idx.execute('''INSERT INTO groups
        SELECT group_code, MAX(group_name), MIN(ts), MAX(ts), COUNT(*)
        FROM messages GROUP BY group_code''')
    db_max_ts = idx.execute('SELECT COALESCE(MAX(ts), 0) FROM messages').fetchone()[0]
    max_ts = max(max_ts, db_max_ts)
    for k, v in (('built_at', str(now_ts())), ('cutoff_ts', str(max_ts)),
                 ('window_days', str(days if days else -1)), ('window_day0', str(day0)),
                 ('window_label', wlabel), ('source_mode', src_mode),
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
    log('[index] 挡位 %s（%s）' % (wlabel, ('window=all' if not days else 'window=%dd' % days)))
    log('[index] 完成：扫描 %d 行 / 入索引 %d 条 / %d 个群 / 读取错误 %d 次' % (n, tot, grp, nerr))
    log('[index] 图片登记 %d 条（可走直连下载，无需点开）' % nmedia)
    log('[index] 数据截止 %s（耗时 %.1fs）' % (fmt(max_ts, '%Y-%m-%d %H:%M:%S'), time.time() - t0))
    log('[index] 近期每天: %s' % ', '.join('%s=%d' % (d[5:], c) for d, c in byday.items()))
    if bad_days:
        log_warn('[index] !! 有 %d 个「群-日」读不出来（已跳过）: %s'
                 % (len(bad_days), ', '.join('%s@%s' % x for x in bad_days[:10])))

    # 索引已经落盘，此时清明文最安全：群变更登记只读 group_info.db，
    # 而 cleanup_plain 只删最大的 nt_msg.db。
    if not (getattr(a, 'keep_plain', False) or cfg.get('keep_plain', True)):
        cleanup_plain(cfg)

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

    print('@@PROGRESS|{"stage":"done","pct":100}', flush=True)
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
    ap.add_argument('--days', type=int, default=None, help='滚动窗口天数（兼容旧参数）')
    ap.add_argument('--window', default=None,
                    help='挡位：all / 365d / 180d / 90d / 30d / 7d（优先于 --days）')
    ap.add_argument('--source', choices=('sql', 'stream', 'snap'), default='sql',
                    help='取数方式：sql=全盘解密（默认、最快）；stream=按需解密（不落盘但慢 2-3 倍）；'
                         'snap=直接消费 ntqq_snapshot.py 产出的一致快照明文库')
    ap.add_argument('--keep-plain', action='store_true',
                    help='保留解密后的全量明文库')
    ap.add_argument('--estimate', action='store_true',
                    help='只打印挡位的预估开销，不解密不建索引')
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--rebuild', action='store_true')
    ap.add_argument('--only-groups', default=None, help='逗号分隔的群号，仅重建这些群')
    ap.add_argument('--include-c2c', action='store_true', help='同时索引私聊消息（存入独立 c2c_messages 表）')
    ap.add_argument('--clean-plain', action='store_true', help='构建索引后自动删除 data/plain/ 下的明文库副本')
    ap.add_argument('--no-fold-repeats', action='store_true', default=False, help='禁用连续相同发言复读折叠')
    ap.add_argument('--no-filter-images', action='store_true', default=False, help='禁用 <15KB 微小表情图元数据过滤')
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
