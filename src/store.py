"""存储与检索层：SQLite + FTS5(中文 bigram) + 向量余弦，RRF 融合。

表结构见 schema.py（v5：meta / projects / memories / sessions / vectors）。

为什么不用向量数据库：
- 单机个人规模（万条以内），SQLite + numpy 暴力检索是毫秒级；
- 少一个常驻服务、少一个故障点、备份就是一个 .db 文件；
- FTS5 和向量在同一个事务里，天然一致。

中文检索为什么用 bigram：
- FTS5 的 unicode61 不切中文，整段会变成一个 token，检索基本失效；
- 按两字滑窗切分后，"项目记忆" → "项目 目记 记忆"，召回率足够且实现简单。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from typing import Any, Iterable

import numpy as np

import card as card_mod
import paths
import projects as projects_mod
import schema

ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
# 数据目录可整体搬走（DSH_RAG_DATA），单库仍可用 DSH_RAG_DB 精确指定
DB_PATH = __import__("pathlib").Path(
    os.environ.get("DSH_RAG_DB") or (paths.DATA_ROOT / "memory.db")
)

_CJK = re.compile(r"[\u4e00-\u9fff]")
_SEG = re.compile(r"[\u4e00-\u9fff]+|[A-Za-z0-9_./\\:@-]+")

# 日志型记忆：又长、又什么都沾一点，RRF 下容易挤掉精炼的决策/事实。
# 值 = (权重, 单次最多占几条)。见 Store._rerank。
LOG_KINDS = {"changelog": (0.78, 2), "session": (0.88, 2)}

# 进 pack 的「长期事实」类型；handoff/checkpoint/session 由各自通道处理
FACT_KINDS = ("decision", "pitfall", "fact", "todo")

# 一个项目最多保留的 active checkpoint 数（更早的自动 superseded，可追溯）
MAX_ACTIVE_CHECKPOINTS = 1


def now_iso() -> str:
    return schema.now_iso()


def tokenize_zh(text: str) -> str:
    """中文 bigram + 英文/数字按词切分，产出空格分隔的 token 串。"""
    out: list[str] = []
    for seg in _SEG.findall(text or ""):
        if _CJK.match(seg):
            if len(seg) == 1:
                out.append(seg)
            else:
                out.extend(seg[i : i + 2] for i in range(len(seg) - 1))
        else:
            out.append(seg.lower())
    return " ".join(out)


def _fts_query(text: str) -> str:
    toks = tokenize_zh(text).split()
    if not toks:
        return ""
    # 去重保序，OR 连接以扩大召回；每个 token 加引号防止语法字符注入
    seen: list[str] = []
    for t in toks:
        if t not in seen:
            seen.append(t)
    return " OR ".join(f'"{t}"' for t in seen[:64])


@dataclass
class Memory:
    id: int
    project: str
    kind: str
    title: str
    content: str
    status: str
    created_at: str
    session_id: str | None
    updated_at: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    source: str = "manual"
    use_count: int = 0
    last_used_at: str | None = None
    score: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project": self.project,
            "kind": self.kind,
            "title": self.title,
            "content": self.content,
            "status": self.status,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at or self.created_at,
            "source": self.source,
            "use_count": self.use_count,
            "last_used_at": self.last_used_at,
            "meta": self.meta,
            "score": round(self.score, 5),
        }

    @classmethod
    def from_row(cls, r: sqlite3.Row, score: float = 0.0) -> "Memory":
        try:
            meta = json.loads(r["meta_json"] or "{}")
        except (ValueError, KeyError):
            meta = {}
        keys = r.keys()
        return cls(
            id=int(r["id"]),
            project=r["project"],
            kind=r["kind"],
            title=r["title"],
            content=r["content"],
            status=r["status"],
            created_at=r["created_at"],
            session_id=r["session_id"],
            updated_at=r["updated_at"] if "updated_at" in keys else "",
            meta=meta if isinstance(meta, dict) else {},
            source=(r["source"] if "source" in keys else "manual") or "manual",
            use_count=int(r["use_count"]) if "use_count" in keys else 0,
            last_used_at=r["last_used_at"] if "last_used_at" in keys else None,
            score=score,
        )


class Store:
    def __init__(self, db_path: Any = DB_PATH) -> None:
        import pathlib

        self.db_path = pathlib.Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        # 至少三个进程会写这个库：MCP 服务、归档守护、运维脚本。默认 busy_timeout=0
        # 时后到的写入直接报 database is locked，不是数据问题，等一会儿就过去了。
        self._conn.execute("PRAGMA busy_timeout=5000")
        self.migration = schema.ensure(self._conn, self.db_path)
        # 页面层（长文档的目录 + 块）建表是纯增量，不碰 memories
        try:
            import pages as pages_mod

            pages_mod.ensure_tables(self._conn)
        except Exception as e:  # noqa: BLE001
            print(f"[store] pages 建表失败：{e}")
        self._vec_cache: tuple[str, np.ndarray, list[int]] | None = None
        # 最近一次危险操作（删项目）留下的备份路径，给 CLI / 面板回显用
        self.last_backup: str | None = None
        if self.migration.get("migrated"):
            self.sync_projects()

    def _backup(self, tag: str) -> str:
        """整库快照（sqlite backup API，WAL 会合并）。失败会抛——危险操作不该硬着头皮继续。"""
        import time as _time

        ts = _time.strftime("%Y%m%d-%H%M%S")
        dest = self.db_path.parent / "backup" / f"{tag}-{ts}.db"
        dest.parent.mkdir(parents=True, exist_ok=True)
        src = sqlite3.connect(str(self.db_path))
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        return str(dest)

    # ----------------------------------------------------------------- 项目
    def _known_projects(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SELECT name FROM projects ORDER BY name").fetchall()
        return [r["name"] for r in rows]

    def normalize_project(self, name: str | None) -> str:
        return projects_mod.normalize(name, self._known_projects())

    def sync_projects(self) -> int:
        """把磁盘目录与记忆里出现过的项目登记进 projects 表。

        目录是权威（决定 hasDir / 归档状态），表只是登记簿。
        只在**真有变化**时写库：面板每 10 秒轮询一次 overview，无脑 UPSERT
        等于每 10 秒一次写事务，和 MCP 的写入抢锁。
        """
        ts = now_iso()
        dirs = projects_mod.scan_dirs(force=True)
        with self._lock:
            rows = {
                r["name"]: (r["dir_path"], r["archived_at"])
                for r in self._conn.execute(
                    "SELECT name, dir_path, archived_at FROM projects"
                ).fetchall()
            }
            changed = False
            for name in dirs:
                want = str(projects_mod.PROJECTS_ROOT / name)
                cur = rows.get(name)
                if cur is None or cur[0] != want or cur[1]:
                    self._conn.execute(
                        """INSERT INTO projects(name, dir_path, first_seen, updated_at)
                           VALUES (?,?,?,?)
                           ON CONFLICT(name) DO UPDATE SET
                             dir_path=excluded.dir_path,
                             updated_at=excluded.updated_at,
                             archived_at=NULL""",
                        (name, want, ts, ts),
                    )
                    changed = True
            for name, (dir_path, archived_at) in rows.items():
                if name in dirs or name in projects_mod.RESERVED:
                    continue
                if dir_path and not archived_at:
                    self._conn.execute(
                        "UPDATE projects SET dir_path=NULL, archived_at=?, updated_at=? WHERE name=?",
                        (ts, ts, name),
                    )
                    changed = True
            if changed:
                self._conn.commit()
        return len(dirs)

    def _touch_project(self, name: str, ts: str | None = None) -> None:
        ts = ts or now_iso()
        d = projects_mod.project_dir(name)
        with self._lock:
            self._conn.execute(
                """INSERT INTO projects(name, dir_path, first_seen, updated_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET
                     dir_path=COALESCE(excluded.dir_path, projects.dir_path),
                     updated_at=excluded.updated_at""",
                (name, str(d) if d else None, ts, ts),
            )

    # ----------------------------------------------------------------- write
    def _hash(self, project: str, title: str, content: str) -> str:
        raw = f"{project}\x00{title}\x00{content}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _fts_put(self, mid: int, title: str, content: str) -> None:
        """FTS 唯一的写入口：先删后插，避免「改完搜不到」。"""
        self._conn.execute("DELETE FROM memories_fts WHERE rowid = ?", (int(mid),))
        self._conn.execute(
            "INSERT INTO memories_fts(rowid, title_tok, content_tok) VALUES (?,?,?)",
            (int(mid), tokenize_zh(title), tokenize_zh(content)),
        )

    def remember(
        self,
        project: str,
        kind: str,
        title: str,
        content: str,
        session_id: str | None = None,
        status: str = "active",
        ts: str | None = None,
        meta: dict[str, Any] | None = None,
        source: str = "manual",
        revive: bool = False,
    ) -> tuple[int, bool]:
        """写入一条记忆。返回 (id, 是否新建)。内容完全相同时幂等返回旧 id。

        `revive` 控制「同内容命中但状态不同」时要不要把状态改回来。默认不动 ——
        否则归档过的会话摘要被 backfill 重抽一次就会复活，等于把 cleanup 静默撤销
        （2026-11-06 审视踩到）。put_handoff 需要这个语义，它显式传 revive=True。
        """
        project = self.normalize_project(project)
        kind = (kind or "fact").strip() or "fact"
        title = (title or "").strip()[:200] or "(无标题)"
        content = (content or "").strip()
        ts = ts or now_iso()
        h = self._hash(project, title, content)
        meta_json = json.dumps(meta or {}, ensure_ascii=False, sort_keys=True)

        with self._lock:
            row = self._conn.execute(
                "SELECT id, status FROM memories WHERE content_hash = ?", (h,)
            ).fetchone()
            if row:
                # 内容相同但状态不同时，只有显式要求才改回去。
                # put_handoff 会先把旧卡置 superseded，再用同样内容写一遍，
                # 如果这里不恢复 active，结果就是「一张生效的卡都没有」。
                if revive and row["status"] != status:
                    self._conn.execute(
                        "UPDATE memories SET status=?, updated_at=? WHERE id=?",
                        (status, ts, int(row["id"])),
                    )
                    self._conn.commit()
                return int(row["id"]), False

            self._touch_project(project, ts)
            cur = self._conn.execute(
                """INSERT INTO memories
                   (project, kind, title, content, meta_json, status, session_id,
                    source, created_at, updated_at, content_hash)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (project, kind, title, content, meta_json, status, session_id,
                 source, ts, ts, h),
            )
            mid = int(cur.lastrowid)
            self._fts_put(mid, title, content)
            self._conn.commit()
            self._invalidate_vec_cache()
            return mid, True

    def put_handoff(
        self,
        project: str,
        content: str,
        session_id: str | None = None,
        source: str = "manual",
        meta: dict[str, Any] | None = None,
    ) -> int:
        """写入项目交接卡：同项目旧的 active handoff 自动失效，并同步落一份 markdown。

        失效时同时填 superseded_by —— v4 只改 status，链是断的，查不到「这张卡被谁取代了」。
        """
        project = self.normalize_project(project)
        with self._lock:
            mid, _ = self.remember(
                project, "handoff", "项目交接卡", content,
                session_id=session_id, source=source, meta=meta, revive=True,
            )
            self._conn.execute(
                "UPDATE memories SET superseded_by=?, status='superseded', updated_at=? "
                "WHERE project=? AND kind='handoff' AND status='active' AND id != ?",
                (mid, now_iso(), project, mid),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT created_at FROM memories WHERE id=?", (mid,)
            ).fetchone()
        # 数据库是真相源，md 只是给人看/给 git 管的导出物；写失败不影响入库
        md_content = content
        if str(source).startswith("checkpoint"):
            # 基线只在 checkpoint 里，卡正文里没有；导出的 md 要让人看到它
            try:
                cp = self.get_checkpoint(project)
                base = str((cp.meta or {}).get("baseline") or "").strip() if cp else ""
                if base:
                    md_content = f"## 基线（稳定）\n{base}\n\n---\n\n{content}"
            except Exception:  # noqa: BLE001
                pass
        try:
            projects_mod.write_handoff_md(
                project, md_content, row["created_at"] if row else now_iso()
            )
        except Exception:  # noqa: BLE001
            pass
        return mid

    # ------------------------------------------------------------- checkpoint
    def get_checkpoint(self, project: str) -> Memory | None:
        project = self.normalize_project(project)
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM memories WHERE project=? AND kind='checkpoint' "
                "AND status='active' ORDER BY id DESC LIMIT 1",
                (project,),
            ).fetchone()
        return Memory.from_row(r) if r else None

    def checkpoint_put(
        self,
        project: str,
        patch: dict[str, Any],
        session_id: str | None = None,
        source: str = "manual",
        render_handoff: bool = True,
        reembed: bool = True,
    ) -> tuple[int, bool, str]:
        """写入/合并结构化状态。

        `patch` 里的字段与当前 checkpoint 合并（列表整体替换、字符串覆盖），
        然后渲染成交接卡。返回 (id, 是否新建, 渲染出的卡正文)。

        **不能复用 remember() 的去重路径**：它的 hash 只看 project/title/content，
        而 render_card 不渲染 baseline —— 只改 baseline 时卡逐字节不变，会命中
        旧行、meta 不更新，调用方却以为成功了（2026-11-06 实测）。baseline 是
        缓存前缀的地基，必须真的能改。所以这里按「当前 active checkpoint」原地
        更新：内容没变也照样写 meta，不再每次新增一行。
        """
        project = self.normalize_project(project)
        prev = self.get_checkpoint(project)
        meta: dict[str, Any] = dict(prev.meta) if prev else {}
        for key in ("baseline", "goal"):
            if key in patch and patch[key] is not None:
                meta[key] = str(patch[key]).strip()
        for key in ("done", "doing", "next", "decisions", "pitfalls", "open_questions", "files"):
            if key in patch and patch[key] is not None:
                meta[key] = list(patch[key])
        ts = now_iso()
        meta["updated_at"] = ts

        title = "项目状态"
        card_text = card_mod.render_card(project, meta)
        meta_json = json.dumps(meta, ensure_ascii=False, sort_keys=True)

        with self._lock:
            created = prev is None
            body_changed = False
            if created:
                mid, _ = self.remember(
                    project, "checkpoint", title, card_text,
                    session_id=session_id, source=source, meta=meta,
                )
            else:
                mid = prev.id
                new_hash = self._hash(project, title, card_text)
                # content_hash 全库唯一：同样内容若被别的记忆占了（极罕见），
                # 退化成把那条复用成 checkpoint，而不是硬写一个重复 hash。
                dup = self._conn.execute(
                    "SELECT id FROM memories WHERE content_hash = ? AND id != ?",
                    (new_hash, mid),
                ).fetchone()
                if dup:
                    mid = int(dup["id"])
                    self._conn.execute(
                        "UPDATE memories SET status='active', meta_json=?, updated_at=? WHERE id=?",
                        (meta_json, ts, mid),
                    )
                    body_changed = True
                else:
                    body_changed = (prev.content or "") != card_text
                    self._conn.execute(
                        "UPDATE memories SET content=?, content_hash=?, meta_json=?, "
                        "status='active', updated_at=?, source=? WHERE id=?",
                        (card_text, new_hash, meta_json, ts, source, mid),
                    )
                    if body_changed:
                        self._fts_put(mid, title, card_text)
                self._touch_project(project, ts)

            # 一个项目只留一张生效 checkpoint；人工确认过状态后草稿就没意义了
            self._conn.execute(
                "UPDATE memories SET status='superseded', superseded_by=?, updated_at=? "
                "WHERE project=? AND kind='checkpoint' AND status='active' AND id != ?",
                (mid, ts, project, mid),
            )
            self._conn.execute(
                "UPDATE memories SET status='superseded', updated_at=? "
                "WHERE project=? AND kind='checkpoint' AND status='draft'",
                (ts, project),
            )
            self._conn.commit()
            if body_changed:
                self._invalidate_vec_cache()

        if reembed:
            # 内容变了、甚至只是重建了同一行，向量都得跟上；
            # 否则语义检索会按旧内容把这条召回来。
            self._reembed(mid, title, card_text)
        if render_handoff:
            self.put_handoff(
                project, card_text, session_id=session_id,
                source=f"checkpoint#{mid}", meta={"checkpoint_id": mid},
            )
        return mid, created, card_text

    def get_baseline(self, project: str) -> str:
        cp = self.get_checkpoint(project)
        return str(cp.meta.get("baseline") or "") if cp else ""

    # --------------------------------------------------------------- 草稿
    # 归档会自动写「状态草稿」：status='draft'，**不参与检索、不进 pack**。
    # 模型只需看一眼、确认后 promote —— 把「手写交接」变成「审阅草稿」。
    def get_draft(self, project: str) -> Memory | None:
        project = self.normalize_project(project)
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM memories WHERE project=? AND kind='checkpoint' "
                "AND status='draft' ORDER BY id DESC LIMIT 1",
                (project,),
            ).fetchone()
        return Memory.from_row(r) if r else None

    def checkpoint_draft(
        self,
        project: str,
        meta: dict[str, Any],
        session_id: str | None = None,
        source: str = "archive-draft",
    ) -> int:
        """写一份状态草稿（不覆盖生效状态）。同项目只留最新一份草稿。

        同项目已有草稿时**原地更新**，不再每次归档都新增一条再置旧的 superseded
        —— 否则每轮归档都留一份历史草稿，cleanup 永远追不上（2026-11-06 审视）。
        """
        project = self.normalize_project(project)
        payload = dict(meta)
        payload["draft"] = True
        payload["updated_at"] = now_iso()
        ts = payload["updated_at"]
        text = card_mod.render_card(project, payload)
        meta_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)

        with self._lock:
            prev = self.get_draft(project)
            if prev is not None:
                mid = prev.id
                h = self._hash(project, "项目状态草稿", text)
                dup = self._conn.execute(
                    "SELECT id FROM memories WHERE content_hash = ? AND id != ?",
                    (h, mid),
                ).fetchone()
                if not dup:
                    self._conn.execute(
                        "UPDATE memories SET content=?, content_hash=?, meta_json=?, "
                        "status='draft', updated_at=?, source=? WHERE id=?",
                        (text, h, meta_json, ts, source, mid),
                    )
                    self._fts_put(mid, "项目状态草稿", text)
                    self._invalidate_vec_cache()
            else:
                mid, _ = self.remember(
                    project, "checkpoint", "项目状态草稿", text,
                    session_id=session_id, status="draft", source=source, meta=payload,
                )
            # 防御：历史遗留的多份 draft 收敛成一份
            self._conn.execute(
                "UPDATE memories SET status='superseded', updated_at=? "
                "WHERE project=? AND kind='checkpoint' AND status='draft' AND id != ?",
                (ts, project, mid),
            )
            self._conn.commit()
        return mid

    def drop_draft(self, project: str) -> int:
        project = self.normalize_project(project)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE memories SET status='superseded', updated_at=? "
                "WHERE project=? AND kind='checkpoint' AND status='draft'",
                (now_iso(), project),
            )
            self._conn.commit()
            return int(cur.rowcount or 0)

    def promote_draft(self, project: str, session_id: str | None = None) -> tuple[int, bool, str] | None:
        """把草稿并入生效状态（草稿字段优先），然后作废草稿。"""
        project = self.normalize_project(project)
        draft = self.get_draft(project)
        if not draft:
            return None
        patch = {k: v for k, v in draft.meta.items() if k not in ("draft", "updated_at")}
        result = self.checkpoint_put(project, patch, session_id=session_id, source="draft-promote")
        self.drop_draft(project)
        return result

    # --------------------------------------------------------------- 归档/恢复
    def archive_memories(self, ids: list[int]) -> int:
        """软删除：置为 archived。检索、pack、recent 都看不到，但记录还在，可 restore。"""
        if not ids:
            return 0
        ph = ",".join("?" * len(ids))
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE memories SET status='archived', updated_at=? WHERE id IN ({ph})",
                (now_iso(), *ids),
            )
            self._conn.commit()
            return int(cur.rowcount or 0)

    def restore_memory(self, memory_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE memories SET status='active', updated_at=? WHERE id=? AND status='archived'",
                (now_iso(), int(memory_id)),
            )
            self._conn.commit()
            ok = bool(cur.rowcount)
            row = self._conn.execute(
                "SELECT title, content FROM memories WHERE id=?", (int(memory_id),)
            ).fetchone()
        if not ok:
            return False
        # 归档时 cleanup 删过向量。只把状态改回来会得到「关键词搜得到、语义搜不到」
        # 的半残记忆 —— 比不恢复更难发现，所以这里补一次。
        if row:
            self._reembed(int(memory_id), row["title"], row["content"])
        return True

    # ----------------------------------------------------------------- 会话
    def supersede_session_memories(self, session_id: str) -> int:
        """把一个会话的旧摘要置为失效（重归档前调用）。返回条数。

        重抽会得到新内容 → 新 hash → 新行。不作废旧行的话，同一个会话会有
        两条 active 摘要，接力度量和整库统计都会重复计数。
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE memories SET status='superseded', updated_at=? "
                "WHERE kind='session' AND session_id=? AND status='active'",
                (now_iso(), session_id),
            )
            self._conn.commit()
            return int(cur.rowcount or 0)

    def supersede_kind(self, project: str, kind: str, keep_id: int | None = None) -> int:
        """把某项目下该 kind 的生效记录置为 superseded（keep_id 除外）。返回条数。

        与 supersede_session_memories 的区别：那个按 session_id 专用于会话摘要，
        这个是通用语义，供 Repository 适配器使用（P2）。
        """
        project = self.normalize_project(project)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE memories SET status='superseded', updated_at=? "
                "WHERE project=? AND kind=? AND status='active' "
                "AND (? IS NULL OR id != ?)",
                (now_iso(), project, kind, keep_id, keep_id),
            )
            self._conn.commit()
            return int(cur.rowcount or 0)

    def upsert_session(
        self,
        session_id: str,
        project: str | None = None,
        title: str | None = None,
        cwd: str | None = None,
        origin: str | None = None,
        started_at: str | None = None,
        last_seen_at: str | None = None,
        archived_at: str | None = None,
        memory_id: int | None = None,
        compact_count: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """会话台账 upsert。传 None 的字段保持原值。"""
        detail_json = json.dumps(detail, ensure_ascii=False) if detail is not None else None
        with self._lock:
            self._conn.execute(
                """INSERT INTO sessions
                     (session_id, project, title, cwd, origin, started_at, last_seen_at,
                      archived_at, memory_id, compact_count, detail_json)
                   VALUES (?,?,?,?,?,?,?,?,?,COALESCE(?,'0'),COALESCE(?, '{}'))
                   ON CONFLICT(session_id) DO UPDATE SET
                     project      = COALESCE(excluded.project, sessions.project),
                     title        = COALESCE(excluded.title, sessions.title),
                     cwd          = COALESCE(excluded.cwd, sessions.cwd),
                     origin       = COALESCE(excluded.origin, sessions.origin),
                     started_at   = COALESCE(excluded.started_at, sessions.started_at),
                     last_seen_at = COALESCE(excluded.last_seen_at, sessions.last_seen_at),
                     archived_at  = COALESCE(excluded.archived_at, sessions.archived_at),
                     memory_id    = COALESCE(excluded.memory_id, sessions.memory_id),
                     compact_count= COALESCE(?, sessions.compact_count),
                     detail_json  = COALESCE(?, sessions.detail_json)""",
                (session_id, project, title, cwd, origin, started_at, last_seen_at,
                 archived_at, memory_id,
                 None if compact_count is None else int(compact_count),
                 detail_json,
                 None if compact_count is None else int(compact_count),
                 detail_json),
            )
            self._conn.commit()

    def session_done(self, session_id: str) -> bool:
        with self._lock:
            r = self._conn.execute(
                "SELECT 1 FROM sessions WHERE session_id=? AND archived_at IS NOT NULL",
                (session_id,),
            ).fetchone()
        return bool(r)

    def sessions(self, project: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        sql = "SELECT * FROM sessions"
        params: list[Any] = []
        if project:
            sql += " WHERE project=?"
            params.append(project)
        sql += " ORDER BY COALESCE(started_at,'') DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["detail"] = json.loads(d.pop("detail_json") or "{}")
            except ValueError:
                d["detail"] = {}
            out.append(d)
        return out

    def latest_session(self, project: str) -> dict[str, Any] | None:
        got = self.sessions(project, limit=1)
        return got[0] if got else None

    # ----------------------------------------------------------------- write
    def handoff_md_path(self, project: str) -> str | None:
        """交接卡导出文件的位置（不一定存在）。"""
        try:
            return str(projects_mod.handoff_path(projects_mod.normalize(project)))
        except Exception:  # noqa: BLE001
            return None

    def update_memory(
        self,
        memory_id: int,
        title: str | None = None,
        content: str | None = None,
        reembed: bool = True,
        meta: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        """就地修改一条记忆。返回 (是否成功, 说明)。

        同步维护三样东西，漏一个就会出现「改完搜不到」或「改了还按旧内容召回」：
        1. memories 行 + content_hash（唯一约束，改了要查重）
        2. FTS 索引（rowid 对齐，先删后插）
        3. 向量 —— 默认由本方法重算；语义检索依赖它，调用方很容易忘
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memories WHERE id = ?", (int(memory_id),)
            ).fetchone()
            if not row:
                return False, f"id={memory_id} 不存在"

            new_title = (title if title is not None else row["title"])
            new_title = (new_title or "").strip()[:200] or "(无标题)"
            new_content = (content if content is not None else row["content"])
            new_content = (new_content or "").strip()

            new_hash = self._hash(row["project"], new_title, new_content)
            new_meta = (
                json.dumps(meta, ensure_ascii=False, sort_keys=True)
                if meta is not None else row["meta_json"]
            )
            if new_hash == row["content_hash"] and new_meta == row["meta_json"]:
                return True, "内容没有变化"

            dup = self._conn.execute(
                "SELECT id FROM memories WHERE content_hash = ? AND id != ?",
                (new_hash, int(memory_id)),
            ).fetchone()
            if dup:
                return False, f"与 id={dup['id']} 内容重复，未修改"

            self._conn.execute(
                "UPDATE memories SET title=?, content=?, content_hash=?, meta_json=?, updated_at=? "
                "WHERE id=?",
                (new_title, new_content, new_hash, new_meta, now_iso(), int(memory_id)),
            )
            self._fts_put(int(memory_id), new_title, new_content)
            self._conn.commit()
            self._invalidate_vec_cache()

        if reembed:
            self._reembed(int(memory_id), new_title, new_content)

        return True, "已更新"

    def _reembed(self, memory_id: int, title: str, content: str) -> None:
        """重算一条记忆的向量。

        失败不算错：关键词检索仍然可用，下次 reembed/backfill 会补上。
        所有「改了正文」的路径都该走这里，漏一个就会「改了还按旧内容召回」。
        """
        try:
            import embed  # 延迟导入：纯文本操作不必付这份开销

            vecs = embed.embed([f"{title}\n{content}"])
            if vecs:
                self.add_vector(int(memory_id), vecs[0], embed.MODEL_NAME)
        except Exception:  # noqa: BLE001
            pass

    def get_memory(self, memory_id: int) -> Memory | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM memories WHERE id = ?", (int(memory_id),)
            ).fetchone()
        return Memory.from_row(r) if r else None

    def add_vector(self, memory_id: int, vec: Iterable[float], model: str) -> None:
        arr = np.asarray(list(vec), dtype=np.float32)
        with self._lock:
            try:
                self._conn.execute(
                    """INSERT OR REPLACE INTO vectors(memory_id, model, dim, vec, updated_at)
                       VALUES (?,?,?,?,?)""",
                    (memory_id, model, int(arr.shape[0]), arr.tobytes(), now_iso()),
                )
                self._conn.commit()
            except Exception:
                # 不 rollback 的话这条连接会一直挂着写事务，别的进程（归档守护、运维脚本）
                # 全部 database is locked —— 踩过一次，堵了好几个小时。
                self._conn.rollback()
                raise
            self._invalidate_vec_cache()

    def rollback(self) -> None:
        """把残留的写事务退掉。工具调用抛异常时由 server 的兜底逻辑调用。"""
        with self._lock:
            try:
                if self._conn.in_transaction:
                    self._conn.rollback()
            except Exception:  # noqa: BLE001
                pass

    def set_status(self, memory_id: int, status: str, superseded_by: int | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE memories SET status=?, superseded_by=?, updated_at=? WHERE id=?",
                (status, superseded_by, now_iso(), memory_id),
            )
            self._conn.commit()

    # ----------------------------------------------------------------- read
    def _invalidate_vec_cache(self) -> None:
        self._vec_cache = None

    def _vectors(self, model: str | None = None) -> tuple[list[int], np.ndarray]:
        """只取指定模型的向量。换后端后旧向量维度不同，混算会出错，必须按 model 隔离。"""
        cache_key = model or ""
        if self._vec_cache is not None and self._vec_cache[0] == cache_key:
            return self._vec_cache[2], self._vec_cache[1]

        sql = "SELECT memory_id, dim, vec FROM vectors"
        params: tuple = ()
        if model:
            sql += " WHERE model = ?"
            params = (model,)
        sql += " ORDER BY memory_id"

        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        ids: list[int] = []
        vecs: list[np.ndarray] = []
        for r in rows:
            ids.append(int(r["memory_id"]))
            vecs.append(np.frombuffer(r["vec"], dtype=np.float32))
        mat = np.vstack(vecs) if vecs else np.zeros((0, 1), dtype=np.float32)
        self._vec_cache = (cache_key, mat, ids)
        return ids, mat

    def _keyword_search(self, query: str, limit: int) -> list[tuple[int, float]]:
        q = _fts_query(query)
        if not q:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT rowid, bm25(memories_fts) AS s FROM memories_fts "
                "WHERE memories_fts MATCH ? ORDER BY s LIMIT ?",
                (q, limit * 4),
            ).fetchall()
        return [(int(r["rowid"]), float(r["s"])) for r in rows]

    def _vector_search(self, query: str, limit: int) -> list[tuple[int, float]]:
        from embed import MODEL_NAME, embed  # 延迟导入：纯关键词场景不必付这份开销

        ids, mat = self._vectors(MODEL_NAME)
        if not ids:
            return []
        out = embed([query])
        if not out:
            return []
        qv = np.asarray(out[0], dtype=np.float32)
        mat2 = mat
        if mat2.shape[1] != qv.shape[0]:
            return []
        qn = np.linalg.norm(qv)
        if qn == 0:
            return []
        norms = np.linalg.norm(mat2, axis=1)
        norms[norms == 0] = 1.0
        sims = (mat2 @ qv) / (norms * qn)
        k = min(limit * 4, len(ids))
        top = np.argpartition(-sims, k - 1)[:k]
        top = top[np.argsort(-sims[top])]
        return [(ids[i], float(sims[i])) for i in top]

    def recall(
        self,
        query: str,
        project: str | None = None,
        limit: int = 8,
        kinds: list[str] | None = None,
        include_superseded: bool = False,
    ) -> list[Memory]:
        """混合检索：关键词 + 向量，RRF 融合，项目隔离。"""
        limit = max(1, min(int(limit), 50))
        # 写入侧永远归一（DSH-RAG → dsh-rag），查询侧不归一就会静默 0 条：
        # 模型以为「没有历史」，比报错还难查。2026-11-06 实测。
        if project:
            project = self.normalize_project(project)
        # 候选池要比 limit 大：重排会按类型限流，池子太小就没得挑，
        # 结果还是被日志型记忆填满（实测 limit=8 时池子只有 16 条。
        pool = max(limit * 3, 24)
        kw = self._keyword_search(query, pool)
        vec = self._vector_search(query, pool)

        RRF_K = 60
        fused: dict[int, float] = {}
        for rank, (mid, _) in enumerate(kw):
            fused[mid] = fused.get(mid, 0.0) + 1.0 / (RRF_K + rank + 1)
        for rank, (mid, _) in enumerate(vec):
            fused[mid] = fused.get(mid, 0.0) + 1.0 / (RRF_K + rank + 1)

        if not fused:
            return []

        ids = list(fused.keys())
        placeholders = ",".join("?" * len(ids))
        sql = f"SELECT * FROM memories WHERE id IN ({placeholders})"
        params: list[Any] = list(ids)
        if project:
            sql += " AND project = ?"
            params.append(project)
        if not include_superseded:
            sql += " AND status = 'active'"
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            params.extend(kinds)

        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()

        out = [Memory.from_row(r, score=fused.get(int(r["id"]), 0.0)) for r in rows]
        out = self._rerank(out, limit, diversify=not kinds)
        # 使用计数：只统计「真的返回给调用方」的那些。清理策略靠它区分
        # 「写了但从来没人用」和「一直在用」——这两个的保留价值完全不同。
        if out:
            self._touch_usage([m.id for m in out])
        return out

    def _rerank(self, items: list[Memory], limit: int,
                diversify: bool = True) -> list[Memory]:
        """对 RRF 结果再排一次：给日志型记忆降权，并限制它们在结果里的占比。

        为什么需要：变更有 39 条、每条 400 字，又是「什么都沾一点」的叙事，
        在 RRF 下会同时命中关键词和向量，把精炼的决策/事实挤出前几名。
        实测 5 个典型查询，8 条里有 5–7 条是 changelog —— 真正的结论反而排后面。

        规则：
        - 权重：changelog 0.78、session 0.88（乘在 RRF 分数上，不改变相对次序之外的东西）；
        - 单类上限：两者各自最多占 `limit // 3` 条（至少 1 条），其余留给别的类型；
        - 调用方显式传了 `kinds` 就不介入——那时是精确查询，别再自作主张。
        """
        for m in items:
            rule = LOG_KINDS.get(m.kind)
            if rule:
                m.score *= rule[0]
        items.sort(key=lambda m: m.score, reverse=True)
        if not diversify or len(items) <= limit:
            return items[:limit]

        picked: list[Memory] = []
        overflow: list[Memory] = []
        seen: dict[str, int] = {}
        for m in items:
            rule = LOG_KINDS.get(m.kind)
            if rule:
                cap = max(1, min(rule[1], limit // 3))
                if seen.get(m.kind, 0) >= cap:
                    overflow.append(m)
                    continue
            seen[m.kind] = seen.get(m.kind, 0) + 1
            picked.append(m)
            if len(picked) >= limit:
                break
        for m in overflow:              # 别的类型不够填时，再用它们补位
            if len(picked) >= limit:
                break
            picked.append(m)
        return picked[:limit]

    def _touch_usage(self, ids: list[int]) -> None:
        """累加检索命中次数。失败不影响检索结果，所以整段吞掉异常。"""
        if not ids:
            return
        ph = ",".join("?" * len(ids))
        try:
            with self._lock:
                self._conn.execute(
                    f"UPDATE memories SET use_count=use_count+1, last_used_at=? WHERE id IN ({ph})",
                    (now_iso(), *ids),
                )
                self._conn.commit()
        except Exception:  # noqa: BLE001
            try:
                self._conn.rollback()
            except Exception:  # noqa: BLE001
                pass

    # ----------------------------------------------------------------- pack
    def facts_for_pack(self, project: str, limit: int = 40) -> list[Memory]:
        """长期事实：只取 active 的 decision/pitfall/fact/todo，**按 id 升序**。

        升序是有意的：新的事实永远追加在末尾，前面的字节不动，缓存前缀才稳定。
        limit 取最近的 N 条（即 id 最大的 N 条），输出时再翻回升序。
        """
        kinds = ",".join("?" * len(FACT_KINDS))
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT * FROM memories
                    WHERE project=? AND status='active' AND kind IN ({kinds})
                    ORDER BY id DESC LIMIT ?""",
                (project, *FACT_KINDS, int(limit)),
            ).fetchall()
        return [Memory.from_row(r) for r in reversed(rows)]

    def handoff_pack(
        self,
        project: str,
        budget_tokens: int = 3000,
        with_meta: bool = False,
        fact_limit: int = 40,
    ) -> dict[str, Any]:
        project = self.normalize_project(project)
        cp = self.get_checkpoint(project)
        h = self.get_handoff(project)
        facts = self.facts_for_pack(project, fact_limit)
        pack = card_mod.build_pack(
            project,
            card=h.content if h else None,
            checkpoint=cp.meta if cp else None,
            facts=[m.as_dict() for m in facts],
            budget_tokens=budget_tokens,
            with_meta=with_meta,
        )
        sess = self.latest_session(project)
        pack["project"] = project
        pack["hasHandoff"] = bool(h)
        pack["checkpoint"] = bool(cp)
        pack["factCount"] = len(facts)
        pack["resume"] = self._resume_hint(project, sess)
        draft = self.get_draft(project)
        pack["draft"] = None if not draft else {
            "id": draft.id,
            "updated_at": draft.updated_at,
            "preview": (draft.content or "").splitlines()[0][:80] if draft.content else "",
        }
        return pack

    def _resume_hint(self, project: str, sess: dict[str, Any] | None) -> dict[str, Any]:
        """「能恢复就别重开」：恢复会话能拿到完整现场 + 完整前缀缓存。

        但有两个前提：会话没被压缩（压缩会把现场截断），而且不算太久远
        （服务端的上下文缓存按小时计，隔天的会话恢复回去也基本是冷启动）。
        """
        if not sess:
            return {"sessionId": None, "advice": None}
        compact = int(sess.get("compact_count") or 0)
        started = sess.get("started_at") or ""
        summary = str(sess.get("title") or "")[:60]

        age_hours: float | None = None
        if started:
            try:
                from datetime import datetime as _dt

                age_hours = (_dt.now().astimezone() - _dt.fromisoformat(started)).total_seconds() / 3600
            except (ValueError, TypeError):
                age_hours = None

        if age_hours is not None and age_hours > 24:
            advice = (
                f"最近会话是 {age_hours / 24:.0f} 天前的（{summary}），缓存早已过期、现场也凉了；"
                "按这个 pack 开新局。"
            )
        elif compact == 0:
            advice = (
                f"最近会话 {sess['session_id']}（{started}，{summary}）没有被压缩过，"
                f"距今 {age_hours:.0f} 小时；优先在 DSH 里恢复它，而不是开新会话。"
                if age_hours is not None else
                f"最近会话 {sess['session_id']}（{started}，{summary}）没有被压缩过，优先恢复它。"
            )
        else:
            advice = (
                f"最近会话 {sess['session_id']}（{started}）压缩过 {compact} 次，"
                "现场已被截断；按这个 pack 开新会话更稳。"
            )
        return {
            "sessionId": sess["session_id"],
            "startedAt": started,
            "title": summary,
            "compactCount": compact,
            "ageHours": round(age_hours, 1) if age_hours is not None else None,
            "advice": advice,
        }

    # ----------------------------------------------------------------- 总览
    def overview(self, sync: bool = True) -> list[dict[str, Any]]:
        """项目总览：每个项目一条，带交接卡摘要与会话台账。

        项目来源 = projects 登记簿 ∪ Projects 目录下的真实目录。
        后者保证「新建了项目但还没写过记忆」也能在面板上看到。

        `sync=False` 给只读场景（doctor）用：它会写 projects 表，
        体检不该顺手改数据。
        """
        if sync:
            self.sync_projects()
        with self._lock:
            rows = self._conn.execute("SELECT * FROM v_project_stats").fetchall()
            hrows = self._conn.execute(
                """SELECT project, content, created_at FROM memories
                   WHERE kind='handoff' AND status='active'"""
            ).fetchall()
            sess = self._conn.execute(
                """SELECT project, COUNT(*) AS n, MAX(COALESCE(started_at,'')) AS last,
                          SUM(compact_count) AS compacts
                   FROM sessions GROUP BY project"""
            ).fetchall()
            kind_rows = self._conn.execute(
                """SELECT project, kind, COUNT(*) AS n FROM memories
                   WHERE status='active' GROUP BY project, kind"""
            ).fetchall()

        by_project: dict[str, dict[str, Any]] = {}
        for r in rows:
            by_project[r["project"]] = {
                "project": r["project"],
                "total": int(r["total"]),
                "active": int(r["active"] or 0),
                "handoffs": 0,
                "kinds": {},
                "last_update": r["last_update"],
                "hasDir": False,
                "handoff": None,
                "sessions": 0,
                "lastSession": None,
                "compacts": 0,
            }

        for h in hrows:
            item = by_project.setdefault(
                h["project"],
                {"project": h["project"], "total": 0, "active": 0, "handoffs": 0,
                 "kinds": {}, "last_update": h["created_at"], "hasDir": False,
                 "handoff": None, "sessions": 0, "lastSession": None, "compacts": 0},
            )
            text = (h["content"] or "").strip()
            item["handoffs"] = 1
            item["handoff"] = {
                "preview": text.splitlines()[0][:80] if text else "",
                "length": len(text),
                "updated_at": h["created_at"],
            }

        for s in sess:
            item = by_project.setdefault(
                s["project"] or "未归类",
                {"project": s["project"] or "未归类", "total": 0, "active": 0,
                 "handoffs": 0, "kinds": {}, "last_update": s["last"], "hasDir": False,
                 "handoff": None, "sessions": 0, "lastSession": None, "compacts": 0},
            )
            item["sessions"] = int(s["n"] or 0)
            item["lastSession"] = s["last"]
            item["compacts"] = int(s["compacts"] or 0)

        for k in kind_rows:
            item = by_project.setdefault(
                k["project"],
                {"project": k["project"], "total": 0, "active": 0, "handoffs": 0,
                 "kinds": {}, "last_update": None, "hasDir": False, "handoff": None,
                 "sessions": 0, "lastSession": None, "compacts": 0},
            )
            item["kinds"][k["kind"]] = int(k["n"])

        for name in projects_mod.scan_dirs():
            item = by_project.setdefault(
                name,
                {"project": name, "total": 0, "active": 0, "handoffs": 0,
                 "kinds": {}, "last_update": None, "hasDir": True, "handoff": None,
                 "sessions": 0, "lastSession": None, "compacts": 0},
            )
            item["hasDir"] = True

        out = list(by_project.values())
        for it in out:
            reserved = it["project"] in projects_mod.RESERVED
            it["reserved"] = reserved
            # 保留名（工作区/未归类）本来就没有目录，不能标成「孤儿」——
            # 否则面板会给出「清理」按钮，一点就把零散会话的记忆删了。
            it["hasDir"] = reserved or projects_mod.project_dir(it["project"]) is not None
            try:
                it["mdPath"] = str(projects_mod.handoff_path(it["project"]))
            except Exception:  # noqa: BLE001
                it["mdPath"] = None

        # 排序（sort 是稳定的，所以从次要键往主要键排）：
        # 1) 最近更新的在前  2) 有交接卡的在前  3) 有内容的在前
        out.sort(key=lambda x: x["last_update"] or "", reverse=True)
        out.sort(key=lambda x: 0 if x["handoff"] else 1)
        out.sort(key=lambda x: 0 if x["total"] else 1)
        return out

    def delete_project(self, project: str) -> int:
        """删除一个项目的全部记忆（含 FTS、向量、页面层）。返回删除条数。

        只删数据库；项目目录和导出的 md 不动——删目录是文件系统的事，
        这里只负责把「记忆」这一层的残留清掉。
        """
        project = self.normalize_project(project)
        # 硬删不可恢复：先留一份整库快照（失败会抛，删除就不会执行）。
        # 项目目录被临时改名 / 断网盘时面板仍会显示「清理」，这道兜底就是为它准备的。
        self.last_backup = self._backup("prune")
        with self._lock:
            ids = [
                int(r["id"])
                for r in self._conn.execute(
                    "SELECT id FROM memories WHERE project = ?", (project,)
                ).fetchall()
            ]
            # 页面层是独立的两张表：不清就会留下「项目已删、pages_index 还查得到」的鬼影
            try:
                page_ids = [
                    int(r["id"])
                    for r in self._conn.execute(
                        "SELECT id FROM pages WHERE project = ?", (project,)
                    ).fetchall()
                ]
                if page_ids:
                    pph = ",".join("?" * len(page_ids))
                    self._conn.execute(
                        f"DELETE FROM chunks WHERE page_id IN ({pph})", page_ids
                    )
                    self._conn.execute("DELETE FROM pages WHERE project = ?", (project,))
            except sqlite3.DatabaseError:
                pass          # 老库可能还没有页面层
            if not ids:
                self._conn.commit()
                return 0
            ph = ",".join("?" * len(ids))
            self._conn.execute(f"DELETE FROM memories_fts WHERE rowid IN ({ph})", ids)
            self._conn.execute(f"DELETE FROM vectors WHERE memory_id IN ({ph})", ids)
            self._conn.execute("DELETE FROM sessions WHERE project = ?", (project,))
            cur = self._conn.execute("DELETE FROM memories WHERE project = ?", (project,))
            self._conn.execute("DELETE FROM projects WHERE name = ?", (project,))
            self._conn.commit()
            self._invalidate_vec_cache()
            return int(cur.rowcount or len(ids))

    def orphans(self) -> list[dict[str, Any]]:
        """目录已不存在、但记忆还在的项目。"""
        out = []
        for p in self.projects():
            name = p["project"]
            if name in projects_mod.RESERVED:
                continue
            if projects_mod.project_dir(name) is None:
                out.append(p)
        return out

    def get_handoff(self, project: str) -> Memory | None:
        project = self.normalize_project(project)
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM memories WHERE project=? AND kind='handoff' AND status='active' "
                "ORDER BY id DESC LIMIT 1",
                (project,),
            ).fetchone()
        return Memory.from_row(r) if r else None

    def projects(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM v_project_stats ORDER BY last_update DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def recent(self, project: str | None = None, limit: int = 20) -> list[Memory]:
        sql = "SELECT * FROM memories WHERE status='active'"
        params: list[Any] = []
        if project:
            sql += " AND project=?"
            params.append(self.normalize_project(project))
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [Memory.from_row(r) for r in rows]

    def missing_vectors(self, model: str | None = None) -> list[dict[str, Any]]:
        """没有可用向量的记忆。

        换过 embedding 后端后，旧模型的向量也算「缺」——它和当前模型的向量不在同一个
        空间里，RAG 里混算没有意义，所以按 model 过滤而不是只看有没有行。
        `model=None` 表示「任何模型的向量都算数」。
        """
        sql = (
            "SELECT m.id, m.project, m.kind, m.title, m.content, m.updated_at "
            "FROM memories m "
            "LEFT JOIN vectors v ON v.memory_id = m.id"
        )
        params: tuple = ()
        if model:
            sql += " AND v.model = ?"
            params = (model,)
        sql += " WHERE v.memory_id IS NULL AND m.status = 'active' ORDER BY m.id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def stats(self, model: str | None = None) -> dict[str, Any]:
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]
            active = self._conn.execute(
                "SELECT COUNT(*) c FROM memories WHERE status='active'"
            ).fetchone()["c"]
            vec = self._conn.execute("SELECT COUNT(*) c FROM vectors").fetchone()["c"]
            proj = self._conn.execute("SELECT COUNT(*) c FROM projects").fetchone()["c"]
            sess = self._conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"]
            by_kind = {
                r["kind"]: int(r["n"])
                for r in self._conn.execute(
                    "SELECT kind, COUNT(*) n FROM memories WHERE status='active' GROUP BY kind"
                ).fetchall()
            }
            if model:
                missing = self._conn.execute(
                    "SELECT COUNT(*) c FROM memories m "
                    "LEFT JOIN vectors v ON v.memory_id = m.id AND v.model = ? "
                    "WHERE v.memory_id IS NULL AND m.status = 'active'",
                    (model,),
                ).fetchone()["c"]
            else:
                missing = self._conn.execute(
                    "SELECT COUNT(*) c FROM memories m "
                    "LEFT JOIN vectors v ON v.memory_id = m.id "
                    "WHERE v.memory_id IS NULL AND m.status = 'active'"
                ).fetchone()["c"]
            archived = self._conn.execute(
                "SELECT COUNT(*) c FROM memories WHERE status='archived'").fetchone()["c"]
            drafts = self._conn.execute(
                "SELECT COUNT(*) c FROM memories WHERE status='draft'").fetchone()["c"]
            info = schema.schema_info(self._conn)
        return {
            "total": total, "active": active, "vectors": vec,
            "missing_vectors": missing,
            "archived": int(archived), "drafts": int(drafts),
            "projects": proj, "sessions": sess, "kinds": by_kind,
            "schema": info,
        }
