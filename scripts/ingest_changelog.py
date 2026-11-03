"""把 `docs/99-变更记录.md` 的条目增量灌进记忆库。

为什么要有它：变更记录里每一条都带「为什么这么改 + 实测数据」，但只能靠 grep 查；
记忆库能语义检索，却要人手把结论抄进去。两边各写一遍迟早漂移。

做法（**不用模型，纯规则**，可重复跑、结果稳定）：
    ## YYYY-MM-DD HH:MM · 标题  →  一条 decision 记忆
    正文取前 700 字，末尾附出处

去重靠 `scripts/.ingest_state.json` 记「已灌过的条目标题」；记忆库自身也有
content_hash 去重兜一层。

用法：
    .venv\\Scripts\\python.exe scripts\\ingest_changelog.py --dry-run
    .venv\\Scripts\\python.exe scripts\\ingest_changelog.py
    .venv\\Scripts\\python.exe scripts\\ingest_changelog.py --limit 5
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))

STATE = HERE / ".ingest_state.json"
DEFAULT_FILE = Path(r"C:\path\to\Projects\qq-cloud-bot\docs\99-变更记录.md")
ENTRY_RE = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*·\s*(.+?)\s*$")
MAX_BODY = 700
# 只灌「有可复用结论」的条目：纯过程记录（改了几个文件、跑了几次测试）不值得进记忆库，
# 灌进去只会淹掉真正有用的决策。经验上，值得留的都带这几类词。
LESSON_MARKS = ("教训", "结论", "踩", "值得", "别再", "回滚", "代价", "风险")
MAX_CHARS = 320


def parse_entries(text: str) -> list[dict]:
    lines = text.splitlines()
    out: list[dict] = []
    cur: dict | None = None
    for ln in lines:
        m = ENTRY_RE.match(ln)
        if m:
            cur = {"date": m.group(1), "title": m.group(2).strip(), "body": []}
            out.append(cur)
            continue
        if ln.startswith("## "):
            cur = None                      # 别的二级标题（比如索引）
            continue
        if cur is not None:
            cur["body"].append(ln)
    for e in out:
        body = "\n".join(e["body"]).strip()
        body = re.sub(r"```.*?```", "", body, flags=re.S)       # 代码块不要
        body = re.sub(r"\n{3,}", "\n\n", body)
        e["body"] = body
    return out


def condense(body: str) -> str:
    """取正文开头 400 字，**不改写**。

    试过两种抽取（按关键词抓结论行、首段+教训小节），都不行——变更记录是给人
    顺着读的，抽出来的一句话既没前因也没结论（「上一轮列的六个未覆盖点…」）。

    所以不装聪明：**标题本身就是最好的摘要**（「删掉等待式防抖，改成语义队列」），
    正文开头通常就是「起因 + 实测数据」。原样截 400 字，检索靠标题命中。
    """
    text = re.sub(r"```.*?```", "", body, flags=re.S)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:400]


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=str(DEFAULT_FILE))
    ap.add_argument("--project", default="qq-cloud-bot")
    ap.add_argument("--kind", default="changelog")
    ap.add_argument("--limit", type=int, default=0, help="最多灌几条（0=全部）")
    ap.add_argument("--min-body", type=int, default=120, help="正文太短的不灌")
    ap.add_argument("--all", action="store_true", help="连纯过程条目也灌（默认只灌带结论的）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    src = Path(args.file)
    if not src.exists():
        print(f"找不到 {src}")
        return 2
    entries = parse_entries(src.read_text(encoding="utf-8"))
    state = load_state()
    done = state.setdefault(args.project, {})

    todo = [e for e in entries
            if f"{e['date']} · {e['title']}" not in done
            and len(e["body"]) >= args.min_body
            and (args.all or any(m in e["body"] for m in LESSON_MARKS))]
    if args.limit:
        todo = todo[: args.limit]
    print(f"文件里 {len(entries)} 条，已灌 {len(done)} 条，本次待灌 {len(todo)} 条")

    if args.dry_run:
        for e in todo[:10]:
            print(f"  · {e['date']} {e['title'][:30]}（正文 {len(e['body'])} 字）")
        if len(todo) > 10:
            print(f"  …共 {len(todo)} 条")
        return 0

    from store import Store          # noqa: E402
    import embed                      # noqa: E402

    s = Store()
    ok = fail = 0
    for i in range(0, len(todo), 8):
        batch = todo[i:i + 8]
        texts, ids = [], []
        for e in batch:
            key = f"{e['date']} · {e['title']}"
            title = f"99 · {e['title']}"[:60]
            content = (f"{condense(e['body'])}\n\n"
                       f"[出处] docs/99-变更记录.md · {e['date']}")
            try:
                mid, created = s.remember(args.project, args.kind, title, content)
            except Exception as ex:  # noqa: BLE001
                print(f"  写库失败 {key}: {type(ex).__name__}: {ex}")
                fail += 1
                continue
            done[key] = mid
            if created:
                texts.append(f"{title}\n{content}")
                ids.append(mid)
            ok += 1
        if texts:
            try:
                vecs = embed.embed(texts)
                if vecs and len(vecs) == len(ids):
                    for mid, v in zip(ids, vecs):
                        s.add_vector(mid, v, embed.MODEL_NAME)
            except Exception as ex:  # noqa: BLE001
                print(f"  向量化失败（不影响入库）：{type(ex).__name__}: {ex}")
        save_state(state)
        print(f"  进度 {min(i + 8, len(todo))}/{len(todo)}")

    save_state(state)
    print(f"完成：入库 {ok} 条，失败 {fail} 条")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
