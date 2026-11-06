"""dsh-rag MCP server（stdio，newline-delimited JSON-RPC 2.0）。

为什么不装官方 Python SDK：
- DSH 的客户端用 @modelcontextprotocol/client 2.2.0（2026-07-28 协议）；
- 这里只用到 tools 能力，协议面很小，手写反而能精确控制版本回声，
  也不会因为 SDK 与客户端版本漂移而连不上。

铁律：stdout 只能输出协议 JSON，任何日志走 stderr。
"""
from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from store import DB_PATH, Store

SERVER_NAME = "dsh-rag"
SERVER_VERSION = "0.1.0"
DEFAULT_PROTOCOL = "2025-06-18"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", newline="\n")

store: Store | None = None

_LOG_FILE = Path(__file__).resolve().parent.parent / "logs" / "mcp.log"


def log(msg: str) -> None:
    print(f"[dsh-rag] {msg}", file=sys.stderr, flush=True)
    # 同时落盘：桌面端拿不到子进程 stderr，出问题时只能靠这个文件判断有没有被拉起
    try:
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().astimezone().isoformat(timespec="seconds")
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{ts}  {msg}\n")
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------- tool schemas
TOOLS: list[dict[str, Any]] = [
    {
        "name": "recall",
        "description": (
            "从项目记忆库检索历史信息（关键词 + 语义混合检索）。"
            "用户提到\"之前/上次/以前那个\"或需要跨会话的上下文时主动调用。"
            "默认只返回当前项目、状态为 active 的记忆。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索语句，用自然语言或关键词"},
                "project": {"type": "string", "description": "限定项目名；省略表示不限项目"},
                "limit": {"type": "integer", "description": "返回条数，默认 8，最大 50"},
                "kinds": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "限定类型：handoff/decision/fact/todo/session 等",
                },
                "include_superseded": {
                    "type": "boolean",
                    "description": "是否包含已失效的旧记录，默认 false",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "pages_search",
        "description": (
            "在长文档（页面层）里做段落级检索。记忆库里的 recall 适合「事实/结论」，"
            "要查文档里的具体段落、命令、步骤时用这个。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要查的内容，自然语言"},
                "project": {"type": "string", "description": "限定项目；省略表示不限"},
                "limit": {"type": "integer", "description": "返回几段，默认 3"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "pages_index",
        "description": (
            "长文档的目录：标题 + 摘要 + 是否过期。想知道某个项目有哪些文档、"
            "每篇讲什么时用；比把全文读进来便宜得多。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "限定项目；省略表示不限"},
                "limit": {"type": "integer", "description": "最多几篇，默认 40"},
            },
        },
    },
    {
        "name": "remember",
        "description": (
            "把一条值得跨会话保留的信息写入项目记忆库。"
            "适合：已确认的决策、踩过的坑、环境事实、进行中的计划。"
            "不适合：临时调试细节、一次性命令输出。内容相同会自动去重。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "项目名，与 Projects 下的目录名对齐"},
                "kind": {
                    "type": "string",
                    "description": "类型：decision（决策）/ fact（事实）/ todo（待办）/ pitfall（坑）",
                },
                "title": {"type": "string", "description": "一句话标题"},
                "content": {"type": "string", "description": "正文，写清背景、结论、影响"},
            },
            "required": ["project", "kind", "title", "content"],
        },
    },
    {
        "name": "handoff_get",
        "description": "读取指定项目的交接卡（当前状态、关键决策、下一步）。新会话开局应优先调用。",
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string", "description": "项目名"}},
            "required": ["project"],
        },
    },
    {
        "name": "handoff_put",
        "description": "写入/更新项目交接卡（会覆盖该项目的旧交接卡）。建议在会话收尾时调用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "项目名"},
                "content": {
                    "type": "string",
                    "description": "交接卡正文，建议含：当前进展 / 关键决策 / 下一步 / 已知问题",
                },
                "session_id": {"type": "string", "description": "可选，来源会话 id"},
            },
            "required": ["project", "content"],
        },
    },
    {
        "name": "memory_update",
        "description": (
            "修改一条已存在的记忆（按 id）。用于纠正过时或写错的内容——"
            "比「新增一条再把旧的标记失效」更干净，也不会留下两条互相矛盾的记录。"
            "不传的字段保持不变。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "记忆 id（recall / memory_recent 会返回）"},
                "title": {"type": "string", "description": "新标题，省略则不改"},
                "content": {"type": "string", "description": "新正文，省略则不改"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "memory_recent",
        "description": "列出最近写入的记忆，用于快速确认记忆库里有什么。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "限定项目；省略表示全部"},
                "limit": {"type": "integer", "description": "条数，默认 20"},
            },
        },
    },
    {
        "name": "memory_stats",
        "description": "查看记忆库统计：记忆条数、向量条数、项目数。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "archive_sessions",
        "description": (
            "扫描 DSH 会话归档，把尚未入库的历史会话抽取成摘要写入记忆库。"
            "用于补录或定期归档。可指定项目过滤。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "仅归档指定项目；省略表示全部"},
                "limit": {"type": "integer", "description": "本次最多处理多少个会话，默认 10"},
            },
        },
    },
]


# --------------------------------------------------------------- formatters
def _fmt_memory(m: Any) -> str:
    return (
        f"### [{m.project}] {m.title}\n"
        f"- 类型: {m.kind} | 状态: {m.status} | 时间: {m.created_at} | id: {m.id}\n"
        f"{m.content}\n"
    )


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


# --------------------------------------------------------------- tool impls
def tool_recall(args: dict[str, Any]) -> dict[str, Any]:
    q = (args.get("query") or "").strip()
    if not q:
        return _err("query 不能为空")
    ms = store.recall(  # type: ignore[union-attr]
        q,
        project=args.get("project") or None,
        limit=int(args.get("limit") or 8),
        kinds=args.get("kinds") or None,
        include_superseded=bool(args.get("include_superseded")),
    )
    if not ms:
        return _ok(f"未检索到与「{q}」相关的记忆。")
    head = f"检索到 {len(ms)} 条相关记忆（query={q}）：\n\n"
    return _ok(head + "\n".join(_fmt_memory(m) for m in ms))


def tool_remember(args: dict[str, Any]) -> dict[str, Any]:
    mid, created = store.remember(  # type: ignore[union-attr]
        project=args.get("project") or "未归类",
        kind=args.get("kind") or "fact",
        title=args.get("title") or "",
        content=args.get("content") or "",
    )
    if not created:
        return _ok(f"已存在相同内容，跳过写入（id={mid}）。")
    # 异步化没必要：单条向量化在 CPU 上是几十毫秒级
    try:
        import embed as embed_mod

        vecs = embed_mod.embed([f"{args.get('title','')}\n{args.get('content','')}"])
        if vecs:
            store.add_vector(mid, vecs[0], embed_mod.MODEL_NAME)  # type: ignore[union-attr]
            return _ok(f"已写入记忆 id={mid}（含向量）。")
        return _ok(f"已写入记忆 id={mid}（向量化不可用，仅关键词可检索：{embed_mod.error()}）。")
    except Exception as exc:  # noqa: BLE001
        # 不能只回一句「失败」就完事：踩过一例——服务是旧代码、库已升过级，
        # 插入 vectors 少了 updated_at，16 条记忆静默无向量，日志里什么都没有。
        log(f"向量化失败 mid={mid}: {type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            "提示：若刚改过 store.py/schema.py，运行中的 MCP 服务仍是旧代码，需重启 DSH。")
        return _ok(f"已写入记忆 id={mid}（向量化失败：{exc}）。")


def tool_handoff_get(args: dict[str, Any]) -> dict[str, Any]:
    project = (args.get("project") or "").strip()
    if not project:
        projects = [p["project"] for p in store.projects()]  # type: ignore[union-attr]
        return _err(f"缺少 project。已有项目：{', '.join(projects) or '（空）'}")
    m = store.get_handoff(project)  # type: ignore[union-attr]
    if not m:
        return _ok(f"项目「{project}」还没有交接卡。")
    sess = store.latest_session(store.normalize_project(project))  # type: ignore[union-attr]
    hint = store._resume_hint(project, sess)  # type: ignore[union-attr]
    tail = f"\n\n（更新时间 {m.created_at}，id {m.id}）"
    if hint.get("advice"):
        tail += f"\n（续接建议：{hint['advice']}）"
    return _ok(f"## 项目交接卡：{project}\n\n{m.content}{tail}")


def tool_handoff_put(args: dict[str, Any]) -> dict[str, Any]:
    project = (args.get("project") or "").strip()
    content = (args.get("content") or "").strip()
    if not project or not content:
        return _err("project 与 content 都不能为空")
    mid = store.put_handoff(project, content, args.get("session_id"))  # type: ignore[union-attr]
    try:
        import embed as embed_mod

        vecs = embed_mod.embed([f"项目交接卡 {project}\n{content}"])
        if vecs:
            store.add_vector(mid, vecs[0], embed_mod.MODEL_NAME)  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass
    return _ok(f"项目「{project}」交接卡已更新（id={mid}）。")


def tool_memory_recent(args: dict[str, Any]) -> dict[str, Any]:
    ms = store.recent(args.get("project") or None, int(args.get("limit") or 20))  # type: ignore[union-attr]
    if not ms:
        return _ok("记忆库为空。")
    return _ok(f"最近 {len(ms)} 条记忆：\n\n" + "\n".join(_fmt_memory(m) for m in ms))


def tool_memory_update(args: dict[str, Any]) -> dict[str, Any]:
    try:
        mid = int(args.get("id") or 0)
    except (TypeError, ValueError):
        return _err("id 必须是整数")
    if not mid:
        return _err("id 必填")
    title = args.get("title")
    content = args.get("content")
    if title is None and content is None:
        return _err("title 和 content 至少要给一个")
    ok, msg = store.update_memory(mid, title, content)  # type: ignore[union-attr]
    if not ok:
        return _err(msg)
    return _ok(f"id={mid}: {msg}")


def tool_memory_stats(_: dict[str, Any]) -> dict[str, Any]:
    s = store.stats()  # type: ignore[union-attr]
    import embed as embed_mod

    err = embed_mod.error()
    lines = [
        f"记忆总数: {s['total']}",
        f"生效中: {s['active']}",
        f"已向量化: {s['vectors']}",
        f"缺向量: {s.get('missing_vectors', 0)}"
        + ("（跑 scripts/backfill_vectors.py 补）" if s.get("missing_vectors") else ""),
        f"项目数: {s['projects']}",
        f"会话台账: {s.get('sessions', 0)}",
        "按类型: " + ("，".join(f"{k}={v}" for k, v in (s.get("kinds") or {}).items()) or "（空）"),
        f"数据库: {DB_PATH}",
        f"结构版本: v{(s.get('schema') or {}).get('version', '?')}"
        + ("（有迁移备份表 _v4_*）" if (s.get("schema") or {}).get("leftovers") else ""),
        f"向量模型: {embed_mod.MODEL_NAME}" + (f"（不可用: {err}）" if err else "（正常）"),
    ]
    return _ok("\n".join(lines))


def tool_archive_sessions(args: dict[str, Any]) -> dict[str, Any]:
    import archive

    return _ok(
        archive.run(
            store=store,
            project_filter=args.get("project") or None,
            limit=int(args.get("limit") or 10),
        )
    )


# ------------------------------------------------------------ v0.5 新增工具
TOOLS.extend([
    {
        "name": "handoff_pack",
        "description": (
            "取项目上下文包：基线 + 长期事实 + 当前状态 + 下一步，按固定顺序渲染。"
            "**新会话开场优先用它**，而不是 handoff_get——包的分层顺序是为前缀缓存优化的"
            "（稳定内容在前，易变内容在后）。输出末尾附读取提示，不参与缓存前缀。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "项目名"},
                "budget_tokens": {"type": "integer", "description": "预算 token，默认 3000"},
                "with_meta": {
                    "type": "boolean",
                    "description": "是否在事实里带 id/类型（默认关闭，保持字节稳定）",
                },
            },
            "required": ["project"],
        },
    },
    {
        "name": "checkpoint_put",
        "description": (
            "写入/合并项目的结构化状态（目标、已完成、进行中、下一步、现场文件、未决问题）。"
            "会自动重渲染交接卡。baseline 字段是**稳定基线**（技术栈/目录/红线），"
            "只在真的变化时才改——它是跨会话复用缓存前缀的地基。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "项目名"},
                "baseline": {"type": "string", "description": "项目基线（稳定，少改）"},
                "goal": {"type": "string", "description": "当前目标"},
                "done": {"type": "array", "items": {"type": "string"}, "description": "已完成"},
                "doing": {"type": "array", "items": {"type": "string"}, "description": "进行中"},
                "next": {"type": "array", "items": {"type": "string"}, "description": "下一步"},
                "open_questions": {
                    "type": "array", "items": {"type": "string"}, "description": "未决问题",
                },
                "files": {
                    "type": "array",
                    "description": "现场文件：[{path, why}]",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "why": {"type": "string"},
                        },
                        "additionalProperties": True,
                    },
                },
                "session_id": {"type": "string"},
            },
            "required": ["project"],
        },
    },
    {
        "name": "checkpoint_get",
        "description": "读项目的结构化状态（checkpoint 原始字段，不是渲染后的卡）。",
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string"}},
            "required": ["project"],
        },
    },
    {
        "name": "cache_stats",
        "description": (
            "统计 DSH 会话的上下文缓存命中率与压缩事件（解析 ~/.dsh/sessions 里的 usage）。"
            "想知道「缓存到底有没有变好」时用这个，别猜。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "限定项目；省略=全部"},
                "limit": {"type": "integer", "description": "最近多少个会话，默认 20"},
            },
        },
    },
    {
        "name": "doctor",
        "description": (
            "体检：结构版本、FTS/向量一致性、会话日志能否解析、孤儿项目、草稿积压。"
            "升级 DSH 或迁移库之后跑一次——格式漂移不会报错，只会静默少记。"
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "cleanup",
        "description": (
            "列出（并可选执行）低价值信息的清理计划：过期会话摘要、多余的失效交接卡、"
            "没人确认的草稿。默认只出计划；执行是软删除（status=archived），可以 restore。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "apply": {"type": "boolean", "description": "真的执行（默认 false，只出计划）"},
            },
        },
    },
    {
        "name": "checkpoint_promote",
        "description": (
            "把归档自动生成的状态草稿并入生效 checkpoint。"
            "会话收尾没写状态时，草稿里有目标/已完成/改过的文件，先看一眼再采纳。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string"}},
            "required": ["project"],
        },
    },
])


def tool_handoff_pack(args: dict[str, Any]) -> dict[str, Any]:
    project = (args.get("project") or "").strip()
    if not project:
        return _err("缺少 project")
    try:
        budget = int(args.get("budget_tokens") or 3000)
    except (TypeError, ValueError):
        budget = 3000
    import card as card_mod

    pack = store.handoff_pack(  # type: ignore[union-attr]
        project, budget_tokens=budget, with_meta=bool(args.get("with_meta"))
    )
    # 元信息放**末尾**：它在 pack 之外，别去污染缓存前缀
    tail = ["", "---", f"（{card_mod.describe_pack_header(pack)}）"]
    if pack.get("resume", {}).get("advice"):
        tail.append(f"（续接建议：{pack['resume']['advice']}）")
    if not pack["hasHandoff"] and not pack["checkpoint"]:
        tail.append("（这个项目还没有交接卡；先干活，收尾时用 checkpoint_put 写一份）")
    return _ok(pack["text"] + "\n".join(tail))


def tool_checkpoint_put(args: dict[str, Any]) -> dict[str, Any]:
    project = (args.get("project") or "").strip()
    if not project:
        return _err("缺少 project")
    patch: dict[str, Any] = {}
    for key in ("baseline", "goal", "done", "doing", "next", "open_questions", "files"):
        if key in args and args[key] is not None:
            patch[key] = args[key]
    if not patch:
        return _err("至少要给一个字段（baseline/goal/done/doing/next/open_questions/files）")

    mid, created, card_text = store.checkpoint_put(  # type: ignore[union-attr]
        project, patch, session_id=args.get("session_id")
    )
    # 向量由 store.checkpoint_put 内部重算（reembed=True），这里不再重复调一次
    return _ok(
        f"项目「{project}」状态已{'新建' if created else '更新'}（checkpoint id={mid}），"
        f"交接卡已重渲染（{len(card_text)} 字）。"
    )


def tool_checkpoint_get(args: dict[str, Any]) -> dict[str, Any]:
    project = (args.get("project") or "").strip()
    if not project:
        return _err("缺少 project")
    cp = store.get_checkpoint(project)  # type: ignore[union-attr]
    draft = store.get_draft(project)  # type: ignore[union-attr]
    if not cp and not draft:
        return _ok(f"项目「{project}」没有结构化状态。用 checkpoint_put 写一份。")

    lines: list[str] = []
    if cp:
        lines = [f"## {project} 结构化状态（checkpoint id={cp.id}，更新于 {cp.updated_at}）"]
        meta = cp.meta
        if meta.get("baseline"):
            lines += ["", "### baseline（稳定）", str(meta["baseline"])]
        if meta.get("goal"):
            lines += ["", "### goal", str(meta["goal"])]
        for key in ("done", "doing", "next", "open_questions"):
            items = meta.get(key) or []
            if items:
                lines += ["", f"### {key}", "\n".join(f"- {x}" for x in items)]
        files = meta.get("files") or []
        if files:
            lines += ["", "### files"]
            lines += [f"- {f.get('path')}" + (f" —— {f.get('why')}" if f.get("why") else "")
                      for f in files if isinstance(f, dict)]

    if draft:
        lines += ["", f"## 待确认草稿（id={draft.id}，来自归档，更新于 {draft.updated_at}）"]
        dm = draft.meta
        if dm.get("goal"):
            lines.append(f"- 目标：{dm['goal']}")
        for it in (dm.get("done") or [])[:3]:
            lines.append(f"- 做过：{it}")
        for it in (dm.get("files") or [])[:6]:
            if isinstance(it, dict) and it.get("path"):
                lines.append(f"- 碰过的文件：{it['path']}")
        lines += ["", "（确认无误就调 checkpoint_promote 采纳；不想要就丢弃）"]

    return _ok("\n".join(lines))


def tool_cache_stats(args: dict[str, Any]) -> dict[str, Any]:
    import insight

    project = args.get("project") or None
    limit = int(args.get("limit") or 20)

    def resolver(sid: str) -> str | None:
        for s in store.sessions(limit=500):  # type: ignore[union-attr]
            if s["session_id"] == sid:
                return s.get("project")
        return None

    rep = insight.report(limit=limit, project=project, resolver=resolver)
    relay = insight.relay_report(store, project=project)  # type: ignore[arg-type]
    if not rep["sessions"]:
        return _ok("没有可解析的会话日志。")
    return _ok(insight.format_report(rep) + "\n\n" + insight.format_relay(relay))


def tool_doctor(_: dict[str, Any]) -> dict[str, Any]:
    import doctor

    rep = doctor.run(store)  # type: ignore[arg-type]
    return _ok(doctor.format_report(rep))


def tool_cleanup(args: dict[str, Any]) -> dict[str, Any]:
    import cleanup

    plan = cleanup.plan(store)  # type: ignore[arg-type]
    text = cleanup.format_plan(plan)
    if not args.get("apply"):
        return _ok(text + "\n\n（只出了计划。要执行就再调一次 cleanup，apply=true）")
    done = cleanup.apply(store, plan)  # type: ignore[arg-type]
    return _ok(
        f"{text}\n\n已归档 {done['archived']} 条、作废草稿 {done['draftsDropped']} 份"
        f"（备份：{done['backup']}）。用 restore id 可以恢复。"
    )


def tool_checkpoint_promote(args: dict[str, Any]) -> dict[str, Any]:
    project = (args.get("project") or "").strip()
    if not project:
        return _err("缺少 project")
    draft = store.get_draft(project)  # type: ignore[union-attr]
    if not draft:
        return _ok(f"项目「{project}」没有待确认的草稿。")
    res = store.promote_draft(project)  # type: ignore[union-attr]
    if not res:
        return _err("采纳失败：草稿已不存在")
    mid, created, card = res
    # 向量由 store.promote_draft → checkpoint_put 内部重算，这里不再重复
    return _ok(f"已采纳草稿 #{draft.id}，生成 checkpoint #{mid}，交接卡已重渲染。")


def tool_pages_search(args: dict[str, Any]) -> dict[str, Any]:
    """长文档里的块级检索（页面层）。记忆库查不到细节时用它。"""
    q = (args.get("query") or "").strip()
    if not q:
        return _err("query 不能为空")
    import pages as pages_mod

    conn = store._conn  # type: ignore[union-attr]
    hits = pages_mod.search_chunks(conn, q, args.get("project") or None,
                                   int(args.get("limit") or 3))
    if not hits:
        return _ok(f"页面层里没检索到与「{q}」相关的段落。")
    lines = []
    for h in hits:
        flag = "（可能过时）" if h["stale"] else ""
        lines.append(f"【{h['title']}{flag}】score={h['score']}\n{h['text']}")
    return _ok("\n\n".join(lines))


def tool_pages_index(args: dict[str, Any]) -> dict[str, Any]:
    """长文档的目录：标题 + 摘要 + 是否过期。比全文便宜得多。"""
    import pages as pages_mod

    conn = store._conn  # type: ignore[union-attr]
    rows = pages_mod.index_list(conn, args.get("project") or None,
                                int(args.get("limit") or 40))
    if not rows:
        return _ok("页面层还是空的（用 `cli.py pages --dir <目录> --project <项目>` 灌）。")
    return _ok(pages_mod.render_index(rows))


HANDLERS = {
    "recall": tool_recall,
    "pages_search": tool_pages_search,
    "pages_index": tool_pages_index,
    "remember": tool_remember,
    "handoff_get": tool_handoff_get,
    "handoff_put": tool_handoff_put,
    "handoff_pack": tool_handoff_pack,
    "checkpoint_put": tool_checkpoint_put,
    "checkpoint_get": tool_checkpoint_get,
    "checkpoint_promote": tool_checkpoint_promote,
    "memory_update": tool_memory_update,
    "memory_recent": tool_memory_recent,
    "memory_stats": tool_memory_stats,
    "cache_stats": tool_cache_stats,
    "doctor": tool_doctor,
    "cleanup": tool_cleanup,
    "archive_sessions": tool_archive_sessions,
}


# --------------------------------------------------------------- JSON-RPC
def send(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(req: dict[str, Any]) -> None:
    method = req.get("method")
    rid = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        client_version = params.get("protocolVersion") or DEFAULT_PROTOCOL
        send({
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "protocolVersion": client_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "项目记忆库。新会话开场先用 handoff_pack 取上下文包（按稳定前缀排好，"
                    "能跨会话复用缓存）；需要更多细节用 recall；确认结论用 remember；"
                    "收尾用 checkpoint_put 写结构化状态（会自动重渲染交接卡）。"
                    "归档会自动生成状态草稿：checkpoint_get 能看到，checkpoint_promote 采纳。"
                    "缓存用 cache_stats 看，健康度用 doctor 看，旧信息该收的用 cleanup 看计划。"
                ),
            },
        })
        return

    if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        return

    if method == "ping":
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
        return

    if method == "tools/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
        return

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        handler = HANDLERS.get(name)
        if handler is None:
            send({"jsonrpc": "2.0", "id": rid,
                  "error": {"code": -32601, "message": f"未知工具: {name}"}})
            return
        try:
            result = handler(args)
        except Exception as exc:  # noqa: BLE001
            log(traceback.format_exc())
            result = _err(f"工具执行失败: {type(exc).__name__}: {exc}")
        send({"jsonrpc": "2.0", "id": rid, "result": result})
        return

    # 未识别的方法：有 id 才回错误，通知类静默
    if rid is not None:
        send({"jsonrpc": "2.0", "id": rid,
              "error": {"code": -32601, "message": f"未实现的方法: {method}"}})


def _startup_archive() -> None:
    """启动时补一次会话归档。

    MCP server 在 DSH 启动时拉起，正好是「上一个会话已经结束」的时刻。
    这样不用装常驻守护也能保证记忆库不落后；失败只记日志，绝不影响工具服务。
    """
    import threading
    import time as _time

    def work() -> None:
        _time.sleep(8)  # 别和 DSH 启动抢 IO
        try:
            import archive

            state = Path(__file__).resolve().parent.parent / "data" / "watch-state.json"
            cutoff = None
            if state.is_file():
                try:
                    cutoff = float(json.loads(state.read_text(encoding="utf-8"))["archive_from"])
                except Exception:  # noqa: BLE001
                    cutoff = None
            msg = archive.run(store=store, limit=10, min_mtime=cutoff)
            log(f"startup archive: {msg}")
        except Exception:  # noqa: BLE001
            log("startup archive failed: " + traceback.format_exc().replace("\n", " | "))

    threading.Thread(target=work, name="dsh-rag-startup-archive", daemon=True).start()


def _warn_missing_vectors() -> None:
    """启动自检：有记忆没向量，语义检索会静默漏召回。只告警，不在这里补
    （补要联网调 embedding，会把启动拖慢；交给 scripts/backfill_vectors.py）。"""
    try:
        import embed as embed_mod

        miss = store.missing_vectors(embed_mod.MODEL_NAME)  # type: ignore[union-attr]
        if miss:
            log(
                f"警告：{len(miss)} 条生效记忆没有 {embed_mod.MODEL_NAME} 的向量，"
                "语义检索会漏召回；修复：.venv/Scripts/python.exe scripts/backfill_vectors.py"
            )
    except Exception:  # noqa: BLE001
        log("缺向量自检失败:\n" + traceback.format_exc())


def main() -> None:
    global store
    import os

    log(f"process start pid={os.getpid()} cwd={os.getcwd()}")
    try:
        store = Store()
    except Exception as exc:  # noqa: BLE001
        log(f"Store 初始化失败: {exc}")
        raise
    log(f"ready, db={DB_PATH}")
    _warn_missing_vectors()
    if (os.environ.get("DSH_RAG_STARTUP_ARCHIVE") or "1") != "0":
        _startup_archive()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            log(f"非法 JSON: {line[:200]}")
            continue
        try:
            if isinstance(req, list):
                for r in req:
                    handle(r)
            else:
                if req.get("method") == "initialize":
                    log("received initialize")
                handle(req)
        except Exception:  # noqa: BLE001
            log(traceback.format_exc())
            # 任何工具调用抛异常都可能留下未提交的写事务，堵住其它进程；这里统一退掉。
            if store is not None:
                store.rollback()


if __name__ == "__main__":
    main()
