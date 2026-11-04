# Changelog

All notable changes to this project will be documented in this file.
This project adheres to [Semantic Versioning](https://semver.org/).

## [1.7.0-beta.1] - 2026-11-04

### Added (Ecosystem Evolution Beta)
- **🔗 资源与链接归档 (ntqq_links)**: 自动提取群聊内外链、代码库、网盘、多媒体与资讯，剔除追踪参数，并在网页与简报中结构化分类呈现。
- **📋 待办与决策追踪 (ntqq_action_items)**: 基于动词与 DDL 规则提取群聊待办任务与共识决策，网页端支持即时交互打勾。
- **🕸️ 社交关系图谱 (ntqq_social)**: 计算群友互动频率与紧密搭档 Top Dyads，纯本地零依赖 SVG 渲染互动拓扑网。
- **🎁 群聊 Wrapped 回顾 (ntqq_wrapped)**: Spotify 风格年度/周期群聊回顾大屏（话痨之王、夜猫子勋章、高光破纪录日）。
- **📓 Obsidian 第二大脑导出 (ntqq_obsidian)**: 一键导出兼容 Obsidian 的知识库笔记，支持 YAML frontmatter、Callouts 与双向维基链。
- **🔍 WinRT 原生 OCR 搜图 (ntqq_ocr)**: Windows 10/11 原生本地 OCR 索引构建，支持图片文字 FTS5 全文搜索。
- **GUI 生态拓展工具箱**: 桌面客户端集成 Wrapped 回顾、Obsidian 导出、资源待办、OCR 索引快捷入口。

### Fixed
- 加固 Tencent Files 路径规范化逻辑，修复由于全局 nt_qq 目录导致的父路径误削切。
- 过滤系统“戳了戳”灰条协议原始 JSON，净化头条重点发言雷达。
- 修复 sqlite3.Row 属性调用异常与 Obsidian 路径配置回退。

## [1.6.1] - 2026-11-03

### Fixed & Optimized
- **Windows GUI 无弹窗静默执行与体验优化 (Silent Subprocess Execution)**:
  - 彻底消除在 Windows 11 / Windows 10 下运行任务（提取密钥、全量索引、生成简报、一键全自动）时弹出的 PowerShell / pwsh 控制台黑框。
  - 在 `gui.service.QQGuiService` 中引入 `get_subprocess_kwargs()`，自动适配注入 `subprocess.CREATE_NO_WINDOW` 与 `STARTF_USESHOWWINDOW` / `SW_HIDE` 标志，保持实时进度与日志管道流式输出的同时抑制终端弹窗。
  - 在 `scripts/ntqq_crypto.py` 中为 `subprocess.run` 和 `subprocess.Popen` 增加静默执行配置，消除底层解密和密钥提取时的终端闪现。
  - 新增 `gui_app.pyw` 纯窗口启动脚本（关联 `pythonw.exe` 零黑框启动），并在 `gui_app.py` 中添加 `_maybe_hide_console()` 自动隐藏双击启动时产生的冗余控制台。
- **自动化测试覆盖**:
  - 新增 `tests/test_gui_service.py::test_subprocess_kwargs_suppress_window` 测试，验证跨平台控制台抑制行为。

## [1.6.0] - 2026-11-01

### Added
- **Reverse Streaming B-Tree Extraction (`--source auto/stream`)**:
  - Implemented reverse chronological B-tree traversal (`Db.rows_desc`) directly on encrypted SQLCipher databases using in-memory page decryption.
  - Added intelligent **Early Stop** threshold detection: stops immediately once messages pass the time window cutoff (`scan_day0`), bypassing hundreds of thousands of historical pages.
  - **Zero 2GB Disk Footprint**: Eliminates writing and deleting 2GB+ temporary plaintext databases on SSD, dramatically reducing disk write wear and temporary storage spikes.
  - Speed improved from 20~30s down to sub-second / 1~2s for typical time windows (7d / 30d / 90d).
- **Graceful Fallback & Full Backward Compatibility**:
  - Classic full decryption mode `--source sql` remains 100% intact and available as fallback or explicit option.
  - Automatic fallback: if streaming encounters malformed roots or checkpoint anomalies, seamlessly degrades to classic extraction.
- **Unit & Performance Tests**:
  - Added `tests/test_reverse_streaming_extract.py` covering multi-level B-tree reverse traversal, AES algorithm caching, early stop thresholds, and CLI source options (67 total passed tests).

## [1.5.1] - 2026-11-01

### Added
- Conventional Commits and GitHub Release workflows.
- Modular architecture decoupling (`ntqq_crypto`, `ntqq_db`, `ntqq_parser`, `ntqq_core` facade).
- Toolhelp32 process inspection fallback via `ctypes` when `psutil` is missing.
- Performance optimizations: N-gram keyword extraction 2x speedup, stream decryption 1MB buffering and preallocation, and SQLite read-only mmap.
