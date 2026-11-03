r"""命令行入口：把项目变更记录灌进记忆库（实现在 src/changelog.py）。

    .venv\Scripts\python.exe scripts\ingest_changelog.py --dry-run
    .venv\Scripts\python.exe scripts\ingest_changelog.py              # 所有项目
    .venv\Scripts\python.exe scripts\ingest_changelog.py --project qq-cloud-bot
    .venv\Scripts\python.exe scripts\ingest_changelog.py --file 路径\CHANGELOG.md

等价于 `python src/cli.py ingest …`；保留本文件是为了兼容旧命令。
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

import cli  # noqa: E402

if __name__ == "__main__":
    sys.argv = ["cli.py", "ingest"] + sys.argv[1:]
    raise SystemExit(cli.main())
