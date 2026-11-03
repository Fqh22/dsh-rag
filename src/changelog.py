"""变更记录 → 记忆：解析、压缩、发现、增量灌入。

为什么是 dsh-rag 的一部分而不是某个项目的脚本：
「项目的变更记录里那些『为什么这么改』」是最该被跨会话检索到的东西，但它写在
项目仓库里、只能 grep。反过来，记忆库能语义检索，却要人把结论再抄一遍——两边
各维护一次必然漂移。把它做成库的一环，任何项目只要有变更记录文件就自动跟上。

约定（按顺序找，找到第一个就用）：
    docs/99-*.md  CHANGELOG.md  docs/CHANGELOG.md  变更记录.md  docs/变更记录.md
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

ENTRY_RE = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2})?)\s*[·:：-]?\s*(.+?)\s*$")
# 只灌「有可复用结论」的条目：纯过程记录（改了几个文件、跑了几次测试）灌进去
# 只会淹掉真正的决策。
LESSON_MARKS = ("教训", "结论", "踩", "值得", "别再", "回滚", "代价", "风险")
MAX_CHARS = 400
PATTERNS = ("docs/99-*.md", "CHANGELOG.md", "docs/CHANGELOG.md",
            "变更记录.md", "docs/变更记录.md")

PROJECTS_ROOT = Path(os.environ.get(
    "DSH_PROJECTS", Path.home() / "Documents" / "Projects"))
STATE = Path(os.environ.get(
    "DSH_RAG_DATA", Path(__file__).resolve().parent.parent / "data")) / "changelog_state.json"


def parse_entries(text: str) -> list[dict]:
    out: list[dict] = []
    cur: dict | None = None
    for ln in text.splitlines():
        m = ENTRY_RE.match(ln)
        if m and ln.startswith("## "):
            cur = {"date": m.group(1).strip(), "title": m.group(2).strip(), "body": []}
            out.append(cur)
            continue
        if ln.startswith("## ") or ln.startswith("# "):
            cur = None                      # 别的标题（索引、附录）不算条目
            continue
        if cur is not None:
            cur["body"].append(ln)
    for e in out:
        body = "\n".join(e["body"])
        body = re.sub(r"```.*?```", "", body, flags=re.S)
        e["body"] = re.sub(r"\n{3,}", "\n\n", body).strip()
    return out


def condense(body: str) -> str:
    """取正文开头 MAX_CHARS 字，**不改写**。

    试过两种"智能抽取"（按关键词抓结论行、首段+教训小节），抽出来都是半句话——
    变更记录是给人顺着读的，单拎一句既没前因也没结论。所以不装聪明：
    标题本身就是最好的摘要，正文开头通常就是「起因 + 实测数据」。
    """
    text = re.sub(r"```.*?```", "", body, flags=re.S)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_CHARS]


def discover(root: Path | None = None) -> list[tuple[str, Path]]:
    """扫出「有变更记录的项目」。项目名 = 目录名，和记忆库的 project 对齐。"""
    root = Path(root or PROJECTS_ROOT)
    found: list[tuple[str, Path]] = []
    if not root.exists():
        return found
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name.startswith((".", "_")):
            continue
        for pat in PATTERNS:
            hits = sorted(d.glob(pat)) if "*" in pat else ([d / pat] if (d / pat).exists() else [])
            if hits:
                found.append((d.name, hits[-1]))
                break
    return found


def project_file(project: str, root: Path | None = None) -> Path | None:
    for name, path in discover(root):
        if name == project:
            return path
    return None


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def ingest(store, project: str, path: Path, *, kind: str = "changelog",
           all_entries: bool = False, limit: int = 0, min_body: int = 120,
           dry_run: bool = False, state: dict | None = None,
           embed_mod=None) -> dict:
    """把一个变更记录文件增量灌进记忆库，返回统计。不调模型，可重复跑。"""
    path = Path(path)
    if not path.exists():
        return {"file": str(path), "error": "文件不存在", "added": 0, "skipped": 0}
    entries = parse_entries(path.read_text(encoding="utf-8", errors="replace"))
    state = state if state is not None else load_state()
    done = state.setdefault(project, {})

    todo = [e for e in entries
            if f"{e['date']} · {e['title']}" not in done
            and len(e["body"]) >= min_body
            and (all_entries or any(m in e["body"] for m in LESSON_MARKS))]
    if limit:
        todo = todo[:limit]

    result = {"file": str(path), "entries": len(entries), "pending": len(todo),
              "added": 0, "skipped": 0, "dry_run": dry_run}
    if dry_run or not todo:
        result["sample"] = [f"{e['date']} {e['title'][:30]}" for e in todo[:8]]
        return result

    if embed_mod is None:                   # 延迟导入：库里可能有纯离线用法
        import embed as embed_mod  # type: ignore  # noqa: N813

    added = 0
    for i in range(0, len(todo), 8):
        batch = todo[i:i + 8]
        texts, ids = [], []
        for e in batch:
            key = f"{e['date']} · {e['title']}"
            title = f"{path.stem[:3]} · {e['title']}"[:60] if path.stem.startswith("99") \
                else e["title"][:60]
            content = f"{condense(e['body'])}\n\n[出处] {path.name} · {e['date']}"
            try:
                mid, created = store.remember(project, kind, title, content)
            except Exception as ex:         # noqa: BLE001
                result.setdefault("errors", []).append(f"{key}: {type(ex).__name__}: {ex}")
                continue
            done[key] = mid
            if created:
                texts.append(f"{title}\n{content}")
                ids.append(mid)
            added += 1 if created else 0
        if texts:
            try:
                vecs = getattr(embed_mod, "embed")(texts)
                if vecs and len(vecs) == len(ids):
                    for mid, v in zip(ids, vecs):
                        store.add_vector(mid, v, getattr(embed_mod, "MODEL_NAME", ""))
            except Exception as ex:         # noqa: BLE001
                result.setdefault("errors", []).append(
                    f"向量化失败：{type(ex).__name__}: {ex}")

    result["added"] = added
    result["skipped"] = len(todo) - added
    save_state(state)
    return result


def ingest_all(store, *, dry_run: bool = False, limit: int = 0,
               all_entries: bool = False, root: Path | None = None) -> list[dict]:
    state = load_state()
    out: list[dict] = []
    for project, path in discover(root):
        r = ingest(store, project, path, dry_run=dry_run, limit=limit,
                   all_entries=all_entries, state=state)
        out.append({"project": project, **r})
    return out
