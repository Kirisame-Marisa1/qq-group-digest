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
import argparse, contextlib, os, re, sqlite3, sys, tempfile, time, json, glob, hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.request, urllib.error
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ntqq_core import (open_index, load_config, now_ts, gchat_image_url, sniff_ext,
                       MEDIA, log, log_debug, log_warn, set_log_level,
                       load_rkey, save_rkey, db_image_url, qq_path)

UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024


def _safe_getsize(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return -1


def _write_atomic_bytes(path, data):
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=parent, prefix='.tmp_', suffix='.part')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise


def _read_limited(resp, max_bytes=MAX_DOWNLOAD_BYTES):
    data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError('响应体超过 %d MB 上限' % (max_bytes // (1024 * 1024)))
    return data


def pic_root(cfg):
    """本机图片缓存目录（nt_data\Pic）。QQ 数据目录可配置，走 core 的 qq_path。"""
    return qq_path(str(cfg.get('account') or ''), 'nt_qq', 'nt_data', 'Pic')


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
        return max(t or c, key=_safe_getsize)
    return pick


def fetch(md5, dst_dir, retries=2):
    """旧路径：按 md5 走免鉴权的 gchat。返回 (状态, 路径或原因)。"""
    for a in range(retries + 1):
        try:
            req = urllib.request.Request(gchat_image_url(md5), headers=UA)
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = _read_limited(resp)
            ext = sniff_ext(data)
            if not ext:
                return 'badformat', data[:8].hex()
            sub = os.path.join(dst_dir, md5[:2])
            path = os.path.join(sub, md5 + ext)
            _write_atomic_bytes(path, data)
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
    path = os.path.join(sub, md5 + ext)
    _write_atomic_bytes(path, data)
    return path


def find_cached(dst_dir, md5):
    """如果之前已经下载到目标目录，直接返回已存路径。"""
    sub = os.path.join(dst_dir, md5[:2])
    if os.path.isdir(sub):
        for ext in ('.jpg', '.png', '.gif', '.webp', '.bmp'):
            p = os.path.join(sub, md5 + ext)
            if os.path.isfile(p) and os.path.getsize(p) > 0:
                return p
    return None


def fetch_db(host, path, rkey, md5, dst_dir, retries=1):
    """主力路径：多媒体 CDN 原图（spec=0）+ 内存 rkey，并校验 md5。"""
    try:
        url = db_image_url(host, path, rkey)
    except ValueError as e:
        return 'badurl', str(e)[:80]
    for a in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = _read_limited(resp)
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


def pick_rkey(samples, max_candidates=30):
    """samples=[(host,path,md5)]；返回第一个能取到 md5 一致原图的 rkey，并写缓存。

    先试缓存里的，再扫内存；每个候选只拿前 2 张样本试，通不过就换下一个。
    """
    cands = []
    cached = load_rkey()
    if cached:
        cands.append(cached)
    try:
        from ntqq_key import scan_rkeys
        cands += scan_rkeys(limit=max_candidates)
    except Exception as e:
        log('[media] 扫描 rkey 失败（QQ 没开？）: %s' % str(e)[:80])
    seen = set()
    for r in cands:
        if not r or r in seen:
            continue
        seen.add(r)
        if len(seen) > max_candidates:
            break
        for host, path, md5 in samples[:2]:
            try:
                url = db_image_url(host, path, r)
            except ValueError:
                continue
            try:
                req = urllib.request.Request(url, headers=UA)
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = _read_limited(resp)
            except urllib.error.HTTPError as e:
                if e.code in (400, 403):
                    break  # 当前 rkey 鉴权失败，直接换下一个 rkey
                continue   # 404 等单张失效继续试下一个样本
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
    ap.add_argument('--ocr', action='store_true', help='下载完成后自动调用 Windows 原生 OCR 识别并建立全文索引')
    ap.add_argument('--verbose', action='store_true', help='显示调试日志')
    a = ap.parse_args()
    if a.verbose:
        set_log_level(True)

    cfg = load_config()
    out_root = cfg.get('media_cache_dir') or MEDIA
    os.makedirs(out_root, exist_ok=True)
    cut = now_ts() - int(a.days * 86400)
    with contextlib.closing(open_index()) as idx:
        idx.row_factory = sqlite3.Row
        want = [int(x.strip()) for x in (a.groups or '').split(',') if x.strip().isdigit()] if (a.groups and a.groups != 'all') else []
        if a.groups and a.groups != 'all' and want:
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
        cached = find_cached(out_root, h)
        if cached:
            return r, 'cached', cached
        try:
            host, pth = r['host'] or '', r['path'] or ''
        except (IndexError, KeyError):
            host = pth = ''
        # 默认优先走多媒体 CDN 原图；若显式指定 --prefer-local 则先查本机 Pic 缓存
        if not a.prefer_local and host and pth and rkey:
            st, val = fetch_db(host, pth, rkey, r['md5'], out_root)
            if st == 'ok':
                return r, 'db', val
        # 本机 Pic 缓存查找（prefer_local 优先，或 CDN 失败后降级兜底）
        lp = pick_local(h)
        if lp:
            return r, 'local', lp
        # 若之前未尝试过 CDN（例如 prefer_local 但本地没有），尝试 CDN
        if a.prefer_local and host and pth and rkey:
            st, val = fetch_db(host, pth, rkey, r['md5'], out_root)
            if st == 'ok':
                return r, 'db', val
        # 兜底旧 gchat 路径
        return r, *fetch(h, out_root)

    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        futs = {ex.submit(work, r): r for r in tasks}
        done = 0
        for f in as_completed(futs):
            orig_r = futs[f]
            try:
                r, st, val = f.result()
            except Exception as exc:
                r, st, val = orig_r, 'error', str(exc)[:80]
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
    ok = stats.get('ok', 0) + stats.get('local', 0) + stats.get('db', 0) + stats.get('cached', 0)
    log('[media] 完成：成功 %d / %d = %.1f%%  用时 %.1f 分钟' % (ok, len(tasks), 100.0*ok/max(1,len(tasks)), (time.time()-t0)/60))
    log('[media] 明细: %s' % stats)
    log('[media] 清单: %s' % mpath)

    if getattr(a, 'ocr', False):
        try:
            from ntqq_ocr import index_media_ocr
            import ntqq_core
            db_path = ntqq_core.INDEX_DB
            ocr_count = index_media_ocr(db_path, out_root)
            log('[media] OCR 索引完成：新识别 %d 张图片' % ocr_count)
        except Exception as e:
            log_warn('[media] OCR 索引异常: %r' % e)


if __name__ == '__main__':
    main()
