"""归档链路端到端自测。

它**自建**会话目录，不依赖本机 `~/.dsh/sessions`。

为什么重写：旧版本只有 print、一条断言都没有，跑完 exit 0 ——
在 CI 上永远是「跳过、通过」，属于假绿。而 `archive.py` 是第三大模块，
「会话变长自动归档」又是 README 的头条卖点，这条链路不该没有断言。

跑法：.venv\\Scripts\\python.exe scripts\\selftest_archive.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

_ISO = Path(tempfile.mkdtemp(prefix="dshrag-archive-"))
os.environ["DSH_RAG_PROJECTS_ROOT"] = str(_ISO / "projects")
os.environ["DSH_RAG_HANDOFFS_ROOT"] = str(_ISO / "handoffs")
os.environ["DSH_RAG_EMBED"] = "off"

import zstandard  # noqa: E402

import archive  # noqa: E402
from store import Store  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "sessions"
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


def make_session(root: Path, ws: str, sid: str, fixture: str,
                 age_seconds: float = 400) -> Path:
    """造一个「已经结束」的会话文件（mtime 早于 STALE_SECONDS）。"""
    d = root / ws / sid
    d.mkdir(parents=True, exist_ok=True)
    p = d / "session.v4.jsonl.zstd"
    p.write_bytes(zstandard.ZstdCompressor().compress((FIXTURES / fixture).read_bytes()))
    t = time.time() - age_seconds
    os.utime(p, (t, t))
    return p


def count(s: Store, kind: str, status: str | None = "active") -> int:
    sql = "SELECT COUNT(*) c FROM memories WHERE kind=?"
    params: list = [kind]
    if status:
        sql += " AND status=?"
        params.append(status)
    return int(s._conn.execute(sql, params).fetchone()["c"])


def main() -> int:
    # 让 guess_project 认得出 "demo"：它只认 Projects 下真实存在的目录
    ( _ISO / "projects" / "demo").mkdir(parents=True, exist_ok=True)
    sessions = _ISO / "sessions"
    make_session(sessions, "--ws-demo--", "sess-min", "minimal.jsonl")
    make_session(sessions, "--ws-demo--", "sess-compact", "compact.jsonl")
    # 一个坏文件：不能让它影响其它会话
    bad = sessions / "--ws-demo--" / "sess-bad" / "session.v4.jsonl.zstd"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"not zstd")
    t = time.time() - 400
    os.utime(bad, (t, t))

    saved = archive.SESSIONS_ROOT
    archive.SESSIONS_ROOT = sessions
    try:
        s = Store(_ISO / "t.db")

        print("== 首次归档 ==")
        msg = archive.run(store=s, limit=10)
        check("报告处理了 2 个会话", "处理 2 个会话" in msg, msg)
        check("坏文件被跳过而不是中断", "2 个会话" in msg, msg)

        n_session = count(s, "session")
        check("写入 2 条会话摘要", n_session == 2, str(n_session))
        check("台账登记 2 个会话", len(s.sessions(limit=10)) == 2,
              str(len(s.sessions(limit=10))))

        rows = s._conn.execute(
            "SELECT project, title FROM memories WHERE kind='session'").fetchall()
        projects = {r["project"] for r in rows}
        check("项目归属收敛到 demo", projects == {"demo"}, str(projects))
        check("标题带「会话归档：」前缀",
              all(r["title"].startswith("会话归档：") for r in rows))

        print("\n== 摘要内容 ==")
        one = s._conn.execute(
            "SELECT content FROM memories WHERE kind='session' "
            "AND title LIKE '%最小会话%'").fetchone()
        body = one["content"] if one else ""
        check("摘要有会话 id", "sess-minimal" in body, body[:80])
        check("摘要有用户请求", "帮我看看 README" in body)
        check("摘要有助手结论", "好的，我先读一下" in body)
        check("摘要有动作行", "read:" in body, body[:200])

        print("\n== 草稿只给「改过东西」的会话 ==")
        drafts = count(s, "checkpoint", "draft")
        check("压缩过的会话生成 1 份草稿", drafts == 1, str(drafts))
        draft = s.get_draft("demo")
        check("草稿带 session 标记", draft is not None and draft.meta.get("session") == "sess-compact",
              str((draft.meta or {}).get("session") if draft else None))
        plain = s._conn.execute(
            "SELECT COUNT(*) c FROM memories WHERE kind='checkpoint' AND status='draft' "
            "AND content LIKE '%最小会话%'").fetchone()["c"]
        check("纯只读会话不生成草稿", int(plain) == 0, str(plain))

        print("\n== 幂等：再跑一次 ==")
        msg2 = archive.run(store=s, limit=10)
        check("第二次跳过已归档的", "已跳过 2 个已归档项" in msg2, msg2)
        check("会话摘要没有重复", count(s, "session") == 2, str(count(s, "session")))

        print("\n== force 重抽不产生重复 ==")
        before = count(s, "session")
        archive.run(store=s, limit=10, force=True)
        check("force 后会话数不变（旧摘要被作废而非堆积）",
              count(s, "session") == before, f"{before} -> {count(s, 'session')}")
        check("没有留下 superseded 的会话摘要",
              count(s, "session", "superseded") == 0,
              str(count(s, "session", "superseded")))

        print("\n== 正在写的会话不碰 ==")
        fresh = make_session(sessions, "--ws-demo--", "sess-fresh", "minimal.jsonl",
                             age_seconds=10)
        msg3 = archive.run(store=s, limit=10)
        check("mtime 太新的会话被跳过", "sess-fresh" not in str(s.sessions(limit=20)),
              msg3)
        check("文件还在", fresh.is_file())
    finally:
        archive.SESSIONS_ROOT = saved

    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
