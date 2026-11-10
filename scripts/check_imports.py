"""依赖方向检查（P0.5）。

目的：重构期间防止"核心层被反向依赖"。这类项目最常见的退化不是写错代码，而是
拆完一个月之后，新代码又悄悄 `import store` 进来了——没有约束，边界就会自己长回去。

规则：
1. **无循环 import**（模块级）。
2. **核心层纯净**：`paths` / `schema` / `card` / `embed` 不得 import 任何其它内部模块。
   这四个是通用引擎的地基，必须能被单独拿走。
3. **已知例外**记录在 BASELINE 里，不阻塞 CI；修掉之后要删掉对应条目（脚本会提示）。
   新增的越界由第 2 条覆盖，其余方向由约定 + review 兜底。

用法：
    .venv\\Scripts\\python.exe scripts\\check_imports.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

# 通用引擎的地基：期望它们零内部依赖
CORE = ("paths", "schema", "card", "embed", "engine")

# 已知的越界依赖：(from, to) -> 为什么留着、什么时候修
BASELINE: dict[tuple[str, str], str] = {
    ("store", "pages"): "store.__init__ 里替页面层建表；P2 收编时应由调用方装配",
}


def imports_of(path: Path, known: set[str]) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    deps: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                head = alias.name.split(".")[0]
                if head in known:
                    deps.add(head)
        elif isinstance(node, ast.ImportFrom) and node.module:
            head = node.module.split(".")[0]
            if head in known:
                deps.add(head)
    return deps


def find_cycles(edges: dict[str, set[str]]) -> list[list[str]]:
    """DFS 找环，返回环路径（按节点集合去重）。"""
    cycles: list[list[str]] = []
    seen: set[frozenset[str]] = set()
    state: dict[str, int] = {}          # 0 未访问 / 1 在栈上 / 2 已完成
    stack: list[str] = []

    def dfs(node: str) -> None:
        state[node] = 1
        stack.append(node)
        for nxt in sorted(edges.get(node, ())):
            if state.get(nxt, 0) == 0:
                dfs(nxt)
            elif state.get(nxt) == 1:
                i = stack.index(nxt)
                cycle = stack[i:] + [nxt]
                key = frozenset(cycle)
                if key not in seen:
                    seen.add(key)
                    cycles.append(cycle)
        stack.pop()
        state[node] = 2

    for mod in sorted(edges):
        if state.get(mod, 0) == 0:
            dfs(mod)
    return cycles


def main() -> int:
    files = sorted(SRC.glob("*.py"))
    known = {p.stem for p in files}
    edges = {p.stem: imports_of(p, known) for p in files}

    # 子包（目前只有 engine）：整包当作一个节点，依赖合并
    for sub in sorted(p for p in SRC.iterdir() if p.is_dir() and (p / "__init__.py").exists()):
        deps: set[str] = set()
        for p in sorted(sub.glob("*.py")):
            deps |= imports_of(p, known)
        edges[sub.name] = deps

    problems: list[str] = []
    notes: list[str] = []

    print(f"依赖检查：{len(edges)} 个单元，{sum(len(v) for v in edges.values())} 条内部依赖")

    cycles = find_cycles(edges)
    if cycles:
        problems.extend("循环依赖：" + " → ".join(c) for c in cycles)
    else:
        print("  ✓ 无循环依赖")

    core_bad = [(m, sorted(edges.get(m, ()))) for m in CORE if edges.get(m)]
    for mod, deps in core_bad:
        problems.append(f"核心层越界：{mod} 依赖了 {', '.join(deps)}")
    if not core_bad:
        print("  ✓ 核心层纯净：" + " / ".join(CORE))

    for (src, dst), why in sorted(BASELINE.items()):
        if dst in edges.get(src, ()):
            notes.append(f"已知例外 {src} → {dst}（{why}）")
        else:
            notes.append(f"BASELINE 里的 {src} → {dst} 已不存在，可以删掉这条")

    for note in notes:
        print("  ·", note)

    if problems:
        print("\n失败：")
        for p in problems:
            print("  ✗", p)
        return 1

    print("\n结果：通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
