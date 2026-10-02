#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQ 群聊自动总结工具 —— 公共库。

职责：路径/配置、schema 指纹校验、SQLCipher 解密编排、私有表读取、
      40800 protobuf 解码、昵称/群名解析、行级容错游标。
"""
import json, os, re, sqlite3, struct, subprocess, sys, time
from datetime import datetime, timedelta, timezone

# ── 目录布局 ──────────────────────────────────────────────────────────────
#   <ROOT>/
#     app/        代码（scripts/、schema/、config.json）
#     data/       解密后的明文库（plain/）+ 滚动索引 index.db
#     keys/       密钥缓存
#     media/      下载到的图片
#     knowledge/  关键词库等长期知识
#     output/     总结产出
#     run/        临时文件
#     logs/
HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)                    # .../app
CONFIG = os.path.join(APP, 'config.json')
SCHEMA_DIR = os.path.join(APP, 'schema')


def _read_config_raw():
    try:
        with open(CONFIG, encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return {}


def _default_root():
    """数据根目录的默认值：Windows 用 D:/QQChatCache，其他平台用 ~/.qqchatcache。

    以前硬写 D:/QQChatCache，在 Linux/macOS 上等于往根目录里塞文件，所以改成按平台取。
    任何情况下都建议自己在 config.json 里写死 root。
    """
    if os.name == 'nt':
        return 'D:/QQChatCache'
    return os.path.expanduser('~/.qqchatcache')


_raw = _read_config_raw()
ROOT = str(_raw.get('root') or _default_root()).replace('\\', '/')

DATA = os.path.join(ROOT, 'data')
PLAIN = os.path.join(DATA, 'plain')
INDEX_DB = os.path.join(DATA, 'index.db')
KEYS = os.path.join(ROOT, 'keys')
MEDIA = os.path.join(ROOT, 'media')
KNOW = os.path.join(ROOT, 'knowledge')
GLOSSARY = os.path.join(KNOW, 'glossary')
OUTPUT = os.path.join(ROOT, 'output')
RUN = os.path.join(ROOT, 'run')
LOGS = os.path.join(ROOT, 'logs')
CACHE = DATA                                   # 兼容旧名
TZ = timezone(timedelta(hours=8))              # 群聊按北京时间理解
EXT_HEADER_BYTES, PAGE_SIZE, SALT_SIZE, RESERVE_BYTES, IV_BYTES = 1024, 4096, 16, 48, 16

DEFAULT_CONFIG = {
    "root": "",                       # 空 = 用 _default_root()（Windows: D:/QQChatCache）
    "account": "",
    "qq_exe": "C:/Program Files/Tencent/QQNT/QQ.exe",
    "index_days": 30,
    "decrypt": ["nt_msg.db", "group_info.db", "profile_info.db"],
    "class_groups": [],
    "glossary_autoupdate": True,
}


def log(*a):
    print(*a, flush=True)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG):
        with open(CONFIG, encoding='utf-8') as f:
            cfg.update(json.load(f))
    return cfg


def save_config(cfg):
    with open(CONFIG, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def ensure_dirs():
    for d in (RUN, PLAIN, DATA, KEYS, MEDIA, KNOW, GLOSSARY, OUTPUT, LOGS):
        os.makedirs(d, exist_ok=True)


# ── schema ────────────────────────────────────────────────────────────────
def find_schema():
    gms = sorted(f for f in os.listdir(SCHEMA_DIR) if f.startswith('ntqq-') and f.endswith('.json'))
    if not gms:
        raise SystemExit('no schema file in %s' % SCHEMA_DIR)
    return os.path.join(SCHEMA_DIR, gms[-1])


def load_schema():
    with open(find_schema(), encoding='utf-8') as f:
        return json.load(f)


def live_structure(con):
    """返回库的表/列结构，用于版本漂移检测。"""
    out = {}
    for name, in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE '%_fts%'"):
        try:
            cols = sorted(c[1] for c in con.execute('PRAGMA table_info("%s")' % name))
        except sqlite3.DatabaseError:
            cols = []
        out[name] = cols
    return out


def check_schema(con, sc):
    """把当前库结构与 schema 文件里记录的结构做比对；返回 (ok, 问题列表)。"""
    want = sc.get('structure')
    if not want:
        return True, ['schema 文件未记录 structure，跳过版本漂移校验']
    got = live_structure(con)
    problems = []
    for t, cols in want.items():
        if t not in got:
            problems.append('表缺失: %s' % t)
            continue
        missing = [c for c in cols if c not in got[t]]
        if missing:
            problems.append('表 %s 缺列: %s' % (t, ','.join(missing)))
    if not problems:
        return True, []
    return False, problems


# ── 时间 ──────────────────────────────────────────────────────────────────
def now_ts():
    return int(time.time())


def local(ts):
    return datetime.fromtimestamp(ts, TZ)


def fmt(ts, pat='%m-%d %H:%M'):
    return local(ts).strftime(pat)


def parse_when(s):
    """支持 '2026-10-02'、'2026-10-02 15:00'、'-24h'、'-90m'、'-7d'。"""
    now = datetime.now(TZ)
    s = (s or '').strip()
    m = re.fullmatch(r'-(\d+)([mhd])', s)
    if m:
        n, u = int(m.group(1)), m.group(2)
        delta = {'m': timedelta(minutes=n), 'h': timedelta(hours=n), 'd': timedelta(days=n)}[u]
        return int((now - delta).timestamp())
    for pat in ('%Y-%m-%d %H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            d = datetime.strptime(s, pat)
            return int(d.replace(tzinfo=TZ).timestamp())
        except ValueError:
            continue
    raise SystemExit('无法理解的时间: %r' % s)


# ── protobuf wire 解码 ────────────────────────────────────────────────────
def _varint(d, i):
    v = 0; s = 0
    while i < len(d):
        b = d[i]; i += 1
        v |= (b & 0x7F) << s
        if b < 0x80:
            return v, i
        s += 7
    raise ValueError('bad varint')


def pb_fields(d):
    """返回 [(field_number, wire_type, value)]，value 为 int 或 bytes。"""
    out = []; i = 0
    while i < len(d):
        k, i = _varint(d, i)
        num, wt = k >> 3, k & 7
        if wt == 0:
            v, i = _varint(d, i); out.append((num, 0, v))
        elif wt == 2:
            l, i = _varint(d, i)
            if i + l > len(d):
                raise ValueError('truncated')
            out.append((num, 2, d[i:i + l])); i += l
        elif wt == 5:
            out.append((num, 5, d[i:i + 4])); i += 4
        elif wt == 1:
            out.append((num, 1, d[i:i + 8])); i += 8
        else:
            raise ValueError('wire type %d' % wt)
    return out


def _s(v):
    if isinstance(v, bytes):
        try:
            return v.decode('utf-8')
        except Exception:
            return ''
    return ''


def decode_body(blob, sc):
    """40800 -> (kind, text)。kind 取 text/image/file/sticker/reply/forward/system/..."""
    if not blob:
        return 'empty', ''
    B = sc['body_40800']
    try:
        top = pb_fields(blob)
    except Exception:
        return 'other', ''
    parts = []
    kinds = []
    for n, w, v in top:
        if n != B['body_content'] or w != 2:
            continue
        try:
            seg = dict()
            segs = pb_fields(v)
        except Exception:
            continue
        f = {}
        for a, b, c in segs:
            f.setdefault(a, []).append(c)
        ct = f.get(B['content_type'], [0])[0]
        text = _s(f.get(B['text'], [b''])[0]).strip()
        if not text:
            for key in ('sys_content', 'reply_summary', 'video_text', 'call_desc', 'filename'):
                vv = f.get(B[key])
                if vv:
                    t = _s(vv[0]).strip()
                    if t:
                        text = t; break
        kind = classify(ct, f, sc)
        kinds.append(kind)
        if text:
            parts.append(text)
        if kind in ('file', 'video'):
            fn = _s(f.get(B['filename'], [b''])[0]).strip()
            if fn and fn != text:
                parts.append('[' + fn + ']')
    txt = ' | '.join(parts).strip()
    # 类型优先级：文本 > 引用 > 系统 > 媒体
    for pref in ('text', 'reply', 'forward', 'file', 'video', 'image', 'sticker', 'audio', 'call', 'system'):
        if pref in kinds:
            return pref, txt
    return (kinds[0] if kinds else 'other'), txt


# 45002 = 消息段内容类型（本机样本实测）
#   10 = 新版 QQ 的「嵌套转发/卡片」段。实测这类叶子条目**在本地库里没有内容**
#        （既没有 45101 文本，也没有子 40800/40900），所以它只能落到 other、正文为空。
#        全库转发子条目里这类约占 3%，属于**数据本身缺失**，不是解码问题。
CT_KIND = {1: 'text', 2: 'image', 3: 'file', 6: 'sticker', 7: 'reply',
           8: 'system', 10: 'other', 16: 'forward', 5: 'video', 4: 'audio'}


def classify(ct, f, sc):
    """按 45002 判定类型；缺失时用结构字段兜底。"""
    k = CT_KIND.get(ct or 0)
    if k == 'file':
        name = (_s(f.get(45402, [b''])[0]) + _s(f.get(45419, [b''])[0])).lower()
        if name.endswith(('.mp4', '.mov', '.mkv', '.avi', '.wmv', '.flv', '.m4v')):
            return 'video'
        return 'file'
    if k:
        return k
    # ct 缺失/未知 -> 结构兜底
    if 45101 in f:
        return 'text'
    if 45402 in f or 45405 in f:
        return 'file'
    if 45526 in f or 45802 in f:
        return 'image'
    if 47601 in f or 47602 in f:
        return 'sticker'
    if 47402 in f or 47403 in f or 47413 in f:
        return 'reply'
    if 48601 in f or 48602 in f:
        return 'forward'
    if 48210 in f or 48501 in f or 48503 in f or 80900 in f:
        return 'system'
    if 48151 in f or 48153 in f:
        return 'call'
    return 'other'


# ── 转发消息展开（40900） ────────────────────────────────────────────────
# 实测：nt_msg.db 的 40900 列是 repeated 结构，每一条子记录就是一条被转发的原始消息，
# 字段编号与外层主表一致（40020 发送者 uid / 40033 QQ / 40050 时间 / 40093,40094 昵称 / 40800 正文）。
# 一条转发可以含几十条消息（实测 71700 字节 / 约 55 条）。
FWD_FIELD = '40900'


def forward_text(f, sc):
    """从转发子记录里取出正文与类型。

    踩过的坑（2026-10-02 实测）：一条 40900 子记录的结构是
        {40001, 40020, 40050, 40093(昵称), 40800: <重复多段>}
    而 **40800 是本层的 repeated**，它的每个叶子条目才是「消息段」：
        45002=1 文本 → 正文在 45101
        45002=2 图片 → 正文在 45402（文件名）
        45002=7 引用 → 只有 47413 引用摘要，正文在**兄弟**条目里
    早期版本写成 `f.get(40800)[0]` 再丢给 decode_body，取到的往往是
    「类型 7 的引用段」或「类型 2 的图片段」，于是转发正文**整段丢失**——
    实测某 60 条聚合转发解出 10 条、条条 text 为空，而原文其实是完整的。
    """
    B = sc['body_40800']
    parts, kinds = [], []
    for seg in f.get(40800, []):
        try:
            s = {}
            for a, b, c in pb_fields(seg):
                s.setdefault(a, []).append(c)
        except Exception:
            continue
        ct = s.get(B['content_type'], [0])[0]
        t = _s(s.get(B['text'], [b''])[0]).strip()
        if not t:
            for key in ('sys_content', 'reply_summary', 'video_text', 'call_desc', 'filename'):
                vv = s.get(B[key])
                if vv:
                    tt = _s(vv[0]).strip()
                    if tt:
                        t = tt
                        break
        k = classify(ct, s, sc)
        # 引用段本身没正文，它的正文在兄弟条目里；只有整条别无内容时才退回引用摘要
        if k != 'reply':
            kinds.append(k)
        if t:
            parts.append(t)
    if not parts:
        # 全部叶子都没正文时，才退回引用摘要/第一条的类型
        for seg in f.get(40800, []):
            try:
                s = {}
                for a, b, c in pb_fields(seg):
                    s.setdefault(a, []).append(c)
            except Exception:
                continue
            t = _s(s.get(B['reply_summary'], [b''])[0]).strip()
            if t:
                parts.append('引用: ' + t)
                break
    txt = ' | '.join(parts).strip()
    for pref in ('text', 'forward', 'file', 'video', 'image', 'sticker', 'audio', 'call', 'system'):
        if pref in kinds:
            return pref, txt
    return (kinds[0] if kinds else 'other'), txt


def decode_forward(blob, sc, limit=60):
    if not blob:
        return []
    out = []
    try:
        for n, w, v in pb_fields(blob):
            if n != 40900 or w != 2:
                continue
            try:
                f = {}
                for a, b, c in pb_fields(v):
                    f.setdefault(a, []).append(c)
            except Exception:
                continue
            kind, text = forward_text(f, sc)
            out.append({
                'uid': _s(f.get(40020, [b''])[0]),
                'qq': f.get(40033, [0])[0],
                'ts': f.get(40050, [0])[0],
                'kind': kind,
                'text': text,
                'name': (_s(f.get(40094, [b''])[0]) or _s(f.get(40093, [b''])[0])
                         or _s(f.get(40090, [b''])[0])),
            })
            if len(out) >= limit:
                break
    except Exception:
        pass
    return out


def format_forward(items, per_line=400):
    """把转发内容排成可读文本。

    per_line 的来历：早期是 140 字符，实测**会截掉转发正文**——今天 47 个群的报告里
    有 81 处转发正文被 `…` 砍掉（转发是这些群的主力形式，砍掉就等于丢内容）。
    改成 400 后，长通知类转发基本完整；索引体积增加约 0.5%，可以接受。
    """
    if not items:
        return ''
    lines = ['[转发聊天记录 %d 条]' % len(items)]
    for m in items:
        t = (m['text'] or '').replace(chr(10), ' ')
        if len(t) > per_line:
            t = t[:per_line] + '…'
        who = m['name'] or (('QQ%d' % m['qq']) if m['qq'] else (m['uid'][:10] or '?'))
        when = ('[' + local(m['ts']).strftime('%Y-%m-%d %H:%M') + '] ') if m['ts'] and m['ts'] > 1e9 else ''
        lines.append('  %s%s: %s' % (when, who, t))
    return chr(10).join(lines)


# ── 图片直连下载（不需要用户点开） ────────────────────────────────────────
# 实测：群图可以直接按 md5 从腾讯 CDN 取原图，不需要登录/cookie/签名。
#   https://gchat.qpic.cn/gchatpic_new/0/0-0-<MD5大写>/0
# 返回的字节数与消息里声明的 filesize 完全一致；失败全是 404（服务端已清理），实测成功率约 80%。
def gchat_image_url(md5_hex):
    return 'https://gchat.qpic.cn/gchatpic_new/0/0-0-%s/0' % md5_hex.upper()


# ── rkey 缓存（2026-10-02）──────────────────────────────────────────────────
# 实测：图片元素里存着 host(45816) + /download?appid=1407&fileid=…&spec=0(45802/3/4)，
# 但**不带 rkey**，直接请求返回 HTTP 400。rkey 是客户端进程内存里的会话凭据，
# 用 ntqq_key.scan_rkeys() 只读扫描即可拿到；拼上去、取 spec=0 就能拿到原图
# （实测 25/25 张 md5 与消息声明完全一致）。
# rkey 会过期，缓存 6 小时后强制重扫。
RKEY_CACHE = os.path.join(os.path.dirname(INDEX_DB), 'rkey.json')
RKEY_TTL = 6 * 3600


def load_rkey(max_age=RKEY_TTL):
    """读缓存的 rkey；不存在或过期返回 None。"""
    try:
        with open(RKEY_CACHE, encoding='utf-8') as f:
            d = json.load(f)
        if now_ts() - int(d.get('ts') or 0) <= max_age:
            return d.get('rkey') or None
    except Exception:
        pass
    return None


def save_rkey(rkey):
    try:
        with open(RKEY_CACHE, 'w', encoding='utf-8') as f:
            json.dump({'rkey': rkey, 'ts': now_ts()}, f)
    except Exception:
        pass


def db_image_url(host, path, rkey):
    """拼多媒体 CDN 的原图地址：https://<host><path>&rkey=<rkey>"""
    return 'https://%s%s&rkey=%s' % (host, path, rkey)


def sniff_ext(data):
    if data[:3] == b'\xff\xd8\xff':
        return '.jpg'
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return '.png'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return '.gif'
    if data[:4] == b'RIFF':
        return '.webp'
    if data[:2] == b'BM':
        return '.bmp'
    return None


def load_config_cached():
    return load_config()


# ── 解密编排 ──────────────────────────────────────────────────────────────
def src_db(cfg, name):
    return os.path.join(os.path.expandvars(r'%USERPROFILE%\Documents\Tencent Files'),
                        cfg['account'], 'nt_qq', 'nt_db', name)


def verify_keymap(km_path, cfg):
    """用 page1 的 HMAC 校验缓存密钥是否仍然有效（不需要 QQ 在运行）。"""
    import hashlib, hmac as _hmac
    if not os.path.exists(km_path):
        return False, '没有缓存'
    with open(km_path, encoding='utf-8') as f:
        km = json.load(f)
    key_map, salt_files = km.get('key_map', {}), km.get('salt_files', {})
    checked = 0
    for sh, files in salt_files.items():
        kh = key_map.get(sh)
        if not kh:
            return False, '缺少 salt %s 的密钥' % sh[:12]
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


def ensure_keymap(cfg, force=False):
    ensure_dirs()
    km = os.path.join(KEYS, 'key_map.json')
    if os.path.exists(km) and not force:
        ok, why = verify_keymap(km, cfg)
        if ok:
            log('[key] 复用缓存密钥（HMAC 校验通过: %s），本步骤无需 QQ 参与' % why)
            return km
        log('[key] 缓存密钥失效（%s）-> 需要 QQ 正在运行以重新提取' % why)
    py = sys.executable
    cmd = [py, os.path.join(HERE, 'ntqq_key.py'), '--qq', cfg['account'], '-o', km]
    log('[key] 从 QQ 进程内存提取密钥 ...')
    r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if r.returncode != 0 or not os.path.exists(km):
        log(r.stdout or '')
        log(r.stderr or '')
        raise SystemExit('取密钥失败：请确认 QQ 已登录并正在运行')
    for line in (r.stdout or '').splitlines():
        if 'recovered' in line or 'scanned' in line:
            log('  ' + line.strip())
    return km


def ensure_plain(cfg, km, name, force=False):
    """确保 run/plain/<name> 存在且不比源文件旧。返回路径。"""
    src = src_db(cfg, name)
    if not os.path.exists(src):
        raise SystemExit('源库不存在: %s' % src)
    dst = os.path.join(PLAIN, name)
    if os.path.exists(dst) and not force:
        if os.path.getmtime(dst) >= os.path.getmtime(src):
            return dst
    cmd = [sys.executable, os.path.join(HERE, 'ntqq_decrypt.py'),
           '--key-map', km, '--db', name, '--out', dst]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if r.returncode != 0 or not os.path.exists(dst):
        log(r.stdout or ''); log(r.stderr or '')
        raise SystemExit('解密失败: %s' % name)
    for line in (r.stdout or '').splitlines():
        if 'done' in line or 'size=' in line:
            log('  ' + line.strip())
    return dst


def open_ro(path):
    return sqlite3.connect('file:' + path.replace('\\', '/') + '?mode=ro', uri=True)


# ── 名称解析 ──────────────────────────────────────────────────────────────
def build_names(cfg):
    """返回 (group_names, member_names, global_names)。
    group_names: {群号int: 群名}
    member_names: {(群号int, uid): 群名片}
    global_names: {uid: 昵称}
    """
    NS = load_schema()['name_sources']
    group_names, member_names, global_names = {}, {}, {}

    p = os.path.join(PLAIN, NS['group_list']['db'])
    if os.path.exists(p):
        con = open_ro(p)
        for table_key in ('group_list', 'group_detail'):
            s = NS[table_key]
            try:
                for code, name in con.execute('SELECT "%s","%s" FROM "%s"' % (s['code'], s['name'], s['table'])):
                    if code and name:
                        group_names[int(code)] = str(name)
            except sqlite3.DatabaseError as e:
                log('  [warn] %s 读群名失败: %s' % (s['table'], e))
        s = NS['group_member']
        try:
            for g, uid, card, nick in con.execute(
                    'SELECT "%s","%s","%s","%s" FROM "%s"' % (s['group'], s['uid'], s['card'], s['nick'], s['table'])):
                if g and uid:
                    nm = (card or '').strip() or (nick or '').strip()
                    if nm:
                        member_names[(int(g), uid)] = nm
        except sqlite3.DatabaseError as e:
            log('  [warn] group_member3 读群名片失败: %s' % e)
        con.close()

    p = os.path.join(PLAIN, NS['profile']['db'])
    if os.path.exists(p):
        con = open_ro(p)
        s = NS['profile']
        try:
            for uid, nick in con.execute('SELECT "%s","%s" FROM "%s"' % (s['uid'], s['nick'], s['table'])):
                if uid and nick:
                    global_names[uid] = str(nick)
        except sqlite3.DatabaseError as e:
            log('  [warn] profile_info_v6 读昵称失败: %s' % e)
        con.close()
    return group_names, member_names, global_names


# ── 行级容错游标（跳过损坏页） ────────────────────────────────────────────
def iter_rows_desc(path, select_cols, table, where='', params=(), batch=2000,
                   stop_when=None, max_rows=800000):
    """按 rowid 倒序分批读，遇损坏页自动缩批/跳行。stop_when(row) -> True 时停止。"""
    cols = ','.join('"%s"' % c for c in select_cols)
    last = None
    seen = 0
    errors = 0
    while seen < max_rows:
        con = open_ro(path)
        try:
            if last is None:
                sql = 'SELECT rowid,%s FROM "%s" %s ORDER BY rowid DESC LIMIT %d' % (cols, table, where, batch)
                cur = con.execute(sql, params)
            else:
                w = (where + ' AND ') if where else 'WHERE '
                sql = ('SELECT rowid,%s FROM "%s" %s rowid<? ORDER BY rowid DESC LIMIT %d'
                       % (cols, table, w, batch))
                cur = con.execute(sql, tuple(params) + (last,))
            rows = cur.fetchall()
        except sqlite3.DatabaseError:
            errors += 1
            con.close()
            if last is None:
                if batch > 50:
                    batch = max(50, batch // 4)
                    continue
                raise
            last -= 1                     # 跳过这一行继续
            if errors > 5000:
                raise
            continue
        con.close()
        if not rows:
            break
        stop = False
        for r in rows:
            last = r[0]
            seen += 1
            yield r
            if stop_when and stop_when(r):
                stop = True
                break
        if stop:
            break
    if errors:
        log('  [warn] 跳过 %d 次损坏读取' % errors)


# ── 索引 ──────────────────────────────────────────────────────────────────
INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS messages(
  msg_id INTEGER PRIMARY KEY,
  ts INTEGER, day TEXT, hour INTEGER,
  group_code INTEGER, group_name TEXT,
  sender_uid TEXT, sender_qq INTEGER, sender_name TEXT,
  direction INTEGER, msg_type INTEGER, subtype INTEGER,
  kind TEXT, text TEXT, reply_seq INTEGER
);
CREATE INDEX IF NOT EXISTS idx_m_ts ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_m_group_ts ON messages(group_code, ts);
CREATE INDEX IF NOT EXISTS idx_m_sender ON messages(sender_qq, ts);
CREATE TABLE IF NOT EXISTS groups(
  group_code INTEGER PRIMARY KEY, group_name TEXT,
  first_ts INTEGER, last_ts INTEGER, n INTEGER
);
CREATE TABLE IF NOT EXISTS media(
  msg_id INTEGER PRIMARY KEY,
  ts INTEGER, day TEXT, group_code INTEGER, group_name TEXT,
  sender_name TEXT, md5 TEXT, declared_size INTEGER,
  host TEXT, path TEXT
);
CREATE INDEX IF NOT EXISTS idx_media_ts ON media(ts);
CREATE INDEX IF NOT EXISTS idx_media_group ON media(group_code, ts);
"""


def open_index():
    ensure_dirs()
    con = sqlite3.connect(INDEX_DB)
    con.executescript(INDEX_SCHEMA)
    # 迁移（2026-10-02）：老索引的 media 没有 host/path。这两列是从消息元素里
    # 抠出来的「多媒体 CDN 原图地址」，配合内存里的 rkey 才能取到未点开过的图。
    cols = {r[1] for r in con.execute('PRAGMA table_info(media)')}
    for c in ('host', 'path'):
        if c not in cols:
            con.execute('ALTER TABLE media ADD COLUMN %s TEXT' % c)
    con.commit()
    return con
