"""项目维度自测：归一、总览、交接卡落 md。

用临时库 + 临时项目名，不碰正式数据；产生的 md 会在结束时删掉。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

tmp = Path(tempfile.mkdtemp(prefix="dshrag-proj-"))
os.environ["DSH_RAG_DB"] = str(tmp / "t.db")

import projects as P  # noqa: E402
from store import Store  # noqa: E402

TEST_PROJ = "__selftest_proj__"
made: list[Path] = []


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

    print("== 项目名归一 ==")

    def t1():
        assert P.normalize("dsh-rag") == "dsh-rag", "已知项目应保持原名"

    check("已知项目原样保留", t1)

    def t2():
        assert P.normalize("DSH-RAG") == "dsh-rag", f"大小写没归一: {P.normalize('DSH-RAG')}"

    check("大小写不敏感匹配目录", t2)

    def t3():
        assert P.normalize("  dsh-rag  ") == "dsh-rag", "首尾空格没去掉"

    check("去首尾空格", t3)

    def t4():
        assert P.normalize("qq   bot") == "qq bot", f"内部空格没压缩: {P.normalize('qq   bot')}"

    check("压缩内部空格", t4)

    def t5():
        assert P.normalize("") == "未归类"
        assert P.normalize(None) == "未归类"

    check("空值兜底为未归类", t5)

    def t6():
        assert P.normalize("工作区") == "工作区", "保留名不应被改写"

    check("保留名不被改写", t6)

    def t7():
        # 未建目录的项目保留原名，但也做规范化
        assert P.normalize("  某个新项目  ") == "某个新项目"

    check("未知项目保留原名", t7)

    print("== 交接卡按项目 ==")
    s = Store()

    def t8():
        s.put_handoff(TEST_PROJ, "第一版内容")
        md = P.handoff_path(TEST_PROJ)
        made.append(md)
        assert md.is_file(), f"md 没生成: {md}"

    check("写交接卡会落 md", t8)

    def t9():
        m = s.get_handoff(TEST_PROJ)
        assert m is not None and "第一版" in m.content

    check("能按项目读回交接卡", t9)

    def t10():
        s.put_handoff(TEST_PROJ, "第二版内容")
        actives = s._conn.execute(
            "SELECT COUNT(*) c FROM memories WHERE project=? AND kind='handoff' AND status='active'",
            (TEST_PROJ,),
        ).fetchone()["c"]
        assert actives == 1, f"同时存活的交接卡应为 1，实际 {actives}"

    check("同项目只保留一张生效卡", t10)

    def t11():
        m = s.get_handoff(TEST_PROJ)
        assert "第二版" in m.content, "读到的不是最新的"

    check("读到的是最新那张", t11)

    def t12():
        # 大小写变体不能分裂出新项目
        s.put_handoff(TEST_PROJ.upper(), "第三版内容")
        n = s._conn.execute(
            "SELECT COUNT(DISTINCT project) c FROM memories WHERE lower(project)=lower(?)",
            (TEST_PROJ,),
        ).fetchone()["c"]
        assert n == 1, f"同一项目分裂成了 {n} 个"

    check("大小写变体不分裂项目", t12)

    def t12b():
        # 内容完全相同再写一次：不能出现「一张生效的卡都没有」
        s.put_handoff(TEST_PROJ, "第三版内容")
        actives = s._conn.execute(
            "SELECT COUNT(*) c FROM memories WHERE lower(project)=lower(?) "
            "AND kind='handoff' AND status='active'",
            (TEST_PROJ,),
        ).fetchone()["c"]
        assert actives == 1, f"重复写入相同内容后生效卡数量为 {actives}"

    check("重复写入相同内容仍保持一张生效卡", t12b)

    print("== 总览 ==")

    def t13():
        ov = s.overview()
        mine = [x for x in ov if x["project"].lower() == TEST_PROJ.lower()]
        assert mine, "总览里没有测试项目"
        assert mine[0]["handoff"] is not None, "总览里交接卡摘要为空"

    check("总览包含测试项目且带卡摘要", t13)

    def t14():
        ov = s.overview()
        names = {x["project"] for x in ov}
        # Projects 目录下的真实项目应该都出现
        for d in P.scan_dirs():
            assert d in names, f"总览漏了目录项目 {d}"

    check("总览包含 Projects 下所有目录", t14)

    def t15():
        ov = s.overview()
        first_has = ov[0]["handoff"] is not None
        assert first_has, "有交接卡的项目没有排在最前"

    check("有卡的项目排在前面", t15)

    # 清理
    for p in made:
        try:
            p.unlink()
        except OSError:
            pass

    print()
    print("结果:", "全部通过" if failed == 0 else f"{failed} 项失败")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
