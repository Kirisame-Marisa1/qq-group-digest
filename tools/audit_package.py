#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第三方包 / 压缩包「装之前」审计（隔离区流程用）。

输入：目录、.whl、.zip、.tar.gz、.tgz。全部在内存里读，解包产物不落盘。
检查项（都按功能分组，给命中位置）：

  A 安装期/启动期执行：package.json 的 install 钩子、Python 的 .pth（解释器每次启动都会执行）、setup.py
  B 可疑调用        ：eval / exec / compile / __import__ / os.system / subprocess / pickle.loads
  C 外联目标        ：代码里的 http(s) 主机
  D 凭据访问        ：os.environ / .ssh / credentials / token / api_key
  E 写盘与删除      ：open( / shutil.rmtree / os.remove / os.unlink

用法：
    python tools/audit_package.py /path/to/pkg.whl
    python tools/audit_package.py /path/to/unpacked_dir

⚠ 这是启发式检查，**报无命中不等于安全**。真正的结论要人看一遍 A 和 B 的命中。
"""
import argparse, os, sys, tarfile, zipfile

LIMIT = 2 * 1024 * 1024          # 单个成员超过 2MB 只列名不看内容
HOOK = ('postinstall', 'preinstall', 'prepublish', 'prepare')
RISKY = ('eval(', 'exec(', 'compile(', '__import__(', 'os.system', 'subprocess', 'pickle.loads')
CRED = ('os.environ', '.ssh', 'credentials', 'api_key', 'token')
WRITE = ('open(', 'shutil.rmtree', 'os.remove', 'os.unlink')


def entries(path):
    if os.path.isdir(path):
        for dp, dn, fns in os.walk(path):
            for fn in fns:
                p = os.path.join(dp, fn)
                yield os.path.relpath(p, path), open(p, 'rb')
    elif zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                if n.endswith('/'):
                    continue
                yield n, z.open(n)
    elif tarfile.is_tarfile(path):
        with tarfile.open(path) as t:
            for m in t.getmembers():
                if not m.isfile():
                    continue
                yield m.name, t.extractfile(m)
    else:
        raise SystemExit('不认识这个输入：%s' % path)


def scan(path):
    hits = {k: [] for k in ('A', 'B', 'C', 'D', 'E')}
    nfiles = nbytes = 0
    for name, fh in entries(path):
        nfiles += 1
        low = name.lower()
        if low.endswith('.pth') or low.endswith('setup.py'):
            hits['A'].append('%s  (文件本身就是执行入口)' % name)
        try:
            data = fh.read(LIMIT + 1) if hasattr(fh, 'read') else b''
        finally:
            try:
                fh.close()
            except Exception:
                pass
        nbytes += len(data)
        if len(data) > LIMIT:
            continue
        try:
            text = data.decode('utf-8')
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            if low.endswith('package.json') and any(k in s for k in HOOK):
                hits['A'].append('%s:%d  %s' % (name, i, s[:100]))
            for k, keys in (('B', RISKY), ('C', ('http://', 'https://')),
                            ('D', CRED), ('E', WRITE)):
                if any(x in s for x in keys):
                    hits[k].append('%s:%d  %s' % (name, i, s[:100]))
    return hits, nfiles, nbytes


TITLES = {
    'A': 'A 安装期/启动期执行（最需要人看）',
    'B': 'B 可疑调用',
    'C': 'C 外联目标',
    'D': 'D 凭据访问',
    'E': 'E 写盘与删除',
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('path')
    ap.add_argument('--show', type=int, default=8, help='每组最多列几条（默认 8）')
    a = ap.parse_args()
    hits, nf, nb = scan(a.path)
    print('== 审计 %s ==' % a.path)
    print('   成员 %d 个，读入 %.1f MB' % (nf, nb / 1048576.0))
    print('')
    for k in ('A', 'B', 'C', 'D', 'E'):
        v = hits[k]
        print('-- %s：%d 处 --' % (TITLES[k], len(v)))
        for x in v[:a.show]:
            print('   %s' % x)
        if len(v) > a.show:
            print('   ...（还有 %d 处）' % (len(v) - a.show))
        print('')
    verdict = '⚠ 先人工看完 A / B 再决定' if (hits['A'] or hits['B']) else '启发式检查无命中（不等于安全）'
    print('结论：%s' % verdict)


if __name__ == '__main__':
    main()

