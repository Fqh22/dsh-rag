"""HTTP 传输自测（P4）：stateless JSON-RPC over POST。

验证 stdio 之外的第二个传输层能用，且和 stdio 共用同一份 `dispatch`：
如果两条通道各写一份分派逻辑，迟早出现「stdio 能跑、HTTP 少个分支」。

跑法：.venv\\Scripts\\python.exe scripts\\selftest_http.py
"""
from __future__ import annotations

import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
PY = ROOT / ".venv" / "Scripts" / "python.exe"
if not PY.exists():
    PY = Path(sys.executable)

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


def free_port() -> int:
    for _ in range(20):
        port = random.randint(55100, 55900)
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("找不到空闲端口")


def post(port: int, payload, timeout=15):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/mcp", data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode("utf-8", "replace")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dshrag-http-"))
    port = free_port()
    env = dict(os.environ)
    env.update({
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": str(SRC),
        "DSH_RAG_DB": str(tmp / "http.db"),
        "DSH_RAG_PROJECTS_ROOT": str(tmp / "projects"),
        "DSH_RAG_HANDOFFS_ROOT": str(tmp / "handoffs"),
        "DSH_RAG_EMBED": "off",
        "DSH_RAG_STARTUP_ARCHIVE": "0",
    })
    proc = subprocess.Popen(
        [str(PY), str(SRC / "server.py"), "--http", f"127.0.0.1:{port}"],
        cwd=str(SRC), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        print("== 启动 ==")
        ready = False
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as r:
                    health = json.loads(r.read())
                ready = True
                break
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                if proc.poll() is not None:
                    break
                time.sleep(0.3)
        check("服务在 20s 内就绪", ready)
        if not ready:
            return 1
        check("healthz 带 server/version", health.get("server") == "dsh-rag" and bool(health.get("version")),
              str(health))

        print("\n== 协议 ==")
        code, init = post(port, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocolVersion": "2025-06-18", "capabilities": {}}})
        check("initialize 200", code == 200, str(code))
        check("协议版本回声", init["result"]["protocolVersion"] == "2025-06-18", str(init.get("result")))
        check("serverInfo 正确", init["result"]["serverInfo"]["name"] == "dsh-rag")

        code, lst = post(port, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in lst["result"]["tools"]}
        check("tools/list 返回工具面", {"recall", "remember", "handoff_pack", "doctor"} <= names,
              str(sorted(names)[:6]))

        code, call = post(port, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                 "params": {"name": "memory_stats", "arguments": {}}})
        text = "".join(c.get("text", "") for c in call["result"]["content"])
        check("tools/call 能执行", "记忆总数" in text, text[:80])

        code, note = post(port, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        check("通知返回 202（无响应体）", code == 202, str(code))

        code, batch = post(port, [
            {"jsonrpc": "2.0", "id": 4, "method": "ping"},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/list"},
        ])
        check("批量请求返回两条", code == 200 and len(batch) == 2, f"{code} {len(batch) if isinstance(batch, list) else batch}")

        print("\n== 错误处理 ==")
        code, bad = post(port, "not-json-but-valid")
        check("非对象 JSON-RPC → 400", code == 400, str(code))

        raw_req = urllib.request.Request(
            f"http://127.0.0.1:{port}/mcp", data=b"{not json",
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(raw_req, timeout=5) as r:
                raw_code = r.status
        except urllib.error.HTTPError as e:
            raw_code = e.code
        check("非 JSON 字节 → 400", raw_code == 400, str(raw_code))

        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/nope", timeout=5) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        check("未知路径 404", code == 404, str(code))

        print(f"\n结果: {PASS} 通过, {FAIL} 失败")
        return 0 if FAIL == 0 else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
