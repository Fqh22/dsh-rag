/**
 * dsh-rag-panel —— Client 侧
 *
 * 在 Harness Web UI 的 shell.overlay 里挂一个右下角悬浮面板，
 * 展示各项目的交接卡与记忆条目，支持检索、查看、手动写入。
 *
 * 数据来自 Host 侧注册的 /dsh-rag/api/*，不直接碰数据库。
 */
;(function () {
  // 注入行与 tapIndex 两条路可能都会触发，这里做一次全局去重
  if (window.__DSH_RAG_PANEL_REGISTERED__) return
  window.__DSH_RAG_PANEL_REGISTERED__ = true

  function boot() {
    if (!window.__ModuleLoader__ || typeof window.__ModuleLoader__.load !== 'function') {
      setTimeout(boot, 120)
      return
    }
    try {
      window.__ModuleLoader__.load({
        id: 'dsh-rag-panel',
        factory(require) {
    const React = require('react')
    const h = React.createElement
    const { useState, useEffect, useCallback, useRef } = React

    const API = '/dsh-rag/api'

    const C = {
      bg: 'rgba(22,24,29,0.97)',
      bg2: 'rgba(255,255,255,0.045)',
      bg3: 'rgba(255,255,255,0.085)',
      line: 'rgba(255,255,255,0.12)',
      text: '#e7e9ec',
      dim: '#9aa0a8',
      accent: '#6aa8ff',
      warn: '#e8b04b',
    }

    const KIND_LABEL = {
      baseline: '基线',
      checkpoint: '状态',
      handoff: '交接卡',
      decision: '决策',
      fact: '事实',
      todo: '待办',
      pitfall: '坑',
      session: '会话',
      note: '笔记',
    }

    async function api(path, opts) {
      const res = await fetch(path, opts)
      let data
      try {
        data = await res.json()
      } catch (e) {
        throw new Error('接口返回非 JSON（HTTP ' + res.status + '）')
      }
      if (!data || data.ok !== true) {
        // CLI 的失败有两种写法：error（参数/流程错误）和 message（业务拒绝，
        // 比如「与 id=7 内容重复」）。只读 error 会让最常见的那种显示成「请求失败」。
        throw new Error((data && (data.error || data.message)) || '请求失败')
      }
      return data
    }

    function relTime(iso) {
      if (!iso) return ''
      const t = Date.parse(iso)
      if (!t) return ''
      const diff = Date.now() - t
      const min = Math.floor(diff / 60000)
      if (min < 1) return '刚刚'
      if (min < 60) return min + ' 分钟前'
      const hr = Math.floor(min / 60)
      if (hr < 24) return hr + ' 小时前'
      const d = Math.floor(hr / 24)
      if (d < 30) return d + ' 天前'
      return new Date(t).toLocaleDateString()
    }

    // 小鲸鱼挂件的根节点是 position:fixed; right:0; bottom:0，
    // 尺寸 = clamp(122px, min(250px, min(100vw,100vh)*28%), 625px) 的正方形。
    // 以前用固定 bottom:84px，正好压在它身上。这里用同一个公式让它自动让开。
    const WHALE_SPACE = 'min(250px, 28vh)'
    const BOTTOM_GAP = '16px'

    const css = {
      wrap: {
        position: 'fixed', right: '20px',
        bottom: `calc(${WHALE_SPACE} + ${BOTTOM_GAP})`, zIndex: 2147483000,
        fontFamily: 'inherit', color: C.text, fontSize: '13px', lineHeight: 1.55,
      },
      btn: {
        width: '46px', height: '46px', borderRadius: '14px',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        background: C.bg, color: C.text, border: '1px solid ' + C.line,
        cursor: 'pointer', boxShadow: '0 6px 24px rgba(0,0,0,0.35)',
        userSelect: 'none', position: 'relative',
      },
      panel: {
        width: '440px', maxWidth: 'calc(100vw - 40px)',
        // 面板从按钮往上长，要保证顶部不出屏
        maxHeight: `min(660px, calc(100vh - ${WHALE_SPACE} - 130px))`,
        display: 'flex', flexDirection: 'column',
        background: C.bg, border: '1px solid ' + C.line, borderRadius: '14px',
        boxShadow: '0 12px 44px rgba(0,0,0,0.45)', overflow: 'hidden',
        marginBottom: '10px', position: 'relative',
      },
      head: {
        display: 'flex', alignItems: 'center', gap: '8px',
        padding: '11px 14px', borderBottom: '1px solid ' + C.line, flexShrink: 0,
      },
      title: { fontWeight: 600, fontSize: '13.5px', flex: 1 },
      stat: { color: C.dim, fontSize: '11.5px' },
      close: {
        cursor: 'pointer', color: C.dim, fontSize: '16px', lineHeight: 1,
        padding: '2px 4px', borderRadius: '6px',
      },
      tools: { padding: '9px 14px 0', flexShrink: 0 },
      search: {
        width: '100%', boxSizing: 'border-box', padding: '7px 10px',
        background: C.bg2, border: '1px solid ' + C.line, borderRadius: '9px',
        color: C.text, fontSize: '12.5px', outline: 'none', fontFamily: 'inherit',
      },
      chips: {
        display: 'flex', gap: '6px', overflowX: 'auto', padding: '9px 14px 2px',
        flexShrink: 0, scrollbarWidth: 'thin',
      },
      chip: (on) => ({
        flexShrink: 0, cursor: 'pointer', padding: '3px 9px', borderRadius: '999px',
        fontSize: '11.5px', whiteSpace: 'nowrap',
        background: on ? 'rgba(106,168,255,0.18)' : C.bg2,
        border: '1px solid ' + (on ? 'rgba(106,168,255,0.55)' : C.line),
        color: on ? C.accent : C.dim,
      }),
      body: { overflowY: 'auto', padding: '8px 14px 14px', flex: 1, minHeight: '80px' },
      card: {
        background: C.bg2, border: '1px solid ' + C.line,
        borderRadius: '10px', padding: '9px 11px', marginTop: '8px',
      },
      cardHead: { display: 'flex', alignItems: 'baseline', gap: '7px' },
      cardTitle: { fontSize: '12.5px', fontWeight: 600, flex: 1, wordBreak: 'break-word' },
      kind: (k) => ({
        fontSize: '10.5px', padding: '1px 6px', borderRadius: '5px', flexShrink: 0,
        background: k === 'handoff' ? 'rgba(232,176,75,0.18)' : 'rgba(106,168,255,0.14)',
        color: k === 'handoff' ? C.warn : C.accent,
      }),
      meta: { color: C.dim, fontSize: '10.5px', marginTop: '3px' },
      content: {
        marginTop: '6px', fontSize: '12px', color: '#cfd3d8',
        whiteSpace: 'pre-wrap', wordBreak: 'break-word',
      },
      clamp: (n) => ({
        display: '-webkit-box', WebkitLineClamp: String(n),
        WebkitBoxOrient: 'vertical', overflow: 'hidden',
      }),
      empty: { color: C.dim, fontSize: '12px', padding: '18px 2px', textAlign: 'center' },
      err: { color: '#ff8a8a', fontSize: '11.5px', padding: '6px 0' },
      toast: {
        position: 'absolute', left: '50%', transform: 'translateX(-50%)', bottom: '8px',
        background: 'rgba(106,168,255,0.95)', color: '#08111f', fontSize: '11.5px',
        fontWeight: 600, padding: '4px 12px', borderRadius: '999px', pointerEvents: 'none',
      },
      searchWrap: { position: 'relative' },
      clearBtn: {
        position: 'absolute', right: '8px', top: '50%', transform: 'translateY(-50%)',
        color: C.dim, cursor: 'pointer', fontSize: '13px', lineHeight: 1, padding: '2px 4px',
        borderRadius: '5px', userSelect: 'none',
      },
      editBtn: {
        flexShrink: 0, cursor: 'pointer', color: C.dim, fontSize: '12px',
        padding: '0 3px', borderRadius: '4px', lineHeight: 1,
      },
      editField: {
        width: '100%', boxSizing: 'border-box', marginTop: '7px',
        background: 'rgba(0,0,0,0.28)', border: '1px solid ' + C.line, borderRadius: '7px',
        color: C.text, fontSize: '12px', padding: '6px 8px', outline: 'none',
        fontFamily: 'inherit',
      },
      editArea: {
        width: '100%', boxSizing: 'border-box', marginTop: '6px', resize: 'vertical',
        background: 'rgba(0,0,0,0.28)', border: '1px solid ' + C.line, borderRadius: '7px',
        color: C.text, fontSize: '12px', padding: '6px 8px', outline: 'none',
        fontFamily: 'inherit', lineHeight: 1.5, minHeight: '72px',
      },
      editBar: { display: 'flex', gap: '8px', justifyContent: 'flex-end', marginTop: '8px' },
      btnPrimary: {
        cursor: 'pointer', border: 'none', borderRadius: '7px', padding: '4px 14px',
        background: C.accent, color: '#08111f', fontSize: '12px', fontWeight: 600,
        fontFamily: 'inherit',
      },
      btnGhost: {
        cursor: 'pointer', borderRadius: '7px', padding: '4px 12px',
        background: 'transparent', border: '1px solid ' + C.line, color: C.dim,
        fontSize: '12px', fontFamily: 'inherit',
      },
      mark: { background: 'rgba(245,196,81,0.85)', color: '#141414', borderRadius: '2px' },
      sectionTitle: {
        fontSize: '11px', color: C.dim, marginTop: '12px', marginBottom: '2px',
        letterSpacing: '0.4px',
      },
      rowCompact: {
        display: 'flex', alignItems: 'center', gap: '7px', cursor: 'pointer',
        padding: '5px 2px', borderBottom: '1px solid rgba(255,255,255,0.06)',
      },
      rowName: {
        flex: 1, minWidth: 0, fontSize: '12px', overflow: 'hidden',
        textOverflow: 'ellipsis', whiteSpace: 'nowrap',
      },
      miniBtn: {
        flexShrink: 0, cursor: 'pointer', fontSize: '10.5px', padding: '1px 7px',
        borderRadius: '5px', border: '1px solid ' + C.line, color: C.accent,
        background: 'rgba(106,168,255,0.12)', whiteSpace: 'nowrap',
      },
      badge: {
        position: 'absolute', top: '-3px', right: '-3px', minWidth: '15px', height: '15px',
        borderRadius: '999px', background: C.accent, color: '#08111f', fontSize: '10px',
        display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '0 4px',
        fontWeight: 700,
      },
    }

    function escapeReg(s) {
      return String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    }

    // 把命中的词包成 <mark>；用切分而不是 innerHTML，避免把内容里的 HTML 当标签
    function highlight(text, q) {
      const src = String(text == null ? '' : text)
      const words = String(q || '').split(/\s+/).filter(Boolean).slice(0, 6)
      if (!words.length) return src
      let re
      try {
        re = new RegExp('(' + words.map(escapeReg).join('|') + ')', 'gi')
      } catch (e) {
        return src
      }
      return src.split(re).map((p, i) =>
        i % 2 === 1 ? h('mark', { key: i, style: css.mark }, p) : p)
    }

    const POS_KEY = 'dsh-rag-panel-pos'

    function loadPos() {
      try {
        const raw = window.localStorage.getItem(POS_KEY)
        if (!raw) return null
        const o = JSON.parse(raw)
        if (o && typeof o.x === 'number' && typeof o.y === 'number') return o
      } catch (e) { /* 隐私模式/禁用存储 */ }
      return null
    }

    function savePos(p) {
      try {
        if (p) window.localStorage.setItem(POS_KEY, JSON.stringify(p))
        else window.localStorage.removeItem(POS_KEY)
      } catch (e) { /* ignore */ }
    }

    // 展开后面板很高，夹取必须按实际元素尺寸算，否则整个面板会被顶出屏幕
    function clampPos(x, y, elW, elH) {
      const vw = window.innerWidth || 1024
      const vh = window.innerHeight || 768
      let width = elW || 52
      let height = elH || 52
      // 元素比视口还大时（小窗口 + 大面板），钉在左上角，至少露出顶部
      if (width > vw - 8) width = vw - 8
      if (height > vh - 8) height = vh - 8
      return {
        x: Math.max(4, Math.min(vw - width - 4, x)),
        y: Math.max(4, Math.min(vh - height - 4, y)),
      }
    }

    function Icon() {
      return h('svg', { width: 22, height: 22, viewBox: '0 0 64 64', 'aria-hidden': true },
        h('rect', { x: 6, y: 10, width: 52, height: 44, rx: 9, fill: '#2a6fd6' }),
        h('rect', { x: 14, y: 18, width: 34, height: 5, rx: 2.5, fill: '#d6e6ff' }),
        h('rect', { x: 14, y: 29, width: 26, height: 5, rx: 2.5, fill: '#a8c8f5' }),
        h('rect', { x: 14, y: 40, width: 18, height: 5, rx: 2.5, fill: '#8ab0ec' }),
        h('circle', { cx: 48, cy: 45, r: 8, fill: '#ffd166' }),
      )
    }

    function Panel() {
      const [open, setOpen] = useState(false)
      const [state, setState] = useState(null)
      const [project, setProject] = useState(null)
      const [query, setQuery] = useState('')
      const [items, setItems] = useState([])
      const [handoff, setHandoff] = useState(null)
      const [loading, setLoading] = useState(false)
      const [error, setError] = useState('')
      const [expanded, setExpanded] = useState({})
      const [editing, setEditing] = useState(null)   // { id, title, content }
      const [saving, setSaving] = useState(false)
      const [toast, setToast] = useState('')
      const [overview, setOverview] = useState([])
      const [cache, setCache] = useState(null)
      const [pos, setPos] = useState(loadPos)
      const [dragging, setDragging] = useState(false)
      const posRef = useRef(pos)
      posRef.current = pos
      const dragRef = useRef(null)
      const timer = useRef(null)
      const toastTimer = useRef(null)
      const searchRef = useRef(null)

      // 位置兜底：窗口变小、或面板展开后高度暴涨时，把整个面板拉回可视区。
      // 高度必须按实际元素算——展开后 wrap 有 700px 高，只按按钮尺寸夹会让面板顶出屏幕。
      useEffect(() => {
        if (!pos) return
        function fix() {
          const el = document.querySelector('[data-dsh-rag-panel]')
          const w = el ? el.offsetWidth : 52
          const h = el ? el.offsetHeight : 52
          const p = clampPos(pos.x, pos.y, w, h)
          if (p.x !== pos.x || p.y !== pos.y) {
            setPos(p)
            savePos(p)
          }
        }
        fix()
        const t = setTimeout(fix, 60)   // 等展开动画/布局稳定后再校一次
        window.addEventListener('resize', fix)
        return function () {
          clearTimeout(t)
          window.removeEventListener('resize', fix)
        }
      }, [pos, open])

      // 关闭面板时清掉「半截状态」：否则重开时搜索框还留着上次的字，列表却是全量的；
      // 更要命的是编辑到一半的旧快照会活下来，重开再保存就把别人改过的新内容盖掉。
      useEffect(() => {
        if (open) return
        setQuery('')
        setEditing(null)
        setExpanded({})
        setError('')
      }, [open])

      const loadBase = useCallback(async () => {
        setLoading(true)
        setError('')
        try {
          const s = await api(API + '/state')
          setState(s)
        } catch (e) {
          setError(String(e.message || e))
        } finally {
          setLoading(false)
        }
      }, [])

      // 项目总览：只在「全部」视图用
      const loadOverview = useCallback(async () => {
        try {
          const r = await api(API + '/overview')
          setOverview(r.projects || [])
        } catch (e) {
          // 一次慢/失败就清空列表，会让人以为数据没了。保留上一次的结果，
          // 下个轮询周期自己会恢复。
          if (typeof console !== 'undefined') {
            console.warn('[dsh-rag] overview 刷新失败：', (e && e.message) || e)
          }
        }
      }, [])

      // 缓存/压缩指标：只在「全部」视图顶部显示一行，点开没意义，不做交互
      const loadCache = useCallback(async () => {
        try {
          const r = await api(API + '/cache?limit=20')
          setCache(r)
        } catch (e) {
          if (typeof console !== 'undefined') {
            console.warn('[dsh-rag] cache 刷新失败：', (e && e.message) || e)
          }
        }
      }, [])

      // 请求序号：recall 走向量要 1–2 秒，比 280ms 的防抖长。没有序号时，
      // 先发的慢响应会后到并覆盖新结果（甚至把 A 的交接卡盖到 B 上）。
      const listSeq = useRef(0)
      const loadList = useCallback(async (proj, q) => {
        const my = ++listSeq.current
        setLoading(true)
        setError('')
        try {
          const qs = []
          if (proj) qs.push('project=' + encodeURIComponent(proj))
          if (q) {
            qs.push('q=' + encodeURIComponent(q))
            const r = await api(API + '/recall?' + qs.join('&'))
            if (my !== listSeq.current) return
            setItems(r.items || [])
          } else {
            qs.push('limit=60')
            const r = await api(API + '/recent?' + qs.join('&'))
            if (my !== listSeq.current) return
            setItems(r.items || [])
          }
          if (proj) {
            const hres = await api(API + '/handoff?project=' + encodeURIComponent(proj))
            if (my !== listSeq.current) return
            setHandoff(hres.handoff)
          } else {
            setHandoff(null)
          }
        } catch (e) {
          if (my !== listSeq.current) return
          setError(String(e.message || e))
        } finally {
          if (my === listSeq.current) setLoading(false)
        }
      }, [])

      useEffect(() => {
        if (!open) return
        loadBase()
        loadList(project, '')
        if (!project) loadOverview()
        if (!project) loadCache()
      }, [open])

      useEffect(() => {
        if (!open) return
        if (timer.current) clearTimeout(timer.current)
        timer.current = setTimeout(() => loadList(project, query.trim()), 280)
        if (!project && !query.trim()) loadOverview()
        return () => {
          if (timer.current) clearTimeout(timer.current)
        }
      }, [query, project])

      // Ctrl/Cmd+K 聚焦搜索；Esc 先退出编辑，再关面板
      useEffect(() => {
        if (!open) return
        function onKey(e) {
          if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) {
            e.preventDefault()
            if (searchRef.current) searchRef.current.focus()
            return
          }
          if (e.key === 'Escape') {
            if (editing) setEditing(null)
          }
        }
        window.addEventListener('keydown', onKey)
        return () => window.removeEventListener('keydown', onKey)
      }, [open, editing])

      // 面板开着时每 10 秒刷一次项目列表：新建/删除目录不用手动刷新
      useEffect(() => {
        if (!open || project) return
        const id = setInterval(() => { loadOverview() }, 10000)
        return () => clearInterval(id)
      }, [open, project, loadOverview])

      const flash = useCallback((msg) => {
        setToast(msg)
        // 复用同一个 timer：连续两条提示时，前一个的 1.8s 会把后一条提前撤掉
        if (toastTimer.current) clearTimeout(toastTimer.current)
        toastTimer.current = setTimeout(function () { setToast('') }, 1800)
      }, [])

      const createHandoff = useCallback(async (name) => {
        const tpl = '当前进展：（刚建卡，还没写）\n\n关键决策：\n\n下一步：\n\n已知问题：'
        try {
          await api(API + '/handoff', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ project: name, content: tpl }),
          })
          flash('已为 ' + name + ' 建卡')
          await loadOverview()
          setProject(name)
        } catch (e) {
          setError(String(e.message || e))
        }
      }, [loadOverview, flash])

      const pruneProject = useCallback(async (name) => {
        const ok = window.confirm('删除「' + name + '」的全部记忆？\n\n只删数据库，不会动任何文件夹。此操作不可恢复。')
        if (!ok) return
        try {
          const r = await api(API + '/prune', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ project: name, confirm: true }),
          })
          flash('已清理 ' + (r.deleted || 0) + ' 条记忆')
          await loadOverview()
          await loadBase()
        } catch (e) {
          setError(String(e.message || e))
        }
      }, [loadOverview, loadBase, flash])

      const saveEdit = useCallback(async () => {
        if (!editing || saving) return
        setSaving(true)
        setError('')
        try {
          const r = await api(API + '/memory', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              id: editing.id,
              title: editing.title,
              content: editing.content,
            }),
          })
          setEditing(null)
          // CLI 在「内容改了但向量没重算」时会带 warning。只读 message
          // 会让面板说「已更新」，而语义检索还在按旧内容召回。
          if (r.warning) {
            flash(r.warning)
            if (typeof console !== 'undefined') console.warn('[dsh-rag]', r.warning)
          } else {
            flash(r.message || '已保存')
          }
          await loadList(project, query.trim())
          if (!project) await loadOverview()
        } catch (e) {
          setError(String(e.message || e))
        } finally {
          setSaving(false)
        }
      }, [editing, saving, project, query, loadList, flash])

      const total = state && state.stats ? state.stats.active : 0

      const chips = []
      chips.push(h('div', {
        key: '__all',
        style: css.chip(project === null),
        onClick: () => setProject(null),
      }, '全部'))
      for (const p of (state && state.projects) || []) {
        chips.push(h('div', {
          key: p.project,
          style: css.chip(project === p.project),
          onClick: () => setProject(p.project),
          title: p.project,
        }, p.project + ' · ' + p.active))
      }

      const head = h('div', {
        style: Object.assign({}, css.head, { cursor: 'grab', touchAction: 'none' }),
        onMouseDown: (e) => startDrag(e, false),
        title: '拖动可移动面板，右键按钮复位',
      },
        h('div', { style: css.title }, '项目记忆'),
        h('div', { style: css.stat }, total + ' 条 / ' + ((state && state.stats && state.stats.projects) || 0) + ' 项目'),
        h('div', {
          style: css.close,
          onClick: () => setOpen(false),
          onMouseDown: (e) => e.stopPropagation(),
          title: '关闭',
        }, '✕'),
      )

      const body = []
      if (error) body.push(h('div', { key: 'e', style: css.err }, '⚠ ' + error))
      if (handoff) {
        body.push(h('div', { key: 'h', style: Object.assign({}, css.card, { borderColor: 'rgba(232,176,75,0.4)' }) },
          h('div', { style: css.cardHead },
            h('div', { style: css.kind('handoff') }, '交接卡'),
            h('div', { style: css.cardTitle }, project),
            h('div', { style: css.meta }, relTime(handoff.updated_at)),
          ),
          h('div', { style: css.content }, handoff.content),
        ))
      }
      if (items.length === 0 && !loading) {
        body.push(h('div', { key: 'z', style: css.empty },
          query ? '没有匹配的记忆' : '这个项目还没有记忆条目'))
      }

      // 「全部」视图：先给一份按项目分组的交接卡总览
      if (!project && !query.trim() && overview.length) {
        body.push(h('div', { key: 'ovt', style: css.sectionTitle }, '项目总览'))
        if (cache && cache.sessions) {
          const compacts = (cache.compactions || []).length
          body.push(h('div', { key: 'cacheline', style: css.meta },
            '最近 ' + cache.sessions + ' 个会话：缓存命中 ' +
            (cache.hitRate * 100).toFixed(1) + '%' +
            (compacts ? '　·　压缩 ' + compacts + ' 次（每次都会让前缀缓存失效）' : '')))
        }
        for (const p of overview) {
          const has = !!p.handoff
          const orphan = !p.hasDir
          const stateLabel = has ? '有卡' : orphan ? '孤儿' : '空'
          body.push(h('div', {
            key: 'ov-' + p.project,
            style: Object.assign({}, css.card, orphan
              ? { cursor: 'pointer', borderColor: 'rgba(255,138,138,0.35)' }
              : { cursor: 'pointer' }),
            onClick: () => setProject(p.project),
            title: p.mdPath || p.project,
          },
            h('div', { style: css.cardHead },
              h('div', {
                style: Object.assign({}, css.kind(has ? 'handoff' : 'fact'),
                  orphan ? { color: '#ff8a8a', background: 'rgba(255,138,138,0.14)' } : null),
              }, stateLabel),
              h('div', { style: css.cardTitle }, p.project),
              h('div', { style: css.meta }, p.total + ' 条' +
                (p.sessions ? '　·　' + p.sessions + ' 会话' : '') +
                (p.compacts ? '　·　压缩 ' + p.compacts : '')),
              !has && !orphan
                ? h('div', {
                    style: css.miniBtn,
                    title: '为这个新项目建一张交接卡',
                    onClick: (e) => { e.stopPropagation(); createHandoff(p.project) },
                  }, '建卡')
                : null,
              orphan
                ? h('div', {
                    style: Object.assign({}, css.miniBtn,
                      { color: '#ff8a8a', borderColor: 'rgba(255,138,138,0.4)', background: 'rgba(255,138,138,0.12)' }),
                    title: '删除该项目的全部记忆（只删数据库）',
                    onClick: (e) => { e.stopPropagation(); pruneProject(p.project) },
                  }, '清理')
                : null,
            ),
            has
              ? h('div', { style: Object.assign({}, css.content, css.clamp(2)) }, p.handoff.preview)
              : h('div', { style: css.meta },
                  orphan ? '目录已删除，但记忆还在' : '目录已建，还没有记忆'),
            has
              ? h('div', { style: css.meta }, '更新于 ' + relTime(p.handoff.updated_at))
              : null,
          ))
        }
        if (items.length) {
          body.push(h('div', { key: 'rect', style: css.sectionTitle },
            '最近 ' + Math.min(items.length, 6) + ' 条（共 ' + items.length + ' 条）'))
        }
      }

      // 「全部」视图用紧凑单行，避免把面板撑满
      const compactMode = !project && !query.trim()
      const listItems = compactMode ? items.slice(0, 6) : items

      for (const it of listItems) {
        if (compactMode) {
          body.push(h('div', {
            key: 'c' + it.id,
            style: css.rowCompact,
            title: it.title,
            onClick: () => { setProject(it.project); setExpanded({}) },
          },
            h('div', { style: css.kind(it.kind) }, KIND_LABEL[it.kind] || it.kind),
            h('div', { style: css.rowName }, it.title),
            h('div', { style: css.meta }, it.project),
          ))
          continue
        }

        // 编辑态：整张卡片换成输入框
        if (editing && editing.id === it.id) {
          body.push(h('div', {
            key: it.id,
            style: Object.assign({}, css.card, { borderColor: 'rgba(106,168,255,0.6)' }),
            onClick: (e) => e.stopPropagation(),
          },
            h('div', { style: css.cardHead },
              h('div', { style: css.kind(it.kind) }, KIND_LABEL[it.kind] || it.kind),
              h('div', { style: css.cardTitle }, '编辑 id=' + it.id),
              h('div', { style: css.meta }, it.project),
            ),
            h('input', {
              style: css.editField,
              value: editing.title,
              placeholder: '标题',
              onChange: (e) => setEditing(Object.assign({}, editing, { title: e.target.value })),
              onKeyDown: (e) => {
                if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) saveEdit()
              },
            }),
            h('textarea', {
              style: css.editArea,
              value: editing.content,
              placeholder: '正文（⌘/Ctrl+Enter 保存）',
              rows: 6,
              onChange: (e) => setEditing(Object.assign({}, editing, { content: e.target.value })),
              onKeyDown: (e) => {
                if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) saveEdit()
              },
            }),
            h('div', { style: css.editBar },
              h('button', {
                style: css.btnGhost,
                onClick: () => setEditing(null),
                disabled: saving,
              }, '取消'),
              h('button', {
                style: Object.assign({}, css.btnPrimary, saving ? { opacity: 0.6 } : null),
                onClick: saveEdit,
                disabled: saving,
              }, saving ? '保存中…' : '保存'),
            ),
          ))
          continue
        }

        const isOpen = !!expanded[it.id]
        const q = query.trim()
        body.push(h('div', {
          key: it.id,
          style: Object.assign({}, css.card, { cursor: 'pointer' }),
          onClick: () => setExpanded(Object.assign({}, expanded, { [it.id]: !isOpen })),
        },
          h('div', { style: css.cardHead },
            h('div', { style: css.kind(it.kind) }, KIND_LABEL[it.kind] || it.kind),
            h('div', { style: css.cardTitle }, highlight(it.title, q)),
            h('div', {
              style: css.editBtn,
              title: '编辑这条记忆',
              onClick: (e) => {
                e.stopPropagation()
                setEditing({ id: it.id, title: it.title, content: it.content })
              },
            }, '✎'),
          ),
          h('div', { style: css.meta },
            it.project + ' · ' + relTime(it.created_at) + (isOpen ? '' : ' · 点击展开')),
          h('div', {
            style: isOpen ? css.content : Object.assign({}, css.content, css.clamp(2)),
          }, highlight(it.content, q)),
        ))
      }

      const panel = h('div', { style: css.panel },
        head,
        h('div', { style: css.tools },
          h('div', { style: css.searchWrap },
            h('input', {
              ref: searchRef,
              style: Object.assign({}, css.search, query ? { paddingRight: '26px' } : null),
              placeholder: '搜索记忆（留空显示最近）· Ctrl+K',
              value: query,
              onChange: (e) => setQuery(e.target.value),
              onKeyDown: (e) => {
                if (e.key === 'Escape') setQuery('')
              },
            }),
            query
              ? h('div', {
                  style: css.clearBtn,
                  title: '清空',
                  onClick: () => {
                    setQuery('')
                    if (searchRef.current) searchRef.current.focus()
                  },
                }, '✕')
              : null,
          ),
        ),
        h('div', { style: css.chips }, chips),
        h('div', { style: css.body }, body),
        toast ? h('div', { style: css.toast }, toast) : null,
      )

      // 拖动：拖动中只改 DOM（跟手），松手才 setState + 落 localStorage。
      // 关键：按下时先把当前位置写进 state —— 否则面板里的轮询/加载触发重渲染时，
      // 内联 style 会按「pos 仍为 null」重新算回 right/bottom，拖动当场被弹回去。
      function startDrag(e, toggleOnClick) {
        if (e.button !== 0) return
        e.preventDefault()
        const el = e.currentTarget
        const wrap = (el.closest && el.closest('[data-dsh-rag-panel]')) || el.parentNode || el
        const rect = wrap.getBoundingClientRect()
        const prevPos = posRef.current
        setPos({ x: rect.left, y: rect.top })
        const startX = e.clientX
        const startY = e.clientY
        let moved = false

        function move(ev) {
          const dx = ev.clientX - startX
          const dy = ev.clientY - startY
          if (!moved && Math.abs(dx) + Math.abs(dy) > 4) moved = true
          if (!moved) return
          const w = wrap.offsetWidth || 52
          const h = wrap.offsetHeight || 52
          const p = clampPos(rect.left + dx, rect.top + dy, w, h)
          wrap.style.left = p.x + 'px'
          wrap.style.top = p.y + 'px'
          wrap.style.right = 'auto'
          wrap.style.bottom = 'auto'
        }

        function detach() {
          window.removeEventListener('mousemove', move)
          window.removeEventListener('mouseup', up)
          window.removeEventListener('blur', onBlur)
        }

        function up() {
          detach()
          if (!moved) {
            setPos(prevPos)   // 只是单击，别把刚才的临时落位当成拖动结果
            if (toggleOnClick) setOpen(function (o) { return !o })
            return
          }
          const nr = wrap.getBoundingClientRect()
          const p = clampPos(nr.left, nr.top, nr.width, nr.height)
          setPos(p)
          savePos(p)
        }

        // 鼠标在窗口外松开（alt-tab、拖出浏览器）时 mouseup 不会来，
        // 监听会常驻、面板继续跟着鼠标跑。blur 兜底把它们摘掉。
        function onBlur() {
          detach()
        }

        window.addEventListener('mousemove', move)
        window.addEventListener('mouseup', up)
        window.addEventListener('blur', onBlur)
      }

      function resetPos(e) {
        e.preventDefault()
        setPos(null)
        savePos(null)
        flash('位置已复位')
      }

      const wrapStyle = pos
        ? Object.assign({}, css.wrap, {
            left: pos.x + 'px', top: pos.y + 'px', right: 'auto', bottom: 'auto',
          })
        : css.wrap

      const button = h('div', {
        style: Object.assign({}, css.btn, { marginLeft: 'auto', cursor: 'grab', touchAction: 'none' }),
        onMouseDown: (e) => startDrag(e, true),
        onContextMenu: resetPos,
        title: (open ? '收起' : '打开') + '项目记忆（可拖动，右键复位）',
      }, h(Icon), total > 0 ? h('div', { style: css.badge }, String(total)) : null)

      return h('div', { style: wrapStyle, 'data-dsh-rag-panel': '1' },
        open ? panel : null,
        h('div', { style: { display: 'flex', justifyContent: 'flex-end' } }, button),
      )
    }

        return {
          inject: ['slots'],
          apply(ctx) {
            try {
              ctx.slots.inject('shell.overlay', () => ctx.slots.register({
                name: 'shell.overlay',
                id: 'dsh-rag-panel',
                order: 120,
              }, Panel))
            } catch (e) {
              console.error('[dsh-rag-panel] slot 注册失败', e)
            }
          },
        }
        },
      })
    } catch (e) {
      console.error('[dsh-rag-panel] boot 失败', e)
    }
  }

  boot()
})()
