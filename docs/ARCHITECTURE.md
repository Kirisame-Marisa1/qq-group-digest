# 技术细节

## 1. SQLCipher 页面格式（实测）

```
文件 = [1024 字节 NTQQ 自定义头] + [N × 4096 字节页]

页 N>1:  [密文 4048B] [IV 16B] [HMAC-SHA1 20B] [12B 未用]
页 1  :  [salt 16B] [密文 4032B] [IV 16B] [HMAC-SHA1 20B] [12B 未用]

从页 1 密文解出来的前 16 字节**就是 SQLite 文件头**（"SQLite format 3\0"），
必须原样保留，尤其 offset 20 的 reserved space = 80。
```

### 踩过的坑

一开始按某些现成工具的做法「自己重建 page1 头」，把 reserved 写成 48。
结果整库报：

```
Fragmentation of 32 bytes reported as 0 on page N
Tree X page Y cell Z: invalid page number ...
```

**改成保留 page1 原始头之后，错误从几十个降到 0，正文立刻能读出来。**

校验解密是否正确的最快方法：用 page1 的 HMAC-SHA1 验证密钥；再用 `sqlite_master` 能否读出表名验证页面格式。

## 2. 索引与流式抽取设计

`group_msg_table` 的主键 `40001` 是消息 ID，实测在同一时刻的消息 `rowid` 可能跳跃较大，不能假设全表严格递增。
当前架构提供三档协同抽取方式（`ntqq_build.py`）：

1. **`--source stream`（默认流式抽取）**：通过 `ntqq_slice.py` 直接在源库页面（含 `-wal` 安全快照 `ntqq_snapshot.py`）按页解密并逆序遍历 B-tree 叶子页（`40050` 时间戳），一旦连续超过安全余量超出时间窗即早停，避免将整个 4GB+ 库解密为明文再扫全表。
2. **`--source wal` / `--source sql`（传统 SQL 回退）**：通过 `ntqq_decrypt.py` 逐页流式解密为明文副本后，以 keyset pagination（`rowid < last_rowid`）安全遍历并写入本地滚动索引 `index.db`。
3. **滚动索引与 FTS5 同步**：`index.db` 在 `ntqq_db.py` 中维护 `messages` / `groups` / `c2c_messages` / `c2c_groups` / `media_ocr` / `micro_digests` 表，并通过 `PRAGMA recursive_triggers = ON` 与 FTS5 `AFTER INSERT/DELETE/UPDATE` 触发器保持 `messages_fts` 实时一致。

## 3. WAL 与一致性快照

源库为 WAL 模式（文件头 offset 18/19 = 2）。`ntqq_snapshot.py` 在抽取前会短暂、只读地将 `nt_msg.db`、`nt_msg.db-wal`、`nt_msg.db-shm` 一并复制到独立临时工作目录后再解密分析，既绝不写入源库，也能覆盖尚未 checkpoint 回主库的最新消息；如遇短暂文件锁竞争则自动退避重试并在退出时清理临时快照。

## 4. 转发消息

外层 `40800` 里 `45002=16`，`48602` 是 XML（`brief="[聊天记录]"`）。
**真正的正文在 `40900` 列**：repeated 结构，每一条子记录就是一条被转发的原始消息，
字段编号与外层主表一致：

| 字段 | 含义 |
|---|---|
| `40020` | 发送者 UID |
| `40033` | 发送者 QQ |
| `40050` | 时间（Unix 秒） |
| `40093` / `40094` | 昵称 / 群名片 |
| `40800` | 该条消息的正文 Protobuf |

`ntqq_parser.py` 的 `forward_text()` 会遍历每条子记录 `40800` 的全部叶子节点而非仅首段，避免因引用段或图片段排在首位而丢失正文；内嵌的 XML 卡片同时兼容单双引号与 HTML 实体反转义。

## 5. 三级媒体获取与本地 OCR

1. **多媒体 CDN（原图主力）**：从消息 `40800` 提取 `45816`（host）与 `45802/45803/45804`（`/download?appid=1407&fileid=...&spec=0`），并通过 `ntqq_core.is_allowed_cdn_host()` 校验主机白名单，结合 `ntqq_key.scan_rkeys()` 内存扫出的会话 `rkey` 拉取并核验 MD5 与文件魔数扩展名。
2. **本机缓存** `nt_data\Pic\`：按文件大小择优匹配（若 `--prefer-local` 则优先），用于已渲染图片的快速命中。
3. **旧 gchat 兜底**：`https://gchat.qpic.cn/gchatpic_new/0/0-0-<MD5大写>/0`。
4. **WinRT 本地 OCR（`ntqq_ocr.py` + `winrt_ocr.ps1`）**：纯本地调用 Windows Runtime OCR 识别群图文字并缓存至 `media_ocr`，与 `ntqq_report.py --search` 联动检索。

## 6. 生态分析、HTML 渲染与安全边界

- **结构化分析管线**：`ntqq_normalizer.py`（消息降噪与刷屏折叠）→ `ntqq_segmenter.py`（话题分段）→ `ntqq_summary_tree.py`（微摘要树缓存）→ `ntqq_report_schema.py`（结构化与可溯源 `[Ref: #seq]` 简报）。
- **扩展洞察模块**：`ntqq_links.py`（资源/外链与跟踪参数清洗）、`ntqq_action_items.py`（待办/决议雷达）、`ntqq_social.py`（群友互动拓扑）、`ntqq_wrapped.py`（Chat Wrapped 回顾）、`ntqq_obsidian.py`（Obsidian Vault 导出与路径穿越防护）。
- **离线多视图 HTML（`ntqq_html.py` + `templates/digest_report.html`）**：通过 `<script type="application/json">` 安全序列化及基于 `data-action` 的事件委托渲染 5 种离线视图，防止消息正文注入脚本。

## 7. 版本漂移

`schema/ntqq-<版本>.json` 里除字段映射外还有一个 `structure`（首次运行自动写入），
是 `sqlite_master` 的表/列清单。`ntqq_build.py` 每次比对，不一致就：
1. 以 exit code 3 停止（**不产出任何总结**，避免基于错列瞎编）
2. 写出 `run/schema_report.md`

重新解析的判据见 `.agents/skills/qq-group-digest/SKILL.md` 第 7 节。
