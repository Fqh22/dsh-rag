"""Markdown 会话源自测（P3）：证明 SessionSource 契约可移植。

跑法：.venv\\Scripts\\python.exe scripts\\selftest_session_markdown.py
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from adapters.session_markdown import MarkdownSessionSource  # noqa: E402
from engine.contracts import SessionSource, SessionTurn  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "sessions_md"
PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  ✓", name)
    else:
        FAIL += 1
        print("  ✗", name, "|", detail)


def main() -> int:
    src = MarkdownSessionSource(FIXTURE)
    print("== 协议 ==")
    check("MarkdownSessionSource 满足 SessionSource", isinstance(src, SessionSource))
    check("name 可读", src.name == "markdown")

    print("\n== 扫描 ==")
    refs = src.scan()
    check("找到 1 个会话", len(refs) == 1, str(len(refs)))
    check("id 是相对路径", refs and refs[0].id == "demo.md", refs[0].id if refs else "")
    check("未来时间过滤掉全部", src.scan(since=time.time() + 3600) == [])
    check("空目录返回空", MarkdownSessionSource(Path(tempfile.mkdtemp())).scan() == [])

    print("\n== 读取 ==")
    turns = list(src.read(refs[0]))
    check("产出 3 轮", len(turns) == 3, str(len(turns)))
    check("角色顺序正确",
          [t.role for t in turns] == ["user", "assistant", "tool"],
          str([t.role for t in turns]))
    check("kind 带来源前缀", all(t.kind == f"md/{t.role}" for t in turns))
    check("标题不进轮次", all("一个手写会话" not in t.text for t in turns))
    check("正文完整", turns[0].text == "帮我看看 README 的结构。" if turns else False,
          turns[0].text if turns else "")
    check("tool 轮保留命令", turns[-1].text == "read README.md" if turns else False)

    print("\n== 与 DSH 源的形状一致 ==")
    check("返回的是 SessionTurn", all(isinstance(t, SessionTurn) for t in turns))
    check("SessionRef 字段可用", all(hasattr(refs[0], f) for f in ("id", "path", "mtime")))

    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
