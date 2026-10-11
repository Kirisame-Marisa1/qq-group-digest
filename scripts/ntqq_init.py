#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQ 群聊自动总结工具 —— 一键初始化向导。

职责：
1. 自动扫描本机 `%USERPROFILE%\\Documents\\Tencent Files` 下包含 `nt_qq\\nt_db\\nt_msg.db` 的账号目录。
2. 自动探测本机 `QQ.exe` 安装路径（常见路径 + 注册表）。
3. 生成或更新 `config.json`，调用 `ntqq_core.validate_config` 与 `ensure_dirs()` 完成目录初始化。
4. 打印清晰的下一步操作指引。
"""
import argparse, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ntqq_core
from ntqq_core import (
    DEFAULT_CONFIG, load_config, save_config, validate_config, ensure_dirs,
    log, log_debug, log_warn, log_error, set_log_level
)


def _default_tencent_files_dir():
    """返回本机默认的 Tencent Files 目录路径（支持配置项与 UserDataInfo.ini 自动探测）。"""
    d = ntqq_core.qq_data_dir()
    if d and '%USERPROFILE%' not in d:
        return d
    expanded = os.path.expandvars(r'%USERPROFILE%\Documents\Tencent Files')
    if '%USERPROFILE%' not in expanded:
        return expanded
    return os.path.join(os.path.expanduser('~'), 'Documents', 'Tencent Files')


def discover_qq_accounts(base_dir=None):
    """扫描包含 `nt_qq/nt_db/nt_msg.db` 的 QQ 账号目录，按数据库最后修改时间降序返回 QQ 号列表。"""
    tdir = base_dir or _default_tencent_files_dir()
    if not tdir or not os.path.isdir(tdir):
        log_debug('[init] Tencent Files 目录不存在: %s' % tdir)
        return []

    found = []
    try:
        entries = os.listdir(tdir)
    except OSError as e:
        log_debug('[init] 无法读取目录 %s: %s' % (tdir, e))
        return []

    for name in entries:
        # QQ 号通常为 5~11 位纯数字目录名
        if not name.isdigit():
            continue
        db_path = os.path.join(tdir, name, 'nt_qq', 'nt_db', 'nt_msg.db')
        if os.path.isfile(db_path):
            try:
                mtime = os.path.getmtime(db_path)
            except OSError:
                mtime = 0
            found.append((mtime, name))

    # 按最近活跃时间降序排列，若时间相同按 QQ 号排序
    found.sort(key=lambda x: (-x[0], x[1]))
    return [acc for _, acc in found]


def detect_qq_exe():
    """探测本机 NTQQ 的 QQ.exe 路径，返回归一化为正斜杠的路径字符串。"""
    candidates = [
        'C:/Program Files/Tencent/QQNT/QQ.exe',
        'C:/Program Files (x86)/Tencent/QQNT/QQ.exe',
        'D:/Program Files/Tencent/QQNT/QQ.exe',
        'D:/Program Files (x86)/Tencent/QQNT/QQ.exe',
        'E:/Program Files/Tencent/QQNT/QQ.exe',
    ]
    local_app = os.environ.get('LOCALAPPDATA')
    if local_app:
        candidates.append(os.path.join(local_app, 'Tencent', 'QQNT', 'QQ.exe'))

    # Windows 注册表探测
    if os.name == 'nt':
        try:
            import winreg
            reg_targets = [
                (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ'),
                (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ'),
                (winreg.HKEY_CURRENT_USER, r'Software\Tencent\QQNT'),
                (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Tencent\QQNT'),
            ]
            for root_key, sub_key in reg_targets:
                try:
                    with winreg.OpenKey(root_key, sub_key) as k:
                        for val_name in ('DisplayIcon', 'Install', 'InstallLocation', 'exe'):
                            try:
                                val, _ = winreg.QueryValueEx(k, val_name)
                                if not val or not isinstance(val, str):
                                    continue
                                clean = val.strip().strip('"').split(',')[0].strip()
                                if clean.lower().endswith('qq.exe'):
                                    candidates.insert(0, clean)
                                elif os.path.isdir(clean):
                                    candidates.insert(0, os.path.join(clean, 'QQ.exe'))
                            except OSError:
                                continue
                except OSError:
                    continue
        except Exception as e:
            log_debug('[init] 注册表探测 QQ.exe 失败: %r' % e)

    for p in candidates:
        norm = p.replace('\\', '/')
        if os.path.isfile(norm):
            return norm

    return DEFAULT_CONFIG['qq_exe']


def _sync_core_dirs(root_path):
    """同步更新 ntqq_core 的目录全局变量并创建目录结构。"""
    norm_root = str(root_path or ntqq_core._default_root()).replace('\\', '/')
    ntqq_core.ROOT = norm_root
    ntqq_core.DATA = os.path.join(norm_root, 'data')
    ntqq_core.PLAIN = os.path.join(ntqq_core.DATA, 'plain')
    ntqq_core.INDEX_DB = os.path.join(ntqq_core.DATA, 'index.db')
    ntqq_core.KEYS = os.path.join(norm_root, 'keys')
    ntqq_core.MEDIA = os.path.join(norm_root, 'media')
    ntqq_core.KNOW = os.path.join(norm_root, 'knowledge')
    ntqq_core.GLOSSARY = os.path.join(ntqq_core.KNOW, 'glossary')
    ntqq_core.OUTPUT = os.path.join(norm_root, 'output')
    ntqq_core.RUN = os.path.join(norm_root, 'run')
    ntqq_core.LOGS = os.path.join(norm_root, 'logs')
    ntqq_core.CACHE = ntqq_core.DATA
    ensure_dirs()


def init_project(qq=None, root=None, include_c2c=False, base_dir=None, non_interactive=True):
    """执行项目一键初始化：发现账号、探测 QQ 路径、生成/更新 config.json、校验并创建目录。

    参数:
        qq: 指定 QQ 号（可选，未指定则自动扫描发现）
        root: 指定数据根目录（可选，未指定则保留已有配置或使用平台默认目录）
        include_c2c: 是否开启私聊索引（默认 False）
        base_dir: 自定义 Tencent Files 扫描根目录（主要供测试使用）
        non_interactive: 是否非交互模式（默认 True，自动选择最新活跃账号）

    返回:
        dict: 生成并写入的完整配置字典
    """
    cfg = load_config()
    accounts = discover_qq_accounts(base_dir=base_dir)
    if accounts:
        log('[init] 扫描到本机 %d 个候选 QQ 账号: %s' % (len(accounts), ', '.join(accounts)))
    else:
        log_debug('[init] 未在默认目录扫描到包含 nt_msg.db 的 QQ 账号')

    # 1. 确定 QQ 账号
    chosen_qq = None
    if qq is not None and str(qq).strip():
        chosen_qq = str(qq).strip()
        log('[init] 使用命令行指定的 QQ 号: %s' % chosen_qq)
    elif accounts:
        if len(accounts) == 1 or non_interactive or not sys.stdin.isatty():
            chosen_qq = accounts[0]
            log('[init] 自动选择 QQ 号: %s' % chosen_qq)
        else:
            print('检测到多个 QQ 账号：')
            for idx, acc in enumerate(accounts, 1):
                print('  [%d] %s' % (idx, acc))
            try:
                ans = input('请输入序号或直接输入 QQ 号 [默认 1 (%s)]: ' % accounts[0]).strip()
            except (EOFError, KeyboardInterrupt):
                ans = ''
            if not ans:
                chosen_qq = accounts[0]
            elif ans.isdigit() and 1 <= int(ans) <= len(accounts):
                chosen_qq = accounts[int(ans) - 1]
            else:
                chosen_qq = ans
    else:
        existing_acc = str(cfg.get('account') or '').strip()
        if existing_acc and existing_acc != 'YOUR_QQ' and existing_acc.isdigit():
            chosen_qq = existing_acc
            log('[init] 保留已有配置的 QQ 号: %s' % chosen_qq)
        elif not non_interactive and sys.stdin.isatty():
            try:
                ans = input('未自动检测到 QQ 数据库，请手动输入你的 QQ 号: ').strip()
                if ans:
                    chosen_qq = ans
            except (EOFError, KeyboardInterrupt):
                pass

    if chosen_qq:
        cfg['account'] = chosen_qq
    elif not cfg.get('account'):
        cfg['account'] = 'YOUR_QQ'

    # 2. 确定数据根目录 root
    if root is not None and str(root).strip():
        cfg['root'] = str(root).strip().replace('\\', '/')
    elif not cfg.get('root'):
        cfg['root'] = ntqq_core._default_root().replace('\\', '/')
    else:
        cfg['root'] = str(cfg['root']).replace('\\', '/')

    # 3. 探测 QQ.exe 路径
    cur_exe = cfg.get('qq_exe') or ''
    if not cur_exe or not os.path.isfile(cur_exe):
        detected_exe = detect_qq_exe()
        cfg['qq_exe'] = detected_exe
        if os.path.isfile(detected_exe):
            log('[init] 探测到 QQ.exe: %s' % detected_exe)
        else:
            log_debug('[init] 未在常见位置找到 QQ.exe，使用默认路径: %s' % detected_exe)

    # 3.5 探测非默认 QQ 数据目录（UserDataInfo.ini 或 account_root）
    cur_data_dir = (cfg.get('qq_data_dir') or cfg.get('account_root') or '').strip()
    if cur_data_dir:
        cur_data_dir = ntqq_core._normalize_qq_data_dir(cur_data_dir, str(cfg.get('account') or ''))
    else:
        ini_dir = ntqq_core._detect_userdata_ini()
        if ini_dir:
            cur_data_dir = ini_dir.replace('\\', '/')
            log('[init] 探测到自定义 QQ 数据目录: %s' % cur_data_dir)
    if cur_data_dir:
        cfg['qq_data_dir'] = cur_data_dir.replace('\\', '/')

    # 4. 补充新增配置项（保持向后兼容）
    cfg.setdefault('class_group_names', {})
    if include_c2c:
        cfg['include_c2c'] = True
    else:
        cfg.setdefault('include_c2c', False)
    cfg.setdefault('clean_plain_after_build', False)

    # 5. 保存配置并初始化目录
    save_config(cfg)
    log('[init] 已写入配置文件: %s' % ntqq_core.CONFIG)

    _sync_core_dirs(cfg['root'])
    log('[init] 已初始化数据目录结构: %s' % cfg['root'])

    # 6. 校验配置合法性
    ok, errors = validate_config(cfg, require_account=True)
    if ok:
        log('[init] 配置校验通过 (account=%s, root=%s, include_c2c=%s)'
            % (cfg.get('account'), cfg.get('root'), cfg.get('include_c2c')))
    else:
        for err in errors:
            log_warn('[init] 配置待完善: %s' % err)
        log_warn('[init] 请使用 `python scripts/ntqq_init.py --qq <你的QQ号>` 或直接编辑 config.json 完成账号配置。')

    # 7. 打印下一步指引
    log('')
    log('── 下一步操作指引 ──────────────────────────────────────────')
    log('1. 确保本机 NTQQ 已登录并正在运行（首次提取密钥需要读取进程内存）')
    if cfg.get('include_c2c'):
        log('2. 构建群聊 + 私聊滚动索引：')
        log('   python scripts/ntqq_build.py --days 30 --include-c2c')
    else:
        log('2. 构建群聊滚动索引（支持增量刷新）：')
        log('   python scripts/ntqq_build.py --days 30')
    log('3. 生成最近 6 小时活跃群聊速览报告：')
    log('   python scripts/ntqq_report.py --active --hours 6 --brief')
    log('4. 自然语言提问检索：')
    log('   python scripts/ntqq_report.py --ask "某群今天聊某话题了吗"')
    log('────────────────────────────────────────────────────────────')

    return cfg


def main():
    ap = argparse.ArgumentParser(description='qq-group-digest 一键初始化向导')
    ap.add_argument('--qq', default=None, help='指定要分析的 QQ 号（不传则自动扫描发现）')
    ap.add_argument('--root', default=None, help='指定数据与缓存根目录（默认 D:/QQChatCache 或 ~/.qqchatcache）')
    ap.add_argument('--include-c2c', action='store_true', help='在配置中默认开启私聊消息索引（物理隔离存储）')
    ap.add_argument('--non-interactive', action='store_true', help='非交互模式：自动选择最近活跃的候选 QQ 号')
    ap.add_argument('--verbose', action='store_true', help='输出调试级别日志')
    args = ap.parse_args()

    if args.verbose:
        set_log_level(True)

    # 默认优先自动选择唯一候选；若传了 --non-interactive 则多候选也自动取最新
    init_project(
        qq=args.qq,
        root=args.root,
        include_c2c=args.include_c2c,
        non_interactive=args.non_interactive
    )


if __name__ == '__main__':
    main()
