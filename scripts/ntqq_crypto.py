#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NTQQ 密钥凭据、DPAPI 保护与明文数据库生命周期管理 (ntqq_crypto)。

职责：
1. Windows DPAPI 凭据本地加密/解密 (dpapi_encrypt, dpapi_decrypt)；
2. 密钥 hex 封装 (protect_key_hex, unwrap_key_hex)；
3. 会话 RKey 凭据缓存与持久化 (load_rkey, save_rkey)；
4. 密钥映射表校验与自动重提 (verify_keymap, ensure_keymap)；
5. 明文库安全解密与按需清理 (ensure_plain, cleanup_plain)。
"""

import base64
import hashlib
import hmac as _hmac
import json
import os
import struct
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

EXT_HEADER_BYTES, PAGE_SIZE, SALT_SIZE, RESERVE_BYTES, IV_BYTES = 1024, 4096, 16, 48, 16
RKEY_TTL = 6 * 3600


def _get_core():
    """按需延迟导入 ntqq_core 避免循环依赖。"""
    import ntqq_core
    return ntqq_core


# ── DPAPI 密钥本地加密保护（Windows） ──────────────────────────────────────
def dpapi_encrypt(raw_bytes: bytes) -> bytes:
    """使用当前 Windows 登录用户的 DPAPI 凭据加密字节流。非 Windows 原样返回。"""
    if os.name != 'nt':
        return raw_bytes
    import ctypes, ctypes.wintypes as wt

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [('cbData', wt.DWORD), ('pbData', ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(raw_bytes, len(raw_bytes))
    blob_in = DATA_BLOB(len(raw_bytes), buf)
    blob_out = DATA_BLOB()
    CRYPTPROTECT_UI_FORBIDDEN = 0x01
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out)
    ):
        raise OSError('DPAPI CryptProtectData failed: %d' % ctypes.GetLastError())
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def dpapi_decrypt(enc_bytes: bytes) -> bytes:
    """使用当前 Windows 登录用户的 DPAPI 凭据解密字节流。非 Windows 原样返回。"""
    if os.name != 'nt':
        return enc_bytes
    import ctypes, ctypes.wintypes as wt

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [('cbData', wt.DWORD), ('pbData', ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(enc_bytes, len(enc_bytes))
    blob_in = DATA_BLOB(len(enc_bytes), buf)
    blob_out = DATA_BLOB()
    CRYPTPROTECT_UI_FORBIDDEN = 0x01
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out)
    ):
        raise OSError('DPAPI CryptUnprotectData failed: %d' % ctypes.GetLastError())
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def protect_key_hex(key_hex: str, use_dpapi: bool = True) -> str:
    """将 64 位 hex 密钥用 DPAPI 封装为 'dpapi:<base64>'；失败或禁用时返回原 hex。"""
    if not key_hex or not use_dpapi or os.name != 'nt' or key_hex.startswith('dpapi:'):
        return key_hex
    try:
        enc = dpapi_encrypt(bytes.fromhex(key_hex))
        return 'dpapi:' + base64.b64encode(enc).decode('ascii')
    except Exception as e:
        core = _get_core()
        core.log_debug('DPAPI 加密密钥失败，回退明文存储: %r' % e)
        return key_hex


def unwrap_key_hex(stored_val: str) -> str:
    """将 'dpapi:<base64>' 或明文 hex 统一还原为 64 位小端 hex 字符串。"""
    if not stored_val:
        return ''
    if stored_val.startswith('dpapi:'):
        enc = base64.b64decode(stored_val[6:])
        return dpapi_decrypt(enc).hex()
    return stored_val


def load_rkey(max_age: int = RKEY_TTL) -> Optional[str]:
    """读缓存的 rkey；不存在或过期返回 None。支持 DPAPI 封装。"""
    core = _get_core()
    rkey_cache = getattr(core, 'RKEY_CACHE', os.path.join(os.path.dirname(core.INDEX_DB), 'rkey.json'))
    try:
        with open(rkey_cache, encoding='utf-8') as f:
            d = json.load(f)
        if core.now_ts() - int(d.get('ts') or 0) <= max_age:
            rk = d.get('rkey') or ''
            if rk.startswith('dpapi:'):
                rk = dpapi_decrypt(base64.b64decode(rk[6:])).decode('utf-8')
            return rk or None
    except Exception as e:
        core.log_debug('读取 rkey 缓存失败: %r' % e)
    return None


def save_rkey(rkey: str) -> None:
    core = _get_core()
    rkey_cache = getattr(core, 'RKEY_CACHE', os.path.join(os.path.dirname(core.INDEX_DB), 'rkey.json'))
    val = rkey
    if val and os.name == 'nt' and not val.startswith('dpapi:'):
        try:
            val = 'dpapi:' + base64.b64encode(dpapi_encrypt(val.encode('utf-8'))).decode('ascii')
        except Exception as e:
            core.log_debug('DPAPI 加密 rkey 失败，回退明文存储: %r' % e)
            val = rkey
    try:
        os.makedirs(os.path.dirname(rkey_cache), exist_ok=True)
        with open(rkey_cache, 'w', encoding='utf-8') as f:
            json.dump({'ts': core.now_ts(), 'rkey': val}, f)
    except Exception as e:
        core.log_warn('写入 rkey 缓存失败: %r' % e)


def verify_keymap(km_path: str, cfg: Optional[dict] = None) -> Tuple[bool, str]:
    """用 page1 的 HMAC 校验缓存密钥是否仍然有效（不需要 QQ 在运行）。"""
    if not os.path.exists(km_path):
        return False, '没有缓存'
    try:
        with open(km_path, encoding='utf-8') as f:
            km = json.load(f)
    except Exception as e:
        return False, '缓存文件损坏: %s' % e
    key_map, salt_files = km.get('key_map', {}), km.get('salt_files', {})
    checked = 0
    for sh, files in salt_files.items():
        kh_raw = key_map.get(sh)
        if not kh_raw:
            return False, '缺少 salt %s 的密钥' % sh[:12]
        try:
            kh = unwrap_key_hex(kh_raw)
        except Exception as e:
            return False, 'DPAPI 解密密钥失败: %s' % e
        fp = files[0] if files else None
        if not fp or not os.path.exists(fp):
            continue
        try:
            with open(fp, 'rb') as f:
                f.seek(EXT_HEADER_BYTES)
                raw = f.read(PAGE_SIZE)
        except OSError:
            continue
        if len(raw) < PAGE_SIZE:
            continue
        salt = raw[:SALT_SIZE]
        if salt.hex() != sh:
            return False, 'salt 已变化（%s）' % os.path.basename(fp)
        page1 = raw
        enc = bytes.fromhex(kh)
        hm = hashlib.pbkdf2_hmac('sha512', enc, bytes(b ^ 0x3a for b in salt), 2, 32)
        de = PAGE_SIZE - RESERVE_BYTES
        content, iv = page1[SALT_SIZE:de], page1[de:de + 16]
        stored = page1[de + 16:de + 16 + 20]
        calc = _hmac.new(hm, content + iv + struct.pack('<I', 1), hashlib.sha1).digest()
        if not _hmac.compare_digest(calc, stored):
            return False, '%s 的密钥校验失败' % os.path.basename(fp)
        checked += 1
    return (True, 'OK (%d 个库)' % checked) if checked else (False, '没有可校验的库')


def ensure_keymap(cfg: dict, force: bool = False) -> str:
    """确保密钥映射表有效并返回路径。"""
    core = _get_core()
    core.ensure_dirs()
    km = os.path.join(core.KEYS, 'key_map.json')
    if os.path.exists(km) and not force:
        ok, why = verify_keymap(km, cfg)
        if ok:
            core.log('[key] 复用缓存密钥（HMAC 校验通过: %s），本步骤无需 QQ 参与' % why)
            return km
        core.log('[key] 缓存密钥失效（%s）-> 需要 QQ 正在运行以重新提取' % why)
    core.log('[key] 从 QQ 进程内存提取密钥 ...')
    try:
        import ntqq_key
        if hasattr(ntqq_key, 'extract_keys'):
            ntqq_key.extract_keys(qq=str(cfg['account']), output=km)
            if os.path.exists(km):
                return km
    except SystemExit:
        raise
    except Exception as e:
        core.log_debug('进程内调用 ntqq_key.extract_keys 失败，回退子进程: %r' % e)
    py = sys.executable
    cmd = [py, os.path.join(core.HERE, 'ntqq_key.py'), '--qq', str(cfg['account']), '-o', km]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if r.returncode != 0 or not os.path.exists(km):
        core.log(r.stdout or '')
        core.log(r.stderr or '')
        raise SystemExit('取密钥失败：请确认 QQ 已登录并正在运行')
    for line in (r.stdout or '').splitlines():
        if 'recovered' in line or 'scanned' in line:
            core.log('  ' + line.strip())
    return km


def ensure_plain(cfg: dict, km: str, name: str, force: bool = False) -> str:
    """确保 data/plain/<name> 存在且不比源文件旧。返回路径。"""
    core = _get_core()
    src = core.src_db(cfg, name)
    if not os.path.exists(src):
        raise SystemExit('源库不存在: %s' % src)
    dst = os.path.join(core.PLAIN, name)
    if os.path.exists(dst) and not force:
        if os.path.getmtime(dst) >= os.path.getmtime(src):
            return dst
    part = dst + '.part'
    for stale in (part,):
        try:
            if os.path.exists(stale):
                os.remove(stale)
        except OSError:
            pass
    try:
        import ntqq_decrypt
        if hasattr(ntqq_decrypt, 'decrypt_db'):
            ntqq_decrypt.decrypt_db(key_map_path=km, db_name=name, out_path=part)
            if os.path.exists(part):
                os.replace(part, dst)
                return dst
    except SystemExit:
        try:
            if os.path.exists(part):
                os.remove(part)
        except OSError:
            pass
        raise
    except Exception as e:
        core.log_debug('进程内调用 ntqq_decrypt.decrypt_db 失败，回退子进程: %r' % e)
    cmd = [sys.executable, os.path.join(core.HERE, 'ntqq_decrypt.py'),
           '--key-map', km, '--db', name, '--out', part]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding='utf-8', errors='replace', bufsize=1)
    tail = []
    for line in p.stdout:
        line = line.rstrip('\r\n')
        if line.startswith('@@PROGRESS') or line.startswith('src='):
            print(line, flush=True)
        else:
            tail.append(line)
            if len(tail) > 30:
                tail.pop(0)
    p.wait()
    if p.returncode != 0 or not os.path.exists(part):
        for line in tail:
            core.log(line)
        try:
            os.remove(part)
        except OSError:
            pass
        raise SystemExit('解密失败: %s' % name)
    for line in tail:
        if line.startswith('done') or 'size=' in line:
            core.log('  ' + line.strip())
    os.replace(part, dst)
    return dst


def cleanup_plain(cfg: dict) -> int:
    """keep_plain=false 时删掉最大的明文缓存 nt_msg.db。返回释放的字节数。"""
    core = _get_core()
    freed = 0
    for suffix in ('', '-wal', '-shm', '-journal'):
        p = os.path.join(core.PLAIN, 'nt_msg.db' + suffix)
        if os.path.exists(p):
            try:
                freed += os.path.getsize(p)
                os.remove(p)
            except OSError:
                pass
    if freed:
        core.log('[plain] 已清理明文缓存 nt_msg.db（释放 %.1f GB）' % (freed / 1073741824.0))
    return freed
