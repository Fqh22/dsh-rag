"""一次性补录历史会话到记忆库。

和 watch_archive.py 的区别：
- 守护进程只处理「基线之后」的新会话（默认不回溯，避免第一次就把几个月的历史全灌进去）；
- 这个脚本就是用来**主动回溯**的。

默认不重抽已归档的会话（只补漏掉的）。要重抽用 --force：它会把同一会话的旧摘要
置为 superseded 再写新的，并在动手前备份整库 —— 以前默认 force，跑一次就留一堆
重复的 active 摘要（2026-11-06 审视）。

用法：
    .venv\\Scripts\\python.exe scripts\\backfill.py                    # 只补没归档的
    .venv\\Scripts\\python.exe scripts\\backfill.py --dry-run          # 先看看会处理谁
    .venv\\Scripts\\python.exe scripts\\backfill.py --force            # 已归档的也重抽（先备份）
    .venv\\Scripts\\python.exe scripts\\backfill.py --project dsh-rag --limit 20
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import archive  # noqa: E402
from store import Store  # noqa: E402


def _pending(store, force: bool) -> list[str]:
    """按 archive.run 的口径列出「会被处理」的会话。

    不含 --project 过滤：那要解析会话内容才知道归属，dry-run 不值得付这个成本。
    """
    out: list[str] = []
    for f in archive.iter_session_files(None):
        sid = f.parent.name
        if not force and store.session_done(sid):
            continue
        out.append(sid)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--project", default=None)
    ap.add_argument("--force", action="store_true",
                    help="已归档的会话也重新抽一遍（会作废旧摘要，且先备份整库）")
    ap.add_argument("--dry-run", action="store_true", help="只列出要处理的会话，不写库")
    ap.add_argument("--no-force", action="store_true", help=argparse.SUPPRESS)  # 兼容旧用法
    args = ap.parse_args()
    force = bool(args.force)

    s = Store()

    if args.dry_run:
        pending = _pending(s, force)
        for sid in pending[: args.limit]:
            print("将处理:", sid)
        print(f"共 {len(pending)} 个会话（force={force}）；dry-run 未写库")
        return 0

    backup_path = None
    if force:
        import cleanup

        backup_path = cleanup.backup(s)
        print("已备份:", backup_path)

    msg = archive.run(
        store=s,
        project_filter=args.project,
        limit=args.limit,
        force=force,
    )
    print(msg)
    st = s.stats()
    print("记忆:", st["total"], "生效:", st["active"], "会话台账:", st["sessions"])
    print("按类型:", "，".join(f"{k}={v}" for k, v in st["kinds"].items()))
    if backup_path:
        print("回滚：用上面的备份覆盖 data/memory.db（先停 DSH）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
