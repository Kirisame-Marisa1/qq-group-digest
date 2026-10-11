// selfloader.mjs — minimal standalone QQNT in-process loader.
//
// Long-term direction for this project: use NapCat (or any community loader) ONLY as a
// technical reference, and carry the pieces we actually need ourselves. This file is
// that: no NapCat shell, no OneBot adapter, no protocol adapter, no packet backend,
// no plugin system. Just the shortest path to "an authenticated NT session we can ask
// for messages by seq".
//
// Everything below was reconstructed from the three things that can be read for free
// (no injection):
//   1. <QQ>\versions\<ver>\resources\app\package.json      (version / buildVersion)
//   2. <QQ>\resources\app\wrapper.node                     (104 exports, N-API)
//   3. NapCat's readable JS, purely as documentation for argument shapes:
//        engine.initWithDeskTopConfig(...)  /  engine.initConfig(...)
//        wrapper.NodeIQQNTWrapperSession.getNTWrapperSession("nt_1")
//        loginService.connect() / getLoginList() / quickLoginWithUin(uin)
//        session.startNT(0)
//
// Two properties matter and are the whole point:
//   * The QQ command line stays free of "-q <uin>" (account comes from env / config.json).
//   * Nothing here opens a listening socket (no HTTP, no WebUI).
//
// READ-ONLY against the account: it never sends a message, never changes a setting.

import { writeFileSync, mkdirSync, existsSync, readFileSync, readdirSync } from 'node:fs';
import { join, resolve, dirname } from 'node:path';
import { createRequire } from 'node:module';
import { release } from 'node:os';

// ---------------------------------------------------------------------------
// config (env first, then repo config.json, then safe defaults)
// ---------------------------------------------------------------------------
const SELF_DIR = dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1'));

function loadRepoConfig() {
  for (const rel of ['..', join('..', 'app')]) {
    const p = join(SELF_DIR, rel, 'config.json');
    if (existsSync(p)) {
      try { return JSON.parse(readFileSync(p, 'utf8')); } catch {}
    }
  }
  return {};
}
const REPO_CFG = loadRepoConfig();
const CFG_QQ_DIR = REPO_CFG.qq_exe ? dirname(String(REPO_CFG.qq_exe)) : '';

const QQ_EXE_DIR = process.env.QQ_DIR || CFG_QQ_DIR || 'D:\\QQNT';
const OUT_DIR = process.env.QQPROBE_OUT || join(SELF_DIR, 'out');
const OUT_FILE = join(OUT_DIR, 'probe-selfloader.jsonl');
const UIN = process.env.QQ_QUICK_UIN || String(REPO_CFG.account || '');

// Fallback probe targets if fill-plan.json is not provided:
const FILL_PLAN = (process.env.QQ_FILL_PLAN || '').trim()
  ? JSON.parse(process.env.QQ_FILL_PLAN)
  : [];

let n = 0;
function out(event, payload) {
  n += 1;
  const rec = { i: n, t: new Date().toISOString(), event, ...payload };
  let line;
  try { line = JSON.stringify(rec, (_k, v) => (typeof v === 'bigint' ? String(v) : v)); }
  catch { line = JSON.stringify({ i: n, event, err: 'unserializable' }); }
  try { mkdirSync(OUT_DIR, { recursive: true }); writeFileSync(OUT_FILE, line + '\n', { flag: 'a' }); } catch {}
  try { console.log('[self] ' + line); } catch {}
}
const brief = (v) => {
  try {
    if (v === undefined) return 'undefined';
    if (v === null) return 'null';
    if (v instanceof Error) return `${v.name}: ${v.message}`.slice(0, 300);
    if (typeof v === 'object') {
      const s = JSON.stringify(v, (_k, x) => (typeof x === 'bigint' ? String(x) : x));
      return s && s.length > 500 ? s.slice(0, 500) + '...' : s;
    }
    return String(v).slice(0, 300);
  } catch (e) { return '<?> ' + String(e).slice(0, 120); }
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------------------------------------------------------------------------
// 0.5 the session.init 4-arg contract  (solved statically 2026-10-10, see
//     docs/SELFLOADER-SESSION-INIT.md for the evidence)
//
// NapCat does:  SAe(session, U, f)  ->  session.init(U, new qae(), new Hae(), new Sae())
// The three listeners are FIXED-METHOD-NAME empty classes: the native side looks the
// names up, nothing else. Passing a bare {} (what this file used to do) is exactly why
// earlier runs killed the QQ process.
// ---------------------------------------------------------------------------
class Qae {
  onMSFStatusChange(_e, _n) {}
  onMSFSsoError(_e, _n) {}
  getGroupCode(_e) {}
}
class Hae {
  dispatchRequest(_e) {}
  dispatchCall(_e) {}
  dispatchCallWithJson(_e) {}
}
class Sae {
  onNTSessionCreate(_e) {}
  onGProSessionCreate(_e) {}
  onSessionInitComplete(_e) {}
  onOpentelemetryInit(_e) {}          // overwritten below: this is the "init done" signal
  onUserOnlineResult(_e) {}
  onGetSelfTinyId(_e) {}
}

// sessionInitConfig == what NapCat's Fae(machineGuid, appid, fullVer, uin, uid, path)
// returns. a2/d2/d2Key/machineId are EMPTY STRINGS even in NapCat, and rdeliveryConfig
// is all-empty too, so nothing here needs a credential or a real device fingerprint.
function sessionInitConfig({ uin, uid, appid, clientVer, accountPath, machineGuid }) {
  const os = require0('node:os');
  const platform = process.platform === 'win32' ? 1 : (process.platform === 'darwin' ? 2 : 3);
  const platVer = os.release();
  const devType = os.type();
  let guid = String(machineGuid || '').toUpperCase().replace(/[^0-9A-F]/g, '');
  if (guid.length >= 32) guid = `${guid.slice(0, 8)}-${guid.slice(8, 12)}-${guid.slice(12, 16)}-${guid.slice(16, 20)}-${guid.slice(20, 32)}`;
  return {
    selfUin: String(uin || ''),
    selfUid: String(uid || ''),
    desktopPathConfig: { account_path: accountPath },
    clientVer,
    a2: '', d2: '', d2Key: '', machineId: '',
    platform,
    platVer,
    appid,
    rdeliveryConfig: {
      appKey: '', systemId: 0, appId: '', logicEnvironment: '',
      platform, language: '', sdkVersion: '', userId: '', appVersion: '',
      osVersion: '', bundleId: '', serverUrl: '', fixedAfterHitKeys: [''],
    },
    defaultFileDownloadPath: join(accountPath, 'temp'),
    deviceInfo: {
      guid, buildVer: clientVer, localId: 2052,
      devName: os.hostname(), devType, vendorName: '',
      osVer: platVer, vendorOsName: devType, setMute: false, vendorType: 0,
    },
    deviceConfig: '{"appearance":{"isSplitViewMode":true},"msg":{}}',
  };
}
function require0(m) { return createRequire(import.meta.url)(m); }

// ---------------------------------------------------------------------------
// 0. version info, straight off the disk (no NapCat tables involved)
// ---------------------------------------------------------------------------
function readVersion() {
  // Two traps here, both hit on 2026-10-10:
  //   1. <QQ>\resources\app\package.json is the STALE base install ("9.9.22-40990").
  //      The build actually running lives in <QQ>\versions\<fwVer>-<build>\.
  //   2. There is no version.json in the QQ root on this install, so the running
  //      version has to be taken from the versions/ directory listing.
  const qqRoot = QQ_EXE_DIR;
  let fwVer = process.env.QQ_FW_VER || '';       // e.g. "9.9.36-53644"
  const versionsDir = join(qqRoot, 'versions');
  const dirs = existsSync(versionsDir) ? readdirSync(versionsDir) : [];
  if (!fwVer) {
    // pick the highest build number among the version directories
    fwVer = dirs.slice().sort((a, b) => {
      const na = +(a.split('-')[1] || 0), nb = +(b.split('-')[1] || 0);
      return nb - na;
    })[0] || '';
  }
  const pkgPath = join(versionsDir, fwVer, 'resources', 'app', 'package.json');
  if (!existsSync(pkgPath)) throw new Error('no package.json at ' + pkgPath + ' (candidates: ' + dirs.join(',') + ')');
  const pkg = JSON.parse(readFileSync(pkgPath, 'utf8'));
  return {
    pkgPath,
    fwVer,                                  // "9.9.36-53644"  <- the real full version
    version: fwVer,                         // what initConfig/initWithDeskTopConfig want
    buildVersion: fwVer.split('-')[1] || pkg.buildVersion,
    dirName: fwVer,
    packageName: pkg.name,
    baseVersion: pkg.version,               // the stale one, kept for the record
  };
}

// appid / qua: NapCat keeps a compatibility table; QQ itself derives them from the
// build. The values are stable per build and only feed telemetry identity, so we key
// them off the version directory name we are actually running.
const APPID_QUA = {
  '9.9.36-53644': { appid: 537391628, qua: 'V1_WIN_NQ_9.9.36_53644_GW_B' },
  '9.9.28-46928': { appid: 537212772, qua: 'V1_WIN_NQ_9.9.28_46928_GW_B' },
};
function appidQua(ver) {
  // fall back: build a qua that matches the shape, and leave appid empty rather than
  // inventing one (engine init tolerates an empty appid far better than a wrong one).
  if (APPID_QUA[ver]) return APPID_QUA[ver];
  const m = /^(\d+\.\d+\.\d+)-(\d+)$/.exec(ver || '');
  return { appid: '', qua: m ? `V1_WIN_NQ_${m[1]}_${m[2]}_GW_B` : '' };
}

// ---------------------------------------------------------------------------
// 1. the wrapper module
// ---------------------------------------------------------------------------
function loadWrapper(ver) {
  const require = createRequire(import.meta.url);
  const candidates = [
    join(QQ_EXE_DIR, 'versions', ver, 'resources', 'app', 'wrapper.node'),
    // the file the injector replaced for its own run may still sit in the app dir
    join(QQ_EXE_DIR, 'resources', 'app', 'wrapper.node'),
  ];
  for (const p of candidates) {
    if (!existsSync(p)) continue;
    try { return { wrapper: require(p), path: p }; } catch (e) { out('wrapper', { path: p, err: brief(e) }); }
  }
  throw new Error('wrapper.node not loadable from: ' + candidates.join(' | '));
}

// ---------------------------------------------------------------------------
// 2. login
// ---------------------------------------------------------------------------
function makeLoginListener(hooks) {
  // Plain object: the native side only needs the method names to exist.
  return {
    onLoginConnected() { hooks.connected?.(); },
    onLoginDisConnected() { out('login', { cb: 'onLoginDisConnected' }); },
    onLoginConnecting() {},
    onQRCodeGetPicture(e) { out('login', { cb: 'onQRCodeGetPicture', hasUrl: !!e?.qrcodeUrl }); hooks.qrNeeded?.(); },
    onQRCodeLoginPollingStarted() {},
    onQRCodeSessionUserScaned() { out('login', { cb: 'onQRCodeSessionUserScaned' }); },
    onQRCodeLoginSucceed(e) { out('login', { cb: 'onQRCodeLoginSucceed', uin: e?.uin }); hooks.succeeded?.(e); },
    onQRCodeSessionFailed(e) { out('login', { cb: 'onQRCodeSessionFailed', e: brief(e) }); },
    onLoginFailed(e) { out('login', { cb: 'onLoginFailed', e: brief(e) }); },
    onLogoutSucceed() { out('login', { cb: 'onLogoutSucceed' }); },
    onLogoutFailed(e) { out('login', { cb: 'onLogoutFailed', e: brief(e) }); },
    onUserLoggedIn(u) { out('login', { cb: 'onUserLoggedIn', u: brief(u) }); },
    onQRCodeSessionQuickLoginFailed(e) { out('login', { cb: 'onQRCodeSessionQuickLoginFailed', e: brief(e) }); },
    onPasswordLoginFailed(e) { out('login', { cb: 'onPasswordLoginFailed', e: brief(e) }); },
    OnConfirmUnusualDeviceFailed(e) { out('login', { cb: 'OnConfirmUnusualDeviceFailed', e: brief(e) }); },
    onQQLoginNumLimited(e) { out('login', { cb: 'onQQNumLimited', e: brief(e) }); },
    onLoginState(e) { out('login', { cb: 'onLoginState', e: brief(e) }); },
    onLoginRecordUpdate() {},
  };
}

async function main() {
  const ver = readVersion();
  out('boot', {
    pid: process.pid, node: process.versions.node, electron: process.versions.electron,
    argv: process.argv.slice(0, 6),             // <- what the QQ command line actually looks like
    version: ver.version, buildVersion: ver.buildVersion, dirName: ver.dirName,
    uinEnv: UIN,
  });

  const { wrapper, path: wrapperPath } = loadWrapper(ver.dirName);
  out('wrapper', { path: wrapperPath, exports: Object.keys(wrapper).length });

  // ---- session ------------------------------------------------------------------
  let startupWrapper = null;
  let session = null;
  const sid = (o) => { try { return o ? (o.constructor?.name || 'obj') : 'null'; } catch { return 'err'; } };

  const getSession = () => {
    try { return wrapper.NodeIQQNTWrapperSession.getNTWrapperSession('nt_1'); }
    catch (e) { out('session', { get: 'nt_1', err: brief(e) }); return null; }
  };

  // ---- engine ----
  const engine = wrapper.NodeIQQNTWrapperEngine.get();
  const { appid, qua } = appidQua(ver.dirName);
  let globalPath;
  try { globalPath = wrapper.NodeQQNTWrapperUtil.getNTUserDataInfoConfig(); } catch (e) { out('paths', { err: brief(e) }); }
  if (!globalPath) globalPath = resolve(process.env.USERPROFILE || '.', '.config', 'QQ');
  const globalNtPath = resolve(globalPath, 'nt_qq', 'global');
  out('paths', { globalPath, globalNtPath, appid, qua, fullVersion: ver.version });

  const OS_VER = release();          // plain node: process.getSystemVersion() does not exist
  engine.initWithDeskTopConfig({
    base_path_prefix: '',
    platform_type: 1,                 // win32
    app_type: 4,
    app_version: ver.version,
    os_version: OS_VER,
    use_xlog: false,
    qua,
    global_path_config: { desktopGlobalPath: globalNtPath },
    thumb_config: { maxSide: 324, minSide: 48, longLimit: 6, density: 2 },
  }, {});
  out('engine', { initWithDeskTopConfig: 'ok', osVersion: OS_VER });

  try {
    startupWrapper = wrapper.NodeIQQNTStartupSessionWrapper.create();
    out('startup', { created: true, startupProto: Object.getOwnPropertyNames(Object.getPrototypeOf(startupWrapper) || {}).slice(0, 20) });
  } catch (e) { out('startup', { create: brief(e) }); }

  session = getSession();
  out('session', {
    get: 'nt_1', got: !!session, kind: sid(session),
    own: session ? Object.getOwnPropertyNames(session).slice(0, 20) : null,
    hasGetMsgService: typeof session?.getMsgService === 'function',
  });
  if (!session) { out('done', { ok: false, reason: 'no session handle' }); return; }

  // ---- session.init -----------------------------------------------------------
  // Deferred until after login so selfUin / selfUid are populated from login result.
  out('session', { init: 'deferred to after login (see below)' });

  // ---- login service ----
  const loginService = wrapper.NodeIKernelLoginService.get();
  try {
    loginService.initConfig({
      machineId: '', appid, platVer: 1, commonPath: globalNtPath,
      clientVer: ver.version, hostName: release(),
      externalVersion: false,
    });
  } catch (e) { out('login', { initConfig: brief(e) }); }

  const connected = { v: false };
  let resolveOk = null, rejectOk = null;
  const loggedIn = new Promise((res, rej) => { resolveOk = res; rejectOk = rej; });
  const listener = (() => {
    const base = makeLoginListener({
      connected: () => { connected.v = true; },
      succeeded: (e) => resolveOk(e),
      qrNeeded: () => out('login', { note: 'server wants a QR scan - quick login did not take' }),
    });
    return new Proxy(base, {
      get(t, k) {
        const v = t[k];
        if (typeof v !== 'function') return v;
        return (...a) => { try { return v.apply(t, a); } catch (e) { out('login', { cbErr: String(k), err: brief(e) }); } };
      },
    });
  })();
  loginService.addKernelLoginListener(listener);
  loginService.connect();
  out('login', { connect: 'called' });

  // ---- fast path: the saved-ticket list may already exist ----
  let targetUin = UIN;
  try {
    const list = await loginService.getLoginList();
    const infos = list?.LocalLoginInfoList ?? [];
    out('login', {
      localLoginInfo: infos.map((x) => ({ uin: x.uin, isQuickLogin: x.isQuickLogin })),
    });
    if (!targetUin && infos.length) {
      const quick = infos.find((x) => x.isQuickLogin) || infos[0];
      targetUin = String(quick.uin || '');
    }
  } catch (e) { out('login', { getLoginList: brief(e) }); }

  // ---- quick login: uin comes from the ENV / config.json, not from the command line ----
  const t0 = Date.now();
  let tried = false;
  const attempt = async () => {
    if (tried) return;
    tried = true;
    try {
      out('login', { action: 'quickLoginWithUin', uin: targetUin, waitedMs: Date.now() - t0 });
      const r = await loginService.quickLoginWithUin(targetUin);
      out('login', { quickLoginWithUin: brief(r), result: r?.result, errMsg: r?.loginErrorInfo?.errMsg ?? '' });
      if (String(r?.result) === '0' && !r?.loginErrorInfo?.errMsg) resolveOk({ via: 'quickLoginWithUin' });
      else rejectOk(new Error('quick login refused: ' + brief(r)));
    } catch (e) { out('login', { quickLoginWithUinErr: brief(e) }); rejectOk(e); }
  };

  const waiter = setInterval(() => { if (connected.v || Date.now() - t0 > 8000) { clearInterval(waiter); attempt(); } }, 500);

  let ok;
  try { ok = await Promise.race([loggedIn, sleep(60000).then(() => null)]); }
  catch (e) { out('login', { failed: brief(e) }); ok = null; }
  if (!ok) { out('done', { ok: false, reason: 'login not completed', connected: connected.v }); return; }
  out('login', { ok: true, detail: brief(ok), totalMs: Date.now() - t0 });

  // ---- session.init: the real 4-arg call, now that we have the login result -----
  let selfUin = targetUin, selfUid = '';
  try {
    const list2 = await loginService.getLoginList();
    const infos = list2?.LocalLoginInfoList ?? [];
    const hit = infos.find((x) => String(x.uin) === String(targetUin)) || infos[0];
    if (hit) { selfUin = hit.uin; selfUid = hit.uid || hit.uidStr || ''; }
    out('session', { loginInfo: infos.map((x) => ({ uin: x.uin, uid: x.uid })) });
  } catch (e) { out('session', { loginInfoErr: brief(e) }); }

  const accountPath = resolve(globalPath, 'nt_qq');
  const siCfg = sessionInitConfig({
    uin: selfUin, uid: selfUid, appid, clientVer: ver.version,
    accountPath, machineGuid: process.env.QQ_MACHINE_GUID || '',
  });
  let initOk = false;
  const otel = new Sae();
  await new Promise((res) => {
    let settled = false;
    otel.onOpentelemetryInit = (s) => {
      out('session', { onOpentelemetryInit: brief(s) });
      if (!settled) { settled = true; initOk = !!(s && s.is_init); res(); }
    };
    try {
      out('session', { initArg: typeof session.init, cfgUin: siCfg.selfUin, cfgUid: siCfg.selfUid });
      session.init(siCfg, new Qae(), new Hae(), otel);      // ★ the 4-arg contract
      out('session', { sessionInit: 'called' });
    } catch (e) { out('session', { sessionInitErr: brief(e) }); settled = true; res(); }
    setTimeout(() => { if (!settled) { settled = true; out('session', { initWait: 'timeout 20s (callback is late but harmless - msgService comes up anyway)' }); res(); } }, 20000);
  });
  out('session', { initOk });

  for (const [label, fn] of [
    ['session.startNT(0)', () => session.startNT(0)],
    ['session.startNT()', () => session.startNT()],
    ['startupWrapper.start()', () => startupWrapper?.start()],
    ['startupWrapper.startNT(0)', () => startupWrapper?.startNT(0)],
  ]) {
    try { fn(); out('session', { start: label, ok: true }); break; }
    catch (e) { out('session', { start: label, err: brief(e) }); }
  }

  // ---- wait for the msg service -------------------------------------------
  let ms = null;
  let firstErr = null;
  let firstKind = null;
  for (let i = 0; i < 25 && !ms; i++) {
    const s = getSession() || session;
    let cand = null, err = null;
    try { cand = s.getMsgService(); } catch (e) { err = e; }
    if (i === 0) {
      firstKind = sid(s);
      firstErr = err ? brief(err) : null;
      out('msgservice', { attempt: 0, kind: firstKind, got: typeof cand, err: firstErr, sameObject: s === session });
    }
    if (cand && typeof cand.getMsgsBySeqAndCount === 'function') {
      ms = cand;
      out('msgservice', {
        ok: true, after: i, waitedMs: Date.now() - t0,
        nMethods: Object.getOwnPropertyNames(Object.getPrototypeOf(cand) || {}).length,
      });
      break;
    }
    if (i % 5 === 0 && i > 0) out('msgservice', { attempt: i, got: typeof cand, err: err ? brief(err) : null });
    await sleep(2000);
  }
  if (!ms) { out('done', { ok: false, reason: 'msg service never became available', firstKind, firstErr }); return; }

  const CH = (g) => ({ guildId: '', chatType: 2, peerUid: String(g) });
  const PLAN_FILE = join(OUT_DIR, 'fill-plan.json');
  const MSG_FILE = join(OUT_DIR, 'filled-messages.jsonl');
  let cfg = { budget_ms: 300000, request_timeout_ms: 25000, count_range: [320, 799], interval_range_ms: [2000, 9000] };
  let tasks = [];
  if (existsSync(PLAN_FILE)) {
    try {
      const plan = JSON.parse(readFileSync(PLAN_FILE, 'utf8'));
      cfg = {
        budget_ms: plan.budget_ms ?? cfg.budget_ms,
        request_timeout_ms: plan.request_timeout_ms ?? cfg.request_timeout_ms,
        count_range: plan.count_range ?? cfg.count_range,
        interval_range_ms: plan.interval_range_ms ?? cfg.interval_range_ms,
      };
      for (const g of plan.groups ?? []) for (const p of g.points ?? []) {
        tasks.push({
          group: g.group,
          start: String(p.start),
          end: p.end != null ? String(p.end) : null,
          gap_len: p.gap_len ?? null,
          holes: Array.isArray(p.holes) ? p.holes.map(([a, b]) => [Number(a), Number(b)]) : null,
        });
      }
      out('plan', { file: PLAN_FILE, generated_at: plan.generated_at ?? null, tasks: tasks.length, cfg });
    } catch (e) { out('plan', { err: brief(e) }); }
  } else {
    out('plan', { file: PLAN_FILE, missing: true });
  }
  if (!tasks.length && FILL_PLAN.length) {
    tasks = FILL_PLAN.map((p) => ({
      group: p.group,
      start: String(p.start),
      end: p.end != null ? String(p.end) : null,
      gap_len: p.gap_len ?? null,
      holes: null,
    }));
  }
  if (!tasks.length) { out('done', { ok: false, reason: 'no tasks in fill-plan.json' }); return; }

  const probeTarget = tasks[0];
  let ready = false;
  for (let i = 0; i < 20 && !ready; i++) {
    try {
      const probe = await Promise.race([
        ms.getMsgsBySeqAndCount(CH(probeTarget.group), probeTarget.start, 1, false, true),
        sleep(6000).then(() => null),
      ]);
      if (probe?.result === 0 && probe.msgList?.length) { ready = true; out('ready', { attempt: i, ms: Date.now() - t0 }); }
      else out('ready', { attempt: i, result: probe?.result ?? 'timeout', errMsg: probe?.errMsg ?? '' });
    } catch (e) { out('ready', { attempt: i, err: brief(e) }); }
    if (!ready) await sleep(3000);
  }
  if (!ready) { out('done', { ok: false, reason: 'msg service never answered', loggedIn: true }); return; }

  const pickText = (m) => (m.elements || []).map((e) => {
    if (!e) return '';
    if (e.textElement?.content) return e.textElement.content;
    if (e.picElement) return '[图片]';
    if (e.faceElement) return '[表情]';
    if (e.videoElement) return '[视频]';
    if (e.audioElement) return '[语音]';
    if (e.fileElement) return '[文件]' + (e.fileElement.fileName || '');
    if (e.arkElement) return '[卡片]';
    if (e.forwardElement) return '[转发]';
    return '';
  }).filter(Boolean).join(' ');
  const kindOf = (m) => {
    const els = m.elements || [], has = (k) => els.some((e) => e && e[k]);
    if (has('textElement')) return 'text';
    if (has('picElement')) return 'image';
    if (has('faceElement')) return 'sticker';
    if (has('videoElement')) return 'video';
    if (has('audioElement')) return 'audio';
    if (has('fileElement')) return 'file';
    if (has('forwardElement')) return 'forward';
    if (has('arkElement')) return 'ark';
    return 'other';
  };
  const inHoles = (seqNum, holes) => {
    if (!holes || !holes.length || !Number.isFinite(seqNum)) return true;
    for (const [lo, hi] of holes) {
      if (seqNum >= lo && seqNum <= hi) return true;
    }
    return false;
  };
  const dumpMessages = (list, group, holes) => {
    if (!list || !list.length) return 0;
    let buf = '';
    let written = 0;
    for (const m of list) {
      const seqNum = Number(m.msgSeq);
      if (!inHoles(seqNum, holes)) continue;
      written += 1;
      buf += JSON.stringify({
        seq: String(m.msgSeq ?? ''), ts: Number(m.msgTime ?? 0),
        sender_uid: String(m.senderUid ?? ''),
        sender_name: m.sendMemberName || m.sendNickName || '',
        kind: kindOf(m), text: pickText(m), group: Number(group),
      }) + '\n';
    }
    if (!buf) return 0;
    try { writeFileSync(MSG_FILE, buf, { flag: 'a' }); return written; } catch { return 0; }
  };

  const [cLo, cHi] = cfg.count_range;
  const [iLo, iHi] = cfg.interval_range_ms;
  const rnd = (lo, hi) => lo + Math.floor(Math.random() * (hi - lo + 1));
  const queue = tasks.slice().sort(() => Math.random() - 0.5);
  const deadline = Date.now() + cfg.budget_ms;
  const tFill = Date.now();
  let reqOk = 0, reqFail = 0, gotTotal = 0, dumpedTotal = 0, holesDone = 0;
  for (const task of queue) {
    if (Date.now() > deadline) { out('fill', { skipped: 'time budget', holes_done: holesDone, holes_total: queue.length }); break; }
    let cursor = Number(task.start);
    const endSeq = task.end ? Number(task.end) : null;
    let remaining = task.gap_len && task.gap_len > 0 ? task.gap_len : Infinity;
    let holeGot = 0, batch = 0, stalled = false;
    while ((endSeq === null ? remaining > 0 : cursor <= endSeq) && Date.now() < deadline && batch < 400) {
      batch += 1;
      let count = rnd(cLo, cHi);
      if (endSeq === null) count = Math.min(count, remaining);
      else count = Math.min(count, endSeq - cursor + 1);
      if (count <= 0) break;
      const t1 = Date.now();
      let r = null;
      try {
        r = await Promise.race([ms.getMsgsBySeqAndCount(CH(task.group), String(cursor), count, false, true),
                                sleep(cfg.request_timeout_ms).then(() => null)]);
      } catch (e) { reqFail += 1; stalled = true; out('fill', { group: task.group, start: String(cursor), err: brief(e) }); break; }
      if (r === null) {
        reqFail += 1; stalled = true;
        out('fill', { group: task.group, start: String(cursor), asked: count, timeout: true, tookMs: Date.now() - t1 });
        break;
      }
      const list = r.msgList ?? [];
      if (!list.length) {
        stalled = true;
        out('fill', { group: task.group, start: String(cursor), asked: count, empty: true, result: r.result, errMsg: r.errMsg });
        break;
      }
      const seqs = list.map((m) => Number(m.msgSeq)).filter((n) => Number.isFinite(n));
      const hi = seqs.length ? Math.max(...seqs) : null;
      const dumped = dumpMessages(list, task.group, task.holes);
      reqOk += 1; gotTotal += list.length; dumpedTotal += dumped; holeGot += list.length;
      remaining -= list.length;
      out('fill', {
        group: task.group, start: String(cursor), asked: count, got: list.length, dumped,
        seqLo: seqs.length ? Math.min(...seqs) : null, seqHi: hi,
        hole_gap: task.gap_len ?? null, hole_got: holeGot, batch, result: r.result, tookMs: Date.now() - t1,
      });
      if (hi === null || hi < cursor) { stalled = true; break; }
      cursor = hi + 1;
      if ((endSeq === null ? remaining > 0 : cursor <= endSeq) && Date.now() < deadline) await sleep(rnd(iLo, iHi));
    }
    holesDone += 1;
    out('fill', { hole_finished: true, group: task.group, gap_len: task.gap_len ?? null, got: holeGot, batches: batch, stalled });
    if (Date.now() < deadline) await sleep(rnd(iLo, iHi));
  }
  out('fillSummary', { requests_ok: reqOk, requests_failed: reqFail, got_total: gotTotal, dumped_total: dumpedTotal, holes_done: holesDone, holes_total: queue.length, elapsedMs: Date.now() - tFill });

  // ---- roam API arg discovery (2 arguments, per the native assertion) ----
  for (const [label, fn] of [
    ['queryFirstRoamMsg(chat, 0)', () => ms.queryFirstRoamMsg(CH(probeTarget.group), 0)],
    ['queryRoamCalendar(chat, 0)', () => ms.queryRoamCalendar(CH(probeTarget.group), 0)],
  ]) {
    try {
      const r = await Promise.race([fn(), sleep(12000).then(() => null)]);
      out('roam', { label, value: brief(r) });
    } catch (e) { out('roam', { label, err: brief(e) }); }
    await sleep(1500 + Math.floor(Math.random() * 2000));
  }

  out('done', { ok: true, standalone: true, loggedIn: true });
}

main().catch((e) => out('fatal', { err: brief(e), stack: String(e?.stack).slice(0, 700) }));
