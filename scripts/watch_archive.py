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
INGEST_DAY = ROOT / "data" / "ingest-day.json"
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


def maybe_ingest() -> str | None:
    """每天一次：把各项目的变更记录增量灌进记忆库（不调模型，只补新的）。

    放在守护进程里而不是让人手动跑——手动步骤迟早会忘，忘了记忆库就落后于代码。
    单项目单次上限 10 条，避免某个项目一次灌几百条把记忆库冲掉。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        st = json.loads(INGEST_DAY.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        st = {}
    if st.get("day") == today:
        return None
    import changelog as cl
    from store import Store

    s = Store()
    cooked = []
    for r in cl.ingest_all(s, limit=10):
        if r.get("added"):
            cooked.append(f"{r['project']}+{r['added']}")
    INGEST_DAY.parent.mkdir(parents=True, exist_ok=True)
    INGEST_DAY.write_text(json.dumps({"day": today}), encoding="utf-8")
    return ("变更记录灌入: " + ", ".join(cooked)) if cooked else None


def main() -> None:
    log(f"watcher start, interval={INTERVAL}s, root={ROOT}")
    while True:
        try:
            msg = scan_once()
            if msg:
                log(msg)
        except Exception:  # noqa: BLE001
            log("ERROR " + traceback.format_exc().replace("\n", " | "))
        try:
            msg = maybe_ingest()
            if msg:
                log(msg)
        except Exception:  # noqa: BLE001
            log("INGEST ERROR " + traceback.format_exc().replace("\n", " | "))
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
