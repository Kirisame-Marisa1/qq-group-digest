#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQ 群聊自动总结工具 —— 公共库。

职责：路径/配置、schema 指纹校验、SQLCipher 解密编排、私有表读取、
      40800 protobuf 解码、昵称/群名解析、行级容错游标。
"""
import json, logging, logging.handlers, os, re, sqlite3, struct, subprocess, sys, time
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
CONFIG = os.environ.get('NTQQ_CONFIG') or os.path.join(APP, 'config.json')
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
    "qq_data_dir": "",                # QQ 数据目录；空 = %USERPROFILE%\Documents\Tencent Files
    "qq_exe": "C:/Program Files/Tencent/QQNT/QQ.exe",
    "index_days": 30,
    "decrypt": ["nt_msg.db", "group_info.db", "profile_info.db"],
    "class_groups": [],
    "glossary_autoupdate": True,
}


def _force_utf8_stdout():
    """中文 Windows 控制台默认 GBK，一旦打印 emoji/生僻字就抛 UnicodeEncodeError，
    把整个流程打断——实测「群改名」的 ✏ 就能崩掉建索引（GUI 因设了
    PYTHONIOENCODING 而看不出来，CLI 用户会中招）。日志永远不该成为崩溃源。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass


_force_utf8_stdout()


# ── 日志分级 ──────────────────────────────────────────────────────────────
_logger = None


def _get_logger():
    global _logger
    if _logger is not None:
        return _logger
    _logger = logging.getLogger('qq-digest')
    _logger.setLevel(logging.DEBUG)
    _logger.propagate = False

    # 控制台 handler（INFO+，保持原有 stdout 输出风格，只输出消息本身）
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter('%(message)s'))
    _logger.addHandler(ch)

    # 文件 handler（DEBUG+，按日滚动，保留 7 天）
    try:
        os.makedirs(LOGS, exist_ok=True)
        fh = logging.handlers.TimedRotatingFileHandler(
            os.path.join(LOGS, 'qq-digest.log'),
            when='midnight', backupCount=7, encoding='utf-8'
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s', '%H:%M:%S'))
        _logger.addHandler(fh)
    except Exception:
        pass  # 日志文件失败不影响主流程

    return _logger


def log(*a):
    """INFO 级别日志，保持原有多参数拼接签名，调用方无需修改。"""
    msg = ' '.join(str(x) for x in a)
    try:
        _get_logger().info(msg)
    except UnicodeEncodeError:
        try:
            enc = getattr(sys.stdout, 'encoding', None) or 'utf-8'
            print(msg.encode(enc, 'replace').decode(enc, 'replace'), flush=True)
        except Exception:
            pass


def log_debug(*a):
    """DEBUG 级别日志（默认只写日志文件，控制台需 --verbose 才显示）。"""
    _get_logger().debug(' '.join(str(x) for x in a))


def log_warn(*a):
    """WARNING 级别日志。"""
    _get_logger().warning(' '.join(str(x) for x in a))


def log_error(*a):
    """ERROR 级别日志。"""
    _get_logger().error(' '.join(str(x) for x in a))


def set_log_level(verbose=False):
    """设置控制台日志级别。verbose=True 时显示 DEBUG 信息。"""
    _get_logger()
    for h in _logger.handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
            h.setLevel(logging.DEBUG if verbose else logging.INFO)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG, encoding='utf-8') as f:
                cfg.update(json.load(f))
        except Exception as e:
            log_warn('[config] 读取 config.json 失败: %s' % e)
    return cfg


def validate_config(cfg, require_account=True):
    """校验 config.json 配置合法性，返回 (ok, errors_list)。"""
    errors = []
    acc = str(cfg.get('account') or '').strip()
    if require_account:
        if not acc or acc == 'YOUR_QQ':
            errors.append('config.json 中未配置有效的 "account"（QQ号），请将 YOUR_QQ 替换为你的真实 QQ 号')
        elif not acc.isdigit():
            errors.append('config.json 中的 "account" (%r) 应为纯数字 QQ 号' % acc)
    days = cfg.get('index_days', 30)
    if not isinstance(days, (int, float)) or days <= 0:
        errors.append('config.json 中的 "index_days" (%r) 必须为正数' % days)
    dec = cfg.get('decrypt')
    if not isinstance(dec, list) or not dec:
        errors.append('config.json 中的 "decrypt" 必须为非空数据库列表')
    return (len(errors) == 0), errors


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
    opt_tables = set(sc.get('added_tables_9936') or ())
    opt_cols = set(sc.get('added_columns_9936') or ('40722',))
    problems = []
    for t, cols in want.items():
        if t not in got:
            if t in opt_tables:
                continue
            problems.append('表缺失: %s' % t)
            continue
        missing = [c for c in cols if c not in got[t] and c not in opt_cols]
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


# ── Protobuf wire 编解码与消息模型（收敛由 ntqq_parser 提供，保证单一事实来源） ──
from ntqq_parser import (
    _varint,
    pb_fields,
    _decode_str,
    _decode_str as _s,
    decode_body,
    CT_KIND,
    classify,
    FWD_FIELD,
    forward_text,
    decode_forward,
    format_forward,
    MessageParser,
    ParsedMessage,
    AtTarget,
    ReplyInfo,
    CardInfo,
    extract_card_info,
)


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


# ── DPAPI 密钥保护与 RKey 会话凭据（由 ntqq_crypto 模块托管） ─────────────
from ntqq_crypto import (
    dpapi_encrypt,
    dpapi_decrypt,
    protect_key_hex,
    unwrap_key_hex,
    load_rkey,
    save_rkey,
)


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
def _detect_userdata_ini():
    """读取 Windows 下 NTQQ 的全局数据路径配置 UserDataInfo.ini。"""
    ini = r'C:\Users\Public\Documents\Tencent\QQ\UserDataInfo.ini'
    if os.name == 'nt' and os.path.isfile(ini):
        try:
            with open(ini, encoding='utf-8', errors='ignore') as f:
                for line in f:
                    if line.strip().startswith('UserDataSavePath='):
                        val = line.split('=', 1)[1].strip()
                        if val and os.path.isdir(val):
                            return val
        except Exception:
            pass
    return ''


def _normalize_qq_data_dir(d, acc=''):
    """将用户或 GUI 填入的目录规范化为顶层 Tencent Files 目录（即 <QQ号> 的父目录）。

    GUI 的 account_root 通常填到 <Tencent Files>/<QQ号>，而 CLI 的 qq_data_dir
    填到 <Tencent Files>。这里统一向上剥离末尾的 nt_db / nt_qq / <QQ号> 层级，
    避免后续 qq_path(qq, 'nt_qq', 'nt_db') 拼出 .../<QQ号>/<QQ号>/nt_qq/nt_db。
    """
    if not d:
        return ''
    d = os.path.expanduser(d).replace('\\', '/').rstrip('/')
    if os.path.basename(d).lower() == 'nt_db':
        d = os.path.dirname(d)
    if os.path.basename(d).lower() == 'nt_qq':
        d = os.path.dirname(d)
    base = os.path.basename(d)
    if (acc and acc != 'YOUR_QQ' and base == acc) or os.path.isdir(os.path.join(d, 'nt_qq')):
        d = os.path.dirname(d)
    return d


def qq_data_dir():
    """QQ 数据目录（装 QQ 时可改到别的盘，所以必须可配置）。

    优先读取 config.json 中的 qq_data_dir 或 account_root（兼容 GUI 配置），
    若为空则尝试从 C:\\Users\\Public\\Documents\\Tencent\\QQ\\UserDataInfo.ini 自动探测，
    最后回退至默认的 %USERPROFILE%\\Documents\\Tencent Files。
    """
    try:
        with open(CONFIG, encoding='utf-8') as f:
            raw = json.load(f)
            d = (raw.get('qq_data_dir') or raw.get('account_root') or '').strip()
            acc = str(raw.get('account') or '').strip()
    except Exception:
        d, acc = '', ''
    d = _normalize_qq_data_dir(d, acc)
    d = d or _detect_userdata_ini() or os.path.expandvars(r'%USERPROFILE%\Documents\Tencent Files')
    return os.path.expanduser(d).replace('\\', '/').rstrip('/')


def qq_path(*parts):
    """拼 <QQ数据目录>/<QQ号>/nt_qq/... 下的路径（自动防重复拼接 <QQ号>）。"""
    base = qq_data_dir()
    if parts and str(parts[0]) and os.path.basename(base.rstrip('/\\')) == str(parts[0]):
        parts = parts[1:]
    return os.path.join(base, *parts)


def src_db(cfg, name):
    return qq_path(str(cfg.get('account') or ''), 'nt_qq', 'nt_db', name)


# ── 密钥校验与明文库生命周期管理（由 ntqq_crypto 模块托管） ───────────────
from ntqq_crypto import (
    verify_keymap,
    ensure_keymap,
    ensure_plain,
    cleanup_plain,
)


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
                log_warn('  [warn] %s 读群名失败: %s' % (s['table'], e))
        s = NS['group_member']
        try:
            for g, uid, card, nick in con.execute(
                    'SELECT "%s","%s","%s","%s" FROM "%s"' % (s['group'], s['uid'], s['card'], s['nick'], s['table'])):
                if g and uid:
                    nm = (card or '').strip() or (nick or '').strip()
                    if nm:
                        member_names[(int(g), uid)] = nm
        except sqlite3.DatabaseError as e:
            log_warn('  [warn] group_member3 读群名片失败: %s' % e)
        con.close()

    p = os.path.join(PLAIN, NS['profile']['db'])
    if os.path.exists(p):
        con = open_ro(p)
        s = NS['profile']
        try:
            for uid, qq, nick in con.execute('SELECT "%s","%s","%s" FROM "%s"' % (s['uid'], s['qq'], s['nick'], s['table'])):
                if uid and nick:
                    global_names[uid] = str(nick)
                if qq and nick:
                    try:
                        global_names['qq:%d' % int(qq)] = str(nick)
                    except (ValueError, TypeError):
                        pass
        except sqlite3.DatabaseError as e:
            log_warn('  [warn] profile_info_v6 读昵称失败: %s' % e)
        con.close()
    return group_names, member_names, global_names


# ── 行级容错游标（跳过损坏页） ────────────────────────────────────────────
# ── 聚合索引、数据库迁移与 FTS5 引擎（由 ntqq_db 模块托管） ─────────────────
from ntqq_db import (
    INDEX_SCHEMA,
    INDEX_MIGRATIONS,
    run_index_migrations,
    rebuild_fts,
    sync_fts_incremental,
    open_ro,
    iter_rows_desc,
)
import ntqq_db


def open_index(index_path=None):
    return ntqq_db.open_index(index_path or INDEX_DB)


# ── Phase 2 演进模块统一导出 ──────────────────────────────────────────────
try:
    from ntqq_snapshot import SafeDBSnapshot, make_safe_snapshot
    from ntqq_parser import MessageParser, ParsedMessage, AtTarget, ReplyInfo, CardInfo
    from ntqq_vision_filter import ImageFilterPipeline, FilterTier, ImageFilterResult, compute_dhash
    from ntqq_normalizer import NoiseNormalizer, NormalizedMessage
except ImportError:
    pass


