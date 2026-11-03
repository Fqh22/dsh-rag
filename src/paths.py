"""路径解析：默认值按「用户主目录」推断，可用环境变量覆盖。

为什么要单独一层：这个工具最早是给一台机器写的，路径直接写死在源码里。
一旦要给别人用（或者放进公开仓库），写死的绝对路径既不可移植、也会泄露
本机用户名。现在所有「项目根目录」都从这里取，默认 `~/Documents/Projects`，
想换位置就设 `DSH_RAG_PROJECTS_ROOT`。
"""
from __future__ import annotations

import os
from pathlib import Path


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    if raw and raw.strip():
        return Path(raw.strip()).expanduser()
    return default


# 被记忆的项目都放在这个目录下，一个子目录 = 一个项目
PROJECTS_ROOT: Path = _env_path(
    "DSH_RAG_PROJECTS_ROOT",
    Path.home() / "Documents" / "Projects",
)

# 本仓库根目录（src/ 的上一级）
REPO_ROOT: Path = Path(__file__).resolve().parent.parent
