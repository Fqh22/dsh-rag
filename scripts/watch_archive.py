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

import paths  # noqa: E402

STATE = paths.DATA_ROOT / "watch-state.json"
INGEST_DAY = paths.DATA_ROOT / "ingest-day.json"
LOCK = paths.DATA_ROOT / "watch.lock"
LOG = ROOT / "logs" / "archive.log"
INTERVAL = int(os.environ.get("DSH_RAG_SCAN_SECONDS") or 300)


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().astimezone().isoformat(timespec="seconds")
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{ts}  {msg}\n")


def acquire_lock():
    """单实例锁。

    装自启时 install-autostart.ps1 会立刻 Start-Process 一次，用户也可能手动再起一个；
    两个 watcher 扫同一批会话时，remember 的 SELECT→INSERT 会撞
    UNIQUE constraint，而 archive.run 没有 per-session 的 try，整批归档会中断。
    """
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    f = open(LOCK, "a+")
    try:
        f.seek(0, os.SEEK_END)
        if f.tell() == 0:
            f.write("lock\n")
            f.flush()
        f.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError:
        f.close()
        return None


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


def scan_once(s) -> str:
    import archive

    st = load_state()
    cutoff = float(st["archive_from"])
    return archive.run(store=s, limit=10, min_mtime=cutoff)


def maybe_ingest(s) -> str | None:
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

    cooked = []
    for r in cl.ingest_all(s, limit=10):
        if r.get("added"):
            cooked.append(f"{r['project']}+{r['added']}")
    INGEST_DAY.parent.mkdir(parents=True, exist_ok=True)
    INGEST_DAY.write_text(json.dumps({"day": today}), encoding="utf-8")
    return ("变更记录灌入: " + ", ".join(cooked)) if cooked else None


def main() -> None:
    lock = acquire_lock()
    if lock is None:
        log("另一个 watcher 已在运行，本进程退出")
        return
    from store import Store

    s = Store()          # 常驻一个连接：每 5 分钟新开一个会泄漏文件句柄
    log(f"watcher start, interval={INTERVAL}s, root={ROOT}")
    while True:
        try:
            msg = scan_once(s)
            if msg:
                log(msg)
        except Exception:  # noqa: BLE001
            log("ERROR " + traceback.format_exc().replace("\n", " | "))
            s.rollback()
        try:
            msg = maybe_ingest(s)
            if msg:
                log(msg)
        except Exception:  # noqa: BLE001
            log("INGEST ERROR " + traceback.format_exc().replace("\n", " | "))
            s.rollback()
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
