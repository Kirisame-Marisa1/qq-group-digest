#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQ 群图片批量获取：三级策略，优先本地，其次走多媒体 CDN + 内存 rkey。

关键发现（2026-10-02 实测，取代了早先「按 md5 走 gchat」的单一路径）：

  1. 本机 `nt_data\\Pic\\`：只有你点开/渲染过的图才落盘，实测命中率 ~13%。
  2. **多媒体 CDN（主力）**：消息元素里就存着
        host  = 45816  → multimedia.nt.qq.com.cn
        path  = 45802/45803/45804 → /download?appid=1407&fileid=…&spec={0,720,198}
     其中 spec=0 是原图。**但这条 URL 不带 rkey，直接请求返回 HTTP 400。**
     rkey 是 QQ 客户端进程内存里的会话凭据，用 ntqq_key.scan_rkeys() 只读扫描拿到，
     拼成 `https://<host><path>&rkey=<rkey>` 即可。实测 **25/25 张 md5 与消息声明一致**，
     且元素里 45518 字段声明有效期 2678400 秒（31 天）。
  3. 旧 gchat（兜底）：https://gchat.qpic.cn/gchatpic_new/0/0-0-<MD5大写>/0
     无鉴权，服务端对较新的图也会 404，实测今天这批只有 ~21% 能取到。

读取内存只做 OpenProcess + ReadProcessMemory，不注入、不写内存；rkey 缓存 6 小时。
"""
import argparse, os, re, sqlite3, sys, time, json, glob, hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.request, urllib.error
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ntqq_core import (open_index, load_config, now_ts, fmt, local,
                       gchat_image_url, sniff_ext, MEDIA, log,
                       load_rkey, save_rkey, db_image_url)

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
    """旧路径：按 md5 走免鉴权的 gchat。返回 (状态, 路径或原因)。"""
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


def _save(data, dst_dir, md5):
    ext = sniff_ext(data)
    if not ext:
        return None
    sub = os.path.join(dst_dir, md5[:2])
    os.makedirs(sub, exist_ok=True)
    path = os.path.join(sub, md5 + ext)
    with open(path, 'wb') as f:
        f.write(data)
    return path


def fetch_db(host, path, rkey, md5, dst_dir, retries=1):
    """主力路径：多媒体 CDN 原图（spec=0）+ 内存 rkey，并校验 md5。"""
    url = db_image_url(host, path, rkey)
    for a in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = resp.read()
            if hashlib.md5(data).hexdigest() != md5.lower():
                return 'mismatch', 'md5 不一致(%d 字节)' % len(data)
            p = _save(data, dst_dir, md5)
            return ('ok', p) if p else ('badformat', data[:8].hex())
        except urllib.error.HTTPError as e:
            if e.code in (400, 403) and a < retries:
                time.sleep(1.0)
                continue
            return 'http%s' % e.code, str(e)[:60]
        except Exception as e:
            if a < retries:
                time.sleep(1.0)
                continue
            return 'error', str(e)[:80]
    return 'error', 'retries exhausted'


def pick_rkey(samples):
    """samples=[(host,path,md5)]；返回第一个能取到 md5 一致原图的 rkey，并写缓存。

    先试缓存里的，再扫内存；每个候选只拿前 2 张样本试，通不过就换下一个。
    """
    cands = []
    cached = load_rkey()
    if cached:
        cands.append(cached)
    try:
        from ntqq_key import scan_rkeys
        cands += scan_rkeys()
    except Exception as e:
        log('[media] 扫描 rkey 失败（QQ 没开？）: %s' % str(e)[:80])
    seen = set()
    for r in cands:
        if not r or r in seen:
            continue
        seen.add(r)
        for host, path, md5 in samples[:2]:
            try:
                req = urllib.request.Request(db_image_url(host, path, r), headers=UA)
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = resp.read()
            except Exception:
                continue
            if hashlib.md5(data).hexdigest() == md5.lower():
                save_rkey(r)
                log('[media] rkey 命中（长度 %d），后续走多媒体 CDN' % len(r))
                return r
    log('[media] 没找到可用 rkey，回退到旧 gchat 路径')
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=float, default=30)
    ap.add_argument('--groups', default=None, help='逗号分隔群号；all=全部')
    ap.add_argument('--limit', type=int, default=0, help='最多处理多少张（0=不限）')
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--prefer-local', action='store_true', default=False,
                    help='先查本机已落盘文件（快，但往往只是缩略图）；默认先走多媒体 CDN 取原图')
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

    # 先在「本机没有、但元素里带 host+path」的图上定一个可用 rkey（只试前几张）
    samples = []
    for r in tasks:
        try:
            host, pth = r['host'], r['path']
        except (IndexError, KeyError):
            host = pth = None
        if host and pth and not (a.prefer_local and pick_local((r['md5'] or '').lower())):
            samples.append((host, pth, r['md5']))
        if len(samples) >= 4:
            break
    rkey = pick_rkey(samples) if samples else None

    def work(r):
        h = r['md5'].lower()
        try:
            host, pth = r['host'] or '', r['path'] or ''
        except (IndexError, KeyError):
            host = pth = ''
        # 默认先走多媒体 CDN：拿到的是逐张校验过 md5 的原图；
        # 本机 Pic 命中虽快，但常只是缩略图（实测 16 张里 15 张 md5 与声明不符）。
        if not a.prefer_local and host and pth and rkey:
            st, val = fetch_db(host, pth, rkey, r['md5'], out_root)
            if st == 'ok':
                return r, 'db', val
        if a.prefer_local:
            lp = pick_local(h)
            if lp:
                return r, 'local', lp
        if host and pth and rkey:
            st, val = fetch_db(host, pth, rkey, r['md5'], out_root)
            if st == 'ok':
                return r, 'db', val
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
    ok = stats.get('ok', 0) + stats.get('local', 0) + stats.get('db', 0)
    log('[media] 完成：成功 %d / %d = %.1f%%  用时 %.1f 分钟' % (ok, len(tasks), 100.0*ok/max(1,len(tasks)), (time.time()-t0)/60))
    log('[media] 明细: %s' % stats)
    log('[media] 清单: %s' % mpath)


if __name__ == '__main__':
    main()
