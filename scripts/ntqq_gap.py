#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQ 群聊聊天记录缺口检测工具 (ntqq_gap.py)

检测本地聊天记录中的断层与缺口：
1. 序号断层 (Sequence Gap)：group_msg_table 中相连记录的 seq 差值 > 1
2. 历史头部缺口 (Head Gap)：请求窗口期内（如 30 天），最早消息与窗口起始点之间的缺失
3. 损坏天缺口 (Bad Days)：ntqq_build.py 构建时由于 SQLite WAL 碰撞读失败的日期
4. 时间断层 (Time Gap)：在正常活跃时段内超过阈值（默认 24h）无任何消息的断档

输出：
- 控制台友好的缺口统计报告
- 结构化待爬清单 run/gaps.json
"""
import argparse, json, os, sqlite3, sys, time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ntqq_core import (load_config, load_schema, ensure_dirs, open_ro, open_index,
                       build_names, now_ts, local, fmt, TZ, RUN, PLAIN, INDEX_DB, log)


def detect_gaps_from_db(plain_db, schema, cut_ts, group_filter=None, group_names=None):
    """从解密后的 nt_msg.db 中精确检测 seq 序号断层与头部缺口。"""
    gaps = []
    if not os.path.exists(plain_db):
        return gaps
    
    group_names = group_names or {}
    sc_g = schema.get('group_msg_table', {})
    col_group = sc_g.get('group_key', '40027')
    col_seq = sc_g.get('seq', '40003')
    col_time = sc_g.get('time', '40050')
    col_day = sc_g.get('day', '40058')

    con = open_ro(plain_db)
    try:
        # 获取候选群列表
        if group_filter:
            target_groups = [group_filter]
        else:
            cur = con.execute('SELECT DISTINCT "%s" FROM group_msg_table WHERE "%s" >= ?' % (col_group, col_time), (cut_ts,))
            target_groups = [r[0] for r in cur.fetchall() if r[0]]

        for g in sorted(target_groups):
            g_name = group_names.get(g, str(g))
            # 查询窗口期内的 seq 和 time，按 seq 升序
            sql = ('SELECT "%s", "%s" FROM group_msg_table '
                   'WHERE "%s" = ? AND "%s" >= ? '
                   'ORDER BY "%s" ASC' % (col_seq, col_time, col_group, col_time, col_seq))
            try:
                rows = con.execute(sql, (g, cut_ts)).fetchall()
            except sqlite3.DatabaseError as e:
                log('[gap] 群 %s 查询异常: %s' % (g, e))
                continue

            if not rows:
                continue

            # 1. 检查头部缺口 (Head Gap)
            first_seq, first_time = rows[0]
            if first_seq and first_seq > 1 and first_time and (first_time - cut_ts) > 86400:
                # 本地最早消息的时间比要求的起始时间晚了 1 天以上，且 seq > 1，说明前面还有更早的历史可漫游
                missing_days = (first_time - cut_ts) / 86400.0
                gaps.append({
                    'group_code': g,
                    'group_name': g_name,
                    'type': 'head_gap',
                    'start_seq': max(1, first_seq - 500), # 估算拉取前 500 条
                    'end_seq': first_seq - 1,
                    'count': min(500, first_seq - 1),
                    'time_start': fmt(cut_ts, '%Y-%m-%d %H:%M'),
                    'time_end': fmt(first_time, '%Y-%m-%d %H:%M'),
                    'description': '头部缺口：距离窗口起始点缺约 %.1f 天' % missing_days
                })

            # 2. 检查中间序号断层 (Sequence Gap)
            prev_seq = None
            prev_time = None
            for seq, ts in rows:
                if not seq:
                    continue
                if prev_seq is not None and (seq - prev_seq) > 1:
                    gap_count = seq - prev_seq - 1
                    gaps.append({
                        'group_code': g,
                        'group_name': g_name,
                        'type': 'seq_gap',
                        'start_seq': prev_seq + 1,
                        'end_seq': seq - 1,
                        'count': gap_count,
                        'time_start': fmt(prev_time, '%Y-%m-%d %H:%M') if prev_time else '未知',
                        'time_end': fmt(ts, '%Y-%m-%d %H:%M') if ts else '未知',
                        'description': '内层断层：缺失 %d 条消息 (seq %d ~ %d)' % (gap_count, prev_seq + 1, seq - 1)
                    })
                prev_seq = seq
                prev_time = ts

    finally:
        con.close()

    return gaps


def detect_gaps_from_index(idx_path, cut_ts, group_filter=None, group_names=None):
    """当没有解密源库时，降级从 index.db 中分析时间断档与 bad_days。"""
    gaps = []
    if not os.path.exists(idx_path):
        return gaps

    group_names = group_names or {}
    con = sqlite3.connect(idx_path)
    try:
        # 1. 检查 bad_days 记录
        try:
            cur = con.execute("SELECT value FROM meta WHERE key = 'bad_days'")
            row = cur.fetchone()
            if row and row[0]:
                bad_days_list = json.loads(row[0])
                for item in bad_days_list:
                    # item 格式为 [group_code, "YYYY-MM-DD"] 或类似结构
                    if isinstance(item, (list, tuple)) and len(item) >= 2:
                        g, d_str = item[0], item[1]
                        if group_filter and g != group_filter:
                            continue
                        gaps.append({
                            'group_code': g,
                            'group_name': group_names.get(g, str(g)),
                            'type': 'bad_day',
                            'start_seq': 0,
                            'end_seq': 0,
                            'count': 0,
                            'time_start': d_str + ' 00:00',
                            'time_end': d_str + ' 23:59',
                            'description': '损坏页缺口：日期 %s 解密损坏缺失' % d_str
                        })
        except Exception as e:
            log('[gap] 读取 bad_days 失败: %s' % e)

        # 2. 检查大段离线时间断层 (Time Gap > 48h)
        target_groups = [group_filter] if group_filter else [
            r[0] for r in con.execute('SELECT DISTINCT group_code FROM messages WHERE ts >= ?', (cut_ts,)).fetchall()
        ]

        for g in target_groups:
            cur = con.execute('SELECT ts FROM messages WHERE group_code = ? AND ts >= ? ORDER BY ts ASC', (g, cut_ts))
            rows = cur.fetchall()
            if not rows:
                continue

            prev_ts = None
            for (ts,) in rows:
                if prev_ts is not None and (ts - prev_ts) > 86400 * 2: # 超过 48 小时完全无消息
                    gaps.append({
                        'group_code': g,
                        'group_name': group_names.get(g, str(g)),
                        'type': 'time_gap',
                        'start_seq': 0,
                        'end_seq': 0,
                        'count': 0,
                        'time_start': fmt(prev_ts, '%Y-%m-%d %H:%M'),
                        'time_end': fmt(ts, '%Y-%m-%d %H:%M'),
                        'description': '时间断档：跨度 %.1f 天无记录' % ((ts - prev_ts) / 86400.0)
                    })
                prev_ts = ts
    finally:
        con.close()

    return gaps


def format_gap_report(gaps):
    """格式化生成终端与 Markdown 友好的缺口检测报告。"""
    if not gaps:
        return '恭喜！当前检测窗口期内未发现任何聊天记录缺口。数据连续完整。'

    lines = []
    lines.append('# QQ 群聊记录缺口扫描报告')
    lines.append('检测时间：%s | 发现缺口总数：%d 个\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), len(gaps)))

    # 按群聚合统计
    by_group = {}
    for gp in gaps:
        by_group.setdefault(gp['group_code'], []).append(gp)

    lines.append('| 群号 | 群名 | 缺口类型 | 缺失条数 / 跨度 | 起止时间 | 状态说明 |')
    lines.append('|---|---|---|---|---|---|')

    for g, glist in sorted(by_group.items()):
        for gp in glist:
            type_label = {
                'seq_gap': '🔴 序号断层',
                'head_gap': '🟡 头部缺失',
                'bad_day': '⚠️ 坏页损坏',
                'time_gap': '⚪ 时间断档'
            }.get(gp['type'], gp['type'])
            
            cnt_str = ('%d 条' % gp['count']) if gp['count'] > 0 else '-'
            t_span = '%s ~ %s' % (gp['time_start'], gp['time_end'])
            lines.append('| `%s` | %s | %s | %s | %s | %s |' % (
                gp['group_code'], gp['group_name'], type_label, cnt_str, t_span, gp['description']
            ))

    total_missing_msgs = sum(gp['count'] for gp in gaps if gp['count'] > 0)
    lines.append('\n> 💡 统计：检测到共约 **%d** 条确切丢失消息待爬取。' % total_missing_msgs)
    lines.append('> 可运行 `python scripts/ntqq_crawler.py` 开始注入拉取并补全缺口。')
    return '\n'.join(lines)


def main():
    ap = argparse.ArgumentParser(description='QQ 群聊记录缺口扫描与分析')
    ap.add_argument('--group', type=str, default=None, help='指定群号或群名过滤')
    ap.add_argument('--days', type=int, default=None, help='扫描最近 N 天（默认取 config.json 中的 index_days）')
    ap.add_argument('--out', type=str, default=None, help='报告输出 Markdown 路径')
    ap.add_argument('--json-out', type=str, default=None, help='待爬清单 JSON 输出路径（默认写 run/gaps.json）')
    a = ap.parse_args()

    cfg = load_config()
    ensure_dirs()
    sc = load_schema()
    group_names, _, _ = build_names(cfg)

    days = a.days or cfg.get('index_days', 30)
    cut = now_ts() - days * 86400

    target_group_code = None
    if a.group:
        if a.group.isdigit():
            target_group_code = int(a.group)
        else:
            for c, name in group_names.items():
                if a.group.lower() in name.lower():
                    target_group_code = c
                    break
            if not target_group_code:
                log('[warn] 未能通过名称匹配到群: %s' % a.group)

    plain_db = os.path.join(PLAIN, 'nt_msg.db')
    gaps = []

    # 优先从解密源库做精确 seq 检测
    if os.path.exists(plain_db):
        log('[gap] 正在扫描解密库 nt_msg.db (窗口 %d 天) ...' % days)
        gaps.extend(detect_gaps_from_db(plain_db, sc, cut, target_group_code, group_names))
    else:
        log('[warn] 未检测到解密库 run/plain/nt_msg.db，降级扫描 cached/index.db ...')
        gaps.extend(detect_gaps_from_index(INDEX_DB, cut, target_group_code, group_names))

    # 输出结构化 JSON
    json_path = a.json_out or os.path.join(RUN, 'gaps.json')
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(gaps, f, indent=2, ensure_ascii=False)
    log('[gap] 待爬缺口清单已保存至: %s' % json_path)

    # 生成文字报告
    report_text = format_gap_report(gaps)
    print('\n' + report_text + '\n')

    if a.out:
        with open(a.out, 'w', encoding='utf-8') as f:
            f.write(report_text)
        log('[gap] 报告已输出到: %s' % a.out)


if __name__ == '__main__':
    main()
