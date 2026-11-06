import sys, tempfile, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# 隔离：临时库只能隔离数据库。put_handoff 会写磁盘，而导出路径来自
# PROJECTS_ROOT / HANDOFFS_ROOT —— 不隔离它会覆盖生产项目的交接卡
# （2026-11-05 就发生过：dsh-rag/交接卡.md 被测试字符串盖掉了）。
_iso = tempfile.mkdtemp(prefix="dshrag-selftest-")
os.environ["DSH_RAG_PROJECTS_ROOT"] = os.path.join(_iso, "projects")
os.environ["DSH_RAG_HANDOFFS_ROOT"] = os.path.join(_iso, "handoffs")
os.environ["DSH_RAG_EMBED"] = "off"          # 自测不联网、不花钱

from store import Store, tokenize_zh  # noqa: E402

print("tokenize:", tokenize_zh("搭建RAG记忆库，防止会话过长"))

tmp = os.path.join(tempfile.gettempdir(), "dshrag_test.db")
for suf in ("", "-wal", "-shm"):
    try: os.remove(tmp + suf)
    except OSError: pass

s = Store(tmp)
s.remember("dsh-rag", "decision", "向量库选型", "用 SQLite + numpy 暴力检索，不引入 Chroma 或 Qdrant，减少常驻服务。")
s.remember("dsh-rag", "pitfall", "中文全文检索踩坑", "FTS5 的 unicode61 不切中文，必须自己做 bigram 分词，否则检索完全失效。")
s.remember("demo", "fact", "示例配置位置", "示例项目的配置放在用户目录下的 demo 文件夹。")
s.put_handoff("dsh-rag", "当前进展：已完成存储层。下一步：MCP server。")

print("\n-- 关键词检索: 中文分词 --")
for m in s.recall("中文全文检索", limit=3):
    print(f"  {m.score:.4f} [{m.project}] {m.title}")

print("\n-- 关键词检索: 向量库 --")
for m in s.recall("用什么向量数据库", limit=3):
    print(f"  {m.score:.4f} [{m.project}] {m.title}")

print("\n-- 项目隔离 --")
for m in s.recall("配置", project="demo", limit=3):
    print(f"  [{m.project}] {m.title}")

print("\n-- 交接卡 --")
h = s.get_handoff("dsh-rag")
print("  ", h.title, "|", h.content[:50])

print("\n-- 去重 --")
mid, created = s.remember("dsh-rag", "decision", "向量库选型", "用 SQLite + numpy 暴力检索，不引入 Chroma 或 Qdrant，减少常驻服务。")
print("   created =", created, "id =", mid)

print("\n-- 统计 --")
print("  ", s.stats())

# ---- 重排：日志型记忆（changelog/session）不能挤满结果 ----
print("\n-- 重排：changelog 占比受限 --")
rt = os.path.join(tempfile.gettempdir(), "dshrag_rerank.db")
for suf in ("", "-wal", "-shm"):
    try:
        os.remove(rt + suf)
    except OSError:
        pass
s2 = Store(rt)
for i in range(12):
    s2.remember("p", "changelog", f"99 · 变更 {i}",
                f"变更记录第 {i} 条：输入防抖和知识库索引的改动，含实测数据与回滚方式。")
for i in range(6):
    s2.remember("p", "decision", f"决策 {i}",
                f"决策第 {i} 条：输入防抖改成语义队列，因为合并零命中、每条多等 1 秒。")
res = s2.recall("输入防抖 知识库索引", project="p", limit=8)
kinds = [m.kind for m in res]
print("   ", kinds)
assert kinds.count("changelog") <= 2, f"changelog 超限: {kinds}"
assert "decision" in kinds, f"决策被挤没了: {kinds}"

# 显式指定 kinds 时不做重排干预（那是精确查询）
only = s2.recall("输入防抖 知识库索引", project="p", limit=5, kinds=["changelog"])
print("   指定 kinds ->", len(only), "条")
assert len(only) == 5 and all(m.kind == "changelog" for m in only), only
print("   OK")
