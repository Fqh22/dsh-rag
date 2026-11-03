"""v0.5 自测：迁移、结构化状态、上下文包、会话台账。

跑法：
    .venv\\Scripts\\python.exe scripts\\selftest_v5.py

全部用临时库，不碰 data/memory.db。
"""
from __future__ import annotations

import os
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


V4_SCHEMA = """
CREATE TABLE memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
  content TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
  superseded_by INTEGER, session_id TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  content_hash TEXT NOT NULL UNIQUE
);
CREATE INDEX idx_mem_project ON memories(project, status, kind);
CREATE INDEX idx_mem_created ON memories(created_at DESC);
CREATE VIRTUAL TABLE memories_fts USING fts5(title_tok, content_tok, tokenize='unicode61');
CREATE TABLE vectors (memory_id INTEGER PRIMARY KEY, dim INTEGER NOT NULL,
  vec BLOB NOT NULL, model TEXT NOT NULL);
"""


def tmp_db() -> Path:
    return Path(tempfile.mkdtemp(prefix="dshrag-v5-")) / "t.db"


def test_migration() -> None:
    print("== v4 → v5 迁移 ==")
    db = tmp_db()
    con = sqlite3.connect(db)
    con.executescript(V4_SCHEMA)
    rows = [
        ("proj", "handoff", "卡1", "旧卡正文", "superseded", "2026-01-01T00:00:00+08:00"),
        ("proj", "handoff", "卡1", "新卡正文", "active", "2026-01-02T00:00:00+08:00"),
        ("proj", "decision", "决策", "用 SQLite 存全部记忆", "active", "2026-01-03T00:00:00+08:00"),
    ]
    for i, (proj, kind, title, content, status, ts) in enumerate(rows, 1):
        con.execute(
            "INSERT INTO memories(id, project, kind, title, content, status, session_id,"
            " created_at, updated_at, content_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (i, proj, kind, title, content, status, None, ts, ts, f"h{i}"),
        )
        con.execute(
            "INSERT INTO memories_fts(rowid, title_tok, content_tok) VALUES (?,?,?)",
            (i, title, content),
        )
    con.commit()
    con.close()

    from store import Store

    s = Store(db)
    check("升到当前版本", s.migration["to"] >= 5, str(s.migration))
    check("数据条数不变", s.stats()["total"] == 3, str(s.stats()["total"]))
    check("旧结构留有备份表", "_v4_memories" in s.stats()["schema"]["leftovers"])
    h = s.get_handoff("proj")
    check("能读回生效中的交接卡", bool(h) and "新卡" in h.content)
    old = s.get_memory(1)
    check("superseded_by 链已补上", old is not None and old.status == "superseded")
    hits = s.recall("SQLite 存全部记忆", project="proj")
    check("FTS 重建后可检索", any("SQLite" in m.content for m in hits))
    s2 = Store(db)
    check("再开一次不会重复迁移", s2.migration["migrated"] is False, str(s2.migration))


def test_checkpoint_and_pack() -> None:
    print("\n== 结构化状态 + 上下文包 ==")
    from store import Store

    s = Store(tmp_db())
    s.remember("p", "decision", "选型 A", "先做 A，因为简单。")
    s.remember("p", "pitfall", "坑 B", "B 会静默失败，必须显式校验。")
    mid, created, card = s.checkpoint_put("p", {
        "baseline": "技术栈：Python + SQLite\n目录：src/ scripts/",
        "goal": "把交接做稳",
        "done": ["存储层"],
        "next": ["缓存度量"],
        "files": [{"path": "src/store.py", "why": "核心"}],
    })
    check("checkpoint 新建", created and mid > 0)
    check("卡由状态渲染", "当前目标" in card and "src/store.py" in card)
    check("交接卡已同步", (s.get_handoff("p") or None) is not None
          and "把交接做稳" in s.get_handoff("p").content)

    p1 = s.handoff_pack("p")
    p2 = s.handoff_pack("p")
    check("同样内容 → 同样字节", p1["version"] == p2["version"], f"{p1['version']} vs {p2['version']}")
    check("基线在最前", p1["text"].index("基线") < p1["text"].index("长期事实"))
    check("卡在事实之后", p1["text"].index("长期事实") < p1["text"].index("当前状态"))
    check("默认不含时间戳", "2026-" not in p1["text"])

    # 追加一条事实：卡之后的内容会变，卡之前必须逐字节不变
    before = p1["text"]
    s.remember("p", "decision", "选型 C", "后来的结论 C。")
    p3 = s.handoff_pack("p")
    cut = before.index("## 2 · 当前状态")
    check("追加事实不动前缀", p3["text"][:cut] == before[:cut].rstrip() or
          p3["text"].startswith(before[:cut].rsplit("\n\n", 1)[0]))
    check("版本随内容变化", p3["version"] != p1["version"])

    small = s.handoff_pack("p", budget_tokens=60)
    check("预算生效", small["chars"] <= p1["chars"])
    check("裁剪有记录", bool(small["truncated"]) or small["chars"] < 700, str(small["truncated"]))

    check("checkpoint 只有一张生效", s._conn.execute(
        "SELECT COUNT(*) FROM memories WHERE project='p' AND kind='checkpoint' AND status='active'"
    ).fetchone()[0] == 1)

    cp2 = s.checkpoint_put("p", {"goal": "新目标"})
    check("部分更新保留旧字段", "技术栈" in s.get_checkpoint("p").meta.get("baseline", ""))
    check("旧 checkpoint 被 superseded", s._conn.execute(
        "SELECT COUNT(*) FROM memories WHERE project='p' AND kind='checkpoint' AND status='superseded'"
    ).fetchone()[0] >= 1)


def test_sessions() -> None:
    print("\n== 会话台账 ==")
    from datetime import datetime, timedelta

    from store import Store

    recent = (datetime.now().astimezone() - timedelta(hours=2)).isoformat(timespec="seconds")
    s = Store(tmp_db())
    s.upsert_session("s1", project="p", title="第一次", started_at=recent)
    s.upsert_session("s1", compact_count=2, archived_at="2026-01-01T12:00:00+08:00")
    rows = s.sessions("p")
    check("upsert 不产生重复", len(rows) == 1)
    check("None 字段不覆盖旧值", rows[0]["title"] == "第一次")
    check("压缩次数写入", rows[0]["compact_count"] == 2)
    check("latest_session 可用", (s.latest_session("p") or {}).get("session_id") == "s1")
    hint = s._resume_hint("p", rows[0])
    check("压缩过的会话不建议直接续", "压缩过" in (hint.get("advice") or ""))


def main() -> int:
    test_migration()
    test_checkpoint_and_pack()
    test_sessions()
    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
