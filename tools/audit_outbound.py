#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""出站目标审计：这个工具究竟会连哪些主机。

静态（默认）：扫 scripts 与 tools 下的 .py，提取所有 http(s) URL 的主机名，
并与允许清单对比。允许清单里只有腾讯自家的图床——本工具的设计是「不登录、只取群图」。
运行期（--netstat）：调用 netstat -ano，列出 python / QQ 进程的 TCP 对端地址。

用法：
    python tools/audit_outbound.py
    python tools/audit_outbound.py --netstat
"""
import argparse, os, subprocess, sys
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOTS = [os.path.join(HERE, '..', 'scripts'), HERE]
ALLOW = ('multimedia.nt.qq.com.cn', 'gchat.qpic.cn', 'qpic.cn', 'qq.com')
SKIP_DIRS = ('__pycache__',)


def iter_py():
    for root in ROOTS:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                if fn.endswith('.py'):
                    yield os.path.join(dirpath, fn)


def hosts_in(line):
    out = []
    i = 0
    for scheme in ('https://', 'http://'):
        while True:
            i = line.find(scheme, i)
            if i < 0:
                break
            j = i + len(scheme)
            k = j
            while k < len(line) and line[k] not in ' \t"\'<>),':
                k += 1
            raw = line[i:k]
            try:
                h = urlsplit(raw).hostname
            except Exception:
                h = None
            if looks_like_host(h):
                out.append(h)
            i = k
    return out


def looks_like_host(h):
    if not h or '.' not in h:
        return False
    ok = 'abcdefghijklmnopqrstuvwxyz0123456789.-_'
    return all(c in ok for c in h.lower())


def static():
    found = {}
    for p in iter_py():
        rel = os.path.relpath(p, os.path.join(HERE, '..'))
        with open(p, encoding='utf-8', errors='replace') as fh:
            for n, line in enumerate(fh, 1):
                for h in hosts_in(line):
                    found.setdefault(h, []).append('%s:%d' % (rel, n))
    print('== 静态扫描：代码里出现的所有主机 ==')
    if not found:
        print('  （没有硬编码的 http(s) 主机）')
    bad = []
    for h, locs in sorted(found.items()):
        ok = any(h == a or h.endswith('.' + a) for a in ALLOW)
        print('  %s %-28s %s' % ('OK  ' if ok else '⚠  ', h, ', '.join(locs[:4])))
        if not ok:
            bad.append(h)
    print('')
    print('允许清单: %s' % ', '.join(ALLOW))
    print('结论：%s' % ('⚠ 出现 %d 个清单外主机，需人工确认' % len(bad) if bad
                      else 'OK 全部主机都在允许清单内'))
    return len(bad)


def netstat():
    try:
        out = subprocess.run(['netstat', '-ano'], capture_output=True, text=True,
                             encoding='utf-8', errors='replace', timeout=30).stdout
    except Exception as e:
        print('netstat 跑不起来：%s' % e)
        return
    pids = set()
    try:
        task = subprocess.run(['tasklist'], capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=30).stdout
    except Exception:
        task = ''
    for line in task.splitlines():
        low = line.lower()
        if 'python' in low or low.startswith('qq.exe') or ' qq.exe' in low:
            parts = line.split()
            if parts and parts[-1].isdigit():
                pids.add(parts[-1])
    print('== 运行期 TCP 对端（python / QQ 进程，PID %s）==' % (', '.join(sorted(pids)) or '无'))
    n = 0
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4 or not parts[-1].isdigit():
            continue
        if pids and parts[-1] not in pids:
            continue
        if 'TCP' not in line or 'ESTABLISHED' not in line:
            continue
        print('  %s' % line.strip())
        n += 1
    if not n:
        print('  （没有 ESTABLISHED 连接）')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--netstat', action='store_true', help='额外看运行期连接')
    a = ap.parse_args()
    bad = static()
    if a.netstat:
        print('')
        netstat()
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())

