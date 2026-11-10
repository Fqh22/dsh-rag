"""Markdown 会话源（P3）：`SessionSource` 的第二个实现。

存在的意义**不是**多支持一种格式，而是证明契约不是为 DSH 定制的：
如果 `SessionSource` 只能描述 `session.v4.jsonl.zstd`，那它就不是契约，只是 DSH 解析器的别名。

约定格式（一个 `.md` = 一个会话）：

    # 会话标题
    ## user
    问题……
    ## assistant
    回答……
    ## tool
    read README.md

`## ` 后面是角色（user / assistant / tool / compact 都行），正文是那一轮的文本。

状态：**实验性，未接入 archive.run**。它只用来跑契约测试；真要导入 Markdown 历史时，
再决定是走 `import-md` 命令还是让 archive 接受可插拔的 source。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

from engine.contracts import SessionRef, SessionTurn

_SECTION = re.compile(r"^##\s+([A-Za-z][\w-]*)\s*$")
_TITLE = re.compile(r"^#\s+(.+?)\s*$")


class MarkdownSessionSource:
    """把按约定写成的 Markdown 当成会话。"""

    name = "markdown"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def scan(self, since: float | None = None) -> list[SessionRef]:
        if not self.root.exists():
            return []
        out: list[SessionRef] = []
        for p in sorted(self.root.rglob("*.md")):
            try:
                st = p.stat()
            except OSError:
                continue
            if since is not None and st.st_mtime < since:
                continue
            rid = str(p.relative_to(self.root)).replace("\\", "/")
            out.append(SessionRef(id=rid, path=str(p), mtime=st.st_mtime, namespace=self.root.name))
        return out

    def read(self, ref: SessionRef) -> Iterator[SessionTurn]:
        text = Path(ref.path).read_text(encoding="utf-8", errors="replace")
        turns: list[SessionTurn] = []
        role: str | None = None
        buf: list[str] = []

        def flush() -> None:
            body = "\n".join(buf).strip()
            if role and body:
                turns.append(SessionTurn(role=role, text=body, kind=f"md/{role}"))

        for line in text.splitlines():
            m = _SECTION.match(line)
            if m:
                flush()
                role, buf = m.group(1).lower(), []
                continue
            if _TITLE.match(line) and role is None:
                continue          # # 标题 只是元信息，不进轮次
            buf.append(line)
        flush()
        return iter(turns)
