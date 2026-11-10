"""清理：把「价值随时间衰减」和「价值本来就不高」的信息收起来。

**默认不删，只归档。** `status='archived'` 的记录检索、pack、recent 全都看不到，
但行还在、能 restore。真要腾空间才用 `--purge` 硬删。

## 清理的判断依据（按可信度排序）

1. **时效**：会话摘要是最快贬值的东西。30 天后如果项目已经有 checkpoint，
   它就只是"当时聊过什么"的考古材料，不该再占检索和 pack 的位置。
2. **结构位置**：失效的交接卡、被取代的 checkpoint 是纯历史链，每个项目留最近几张就够。
3. **使用情况**：`use_count` 记录了每条记忆被检索命中过几次。
   "写了 60 天、一次都没被召回"是比年龄更硬的证据——但它不能单独作为删除理由，
   所以这里只用来生成提醒，不用来自动作废。
4. **孤儿**：目录已经不存在、且很久没更新的项目，只在报告里列出，等人工决定。

阈值全部可改，且 `--dry-run` 是默认行为。
"""
from __future__ import annotations

import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_POLICY: dict[str, Any] = {
    "session_days": 30,     # 项目已有 checkpoint 时，session 摘要的保留天数
    "hard_days": 90,        # 不管有没有 checkpoint，超过这个天数都归档
    "keep_handoffs": 3,     # 每个项目保留最近几张失效交接卡
    "keep_superseded": 5,   # 每个项目保留最近几条失效 checkpoint
    "draft_days": 7,        # 草稿超过这么多天没人确认就作废
    "purge_days": 180,      # archived 超过这么多天，允许 --purge 硬删
}

# 这些 kind 是"支撑当前状态"的，不参与时效清理
PROTECTED_KINDS = ("handoff", "checkpoint", "baseline")


def _age_days(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return (datetime.now().astimezone() - datetime.fromisoformat(iso)).total_seconds() / 86400
    except ValueError:
        return None


def _has_checkpoint(conn: sqlite3.Connection, project: str) -> bool:
    r = conn.execute(
        "SELECT 1 FROM memories WHERE project=? AND kind='checkpoint' AND status='active' LIMIT 1",
        (project,),
    ).fetchone()
    return bool(r)


def plan(store=None, *, repo=None, **overrides: Any) -> dict[str, Any]:
    """算出候选，不写库。返回 {candidates, stats}。

    只读路径走 Repository 协议（P2）：不再直接碰 `store._conn`，生命周期规则
    因此可以脱离 SQLite 复用——`repo=` 直接传任何符合协议的实现即可，
    规则测试（scripts/selftest_cleanup_rules.py）就是这么做的。
    写入路径（apply / purge）仍在存储层内部完成。
    """
    from adapters.repository import for_store

    if repo is None:
        if store is None:
            raise TypeError("plan() 需要 store 或 repo 之一")
        repo = for_store(store)

    policy = dict(DEFAULT_POLICY)
    policy.update({k: v for k, v in overrides.items() if v is not None})
    candidates: list[dict[str, Any]] = []

    def add(rec, reason: str, detail: str) -> None:
        candidates.append({
            "id": int(rec.id),
            "project": rec.namespace,
            "kind": rec.kind,
            "status": rec.status,
            "title": rec.title,
            "chars": len(rec.content or ""),
            "created_at": rec.created_at,
            "ageDays": round(_age_days(rec.created_at) or 0, 1),
            "useCount": int(rec.use_count or 0),
            "reason": reason,
            "detail": detail,
        })

    # 1) 会话摘要：先用"项目已有 checkpoint"降级，再用硬时限兜底
    cp_projects = {r.namespace for r in repo.scan(kinds=["checkpoint"], status="active")}
    for r in repo.scan(kinds=["session"], status="active"):
        age = _age_days(r.created_at) or 0
        if age >= policy["hard_days"]:
            add(r, "session-old", f"{age:.0f} 天前的会话摘要")
        elif age >= policy["session_days"] and r.namespace in cp_projects:
            add(r, "session-stale", f"{age:.0f} 天前，且项目已有 checkpoint")

    # 2) 失效交接卡：每项目只留最近 N 张
    seen: dict[str, int] = {}
    for r in repo.scan(kinds=["handoff"], status="superseded"):
        seen[r.namespace] = seen.get(r.namespace, 0) + 1
        if seen[r.namespace] > policy["keep_handoffs"]:
            add(r, "handoff-history",
                f"第 {seen[r.namespace]} 张失效卡（保留最近 {policy['keep_handoffs']} 张）")

    # 3) 失效 checkpoint：每项目只留最近 N 条
    seen = {}
    for r in repo.scan(kinds=["checkpoint"], status="superseded"):
        seen[r.namespace] = seen.get(r.namespace, 0) + 1
        if seen[r.namespace] > policy["keep_superseded"]:
            add(r, "checkpoint-history", f"第 {seen[r.namespace]} 条失效状态")

    # 4) 过期草稿：写了没人确认
    for r in repo.scan(kinds=["checkpoint"], status="draft"):
        age = _age_days(r.created_at) or 0
        if age >= policy["draft_days"]:
            add(r, "draft-expired", f"{age:.0f} 天未确认的草稿")

    # 5) 提醒但不自动动：长期零命中的普通记忆
    cutoff = _iso_days_ago(policy["hard_days"])
    never = [
        r for r in repo.scan(status="active")
        if r.kind not in PROTECTED_KINDS and r.use_count == 0 and r.created_at < cutoff
    ]

    # 去重（同一条可能同时命中多条规则，保留第一条）
    uniq: dict[int, dict[str, Any]] = {}
    for c in candidates:
        uniq.setdefault(c["id"], c)
    items = sorted(uniq.values(), key=lambda c: (-c["chars"], c["id"]))

    by_reason: dict[str, int] = {}
    for c in items:
        by_reason[c["reason"]] = by_reason.get(c["reason"], 0) + 1

    return {
        "policy": policy,
        "candidates": items,
        "stats": {
            "count": len(items),
            "chars": sum(c["chars"] for c in items),
            "byReason": by_reason,
        },
        "neverUsedOld": {
            "count": len(never),
            "chars": sum(len(r.content or "") for r in never),
            "note": f"{policy['hard_days']} 天以上、一次没被检索命中过的普通记忆（只提醒，不自动归档）",
        },
    }


def _iso_days_ago(days: float) -> str:
    from datetime import timedelta

    return (datetime.now().astimezone() - timedelta(days=days)).isoformat(timespec="seconds")


def backup(store) -> str:
    """动手前先拷一份整库。用 sqlite backup API，WAL 会一起合并。"""
    ts = time.strftime("%Y%m%d-%H%M%S")
    dest = store.db_path.parent / "backup" / f"cleanup-{ts}.db"
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(store.db_path))
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return str(dest)


def apply(store, result: dict[str, Any], drop_vectors: bool = True) -> dict[str, Any]:
    """执行 plan 的结果。先备份，再软删除。"""
    items = result.get("candidates") or []
    if not items:
        return {"archived": 0, "backup": None}

    path = backup(store)
    ids = [c["id"] for c in items]
    draft_ids = [c["id"] for c in items if c["reason"] == "draft-expired"]
    normal_ids = [i for i in ids if i not in set(draft_ids)]

    with store._lock:
        conn = store._conn
        if normal_ids:
            ph = ",".join("?" * len(normal_ids))
            conn.execute(
                f"UPDATE memories SET status='archived', updated_at=? WHERE id IN ({ph})",
                (_iso_now(), *normal_ids),
            )
            if drop_vectors:
                conn.execute(f"DELETE FROM vectors WHERE memory_id IN ({ph})", normal_ids)
        if draft_ids:
            ph = ",".join("?" * len(draft_ids))
            conn.execute(
                f"UPDATE memories SET status='superseded', updated_at=? WHERE id IN ({ph})",
                (_iso_now(), *draft_ids),
            )
        conn.commit()
    store._invalidate_vec_cache()
    return {"archived": len(normal_ids), "draftsDropped": len(draft_ids), "backup": path}


def purge(store, days: int | None = None) -> dict[str, Any]:
    """硬删 archived 超过 N 天的记录（连同 FTS 与向量）。不可恢复，先备份。"""
    days = DEFAULT_POLICY["purge_days"] if days is None else int(days)
    cutoff = _iso_days_ago(days)
    with store._lock:
        rows = store._conn.execute(
            "SELECT id FROM memories WHERE status='archived' AND updated_at < ?", (cutoff,)
        ).fetchall()
    ids = [int(r["id"]) for r in rows]
    if not ids:
        return {"deleted": 0, "backup": None}

    path = backup(store)
    with store._lock:
        conn = store._conn
        ph = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM memories_fts WHERE rowid IN ({ph})", ids)
        conn.execute(f"DELETE FROM vectors WHERE memory_id IN ({ph})", ids)
        # 会话台账里还指着这条记忆的话一并断掉，否则 memory_id 指向已删除的行
        conn.execute(f"UPDATE sessions SET memory_id=NULL WHERE memory_id IN ({ph})", ids)
        conn.execute(f"DELETE FROM memories WHERE id IN ({ph})", ids)
        conn.commit()
    store._invalidate_vec_cache()
    return {"deleted": len(ids), "backup": path}


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def format_plan(result: dict[str, Any]) -> str:
    st = result["stats"]
    lines = [
        f"清理计划：{st['count']} 条候选，约 {st['chars'] / 1024:.1f} KB",
        "（默认只归档不删除；加 --apply 执行，--purge 才硬删）",
    ]
    if st["byReason"]:
        lines.append("")
        for reason, n in sorted(st["byReason"].items(), key=lambda x: -x[1]):
            lines.append(f"  · {reason}: {n} 条")
    for c in result["candidates"][:15]:
        lines.append(
            f"  [{c['reason']}] {c['project']} / {c['kind']} #{c['id']} "
            f"{c['title'][:32]}（{c['chars']} 字，{c['ageDays']} 天，命中 {c['useCount']} 次）"
        )
    if len(result["candidates"]) > 15:
        lines.append(f"  …另有 {len(result['candidates']) - 15} 条")
    nu = result.get("neverUsedOld") or {}
    if nu.get("count"):
        lines.append("")
        lines.append(f"提醒：{nu['note']} —— {nu['count']} 条 / {nu['chars'] / 1024:.1f} KB")
    return "\n".join(lines)
