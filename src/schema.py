"""数据库结构与迁移（唯一真相）。

v5 的设计目标：**每一层只做一件事，且名字能自解释**。

    meta        库自身的元信息（schema_version / 建库时间 / 向量模型）
    projects    项目实体（目录是权威，表里是登记簿：路径、首见时间、是否归档）
    memories    唯一的事实表：一条记忆 = 一行。结构化字段放 meta_json
    sessions    会话台账：一条 DSH 会话 = 一行（替代 v4 那个临时建的 archivals）
    vectors     向量，按 model 隔离（换后端必须重建，混算没有意义）
    memories_fts 全文索引（独立表，rowid 与 memories.id 对齐）

**为什么把会话单独建表**：v4 把「这个会话归档过没有」记在 `archivals` 里，
但那个表是 archive.py 运行时顺手建的，不在 SCHEMA 里、没有迁移、面板看不到。
会话本来就是一等公民（标题/项目/压缩次数/对应记忆），单独一表才清楚。

**为什么结构化字段用 meta_json 而不是拆表**：记忆的形态差异很大
（decision 只有正文，checkpoint 才有 goal/next/files），拆表会得到一堆空列。
JSON 一列 + Python 侧校验，既保持单表单写路径，也方便以后加字段。

版本迁移只做加法：老库打开时自动升到 v5，旧表重命名后保留一份拷贝，
确认没问题再手工删（迁移脚本自己不会 DROP 掉带数据的备份表）。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

SCHEMA_VERSION = 6

# 允许的 kind。checkpoint / baseline 是 v5 新增：
#   baseline   项目基线（技术栈、目录、红线），**几乎不变** —— 缓存前缀靠它
#   checkpoint 结构化状态（目标/已完成/进行中/下一步/文件）
#   handoff    渲染出来的交接卡（人读的、给模型开场看的）
#   session    会话归档摘要（含 DSH 压缩产生的 compact 摘要）
KINDS = (
    "baseline",
    "checkpoint",
    "handoff",
    "decision",
    "fact",
    "todo",
    "pitfall",
    "session",
    "note",
)

STATUSES = ("active", "superseded", "archived")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
  name       TEXT PRIMARY KEY,
  dir_path   TEXT,
  first_seen TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  archived_at TEXT
);

CREATE TABLE IF NOT EXISTS memories (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  project       TEXT NOT NULL,
  kind          TEXT NOT NULL,
  title         TEXT NOT NULL,
  content       TEXT NOT NULL,
  meta_json     TEXT NOT NULL DEFAULT '{}',
  status        TEXT NOT NULL DEFAULT 'active',
  superseded_by INTEGER,
  session_id    TEXT,
  source        TEXT NOT NULL DEFAULT 'manual',
  use_count     INTEGER NOT NULL DEFAULT 0,
  last_used_at  TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  content_hash  TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_mem_project ON memories(project, status, kind, id);
CREATE INDEX IF NOT EXISTS idx_mem_kind    ON memories(kind, status, id DESC);
CREATE INDEX IF NOT EXISTS idx_mem_session ON memories(session_id);
CREATE INDEX IF NOT EXISTS idx_mem_used    ON memories(last_used_at);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
  USING fts5(title_tok, content_tok, tokenize='unicode61');

CREATE TABLE IF NOT EXISTS vectors (
  memory_id  INTEGER PRIMARY KEY,
  model      TEXT NOT NULL,
  dim        INTEGER NOT NULL,
  vec        BLOB NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vec_model ON vectors(model);

CREATE TABLE IF NOT EXISTS sessions (
  session_id    TEXT PRIMARY KEY,
  project       TEXT,
  title         TEXT,
  cwd           TEXT,
  origin        TEXT,
  started_at    TEXT,
  last_seen_at  TEXT,
  archived_at   TEXT,
  memory_id     INTEGER,
  compact_count INTEGER NOT NULL DEFAULT 0,
  detail_json   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_sess_project ON sessions(project, started_at DESC);

-- 人读的视图：项目总览、当前交接卡。写逻辑仍只在 Python 里
CREATE VIEW IF NOT EXISTS v_project_stats AS
  SELECT project,
         COUNT(*)                                        AS total,
         SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) AS active,
         MAX(updated_at)                                 AS last_update
  FROM memories GROUP BY project;

CREATE VIEW IF NOT EXISTS v_handoff AS
  SELECT * FROM memories WHERE kind='handoff' AND status='active';
"""

# v4 的 kind 里没有 baseline/checkpoint，迁移时不用改；
# 但要重算 superseded_by 链（v4 只改 status，从不填这个字段）。


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (name,)
    ).fetchone()
    return bool(row)


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    except sqlite3.DatabaseError:
        return []


def ensure(conn: sqlite3.Connection) -> dict:
    """把连接上的库升到当前版本。返回 {from, to, migrated}。"""
    conn.execute("PRAGMA foreign_keys=OFF")
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])

    if version == SCHEMA_VERSION:
        return {"from": version, "to": SCHEMA_VERSION, "migrated": False}

    has_memories = _table_exists(conn, "memories")
    old_version = version
    if not has_memories:
        _create_fresh(conn)
        return {"from": 0, "to": SCHEMA_VERSION, "migrated": True}

    cols = _columns(conn, "memories")
    if "meta_json" in cols:
        # 结构已经是 v5 系，只补增量列（v6 加使用计数，用于判断"值不值得留"）
        changed = _add_missing_columns(conn, "memories", {
            "use_count": "INTEGER NOT NULL DEFAULT 0",
            "last_used_at": "TEXT",
        })
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_used ON memories(last_used_at)")
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        _ensure_meta(conn)
        conn.commit()
        return {"from": old_version, "to": SCHEMA_VERSION, "migrated": changed}

    _migrate_v4_to_v5(conn)
    return {"from": old_version, "to": SCHEMA_VERSION, "migrated": True}


def _add_missing_columns(conn: sqlite3.Connection, table: str, spec: dict[str, str]) -> bool:
    """只加列，不改不删。返回是否真的动过结构。"""
    have = set(_columns(conn, table))
    changed = False
    for name, decl in spec.items():
        if name in have:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        changed = True
    return changed


def _create_fresh(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    _ensure_meta(conn)
    conn.commit()


def _ensure_meta(conn: sqlite3.Connection) -> None:
    ts = now_iso()
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('created_at', ?)", (ts,))


def _migrate_v4_to_v5(conn: sqlite3.Connection) -> None:
    """v4 → v5：换一张更清楚的表，但一条记忆都不丢。

    旧表不删，重命名成 `_v4_*` 留在库里 —— 迁移是一次性操作，
    留一份原样数据比任何「应该没问题」都可靠；确认无事后再手工 DROP。
    """
    ts = now_iso()
    conn.executescript(
        """
        ALTER TABLE memories RENAME TO _v4_memories;
        ALTER TABLE vectors  RENAME TO _v4_vectors;
        """
    )
    if _table_exists(conn, "archivals"):
        conn.execute("ALTER TABLE archivals RENAME TO _v4_archivals")
    # FTS 表与 memories 解耦，重建一次最省心
    conn.execute("DROP TABLE IF EXISTS memories_fts")

    conn.executescript(SCHEMA_SQL)

    conn.execute(
        """INSERT INTO memories
             (id, project, kind, title, content, meta_json, status, superseded_by,
              session_id, source, created_at, updated_at, content_hash)
           SELECT id, project, kind, title, content, '{}', status, NULL,
                  session_id,
                  CASE WHEN session_id IS NULL OR session_id='' THEN 'manual' ELSE 'archive' END,
                  created_at, updated_at, content_hash
           FROM _v4_memories"""
    )
    conn.execute(
        """INSERT INTO vectors (memory_id, model, dim, vec, updated_at)
           SELECT memory_id, model, dim, vec, ? FROM _v4_vectors""",
        (ts,),
    )

    # 项目登记簿：从已有记忆推出来（目录路径由 Store 打开时按磁盘补齐）
    conn.execute(
        """INSERT OR IGNORE INTO projects (name, dir_path, first_seen, updated_at)
           SELECT project, NULL, MIN(created_at), MAX(updated_at)
           FROM memories GROUP BY project"""
    )

    # 旧交接卡链：v4 只把 status 置 superseded，superseded_by 一直是 NULL
    _rebuild_supersede_chain(conn)

    if _table_exists(conn, "_v4_archivals"):
        conn.execute(
            """INSERT OR IGNORE INTO sessions
                 (session_id, project, archived_at, memory_id, started_at, last_seen_at)
               SELECT a.session_id, m.project, a.archived_at, a.memory_id, m.created_at, a.archived_at
               FROM _v4_archivals a
               LEFT JOIN memories m ON m.id = a.memory_id"""
        )

    # 重建 FTS
    conn.execute("DELETE FROM memories_fts")
    for r in conn.execute("SELECT id, title, content FROM memories").fetchall():
        conn.execute(
            "INSERT INTO memories_fts(rowid, title_tok, content_tok) VALUES (?,?,?)",
            (int(r["id"]), _tok(r["title"]), _tok(r["content"])),
        )

    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    _ensure_meta(conn)
    conn.commit()


def _rebuild_supersede_chain(conn: sqlite3.Connection) -> None:
    """把 superseded 的行按 (project, kind, id) 顺序串成链：superseded_by = 下一条 id。"""
    rows = conn.execute(
        """SELECT id, project, kind, status FROM memories
           ORDER BY project, kind, id"""
    ).fetchall()
    prev: dict[tuple[str, str], int] = {}
    for r in rows:
        key = (r["project"], r["kind"])
        if key in prev:
            conn.execute(
                "UPDATE memories SET superseded_by=? WHERE id=?", (int(r["id"]), prev[key])
            )
        if r["status"] != "active":
            prev[key] = int(r["id"])
        else:
            prev.pop(key, None)


def _tok(text: str) -> str:
    """中文 bigram 分词。与 store.tokenize_zh 保持一致——迁移期间 store 还没导入。"""
    import re

    cjk = re.compile(r"[\u4e00-\u9fff]")
    seg = re.compile(r"[\u4e00-\u9fff]+|[A-Za-z0-9_./\\:@-]+")
    out: list[str] = []
    for part in seg.findall(text or ""):
        if cjk.match(part):
            if len(part) == 1:
                out.append(part)
            else:
                out.extend(part[i : i + 2] for i in range(len(part) - 1))
        else:
            out.append(part.lower())
    return " ".join(out)


def schema_info(conn: sqlite3.Connection) -> dict:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    meta = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM meta").fetchall()}
    counts = {}
    for t in ("projects", "memories", "sessions", "vectors"):
        try:
            counts[t] = int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
        except sqlite3.DatabaseError:
            counts[t] = -1
    leftovers = [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '_v4_%'"
        ).fetchall()
    ]
    return {"version": version, "meta": meta, "counts": counts, "leftovers": leftovers}
