"""重建全文索引（memories_fts）。

什么时候用：`doctor` 报「全文索引 缺 N 条 / 多 N 条」。正常写入路径（remember /
update_memory / checkpoint_put）都会同步维护 FTS，所以出现不一致通常是：
手改过库、进程被强杀、或从旧备份恢复了一半。

用法：
    .venv\\Scripts\\python.exe scripts\\rebuild_fts.py
    .venv\\Scripts\\python.exe scripts\\rebuild_fts.py --dry-run

重建是幂等的：先清空再按 memories 全量重算，不动 memories / vectors。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from store import Store, tokenize_zh  # noqa: E402

FTS_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts "
    "USING fts5(title_tok, content_tok, tokenize='unicode61')"
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    args = ap.parse_args()

    s = Store()
    with s._lock:
        conn = s._conn
        rows = conn.execute("SELECT id, title, content FROM memories").fetchall()
        if args.dry_run:
            print(f"将重建 {len(rows)} 条全文索引（dry-run，未写库）")
            return 0
        conn.execute(FTS_DDL)
        conn.execute("DELETE FROM memories_fts")
        for r in rows:
            conn.execute(
                "INSERT INTO memories_fts(rowid, title_tok, content_tok) VALUES (?,?,?)",
                (int(r["id"]), tokenize_zh(r["title"]), tokenize_zh(r["content"])),
            )
        conn.commit()
    print(f"已重建 {len(rows)} 条全文索引")
    return 0


if __name__ == "__main__":
    sys.exit(main())
