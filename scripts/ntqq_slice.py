#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在加密库上「按需解密」直接抽行，不落地全量明文库。

原理：SQLite 的 b-tree 里，
  - 内部页（interior）只占极少数，格式极简（4字节子页号 + varint 键）
  - 叶子页（leaf）的每个 cell 就是一个完整记录（溢出页补齐）
所以：解密 page1 -> 读 sqlite_master -> 拿到目标表根页
      -> 只走内部页，列出全部叶子页号
      -> 逐页解密（用完即丢）+ 解析 cell + 解 record
即可把指定窗口的行直接写进索引，全程不产生 4GB 中间文件。
"""
import os, struct, sys, time
try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError:
    Cipher = algorithms = modes = None

EXT_HEADER, PAGE_SIZE, SALT, RESERVE, IV_SIZE = 1024, 4096, 16, 48, 16


def varint(buf, i):
    v = 0
    for k in range(9):
        b = buf[i + k]
        if k == 8:
            return (v << 8) | b, i + 9
        v = (v << 7) | (b & 0x7F)
        if not (b & 0x80):
            return v, i + k + 1
    return v, i + 9


class Src:
    """按需解密的页读取器（带小 LRU，不落盘）。"""

    def __init__(self, path, key, cache=512):
        self.f = open(path, 'rb')
        self.key = key
        self.size = os.path.getsize(path)
        self.npages = max(0, (self.size - EXT_HEADER) // PAGE_SIZE)
        self.cache = {}
        self.order = []
        self.cap = cache
        self.hits = 0
        self.miss = 0
        self.bad_pages = 0
        self.bad_rows = 0

    def _dec(self, body):
        if Cipher is None:
            from cryptography.hazmat.primitives.ciphers import Cipher as _C, algorithms as _A, modes as _M
        else:
            _C, _A, _M = Cipher, algorithms, modes
        enc = body[:len(body) - RESERVE]
        iv = body[len(body) - RESERVE:len(body) - RESERVE + IV_SIZE]
        d = _C(_A.AES(self.key), _M.CBC(iv)).decryptor()
        return d.update(enc) + d.finalize()

    def page(self, pno):
        """pno 是 SQLite 的 1-based 页号。"""
        i = pno - 1
        p = self.cache.get(i)
        if p is not None:
            self.hits += 1
            return p
        self.miss += 1
        self.f.seek(EXT_HEADER + i * PAGE_SIZE)
        raw = self.f.read(PAGE_SIZE)
        if len(raw) < PAGE_SIZE:
            raw += b'\x00' * (PAGE_SIZE - len(raw))
        if pno == 1:
            pt = self._dec(raw[SALT:])
            p = b'SQLite format 3\x00' + pt
        else:
            p = self._dec(raw)
        if len(p) < PAGE_SIZE:
            p += b'\x00' * (PAGE_SIZE - len(p))
        if len(self.order) >= self.cap:
            self.cache.pop(self.order.pop(0), None)
        self.cache[i] = p
        self.order.append(i)
        return p

    def close(self):
        self.f.close()


class Db:
    def __init__(self, src):
        self.s = src
        h = self.s.page(1)
        assert h[:16] == b'SQLite format 3\x00', '不是 SQLite 页（密钥或偏移不对）'
        self.page_size = int.from_bytes(h[16:18], 'big')
        self.reserved = h[20]
        self.usable = self.page_size - self.reserved
        self.tables = {}          # name -> (root, sql)

    # ── 页头 ──
    def hdr(self, i):
        p = self.s.page(i)
        off = 100 if i == 1 else 0
        t = p[off]
        n = int.from_bytes(p[off + 3:off + 5], 'big')
        hs = 12 if t in (2, 5) else 8
        cells = []
        for k in range(n):
            cells.append(int.from_bytes(p[off + hs + 2 * k: off + hs + 2 * k + 2], 'big'))
        right = int.from_bytes(p[off + 8:off + 12], 'big') if t in (2, 5) else 0
        return t, n, cells, right, off

    def leaves(self, root):
        """只走内部页，列出该表/索引的全部叶子页号（1-based）。"""
        out = []
        stack = [root]
        seen = set()
        while stack:
            i = stack.pop()
            if i in seen or i <= 0 or i > self.s.npages:
                continue
            seen.add(i)
            t, n, cells, right, off = self.hdr(i)
            p = self.s.page(i)
            if t == 5:                      # 内部表页
                for c in cells:
                    stack.append(int.from_bytes(p[c:c + 4], 'big'))
                if right:
                    stack.append(right)
            elif t == 13:                   # 叶子表页
                out.append(i)
            elif t == 2:                    # 内部索引页
                for c in cells:
                    stack.append(int.from_bytes(p[c:c + 4], 'big'))
                if right:
                    stack.append(right)
            elif t == 10:
                out.append(i)
        return out

    def overflow(self, first, need):
        buf = b''
        pg = first
        guard = 0
        while pg and len(buf) < need:
            guard += 1
            if guard > 20000:
                raise ValueError('overflow chain too long')
            p = self.s.page(pg)
            nxt = int.from_bytes(p[:4], 'big')
            buf += p[4: self.usable]
            pg = nxt
        return buf[:need]

    def page_info(self, pno):
        p = self.s.page(pno)
        off = 100 if pno == 1 else 0
        t = p[off]
        n = int.from_bytes(p[off + 3:off + 5], 'big')
        hs = 12 if t in (2, 5) else 8
        cells = [int.from_bytes(p[off + hs + 2 * k: off + hs + 2 * k + 2], 'big') for k in range(n)]
        right = int.from_bytes(p[off + 8:off + 12], 'big') if t in (2, 5) else 0
        return p, t, n, cells, right, off

    def _payload(self, p, c):
        i = c
        plen, i = varint(p, i)
        rowid, i = varint(p, i)
        maxlocal = self.usable - 35
        if plen <= maxlocal:
            return rowid, p[i:i + plen]
        # SQLite btreeComputePayloadSize：surplus 只有在 <= maxLocal 时才是本地长度
        minlocal = ((self.usable - 12) * 32 // 255) - 23
        surplus = minlocal + (plen - minlocal) % (self.usable - 4)
        local = surplus if surplus <= maxlocal else minlocal
        head = p[i:i + local]
        ovf = int.from_bytes(p[i + local:i + local + 4], 'big')
        return rowid, head + self.overflow(ovf, plen - local)

    def rows(self, root):
        """单遍遍历（每页只解一次），产出 (rowid, payload)。坏页跳过并计数。"""
        stack = [root]
        seen = set()
        while stack:
            pg = stack.pop()
            if pg <= 0 or pg > self.s.npages or pg in seen:
                continue
            seen.add(pg)
            try:
                p, t, n, cells, right, off = self.page_info(pg)
            except Exception:
                self.s.bad_pages += 1
                continue
            if t == 13:
                for c in cells:
                    try:
                        yield self._payload(p, c)
                    except Exception:
                        self.s.bad_rows += 1
            elif t == 5:
                for c in cells:
                    try:
                        stack.append(int.from_bytes(p[c:c + 4], 'big'))
                    except Exception:
                        self.s.bad_pages += 1
                if right:
                    stack.append(right)
            elif t == 10:
                for c in cells:
                    try:
                        yield self._idx_payload(p, c)
                    except Exception:
                        self.s.bad_rows += 1

    def iter_decoded(self, root, want):
        """产出 (rowid, [只含 want 槽位的值])。坏行跳过。"""
        for rowid, pl in self.rows(root):
            try:
                yield rowid, decode_some(pl, want)
            except Exception:
                self.s.bad_rows += 1

    def _idx_payload(self, p, c):
        """索引叶子 cell：返回原始 payload（不含 rowid 解析）。"""
        i = c
        plen, i = varint(p, i)
        return plen, i


def decode_record(buf):
    """SQLite record -> 值列表。"""
    hl, j = varint(buf, 0)
    types = []
    while j < hl:
        t, j = varint(buf, j)
        types.append(t)
    vals = []
    k = hl
    for t in types:
        if t == 0:
            vals.append(None)
        elif t == 1:
            vals.append(struct.unpack('>b', buf[k:k + 1])[0]); k += 1
        elif t == 2:
            vals.append(struct.unpack('>h', buf[k:k + 2])[0]); k += 2
        elif t == 3:
            v = struct.unpack('>i', buf[k:k + 3] + b'\x00')[0]; k += 3; vals.append(v)
        elif t == 4:
            vals.append(struct.unpack('>i', buf[k:k + 4])[0]); k += 4
        elif t == 5:
            v = struct.unpack('>q', buf[k:k + 6] + b'\x00\x00')[0]; k += 6; vals.append(v)
        elif t == 6:
            vals.append(struct.unpack('>q', buf[k:k + 8])[0]); k += 8
        elif t == 7:
            vals.append(struct.unpack('>d', buf[k:k + 8])[0]); k += 8
        elif t == 8:
            vals.append(0)
        elif t == 9:
            vals.append(1)
        elif t >= 12 and t % 2 == 0:
            n = (t - 12) // 2; vals.append(buf[k:k + n]); k += n
        elif t >= 13:
            n = (t - 13) // 2; vals.append(buf[k:k + n].decode('utf-8', 'replace')); k += n
        else:
            vals.append(None)
    return vals


def stype_size(t):
    if t == 0 or t in (8, 9):
        return 0
    if t <= 4:
        return t
    if t == 5:
        return 6
    if t in (6, 7):
        return 8
    if t >= 12 and t % 2 == 0:
        return (t - 12) // 2
    if t >= 13:
        return (t - 13) // 2
    return 0


def stype_val(buf, k, t):
    if t == 0:
        return None
    if t == 1:
        return struct.unpack('>b', buf[k:k + 1])[0]
    if t == 2:
        return struct.unpack('>h', buf[k:k + 2])[0]
    if t == 3:
        return struct.unpack('>i', b'\x00' + buf[k:k + 3])[0]
    if t == 4:
        return struct.unpack('>i', buf[k:k + 4])[0]
    if t == 5:
        return struct.unpack('>q', b'\x00\x00' + buf[k:k + 6])[0]
    if t == 6:
        return struct.unpack('>q', buf[k:k + 8])[0]
    if t == 7:
        return struct.unpack('>d', buf[k:k + 8])[0]
    if t == 8:
        return 0
    if t == 9:
        return 1
    if t >= 12 and t % 2 == 0:
        n = (t - 12) // 2
        return buf[k:k + n]
    if t >= 13:
        n = (t - 13) // 2
        return buf[k:k + n].decode('utf-8', 'replace')
    return None


def decode_some(buf, want):
    """只解 want 里的列：{表内列序号(0-based): 输出槽位}。其余列只跳过不解码。"""
    if not want:
        return []
    hl, j = varint(buf, 0)
    maxpos = max(want)
    out = [None] * (max(want.values()) + 1)
    k = hl
    pos = 0
    # 只走到需要的最后一列为止：不解列、不建列表、不遍历后面 17 列
    while j < hl and pos <= maxpos:
        t, j = varint(buf, j)
        if pos in want:
            out[want[pos]] = stype_val(buf, k, t)
        k += stype_size(t)
        pos += 1
    return out


def structure_of(db):
    """等价于 ntqq_core.live_structure()，但不需要明文库。"""
    out = {}
    for name, (root, sql) in db.tables.items():
        if name.endswith('_fts') or '_fts_' in name:
            continue
        try:
            out[name] = sorted(cols_of(sql or ''))
        except Exception:
            out[name] = []
    return out


def load_master(db):
    """读 sqlite_master（page1 就是它的根）。"""
    for rowid, payload in db.rows(1):
        v = decode_record(payload)
        if len(v) >= 5 and v[0] == 'table':
            db.tables[v[1]] = (v[3], v[4])


def cols_of(sql):
    """从 CREATE TABLE 语句里按顺序取列名。"""
    import re
    i = sql.find('(')
    if i < 0:
        return []
    depth = 1
    cur = ''
    parts = []
    for ch in sql[i + 1:]:
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                parts.append(cur); break
        if ch == ',' and depth == 1:
            parts.append(cur); cur = ''
        else:
            cur += ch
    out = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        m = re.match(r'^"([^"]+)"', p) or re.match(r'^\[([^\]]+)\]', p) or re.match(r'^([A-Za-z_][\w]*)', p)
        if m:
            nm = m.group(1)
            if nm.upper() in ('PRIMARY', 'UNIQUE', 'CHECK', 'FOREIGN', 'CONSTRAINT'):
                continue
            out.append(nm)
    return out


if __name__ == '__main__':
    import json, argparse
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from ntqq_core import unwrap_key_hex, KEYS as KEYS_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument('--key-map', default=os.path.join(KEYS_DIR, 'key_map.json'))
    ap.add_argument('--db', default='nt_msg.db')
    ap.add_argument('--probe', action='store_true')
    a = ap.parse_args()
    with open(a.key_map, encoding='utf-8') as fh:
        km = json.load(fh)
    src = key = None
    for sh, files in km['salt_files'].items():
        for f in files:
            if os.path.basename(f) == a.db:
                src, key = f, bytes.fromhex(unwrap_key_hex(km['key_map'][sh]))
    if not src:
        print('key_map 里没有', a.db); sys.exit(1)
    print('src =', src)
    t0 = time.time()
    s = Src(src, key)
    print('页数 %d，大小 %.2f GB' % (s.npages, s.size / 1024 ** 3))
    db = Db(s)
    print('page_size=%d reserved=%d usable=%d  耗时 %.2fs' % (db.page_size, db.reserved, db.usable, time.time() - t0))
    load_master(db)
    print('表 %d 个' % len(db.tables))
    for n, (root, sql) in db.tables.items():
        if n in ('group_msg_table',) or 'group' in n:
            print('  %-28s root=%-8d 列数=%d' % (n, root, len(cols_of(sql or ''))))
    if a.probe:
        root, sql = db.tables.get('group_msg_table', (None, None))
        print()
        print('group_msg_table 列（前 20）:', cols_of(sql)[:20])
        t0 = time.time()
        lv = db.leaves(root)
        print('叶子页 %d 个（%.1f%% 的库），耗时 %.1fs' % (len(lv), 100.0 * len(lv) / s.npages, time.time() - t0))
        t0 = time.time()
        n = 0
        for _ in db.rows(root):
            n += 1
            if n >= 2000:
                break
        print('前 %d 行读取耗时 %.2fs（页缓存命中 %d / 未命中 %d）' % (n, time.time() - t0, s.hits, s.miss))
