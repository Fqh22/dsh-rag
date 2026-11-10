# 开发、测试与贡献


全部自测都用临时库 + 临时目录，不会碰 `data/memory.db`：

```powershell
# 单元 / 集成
.\.venv\Scripts\python.exe scripts\selftest_pack.py
.\.venv\Scripts\python.exe scripts\selftest_v6.py
.\.venv\Scripts\python.exe scripts\selftest_store.py
.\.venv\Scripts\python.exe scripts\selftest_archive.py

# MCP 端到端（起一个 server 子进程，走完整协议）
.\.venv\Scripts\python.exe scripts\selftest_mcp.py

# 插件静态自测（mock 宿主，不需要开 DSH）
node scripts\selftest_plugin.mjs
```

自测脚本在 import `store` 之前会设 `DSH_RAG_PROJECTS_ROOT` / `DSH_RAG_HANDOFFS_ROOT` /
`DSH_RAG_EMBED=off`——只隔离 `DSH_RAG_DB` 是不够的，`put_handoff` 会写磁盘。

改完 `src/*.py` 之后**必须重启 DSH**：MCP server 是常驻进程，盘上的新代码它不会自己加载。
CLI 和脚本没有这个限制。

欢迎 issue / PR。有两点约定：

1. **改库只走 `Store`**，不要在别处直接 `conn.execute`（页面层例外，它有独立事务）。
2. **自测必须临时库**；涉及文件系统副作用的，把三个路径变量都指到临时目录。
