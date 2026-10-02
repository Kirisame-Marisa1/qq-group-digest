---
name: qq-group-digest
description: 本地解密 QQ 聊天数据库并总结群聊。当用户问「QQ群聊了什么」「总结一下XX群」「今天各群都在聊什么」「现在群里在聊什么」「某关键词在哪个群出现过」或要求统计群消息量/趋势/话题/观点时使用。全程本地、不登录、不联网；也用于查询/更新群聊关键词库。
compatibility: {}
---

# QQ 群聊自动总结

## 一句话

解密本机 `nt_msg.db` → 建滚动索引 → 出报告（可含图片识别）→ 我读报告做语义总结 → 写成 Markdown。

**不登录、不跑协议端、不注入 QQ、不改 QQ 数据。** 只读本地文件 + 读一次 QQ 进程内存拿密钥 + 按 md5 从腾讯 CDN 取图。

## 0. 铁律

1. **每一次输出都必须声明数据截止时间，写在第一句。**
   格式：「**数据截止到 YYYY-MM-DD HH:MM**（距现在 N 分钟）」。
   报告脚本会把这个时间打在标题下方，**照抄，不要自己算**。
2. **取不到数据就明说，绝不编造。**
3. **密钥不写进对话。** 只汇报「校验通过/失败」。
4. **图片必须报覆盖率**：「本窗口图片 N 张，本机可取 X 张，CDN 补到 Y 张，已识别 Z 张」。

## 1. 目录布局（全部在 D:\QQChatCache）

```
D:\QQChatCache\
├─ app\            代码：scripts\  schema\  config.json
├─ data\           解密后的明文库 plain\  +  滚动索引 index.db
├─ keys\           密钥缓存 key_map.json
├─ media\          下载到的图片（按 md5 前两位分子目录）
├─ knowledge\glossary\   关键词库 terms.json + 关键词库.md
├─ output\         总结产出
├─ run\            临时文件
└─ logs\
```

**所有新增产物都放这里，不要写到桌面或别处。** 明文库可随时整目录删除，下次自动重建。

Python：`D:\Python38\python.exe`（已装 cryptography / protobuf，**不要装 jieba**）。

## 2. 标准流程

```powershell
$env:PYTHONIOENCODING='utf-8'
cd 'D:\QQChatCache\app'
& 'D:\Python38\python.exe' -X utf8 'scripts\ntqq_build.py' --days 30
```

出报告（**加 --out 写到 output\，再用 read 分段读**）：

```powershell
& 'D:\Python38\python.exe' -X utf8 'scripts\ntqq_report.py' --active --hours 6 --brief --max-msgs 40 --out 'D:\QQChatCache\output\_tmp.md'
& 'D:\Python38\python.exe' -X utf8 'scripts\ntqq_report.py' --class-mode --days 7 --out '...'
& 'D:\Python38\python.exe' -X utf8 'scripts\ntqq_report.py' --group <群号> --days 2 --out '...'
& 'D:\Python38\python.exe' -X utf8 'scripts\ntqq_report.py' --search <关键词> --days 7 --out '...'
& 'D:\Python38\python.exe' -X utf8 'scripts\ntqq_report.py' --list-groups
```

产出命名：单群 `output\<群名>_<日期>.md`；多群 `output\全部群_<日期_时分>.md`。

## 3. QQ 要不要开着？

- **第一次运行**或密钥缓存失效时：**必须 QQ 已登录并正在运行**（密钥只在进程内存）。
- **缓存有效时完全不需要 QQ**：脚本先用 page1 HMAC 校验，通过就跳过内存扫描。
- 密钥跨 QQ 重启是否仍有效**尚未实测**，工具会自动判断，不要凭猜回答。

## 4. 图片：不用点开也能拿到

**实测结论**：腾讯群图可按 md5 直接取原图，**不需要登录/cookie/签名**：

```
https://gchat.qpic.cn/gchatpic_new/0/0-0-<MD5大写>/0
```

- 下载回来的文件 MD5 正好等于消息里的 md5（就是原图），字节数与消息声明的 filesize 一致。
- 实测成功率约 **80%**，失败全是 404（服务端已清理）。
- QQ 只把你**点开过**的图落到 `nt_data\Pic\`，本机命中率可能只有 5%；直连能把大部分补回来。

```powershell
& 'D:\Python38\python.exe' -X utf8 'scripts\ntqq_media.py' --groups <群号,群号> --days 30 --workers 4
```

- 先查本机 Pic（命中不走网络），缺的走 CDN，**按文件头 magic 修正扩展名**（QQ 常「叫 .jpg 实为 PNG/GIF」）。
- 输出 `media\manifest.jsonl`：md5 / 来源 / 路径 / 群 / 发送者 / 时间。

**哪些图必须识**（优先级从高到低）：
1. 出现在 @全体成员 / 通知 / 公告 附近的图
2. 文件 > 100KB（截图通常大，表情包小）
3. 同一 md5 只出现 1 次（反复出现的必是万用表情包，可跳过）
4. 转发消息里的图

纯表情包可跳过，但**必须报覆盖率**。

## 5. 关键词库（knowledge\glossary\）

群聊里有两类词必须理解才能读懂内容：**群内黑话**（如东方圈的「车万/THO/打则/花赛」）和**网络流行语**（如「耍起/扫码/芝士雪豹」）。

```powershell
& 'D:\Python38\python.exe' -X utf8 'scripts\ntqq_glossary.py' --scan --days 60 --top 60
& 'D:\Python38\python.exe' -X utf8 'scripts\ntqq_glossary.py' --context 车万 --limit 15
& 'D:\Python38\python.exe' -X utf8 'scripts\ntqq_glossary.py' --apply <draft.json>
& 'D:\Python38\python.exe' -X utf8 'scripts\ntqq_glossary.py' --list
```

**编写流程**：
1. `--scan` 拿候选；`--context` 拿证据（时间/群/发言人/上下文）。
2. 网络流行语去**网上搜**确认出处与含义，不要凭印象编。
3. 汇总成 draft.json（term/type/meaning/origin/groups/evidence），`--apply` 入库。
4. **拿不准的写「待确认」并说明依据。**

**更新时机**：每次总结顺手跑一次 `--scan`，发现新词就补。

**⚠️ 同一个词在不同群可能意思完全不同**，必须结合群和上下文判断。已知例子：
`马头` 在炒股群 = 马斯克的 SpaceX；在 LOL/电竞语境 = 选手 TheShy 的外号；在西餐语境 = 意式猪脸肉 guanciale。
词库用 `meanings` 数组存多义（每条带 scope / meaning / origin / groups / evidence）。
`--context <词>` 会先打印「按群分布」，先看分布再判断该用哪个义项。

## 6. 输出格式

五件事：**聊了什么 / 聊了多少多久 / 话题关键词 / 观点与结论 / 量化与趋势**。

- **重点速览**（通知/公告/@全体/报名截止/考试安排/资料分享）**单独成节排在最前**。
- **班群格外详细**：篇幅不设上限，逐条写「谁、何时、说了什么、结论是什么」；学校事务逐条列；涉及用户的 @ 单独高亮；昵称原文保留。班群号写在 config.json 的 class_groups。
- **多群场景**：每个有对话的群都要回答到「主要聊了什么」，按热度排序。
- **转发消息必须展开**：正文在 40900 列（repeated，每条子记录就是一条被转发的原始消息）。

## 7. 版本漂移后重新解析私有表结构

`ntqq_build.py` 每次比对 `sqlite_master` 与 `schema/ntqq-<版本>.json` 的 structure；
不一致会 **exit 3** 并写出 `run/schema_report.md`，**不产出任何总结**。

处理：读报告 → 对候选列抽样（类型/基数/极值/是否秒级时间戳/是否命中群号）→ 按判据重推映射 → 写新 schema 文件 → **跑冒烟用例**（某活跃群最近 100 条：时间合理、群号正确、正文非空 > 50%）通过才保留。

## 8. 已知限制

| 限制 | 说明 |
|---|---|
| 图片 | 本机已落盘 + CDN 未清理的部分（约 80%+）；视频/语音无法理解 |
| 视频/语音/文件 | 只能拿到文件名 |
| WAL | 源库是 WAL 模式，最新一小段可能未落盘；以报告的「数据截止」为准 |
| 损坏页 | 脚本自动重解密重试 3 轮，仍失败按天补取并在报告顶部标「⚠ 数据缺口」 |
| 昵称 | 群名片 → 全局昵称 → QQ 号 → 「未知」 |
| 账号风险 | 极低：不登录，只在取图时访问腾讯 CDN |
