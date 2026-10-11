#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NTQQ SQLCipher 密钥提取（只扫内存拿 key，不解密）
算法与常量来自 NapNeko/qq_dump_db (MIT) 的 dump_qq_key_auto.py，
已按其源码逐行审计：无 eval/exec/subprocess/socket/网络调用，
仅使用 kernel32 的 OpenProcess + VirtualQueryEx + ReadProcessMemory 做只读扫描。
本脚本相对原版做的唯一删减：去掉「自动解密所有 db」那一步（原版会一次性把 5.4GB 读进内存）。
"""
import ctypes, ctypes.wintypes as wt
from ctypes import windll, byref, sizeof, create_string_buffer
import struct, hashlib, hmac as hmac_mod, os, sys, json, argparse, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ntqq_core import load_config, qq_path  # noqa: E402

PROCESS_VM_READ           = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
TH32CS_SNAPPROCESS        = 0x00000002
TH32CS_SNAPMODULE         = 0x00000008
TH32CS_SNAPMODULE32       = 0x00000010
MEM_COMMIT                = 0x1000
READABLE_PROTS            = {0x02, 0x04, 0x06, 0x08, 0x20, 0x40, 0x60, 0x80}
MAX_REGION                = 256 * 1024 * 1024

EXT_HEADER, PAGE_SIZE, SALT_SIZE, KEY_SIZE = 1024, 4096, 16, 32
IV_SIZE, HMAC_SIZE, RESERVE, HMAC_MASK = 16, 20, 48, 0x3a
KDF_ITER, FAST_ITER = 4000, 2
PRE_LOGIN_KEY = b"BD156D6710D54D8782F4"
HEX_CHARS = set(b'0123456789abcdefABCDEF')


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [('BaseAddress', ctypes.c_void_p), ('AllocationBase', ctypes.c_void_p),
                ('AllocationProtect', wt.DWORD), ('RegionSize', ctypes.c_size_t),
                ('State', wt.DWORD), ('Protect', wt.DWORD), ('Type', wt.DWORD)]

class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [('dwSize', wt.DWORD), ('cntUsage', wt.DWORD), ('th32ProcessID', wt.DWORD),
                ('th32DefaultHeapID', ctypes.POINTER(ctypes.c_ulong)), ('th32ModuleID', wt.DWORD),
                ('cntThreads', wt.DWORD), ('th32ParentProcessID', wt.DWORD),
                ('pcPriClassBase', ctypes.c_long), ('dwFlags', wt.DWORD),
                ('szExeFile', ctypes.c_char * 260)]

class MODULEENTRY32(ctypes.Structure):
    _fields_ = [('dwSize', wt.DWORD), ('th32ModuleID', wt.DWORD), ('th32ProcessID', wt.DWORD),
                ('GlblcntUsage', wt.DWORD), ('ProccntUsage', wt.DWORD),
                ('modBaseAddr', ctypes.POINTER(ctypes.c_byte)), ('modBaseSize', wt.DWORD),
                ('hModule', wt.HMODULE), ('szModule', ctypes.c_char * 256),
                ('szExePath', ctypes.c_char * 260)]


def find_qq_pids():
    snap = windll.kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap in (ctypes.c_void_p(-1).value, -1):
        return []
    pe = PROCESSENTRY32(); pe.dwSize = sizeof(PROCESSENTRY32); pids = []
    if windll.kernel32.Process32First(snap, byref(pe)):
        while True:
            if pe.szExeFile.decode('utf-8', 'replace').lower() == 'qq.exe':
                pids.append(pe.th32ProcessID)
            if not windll.kernel32.Process32Next(snap, byref(pe)):
                break
    windll.kernel32.CloseHandle(snap)
    return pids


def pid_has_module(pid, name):
    snap = windll.kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if snap in (ctypes.c_void_p(-1).value, -1, 0):
        return False
    me = MODULEENTRY32(); me.dwSize = sizeof(MODULEENTRY32); found = False
    if windll.kernel32.Module32First(snap, byref(me)):
        while True:
            if me.szModule.decode('utf-8', 'replace').lower() == name.lower():
                found = True; break
            if not windll.kernel32.Module32Next(snap, byref(me)):
                break
    windll.kernel32.CloseHandle(snap)
    return found


def find_qq_main_pid():
    pids = find_qq_pids()
    if not pids:
        return None
    print('QQ PIDs: %s' % pids)
    for pid in pids:
        if pid_has_module(pid, 'wrapper.node'):
            print('  PID %d has wrapper.node -> main process' % pid)
            return pid
    print('  WARNING: wrapper.node not found, fallback to %d' % pids[0])
    return pids[0]


def read_mem(handle, addr, size):
    buf = create_string_buffer(size); n = ctypes.c_size_t(0)
    if windll.kernel32.ReadProcessMemory(handle, ctypes.c_void_p(addr), buf, size, byref(n)):
        return buf.raw[:n.value]
    return None


def db_dir(qq):
    """nt_db 目录。QQ 数据目录装 QQ 时可能被改到别的盘，所以读 config.json 的
    qq_data_dir；配置里的 account 优先，为空时回退命令行的 --qq。"""
    try:
        qq = str(load_config().get('account') or qq)
    except Exception:
        pass
    return qq_path(qq, 'nt_qq', 'nt_db')


def collect_db_info(d):
    pa_map = {}
    for fn in sorted(os.listdir(d)):
        if not fn.lower().endswith('.db'):
            continue
        fp = os.path.join(d, fn)
        try:
            with open(fp, 'rb') as f:
                hdr = f.read(EXT_HEADER + PAGE_SIZE)
        except (OSError, PermissionError):
            continue
        if len(hdr) < EXT_HEADER + PAGE_SIZE:
            continue
        if hdr[:16] != b'SQLite header 3\x00':
            continue
        page1 = hdr[EXT_HEADER:EXT_HEADER + PAGE_SIZE]
        pa_map.setdefault(page1[:SALT_SIZE].hex(), []).append((fp, page1))
    return pa_map


def verify_key_hmac(page1, enc_key):
    salt = page1[:SALT_SIZE]
    hmac_salt = bytes(b ^ HMAC_MASK for b in salt)
    hmac_key = hashlib.pbkdf2_hmac('sha512', enc_key, hmac_salt, FAST_ITER, KEY_SIZE)
    data_end = PAGE_SIZE - RESERVE
    content = page1[SALT_SIZE:data_end]
    iv = page1[data_end:data_end + IV_SIZE]
    stored = page1[data_end + IV_SIZE:data_end + IV_SIZE + HMAC_SIZE]
    computed = hmac_mod.new(hmac_key, content + iv + struct.pack('<I', 1), hashlib.sha1).digest()
    return hmac_mod.compare_digest(computed, stored)


def derive_enc_key(passphrase, salt):
    return hashlib.pbkdf2_hmac('sha512', passphrase, salt, KDF_ITER, KEY_SIZE)


def scan_rkeys(limit=400):
    """只读扫描 QQ 进程内存，提取图片下载用的 rkey 候选（按出现次数降序）。

    实测（2026-10-02）：图片元素里只有 host + /download?appid=…&spec=0，**不带 rkey**，
    直接请求返回 HTTP 400；把内存里扫到的 rkey 拼上去、取 spec=0 才能拿到原图，
    md5 与消息声明一致。这里只做 OpenProcess / ReadProcessMemory，不注入、不写内存。
    """
    import re as _re
    from collections import Counter as _Counter
    pid = find_qq_main_pid()
    handle = windll.kernel32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not handle:
        print('  [rkey] OpenProcess 失败 err=%d' % ctypes.GetLastError())
        return []
    cnt = _Counter()
    scanned = 0
    t0 = time.time()
    mbi = MEMORY_BASIC_INFORMATION(); addr = 0
    try:
        while True:
            if windll.kernel32.VirtualQueryEx(handle, ctypes.c_void_p(addr), byref(mbi), sizeof(mbi)) == 0:
                break
            base = mbi.BaseAddress or 0
            size = mbi.RegionSize
            if (mbi.State == MEM_COMMIT and (mbi.Protect & 0xFF) in READABLE_PROTS
                    and 0 < size <= MAX_REGION):
                data = read_mem(handle, base, size)
                if data:
                    scanned += len(data)
                    off = 0
                    while True:
                        i = data.find(b'&rkey=', off)
                        if i == -1:
                            break
                        m = _re.match(rb'&rkey=([A-Za-z0-9_\-]{10,200})', data[i:i + 210])
                        if m:
                            cnt[m.group(1).decode()] += 1
                        off = i + 1
            nxt = base + size
            if nxt <= addr or nxt >= 0x7FFFFFFFFFFF:
                break
            addr = nxt
    finally:
        windll.kernel32.CloseHandle(handle)
    print('  [rkey] 扫描 %.0f MB / %.1fs，候选 %d 个'
          % (scanned / 1048576.0, time.time() - t0, len(cnt)))
    return [k for k, _ in cnt.most_common(limit)]


def scan_by_salt(handle, salt_hexes):
    needles = [(sh.encode('ascii'), sh) for sh in salt_hexes]
    results = {}
    total = 0; t0 = time.time()
    mbi = MEMORY_BASIC_INFORMATION(); addr = 0
    while True:
        ret = windll.kernel32.VirtualQueryEx(handle, ctypes.c_void_p(addr), byref(mbi), sizeof(mbi))
        if ret == 0:
            break
        base = mbi.BaseAddress or 0; size = mbi.RegionSize
        if (mbi.State == MEM_COMMIT and (mbi.Protect & 0xFF) in READABLE_PROTS and 0 < size <= MAX_REGION):
            data = read_mem(handle, base, size)
            if data:
                total += len(data)
                for needle, sh in needles:
                    if sh in results:
                        continue
                    off = 0
                    while True:
                        idx = data.find(needle, off)
                        if idx == -1:
                            break
                        if idx >= 66 and idx + 33 <= len(data):
                            if (data[idx - 66:idx - 64] == b"x'" and
                                    data[idx + 32:idx + 33] == b"'" and
                                    all(c in HEX_CHARS for c in data[idx - 64:idx])):
                                results[sh] = data[idx - 64:idx].decode('ascii').lower()
                                break
                        off = idx + 1
        nxt = base + size
        if nxt <= addr or nxt >= 0x7FFFFFFFFFFF:
            break
        addr = nxt
    print('  scanned %.0f MB in %.1fs -> %d/%d salts matched' %
          (total / 1048576.0, time.time() - t0, len(results), len(salt_hexes)))
    return results


def extract_keys(qq, pid=0, output=None, use_dpapi=True):
    """从运行中的 QQ 进程只读扫描提取 SQLCipher 密钥，校验 HMAC 后保存（默认 DPAPI 加密）。"""
    for _s in (sys.stdout, sys.stderr):          # 控制台可能是 GBK，别让中文输出崩掉
        try:
            _s.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from ntqq_core import protect_key_hex, log

    pid = pid or find_qq_main_pid()
    if not pid:
        log('ERROR: no QQ.exe running')
        raise SystemExit(1)

    d = db_dir(str(qq))
    if not os.path.isdir(d):
        log('ERROR: db dir not found: %s' % d)
        raise SystemExit(1)
    pa_map = collect_db_info(d)
    log('per-account: %d files, %d unique salts' % (sum(len(v) for v in pa_map.values()), len(pa_map)))
    if not pa_map:
        log('ERROR: no NTQQ db found')
        raise SystemExit(1)

    print('@@PROGRESS|{"stage":"key","pct":0,"detail":"扫描进程内存"}', flush=True)
    handle = windll.kernel32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not handle:
        log('ERROR: OpenProcess failed, GetLastError=%d' % ctypes.GetLastError())
        raise SystemExit(1)
    try:
        hits = scan_by_salt(handle, list(pa_map.keys()))
    finally:
        windll.kernel32.CloseHandle(handle)

    key_map = {}; names = {}
    for sh, kh in hits.items():
        page1 = pa_map[sh][0][1]
        if verify_key_hmac(page1, bytes.fromhex(kh)):
            key_map[sh] = protect_key_hex(kh, use_dpapi=use_dpapi)
            names[sh] = [os.path.basename(x[0]) for x in pa_map[sh]]
        else:
            log('  HMAC mismatch for salt %s' % sh)

    unresolved = [sh for sh in pa_map if sh not in key_map]
    log('recovered %d/%d salts' % (len(key_map), len(pa_map)))
    for sh in unresolved:
        log('  UNRESOLVED %s (%s)' % (sh, ', '.join(os.path.basename(x[0]) for x in pa_map[sh])))

    out = {'qq': str(qq), 'pid': pid, 'generated': time.strftime('%Y-%m-%d %H:%M:%S'),
           'dpapi_protected': bool(use_dpapi and os.name == 'nt'),
           'key_map': key_map, 'salt_files': {sh: [x[0] for x in pa_map[sh]] for sh in key_map}}
    if output:
        os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
        with open(output, 'w', encoding='utf-8') as f:
            json.dump(out, f, indent=2, ensure_ascii=False)
        log('written: %s (dpapi=%s)' % (output, out['dpapi_protected']))
    print('@@PROGRESS|{"stage":"key","pct":100,"detail":"%d 个密钥"}' % len(key_map), flush=True)

    log('\n--- salt -> files ---')
    for sh in key_map:
        log('  %s: %s' % (sh[:16] + '..', ', '.join(names[sh])))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--qq', required=True)
    ap.add_argument('--pid', type=int, default=0)
    ap.add_argument('-o', '--output', required=True)
    ap.add_argument('--plain-key', action='store_true', help='不使用 Windows DPAPI 加密，直接保存明文 hex 密钥')
    a = ap.parse_args()
    extract_keys(qq=a.qq, pid=a.pid, output=a.output, use_dpapi=(not a.plain_key))


if __name__ == '__main__':
    main()

