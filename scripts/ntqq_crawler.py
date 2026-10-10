#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQ 群聊缺口历史消息注入爬虫调度器 (ntqq_crawler.py)

功能：
1. 读取 run/gaps.json 缺口任务，或指定单个群的目标回溯窗口
2. 连接 QQ 进程内注入的 qq-digest-bridge RPC 服务 (http://127.0.0.1:48920)
3. 执行大群分批流式漫游拉取，带有自适应防风控频控（随机延时与退避）
4. 即时双写：解析返回的消息直接写入 cached/index.db，同时 NTQQ 内核自动持久化至 nt_msg.db
5. 支持断点续爬（run/crawl_state.json）与外部 QCE (qq-chat-exporter) 适配
"""
import argparse, json, os, random, sqlite3, sys, time, urllib.request, urllib.error
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ntqq_core import (load_config, ensure_dirs, open_index, build_names,
                       now_ts, local, fmt, TZ, RUN, DATA, INDEX_DB, log)

DEFAULT_PORT = 48920
DEFAULT_BATCH_SIZE = 30
DEFAULT_DELAY_MS = 1000


def find_bridge_token(cfg_root=None):
    """查找注入插件生成的授权 token。"""
    candidates = [
        os.path.join(RUN, 'bridge_token.txt'),
        os.path.join(os.path.expanduser('~'), '.qqchatcache', 'run', 'bridge_token.txt'),
        r'D:\QQChatCache\run\bridge_token.txt',
        os.path.join(os.path.dirname(__file__), '..', 'extension', 'qq-digest-bridge', 'bridge_token.txt')
    ]
    if cfg_root:
        candidates.insert(0, os.path.join(cfg_root, 'run', 'bridge_token.txt'))

    for c in candidates:
        if os.path.exists(c):
            try:
                with open(c, 'r', encoding='utf-8') as f:
                    tk = f.read().strip()
                    if tk:
                        return tk
            except Exception:
                pass
    return ''


def rpc_call(url, payload, token=None, timeout=10):
    """向本地桥接服务发送 HTTP POST 请求。"""
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={
        'Content-Type': 'application/json; charset=utf-8',
        'X-Digest-Token': token or ''
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode('utf-8', 'replace')
        raise RuntimeError('RPC HTTP %d: %s' % (e.code, err_msg))
    except Exception as e:
        raise RuntimeError('RPC 请求失败: %s' % e)


def extract_raw_text(msg_obj):
    """从 NTQQ 返回的 RawMessage 对象中提取纯文本与消息类型。"""
    elements = msg_obj.get('elements') or []
    texts = []
    kind = 'text'
    reply_seq = 0

    for ele in elements:
        ele_type = ele.get('elementType')
        # 1: 文本
        if ele_type == 1 and 'textElement' in ele:
            c = ele['textElement'].get('content', '')
            if c:
                texts.append(c)
        # 2: 图片
        elif ele_type == 2:
            kind = 'image' if kind == 'text' else kind
            texts.append('[图片]')
        # 3: 文件
        elif ele_type == 3:
            kind = 'file'
            fn = ele.get('fileElement', {}).get('fileName', '')
            texts.append('[文件: %s]' % fn if fn else '[文件]')
        # 7: 引用
        elif ele_type == 7:
            kind = 'reply'
            reply_seq = ele.get('replyElement', {}).get('replayMsgSeq', 0)
        # 16: 合并转发
        elif ele_type == 16:
            kind = 'forward'
            texts.append('[转发聊天记录]')

    return kind, ' '.join(texts).strip(), reply_seq


def ingest_messages_into_index(idx, group_code, group_name, raw_msgs):
    """将桥接返回的消息列表直接结构化写入 index.db (INSERT OR IGNORE)。"""
    if not raw_msgs:
        return 0

    rows = []
    for m in raw_msgs:
        # msgId 优先取数字 id 或转 hash
        m_id = m.get('msgId') or m.get('id')
        if isinstance(m_id, str):
            try:
                m_id = int(m_id)
            except ValueError:
                m_id = abs(hash(m_id)) & 0x7FFFFFFFFFFFFFFF

        ts = int(m.get('msgTime') or m.get('time') or 0)
        if not ts:
            continue

        d = local(ts)
        day_str = d.strftime('%Y-%m-%d')
        hour = d.hour

        sender_uid = str(m.get('senderUid') or '')
        sender_qq = int(m.get('senderUin') or 0)
        sender_name = m.get('sendMemberName') or m.get('sendNickName') or ('QQ%d' % sender_qq if sender_qq else '群友')

        direction = int(m.get('direction') or 0)
        msg_type = int(m.get('msgType') or 2)
        subtype = int(m.get('subMsgType') or 0)

        kind, text, reply_seq = extract_raw_text(m)

        rows.append((
            m_id, ts, day_str, hour,
            int(group_code), str(group_name),
            sender_uid, sender_qq, sender_name,
            direction, msg_type, subtype,
            kind, text, reply_seq
        ))

    if rows:
        idx.executemany('INSERT OR IGNORE INTO messages VALUES (%s)' % ','.join('?' * 15), rows)
        idx.commit()

    return len(rows)


def load_crawl_state():
    import ntqq_core
    state_file = os.path.join(ntqq_core.RUN, 'crawl_state.json')
    if os.path.exists(state_file):
        try:
            with open(state_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {'completed_tasks': [], 'last_updated': 0}


def save_crawl_state(state):
    import ntqq_core
    state['last_updated'] = now_ts()
    state_file = os.path.join(ntqq_core.RUN, 'crawl_state.json')
    os.makedirs(os.path.dirname(state_file), exist_ok=True)
    with open(state_file, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def run_crawler(cfg, gaps, bridge_port=DEFAULT_PORT, batch_size=DEFAULT_BATCH_SIZE,
                delay_ms=DEFAULT_DELAY_MS, mock_mode=False):
    """执行主要的爬取编排循环。"""
    token = find_bridge_token(cfg.get('root'))
    bridge_url = 'http://127.0.0.1:%d/pull_batch' % bridge_port

    if not mock_mode:
        log('[crawler] 正在连接本地桥接服务 http://127.0.0.1:%d ...' % bridge_port)
        if not token:
            log('[warn] 未找到 bridge_token.txt，尝试无 Token 请求（若被拒绝请先安装并启动插件）')

    idx = open_index()
    state = load_crawl_state()
    group_names, _, _ = build_names(cfg)

    total_pulled = 0
    total_tasks = len(gaps)

    log('[crawler] 共有 %d 个缺口任务待调度执行' % total_tasks)

    for i, gap in enumerate(gaps, 1):
        task_id = '%s_%s_%s_%s' % (gap.get('group_code'), gap.get('type'), gap.get('start_seq'), gap.get('end_seq'))
        if task_id in state['completed_tasks']:
            log('[crawler] [%d/%d] 任务 %s 已在此前完成，跳过' % (i, total_tasks, task_id))
            continue

        g_code = gap['group_code']
        g_name = gap.get('group_name') or group_names.get(g_code, str(g_code))
        g_type = gap.get('type')
        s_seq = gap.get('start_seq', 0)
        e_seq = gap.get('end_seq', 0)
        needed_cnt = gap.get('count', 30)

        log('[crawler] [%d/%d] 正在补全群 %s (%s) | 类型: %s | seq: %s~%s (约 %d 条)' % (
            i, total_tasks, g_code, g_name, g_type, s_seq, e_seq, needed_cnt
        ))

        # 分页调度计算
        curr_seq = e_seq if e_seq > 0 else s_seq
        remain = needed_cnt if needed_cnt > 0 else 60

        while remain > 0:
            step = min(batch_size, remain)
            payload = {
                'group_code': g_code,
                'start_seq': str(curr_seq),
                'count': step,
                'desc': True,
                'reverse_order': False
            }

            if mock_mode:
                # 模拟测试模式数据
                mock_msgs = []
                for seq_i in range(step):
                    mock_msgs.append({
                        'msgId': '%d%04d' % (curr_seq or 1000, seq_i),
                        'msgTime': now_ts() - seq_i * 60,
                        'senderUin': '10001',
                        'sendNickName': '测试群友',
                        'elements': [{'elementType': 1, 'textElement': {'content': '模拟漫游历史消息 seq=%d' % (curr_seq - seq_i)}}]
                    })
                resp = {'success': True, 'count': len(mock_msgs), 'messages': mock_msgs}
            else:
                retry = 0
                resp = None
                while retry < 3:
                    try:
                        resp = rpc_call(bridge_url, payload, token=token, timeout=15)
                        break
                    except Exception as err:
                        retry += 1
                        wait_sec = retry * 2
                        log('[warn] 请求失败: %s，%d 秒后重试 (%d/3) ...' % (err, wait_sec, retry))
                        time.sleep(wait_sec)

                if not resp or not resp.get('success'):
                    log('[err] 群 %s 批量拉取失败，跳过该段' % g_code)
                    break

            raw_msgs = resp.get('messages') or []
            n_in = ingest_messages_into_index(idx, g_code, g_name, raw_msgs)
            total_pulled += n_in

            log('  -> 成功拉取并入库 %d 条消息 (本群累计 %d 条)' % (n_in, total_pulled))

            remain -= step
            curr_seq = max(1, curr_seq - step)

            # 防风控平滑随机延迟 (800ms ~ 1200ms)
            if not mock_mode and remain > 0:
                jitter = random.uniform(-200, 300) / 1000.0
                sleep_time = max(0.5, (delay_ms / 1000.0) + jitter)
                time.sleep(sleep_time)

        # 登记完成状态
        state['completed_tasks'].append(task_id)
        save_crawl_state(state)

    idx.close()
    log('[crawler] 缺口爬取调度完成！共向 index.db 写入补全 %d 条历史漫游记录。' % total_pulled)
    return total_pulled


def main():
    ap = argparse.ArgumentParser(description='QQ 聊天记录缺口注入爬虫调度控制台')
    ap.add_argument('--gap-file', type=str, default=None, help='指定缺口清单路径（默认读 run/gaps.json）')
    ap.add_argument('--group', type=str, default=None, help='单独指定群号或群名拉取')
    ap.add_argument('--count', type=int, default=50, help='指定拉取的漫游条数')
    ap.add_argument('--port', type=int, default=None, help='桥接服务端口 (默认 48920)')
    ap.add_argument('--delay', type=int, default=None, help='请求间隔毫秒 (默认 1000ms)')
    ap.add_argument('--mock-test', action='store_true', help='模拟测试模式（脱机验证入库与调度）')
    a = ap.parse_args()

    cfg = load_config()
    ensure_dirs()

    crawler_cfg = cfg.get('crawler', {})
    port = a.port or crawler_cfg.get('bridge_port', DEFAULT_PORT)
    delay_ms = a.delay or crawler_cfg.get('request_delay_ms', DEFAULT_DELAY_MS)
    batch_size = crawler_cfg.get('batch_size', DEFAULT_BATCH_SIZE)

    # 1. 确定待爬任务列表
    gaps = []
    if a.group:
        group_names, _, _ = build_names(cfg)
        target_code = int(a.group) if a.group.isdigit() else None
        if not target_code:
            for c, name in group_names.items():
                if a.group.lower() in name.lower():
                    target_code = c
                    break
        if not target_code:
            raise SystemExit('未识别的群: %s' % a.group)

        gaps.append({
            'group_code': target_code,
            'group_name': group_names.get(target_code, str(target_code)),
            'type': 'manual_pull',
            'start_seq': 0,
            'end_seq': 0,
            'count': a.count,
            'time_start': '近期',
            'time_end': '现在',
            'description': '手动触发漫游历史拉取 %d 条' % a.count
        })
    else:
        gap_json = a.gap_file or os.path.join(RUN, 'gaps.json')
        if not os.path.exists(gap_json):
            log('[tip] 未检测到 %s。正在自动先执行一次 ntqq_gap.py 生成缺口清单 ...' % gap_json)
            from ntqq_gap import main as gap_main
            # 临时无参调用
            sys.argv = ['ntqq_gap.py']
            gap_main()

        if os.path.exists(gap_json):
            with open(gap_json, 'r', encoding='utf-8') as f:
                gaps = json.load(f)

    if not gaps:
        log('[crawler] 暂无需要补全的缺口任务！')
        return

    # 2. 运行爬取调度器
    run_crawler(cfg, gaps, bridge_port=port, batch_size=batch_size,
                delay_ms=delay_ms, mock_mode=a.mock_test)


if __name__ == '__main__':
    main()
