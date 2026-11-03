"""缓存与压缩的度量：把「感觉慢」变成数字。

数据源只有一个：DSH 自己的会话日志
`~/.dsh/sessions/<workspace>/<session-id>/session.v4.jsonl.zstd`。

里面每条 `assistant/message` 都带 usage：

    inputTokens     本条**新增**（未命中缓存）的输入 token
    cacheReadTokens 命中上下文缓存的 token

所以真实命中率是 `cache / (cache + input)`，而不是 `cache / input`
（v4 时期有人按后者算，得到 400% 这种数字）。

另一个关键事件是 `user/message` 且 `source.kind == 'compact-checkpoint'`：
DSH 在这里把前面的长历史压成一段摘要。压缩会**改写整段前缀**，
所以压缩后的第一次请求几乎全是 miss —— 这是长会话里最贵的一步。

本模块只读日志，不写任何东西。
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import paths

SESSIONS_ROOT = Path.home() / ".dsh" / "sessions"

_PROJECTS_ROOT = paths.PROJECTS_ROOT
_PATH_RE = re.compile(r"Projects[\\/]+([^\\/\s\"'`<>|:,;]{1,40})", re.I)


def session_files(limit: int = 40) -> list[Path]:
    if not SESSIONS_ROOT.exists():
        return []
    files = [p for p in SESSIONS_ROOT.glob("*/*/session.v4.jsonl.zstd") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:limit]


def _read(path: Path) -> str | None:
    try:
        import zstandard

        dctx = zstandard.ZstdDecompressor()
        with open(path, "rb") as f:
            return dctx.stream_reader(f).read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None


def _guess_project(blob: str, cwd: str) -> str:
    """项目归属复用归档那套推断，避免两处规则漂移。"""
    try:
        import archive

        return archive.guess_project(blob, cwd)
    except Exception:  # noqa: BLE001
        return "工作区"


def parse(path: Path) -> dict[str, Any] | None:
    raw = _read(path)
    if raw is None:
        return None

    header: dict[str, Any] = {}
    title = ""
    usages: list[tuple[int, int, int]] = []   # (seq, input, cache)
    compacts: list[int] = []                  # 压缩发生的 seq
    user_blob: list[str] = []

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = ev.get("type")
        d = ev.get("data") or {}
        seq = int(ev.get("seq") or 0)

        if t == "session":
            header = ev
        elif t == "session/title":
            title = str(d.get("title") or d.get("text") or title)
        elif t == "user/message":
            kind = (d.get("source") or {}).get("kind")
            text = "".join(
                b.get("text", "") for b in (d.get("content") or [])
                if isinstance(b, dict) and b.get("type") == "text"
            )
            if kind == "compact-checkpoint":
                compacts.append(seq)
            elif kind == "user":
                user_blob.append(text[:300])
        elif t == "assistant/message":
            u = d.get("usage") or {}
            if u:
                usages.append((seq, int(u.get("inputTokens") or 0),
                               int(u.get("cacheReadTokens") or 0)))

    if not usages:
        return None

    sid = str(header.get("id") or path.parent.name)
    cwd = str(header.get("cwd") or "")
    created = header.get("createdAt")
    started = (
        datetime.fromtimestamp(created / 1000).astimezone().isoformat(timespec="seconds")
        if isinstance(created, (int, float))
        else datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
    )

    project = _guess_project("\n".join(user_blob) + "\n" + cwd, cwd)

    new_tokens = sum(u[1] for u in usages)
    cache_tokens = sum(u[2] for u in usages)
    first_seq, first_in, first_cache = usages[0]

    # 每次压缩的代价：压缩前最后一步的缓存量 vs 压缩后第一步的新增 token
    compact_detail: list[dict[str, Any]] = []
    for cseq in compacts:
        before = [u for u in usages if u[0] < cseq]
        after = [u for u in usages if u[0] > cseq]
        compact_detail.append({
            "seq": cseq,
            "cacheBefore": before[-1][2] if before else 0,
            "newAfter": after[0][1] if after else 0,
            "cacheAfter": after[0][2] if after else 0,
            "steps": (after[0][0] - cseq) if after else None,
        })

    return {
        "sessionId": sid,
        "project": project,
        "title": title or (user_blob[0][:40] if user_blob else sid),
        "startedAt": started,
        "cwd": cwd,
        "steps": len(usages),
        "newTokens": new_tokens,
        "cacheTokens": cache_tokens,
        "hitRate": round(cache_tokens / (new_tokens + cache_tokens), 4) if (new_tokens + cache_tokens) else 0.0,
        "firstStep": {"new": first_in, "cache": first_cache,
                      "hitRate": round(first_cache / (first_in + first_cache), 4) if (first_in + first_cache) else 0.0},
        "compacts": compact_detail,
        "file": str(path),
    }


def report(
    limit: int = 20,
    project: str | None = None,
    resolver: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    """汇总最近 N 个会话的缓存/压缩情况。

    `resolver(session_id)` 可以按会话台账纠正项目归属（推断不如台账可靠）。
    """
    rows: list[dict[str, Any]] = []
    for p in session_files(limit):
        info = parse(p)
        if not info:
            continue
        if resolver:
            try:
                fixed = resolver(info["sessionId"])
            except Exception:  # noqa: BLE001
                fixed = None
            if fixed:
                info["project"] = fixed
        if project and info["project"] != project:
            continue
        rows.append(info)

    by_project: dict[str, dict[str, Any]] = {}
    for r in rows:
        b = by_project.setdefault(r["project"], {
            "project": r["project"], "sessions": 0, "steps": 0,
            "newTokens": 0, "cacheTokens": 0, "compacts": 0,
            "lastSession": None, "firstStepHit": [],
        })
        b["sessions"] += 1
        b["steps"] += r["steps"]
        b["newTokens"] += r["newTokens"]
        b["cacheTokens"] += r["cacheTokens"]
        b["compacts"] += len(r["compacts"])
        b["firstStepHit"].append(r["firstStep"]["hitRate"])
        if not b["lastSession"] or r["startedAt"] > b["lastSession"]:
            b["lastSession"] = r["startedAt"]

    for b in by_project.values():
        total = b["newTokens"] + b["cacheTokens"]
        b["hitRate"] = round(b["cacheTokens"] / total, 4) if total else 0.0
        hits = b.pop("firstStepHit")
        b["firstStepHitRate"] = round(sum(hits) / len(hits), 4) if hits else 0.0

    total_new = sum(r["newTokens"] for r in rows)
    total_cache = sum(r["cacheTokens"] for r in rows)
    return {
        "sessions": len(rows),
        "steps": sum(r["steps"] for r in rows),
        "newTokens": total_new,
        "cacheTokens": total_cache,
        "hitRate": round(total_cache / (total_new + total_cache), 4) if (total_new + total_cache) else 0.0,
        "firstStepCache": Counter(r["firstStep"]["cache"] for r in rows).most_common(),
        "compactions": [
            {"sessionId": r["sessionId"], "project": r["project"], **c}
            for r in rows for c in r["compacts"]
        ],
        "byProject": sorted(by_project.values(), key=lambda x: x["lastSession"] or "", reverse=True),
        "sessionsDetail": rows,
    }


def format_report(rep: dict[str, Any], top: int = 12) -> str:
    lines = [
        f"最近 {rep['sessions']} 个会话 / {rep['steps']} 步",
        f"整体缓存命中：{rep['hitRate'] * 100:.1f}%（命中 {rep['cacheTokens']:,} / 新增 {rep['newTokens']:,} token）",
        "首步命中分布：" + "，".join(f"{k} token × {v}" for k, v in rep["firstStepCache"][:6]),
    ]
    if rep["compactions"]:
        lines.append("")
        lines.append("压缩事件（前缀被改写，代价最大）：")
        for c in rep["compactions"][:10]:
            lines.append(
                f"- [{c['project']}] {c['sessionId'][:22]}… 压缩前缓存 {c['cacheBefore']:,} → "
                f"压缩后首步新增 {c['newAfter']:,} token"
            )
    if rep["byProject"]:
        lines.append("")
        lines.append("按项目：")
        for b in rep["byProject"][:top]:
            lines.append(
                f"- {b['project']}: {b['sessions']} 会话 / {b['steps']} 步 / "
                f"命中 {b['hitRate'] * 100:.1f}% / 首步 {b['firstStepHitRate'] * 100:.0f}% / "
                f"压缩 {b['compacts']} 次"
            )
    return "\n".join(lines)


# --------------------------------------------------------------------- 接力
# 缓存度量回答的是「花了多少钱」，接力度量回答的是「有没有更快进入状态」。
# 后者才是这个项目存在的理由，所以单独一块。

def relay_report(store, project: str | None = None, limit: int = 100) -> dict[str, Any]:
    """从会话台账里汇总接力效率。

    指标（都来自 archive._metrics）：
      stepsToFirstWrite  开场到第一次写操作之间经过了多少个模型步
      repeatedReads      同一文件被重复读了几次（上下文没记住的硬证据）
      packVersion        这轮读过哪个版本的上下文包（null = 没读）
    """
    rows = store.sessions(project, limit)
    items: list[dict[str, Any]] = []
    for r in rows:
        m = (r.get("detail") or {}).get("metrics") or {}
        if not m:
            continue
        items.append({
            "sessionId": r["session_id"],
            "project": r.get("project"),
            "startedAt": r.get("started_at"),
            "stepsToFirstWrite": m.get("stepsToFirstWrite"),
            "toolCalls": m.get("toolCalls") or 0,
            "writeCalls": m.get("writeCalls") or 0,
            "repeatedReads": m.get("repeatedReads") or 0,
            "packVersion": (r.get("detail") or {}).get("packVersion") or m.get("packVersion"),
        })

    def agg(sub: list[dict[str, Any]]) -> dict[str, Any]:
        firsts = [i["stepsToFirstWrite"] for i in sub if i["stepsToFirstWrite"] is not None]
        return {
            "sessions": len(sub),
            "avgStepsToFirstWrite": round(sum(firsts) / len(firsts), 1) if firsts else None,
            "avgRepeatedReads": round(
                sum(i["repeatedReads"] for i in sub) / len(sub), 1) if sub else None,
            "avgToolCalls": round(sum(i["toolCalls"] for i in sub) / len(sub), 1) if sub else None,
        }

    with_pack = [i for i in items if i["packVersion"]]
    without = [i for i in items if not i["packVersion"]]

    by_project: dict[str, list[dict[str, Any]]] = {}
    for i in items:
        by_project.setdefault(i["project"] or "未归类", []).append(i)

    return {
        "sessions": len(items),
        "items": sorted(items, key=lambda i: i["startedAt"] or "", reverse=True),
        "overall": agg(items),
        "withPack": agg(with_pack),
        "withoutPack": agg(without),
        "byProject": {k: agg(v) for k, v in by_project.items()},
        "note": "stepsToFirstWrite 越小越好；repeatedReads 高说明开场时上下文没给够。",
    }


def format_relay(rep: dict[str, Any]) -> str:
    if not rep["sessions"]:
        return "还没有带指标（tool/call）的归档会话。"
    o = rep["overall"]
    lines = [
        f"接力度量：{o['sessions']} 个会话",
        f"  平均 {o['avgStepsToFirstWrite']} 步才第一次写；"
        f"平均重复读 {o['avgRepeatedReads']} 次；平均工具调用 {o['avgToolCalls']} 次",
    ]
    w, wo = rep["withPack"], rep["withoutPack"]
    if w["sessions"] and wo["sessions"]:
        lines.append(
            f"  读过 pack：{w['sessions']} 个，{w['avgStepsToFirstWrite']} 步首写 / "
            f"{w['avgRepeatedReads']} 次重复读"
        )
        lines.append(
            f"  没读 pack：{wo['sessions']} 个，{wo['avgStepsToFirstWrite']} 步首写 / "
            f"{wo['avgRepeatedReads']} 次重复读"
        )
    else:
        lines.append("  对照样本不足（需要同时有读过/没读过 pack 的会话）")
    if rep["byProject"]:
        lines.append("  按项目：")
        for proj, a in rep["byProject"].items():
            lines.append(
                f"    - {proj}: {a['sessions']} 会话 / {a['avgStepsToFirstWrite']} 步首写 / "
                f"{a['avgRepeatedReads']} 次重复读"
            )
    lines.append(f"  {rep['note']}")
    return "\n".join(lines)
