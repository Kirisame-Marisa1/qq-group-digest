# 自建加载器（Selfloader）核心机制：`session.init` 的 4 参契约

**日期**：2026-10-10
**状态**：已在静态与真机层面验证通过（落地于 `injector/selfloader.mjs`）。
**为什么重要**：补齐后可以**不依赖 NapCat 运行时或 LiteLoader 框架**完成注入，仅需 `injector/boot/` 下 57 KB 的最小启动器即可拿到经过认证的 `KernelMsgService`。

---

## 1. 调用约定与 3 个监听类

原生层 `session.init` 接收 4 个参数：

```js
class Qae {                       // 第 2 参
  onMSFStatusChange(e, n) {}
  onMSFSsoError(e, n) {}
  getGroupCode(e) {}
}

class Hae {                       // 第 3 参
  dispatchRequest(e) {}
  dispatchCall(e) {}
  dispatchCallWithJson(e) {}
}

class Sae {                       // 第 4 参
  onNTSessionCreate(e) {}
  onGProSessionCreate(e) {}
  onSessionInitComplete(e) {}
  onOpentelemetryInit(e) {}       // init 完成的回调信号
  onUserOnlineResult(e) {}
  onGetSelfTinyId(e) {}
}
```

**关键结论：它们都是「带固定方法名的空类」**——C++ 原生层只按**方法名**查找回调，
因此自建加载器只要提供**同名方法**即可。此前传裸对象 `{}` 会让 QQ 进程直接崩溃，根因就是这 12 个方法名缺失。

---

## 2. 第 1 参（`sessionInitConfig`）的完整结构

```js
{
  selfUin: String(uin),
  selfUid: String(uid),
  desktopPathConfig: { account_path: accountPath },
  clientVer: fullQQVersion,           // 如 "9.9.36-53644"
  a2: "", d2: "", d2Key: "", machineId: "",             // 全部留空
  platform: 1,                        // win32 = 1, darwin = 2, linux = 3
  platVer: os.release(),
  appid: QQVersionAppid,
  rdeliveryConfig: {                  // 整块留空/零值
    appKey: "", systemId: 0, appId: "", logicEnvironment: "",
    platform: 1, language: "", sdkVersion: "", userId: "",
    appVersion: "", osVersion: "", bundleId: "", serverUrl: "", fixedAfterHitKeys: [""]
  },
  defaultFileDownloadPath: join(accountPath, "temp"),
  deviceInfo: {
    guid: machineGuid,                // 形如 XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX
    buildVer: fullQQVersion,
    localId: 2052,
    devName: os.hostname(),
    devType: os.type(),
    vendorName: "",
    osVer: os.release(),
    vendorOsName: os.type(),
    setMute: false,
    vendorType: 0
  },
  deviceConfig: '{"appearance":{"isSplitViewMode":true},"msg":{}}'
}
```

---

## 3. 四个关键时序要点（真机验证定案）

1. **`session.init` 的 4 参必须是带 12 个固定方法名的类实例**：`session.init(siCfg, new Qae(), new Hae(), otel)`。
2. **顺序必须是「先登录、后 init」**：`siCfg` 里的 `selfUin / selfUid` 必须来自登录完成后的 `getLoginList()` 结果。
3. **启动会话**：调用 `session.startNT(0)` / `startupWrapper.start()`。
4. **`onOpentelemetryInit` 回调可能晚到（>20 s）**：但 `session.getMsgService()` 往往早已就绪，因此等待 20 s 后直接轮询 `getMsgService()` 是否拿到 `getMsgsBySeqAndCount` 即可，不应因回调晚到直接判定失败。
