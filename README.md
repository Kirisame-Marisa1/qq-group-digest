# qq-group-digest

> 把 QQ 群聊变成可检索、可统计、可总结的本地资料库。
> 全程**本地、只读、不登录**：不跑协议端、不注入 QQ、不改任何 QQ 数据。

一个给 AI Agent 用的 **skill**（技能）包 + 一套可独立运行的 Python 脚本。
把本机的 QQ 聊天数据库解密成明文，建滚动索引，出结构化报告（含群图片识别），
再由 AI 按你要的格式写成总结。

---

## 它解决什么

- **我 QQ 里的群到底在聊什么？**（一周 / 一个月 / 今天）
- **现在各群都在聊什么？**（多群横向汇总）
- **哪些是通知公告、哪些是闲聊？**（自动挑「重点速览」）
- **班群/工作群有什么必须知道的事？**（逐条详细模式）
- **某个关键词在哪些群、什么时候出现过？**
- **图片里的通知、二维码、表格怎么办？**（不用你点开，直接按 md5 取原图并识图）

---

## 它为什么安全

| 做法 | 说明 |
|---|---|
| ❌ 不登录 | 不使用任何 QQ 协议端（NapCat / go-cqhttp / OneBot），没有账号风控风险 |
| ❌ 不注入 | 不注入 DLL、不 Hook QQ、不修改 QQ 内存 |
| ❌ 不上传 | 聊天内容不出本机；只有取图片时会访问腾讯自己的 CDN |
| ✅ 只读 | 读本机已存在的数据库文件 + 读一次 QQ 进程内存拿解密密钥 |
| ✅ 可删 | 解密出的 4.3 GB 明文库放在缓存目录，随时整目录删除 |

> 从腾讯侧看，本工具只做了两件事：读本地文件、按图片 md5 请求一次公共 CDN。
> 拿到的图片 URL 是腾讯自家的公开图片地址（见下方「图片」一节）。

---

## 原理（三句话）

腾讯 NTQQ 把聊天记录存在本地的 **SQLCipher 加密 SQLite** 里（`nt_msg.db`，文件开头有 1024 字节自定义头）。
密钥只存在于 QQ 进程内存中（`wrapper.node` 里形如 `x'<64hex_key><32hex_salt>'` 的字符串），磁盘上没有明文副本。
拿到密钥后逐页 AES-256-CBC 解密，就得到一个普通 SQLite 库，之后全是常规 SQL 查询。

实测参数（QQ 9.9.27 时代）：

| 参数 | 值 |
|---|---|
| 自定义头 | 1024 字节 |
| page size | 4096 |
| reserved space per page | **80**（写在 page1 原始头里，别自己重建！） |
| 加密区 | 每页前 4048 字节 |
| HMAC | SHA-1，20 字节，位于页尾 |
| KDF | PBKDF2-HMAC-SHA512，4000 次迭代，32 字节密钥 |
| 模式 | WAL |

实测性能：**4.3 GB 库解密约 12–14 秒**。

---

## 安装

需要：Windows + 本机 QQ（NTQQ 版本）+ Python 3.10+。

```bash
pip install cryptography
```

把仓库放到任意位置，然后：

```bash
# 1) 复制配置模板并填入你的 QQ 号
copy config.example.json config.json

# 2) 取密钥（需要 QQ 正在运行并已登录）
python scripts/ntqq_key.py --qq <你的QQ号> -o ../keys/key_map.json

# 3) 解密 + 建索引
python scripts/ntqq_build.py --days 30

# 4) 出报告
python scripts/ntqq_report.py --active --hours 6 --brief --out ../output/_tmp.md
```

Windows 上建议用绝对路径调用解释器，并设 `PYTHONIOENCODING=utf-8`，避免中文乱码。

### 目录布局

脚本把产物都放在**数据根目录**下（`config.json` 的 `root` 指定，想放哪都行，只要可写）。
`root` 留空时的默认值：**Windows 用 `D:/QQChatCache`，Linux/macOS 用 `~/.qqchatcache`**。
下面用 `<root>` 代指：

```
<root>/
├─ app/            本仓库（scripts/ schema/ config.json SKILL.md）
├─ data/           明文库 plain/ + 索引 index.db
├─ keys/           密钥缓存
├─ media/          下载到的图片
├─ knowledge/glossary/  关键词库
├─ output/         总结产出
└─ run/            临时文件
```

---

## 常用命令

```bash
# 建/刷新滚动索引（自动判断密钥缓存是否有效、源库是否变化）
python scripts/ntqq_build.py --days 30

# 多群速览 / 单群详述 / 关键词检索 / 群列表
python scripts/ntqq_report.py --active --hours 6 --brief
python scripts/ntqq_report.py --group <群号或群名片段> --days 7
python scripts/ntqq_report.py --search <关键词> --days 30
python scripts/ntqq_report.py --list-groups

# 班群模式：重点速览在前 + 逐群不限篇幅
python scripts/ntqq_report.py --class-mode --days 7

# 图片：本机已落盘的直接用，缺的按 md5 从 CDN 取
python scripts/ntqq_media.py --groups <群号,群号> --days 30 --workers 4

# 直接问一句话（自动解析时间 + 群名 + 关键词，IDF 加权打分）
python scripts/ntqq_report.py --ask "某群今天聊某话题了吗"

# 外传前脱敏：昵称->代号、群号->群N、QQ号抹掉
python scripts/ntqq_report.py --group <群> --days 7 --anonymize

# 关键词库：扫描 / 查用法 / 入库 / 看清单
python scripts/ntqq_glossary.py --scan --days 60 --top 60
python scripts/ntqq_glossary.py --context X --limit 15
python scripts/ntqq_glossary.py --apply draft.json
python scripts/ntqq_glossary.py --list

# 群变更：新入群 / 已退群 / 改名（report 与 build 里也会自动跑）
python scripts/ntqq_group_state.py --scan
python scripts/ntqq_group_state.py --list
python scripts/ntqq_group_state.py --list --in-group
python scripts/ntqq_group_state.py --left <群号或群名片段>    # 手工补记一个已退的群
```

---

## 功能

### 数据层
- 从 QQ 进程内存提取 SQLCipher 密钥（不注入、不 Hook，普通用户权限即可；实测扫描 808 MB 用 0.8 秒）
- 流式逐页解密，**不把 4 GB 读进内存**（识别出的一些同类工具会整文件读入，容易 OOM）
- **保留 page1 原始头**（reserved=80），这是能否正确读出内容的关键
- **行级容错**：活跃写入的库会有损坏页；自动重拉快照重试 3 轮，仍失败则按天补取并记录缺口
- 版本漂移检测：比对 `sqlite_master` 结构指纹，不一致就拒绝产出并生成重解析报告

### 抽取层
- 群消息主表秒级时间戳、发送者 QQ/UID、群名片/昵称（群名片 → 全局昵称 → QQ 号 回退）
- 消息正文是 Protobuf（`40800` 列），自带一个**无依赖的 wire 解码器**
- **转发消息展开**：转发正文在 `40900` 列（repeated，每条子记录就是一条被转发的原始消息），
  而每条子记录的 `40800` **本身又是 repeated**——它的每个叶子条目才是消息段
  （`45002=1` 文本在 `45101`、`45002=2` 图片在 `45402` 文件名）。取文本要遍历全部叶子，
  只取第一条会拿到引用段/图片段，导致**转发正文整段丢失**
- 消息类型：文本 / 图片 / 表情 / 视频 / 音频 / 文件 / 引用 / 转发 / 系统
- **群变更登记**：把当前 `group_list` 与上一次的记录比对，报出 **新入群 / 已退群 /
  退群后回归 / 群改名**。首次只建基线；若一次扫描读到的群数掉了一半以上，判为库没读出来，
  **挂起退群判定**，避免批量误报

### 报告层
- 逐日 / 逐时分布、参与人数、活跃时长、消息类型构成
- 关键词提取（n-gram，无需分词库）、发言榜、被 @ 榜
- 与上一个等长窗口的趋势对比
- **重点速览**：自动挑出通知/公告/@全体/长文/转发
- 群名或群号模糊匹配、跨群关键词检索

### 图片层（不用点开也能拿到）
三级取法，默认走第二级：

1. **本机缓存** `nt_data\Pic\` —— 只有你点开/渲染过的图才落盘（实测命中 ~13%），而且多数只是缩略图。
2. **多媒体 CDN（原图）** —— 消息元素里本来就带着地址：`45816` 是 host
   （`multimedia.nt.qq.com.cn`），`45802/45803/45804` 是
   `/download?appid=1407&fileid=…&spec={0,720,198}`，其中 **spec=0 是原图**。
   这条 URL **不带 rkey，直接请求返回 400**；rkey 是 QQ 客户端**进程内存**里的会话凭据，
   用 `ntqq_key.scan_rkeys()` 只读扫出来拼上去即可（同样是
   `OpenProcess` + `ReadProcessMemory`，不注入、不写内存）。
   **需要 QQ 在运行**；rkey 缓存 6 小时。
3. **旧 gchat 兜底** `https://gchat.qpic.cn/gchatpic_new/0/0-0-<MD5大写>/0` ——
   免鉴权，但较新的图大量 404。

- 走 CDN 的每张图**保存前都校验 md5**，不符即判失败，不会把缩略图当原图存下来
- 会自动按文件头 magic 修正扩展名（QQ 经常「叫 .jpg 实为 PNG/GIF」）
- 实测：某群一天 63 张图 **63/63 全部取到、且 md5 与消息声明一致**（旧的单一路径只有 ~35%）

### 关键词库
群聊里有两类词不查就读不懂：**群内黑话**（某个圈子/展会/游戏里的专用简称）和**网络流行语**（每年都会冒出新词）。同一个词在不同群可能意思完全不同，词库为此支持一词多义。
`ntqq_glossary.py` 负责扫描候选、拉历史用法证据、入库、渲染成可读文档；释义由 AI 结合上下文与网络考证后写入（拿不准的标「待确认」）。

---

## 给 AI Agent 用

仓库里的 [SKILL.md](SKILL.md) 是一份可直接放进 Agent 技能目录的说明，
定义了铁律（必须声明数据截止时间、取不到就明说、图片必须报覆盖率）、
标准流程、输出格式、以及 QQ 版本升级后如何重新解析私有表结构。

把它放到你的技能目录（例如 DSH 的 `~/.dsh/skills/qq-group-digest/SKILL.md`），
Agent 就能在你说「总结一下 XX 群最近一周」时自动跑通全流程。

---

## 已知限制

| 限制 | 说明 |
|---|---|
| 平台 | Windows（依赖 `%USERPROFILE%\Documents\Tencent Files` 布局与进程内存读取） |
| 版本 | 字段映射按 QQ 9.9.27 实测；QQ 升级后需要按 SKILL.md 的流程重解析（工具会主动报错而不是给错结果） |
| 图片 | 只能拿到本机已落盘 + CDN 未清理的部分；**视频/语音/文件内容无法理解** |
| WAL | 源库是 WAL 模式，最新一小段可能未落盘；报告里的「数据截止」是准的 |
| 隐私 | 解密后的明文库等于完整聊天记录，注意存放位置与清理 |

---

## 致谢 / 参考

本项目的解密算法与字段语义参考了这些优秀项目的公开资料（**代码为本仓库自行实现**）：

- [NapNeko/qq_dump_db](https://github.com/NapNeko/qq_dump_db)（MIT）—— 内存取密钥与 SQLCipher 页格式
- [QQBackup/nt_msg_db_util](https://github.com/QQBackup/nt_msg_db_util)（GPL-3.0）—— `group_msg_table` 字段实测文档、`40800` protobuf 结构、行级容错导出思路
- [QQBackup/QQDecrypt](https://github.com/QQBackup/QQDecrypt) —— 1024 字节自定义头与 SQLCipher PRAGMA 说明

## 许可

MIT，见 [LICENSE](LICENSE)。

## 免责声明

仅用于处理**你自己账号**的聊天数据。请遵守当地法律与平台服务条款，
导出与分享他人聊天内容前请取得相关当事人同意。
