#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键安装 / 卸载 / 检查 qq-digest-bridge 插件 (install_bridge.py)

自动探测 LiteLoaderQQNT 插件目录，并部署/移除桥接插件。
"""
import argparse, json, os, shutil, sys, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_SRC = os.path.join(HERE, 'qq-digest-bridge')
DEFAULT_PORT = 48920


def find_liteloader_plugins_dir():
    """查找可能存在的 LiteLoaderQQNT 插件目录。"""
    candidates = []

    # 1. 环境变量与常规文档目录
    user_profile = os.environ.get('USERPROFILE', '')
    appdata = os.environ.get('APPDATA', '')
    localappdata = os.environ.get('LOCALAPPDATA', '')

    if user_profile:
        candidates.append(os.path.join(user_profile, 'Documents', 'LiteLoaderQQNT', 'plugins'))
        candidates.append(os.path.join(user_profile, 'LiteLoaderQQNT', 'plugins'))
    if appdata:
        candidates.append(os.path.join(appdata, 'LiteLoaderQQNT', 'plugins'))
    if localappdata:
        candidates.append(os.path.join(localappdata, 'LiteLoaderQQNT', 'plugins'))

    for c in candidates:
        if os.path.exists(c):
            return c

    # 如果父级 LiteLoaderQQNT 存在，自动定位 plugins
    for c in candidates:
        parent = os.path.dirname(c)
        if os.path.exists(parent):
            return c

    return None


def check_health(port=DEFAULT_PORT):
    """检查桥接服务是否正常运行。"""
    url = 'http://127.0.0.1:%d/health' % port
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            return True, data
    except urllib.error.URLError as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)


def install_plugin(target_dir=None):
    plugins_dir = target_dir or find_liteloader_plugins_dir()
    if not plugins_dir:
        print('[错误] 未能自动找到 LiteLoaderQQNT 插件目录。')
        print('请使用 --plugin-dir <路径> 手动指定 LiteLoaderQQNT 的 plugins 目录。')
        return False

    os.makedirs(plugins_dir, exist_ok=True)
    dst = os.path.join(plugins_dir, 'qq-digest-bridge')

    if os.path.exists(dst):
        print('[提示] 目标已存在旧版本，正在覆盖更新: %s' % dst)
        if os.path.islink(dst):
            os.unlink(dst)
        else:
            shutil.rmtree(dst)

    try:
        # 尝试优先创建符号链接 (软链接更新方便)
        os.symlink(PLUGIN_SRC, dst, target_is_directory=True)
        print('[成功] 已创建插件软链接: %s -> %s' % (dst, PLUGIN_SRC))
    except (OSError, NotImplementedError):
        # 权限不足时回退为物理拷贝
        shutil.copytree(PLUGIN_SRC, dst)
        print('[成功] 已复制插件文件至: %s' % dst)

    print('\n安装完成！请重启 QQ 客户端使插件生效。')
    print('重启后可运行 `python extension/install_bridge.py --status` 检验连通性。')
    return True


def uninstall_plugin(target_dir=None):
    plugins_dir = target_dir or find_liteloader_plugins_dir()
    if not plugins_dir:
        print('[错误] 未能找到 LiteLoaderQQNT 插件目录。')
        return False

    dst = os.path.join(plugins_dir, 'qq-digest-bridge')
    if not os.path.exists(dst):
        print('[提示] 插件未在目标目录中安装: %s' % dst)
        return True

    if os.path.islink(dst):
        os.unlink(dst)
    else:
        shutil.rmtree(dst)
    print('[成功] 已卸载移除插件: %s' % dst)
    return True


def main():
    ap = argparse.ArgumentParser(description='qq-digest-bridge 插件安装与状态检测')
    ap.add_argument('--install', action='store_true', help='安装插件到 LiteLoader 插件目录')
    ap.add_argument('--uninstall', action='store_true', help='卸载插件')
    ap.add_argument('--status', action='store_true', help='检查桥接服务是否运行')
    ap.add_argument('--plugin-dir', type=str, default=None, help='手动指定 LiteLoader plugins 目录')
    ap.add_argument('--port', type=int, default=DEFAULT_PORT, help='指定检测的端口号')
    a = ap.parse_args()

    if a.status or (not a.install and not a.uninstall):
        ok, res = check_health(a.port)
        if ok:
            print('🟢 [健康状态正常] qq-digest-bridge 正在运行！')
            print('  - 端口: %s' % res.get('port'))
            print('  - QQ进程 PID: %s' % res.get('pid'))
            print('  - 内核服务就绪: %s' % res.get('kernel_ready'))
            print('  - 登录账号: %s' % (res.get('account') or '未检测到/免密登录'))
        else:
            print('🔴 [未连通] 无法连接到 127.0.0.1:%d (%s)' % (a.port, res))
            print('  - 请确认 QQ 是否已启动且已安装本插件。')
        return

    if a.install:
        install_plugin(a.plugin_dir)
    elif a.uninstall:
        uninstall_plugin(a.plugin_dir)


if __name__ == '__main__':
    main()
