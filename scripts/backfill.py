"""一次性补录历史会话到记忆库。

和 watch_archive.py 的区别：
- 守护进程只处理「基线之后」的新会话（默认不回溯，避免第一次就把几个月的历史全灌进去）；
- 这个脚本就是用来**主动回溯**的，默认 force=True（已归档的会话也重新抽一遍）。

用法：
    .venv\\Scripts\\python.exe scripts\\backfill.py            # 全部会话
    .venv\\Scripts\\python.exe scripts\\backfill.py --limit 20 # 最多 20 个
    .venv\\Scripts\\python.exe scripts\\backfill.py --project dsh-rag
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import archive  # noqa: E402
from store import Store  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--project", default=None)
    ap.add_argument("--no-force", action="store_true", help="跳过已归档的会话")
    args = ap.parse_args()

    s = Store()
    msg = archive.run(
        store=s,
        project_filter=args.project,
        limit=args.limit,
        force=not args.no_force,
    )
    print(msg)
    st = s.stats()
    print("记忆:", st["total"], "生效:", st["active"], "会话台账:", st["sessions"])
    print("按类型:", "，".join(f"{k}={v}" for k, v in st["kinds"].items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
