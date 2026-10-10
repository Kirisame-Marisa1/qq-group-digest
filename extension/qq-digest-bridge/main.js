// ==============================================================================
// qq-digest-bridge: NTQQ 官方内核漫游历史消息拉取桥接插件
// 运行环境：QQ NT Electron 主进程 (LiteLoaderQQNT 或加载器注入)
// 作用：提供纯本地 127.0.0.1 HTTP RPC 接口，允许 Python 端触发官方漫游消息拉取
// ==============================================================================

const http = require('http');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const os = require('os');

const DEFAULT_PORT = 48920;
let serverInstance = null;
let authToken = '';

// 寻找可能的 token 写入路径
function resolveTokenPaths() {
    const paths = [
        path.join(__dirname, '..', '..', 'run', 'bridge_token.txt'),
        path.join(os.homedir(), '.qqchatcache', 'run', 'bridge_token.txt'),
        'D:\\QQChatCache\\run\\bridge_token.txt',
        path.join(__dirname, 'bridge_token.txt')
    ];
    return paths;
}

function initToken() {
    authToken = crypto.randomBytes(16).toString('hex');
    const tokenPaths = resolveTokenPaths();
    for (const p of tokenPaths) {
        try {
            const dir = path.dirname(p);
            if (!fs.existsSync(dir)) {
                fs.mkdirSync(dir, { recursive: true });
            }
            fs.writeFileSync(p, authToken, 'utf-8');
            console.log(`[qq-digest-bridge] Auth token saved to: ${p}`);
        } catch (e) {
            // ignore inaccessible directories
        }
    }
}

// 获取 NTQQ 内核消息服务
function getKernelMsgService() {
    try {
        if (global.wrapperSession && typeof global.wrapperSession.getMsgService === 'function') {
            return global.wrapperSession.getMsgService();
        }
    } catch (e) {}

    try {
        if (global.ntWrapper && global.ntWrapper.NodeIKernelMsgService) {
            return global.ntWrapper.NodeIKernelMsgService;
        }
    } catch (e) {}

    // 广度遍历 global 探测
    try {
        for (const k of Object.keys(global)) {
            const v = global[k];
            if (v && typeof v === 'object') {
                if (typeof v.getMsgsBySeqAndCount === 'function' || typeof v.getAioFirstViewLatestMsgs === 'function') {
                    return v;
                }
                if (typeof v.getMsgService === 'function') {
                    const svc = v.getMsgService();
                    if (svc) return svc;
                }
            }
        }
    } catch (e) {}

    return null;
}

// 解析当前登录的账号信息
function getAccountInfo() {
    let uin = '';
    let uid = '';
    try {
        if (global.wrapperSession?.getLoginService) {
            const loginService = global.wrapperSession.getLoginService();
            if (loginService?.getAccount) uin = loginService.getAccount();
            if (loginService?.getCurrentAccount) uin = loginService.getCurrentAccount();
        }
    } catch (e) {}
    return { uin: String(uin || ''), uid: String(uid || '') };
}

// 统一构造 Peer 对象 (群聊 chatType = 2)
function makeGroupPeer(groupCode) {
    return {
        chatType: 2,
        peerUid: String(groupCode),
        guildId: ''
    };
}

// 启动本地 HTTP RPC 服务
function startServer(port = DEFAULT_PORT) {
    if (serverInstance) return;

    initToken();

    serverInstance = http.createServer((req, res) => {
        // 跨域/预检配置（仅限本机）
        res.setHeader('Access-Control-Allow-Origin', '*');
        res.setHeader('Access-Control-Allow-Headers', 'Content-Type, X-Digest-Token');
        res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');

        if (req.method === 'OPTIONS') {
            res.writeHead(200);
            res.end();
            return;
        }

        // Token 鉴权 (健康检查可免 Token，便于探针发现)
        const reqToken = req.headers['x-digest-token'];
        const isHealth = req.url === '/health' && req.method === 'GET';

        if (!isHealth && reqToken !== authToken) {
            res.writeHead(403, { 'Content-Type': 'application/json; charset=utf-8' });
            res.end(JSON.stringify({ error: 'Forbidden: invalid or missing X-Digest-Token' }));
            return;
        }

        const parsedUrl = new URL(req.url, `http://127.0.0.1:${port}`);

        // 1. GET /health
        if (parsedUrl.pathname === '/health' && req.method === 'GET') {
            const svc = getKernelMsgService();
            const acc = getAccountInfo();
            res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
            res.end(JSON.stringify({
                status: 'ok',
                port: port,
                pid: process.pid,
                kernel_ready: !!svc,
                account: acc.uin,
                time: Math.floor(Date.now() / 1000)
            }));
            return;
        }

        // 收集 POST 数据体
        let body = '';
        req.on('data', chunk => { body += chunk; });
        req.on('end', async () => {
            let data = {};
            if (body) {
                try {
                    data = JSON.parse(body);
                } catch (e) {
                    res.writeHead(400, { 'Content-Type': 'application/json; charset=utf-8' });
                    res.end(JSON.stringify({ error: 'Invalid JSON body' }));
                    return;
                }
            }

            const svc = getKernelMsgService();
            if (!svc) {
                res.writeHead(503, { 'Content-Type': 'application/json; charset=utf-8' });
                res.end(JSON.stringify({ error: 'KernelMsgService not ready in current QQ process' }));
                return;
            }

            // 2. POST /pull_batch (按 seq 序号和数量拉取历史漫游)
            if (parsedUrl.pathname === '/pull_batch' && req.method === 'POST') {
                const groupCode = data.group_code;
                const startSeq = String(data.start_seq || '0');
                const count = Number(data.count || 30);
                const desc = data.desc !== false; // 默认向前查找
                const isReverseOrder = Boolean(data.reverse_order);

                if (!groupCode) {
                    res.writeHead(400, { 'Content-Type': 'application/json; charset=utf-8' });
                    res.end(JSON.stringify({ error: 'Missing group_code' }));
                    return;
                }

                const peer = makeGroupPeer(groupCode);
                try {
                    console.log(`[qq-digest-bridge] Pulling group ${groupCode} seq=${startSeq} count=${count}`);
                    let result = null;

                    // 优先调用 getMsgsBySeqAndCount (官方分页与漫游拉取核心接口)
                    if (typeof svc.getMsgsBySeqAndCount === 'function') {
                        result = await svc.getMsgsBySeqAndCount(peer, startSeq, count, desc, isReverseOrder);
                    } else if (typeof svc.getMsgs === 'function') {
                        result = await svc.getMsgs(peer, String(data.msg_id || '0'), count, isReverseOrder);
                    } else if (typeof svc.getAioFirstViewLatestMsgs === 'function') {
                        result = await svc.getAioFirstViewLatestMsgs(peer, count);
                    }

                    const msgList = result?.msgList || [];
                    res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
                    res.end(JSON.stringify({
                        success: true,
                        group_code: groupCode,
                        count: msgList.length,
                        messages: msgList
                    }));
                } catch (err) {
                    console.error('[qq-digest-bridge] pull_batch error:', err);
                    res.writeHead(500, { 'Content-Type': 'application/json; charset=utf-8' });
                    res.end(JSON.stringify({ error: err.message || String(err) }));
                }
                return;
            }

            // 3. POST /pull_seq_range (按起止 seq 范围精确拉取)
            if (parsedUrl.pathname === '/pull_seq_range' && req.method === 'POST') {
                const groupCode = data.group_code;
                const startSeq = String(data.start_seq);
                const endSeq = String(data.end_seq);

                if (!groupCode || !startSeq || !endSeq) {
                    res.writeHead(400, { 'Content-Type': 'application/json; charset=utf-8' });
                    res.end(JSON.stringify({ error: 'Missing group_code, start_seq or end_seq' }));
                    return;
                }

                const peer = makeGroupPeer(groupCode);
                try {
                    let result = null;
                    if (typeof svc.getMsgsBySeqRange === 'function') {
                        result = await svc.getMsgsBySeqRange(peer, startSeq, endSeq);
                    } else {
                        // 回退到按 seq+count 模拟
                        const count = Math.max(1, Math.min(100, Number(endSeq) - Number(startSeq) + 1));
                        result = await svc.getMsgsBySeqAndCount(peer, endSeq, count, true, false);
                    }

                    const msgList = result?.msgList || [];
                    res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
                    res.end(JSON.stringify({
                        success: true,
                        group_code: groupCode,
                        count: msgList.length,
                        messages: msgList
                    }));
                } catch (err) {
                    res.writeHead(500, { 'Content-Type': 'application/json; charset=utf-8' });
                    res.end(JSON.stringify({ error: err.message || String(err) }));
                }
                return;
            }

            // 404
            res.writeHead(404, { 'Content-Type': 'application/json; charset=utf-8' });
            res.end(JSON.stringify({ error: 'Not found' }));
        });
    });

    serverInstance.listen(port, '127.0.0.1', () => {
        console.log(`[qq-digest-bridge] Service listening on http://127.0.0.1:${port}`);
    });

    serverInstance.on('error', err => {
        console.error('[qq-digest-bridge] Server error:', err);
    });
}

// LiteLoaderQQNT 插件主生命周期入口
function onLoad() {
    console.log('[qq-digest-bridge] Initializing plugin inside QQ NT process ...');
    startServer(DEFAULT_PORT);
}

// 无论是通过 LiteLoader 加载还是直接被 node 引入，均自动启动
try {
    onLoad();
} catch (e) {
    console.error('[qq-digest-bridge] onLoad failed:', e);
}

module.exports = {
    onLoad,
    startServer,
    getKernelMsgService
};
