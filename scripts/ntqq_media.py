#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQ 群图片批量获取：优先用本机已落盘文件，缺的走腾讯 CDN 直连下载。

关键发现（2026-10-02 实测）：
  群图可以按 md5 直接取原图，**不需要登录、cookie、签名**：
      https://gchat.qpic.cn/gchatpic_new/0/0-0-<MD5大写>/0
  实测 200 返回的字节数与消息里声明的 filesize 完全一致（是原图），
  失败全是 404（服务端已清理），成功率约 80%。
  → 所以不需要用户「点开」图片也能拿到内容。
"""
import argparse, os, re, sqlite3, sys, time, json, glob
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.request, urllib.error
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ntqq_core import (open_index, load_config, now_ts, fmt, local,
                       gchat_image_url, sniff_ext, MEDIA, log)

UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}


def pic_root(cfg):
    return os.path.join(os.path.expandvars(r'%USERPROFILE%\Documents\Tencent Files'),
                        str(cfg.get('account') or ''), 'nt_qq', 'nt_data', 'Pic')


def build_local_index(root):
    idx = {}
    for p in glob.iglob(os.path.join(root, '**', '*'), recursive=True):
        if not os.path.isfile(p):
            continue
        m = re.match(r'^([0-9a-fA-F]{32})', os.path.basename(p))
        if m:
            idx.setdefault(m.group(1).lower(), []).append(p)

    def pick(h):
        c = idx.get(h, [])
        if not c:
            return None
        t = [x for x in c if '_0.' in x]
        return min(t or c, key=os.path.getsize)
    return pick


def fetch(md5, dst_dir, retries=2):
    """返回 (状态, 路径或原因)。"""
    for a in range(retries + 1):
        try:
            req = urllib.request.Request(gchat_image_url(md5), headers=UA)
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = resp.read()
            ext = sniff_ext(data)
            if not ext:
                return 'badformat', data[:8].hex()
            sub = os.path.join(dst_dir, md5[:2])
            os.makedirs(sub, exist_ok=True)
            path = os.path.join(sub, md5 + ext)
            with open(path, 'wb') as f:
                f.write(data)
            return 'ok', path
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return 'expired', 'HTTP 404'
            if a < retries:
                time.sleep(1.5 * (a + 1)); continue
            return 'http%s' % e.code, str(e)
        except Exception as e:
            if a < retries:
                time.sleep(1.5 * (a + 1)); continue
            return 'error', str(e)[:80]
    return 'error', 'retries exhausted'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=float, default=30)
    ap.add_argument('--groups', default=None, help='逗号分隔群号；all=全部')
    ap.add_argument('--limit', type=int, default=0, help='最多处理多少张（0=不限）')
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--prefer-local', action='store_true', default=True)
    ap.add_argument('--manifest', default=None)
    a = ap.parse_args()

    cfg = load_config()
    out_root = cfg.get('media_cache_dir') or MEDIA
    os.makedirs(out_root, exist_ok=True)
    idx = open_index(); idx.row_factory = sqlite3.Row
    cut = now_ts() - int(a.days * 86400)

    if a.groups and a.groups != 'all':
        want = [int(x) for x in a.groups.split(',')]
        q = 'SELECT * FROM media WHERE ts>=? AND group_code IN (%s) ORDER BY ts DESC' % ','.join('?' * len(want))
        rows = idx.execute(q, [cut] + want).fetchall()
    else:
        rows = idx.execute('SELECT * FROM media WHERE ts>=? ORDER BY ts DESC', (cut,)).fetchall()
    seen = set()
    tasks = []
    for r in rows:
        h = (r['md5'] or '').lower()
        if not h or h in seen:
            continue
        seen.add(h)
        tasks.append(r)
    if a.limit:
        tasks = tasks[:a.limit]
    log('[media] 窗口 %d 天，待处理唯一图片 %d 张 -> %s' % (a.days, len(tasks), out_root))
    if not tasks:
        return

    pick_local = build_local_index(pic_root(cfg))
    stats = {}
    manifest = []
    t0 = time.time()

    def work(r):
        h = r['md5'].lower()
        if a.prefer_local:
            lp = pick_local(h)
            if lp:
                return r, 'local', lp
        return r, *fetch(h, out_root)

    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        futs = {ex.submit(work, r): r for r in tasks}
        done = 0
        for f in as_completed(futs):
            r, st, val = f.result()
            stats[st] = stats.get(st, 0) + 1
            manifest.append({'md5': r['md5'], 'status': st, 'path': val,
                             'ts': r['ts'], 'group_code': r['group_code'],
                             'group_name': r['group_name'], 'sender': r['sender_name'],
                             'declared_size': r['declared_size']})
            done += 1
            if done % 200 == 0:
                log('   %d/%d  %.1f 分钟  %s' % (done, len(tasks), (time.time()-t0)/60, stats))

    mpath = a.manifest or os.path.join(out_root, 'manifest.jsonl')
    with open(mpath, 'w', encoding='utf-8') as f:
        for m in manifest:
            f.write(json.dumps(m, ensure_ascii=False) + chr(10))
    ok = stats.get('ok', 0) + stats.get('local', 0)
    log('[media] 完成：成功 %d / %d = %.1f%%  用时 %.1f 分钟' % (ok, len(tasks), 100.0*ok/max(1,len(tasks)), (time.time()-t0)/60))
    log('[media] 明细: %s' % stats)
    log('[media] 清单: %s' % mpath)


if __name__ == '__main__':
    main()
