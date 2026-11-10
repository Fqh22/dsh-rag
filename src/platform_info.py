"""平台差异收编（P4）：把「这台机器是什么样」收进一个模块。

为什么要单独一层：核心不该知道注册表、`~/.config`、Windows 与非 Windows 的区别。
目前真正平台相关的只有一件事——**密钥从哪读**：

    Windows  环境变量 → 注册表 HKCU/HKLM\\Environment → 调用方给的文件
    POSIX    环境变量 → ~/.config/dsh-rag/secrets.env  → 调用方给的文件

路径解析已经在 paths.py；自启安装是 scripts/ 里的事，不进核心。

模块名带 `_info` 后缀：避免和标准库 `platform` 撞名。`src` 在 sys.path 最前面，
真撞了会静默替换掉标准库模块，是那种「换个机器才炸」的坑。

依赖：零内部依赖（scripts/check_imports.py 把 platform_info 列为核心层）。
"""
from __future__ import annotations

import os
from pathlib import Path

IS_WINDOWS = os.name == "nt"

# POSIX 下的密钥文件（一行 KEY=VALUE，# 注释）
POSIX_SECRET_FILE = Path.home() / ".config" / "dsh-rag" / "secrets.env"


def _from_env(names: tuple[str, ...]) -> str | None:
    for name in names:
        v = os.environ.get(name)
        if v and v.strip():
            return v.strip()
    return None


def _from_windows_registry(names: tuple[str, ...]) -> str | None:
    """DSH 桌面端是登录时启动的，未必继承后来设置的用户环境变量，所以回退到注册表。"""
    try:
        import winreg
    except ImportError:
        return None
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            key = winreg.OpenKey(root, r"Environment")
        except OSError:
            continue
        for name in names:
            try:
                val, _ = winreg.QueryValueEx(key, name)
                if val and str(val).strip():
                    return str(val).strip()
            except OSError:
                continue
    return None


def _from_posix_secret_file(names: tuple[str, ...]) -> str | None:
    if not POSIX_SECRET_FILE.is_file():
        return None
    try:
        lines = POSIX_SECRET_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    wanted = set(names)
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        if k.strip() in wanted and v.strip():
            return v.strip()
    return None


def _from_file(path: Path | str | None) -> str | None:
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    try:
        v = p.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return v or None


def get_secret(*names: str, fallback_file: Path | str | None = None) -> str | None:
    """按顺序找密钥：环境变量 → 平台存储 → 调用方给的文件。都找不到返回 None。"""
    if not names:
        return None
    v = _from_env(names)
    if v:
        return v
    v = _from_windows_registry(names) if IS_WINDOWS else _from_posix_secret_file(names)
    if v:
        return v
    return _from_file(fallback_file)


def describe_sources(*names: str) -> list[str]:
    """给报错用的「我找过哪些地方」。"""
    places = [f"环境变量 {' / '.join(names)}"]
    if IS_WINDOWS:
        places.append("注册表 HKCU/HKLM\\Environment")
    else:
        places.append(str(POSIX_SECRET_FILE))
    return places
