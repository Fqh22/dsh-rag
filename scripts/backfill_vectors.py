"""只补「没有当前模型向量」的记忆，不动已有的。

和 reembed.py 的区别：
- reembed 是换后端后全量重建（对每条都调一次 embedding，费钱）；
- 这里只挑缺的补，日常运维用这个。

用法：
  .venv/Scripts/python.exe scripts/backfill_vectors.py
  .venv/Scripts/python.exe scripts/backfill_vectors.py --dry-run   # 只列不写

典型场景：MCP 服务在跑旧代码、库已经升到 v5，写入的记忆没向量；
或换过 embedding 后端，旧模型的向量按当前模型算「缺」。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import embed  # noqa: E402
from store import Store  # noqa: E402

BATCH = 16


def main() -> int:
    dry = "--dry-run" in sys.argv

    s = Store()
    rows = s.missing_vectors(embed.MODEL_NAME)
    print(f"模型: {embed.MODEL_NAME}")
    print(f"缺向量的生效记忆: {len(rows)} 条")
    for r in rows[:20]:
        print(f"  id={r['id']:<4} [{r['project']}] {r['kind']:<10} {r['title'][:40]}")
    if len(rows) > 20:
        print(f"  ... 另有 {len(rows) - 20} 条")
    if not rows or dry:
        return 0

    if not embed.probe()["hasKey"]:
        print("没有可用的 embedding 凭据，先配 key 再跑；关键词检索不受影响。")
        return 1

    ok = failed = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i : i + BATCH]
        texts = [f"{r['title']}\n{r['content']}" for r in chunk]
        vecs = embed.embed(texts)
        if not vecs:
            print(f"  批次 {i}-{i + len(chunk)} 失败: {embed.error()}")
            failed += len(chunk)
            continue
        for r, v in zip(chunk, vecs):
            s.add_vector(int(r["id"]), v, embed.MODEL_NAME)
            ok += 1
        print(f"  已补 {ok}/{len(rows)}")

    stats = s.stats(embed.MODEL_NAME)
    print(f"\n完成：成功 {ok}，失败 {failed}")
    print(f"统计：记忆 {stats['total']}，向量 {stats['vectors']}，缺 {stats['missing_vectors']}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
