"""项目实体的唯一来源：`PROJECTS_ROOT`（默认 `~/Documents/Projects`）下的真实目录。

为什么要有这一层：
- 以前 project 只是 memories 表里的自由字符串，agent 写 "DSH-RAG" 和 "dsh-rag"
  会分裂成两个项目，交接卡也就各存各的；
- 现在以目录名为准，写入前先归一，保证同一个项目只有一个名字。

约定：
- 目录名（去掉 `_` 开头的缓存/工具目录）就是权威项目名；
- `工作区` / `未归类` 是保留名，不对应任何目录（零散会话、DSH 自身杂事）；
- 目录不存在时保留原名，但会 trim、压缩空格。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import paths

PROJECTS_ROOT = paths.PROJECTS_ROOT
RESERVED = ("工作区", "未归类")
ALIAS_FILE = paths.REPO_ROOT / "data" / "project-aliases.json"

_CACHE: dict[str, Any] = {"at": 0.0, "dirs": []}
_TTL = 60.0


def scan_dirs(force: bool = False) -> list[str]:
    """Projects 下的真实项目目录名（排除 _cache 之类的辅助目录）。"""
    now = time.time()
    if not force and now - _CACHE["at"] < _TTL:
        return _CACHE["dirs"]
    out: list[str] = []
    try:
        for p in PROJECTS_ROOT.iterdir():
            if not p.is_dir():
                continue
            name = p.name
            if name.startswith(("_", ".")):
                continue
            out.append(name)
    except OSError:
        out = []
    out.sort()
    _CACHE["dirs"] = out
    _CACHE["at"] = now
    return out


def aliases() -> dict[str, str]:
    """可选的人工别名表：{"记忆库": "dsh-rag"}。文件不存在就是空表。"""
    try:
        raw = json.loads(ALIAS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k).strip(): str(v).strip() for k, v in raw.items() if str(v).strip()}


def normalize(name: str | None, known: list[str] | None = None) -> str:
    """把任意写法收敛成一个确定的项目名。

    顺序：去空白 → 压空格 → 别名表 → 大小写不敏感匹配（已知项目 ∪ 真实目录）→ 原样返回

    `known` 由调用方传入数据库里已存在的项目名。没有这一步的话，
    一个还没建目录的项目（比如只存在于记忆里的）换个大小写就会分裂成两个。
    """
    s = re.sub(r"\s+", " ", str(name or "").strip())
    if not s:
        return "未归类"
    if s in RESERVED:
        return s

    amap = aliases()
    if s in amap:
        s = amap[s]

    low = s.lower()
    pool: list[str] = []
    for x in list(known or []) + scan_dirs():
        if x and x not in pool:
            pool.append(x)
    for d in pool:
        if d.lower() == low:
            return d
    return s


def resolve_known(candidate: str, pool: list[str] | None = None) -> str | None:
    """把一个疑似项目名收敛到已知项目。

    归档是**从文本里猜**项目名的，猜出来经常是残缺的：
    `Projects\\my-api\\...` 会得到 `my-api`，而真实目录是 `my-api-server`；
    路径里带文件时还会得到 `README.md`。

    规则：
    - 带点的（文件名/域名）直接判负；
    - 精确匹配（大小写不敏感）优先；
    - 否则做**唯一**前缀匹配（`my-api` → `my-api-server`），多个候选时判负——
      宁可落到「工作区」，也不要瞎合并两个项目。
    """
    cand = (candidate or "").strip()
    if not cand or "." in cand:
        return None
    pool = [p for p in (pool or []) + scan_dirs() if p]
    low = cand.lower()
    for name in pool:
        if name.lower() == low:
            return name
    prefixed = [n for n in pool if n.lower().startswith(low)]
    if len(prefixed) == 1:
        return prefixed[0]
    return None


def project_dir(name: str) -> Path | None:
    """返回项目的目录（存在且可写才算）。保留名和未建目录的项目返回 None。"""
    if name in RESERVED:
        return None
    p = PROJECTS_ROOT / name
    try:
        return p if p.is_dir() else None
    except OSError:
        return None


def handoff_path(name: str) -> Path:
    """交接卡 md 的落盘位置。

    有项目目录 → `Projects\\<名>\\交接卡.md`（人可读、可纳入版本管理）
    没有 → 退回 `DSH_RAG_HANDOFFS_ROOT`（默认 data/handoffs），保证每个项目都有导出物

    两条路径都可被环境变量改：自测要用临时库，但导出是文件系统副作用，
    只隔离数据库挡不住它（见 paths.HANDOFFS_ROOT 的注释）。
    """
    d = project_dir(name)
    if d is not None:
        return d / "交接卡.md"
    safe = re.sub(r"[\\/:*?\"<>|]", "_", name) or "未命名"
    return paths.HANDOFFS_ROOT / f"{safe}.md"


def write_handoff_md(name: str, content: str, updated_at: str) -> tuple[bool, str]:
    """把交接卡写成 markdown。返回 (是否写入, 路径或原因)。"""
    path = handoff_path(name)
    body = (
        f"# {name} · 交接卡\n\n"
        f"> 更新于 {updated_at}　·　由 dsh-rag 自动生成，手改会在下次写入时被覆盖\n"
        f"> 数据源：`Projects\\dsh-rag\\data\\memory.db`\n\n"
        f"---\n\n"
        f"{content.strip()}\n"
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8", newline="\n")
        return True, str(path)
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"
