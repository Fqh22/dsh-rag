/**
 * dsh-rag-panel —— Host 侧
 *
 * 职责：把本地记忆库（SQLite）通过 HTTP JSON 暴露给浏览器里的面板。
 * 数据读写的真相在 Python（src/cli.py），这里只做薄桥接：
 *   - 不在 Node 里直接读 SQLite：Electron 内置的 Node 版本决定 node:sqlite 是否可用，不可控；
 *   - 复用已经验证过的 Python 检索/写入逻辑，避免两套实现漂移。
 */
import { execFile } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

export const name = 'dsh-rag-panel'

const PLUGIN_DIR = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(PLUGIN_DIR, '..')

/**
 * 用哪个 Python 跑 cli.py：优先环境变量，其次项目 .venv（Windows / POSIX 两种布局），
 * 最后退回 PATH 里的 python。写死 `.venv\Scripts\python.exe` 时，
 * 换台机器 / Linux / CI 上会直接 ENOENT（2026-11-06 CI 复现）。
 */
function resolvePython() {
  if (process.env.DSH_RAG_PYTHON) return process.env.DSH_RAG_PYTHON
  const candidates = process.platform === 'win32'
    ? [path.join(ROOT, '.venv', 'Scripts', 'python.exe')]
    : [path.join(ROOT, '.venv', 'bin', 'python3'), path.join(ROOT, '.venv', 'bin', 'python')]
  for (const p of candidates) {
    try {
      if (fs.existsSync(p)) return p
    } catch (e) { /* ignore */ }
  }
  return process.platform === 'win32' ? 'python' : 'python3'
}

const PY = resolvePython()
const CLI = path.join(ROOT, 'src', 'cli.py')
const SRC = path.join(ROOT, 'src')
const CLIENT_JS_PATH = path.join(PLUGIN_DIR, 'client.js')

// 桌面壳的 index.html 从安装包静态 dist 直出（dsh-app://），不经过 tapIndex，
// 唯一的注入通道是 webserver/index-inject 推的结构化行。而且页面解释器对
// `script-src` 行是「加载失败即 reject」，会连带把 __DSH_BOOT_READY__ 弄挂，
// 所以这里推**内联行**，由我们自己建 script 并吞掉 onerror。
const INJECT_ROW_TEXT =
  '(function(){try{' +
  'if(window.__DSH_RAG_PANEL_BOOTED__)return;window.__DSH_RAG_PANEL_BOOTED__=1;' +
  'var d=document.body||document.head||document.documentElement;if(!d)return;' +
  'var s=document.createElement("script");' +
  's.src="/dsh-rag-panel/client.js?t="+Date.now();' +
  's.async=true;s.onerror=function(){};d.appendChild(s)' +
  '}catch(e){}})()'

// 按 mtime 失效：这样改 client.js 之后只要刷新页面就能看到效果，不必重启 DSH
let clientJsCache = null
let clientJsMtime = 0
function loadClientJs() {
  let mtime = -1
  try {
    mtime = fs.statSync(CLIENT_JS_PATH).mtimeMs
  } catch (e) {
    if (clientJsCache == null) throw e
    return clientJsCache
  }
  if (clientJsCache == null || mtime !== clientJsMtime) {
    clientJsCache = fs.readFileSync(CLIENT_JS_PATH, 'utf8')
    clientJsMtime = mtime
  }
  return clientJsCache
}

// 单次 CLI 调用的超时；向量化冷启动（首次加载 ONNX）会慢一些
const CLI_TIMEOUT = 60000
const MAX_BODY = 1024 * 1024

function runCli(args) {
  return new Promise((resolve, reject) => {
    execFile(
      PY,
      [CLI, ...args],
      {
        cwd: SRC,
        windowsHide: true,
        timeout: CLI_TIMEOUT,
        maxBuffer: 16 * 1024 * 1024,
        env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
      },
      (err, stdout, stderr) => {
        if (err) {
          const detail = String(stderr || '').trim() || String(err.message || err)
          reject(new Error(detail.slice(0, 800)))
          return
        }
        try {
          resolve(JSON.parse(String(stdout)))
        } catch (e) {
          reject(new Error('CLI 返回了非 JSON 内容：' + String(stdout).slice(0, 200)))
        }
      },
    )
  })
}

function sendJson(res, code, obj) {
  const body = JSON.stringify(obj)
  res.writeHead(code, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
    'Content-Length': Buffer.byteLength(body),
  })
  res.end(body)
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0
    const chunks = []
    req.on('data', (c) => {
      size += c.length
      if (size > MAX_BODY) {
        reject(new Error('请求体过大'))
        req.destroy()
        return
      }
      chunks.push(c)
    })
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8').trim()
      if (!raw) return resolve({})
      try {
        resolve(JSON.parse(raw))
      } catch (e) {
        reject(new Error('请求体不是合法 JSON'))
      }
    })
    req.on('error', reject)
  })
}

/** 只允许本机回环访问：面板和 CLI 都在同一台机器上，没有理由放开。 */
function isLoopbackName(h) {
  return h === '127.0.0.1' || h === 'localhost' || h === '::1' || h === '::ffff:127.0.0.1'
}

function isLoopback(req) {
  const host = String(req.headers.host || '')
  const hostname = host.replace(/:\d+$/, '').replace(/^\[|\]$/g, '')
  if (!isLoopbackName(hostname)) return false
  // Host 是客户端可控的请求头。真正的对端地址也要看：万一宿主把监听开出去，
  // 远端只要伪造 Host 就能过（2026-11-06 审视）。
  const addr = String((req.socket && req.socket.remoteAddress) || '')
  if (addr && !isLoopbackName(addr) && !addr.startsWith('127.')) return false
  if (String(req.headers['sec-fetch-site'] || '') === 'cross-site') return false
  const origin = req.headers.origin
  if (origin && origin !== 'null') {
    try {
      const o = new URL(origin)
      // 只有 http(s) 的 Origin 才可比 authority：桌面壳走 dsh-app://，
      // 它的 Origin 不是 http，靠 Host + 对端地址这两道就够了。
      if (o.protocol === 'http:' || o.protocol === 'https:') {
        if (!isLoopbackName(o.hostname)) return false
        // 端口也要一致，否则任意本地端口的页面都能打这个 API（含 prune）
        if (o.host !== host) return false
      }
    } catch (e) {
      return false
    }
  }
  return true
}

/**
 * ③ 开场自动注入（P0 自动化）
 *
 * 为什么挂在 session/event 而不是启动时：会话开始时并不知道是哪个项目，
 * 要等用户第一句话。命中已登记项目后，往 agent 的 inbox 里塞一句「指针」，
 * 模型就能自己去读 pack——不依赖它记得调工具。
 *
 * 三个约束：
 * - 只注入**指针**（几十 token），不注入整包；整包由模型按需读，避免每轮都付钱。
 * - 只对 `source.kind === 'user'` 的第一条消息触发，且每个会话只试一次。
 * - 整个链路 try/catch 兜底，失败只写日志，绝不影响会话。
 *
 * 关闭：环境变量 DSH_RAG_AUTO_INJECT=off
 */
function attachAutoInject(ctx, runCli) {
  const mode = String(process.env.DSH_RAG_AUTO_INJECT || 'pointer').toLowerCase()
  if (mode === 'off' || mode === '0' || mode === 'false') return null

  const agents = new Map()   // sessionId → agent（agent 可能先于会话创建）
  const tries = new Set()
  const disposers = []

  const log = (msg) => {
    try {
      fs.appendFileSync(
        path.join(ROOT, 'logs', 'auto-inject.log'),
        `[${new Date().toISOString()}] ${msg}\n`,
      )
    } catch (e) { /* ignore */ }
  }

  const textOf = (content) => {
    if (!Array.isArray(content)) return ''
    return content
      .filter((b) => b && b.type === 'text')
      .map((b) => String(b.text || ''))
      .join('\n')
      .trim()
  }

  const build = (r) => {
    const head = `【dsh-rag】本轮命中项目「${r.project}」（上下文包 v${r.packVersion}，约 ${r.packTokens} token）。`
    const hint = r.resume ? `续接建议：${r.resume}` : ''
    return [
      head,
      `开工前请先调用 mcp__rag__handoff_pack(project="${r.project}") 读上下文包；`,
      '收尾时用 checkpoint_put 更新结构化状态（会自动重渲染交接卡）。',
      hint,
    ].filter(Boolean).join('\n')
  }

  try {
    disposers.push(ctx.on('agent/created', ({ agent }) => {
      try {
        const sid = agent && (agent.sessionId || (agent.session && agent.session.id))
        if (sid) agents.set(String(sid), agent)
      } catch (e) { /* ignore */ }
    }))
  } catch (e) { /* 宿主版本不支持就跳过 */ }

  try {
    disposers.push(ctx.on('session/event', (session, event) => {
      try {
        if (!event || event.type !== 'user/message') return
        const sid = session && session.id
        if (!sid || tries.has(sid)) return
        const src = (event.data && event.data.source) || {}
        if (src.kind !== 'user') return
        const text = textOf(event.data && event.data.content)
        if (!text || text.length < 4) return

        // 优先从会话对象上直接拿 agent：`agent/created` 触发时 agent 未必已经挂上 session，
        // 靠那张 map 查会一直是空的（第一版就是这么静默失败的）。
        const agent = (session && session.agent) || agents.get(String(sid))
        if (!agent || typeof agent.inject !== 'function') {
          // 这里**不能**先记 tries：agent 还没挂上只是时序问题，
          // 后面的用户消息可能就好了。先记会让该会话的自动注入永久失效。
          log(`skip sid=${sid}: agent 不可用（session.agent=${Boolean(session && session.agent)}）`)
          return
        }
        tries.add(sid)
        agents.set(String(sid), agent)

        runCli(['hook', '--text', text.slice(0, 500), '--cwd', String(session.cwd || '')])
          .then((r) => {
            if (!r || !r.matched) {
              log(`hook unmatched sid=${sid}`)
              return false
            }
            agent.inject({
              role: 'user',
              content: [{ type: 'text', text: build(r) }],
              source: { kind: 'dsh-rag-auto' },
              id: randomUUID(),
            })
            log(`injected project=${r.project} pack=v${r.packVersion} sid=${sid}`)
            return true
          })
          .catch((e) => log(`hook failed sid=${sid}: ${String((e && e.message) || e)}`))
      } catch (e) { /* 绝不影响会话 */ }
    }))
  } catch (e) { /* ignore */ }

  return () => {
    for (const d of disposers) {
      try { d() } catch (e) { /* ignore */ }
    }
  }
}

export function apply(ctx) {
  // ① 注入行：不依赖任何服务，必须在宿主收集注入表之前注册
  const earlyDisposers = []
  const offInject = ctx.on('webserver/index-inject', (table) => {
    try {
      if (!Array.isArray(table)) return
      for (const row of table) {
        if (row && typeof row.text === 'string' && row.text.indexOf('/dsh-rag-panel/client.js') >= 0) return
      }
      table.push({ kind: 'script', placement: 'body', text: INJECT_ROW_TEXT })
    } catch (e) { /* 绝不影响宿主启动 */ }
  })
  if (typeof offInject === 'function') earlyDisposers.push(offInject)
  ctx.effect(() => () => {
    for (const d of earlyDisposers) {
      try { d() } catch (e) { /* ignore */ }
    }
  })

  // ③ 开场自动注入：把「记得调 handoff_pack」交给插件，而不是靠模型自觉
  try {
    const offAuto = attachAutoInject(ctx, runCli)
    if (typeof offAuto === 'function') {
      ctx.effect(() => () => {
        try { offAuto() } catch (e) { /* ignore */ }
      })
    }
  } catch (e) { /* ignore */ }

  // ② 需要 webServer 才能注册的东西
  ctx.inject(['webServer'], (ctx) => {
    const disposers = []

    const guard = (handler) => async (req, res) => {
      if (!isLoopback(req)) return sendJson(res, 403, { ok: false, error: 'forbidden' })
      try {
        await handler(req, res)
      } catch (err) {
        sendJson(res, 500, { ok: false, error: String((err && err.message) || err) })
      }
    }

    const route = (p, handler) => {
      const dispose = ctx.webServer.register({
        kind: 'exact',
        path: p,
        handler: guard(handler),
      })
      if (typeof dispose === 'function') disposers.push(dispose)
    }

    // 面板脚本本体：注入行指向这个地址
    route('/dsh-rag-panel/client.js', async (req, res) => {
      let body
      try {
        body = loadClientJs()
      } catch (e) {
        res.writeHead(500, { 'Content-Type': 'text/plain; charset=utf-8' })
        res.end('client.js 读取失败: ' + String((e && e.message) || e))
        return
      }
      res.writeHead(200, {
        'Content-Type': 'application/javascript; charset=utf-8',
        'Cache-Control': 'no-cache',
        'Content-Length': Buffer.byteLength(body),
      })
      res.end(body)
    })

    route('/dsh-rag/api/state', async (req, res) => {
      sendJson(res, 200, await runCli(['state']))
    })

    // 项目总览：每个项目一行，带交接卡摘要
    route('/dsh-rag/api/overview', async (req, res) => {
      sendJson(res, 200, await runCli(['overview']))
    })

    // 清掉某个项目的全部记忆（目录已删、只剩记忆的「孤儿」用它）
    route('/dsh-rag/api/prune', async (req, res) => {
      if (req.method !== 'POST') return sendJson(res, 405, { ok: false, error: '只接受 POST' })
      const body = await readBody(req)
      const { project, confirm } = body || {}
      if (!project) return sendJson(res, 400, { ok: false, error: '缺少 project' })
      if (confirm !== true) return sendJson(res, 400, { ok: false, error: '需要 confirm: true' })
      sendJson(res, 200, await runCli(['prune', '--project', String(project), '--confirm']))
    })

    // 同一路径两种语义：GET 读交接卡，POST 写交接卡
    route('/dsh-rag/api/handoff', async (req, res) => {
      if (req.method === 'POST') {
        const body = await readBody(req)
        const { project, content } = body || {}
        if (!project || !content) {
          return sendJson(res, 400, { ok: false, error: 'project / content 必填' })
        }
        return sendJson(res, 200, await runCli([
          'handoff_put', '--project', String(project), '--content', String(content),
        ]))
      }
      const project = new URL(req.url || '/', 'http://x').searchParams.get('project') || ''
      if (!project) return sendJson(res, 400, { ok: false, error: '缺少 project' })
      sendJson(res, 200, await runCli(['handoff', '--project', project]))
    })

    // 缓存/压缩指标：面板顶部那一行「最近 N 会话命中 x%」
    route('/dsh-rag/api/cache', async (req, res) => {
      const sp = new URL(req.url || '/', 'http://x').searchParams
      const args = ['cache', '--limit', String(Math.max(1, Math.min(100, Number(sp.get('limit')) || 20)))]
      const project = sp.get('project')
      if (project) args.push('--project', project)
      sendJson(res, 200, await runCli(args))
    })

    route('/dsh-rag/api/recent', async (req, res) => {
      const sp = new URL(req.url || '/', 'http://x').searchParams
      const args = ['recent', '--limit', String(Math.max(1, Math.min(200, Number(sp.get('limit')) || 40)))]
      const project = sp.get('project')
      if (project) args.push('--project', project)
      sendJson(res, 200, await runCli(args))
    })

    route('/dsh-rag/api/recall', async (req, res) => {
      const sp = new URL(req.url || '/', 'http://x').searchParams
      const q = (sp.get('q') || '').trim()
      if (!q) return sendJson(res, 400, { ok: false, error: '缺少 q' })
      const args = ['recall', '--query', q, '--limit', String(Math.max(1, Math.min(50, Number(sp.get('limit')) || 12)))]
      const project = sp.get('project')
      if (project) args.push('--project', project)
      sendJson(res, 200, await runCli(args))
    })

    // 单条记忆的读 / 改：GET 取原文（编辑框要用完整内容），POST 提交修改
    route('/dsh-rag/api/memory', async (req, res) => {
      if (req.method === 'POST') {
        const body = await readBody(req)
        const { id, title, content } = body || {}
        if (id == null) return sendJson(res, 400, { ok: false, error: '缺少 id' })
        if (title == null && content == null) {
          return sendJson(res, 400, { ok: false, error: 'title 和 content 至少给一个' })
        }
        const args = ['update', '--id', String(id)]
        if (title != null) args.push('--title', String(title))
        if (content != null) args.push('--content', String(content))
        return sendJson(res, 200, await runCli(args))
      }
      const id = new URL(req.url || '/', 'http://x').searchParams.get('id')
      if (!id) return sendJson(res, 400, { ok: false, error: '缺少 id' })
      sendJson(res, 200, await runCli(['get', '--id', String(id)]))
    })

    route('/dsh-rag/api/remember', async (req, res) => {
      // 面板目前没有「新建记忆」入口，这个写接口没有调用方。
      // 少一个可写入口就少一份攻击面：留着等真做了 UI 再开。
      return sendJson(res, 404, { ok: false, error: '未开放' })
    })

    // Web 形态（非桌面壳）走 tapIndex 直接改 HTML，两条路并存、各自防重
    try {
      const disposeTap = ctx.webServer.tapIndex((html) => {
        try {
          if (typeof html !== 'string') return html
          if (html.indexOf('/dsh-rag-panel/client.js') >= 0) return html
          const tag = '<script async src="/dsh-rag-panel/client.js"></script>'
          return html.indexOf('</body>') >= 0
            ? html.replace('</body>', tag + '</body>')
            : html + tag
        } catch (e) {
          return html
        }
      })
      if (typeof disposeTap === 'function') disposers.push(disposeTap)
    } catch (e) { /* tapIndex 不存在时忽略 */ }

    ctx.effect(() => () => {
      for (const d of disposers) {
        try { d() } catch (e) { /* ignore */ }
      }
    })
  })
}
