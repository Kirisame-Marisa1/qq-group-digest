# L1b 补缺口：一次完整动作清单 × 服务端可见性

**日期**：2026-10-10
**依据**：`injector/run_selfloader.ps1` + `injector/selfloader.mjs` + 登录/上报流程分析。
**结论先行**：服务端能知道 **① 一次新设备登录、② 登录时上报的设备信息、③ 每一次按 seq 取数的请求明细**；
其余的读库、建索引、出报告**全部只发生在本地**，服务端无从得知。

---

## 1. 一次补缺口 = 完整动作链

| # | 动作 | 谁做的 | 服务端可见？ |
|---|---|---|---|
| 1 | `tools/gap_plan.py` 读本地 `index.db` 算出缺口表 `injector/out/fill-plan.json` | 本地 Python | ❌**只在本地** |
| 2 | 记录 QQ 6 个关键文件的 SHA256 基线 | 本地 | ❌ 只在本地 |
| 3 | **`taskkill QQ`**（本地把 QQ 全部杀掉） | 本地 | ✅**间接可见**：服务端会看到会话断开（掉线），与"用户自己关 QQ"形态一致 |
| 4 | 启动 `NapCatWinBootMain.exe` + `NapCatWinBootHook.dll`，把 `selfloader.mjs` 加载进 QQ 进程 | 本地注入 | ❌ 注入动作本身服务端看不到（无本地端口监听） |
| 5 | **QQ 以注入态启动并登录**（`quickLoginWithUin`，账号走环境变量 `QQ_QUICK_UIN`） | QQ ↔ 服务端 | ✅✅**最明显的信号：一次完整的新设备登录** |
| 6 | 登录过程中**主动上报**（见 §2.2） | QQ → 服务端 | ✅✅**服务端必然收到** |
| 7 | `selfloader.mjs` 按表逐条调 `getMsgsBySeqAndCount` 取数（翻页直到缺口补完） | QQ ↔ 服务端 | ✅✅**每次请求都可见**（见 §2.3） |
| 8 | 取回的消息**由 QQ 自己写进本地 `nt_msg.db`**，同时落盘副本 `filled-messages.jsonl` | 本地 | ❌ 写的是本机文件 |
| 9 | 拆解：`taskkill QQ` → 清理 `NapCatWinBootMain` → **复检 6 文件哈希 + 端口释放** | 本地 | ✅ 看到一次断线 |
| 10 | **正常启动 QQ**（`Start-Process QQ.exe`）→ 用户恢复使用 | QQ ↔ 服务端 | ✅ 一次正常登录（**官方启动方式**） |

---

## 2. 服务端能看到的三类信息

### 2.1 一次新设备登录
这是**最大、最不可掩盖**的信号。`run_selfloader.ps1` 的设计目标之一就是
「**ONE injection, ONE login event, then out**」——把登录次数压到最低。

### 2.2 登录时上报的设备信息
登录配置包含：
- `selfUin` / `selfUid`
- `appid`、`clientVer`（如 `9.9.36-53644`）
- `deviceInfo`: `guid`(machineGuid 大写加连字符)、`devName`、`devType`、`osVer`、`platform`、`localId: 2052`
- `rdeliveryConfig`: 整块空/零值
- `a2/d2/d2Key/machineId`: **全部空串**

→ 服务端拿到的是：**同一台机器（同一 machineGuid）+ 同一账号 + 同一版本**，但**登录次数多了一次**。

### 2.3 每一次按 seq 取数的请求
`getMsgsBySeqAndCount` 走的是 **QQNT 内核的服务端接口**。服务端对每次请求能看到：

| 字段 | 含义 | 随机化对策 |
|---|---|---|
| `peerUid` | 取哪个群 | 群/缺口顺序洗牌打乱（不按群号顺序扫） |
| `start`(seq) | 从哪一条开始 | 起点来自合并后的缺口表左端，按真实返回 `max(msgSeq)+1` 推进 |
| `count` | 要多少条 | **随机 320–799**（最后一页按剩余量收窄） |
| 时间/间隔 | 请求频次 | **随机 2–9 s** |
| 登录会话 | 来自哪次登录/设备 | 只登录一次，整轮共用 |

---

## 3. 只在本地进行的部分（服务端无感）

1. 读 `index.db` 算缺口、生成 `fill-plan.json`；
2. 备份与比对 QQ 文件哈希；
3. 注入器把 JS 载入 QQ 进程这个**动作本身**；
4. `selfloader.mjs` 读表、按缺口子区间 `holes` 过滤写 `filled-messages.jsonl`、写 `probe-selfloader.jsonl`；
5. 取回的消息落进本地 `nt_msg.db` 及 `import_filled.py` 合并入 `index.db`；
6. 解密本地库、建索引、覆盖率体检、生成报告——**全程不联网**。

---

## 4. 一句话总结

> **补缺口对服务端而言不是"隐形"的**：它看到一次新设备登录、一份带 machineGuid 的设备上报、
> 以及按序取数请求。它看不到的是我们为什么取、取了以后在本地做了什么。
> 因此这一操作的正确姿势是：**缺口够大才做、一次做完、做完立刻清理、绝不反复登录**。
