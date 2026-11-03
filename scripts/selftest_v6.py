"""v0.6 自测：增量迁移、使用计数、草稿流转、清理、体检、接力度量。

跑法：.venv\\Scripts\\python.exe scripts\\selftest_v6.py
全部用临时库。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  ✓", name)
    else:
        FAIL += 1
        print("  ✗", name, ("| " + detail) if detail else "")


def tmp_db() -> Path:
    return Path(tempfile.mkdtemp(prefix="dshrag-v6-")) / "t.db"


V5_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE projects (name TEXT PRIMARY KEY, dir_path TEXT, first_seen TEXT NOT NULL,
  updated_at TEXT NOT NULL, archived_at TEXT);
CREATE TABLE memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, kind TEXT NOT NULL,
  title TEXT NOT NULL, content TEXT NOT NULL, meta_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'active', superseded_by INTEGER, session_id TEXT,
  source TEXT NOT NULL DEFAULT 'manual', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  content_hash TEXT NOT NULL UNIQUE);
CREATE VIRTUAL TABLE memories_fts USING fts5(title_tok, content_tok, tokenize='unicode61');
CREATE TABLE vectors (memory_id INTEGER PRIMARY KEY, model TEXT NOT NULL,
  dim INTEGER NOT NULL, vec BLOB NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE sessions (session_id TEXT PRIMARY KEY, project TEXT, title TEXT, cwd TEXT,
  origin TEXT, started_at TEXT, last_seen_at TEXT, archived_at TEXT, memory_id INTEGER,
  compact_count INTEGER NOT NULL DEFAULT 0, detail_json TEXT NOT NULL DEFAULT '{}');
PRAGMA user_version=5;
"""


def test_v5_to_v6() -> None:
    print("== v5 → v6 增量迁移 ==")
    db = tmp_db()
    con = sqlite3.connect(db)
    con.executescript(V5_SCHEMA)
    con.execute(
        "INSERT INTO memories(id,project,kind,title,content,created_at,updated_at,content_hash)"
        " VALUES (1,'p','decision','旧决策','正文','2026-01-01T00:00:00+08:00',"
        "'2026-01-01T00:00:00+08:00','h1')"
    )
    con.commit()
    con.close()

    from store import Store

    s = Store(db)
    check("版本升到 6", s.migration["to"] == 6, str(s.migration))
    cols = [r[1] for r in s._conn.execute("PRAGMA table_info(memories)")]
    check("新增 use_count", "use_count" in cols)
    check("旧数据保留", s.stats()["total"] == 1)


def test_usage() -> None:
    print("\n== 使用计数 ==")
    from store import Store

    s = Store(tmp_db())
    mid, _ = s.remember("p", "decision", "检索测试", "内容里有 缓存 这个词")
    s.recall("缓存", project="p")
    m = s.get_memory(mid)
    check("recall 会累加 use_count", m.use_count >= 1, str(m.use_count))
    check("last_used_at 有值", bool(m.last_used_at))


def test_draft_flow() -> None:
    print("\n== 草稿流转 ==")
    from store import Store

    s = Store(tmp_db())
    s.checkpoint_put("p", {"baseline": "基线内容", "goal": "旧目标"})
    did = s.checkpoint_draft("p", {"goal": "新目标", "files": [{"path": "a.py"}]})
    check("草稿写入", did > 0)
    check("草稿不进检索", not any(m.id == did for m in s.recall("新目标", project="p")))
    check("草稿不进 pack", "新目标" not in s.handoff_pack("p")["text"])
    check("草稿能被读到", (s.get_draft("p") or None) is not None)

    s.checkpoint_draft("p", {"goal": "更新的目标"})
    drafts = s._conn.execute(
        "SELECT COUNT(*) FROM memories WHERE project='p' AND kind='checkpoint' AND status='draft'"
    ).fetchone()[0]
    check("同项目只留一份草稿", drafts == 1)

    res = s.promote_draft("p")
    check("采纳成功", res is not None)
    cp = s.get_checkpoint("p")
    check("采纳后目标更新", cp.meta.get("goal") == "更新的目标")
    check("采纳后草稿清空", s.get_draft("p") is None)
    check("基线被保留", "基线内容" in (cp.meta.get("baseline") or ""))


def test_cleanup() -> None:
    print("\n== 清理 ==")
    from datetime import datetime, timedelta

    import cleanup
    from store import Store

    s = Store(tmp_db())
    old = (datetime.now().astimezone() - timedelta(days=40)).isoformat(timespec="seconds")
    s.checkpoint_put("p", {"baseline": "b"})
    s.remember("p", "session", "旧会话摘要", "很久以前的会话内容" * 5, ts=old)
    s.remember("p", "decision", "旧决策", "决策不该被会话规则清掉", ts=old)

    plan = cleanup.plan(s)
    reasons = {c["reason"] for c in plan["candidates"]}
    check("过期会话被标出", "session-stale" in reasons, str(reasons))
    check("决策不在候选里", all(c["kind"] != "decision" for c in plan["candidates"]))

    done = cleanup.apply(s, plan)
    check("执行后有备份", bool(done["backup"]) and Path(done["backup"]).exists())
    row = s._conn.execute(
        "SELECT id, status FROM memories WHERE title='旧会话摘要'").fetchone()
    check("软删除为 archived", row["status"] == "archived")
    mid = int(row["id"])
    # 关键词是共用的（"会话"在别处也出现），所以只断言"这一条"检索不到
    check("归档后检索不到", all(m.id != mid for m in s.recall("很久以前的会话", project="p")))

    check("可以恢复", s.restore_memory(int(mid)))
    check("恢复后又是 active", s._conn.execute(
        "SELECT status FROM memories WHERE id=?", (int(mid),)).fetchone()["status"] == "active")


def test_doctor_and_relay() -> None:
    print("\n== 体检 + 接力度量 ==")
    import doctor
    import insight
    from store import Store

    s = Store(tmp_db())
    s.remember("p", "decision", "x", "y")
    s.upsert_session("s1", project="p", started_at="2026-11-03T10:00:00+08:00",
                     detail={"metrics": {"stepsToFirstWrite": 3, "toolCalls": 20,
                                         "repeatedReads": 4, "writeCalls": 2},
                             "packVersion": "abcdef12"})
    s.upsert_session("s2", project="p", started_at="2026-11-02T10:00:00+08:00",
                     detail={"metrics": {"stepsToFirstWrite": 9, "toolCalls": 30,
                                         "repeatedReads": 12, "writeCalls": 1}})

    rep = doctor.run(s, session_probe=1)
    check("体检有结果", len(rep["checks"]) >= 6)
    check("体检无硬错误", rep["errors"] == 0, json.dumps(rep["checks"], ensure_ascii=False)[:200])

    relay = insight.relay_report(s)
    check("接力度量读到 2 个会话", relay["sessions"] == 2, str(relay["sessions"]))
    check("读过 pack 的样本被单列", relay["withPack"]["sessions"] == 1)
    check("首写步数均值可算", relay["overall"]["avgStepsToFirstWrite"] == 6.0,
          str(relay["overall"]))


def main() -> int:
    test_v5_to_v6()
    test_usage()
    test_draft_flow()
    test_cleanup()
    test_doctor_and_relay()
    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
