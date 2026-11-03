import sys, tempfile, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from store import Store, tokenize_zh

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
