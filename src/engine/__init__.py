"""通用记忆引擎的边界层（P0）。

这里只放**契约与纯函数**，不放实现；实现仍留在 src/ 顶层，等 P2 逐步收编。

依赖方向（由 scripts/check_imports.py 守护）：

    engine    ← 不 import 任何宿主/适配器
    adapters  → 可以 import engine（archive / projects / insight 将来落这里）
    host      → 可以 import 一切（cli / server）
"""
