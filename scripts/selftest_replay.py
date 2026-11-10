"""行为重放测试（P2 安全网）。

给定同样的种子库和调用序列，输出的 JSON 必须逐字节一致。
重构 store / 换 Repository 实现 / 拆层时，这个测试是「行为没变」的度量——
`selftest_*` 只证明「功能还在」，这里证明「结果一模一样」。

用法：
    .venv\\Scripts\\python.exe scripts\\selftest_replay.py            # 比对 golden
    .venv\\Scripts\\python.exe scripts\\selftest_replay.py --update   # 重写 golden

注意：只有**确认行为变更**时才用 --update，并在提交信息里写清为什么变。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

_ISO = Path(tempfile.mkdtemp(prefix="dshrag-replay-"))
os.environ["DSH_RAG_PROJECTS_ROOT"] = str(_ISO / "projects")
os.environ["DSH_RAG_HANDOFFS_ROOT"] = str(_ISO / "handoffs")
os.environ["DSH_RAG_EMBED"] = "off"

GOLDEN = ROOT / "tests" / "golden" / "replay.json"

# 这些字段是时间戳/运行时路径，重放时必须抹掉
TS_KEYS = {"created_at", "updated_at", "last_used_at", "generatedAt", "db", "backup"}
TS = "2026-01-01T00:00:00+08:00"


def norm(obj):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in TS_KEYS:
                out[k] = "<ts>"
            elif k == "ageDays":
                out[k] = "<age>"
            else:
                out[k] = norm(v)
        return out
    if isinstance(obj, list):
        return [norm(x) for x in obj]
    return obj


def build():
    """种一份固定数据，跑固定调用序列，返回规范化后的结果。"""
    import cleanup
    import card
    from store import Store

    for stale in _ISO.glob("replay.db*"):      # 每次从干净库开始，序列才可重复
        try:
            stale.unlink()
        except OSError:
            pass

    s = Store(_ISO / "replay.db")
    out: dict = {}

    # 1. 写入（固定时间，保证 age 之外都稳定）
    ids = {}
    for key, (kind, title, body) in {
        "decision": ("decision", "选 SQLite", "不引入向量数据库，用 SQLite + numpy。"),
        "fact": ("fact", "项目位置", "项目都在 ~/Documents/Projects 下。"),
        "pitfall": ("pitfall", "FTS5 不切中文", "unicode61 不切中文，必须 bigram。"),
    }.items():
        mid, created = s.remember("demo", kind, title, body, ts=TS)
        ids[key] = mid
        out[f"remember:{key}"] = {"id": mid, "created": created}

    dup_id, dup_created = s.remember("demo", "decision", "选 SQLite",
                                     "不引入向量数据库，用 SQLite + numpy。", ts=TS)
    out["remember:dup"] = {"same_id": dup_id == ids["decision"], "created": dup_created}

    # 2. 交接卡 → checkpoint → pack
    s.put_handoff("demo", "第一版交接卡：存储层已就位。")
    s.checkpoint_put("demo", {
        "baseline": "技术栈：Python + SQLite",
        "goal": "验证重放",
        "done": ["存储层", "FTS"],
        "next": ["向量检索"],
    })

    cp = s.get_checkpoint("demo")
    out["checkpoint"] = {
        "baseline": cp.meta.get("baseline"),
        "goal": cp.meta.get("goal"),
        "done": cp.meta.get("done"),
        "next": cp.meta.get("next"),
    }

    pack = s.handoff_pack("demo", budget_tokens=3000)
    out["pack"] = {
        "version": pack["version"],
        "estTokens": pack["estTokens"],
        "truncated": pack["truncated"],
        "sections": pack["sections"],
        "hasHandoff": pack["hasHandoff"],
        "checkpoint": pack["checkpoint"],
        "factCount": pack["factCount"],
        "text": pack["text"],
    }

    # 3. 检索（纯关键词，确定性）
    for q in ("向量数据库", "FTS", "项目位置"):
        hits = s.recall(q, project="demo", limit=5)
        out[f"recall:{q}"] = [
            {"id": m.id, "kind": m.kind, "title": m.title, "score": round(m.score, 5)}
            for m in hits
        ]

    # 4. 台账 / 统计
    out["recent"] = [
        {"id": m.id, "kind": m.kind, "title": m.title}
        for m in s.recent("demo", limit=10)
    ]
    st = s.stats()
    out["stats"] = {
        "total": st["total"],
        "active": st["active"],
        "vectors": st["vectors"],
        "missing_vectors": st["missing_vectors"],
        "projects": st["projects"],
        "sessions": st["sessions"],
        "kinds": st["kinds"],
        "schema_version": st["schema"]["version"],
    }

    # 5. 生命周期：归档 → 恢复
    s.archive_memories([ids["pitfall"]])
    out["after_archive"] = {
        "active": s.stats()["active"],
        "recall_hits": len(s.recall("FTS", project="demo")),
    }
    restored = s.restore_memory(ids["pitfall"])
    out["after_restore"] = {
        "restored": restored,
        "recall_hits": len(s.recall("FTS", project="demo")),
    }

    # 6. 清理计划（只取 reason，避开随时间变化的 ageDays）
    plan = cleanup.plan(s)
    out["cleanup_reasons"] = sorted({c["reason"] for c in plan["candidates"]})

    return norm(out)


def main() -> int:
    update = "--update" in sys.argv
    try:
        got = build()
    except Exception as exc:  # noqa: BLE001
        print(f"重放失败：{type(exc).__name__}: {exc}")
        return 1

    text = json.dumps(got, ensure_ascii=False, indent=1, sort_keys=True)

    if update or not GOLDEN.exists():
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(text + "\n", encoding="utf-8")
        print(f"golden 已写入：{GOLDEN.relative_to(ROOT)}（{len(got)} 个步骤）")
        return 0

    want = json.loads(GOLDEN.read_text(encoding="utf-8"))
    if want == got:
        print(f"重放一致：{len(got)} 个步骤，与 golden 逐字节相同")
        return 0

    diff = [k for k in sorted(set(want) | set(got)) if want.get(k) != got.get(k)]
    print(f"重放不一致：{len(diff)} 个步骤有差异")
    for k in diff[:10]:
        print(f"  ✗ {k}")
        print("    期望:", json.dumps(want.get(k), ensure_ascii=False)[:300])
        print("    实际:", json.dumps(got.get(k), ensure_ascii=False)[:300])
    return 1


if __name__ == "__main__":
    sys.exit(main())
