"""重建全部向量（换 embedding 后端后必须跑一次）。

用法：
  .venv/Scripts/python.exe scripts/reembed.py            # 重建并清掉其它模型的旧向量
  .venv/Scripts/python.exe scripts/reembed.py --keep-old # 保留旧向量（排查问题时用）

为什么必须重建：不同后端的向量维度/空间不同，混在一张表里做余弦相似度是没有意义的。
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
    keep_old = "--keep-old" in sys.argv

    info = embed.probe()
    print("后端:", info["backend"])
    print("模型:", info["model"], "维度:", info["dim"])
    print("key :", "有" if info["hasKey"] else "缺失")
    if info["error"]:
        print("自检失败:", info["error"])
        return 1
    if info["sampleDim"] != info["dim"]:
        print(f"维度不符：期望 {info['dim']}，实际 {info['sampleDim']}")
        return 1
    print("自检通过，返回维度", info["sampleDim"])

    s = Store()
    rows = s._conn.execute(
        "SELECT id, title, content FROM memories ORDER BY id"
    ).fetchall()
    print(f"待重建 {len(rows)} 条")

    ok = 0
    failed = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i : i + BATCH]
        texts = [f"{r['title']}\n{r['content']}" for r in chunk]
        vecs = embed.embed(texts)
        if not vecs:
            print(f"  批次 {i}-{i+len(chunk)} 失败: {embed.error()}")
            failed += len(chunk)
            continue
        for r, v in zip(chunk, vecs):
            s.add_vector(int(r["id"]), v, embed.MODEL_NAME)
            ok += 1
        print(f"  已完成 {ok}/{len(rows)}")

    if not keep_old:
        cur = s._conn.execute(
            "DELETE FROM vectors WHERE model != ?", (embed.MODEL_NAME,)
        )
        s._conn.commit()
        print(f"已清除其它模型的旧向量 {cur.rowcount} 条")

    s._invalidate_vec_cache()
    stats = s.stats()
    print(f"\n完成：成功 {ok}，失败 {failed}")
    print("统计:", stats)

    # 抽样验证：用一句话检索，看是否命中语义相近的条目
    test_q = "向量库用什么方案"
    hits = s.recall(test_q, limit=3)
    print(f"\n抽样检索「{test_q}」:")
    for m in hits:
        print(f"  {m.score:.5f}  [{m.project}] {m.title}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
