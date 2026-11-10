"""Repository 适配器自测（P2）。

验证两件事：
1. `SqliteRepository` 满足 `engine.contracts.Repository` 协议；
2. 它的结果与直接调 `store.Store` **完全一致**——适配器只做翻译，不改行为。

跑法：.venv\\Scripts\\python.exe scripts\\selftest_repository.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

_ISO = Path(tempfile.mkdtemp(prefix="dshrag-repo-"))
os.environ["DSH_RAG_PROJECTS_ROOT"] = str(_ISO / "projects")
os.environ["DSH_RAG_HANDOFFS_ROOT"] = str(_ISO / "handoffs")
os.environ["DSH_RAG_EMBED"] = "off"

from adapters.repository import SqliteRepository, for_store, to_record  # noqa: E402
from engine.contracts import Repository  # noqa: E402
from store import Store  # noqa: E402

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


def main() -> int:
    s = Store(_ISO / "t.db")
    repo = for_store(s)

    print("== 协议 ==")
    check("SqliteRepository 满足 Repository", isinstance(repo, Repository))
    check("to_record(None) 透传 None", to_record(None) is None)

    print("\n== 写读一致 ==")
    mid, created = repo.save("demo", "decision", "选 SQLite", "不引入向量数据库")
    check("save 返回 (id, created)", mid > 0 and created is True, f"{mid},{created}")
    again, created2 = repo.save("demo", "decision", "选 SQLite", "不引入向量数据库")
    check("同内容幂等（与 Store 一致）", again == mid and created2 is False)

    rec = repo.get(mid)
    check("get 返回 MemoryRecord", rec is not None and rec.namespace == "demo")
    check("字段与 Store 一致", rec.title == s.get_memory(mid).title and rec.kind == "decision")

    print("\n== namespace 归一 ==")
    hits = repo.search("DEMO", "SQLite")
    check("大小写不同的 namespace 也能命中", len(hits) == 1, str(len(hits)))
    store_hits = s.recall("SQLite", project="demo")
    check("与 Store 直接检索结果一致", [h.id for h in hits] == [m.id for m in store_hits])

    print("\n== 生命周期 ==")
    repo.save("demo", "handoff", "交接卡 A", "第一版")
    repo.save("demo", "handoff", "交接卡 B", "第二版")
    n = repo.supersede("demo", "handoff")
    check("supersede 作废该 kind 的生效记录", n == 2, str(n))
    check("作废后 recent 不含 handoff",
          all(r.kind != "handoff" for r in repo.recent("demo")))

    keep = repo.save("demo", "handoff", "交接卡 C", "第三版")[0]
    n2 = repo.supersede("demo", "handoff", keep_id=keep)
    check("keep_id 保护指定记录", n2 == 0, str(n2))

    print("\n== 统计 ==")
    a = repo.stats()
    b = s.stats()
    for key in ("total", "active", "vectors", "projects"):
        check(f"stats.{key} 与 Store 一致", a[key] == b[key], f"{a[key]} vs {b[key]}")

    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
