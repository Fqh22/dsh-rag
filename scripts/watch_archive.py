"""会话归档守护：定期把结束的 DSH 会话抽成记忆入库。

为什么需要它：MCP server 只在 DSH 调用工具时才跑，而"会话结束后自动记一笔"
不是工具能触发的时机。这个进程独立常驻，负责扫盘。

基线机制：
- 首次启动把当前时间写入 data/watch-state.json 的 archive_from；
- 之后只处理 mtime >= archive_from 的会话，历史存量默认不碰（对应"不回溯旧会话"的选择）；
- 想补录历史：删掉该文件，或给 archive.run 传参手动跑。

日志：logs/archive.log（按天切分不必要，单文件滚动即可）。
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

STATE = ROOT / "data" / "watch-state.json"
LOG = ROOT / "logs" / "archive.log"
INTERVAL = int(os.environ.get("DSH_RAG_SCAN_SECONDS") or 300)


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().astimezone().isoformat(timespec="seconds")
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{ts}  {msg}\n")


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    state = {"archive_from": time.time(), "created": datetime.now().isoformat()}
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state


def scan_once() -> str:
    import archive
    import store as store_mod

    st = load_state()
    cutoff = float(st["archive_from"])
    s = store_mod.Store()
    return archive.run(store=s, limit=10, min_mtime=cutoff)


def main() -> None:
    log(f"watcher start, interval={INTERVAL}s, root={ROOT}")
    while True:
        try:
            msg = scan_once()
            if msg:
                log(msg)
        except Exception:  # noqa: BLE001
            log("ERROR " + traceback.format_exc().replace("\n", " | "))
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
