#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动化装配 Windows 64位绿色免安装发行包。

将编译好的 gui_app.exe、_internal、增强版嵌入式 Python 运行时、scripts、schema
与 config.json 组装至 release_pkg 目录，并打成 gui_app-win64.zip。
"""

import os
import shutil
import subprocess
import sys
import zipfile
import hashlib

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST_DIR = os.path.join(BASE_DIR, "dist", "gui_app")
BUILD_TMP_PYTHON = os.path.join(BASE_DIR, "build_tmp", "python")
RELEASE_PKG = os.path.join(BASE_DIR, "release_pkg")
OUTPUT_ZIP = os.path.join(BASE_DIR, "gui_app-win64.zip")

SYS_SITE_PACKAGES = r"C:\Users\LENOVO\AppData\Local\Programs\Python\Python311\Lib\site-packages"

REQUIRED_PACKAGES = [
    "pydantic",
    "pydantic_core",
    "annotated_types",
    "typing_extensions.py",
    "typing_inspection",
    "PIL",
    "google",
]


def assemble():
    print(">>> 1. 清理并初始化 release_pkg 目录...")
    if os.path.exists(RELEASE_PKG):
        shutil.rmtree(RELEASE_PKG)
    os.makedirs(RELEASE_PKG, exist_ok=True)

    print(">>> 2. 复制 PyInstaller 编译成果 (gui_app.exe & _internal)...")
    shutil.copy2(os.path.join(DIST_DIR, "gui_app.exe"), os.path.join(RELEASE_PKG, "gui_app.exe"))
    shutil.copytree(os.path.join(DIST_DIR, "_internal"), os.path.join(RELEASE_PKG, "_internal"))

    print(">>> 3. 复制 scripts、schema 与 config.json...")
    shutil.copytree(os.path.join(BASE_DIR, "scripts"), os.path.join(RELEASE_PKG, "scripts"), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(os.path.join(BASE_DIR, "schema"), os.path.join(RELEASE_PKG, "schema"))
    
    cfg_src = os.path.join(BASE_DIR, "config.example.json")
    if not os.path.exists(cfg_src):
        cfg_src = os.path.join(BASE_DIR, "config.json")
    shutil.copy2(cfg_src, os.path.join(RELEASE_PKG, "config.json"))

    print(">>> 4. 复制并扩展嵌入式 Python 运行时...")
    target_python = os.path.join(RELEASE_PKG, "python")
    shutil.copytree(BUILD_TMP_PYTHON, target_python)

    target_sp = os.path.join(target_python, "Lib", "site-packages")
    os.makedirs(target_sp, exist_ok=True)

    for item in REQUIRED_PACKAGES:
        src = os.path.join(SYS_SITE_PACKAGES, item)
        dst = os.path.join(target_sp, item)
        if os.path.isdir(src):
            print(f"    + 注入包目录: {item}")
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        elif os.path.isfile(src):
            print(f"    + 注入单文件模块: {item}")
            shutil.copy2(src, dst)
        else:
            print(f"    ! 警告: 未找到 {src}")

    print(">>> 5. 验证独立运行时的导入完整性...")
    py_exe = os.path.join(target_python, "python.exe")
    check_code = """
import sys
for mod in ['cryptography', 'cffi', 'pydantic', 'PIL', 'google.protobuf']:
    try:
        m = __import__(mod)
        print(f"  [OK] {mod} -> {getattr(m, '__file__', 'builtin')}")
    except Exception as e:
        print(f"  [FAIL] {mod}: {e}")
        sys.exit(1)
"""
    res = subprocess.run([py_exe, "-c", check_code], capture_output=True, text=True)
    print(res.stdout)
    if res.returncode != 0:
        print("错误: 独立 Python 运行时校验未通过!")
        print(res.stderr)
        sys.exit(1)

    print(">>> 6. 验证各个演进模块脚本在独立运行时下的可执行性...")
    test_scripts = [
        "ntqq_snapshot.py",
        "ntqq_parser.py",
        "ntqq_vision_filter.py",
        "ntqq_normalizer.py",
        "ntqq_segmenter.py",
        "ntqq_summary_tree.py",
        "ntqq_report_schema.py",
    ]
    for s in test_scripts:
        script_path = os.path.join(RELEASE_PKG, "scripts", s)
        r = subprocess.run([py_exe, script_path, "--help"], capture_output=True, text=True)
        # 部分脚本没有 --help 也可以 exit 0 或报错参数，只要不是 ImportError 就算通过
        if "ImportError" in r.stderr or "ModuleNotFoundError" in r.stderr:
            print(f"  [FAIL] {s} 存在导入缺失:\n{r.stderr}")
            sys.exit(1)
        else:
            print(f"  [OK] {s} 依赖检查通过")

    print(">>> 7. 验证打包后的 GUI 应用自检 (--self-test)...")
    res_gui = subprocess.run([os.path.join(RELEASE_PKG, "gui_app.exe"), "--self-test"], capture_output=True, text=True)
    print("GUI 进程输出:")
    print(res_gui.stdout)
    if res_gui.returncode != 0:
        print(f"错误: gui_app.exe --self-test 失败: code={res_gui.returncode}")
        print(res_gui.stderr)
        sys.exit(1)

    smoke_file = os.path.join(RELEASE_PKG, "smoke_result.txt")
    if os.path.exists(smoke_file):
        with open(smoke_file, "r", encoding="utf-8") as f:
            print("自检报告内容:")
            print(f.read().strip())
        os.remove(smoke_file)

    print(">>> 8. 打包为 zip 发行包...")
    if os.path.exists(OUTPUT_ZIP):
        os.remove(OUTPUT_ZIP)

    with zipfile.ZipFile(OUTPUT_ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for root, dirs, files in os.walk(RELEASE_PKG):
            for file in files:
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, RELEASE_PKG)
                z.write(full_path, rel_path)

    zip_size_mb = os.path.getsize(OUTPUT_ZIP) / (1024 * 1024)
    print(f"✅ 发行包打包完成: {OUTPUT_ZIP} ({zip_size_mb:.2f} MiB)")

    print(">>> 9. 计算 SHA256 哈希值...")
    hasher = hashlib.sha256()
    with open(OUTPUT_ZIP, "rb") as f:
        while chunk := f.read(1024 * 1024):
            hasher.update(chunk)
    sha256_val = hasher.hexdigest().upper()
    print(f"SHA256: {sha256_val}")

    print(">>> 10. 运行 tools/audit_package.py 审计压缩包...")
    audit_script = os.path.join(BASE_DIR, "tools", "audit_package.py")
    if os.path.exists(audit_script):
        r_audit = subprocess.run([sys.executable, audit_script, OUTPUT_ZIP], capture_output=True, text=True)
        print(r_audit.stdout)

    print("\n🎉 装配与多重自检全部顺利通过！")


if __name__ == "__main__":
    assemble()
