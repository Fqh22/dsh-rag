"""契约自测（P0）：证明接口能闭合，且现有实现能对上。

不做真实 IO：用 FakeRepository / FakeSessionSource 跑一遍最小流程，再检查
store / archive / card 的方法映射还在——契约与实现对不上时这里先红，而不是等
P2 拆层拆到一半才发现。

跑法：.venv\\Scripts\\python.exe scripts\\selftest_contracts.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from engine.contracts import (  # noqa: E402
    MemoryRecord,
    Pack,
    PackFormat,
    Repository,
    SessionRef,
    SessionSource,
    SessionTurn,
)

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  ✓", name)
    else:
        FAIL += 1
        print("  ✗", name, "|", detail)


class FakeRepository:
    """内存实现：只为验证协议闭合，不追求行为完整。"""

    def __init__(self) -> None:
        self._rows: dict[int, MemoryRecord] = {}
        self._seq = 0

    def save(self, namespace, kind, title, content, *, meta=None,
             session_id=None, ts=None, source="manual", revive=False):
        for rid, row in self._rows.items():
            if (row.namespace, row.kind, row.title, row.content) == (namespace, kind, title, content):
                if revive:
                    row.status = "active"
                return rid, False
        self._seq += 1
        self._rows[self._seq] = MemoryRecord(
            id=self._seq, namespace=namespace, kind=kind, title=title,
            content=content, meta=dict(meta or {}), session_id=session_id,
            created_at=ts or "", updated_at=ts or "",
        )
        return self._seq, True

    def get(self, memory_id):
        return self._rows.get(int(memory_id))

    def search(self, namespace, query, *, limit=8, kinds=None, include_superseded=False):
        rows = [
            r for r in self._rows.values()
            if (namespace is None or r.namespace == namespace)
            and (include_superseded or r.status == "active")
            and (not kinds or r.kind in kinds)
            and (query in r.title or query in r.content)
        ]
        return rows[:limit]

    def supersede(self, namespace, kind, *, keep_id=None):
        n = 0
        for rid, row in self._rows.items():
            if row.namespace == namespace and row.kind == kind and rid != keep_id and row.status == "active":
                row.status = "superseded"
                n += 1
        return n

    def recent(self, namespace=None, *, limit=20):
        rows = [r for r in self._rows.values()
                if (namespace is None or r.namespace == namespace) and r.status == "active"]
        return rows[-limit:]

    def stats(self):
        return {"total": len(self._rows), "active": sum(1 for r in self._rows.values() if r.status == "active")}


class FakeSessionSource:
    name = "fake"

    def scan(self, since=None):
        return [SessionRef(id="s1", path="/dev/null", mtime=0.0, namespace="demo")]

    def read(self, ref):
        yield SessionTurn(role="user", text="帮我看看这个项目")
        yield SessionTurn(role="tool", text="read store.py", kind="tool/call")
        yield SessionTurn(role="assistant", text="看完了")


class FakePackFormat:
    def render(self, namespace, *, budget_tokens=3000, with_meta=False):
        import card

        p = card.build_pack(
            namespace,
            card="### 当前目标\n验证契约",
            checkpoint={"baseline": "基线"},
            facts=[],
            budget_tokens=budget_tokens,
            with_meta=with_meta,
        )
        return Pack(
            namespace=namespace, text=p["text"], version=p["version"],
            est_tokens=p["estTokens"], chars=p["chars"], truncated=p["truncated"],
            sections=p["sections"], has_handoff=True, has_checkpoint=True,
        )


def main() -> int:
    print("== 协议可实现 ==")
    repo = FakeRepository()
    src = FakeSessionSource()
    fmt = FakePackFormat()
    check("FakeRepository 满足 Repository", isinstance(repo, Repository))
    check("FakeSessionSource 满足 SessionSource", isinstance(src, SessionSource))
    check("FakePackFormat 满足 PackFormat", isinstance(fmt, PackFormat))

    print("\n== 最小流程：save → get → search → supersede → recent ==")
    mid, created = repo.save("demo", "decision", "选 SQLite", "不引入向量数据库")
    check("save 返回 (id, created)", mid > 0 and created is True, f"{mid},{created}")
    again, created2 = repo.save("demo", "decision", "选 SQLite", "不引入向量数据库")
    check("同内容幂等", again == mid and created2 is False)
    check("get 能读回", repo.get(mid) is not None and repo.get(mid).title == "选 SQLite")
    hits = repo.search("demo", "SQLite")
    check("search 命中", len(hits) == 1 and hits[0].id == mid, str(len(hits)))
    check("search 按 namespace 隔离", repo.search("other", "SQLite") == [])
    repo.save("demo", "handoff", "交接卡", "当前进展")
    n = repo.supersede("demo", "handoff")
    check("supersede 生效", n == 1)
    check("recent 不含已失效", all(r.status == "active" for r in repo.recent("demo")))

    print("\n== 会话源迭代 ==")
    refs = src.scan()
    turns = list(src.read(refs[0]))
    check("scan 返回 SessionRef", len(refs) == 1 and isinstance(refs[0], SessionRef))
    check("read 产出 SessionTurn", len(turns) == 3 and turns[0].role == "user")
    check("tool 轮保留 kind", turns[1].kind == "tool/call")

    print("\n== pack 契约 ==")
    pack = fmt.render("demo", budget_tokens=3000)
    check("render 返回 Pack", isinstance(pack, Pack) and pack.est_tokens > 0)
    check("同一输入逐字节相同", fmt.render("demo").text == pack.text)
    check("基线在最前", pack.text.index("基线") < pack.text.index("当前状态"))

    print("\n== 与现有实现的映射（改名就红）==")
    import archive
    import card
    from store import Store

    store_methods = ["remember", "get_memory", "recall", "recent", "stats",
                     "supersede_session_memories"]
    missing = [m for m in store_methods if not hasattr(Store, m)]
    check("Store 提供映射表里的方法", not missing, str(missing))
    check("archive 提供 iter_session_files / parse_session",
          hasattr(archive, "iter_session_files") and hasattr(archive, "parse_session"))
    check("card 提供 build_pack", hasattr(card, "build_pack"))

    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
