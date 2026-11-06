import sys, os, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
tmp = os.path.join(tempfile.gettempdir(), "dshrag_archive_test.db")
for suf in ("", "-wal", "-shm"):
    try: os.remove(tmp + suf)
    except OSError: pass
os.environ["DSH_RAG_DB"] = tmp
os.environ["DSH_RAG_EMBED"] = "off"      # 自测不联网、不花钱
import importlib, store, archive, embed
importlib.reload(store)
s = store.Store(tmp)
print("== 归档 3 个会话 ==")
print(archive.run(store=s, limit=3))
print("\n== 项目分布 ==")
for p in s.projects(): print("  ", p)
print("\n== 向量检索: 浏览器插件 ==")
for m in s.recall("浏览器插件相关的工作", limit=3):
    print(f"  {m.score:.4f} [{m.project}] {m.title}")
print("\n== 一个会话摘要样例 ==")
r = s.recent(limit=1)[0]
print("  project:", r.project)
print("  ", r.content[:600].replace("\n","\n  "))
