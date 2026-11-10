"""适配层：把宿主的实现接到引擎契约上。

依赖方向：
    adapters → engine / store / archive / projects      （允许）
    engine    → adapters / store / archive              （禁止，由 scripts/check_imports.py 守护）

P2 先落一个：`repository.SqliteRepository`（Repository 协议的 SQLite 实现）。
"""
