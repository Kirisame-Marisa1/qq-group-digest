# Digest Ecosystem Evolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the 5 major ecosystem capabilities inspired by leading open-source chat intelligence projects: Links & Resources Digest, Action Items & Decision Radar, Zero-dependency Windows WinRT OCR Indexing & Search, Obsidian Second Brain Export, and Social Interaction Network Graph & Chat Wrapped.

**Architecture:** Maintain the core philosophy of `qq-group-digest` (local, read-only, zero account risk, zero intrusive hooks, zero heavy binary dependencies). Each capability is implemented as a standalone, decoupled modular engine in `scripts/ntqq_*.py` with pure input/output interfaces, covered by unit tests in `tests/`, and then plugged cleanly into `ntqq_report.py`, `ntqq_html.py`, and `index.db`.

**Tech Stack:** Python 3.10+, SQLite3 with FTS5, Windows 10/11 native WinRT OCR (`Windows.Media.Ocr.OcrEngine`), Pure JavaScript/SVG for interactive HTML visualizations (zero external npm bundle).

**Spec:** Based on open-source benchmark analysis (`shuakami/qq-chat-exporter`, `CutrelyAlex/QQChatAnalyzer`, `Wxw-Gu/TraceMemo`, `LC044/WeChatMsg`, `ChatMemory`).

## Global Constraints

- **Platform & Security:** Must remain completely read-only on QQ databases. Must not require QQ login credentials or inject QQ process. DPAPI protected where keys are cached.
- **Zero Heavy Dependencies:** The OCR component MUST NOT introduce heavy packages like PaddleOCR, PyTorch, or Tesseract; it must leverage Windows 10/11 built-in `Windows.Media.Ocr` engine.
- **Backward Compatibility:** All existing CLI flags (`--active`, `--brief`, `--hours`, `--search`, `--class-mode`, `--structured`) must remain functional without breaking changes.
- **Test Integrity:** All existing 68 tests must continue to pass throughout every task.

## Review Focus

1. **URL normalization:** Trailing tracking parameters (`spm_id_from`, `utm_source`) stripped without breaking valid URL query parameters.
2. **Action item false positives:** Casual conversations ("记得吃饭", "今天天气不错") must not be flagged as official task action items.
3. **WinRT OCR availability:** Graceful degradation on non-Windows systems or when an image cannot be decoded; never crash the report pipeline.
4. **Obsidian wikilink escaping:** Special characters (`[`, `]`, `|`, `#`) in nicknames or glossary terms must not corrupt Obsidian YAML frontmatter or Wikilink syntax.
5. **Social graph edge explosion:** Large groups with 50,000+ messages must aggregate edges efficiently without freezing HTML DOM rendering (top N nodes + pruning).

---

### Task 1: Links & Resources Archive Engine (`ntqq_links.py`)

**Files:**
- Create: `scripts/ntqq_links.py`
- Create: `tests/test_ntqq_links.py`
- Modify: `scripts/ntqq_report.py`
- Modify: `scripts/ntqq_html.py`

**Interfaces:**
- Consumes: List of message dictionaries `[{'text': str, 'sender': str, 'sender_name': str, 'timestamp': int, 'seq': int}]`
- Produces: `extract_links_and_resources(messages: list[dict]) -> list[dict]` where each item is:
  ```python
  {
      "url": str,
      "clean_url": str,
      "category": str, # "code", "article", "video", "drive", "doc", "other"
      "domain": str,
      "context": str,
      "sender_name": str,
      "sender_qq": str,
      "timestamp": int,
      "seq": int,
      "share_count": int
  }
  ```

- [ ] **Step 1: Write the failing test**

Write `tests/test_ntqq_links.py` testing URL extraction, tracking param stripping, domain categorization (GitHub -> code, Bilibili -> video, Pan.baidu -> drive), and deduplication.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ntqq_links.py -v`
Expected: FAIL (ModuleNotFoundError: No module named 'scripts.ntqq_links')

- [ ] **Step 3: Implement `extract_links_and_resources` in `scripts/ntqq_links.py`**

Implement URL regex matching, URL cleaning via `urllib.parse`, category classification table, and recommendation context capture.

- [ ] **Step 4: Integrate into `ntqq_report.py` and `ntqq_html.py`**

Add `--links` option to `ntqq_report.py` and embed a "📦 群内资源与链接合辑" section in markdown and HTML templates.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_ntqq_links.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add scripts/ntqq_links.py tests/test_ntqq_links.py scripts/ntqq_report.py scripts/ntqq_html.py
git commit -m "feat(links): add links and resources archive extractor"
```

---

### Task 2: Action Items & Decisions Radar Engine (`ntqq_action_items.py`)

**Files:**
- Create: `scripts/ntqq_action_items.py`
- Create: `tests/test_ntqq_action_items.py`
- Modify: `scripts/ntqq_report.py`
- Modify: `scripts/ntqq_html.py`

**Interfaces:**
- Consumes: List of message dictionaries `[{'text': str, 'sender': str, 'sender_name': str, 'timestamp': int, 'seq': int}]`
- Produces:
  ```python
  def extract_action_items_and_decisions(messages: list[dict]) -> dict:
      # returns {"actions": [ActionItem], "decisions": [DecisionItem]}
  ```
  Where `ActionItem` contains `assignee`, `task`, `deadline`, `confidence`, `seq`, `sender_name`, `timestamp`.
  Where `DecisionItem` contains `decision`, `announcer`, `seq`, `timestamp`.

- [ ] **Step 1: Write the failing test**

Write `tests/test_ntqq_action_items.py` covering:
- Mentions with deadline (`@张三 请于周五前提交周报`) -> `ActionItem(assignee='张三', deadline='周五前')`
- General action (`大家记得今晚前填写表格`) -> `ActionItem(assignee='大家', deadline='今晚前')`
- Decision keyword (`经讨论，方案确定采用方案B`) -> `DecisionItem`
- Casual talk ("今天下午吃了汉堡，记得挺好吃的") -> Filtered out (not an action item).

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ntqq_action_items.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Implement `scripts/ntqq_action_items.py`**

Rule-based pattern matcher, deadline regexes, assignee mention parser, decision keyword triggers, and confidence scoring.

- [ ] **Step 4: Integrate into `ntqq_report.py` and `ntqq_html.py`**

Add `--actions` flag to `ntqq_report.py` and display the Radar section in `--class-mode`, `--structured`, and HTML dashboard.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_ntqq_action_items.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add scripts/ntqq_action_items.py tests/test_ntqq_action_items.py scripts/ntqq_report.py scripts/ntqq_html.py
git commit -m "feat(actions): add action items and decisions radar"
```

---

### Task 3: Zero-Dependency Windows WinRT OCR Indexer & Search (`ntqq_ocr.py`)

**Files:**
- Create: `scripts/ntqq_ocr.py`
- Create: `tests/test_ntqq_ocr.py`
- Modify: `scripts/ntqq_media.py`
- Modify: `scripts/ntqq_build.py`
- Modify: `scripts/ntqq_report.py`

**Interfaces:**
- Consumes: Image file path or image MD5 in `<root>/media/`
- Produces:
  ```python
  def ocr_image(image_path: str) -> str:
      """Returns recognized text using native Windows.Media.Ocr, or empty string if failed."""
  def init_ocr_table(conn: sqlite3.Connection):
      """Ensures media_ocr table exists in index.db."""
  def index_ocr_for_media(db_path: str, media_dir: str, days: int = 30) -> int:
      """Scans media, runs OCR, and records in media_ocr table."""
  ```

- [ ] **Step 1: Write the failing test**

Write `tests/test_ntqq_ocr.py` with mock / sample test image and database insertion checks, plus fallback handling when not on Windows or image is invalid.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ntqq_ocr.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Implement `scripts/ntqq_ocr.py`**

Implement Windows Runtime OCR invocation using lightweight PowerShell runner executing `[Windows.Media.Ocr.OcrEngine]`. Maintain `media_ocr(md5 TEXT PRIMARY KEY, ocr_text TEXT, language TEXT, updated_at INTEGER)`.

- [ ] **Step 4: Hook into `ntqq_media.py` and `ntqq_report.py --search`**

- In `ntqq_media.py`: automatically queue downloaded pictures for OCR if `--ocr` is enabled.
- In `ntqq_report.py`: `--search <keyword>` queries both message texts and `media_ocr`, displaying `[📷 识图命中: "..."]` when text matches inside an image.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_ntqq_ocr.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add scripts/ntqq_ocr.py tests/test_ntqq_ocr.py scripts/ntqq_media.py scripts/ntqq_build.py scripts/ntqq_report.py
git commit -m "feat(ocr): add zero-dependency Windows WinRT OCR indexer and search"
```

---

### Task 4: Obsidian / Second Brain Export Module (`ntqq_obsidian.py`)

**Files:**
- Create: `scripts/ntqq_obsidian.py`
- Create: `tests/test_ntqq_obsidian.py`
- Modify: `scripts/ntqq_report.py`

**Interfaces:**
- Consumes: Report data dictionary (metadata, messages, summaries, links, actions, topics, quotes)
- Produces:
  ```python
  def export_to_obsidian(report_data: dict, output_dir: str, options: dict = None) -> str:
      """Generates an Obsidian-compliant markdown file with YAML frontmatter, [[wikilinks]], callouts, and chat-qq code blocks."""
  ```

- [ ] **Step 1: Write the failing test**

Write `tests/test_ntqq_obsidian.py` checking YAML frontmatter formatting, Obsidian callouts (`> [!tip]`, `> [!todo]`), Wikilink conversion, and ````chat-qq` block styling.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ntqq_obsidian.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Implement `scripts/ntqq_obsidian.py`**

Generate YAML metadata block, convert glossary terms to `[[...]]`, format callouts, and write out clean `.md` to the specified Obsidian vault path.

- [ ] **Step 4: Integrate CLI into `ntqq_report.py`**

Add `--export obsidian` and `--obsidian-vault <dir>` options to `ntqq_report.py`.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_ntqq_obsidian.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add scripts/ntqq_obsidian.py tests/test_ntqq_obsidian.py scripts/ntqq_report.py
git commit -m "feat(obsidian): add Obsidian second-brain export format"
```

---

### Task 5: Social Interaction Network Graph & Chat Wrapped (`ntqq_social.py`, `ntqq_wrapped.py`)

**Files:**
- Create: `scripts/ntqq_social.py`
- Create: `scripts/ntqq_wrapped.py`
- Create: `tests/test_ntqq_social.py`
- Create: `tests/test_ntqq_wrapped.py`
- Modify: `scripts/ntqq_report.py`
- Modify: `scripts/ntqq_html.py`

**Interfaces:**
- Consumes: List of messages in the target period
- Produces:
  - `build_social_graph(messages: list[dict], top_n: int = 30) -> dict` with nodes and weighted links.
  - `build_chat_wrapped(messages: list[dict], period_name: str) -> dict` with yearly/monthly milestones, badges, high-impact quotes, and night-owl stats.
  - Interactive SVG / Canvas visual components in `ntqq_html.py`.

- [ ] **Step 1: Write the failing tests**

Write `tests/test_ntqq_social.py` (mention/quote network calculation) and `tests/test_ntqq_wrapped.py` (wrapped statistics and badge calculation).

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ntqq_social.py tests/test_ntqq_wrapped.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Implement `scripts/ntqq_social.py` and `scripts/ntqq_wrapped.py`**

Calculate directed interaction edges (A mentions B, A quotes B, A responds to B within 60s). Calculate wrapped records (earliest message, latest message, active streak, top keywords, longest monologue).

- [ ] **Step 4: Integrate into `ntqq_report.py` and `ntqq_html.py`**

- Add `--social` and `--wrapped` flags to `ntqq_report.py`.
- Add interactive SVG social graph modal / panel into HTML reports.
- Support `--wrapped --html <path>` to produce a standalone Wrapped Card Deck view.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_ntqq_social.py tests/test_ntqq_wrapped.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add scripts/ntqq_social.py scripts/ntqq_wrapped.py tests/test_ntqq_social.py tests/test_ntqq_wrapped.py scripts/ntqq_report.py scripts/ntqq_html.py
git commit -m "feat(social): add social graph analyzer and chat wrapped mode"
```

---

### Task 6: Comprehensive Regression, Documentation & Release Polish

**Files:**
- Create: `tests/test_digest_ecosystem_e2e.py`
- Modify: `README.md`
- Modify: `SKILL.md`

- [ ] **Step 1: Write E2E test covering the entire new pipeline**

Write `tests/test_digest_ecosystem_e2e.py` testing links extraction, action items extraction, OCR query matching, Obsidian note generation, social graph calculation, and wrapped generation in a unified pipeline.

- [ ] **Step 2: Run full test suite**

Run: `pytest -v`
Expected: All tests pass (68 existing + all new tests).

- [ ] **Step 3: Update `README.md` and `SKILL.md`**

Document all 5 new capabilities, command-line arguments, architecture, and examples.

- [ ] **Step 4: Commit**

```bash
git add tests/test_digest_ecosystem_e2e.py README.md SKILL.md
git commit -m "docs: update README and SKILL for digest ecosystem evolution"
```

