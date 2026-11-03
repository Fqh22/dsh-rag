"""把 DSH 会话归档抽取成项目记忆。

数据源：`~/.dsh/sessions/<workspace>/<session-id>/session.v4.jsonl.zstd`
（zstd 压缩的 JSONL 事件流）

## v0.5 的三处关键改动

1. **按 `source.kind` 过滤，而不是猜文本前缀。**
   DSH 给每条注入都打了标签：`user` / `agent-instructions` / `runtime-context` /
   `skill-catalog` / `time-context` / `user-approval` / `agent-message` /
   `subagent-settled` / `tool-jobs`。以前靠 `_NOISE_PREFIXES` 匹配正文开头，
   DSH 改一个字就漏，而且永远在追。

2. **接住 `compact-checkpoint`。**
   DSH 上下文压缩时会生成一段几千到几万字符的摘要（`<compacted-summary>`），
   那是模型自己对前文的高质量复述——比这里用规则拼的摘要强得多。v0.4 把它
   当成普通用户消息，还被 `CUT_USER=300` 截成 300 字。现在整段保留。

3. **记动作，不记噪声。**
   一次会话里工具调用往往比对话更能说明"做到哪了"。现在抽 `tool/call`，
   把「改了哪些文件、跑了什么命令」写进摘要，交接卡里的"下一步"才有依据。

不做 LLM 摘要的理由：
- 归档要能在无网、无 key 的情况下跑；
- 规则抽取已经能保住"谁在什么时候要做什么"，噪声由后面检索阶段的 kind/项目过滤吸收。
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import paths

SESSIONS_ROOT = Path.home() / ".dsh" / "sessions"
PROJECTS_ROOT = paths.PROJECTS_ROOT

# 正在写入的会话先不碰
STALE_SECONDS = 300
MAX_USER = 12
MAX_ASSISTANT = 10
MAX_ACTIONS = 40
CUT_USER = 300
CUT_ASSISTANT = 400
CUT_COMPACT = 14000
CUT_ACTION = 180

# 哪些工具算「写」。这个分类只服务于接力度量（要回答"开场多久才动真格"），
# 不追求穷尽 —— 漏判一两个只会让指标偏保守，不会出错。
WRITE_TOOLS = {"write", "edit", "ops_write", "ops_edit", "notebook_edit", "apply_patch"}
READ_TOOLS = {
    "read", "ops_read_files", "grep", "glob", "ops_grep", "ops_glob",
    "memory_read", "web_fetch", "web_search",
}
# ops_bash 既能读也能写，靠命令判断
_WRITE_CMD_RE = re.compile(
    r"(^|\s)(rm|rmdir|mv|cp|mkdir|touch|chmod|chown)\s"
    r"|>>?\s*\S"
    r"|git\s+(commit|add|push|checkout|reset|merge|rebase)"
    r"|pip\s+install|python\s+-m\s+pip|npm\s+(i|install)|pnpm\s+(i|add)"
    r"|Set-Content|Out-File|New-Item|Remove-Item|Copy-Item|Move-Item",
    re.I,
)
_PACK_RE = re.compile(r"pack v([0-9a-f]{8})")

# 这些 source.kind 是环境注入/框架消息，不是会话内容
IGNORE_KINDS = {
    "agent-instructions",
    "runtime-context",
    "skill-catalog",
    "time-context",
    "user-approval",
    "agent-message",
    "subagent-settled",
    "tool-jobs",
    "session-log",
}

# 压缩摘要开头的固定引导语，存库时去掉（它只是给模型的指令）
_COMPACT_PREAMBLE = (
    "This is an automatically generated checkpoint condensing an earlier span "
    "of the conversation to free up context."
)

_PATH_RE = re.compile(r"Projects[\\/]+([^\\/\s\"'`<>|:,;]{1,40})", re.I)
_PROJECTS_CACHE: list[str] | None = None


def _decode_zstd(path: Path) -> str:
    import zstandard

    dctx = zstandard.ZstdDecompressor()
    with open(path, "rb") as f:
        return dctx.stream_reader(f).read().decode("utf-8", "replace")


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]{1,40}>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _texts(content: Any) -> str:
    """从 content 数组里取纯文本（丢掉 reasoning / tool_use 等）。"""
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "\n".join(p for p in parts if p)


def _known_projects() -> list[str]:
    """Projects 目录下的真实项目名，作为推断候选。"""
    global _PROJECTS_CACHE
    if _PROJECTS_CACHE is None:
        try:
            _PROJECTS_CACHE = [
                p.name for p in PROJECTS_ROOT.iterdir()
                if p.is_dir() and not p.name.startswith(("_", "."))
            ]
        except OSError:
            _PROJECTS_CACHE = []
    return _PROJECTS_CACHE


def guess_project(blob: str, cwd: str) -> str:
    import projects as projects_mod

    low = blob.lower()
    scores: Counter = Counter()

    # 1) 真实项目名以独立词出现（避免 api 命中 api-server 之类的部分匹配）
    for name in _known_projects():
        pat = re.compile(rf"(?<![A-Za-z0-9]){re.escape(name.lower())}(?![A-Za-z0-9])")
        n = len(pat.findall(low))
        if n:
            scores[name] += n

    # 2) 显式路径 Projects\<name>\：可能是残缺名（my-api）或文件名（README.md），
    #    先收敛到已知目录再计分，收敛不了就丢掉——宁可归到「工作区」也不要造孤儿项目。
    for m in _PATH_RE.finditer(blob):
        name = m.group(1).strip()
        if not name or name.startswith("_"):
            continue
        resolved = projects_mod.resolve_known(name)
        if resolved:
            scores[resolved] += 3

    if scores:
        return scores.most_common(1)[0][0]

    parts = [p for p in re.split(r"[\\/]", cwd or "") if p]
    tail = parts[-1] if parts else ""
    # DSH 自身的配置/工作区路径不是业务项目
    if ".dsh" in cwd or "profiles" in cwd:
        return "工作区"
    if tail in ("default-workspace", "deepseek-harness", ""):
        return "工作区"
    return tail


def _classify_call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """判定一次工具调用是读、是写、还是其它；顺带取出它触碰的路径。"""
    short = (name or "").replace("mcp__rag__", "rag:")
    kind = "other"
    if short in WRITE_TOOLS:
        kind = "write"
    elif short in READ_TOOLS:
        kind = "read"
    elif short == "ops_bash":
        cmd = str(args.get("command") or "")
        kind = "write" if _WRITE_CMD_RE.search(cmd) else "read"

    path = ""
    for key in ("path", "file_path"):
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            path = v.strip()
            break
    if not path and isinstance(args.get("files"), list):
        for f in args["files"]:
            if isinstance(f, dict) and f.get("path"):
                path = str(f["path"])
                break
    return {"name": short, "kind": kind, "path": path}


def _summarize_action(name: str, args: dict[str, Any]) -> str | None:
    """把一次工具调用压成一行「做了什么」。太长或太噪的返回 None。"""
    short = name.replace("mcp__rag__", "rag:") if name else "?"
    detail = ""
    for key in ("description", "path", "file_path", "query", "command", "project"):
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            detail = v.strip().splitlines()[0]
            break
    if not detail and isinstance(args.get("files"), list):
        try:
            detail = ", ".join(str(f.get("path")) for f in args["files"][:3] if isinstance(f, dict))
        except Exception:  # noqa: BLE001
            detail = ""
    if not detail:
        return None
    return f"{short}: {detail[:CUT_ACTION]}"


def _metrics(calls: list[dict[str, Any]], assistant_seqs: list[int]) -> dict[str, Any]:
    """接力度量：开场到第一次写操作隔了多少步、同一文件被重复读了几次。

    这两个数才回答「上下文包有没有让人更快进入状态」；缓存命中率只回答成本。
    """
    first_write = next((c for c in calls if c["kind"] == "write"), None)
    steps_to_first_write = None
    if first_write is not None and first_write.get("seq") is not None:
        steps_to_first_write = sum(1 for s in assistant_seqs if s < first_write["seq"])

    read_paths = [c["path"] for c in calls if c["kind"] == "read" and c["path"]]
    repeated = sum(max(0, n - 1) for n in Counter(read_paths).values())
    write_paths = [c["path"] for c in calls if c["kind"] == "write" and c["path"]]

    return {
        "toolCalls": len(calls),
        "readCalls": sum(1 for c in calls if c["kind"] == "read"),
        "writeCalls": sum(1 for c in calls if c["kind"] == "write"),
        "stepsToFirstWrite": steps_to_first_write,
        "uniqueFilesRead": len(set(read_paths)),
        "repeatedReads": repeated,
        "filesWritten": list(dict.fromkeys(write_paths))[:12],
    }


def _worth_draft(sess: dict[str, Any]) -> bool:
    """值不值得生成状态草稿。

    门槛定得比归档高：草稿是给人/模型**确认**的，宁缺毋滥。
    只有「真的改过东西」或「被 DSH 压缩过（说明会话够长）」才值得起草。
    """
    metrics = sess.get("metrics") or {}
    return metrics.get("writeCalls", 0) > 0 or bool(sess["compacts"])


def _draft_meta(store, sess: dict[str, Any], project: str) -> dict[str, Any]:
    """从会话里拼一份状态草稿。刻意保守：只写"有证据"的字段。"""
    base = ""
    try:
        base = store.get_baseline(project)
    except Exception:  # noqa: BLE001
        base = ""
    goal = sess["users"][-1][:200] if sess["users"] else ""
    metrics = sess.get("metrics") or {}
    files = [{"path": p} for p in (metrics.get("filesWritten") or [])[:8]]

    done = [f"{sess['created_at'][:10]} {sess['title'][:60]}"]
    if not files:
        # 用 ops_bash 改东西时拿不到路径，退而记录动作，别让草稿空空如也
        done += [f"动作：{a}" for a in sess["actions"][:3]]
    return {
        "baseline": base,
        "goal": goal,
        "done": done,
        "files": files,
        "session": sess["session_id"],
    }


def parse_session(path: Path) -> dict[str, Any] | None:
    try:
        raw = _decode_zstd(path)
    except Exception:  # noqa: BLE001
        return None

    header: dict[str, Any] = {}
    title = ""
    users: list[str] = []
    assistants: list[str] = []
    actions: list[str] = []
    compact_parts: list[str] = []
    ignored: Counter = Counter()
    seen_user: set[str] = set()
    calls: list[dict[str, Any]] = []
    assistant_seqs: list[int] = []

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = ev.get("type")
        data = ev.get("data") or {}

        if t == "session":
            header = ev
        elif t == "session/title":
            title = str(data.get("title") or data.get("text") or title)
        elif t == "user/message":
            kind = str((data.get("source") or {}).get("kind") or "")
            if kind in IGNORE_KINDS:
                ignored[kind] += 1
                continue
            txt = _texts(data.get("content"))
            if not txt.strip():
                continue
            if kind == "compact-checkpoint":
                body = txt.replace(_COMPACT_PREAMBLE, "").strip()
                compact_parts.append(body)
                continue
            if kind != "user":
                ignored[kind or "unknown"] += 1
                continue
            cleaned = _clean(txt)
            if not cleaned:
                continue
            key = cleaned[:80]
            if key not in seen_user:
                seen_user.add(key)
                users.append(cleaned)
        elif t == "assistant/message":
            msg = data.get("message") or {}
            txt = _clean(_texts(msg.get("content")))
            if txt:
                assistants.append(txt)
            assistant_seqs.append(int(ev.get("seq") or 0))
        elif t == "tool/call":
            name = str(data.get("name") or "")
            try:
                args = json.loads(str(data.get("arguments") or "{}"))
            except (ValueError, TypeError):
                args = {}
            if not isinstance(args, dict):
                args = {}
            info = _classify_call(name, args)
            info["seq"] = int(ev.get("seq") or 0)
            calls.append(info)
            line_txt = _summarize_action(name, args)
            if line_txt:
                actions.append(line_txt)

    if not users and not assistants and not compact_parts:
        return None

    sid = str(header.get("id") or path.parent.name)
    cwd = str(header.get("cwd") or "")
    created = header.get("createdAt")
    ts = (
        datetime.fromtimestamp(created / 1000).astimezone().isoformat(timespec="seconds")
        if isinstance(created, (int, float))
        else datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
    )

    return {
        "session_id": sid,
        "title": title or (users[0][:40] if users else sid),
        "created_at": ts,
        "cwd": cwd,
        "users": users,
        "assistants": assistants,
        "actions": actions,
        "calls": calls,
        "metrics": {**_metrics(calls, assistant_seqs), **_pack_usage(raw)},
        "compacts": compact_parts,
        "ignored": dict(ignored),
        "raw_len": len(raw),
        "origin": header.get("origin") or "main",
    }


def _pack_usage(raw: str) -> dict[str, Any]:
    """从会话原文里找「这一轮读过哪个版本的上下文包」。

    包末尾那行 `(pack vXXXXXXXX · ...)` 会进 tool/result，所以能反查。
    有了它才能做「读过 pack 的会话 vs 没读过的」对照。
    """
    hits = _PACK_RE.findall(raw)
    return {
        "packVersion": hits[-1] if hits else None,
        "packReads": len(hits),
    }


def summarize(sess: dict[str, Any]) -> str:
    """拼摘要。有压缩摘要时以它为主体 —— 那是模型自己写的高质量复述。"""
    lines = [f"会话 {sess['session_id']}（{sess['origin']}）", f"时间：{sess['created_at']}"]
    if sess["cwd"]:
        lines.append(f"工作目录：{sess['cwd']}")

    if sess["compacts"]:
        body = "\n\n".join(sess["compacts"])[:CUT_COMPACT]
        lines.append("\n【DSH 压缩摘要（自动生成，优先采信）】")
        lines.append(body)

    if sess["users"]:
        lines.append("\n【用户请求】")
        for i, u in enumerate(sess["users"][:MAX_USER], 1):
            lines.append(f"{i}. {u[:CUT_USER]}")

    if sess["actions"]:
        lines.append("\n【动作（工具调用，已压缩）】")
        for i, a in enumerate(sess["actions"][:MAX_ACTIONS], 1):
            lines.append(f"{i}. {a}")

    if sess["assistants"]:
        lines.append("\n【助手结论（顺序）】")
        # 取前几条 + 最后几条，收尾的总结通常最有价值
        a = sess["assistants"]
        picked = a[:3] + (a[-4:] if len(a) > 7 else a[3:])
        for i, s in enumerate(picked, 1):
            lines.append(f"{i}. {s[:CUT_ASSISTANT]}")

    return "\n".join(lines)


def _project_of(sess: dict[str, Any]) -> str:
    blob = (
        "\n".join(sess["users"][:6])
        + "\n"
        + "\n".join(sess["actions"][:30])
        + "\n"
        + "\n".join(sess["assistants"][:3])
        + "\n"
        + sess["cwd"]
    )
    return guess_project(blob, sess["cwd"])


def iter_session_files(workspace_hint: str | None = None):
    if not SESSIONS_ROOT.exists():
        return
    for ws_dir in SESSIONS_ROOT.iterdir():
        if not ws_dir.is_dir():
            continue
        if workspace_hint and workspace_hint not in ws_dir.name:
            continue
        for sd in ws_dir.iterdir():
            if not sd.is_dir():
                continue
            f = sd / "session.v4.jsonl.zstd"
            if f.is_file():
                yield f


def run(
    store,
    project_filter: str | None = None,
    limit: int = 10,
    workspace_hint: str | None = None,
    min_mtime: float | None = None,
    force: bool = False,
) -> str:
    now = time.time()

    candidates = []
    for f in iter_session_files(workspace_hint):
        try:
            st = f.stat()
        except OSError:
            continue
        # 正在写入的会话一律不碰 —— force 只表示「已归档的也重抽」，
        # 不代表可以抽一个还在写的会话（会把半截内容固化下来）。
        if now - st.st_mtime < STALE_SECONDS:
            continue
        if min_mtime and st.st_mtime < min_mtime:
            continue  # 基线之前的存量，默认不回溯
        candidates.append((st.st_mtime, f))
    candidates.sort()  # 旧->新

    processed = 0
    added = 0
    skipped = 0
    compacts = 0
    drafts = 0
    projects: Counter = Counter()

    for _, f in candidates:
        if processed >= limit:
            break
        sess = parse_session(f)
        if not sess:
            continue
        if not force and store.session_done(sess["session_id"]):
            skipped += 1
            continue
        project = _project_of(sess)
        if project_filter and project != project_filter:
            # 不匹配的也记入台账之外——留待下次按项目归档
            continue
        processed += 1
        summary = summarize(sess)
        is_compact = bool(sess["compacts"])
        mid, created = store.remember(
            project=project,
            kind="session",
            title=f"会话归档：{sess['title'][:60]}",
            content=summary,
            session_id=sess["session_id"],
            ts=sess["created_at"],
            source="compact-checkpoint" if is_compact else "archive",
            meta={
                "steps": len(sess["assistants"]),
                "actions": len(sess["actions"]),
                "compacts": len(sess["compacts"]),
                "origin": sess["origin"],
            },
        )
        store.upsert_session(
            session_id=sess["session_id"],
            project=project,
            title=sess["title"][:120],
            cwd=sess["cwd"],
            origin=sess["origin"],
            started_at=sess["created_at"],
            last_seen_at=datetime.fromtimestamp(f.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
            archived_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            memory_id=mid,
            compact_count=len(sess["compacts"]),
            detail={
                "ignoredKinds": sess["ignored"],
                "rawChars": sess["raw_len"],
                "users": len(sess["users"]),
                "assistants": len(sess["assistants"]),
                "actions": len(sess["actions"]),
                "metrics": sess["metrics"],
                "packVersion": sess["metrics"].get("packVersion"),
            },
        )
        # 自动状态草稿：把「该写交接卡但忘了写」这半个问题补上。
        # 草稿是 status='draft'，不进检索、不进 pack，等模型/人确认。
        if project not in ("工作区", "未归类") and _worth_draft(sess):
            try:
                store.checkpoint_draft(
                    project,
                    _draft_meta(store, sess, project),
                    session_id=sess["session_id"],
                )
                drafts += 1
            except Exception:  # noqa: BLE001
                pass
        projects[project] += 1
        compacts += len(sess["compacts"])
        if created:
            added += 1
            try:
                import embed as embed_mod

                vecs = embed_mod.embed([summary[:2000]])
                if vecs:
                    store.add_vector(mid, vecs[0], embed_mod.MODEL_NAME)
            except Exception:  # noqa: BLE001
                pass

    if processed == 0:
        return f"没有需要归档的会话（已跳过 {skipped} 个已归档项）。"
    detail = "，".join(f"{p}: {c}" for p, c in projects.most_common())
    extra = f"，其中含 {compacts} 段压缩摘要" if compacts else ""
    if drafts:
        extra += f"，生成 {drafts} 份状态草稿"
    return f"归档完成：处理 {processed} 个会话，新增 {added} 条记忆{extra}（{detail}），跳过已归档 {skipped} 个。"
