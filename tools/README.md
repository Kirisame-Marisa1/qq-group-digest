# tools —— 审计工具 + 缺口体检与补缺口工具链

本目录回答两类问题：

**A. 安全类（这个工具会不会碰我的 QQ 源库？会不会往外发东西？装进来的包干不干净？）**

| 脚本 | 回答的问题 | 常用命令 |
|---|---|---|
| `audit_qq_untouched.py` | 本工具**有没有写过** QQ 的源库 | `python tools/audit_qq_untouched.py --static` |
| `audit_outbound.py` | 本工具**会连哪些主机** | `python tools/audit_outbound.py`（加 `--netstat` 看运行期连接） |
| `audit_package.py` | 待安装的第三方包**干不干净** | `python tools/audit_package.py <whl/zip/tar.gz/目录>` |

**B. 数据完整性类（到底缺了多少？缺在哪？如何补到 100%？）**

| 脚本 | 回答的问题 / 作用 | 常用命令 |
|---|---|---|
| `gap_audit.py` | 静默窗口、逐日总量、最空几天逐小时、按时间窗的 seq 覆盖率 | `python tools/gap_audit.py --days 30 --top 18` |
| `seq_gap_shape.py` | 缺口**形态**：大段连续 还是 细碎散落 | `python tools/seq_gap_shape.py --group <群号> --days 30` |
| `coverage_sanity.py` | 直接测量原始库 `nt_msg.db` 的 seq 覆盖率（不经索引） | `python tools/coverage_sanity.py --days 30 --quiet` |
| `raw_check.py` | 索引里「缺」的 seq，在**原始库**里到底有没有（排除构建侧过滤） | `python tools/raw_check.py` |
| **`gap_plan.py`** | **把缺口变成一张可执行的补数表**（`L1b` 入口，生成 `injector/out/fill-plan.json`）。默认=**全部缺口 + 合并扫描段 + 自动预算** | `python tools/gap_plan.py --days 30` |
| **`import_filled.py`** | 把自建加载器落盘的取回消息副本（`filled-messages.jsonl`）幂等并入 `index.db`（`ntqq_build.py` 之后跑） | `python tools/import_filled.py` |
| **`compact_filled.py`** | 压缩副本：按 `(群,seq)` 去重（可选 `--gzip`） | `python tools/compact_filled.py --gzip` |
| **`purge_filled.py`** | **用完即删**：先校验「副本 ⊆ 索引」（有一条缺失就拒绝删），通过才删副本并记账 | `python tools/purge_filled.py` |
| **`coverage_check.py`** | **覆盖率达到 100% 了吗**（四口径交叉验证；验收看「大段缺口总长」= 0，默认排除已退群） | `python tools/coverage_check.py --days 11 --top 25` |
| **`report_parity.py`** | **报告有没有漏行**：逐群比对「报告声明的总消息 / 末条时间 / 正文明细」与 `index.db` | `python tools/report_parity.py --dir <报告目录> --since '2026-10-01 00:00'` |

### 一键编排脚本（PowerShell）

1. **准备阶段**（重建索引 → 生成合并后的全量缺口表 `injector/out/fill-plan.json`）：
   ```powershell
   powershell -ExecutionPolicy Bypass -File tools\refill_prepare.ps1
   ```
2. **注入补缺口**（自建加载器 `selfloader.mjs`，无需 NapCat 运行时，不监听任何端口，跑完自动恢复官方 QQ）：
   ```powershell
   powershell -ExecutionPolicy Bypass -File injector\run_selfloader.ps1
   ```
3. **收尾与验收**（重建索引 → 导入 `filled-messages.jsonl` 兜底副本 → 四口径覆盖率验收 → 安全清理副本）：
   ```powershell
   powershell -ExecutionPolicy Bypass -File tools\refill_finish.ps1
   ```
