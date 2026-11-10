"""清理规则自测（P2 收尾）。

为什么单独写：`cleanup.plan` 的规则以前全埋在 SQL 里，只能靠"在一个真库上跑一遍、
看看候选大概长什么样"来验证。改成走 Repository 协议之后，规则可以用 Fake 逐条钉死——
这才是把「协议化」从结构收益变成可验证收益的那一步。

不碰 SQLite，也不需要临时库：`plan(repo=FakeRepo(...))` 直接传实现。

跑法：.venv\\Scripts\\python.exe scripts\\selftest_cleanup_rules.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import cleanup  # noqa: E402
from engine.contracts import MemoryRecord  # noqa: E402

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


def days_ago(n: float) -> str:
    return (datetime.now().astimezone() - timedelta(days=n)).isoformat(timespec="seconds")


def rec(mid, ns, kind, status, created, *, use=0, content="内容"):
    return MemoryRecord(
        id=mid, namespace=ns, kind=kind, title=f"{kind}-{mid}", content=content,
        status=status, created_at=created, updated_at=created, use_count=use,
    )


class FakeRepo:
    """只实现 plan 用到的那一个方法；排序与 SqliteRepository 保持一致。"""

    def __init__(self, rows):
        self.rows = list(rows)

    def scan(self, *, kinds=None, status="active", namespace=None):
        out = [
            r for r in self.rows
            if (status is None or r.status == status)
            and (namespace is None or r.namespace == namespace)
            and (not kinds or r.kind in kinds)
        ]
        return sorted(out, key=lambda r: (r.namespace, -r.id))


def reasons_of(plan):
    counts: dict[str, int] = {}
    for c in plan["candidates"]:
        counts[c["reason"]] = counts.get(c["reason"], 0) + 1
    return counts


def base_rows():
    rows = [
        # p1 有生效 checkpoint；p2 没有
        rec(1, "p1", "checkpoint", "active", days_ago(1)),
        rec(2, "p1", "session", "active", days_ago(100)),   # session-old
        rec(3, "p1", "session", "active", days_ago(40)),    # session-stale（有 cp）
        rec(4, "p1", "session", "active", days_ago(1)),     # 太新
        rec(5, "p2", "session", "active", days_ago(40)),    # 无 cp 且未到 hard → 不候选
        rec(6, "p2", "session", "active", days_ago(100)),   # hard 兜底 → session-old
    ]
    # 5 张失效交接卡：默认保留 3 张，第 4、5 张入选
    for i, mid in enumerate(range(10, 15)):
        rows.append(rec(mid, "p1", "handoff", "superseded", days_ago(10 + i)))
    # 7 条失效 checkpoint：默认保留 5 条，第 6、7 条入选
    for i, mid in enumerate(range(20, 27)):
        rows.append(rec(mid, "p1", "checkpoint", "superseded", days_ago(20 + i)))
    # 草稿：10 天前过期，1 天前没过期
    rows.append(rec(30, "p1", "checkpoint", "draft", days_ago(10)))
    rows.append(rec(31, "p1", "checkpoint", "draft", days_ago(1)))
    # 零命中提示：只统计 active、非保护 kind、use_count=0、且超过 hard_days
    rows.append(rec(40, "p1", "fact", "active", days_ago(100), use=0))
    rows.append(rec(41, "p1", "fact", "active", days_ago(100), use=5))
    return rows


def main() -> int:
    print("== 默认政策下的候选 ==")
    plan = cleanup.plan(repo=FakeRepo(base_rows()))
    rs = reasons_of(plan)

    check("session 超过硬时限 → session-old", rs.get("session-old") == 2, str(rs))
    check("session 中等年龄且项目有 checkpoint → session-stale", rs.get("session-stale") == 1, str(rs))
    check("无 checkpoint 的中等年龄 session 不入选", all(
        c["id"] != 5 for c in plan["candidates"]))
    check("失效交接卡保留最近 3 张，其余入选", rs.get("handoff-history") == 2, str(rs))
    check("失效 checkpoint 保留最近 5 条", rs.get("checkpoint-history") == 2, str(rs))
    check("过期草稿入选（只选过期的）", rs.get("draft-expired") == 1, str(rs))
    check("候选总数符合预期", len(plan["candidates"]) == 8, str(len(plan["candidates"])))

    print("\n== 保留的是最新的那几张 ==")
    handoff_ids = sorted(c["id"] for c in plan["candidates"] if c["reason"] == "handoff-history")
    check("被清的是 id 较小的旧卡", handoff_ids == [10, 11], str(handoff_ids))
    cp_ids = sorted(c["id"] for c in plan["candidates"] if c["reason"] == "checkpoint-history")
    check("被清的是最旧的失效状态", cp_ids == [20, 21], str(cp_ids))

    print("\n== 零命中提示 ==")
    n = plan["neverUsedOld"]
    check("只统计超过 hard_days 且从未命中的", n["count"] == 3, str(n))
    check("命中的那条不算（use_count>0）", n["count"] != 4)

    print("\n== 边界：阈值前后 ==")
    rows = [
        rec(1, "p1", "checkpoint", "active", days_ago(1)),
        rec(2, "p1", "session", "active", days_ago(31)),    # > session_days(30)
        rec(3, "p1", "session", "active", days_ago(29)),    # < session_days
        rec(4, "p1", "session", "active", days_ago(91)),    # > hard_days(90)
        rec(5, "p1", "session", "active", days_ago(89)),    # < hard_days
    ]
    rs = reasons_of(cleanup.plan(repo=FakeRepo(rows)))
    ids = {c["id"]: c["reason"] for c in cleanup.plan(repo=FakeRepo(rows))["candidates"]}
    check("31 天 → session-stale", ids.get(2) == "session-stale", str(ids))
    check("29 天 → 不入选", 3 not in ids, str(ids))
    check("91 天 → session-old", ids.get(4) == "session-old", str(ids))
    check("89 天 → 走 stale 而不是 old", ids.get(5) == "session-stale", str(ids))

    print("\n== 政策可覆盖 ==")
    rs = reasons_of(cleanup.plan(repo=FakeRepo(base_rows()), session_days=10, hard_days=1000))
    # 只有「项目已有 checkpoint」的才会走 stale：p1 的两条入选，p2 的两条因为没 cp 不进
    check("调低 session_days 后 p1 的两条进 stale", rs.get("session-stale") == 2, str(rs))
    check("没有 checkpoint 的项目不会走 stale",
          all(c["project"] == "p1" for c in cleanup.plan(
              repo=FakeRepo(base_rows()), session_days=10, hard_days=1000)["candidates"]
              if c["reason"] == "session-stale"))
    check("调高 hard_days 后没有 session-old", rs.get("session-old") is None, str(rs))

    rs = reasons_of(cleanup.plan(repo=FakeRepo(base_rows()), keep_handoffs=5))
    check("keep_handoffs=5 时不再清失效卡", rs.get("handoff-history") is None, str(rs))

    print("\n== 保护规则 ==")
    rows = [
        rec(1, "p1", "handoff", "active", days_ago(500)),      # 生效卡永不因年龄被清
        rec(2, "p1", "checkpoint", "active", days_ago(500)),   # 生效状态同理
        rec(3, "p1", "baseline", "active", days_ago(500)),     # 基线同理
    ]
    plan = cleanup.plan(repo=FakeRepo(rows))
    check("生效的 handoff/checkpoint/baseline 不进候选", plan["candidates"] == [], str(plan["candidates"]))
    check("它们也不计入零命中提示", plan["neverUsedOld"]["count"] == 0, str(plan["neverUsedOld"]))

    print("\n== 参数校验 ==")
    try:
        cleanup.plan()
        check("不传 store/repo 会报错", False)
    except TypeError:
        check("不传 store/repo 会报错", True)

    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
