"""Repository 协议的 SQLite 实现（P2）。

把 `store.Store` 包一层，对外只暴露 `engine.contracts.Repository` 的 6 个方法。
**行为零变化**：每个方法都是对现有方法的直接转调，唯一的翻译是 namespace ↔ project
（引擎层不绑定宿主的"项目=目录"语义）。

为什么需要这一层：
- 给通用引擎一个稳定的存储面——换后端时只改这个文件；
- 让 `cleanup` / `pages` / `insight` 将来能依赖协议，而不是 `store._conn`；
- 重放测试（scripts/selftest_replay.py）保证这一层包完之后行为逐字节不变。
"""
from __future__ import annotations

from typing import Any

from engine.contracts import MemoryRecord


def to_record(memory: Any) -> MemoryRecord | None:
    """store.Memory → MemoryRecord。None 透传。"""
    if memory is None:
        return None
    return MemoryRecord(
        id=int(memory.id),
        namespace=memory.project,
        kind=memory.kind,
        title=memory.title,
        content=memory.content,
        status=memory.status,
        created_at=memory.created_at,
        updated_at=memory.updated_at or memory.created_at,
        session_id=memory.session_id,
        meta=dict(memory.meta or {}),
        score=float(getattr(memory, "score", 0.0) or 0.0),
        use_count=int(getattr(memory, "use_count", 0) or 0),
        last_used_at=getattr(memory, "last_used_at", None),
    )


class SqliteRepository:
    """`engine.contracts.Repository` 的 SQLite 实现。"""

    def __init__(self, store: Any) -> None:
        self._store = store

    # -- 写 ----------------------------------------------------------
    def save(
        self,
        namespace: str,
        kind: str,
        title: str,
        content: str,
        *,
        meta: dict[str, Any] | None = None,
        session_id: str | None = None,
        ts: str | None = None,
        source: str = "manual",
        revive: bool = False,
    ) -> tuple[int, bool]:
        return self._store.remember(
            namespace, kind, title, content,
            session_id=session_id, ts=ts, meta=meta, source=source, revive=revive,
        )

    def supersede(self, namespace: str, kind: str, *, keep_id: int | None = None) -> int:
        return self._store.supersede_kind(namespace, kind, keep_id=keep_id)

    # -- 读 ----------------------------------------------------------
    def get(self, memory_id: int) -> MemoryRecord | None:
        return to_record(self._store.get_memory(int(memory_id)))

    def search(
        self,
        namespace: str | None,
        query: str,
        *,
        limit: int = 8,
        kinds: list[str] | None = None,
        include_superseded: bool = False,
    ) -> list[MemoryRecord]:
        rows = self._store.recall(
            query,
            project=namespace,
            limit=limit,
            kinds=kinds,
            include_superseded=include_superseded,
        )
        return [to_record(m) for m in rows if m is not None]  # type: ignore[misc]

    def recent(self, namespace: str | None = None, *, limit: int = 20) -> list[MemoryRecord]:
        return [to_record(m) for m in self._store.recent(namespace, limit)]  # type: ignore[misc]

    def scan(
        self,
        *,
        kinds: list[str] | None = None,
        status: str | None = "active",
        namespace: str | None = None,
    ) -> list[MemoryRecord]:
        """按 kind/status 列出记忆。生命周期规则（cleanup）用它，不做相关性排序。"""
        from store import Memory

        sql = "SELECT * FROM memories WHERE 1=1"
        params: list[Any] = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if namespace:
            sql += " AND project = ?"
            params.append(self._store.normalize_project(namespace))
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            params.extend(kinds)
        sql += " ORDER BY project, id DESC"
        with self._store._lock:
            rows = self._store._conn.execute(sql, params).fetchall()
        return [to_record(Memory.from_row(r)) for r in rows]  # type: ignore[misc]

    def stats(self) -> dict[str, Any]:
        return self._store.stats()


def for_store(store: Any) -> SqliteRepository:
    """给现有 Store 套一个 Repository 视图。"""
    return SqliteRepository(store)
