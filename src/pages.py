"""页面层：给「长文档」准备的第二层，和 memories（原子事实）并存。

为什么要这一层：memories 的设计是「一条 = 一个事实」，检索靠向量和全文。
但长文档塞进去只有两个结果——要么整篇塞进 prompt 太贵，要么切碎后丢掉上下文。
所以分成两级：

    pages   标题 + 摘要 + 时效  → 注入 prompt 的「目录」，几十条也只要几百 token
    chunks 400 字一块 + 向量    → 真正要细节时精确检索

和 memories 的关系：**读写都不碰 memories**。这里是纯增量的一层，
数据库里加两张表，老路径一行不改。

时效判定沿用 QQ bot 那套「按内容词判类别」的做法，但词表换成了工程笔记更常见的说法
（版本 / 废弃 / 迁移 / 政策）。判不出就当半年。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

CHUNK_CHARS = 400
CHUNK_OVERLAP = 80
INDEX_SUMMARY_CHARS = 160

# 保质期分档：越容易变化的越短。
TTL_TIERS = (
    (14, ("价格", "报价", "多少钱", "计费", "收费", "资费", "优惠", "涨价", "降价")),
    (30, ("版本", "发布", "更新", "新版", "刚出", "上线", "弃用", "deprecated", "迁移")),
    (180, ("政策", "法规", "条例", "规定", "标准")),
)
DEFAULT_TTL_DAYS = 180


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ttl_days(*texts: str) -> int:
    blob = " ".join(t or "" for t in texts)
    for days, words in TTL_TIERS:
        if any(w in blob for w in words):
            return days
    return DEFAULT_TTL_DAYS


def is_stale(stale_days: int, updated_at: str) -> bool:
    if stale_days is None or stale_days < 0:
        return False
    try:
        ts = datetime.fromisoformat(updated_at)
    except Exception:  # noqa: BLE001
        return False
    return (datetime.now(timezone.utc) - ts).days > int(stale_days)


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """按段落切，段落太长再按句切。重叠是为了让被切断的一句话两边都完整。"""
    text = (text or "").strip()
    if not text:
        return []
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paras:
        if len(buf) + len(p) + 1 <= size:
            buf = f"{buf}\n{p}".strip()
            continue
        if buf:
            chunks.append(buf)
        if len(p) <= size:
            buf = p
            continue
        sents = [s for s in re.split(r"(?<=[。！？!?])", p) if s.strip()]
        cur = ""
        for s in sents:
            if len(cur) + len(s) <= size:
                cur += s
            else:
                if cur:
                    chunks.append(cur)
                cur = s
        buf = cur
    if buf:
        chunks.append(buf)
    # 加一点重叠：从上一块尾巴接过来，避免检索到时开头被切断
    if overlap and len(chunks) > 1:
        out = [chunks[0]]
        for c in chunks[1:]:
            tail = out[-1][-overlap:]
            out.append((tail + "\n" + c).strip() if tail else c)
        chunks = out
    return chunks


def _title_of(path: Path, text: str) -> str:
    for line in text.splitlines()[:5]:
        s = line.strip()
        if s.startswith("#"):
            t = s.lstrip("# ").strip()
            if t:
                return t[:80]
    return path.stem[:80]


def _clean_md(s: str) -> str:
    # 用 lambda 代替反向引用：反斜杠加数字在这种字符串里太容易被吃掉
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", lambda m: m.group(1), s)
    s = re.sub(r"[`*_]{1,3}", "", s)
    s = re.sub(r"^>\s?", "", s, flags=re.M)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _summary_of(text: str) -> str:
    """摘要取**开头那段正文**，引用块也算。

    四次才调对，每一步都是被真实数据打的：
    1. 直接取第一段 → 全是「早期 55 条…」这种引用块
    2. 跳过引用/表格/列表 → 取到文档中段（开头全被跳过就没得取）
    3. 只看第一个二级标题之前 → 这些文档的引言恰好就是那句引用
    4. 现在：引言区**允许引用块**（去掉 `>` 后当摘要），后面区域仍跳过
    """
    body = re.sub(r"(?s)```.*?```", "", text)
    head, _, rest = body.partition("\n## ")

    def pick(blob: str, allow_quote: bool) -> str:
        for para in re.split(r"\n\s*\n", blob):
            lines = [ln.strip() for ln in para.strip().splitlines()]
            bad = (">", "|", "-", "*", "!", "#") if not allow_quote else ("|", "-", "*", "!", "#")
            lines = [ln for ln in lines if ln and not ln.startswith(bad)]
            cand = _clean_md(" ".join(lines))
            if len(cand) >= 20:
                return cand[:INDEX_SUMMARY_CHARS]
        return ""

    return pick(head, True) or pick(rest, False) or _clean_md(head)[:INDEX_SUMMARY_CHARS]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- 表
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pages (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  project     TEXT NOT NULL,
  path        TEXT NOT NULL,
  title       TEXT NOT NULL,
  summary     TEXT NOT NULL DEFAULT '',
  source      TEXT NOT NULL DEFAULT 'doc',
  status      TEXT NOT NULL DEFAULT 'active',
  stale_days  INTEGER NOT NULL DEFAULT -1,
  verified_at TEXT,
  mtime       REAL NOT NULL DEFAULT 0,
  size        INTEGER NOT NULL DEFAULT 0,
  sha         TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  UNIQUE(project, path)
);

CREATE TABLE IF NOT EXISTS chunks (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  page_id  INTEGER NOT NULL,
  idx      INTEGER NOT NULL,
  text     TEXT NOT NULL,
  vec      BLOB,
  UNIQUE(page_id, idx)
);

CREATE INDEX IF NOT EXISTS idx_pages_project ON pages(project, status);
CREATE INDEX IF NOT EXISTS idx_chunks_page ON chunks(page_id);
"""


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def ensure(store) -> None:
    """宿主用的装配入口：按需建表（幂等）。

    放在 pages 自己这边，而不是 store.__init__ 里——页面层是可选的独立子系统，
    存储层不该知道它存在（P2 拆层，scripts/check_imports.py 会盯着）。
    """
    ensure_tables(store._conn)


# ---------------------------------------------------------------- 写入
def ingest_file(conn: sqlite3.Connection, path: Path, project: str,
                source: str = "doc", force: bool = False) -> tuple[int, int, bool]:
    """灌一个文件。返回 (page_id, 块数, 是否有变化)。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    sha = _sha(text)
    now = _now()
    st = path.stat()
    row = conn.execute(
        "SELECT id, sha FROM pages WHERE project=? AND path=?", (project, str(path))
    ).fetchone()
    if row and row["sha"] == sha and not force:
        return int(row["id"]), 0, False

    title = _title_of(path, text)
    summary = _summary_of(text)
    ttl = ttl_days(title, text)

    if row:
        page_id = int(row["id"])
        conn.execute(
            "UPDATE pages SET title=?, summary=?, stale_days=?, mtime=?, size=?,"
            " sha=?, updated_at=?, status='active' WHERE id=?",
            (title, summary, ttl, st.st_mtime, st.st_size, sha, now, page_id))
        conn.execute("DELETE FROM chunks WHERE page_id=?", (page_id,))
    else:
        cur = conn.execute(
            "INSERT INTO pages(project,path,title,summary,source,status,stale_days,"
            "mtime,size,sha,created_at,updated_at) VALUES(?,?,?,?,?,'active',?,?,?,?,?,?)",
            (project, str(path), title, summary, source, ttl,
             st.st_mtime, st.st_size, sha, now, now))
        page_id = int(cur.lastrowid or 0)

    parts = chunk_text(text)
    # 向量是可选的：拿不到（没 key / 没本地模型）也能存文本，检索退回关键词
    vecs: list[list[float]] = []
    try:
        from embed import embed

        vecs = embed(parts) or []
    except Exception:  # noqa: BLE001
        vecs = []
    for i, part in enumerate(parts):
        vec = None
        if i < len(vecs) and vecs[i]:
            import numpy as np

            vec = np.asarray(vecs[i], dtype=np.float32).tobytes()
        conn.execute("INSERT OR REPLACE INTO chunks(page_id,idx,text,vec) VALUES(?,?,?,?)",
                     (page_id, i, part, vec))
    conn.commit()
    return page_id, len(parts), True


def ingest_dir(conn: sqlite3.Connection, directory: Path, project: str,
               pattern: str = "*.md", limit: int = 500, force: bool = False) -> dict:
    files = sorted(Path(directory).rglob(pattern))[:limit]
    pages = chunks = changed = 0
    for f in files:
        try:
            _pid, n, ch = ingest_file(conn, f, project, force=force)
            pages += 1
            chunks += n
            changed += 1 if ch else 0
        except Exception as e:  # noqa: BLE001
            print(f"  skip {f}: {e}")
    return {"files": len(files), "pages": pages, "chunks": chunks, "changed": changed}


# ---------------------------------------------------------------- 读取
def index_list(conn: sqlite3.Connection, project: str | None = None,
               limit: int = 40) -> list[dict]:
    """目录：标题 + 摘要 + 是否过期。注入 prompt 只用这个。"""
    sql = ("SELECT project, path, title, summary, stale_days, updated_at, status"
           " FROM pages WHERE status='active'")
    args: list = []
    if project:
        sql += " AND project=?"
        args.append(project)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    args.append(limit)
    out = []
    for r in conn.execute(sql, args):
        out.append({
            "project": r["project"], "path": r["path"], "title": r["title"],
            "summary": r["summary"],
            "stale": is_stale(int(r["stale_days"] or -1), r["updated_at"]),
        })
    return out


def render_index(rows: list[dict], mark: str = "（可能过时）") -> str:
    if not rows:
        return ""
    lines = []
    for r in rows:
        flag = mark if r["stale"] else ""
        lines.append(f"- {r['title']}{flag}：{r['summary'][:80]}")
    return "\n".join(lines)


def search_chunks(conn: sqlite3.Connection, query: str,
                  project: str | None = None, limit: int = 3) -> list[dict]:
    """先试向量，失败退回关键词。返回块 + 所属页面标题。"""
    sql = ("SELECT c.text, c.vec, p.title, p.path, p.stale_days, p.updated_at"
           " FROM chunks c JOIN pages p ON p.id=c.page_id WHERE p.status='active'")
    args: list = []
    if project:
        sql += " AND p.project=?"
        args.append(project)
    rows = conn.execute(sql, args).fetchall()

    scored: list[tuple[float, dict]] = []
    used_vec = False
    try:
        import numpy as np

        from embed import embed

        qv = embed([query])
        if qv:
            q = np.asarray(qv[0], dtype=np.float32)
            qn = float(np.linalg.norm(q)) or 1.0
            for r in rows:
                if not r["vec"]:
                    continue
                v = np.frombuffer(r["vec"], dtype=np.float32)
                if v.shape[0] != q.shape[0]:
                    continue
                score = float(v @ q) / ((float(np.linalg.norm(v)) or 1.0) * qn)
                scored.append((score, r))
            used_vec = bool(scored)
    except Exception:  # noqa: BLE001
        used_vec = False

    if not used_vec:
        q_terms = [t for t in re.findall(r"[\u4e00-\u9fa5]{2,}|[A-Za-z]{3,}", query)]
        for r in rows:
            score = sum(1 for t in q_terms if t in r["text"]) / max(1, len(q_terms))
            if score > 0:
                scored.append((score, r))

    scored.sort(key=lambda x: -x[0])
    out = []
    for score, r in scored[:limit]:
        out.append({
            "score": round(score, 3), "title": r["title"], "path": r["path"],
            "text": r["text"][:600],
            "stale": is_stale(int(r["stale_days"] or -1), r["updated_at"]),
            "by": "vector" if used_vec else "keyword",
        })
    return out


def stats(conn: sqlite3.Connection) -> dict:
    p = conn.execute("SELECT COUNT(*) c FROM pages WHERE status='active'").fetchone()["c"]
    c = conn.execute("SELECT COUNT(*) c FROM chunks").fetchone()["c"]
    with_vec = conn.execute("SELECT COUNT(*) c FROM chunks WHERE vec IS NOT NULL").fetchone()["c"]
    return {"pages": p, "chunks": c, "chunks_with_vec": with_vec}
