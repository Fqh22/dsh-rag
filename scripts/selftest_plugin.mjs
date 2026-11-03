/**
 * 插件静态自测：不开 DSH，用 mock 环境跑一遍 Host 与 Client 的加载路径。
 *
 * 目的：把"重启后 DSH 起不来"的风险前置到命令行里。
 */
import { pathToFileURL } from 'node:url'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')), '..')
const PLUGIN = path.join(ROOT, 'plugin')

let failed = 0
function check(name, fn) {
  try {
    fn()
    console.log('  ✓', name)
  } catch (e) {
    failed++
    console.log('  ✗', name, '->', e.message)
  }
}

// ---------------------------------------------------------------- Host
console.log('== Host (index.js) ==')
const mod = await import(pathToFileURL(path.join(PLUGIN, 'index.js')).href)

check('导出了 apply 函数', () => {
  if (typeof mod.apply !== 'function') throw new Error('apply 不是函数')
})

const routes = []
let disposerCount = 0
const injectedRows = []
let tapIndexFn = null
const fakeCtx = {
  on(event, handler) {
    if (event !== 'webserver/index-inject') throw new Error('意外的事件: ' + event)
    const table = []
    handler(table)
    injectedRows.push(...table)
    return () => {}
  },
  inject(services, cb) {
    if (!Array.isArray(services) || !services.includes('webServer')) {
      throw new Error('inject 参数不对: ' + JSON.stringify(services))
    }
    cb(fakeCtx)
  },
  webServer: {
    register(route) {
      if (!route || typeof route.path !== 'string' || typeof route.handler !== 'function') {
        throw new Error('路由结构不合法: ' + JSON.stringify(route))
      }
      routes.push(route)
      return () => { disposerCount++ }
    },
    tapIndex(fn) {
      tapIndexFn = fn
      return () => { disposerCount++ }
    },
  },
  effect(fn) {
    const d = fn()
    return d
  },
}

check('apply 能执行且不抛错', () => {
  mod.apply(fakeCtx)
})

check('注册了 index-inject 注入行（桌面端唯一通道）', () => {
  if (injectedRows.length === 0) throw new Error('没有 push 任何注入行')
  const row = injectedRows[0]
  if (row.kind !== 'script') throw new Error('注入行 kind 应为 script，实际 ' + row.kind)
  if (String(row.text).indexOf('/dsh-rag-panel/client.js') < 0) {
    throw new Error('注入行没有指向 client.js')
  }
})

check('同一注入表不会被重复 push', () => {
  const table = []
  const ctx2 = {
    on(e, h) { h(table); return () => {} },
    inject() {},
    webServer: { register: () => () => {}, tapIndex: () => () => {} },
    effect: () => {},
  }
  mod.apply(ctx2)
  mod.apply(ctx2)
  if (table.length !== 1) throw new Error('重复注入，table 长度=' + table.length)
})

check('注册了预期的路由', () => {
  const paths = routes.map((r) => r.path).sort()
  const want = [
    '/dsh-rag-panel/client.js',
    '/dsh-rag/api/handoff',
    '/dsh-rag/api/recall',
    '/dsh-rag/api/recent',
    '/dsh-rag/api/remember',
    '/dsh-rag/api/state',
    '/dsh-rag/api/memory',
    '/dsh-rag/api/overview',
    '/dsh-rag/api/prune',
  ]
  for (const w of want) {
    if (!paths.includes(w)) throw new Error('缺少路由 ' + w + '，实际: ' + paths.join(', '))
  }
  const uniq = new Set(paths)
  if (uniq.size !== paths.length) throw new Error('存在重复注册的路由: ' + paths.join(', '))
})

check('每个路由都是 exact 类型', () => {
  for (const r of routes) {
    if (r.kind !== 'exact') throw new Error(r.path + ' 的 kind 不是 exact')
  }
})

// 直接调一个 handler，走完整 CLI 链路
console.log('== Host → CLI 链路 ==')
const stateRoute = routes.find((r) => r.path === '/dsh-rag/api/state')
const captured = await new Promise((resolve, reject) => {
  const res = {
    writeHead(code, headers) { this._code = code; this._headers = headers },
    end(body) { resolve({ code: this._code, body }) },
  }
  stateRoute.handler({ headers: { host: '127.0.0.1:55290' }, url: '/dsh-rag/api/state' }, res)
    .catch(reject)
})
check('state 路由返回 200 且是合法 JSON', () => {
  if (captured.code !== 200) throw new Error('HTTP ' + captured.code + ' : ' + captured.body)
  const obj = JSON.parse(captured.body)
  if (obj.ok !== true) throw new Error('返回 ok=false: ' + captured.body.slice(0, 200))
  if (!obj.stats) throw new Error('缺少 stats')
})

// 非回环请求必须被拒
const denied = await new Promise((resolve) => {
  const res = {
    writeHead(code) { this._code = code },
    end(body) { resolve({ code: this._code, body }) },
  }
  stateRoute.handler({ headers: { host: 'evil.example.com' }, url: '/dsh-rag/api/state' }, res)
})
check('非回环 Host 被拒绝 (403)', () => {
  if (denied.code !== 403) throw new Error('期望 403，实际 ' + denied.code)
})

// client.js 路由必须能吐出真正的脚本
const clientRoute = routes.find((r) => r.path === '/dsh-rag-panel/client.js')
const clientServed = await new Promise((resolve, reject) => {
  const res = {
    writeHead(code) { this._code = code },
    end(body) { resolve({ code: this._code, body }) },
  }
  clientRoute.handler({ headers: { host: '127.0.0.1:55290' }, url: '/dsh-rag-panel/client.js' }, res)
    .catch(reject)
})
check('client.js 路由返回可执行的脚本', () => {
  if (clientServed.code !== 200) throw new Error('HTTP ' + clientServed.code)
  if (clientServed.body.indexOf('__ModuleLoader__.load') < 0) {
    throw new Error('返回内容不像客户端模块')
  }
  if (clientServed.body.length < 500) throw new Error('内容过短，可能读错文件')
})

// ---------------------------------------------------------------- Client
console.log('== Client (client.js) ==')
let factory = null
const savedWindow = globalThis.window
globalThis.window = {
  __ModuleLoader__: {
    load(opts) {
      if (!opts || !opts.id || typeof opts.factory !== 'function') throw new Error('load 参数不合法')
      factory = opts.factory
    },
  },
}

const fakeReact = {
  createElement: () => null,
  useState: (v) => [v, () => {}],
  useEffect: () => {},
  useCallback: (f) => f,
  useRef: (v) => ({ current: v }),
}

await import(pathToFileURL(path.join(PLUGIN, 'client.js')).href)

check('client.js 调用了 __ModuleLoader__.load', () => {
  if (!factory) throw new Error('没有调用 load')
})

let clientMod = null
check('factory 能用 mock React 执行', () => {
  clientMod = factory((name) => {
    if (name === 'react') return fakeReact
    throw new Error('未知依赖: ' + name)
  })
  if (!clientMod || typeof clientMod.apply !== 'function') throw new Error('没有返回 apply')
})

let slotName = null
const fakeSlots = {
  inject(name, cb) {
    if (name !== 'shell.overlay') throw new Error('注入的 slot 不对: ' + name)
    cb()
  },
  register(opts, Component) {
    slotName = opts.name
    if (typeof Component !== 'function') throw new Error('组件不是函数')
    if (!opts.id) throw new Error('缺少 id')
  },
}

check('apply 注册到 shell.overlay', () => {
  clientMod.apply({ slots: fakeSlots })
  if (slotName !== 'shell.overlay') throw new Error('实际注册到: ' + slotName)
})

globalThis.window = savedWindow

console.log()
if (failed) {
  console.log(`结果: ${failed} 项失败`)
  process.exit(1)
}
console.log('结果: 全部通过')
