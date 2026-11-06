"""「修改」功能自测：改完之后 FTS、向量、去重都必须跟着走。

用临时库跑，不碰正式数据。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

tmp = Path(tempfile.mkdtemp(prefix="dshrag-upd-"))
os.environ["DSH_RAG_DB"] = str(tmp / "t.db")
# 这里**不能**设 DSH_RAG_EMBED=off：本测试要验证「改完向量按新内容重算」，
# 关掉 embedding 就只剩关键词，那两条断言会假失败。
sys.path.insert(0, str(SRC))

import embed  # noqa: E402
from store import Store  # noqa: E402


def main() -> int:
    failed = 0

    def check(name, fn):
        nonlocal failed
        try:
            fn()
            print("  ✓", name)
        except AssertionError as e:
            failed += 1
            print("  ✗", name, "->", e)

    s = Store()
    mid, _ = s.remember("proj", "fact", "旧标题", "这是旧的内容，讲的是 python 异步编程。")
    other, _ = s.remember("proj", "fact", "另一条", "另一条完全不同的内容，讲的是数据库索引。")

    for m in (mid, other):
        v = embed.embed([f"x{m}"])
        if v:
            s.add_vector(m, v[0], embed.MODEL_NAME)

    print("== 修改标题和内容 ==")

    before_vec = s._conn.execute(
        "SELECT vec FROM vectors WHERE memory_id=?", (mid,)
    ).fetchone()
    before_bytes = bytes(before_vec["vec"]) if before_vec else None

    def t1():
        ok, msg = s.update_memory(mid, title="新标题", content="改成了协程和事件循环的内容。")
        assert ok, f"更新失败: {msg}"

    check("update_memory 返回成功", t1)

    def t2():
        m = s.get_memory(mid)
        assert m is not None, "取不到"
        assert m.title == "新标题", f"标题没变: {m.title}"
        assert "协程" in m.content, f"内容没变: {m.content}"

    check("内容真的改了", t2)

    print("== 索引同步 ==")

    def t3():
        hits = s.recall("协程", project="proj", limit=5)
        assert any(m.id == mid for m in hits), "改完用新内容搜不到"

    check("FTS 搜得到新内容", t3)

    def t4():
        # 直接测 FTS，避开向量召回（向量是语义的，本来就可能命中相近语义）
        hits = s._keyword_search("异步编程", 10)
        assert all(h[0] != mid for h in hits), "旧内容还能被关键词搜到，FTS 没更新"

    check("FTS 搜不到旧内容", t4)

    def t4b():
        # 「异步编程」和「协程」语义相近，召回本身是对的，不能拿它判对错。
        # 真正的判据是向量字节变了没有。
        after = s._conn.execute(
            "SELECT vec FROM vectors WHERE memory_id=?", (mid,)
        ).fetchone()
        assert after is not None, "向量记录没了"
        assert before_bytes is not None, "更新前就没有向量"
        assert bytes(after["vec"]) != before_bytes, "向量没重算"

    check("向量已按新内容重算", t4b)

    def t5():
        rows = s._conn.execute(
            "SELECT dim FROM vectors WHERE memory_id=?", (mid,)
        ).fetchall()
        assert rows, "向量被删没了"

    check("向量记录仍存在", t5)

    print("== 边界情况 ==")

    def t6():
        ok, msg = s.update_memory(mid, title="新标题", content="改成了协程和事件循环的内容。")
        assert ok and "没有变化" in msg, f"期望无变化，实际: {ok} {msg}"

    check("内容没变时幂等", t6)

    def t7():
        ok, msg = s.update_memory(mid, title="另一条", content="另一条完全不同的内容，讲的是数据库索引。")
        assert not ok, "应该因为重复被拒"

    check("改成与他条重复会被拒", t7)

    def t8():
        ok, msg = s.update_memory(999999, title="x")
        assert not ok, "不存在的 id 应该失败"

    check("不存在的 id 返回失败", t8)

    def t9():
        ok, _ = s.update_memory(mid, title="   ")
        m = s.get_memory(mid)
        assert ok and m.title == "(无标题)", f"空标题没兜底: {m.title}"

    check("空标题兜底", t9)

    print("== 改完后的检索融合 ==")

    def t10():
        hits = s.recall("事件循环", project="proj", limit=5)
        assert hits and hits[0].id == mid, "改完的条目没排第一"

    check("改完仍能被语义召回", t10)

    print()
    print("结果:", "全部通过" if failed == 0 else f"{failed} 项失败")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
