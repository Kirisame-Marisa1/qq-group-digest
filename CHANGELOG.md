# Changelog

All notable changes to this project will be documented in this file.
This project adheres to [Semantic Versioning](https://semver.org/).

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
