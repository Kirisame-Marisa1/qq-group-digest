#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQ 源库「没被本工具动过」审计。

设计前提：本工具**只读** QQ 的库，从不写。这个脚本把前提变成可复核的两件事：

  1. 静态（--static）：扫 scripts/*.py，列出任何*可能*写盘的调用
     （open( / os.remove / os.rename / os.replace / shutil. / sqlite3.connect），
     并单独标出同一条语句里出现「源库目录 / 源库文件名」的那些——那些才是真正要盯的。
  2. 动态（--snapshot / --check）：给源库拍 (size, mtime_ns, 前1MB sha256, 末1MB sha256) 快照，
     跑完工具后再比。QQ 是活跃写入的库，**差异不等于我们写的**；脚本只负责把差异摆出来。

用法：
    python tools/audit_qq_untouched.py --static
    python tools/audit_qq_untouched.py --snapshot      # 跑工具前
    python tools/audit_qq_untouched.py --check         # 跑工具后
"""
import argparse, hashlib, json, os, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'scripts'))
from ntqq_core import load_config, src_db, RUN, log, fmt  # noqa: E402

SNAP = os.path.join(RUN, 'qq_untouched.json')
SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'scripts')
WRITE_KEYS = ('open(', 'os.remove', 'os.unlink', 'os.rename', 'os.replace',
              'shutil.', 'sqlite3.connect')
SRC_KEYS = ('src_db', 'nt_db', 'Tencent Files', 'src_db_path')


def static_scan():
    hits = []
    for fn in sorted(os.listdir(SCRIPTS)):
        if not fn.endswith('.py'):
            continue
        p = os.path.join(SCRIPTS, fn)
        try:
            with open(p, encoding='utf-8') as fh:
                lines = fh.read().splitlines()
        except Exception as e:
            log('[static] 读不了 %s: %s' % (fn, e))
            continue
        for i, ln in enumerate(lines, 1):
            if not any(k in ln for k in WRITE_KEYS):
                continue
            s = ln.strip()
            # open(src_db(...), 'rb') 或 mode=ro 为只读打开，不算写源库
            is_ro = ("'rb'" in s or '"rb"' in s or 'mode=ro' in s or 'open_ro(' in s)
            hits.append((fn, i, s, any(k in s for k in SRC_KEYS) and not is_ro))
    return hits


def digest(path):
    st = os.stat(path)
    h1, h2 = hashlib.sha256(), hashlib.sha256()
    with open(path, 'rb') as f:
        h1.update(f.read(1024 * 1024))
        f.seek(max(0, st.st_size - 1024 * 1024))
        h2.update(f.read(1024 * 1024))
    return {'size': st.st_size, 'mtime_ns': st.st_mtime_ns,
            'head': h1.hexdigest()[:16], 'tail': h2.hexdigest()[:16]}


def targets(cfg):
    out = {}
    for name in cfg.get('decrypt', ['nt_msg.db', 'group_info.db', 'profile_info.db']):
        try:
            p = src_db(cfg, name)
        except Exception:
            continue
        if os.path.exists(p):
            out[name] = p
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--static', action='store_true')
    ap.add_argument('--snapshot', action='store_true')
    ap.add_argument('--check', action='store_true')
    a = ap.parse_args()
    if not (a.static or a.snapshot or a.check):
        a.static = a.check = True

    cfg = load_config()
    tgt = targets(cfg)

    if a.static:
        hits = static_scan()
        bad = [h for h in hits if h[3]]
        print('== 静态扫描：scripts/*.py 里可能写盘的调用 %d 处 ==' % len(hits))
        for fn, ln, txt, s in hits:
            print('  %s %s:%d  %s' % ('!!' if s else '  ', fn, ln, txt[:110]))
        print('')
        print('结论：%s' % (('⚠ 有 %d 处写盘调用出现在源库相关上下文，需人工确认' % len(bad))
                          if bad else 'OK 没有任何写盘调用出现在源库路径上（源库只以只读模式打开）'))

    if a.snapshot:
        snap = {'at': int(time.time()), 'files': {}}
        for n, p in tgt.items():
            snap['files'][n] = digest(p)
        os.makedirs(RUN, exist_ok=True)
        with open(SNAP, 'w', encoding='utf-8') as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
        log('[snapshot] %d 个源库 -> %s（%s）' % (len(snap['files']), SNAP, fmt(snap['at'])))

    if a.check:
        if not os.path.exists(SNAP):
            print('没有基线，先跑 --snapshot')
            return
        with open(SNAP, encoding='utf-8') as fh:
            snap = json.load(fh)
        print('== 动态对比（基线 %s）==' % fmt(snap['at']))
        for n, p in tgt.items():
            old = snap['files'].get(n)
            new = digest(p)
            if not old:
                print('  %-16s 基线里没有，跳过' % n)
                continue
            if old == new:
                print('  %-16s OK 完全一致' % n)
            else:
                d = ', '.join('%s: %s -> %s' % (k, old[k], new[k]) for k in new if old.get(k) != new[k])
                print('  %-16s ⚠ 有变化（QQ 在跑时属正常）：%s' % (n, d))
        print('')
        print('提醒：QQ 是活跃写入的库，变化 != 本工具写的。真正的保证来自上面的静态扫描。')


if __name__ == '__main__':
    main()
