# Changelog

All notable changes to this project will be documented in this file.
This project adheres to [Semantic Versioning](https://semver.org/).

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
