"""MCP 端到端自测：走完整协议，校验每个工具的返回。

用文件重定向而不是管道喂 stdin/stdout：DSH 的沙箱里子进程管道会被拦，
文件方式在任何模式下都能跑。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
PY = ROOT / ".venv" / "Scripts" / "python.exe"

REQUESTS = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2026-07-28", "capabilities": {},
                "clientInfo": {"name": "selftest", "version": "0"}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
     "params": {"name": "remember", "arguments": {
         "project": "dsh-rag", "kind": "decision", "title": "MCP 传输方式（自测）",
         "content": "本地 MCP server 用 stdio + newline-delimited JSON-RPC 2.0，不依赖官方 SDK。"}}},
    {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
     "params": {"name": "recall", "arguments": {"query": "MCP 用什么传输协议", "limit": 3}}},
    {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
     "params": {"name": "handoff_put", "arguments": {
         "project": "dsh-rag", "content": "自测写入：交接卡读写正常。"}}},
    {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
     "params": {"name": "handoff_get", "arguments": {"project": "dsh-rag"}}},
    {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
     "params": {"name": "memory_stats", "arguments": {}}},
    {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
     "params": {"name": "memory_update", "arguments": {
         "id": 1, "title": "MCP 传输方式（已改）"}}},
    {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
     "params": {"name": "recall", "arguments": {"query": "已改", "limit": 3}}},
    {"jsonrpc": "2.0", "id": 10, "method": "tools/call",
     "params": {"name": "checkpoint_put", "arguments": {
         "project": "dsh-rag", "baseline": "技术栈：Python + SQLite（自测基线）",
         "goal": "验证结构化状态", "next": ["跑通 pack"],
         "files": [{"path": "src/store.py", "why": "核心"}]}}},
    {"jsonrpc": "2.0", "id": 11, "method": "tools/call",
     "params": {"name": "handoff_pack", "arguments": {"project": "dsh-rag"}}},
    {"jsonrpc": "2.0", "id": 12, "method": "tools/call",
     "params": {"name": "cache_stats", "arguments": {"limit": 2}}},
]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dshrag-"))
    req = tmp / "req.jsonl"
    out = tmp / "out.jsonl"
    req.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in REQUESTS) + "\n",
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(SRC)
    # 用临时库跑，绝不污染正式记忆
    env["DSH_RAG_DB"] = str(tmp / "selftest.db")

    with open(req, "r", encoding="utf-8") as fin, open(out, "w", encoding="utf-8") as fout:
        proc = subprocess.run(
            [str(PY), str(SRC / "server.py")],
            stdin=fin, stdout=fout, stderr=subprocess.PIPE,
            cwd=str(SRC), env=env, timeout=300,
        )

    if proc.returncode != 0:
        print("server 退出码", proc.returncode)
        print(proc.stderr.decode("utf-8", "replace")[:2000])
        return 1

    responses = {}
    for line in out.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        o = json.loads(line)
        if "id" in o:
            responses[o["id"]] = o

    def text_of(rid: int) -> str:
        r = responses[rid]["result"]
        return "".join(c.get("text", "") for c in r.get("content", []) if c.get("type") == "text")

    ok = True

    print("== 1 initialize ==")
    init = responses[1]["result"]
    print(f"   协议版本 {init['protocolVersion']} / server {init['serverInfo']['name']}")
    ok &= init["protocolVersion"] == "2026-07-28"

    print("== 2 tools/list ==")
    names = [t["name"] for t in responses[2]["result"]["tools"]]
    print("  ", names)
    ok &= {"recall", "remember", "handoff_get", "handoff_put",
           "handoff_pack", "checkpoint_put", "cache_stats"} <= set(names)

    print("== 3 remember ==")
    t3 = text_of(3)
    print("  ", t3)
    ok &= ("已写入" in t3)

    print("== 4 recall ==")
    t4 = text_of(4)
    print("  ", t4[:160].replace("\n", " | "))
    ok &= ("MCP" in t4)

    print("== 5 handoff_put ==")
    print("  ", text_of(5))
    ok &= ("已更新" in text_of(5))

    print("== 6 handoff_get ==")
    t6 = text_of(6)
    print("  ", t6[:120].replace("\n", " | "))
    ok &= ("交接卡" in t6)

    print("== 7 memory_stats ==")
    t7 = text_of(7)
    print("  ", t7.replace("\n", " | "))
    ok &= ("记忆总数" in t7)

    print("== 8 memory_update ==")
    t8 = text_of(8)
    print("  ", t8.replace("\n", " | "))
    ok &= ("已更新" in t8)

    print("== 9 recall 改后内容 ==")
    t9 = text_of(9)
    print("  ", t9[:140].replace("\n", " | "))
    ok &= ("已改" in t9)

    print("== 10 checkpoint_put ==")
    t10 = text_of(10)
    print("  ", t10[:140])
    ok &= ("交接卡已重渲染" in t10)

    print("== 11 handoff_pack ==")
    t11 = text_of(11)
    print("  ", t11[:200])
    ok &= ("基线" in t11 and "当前状态" in t11 and "pack v" in t11)

    print("== 12 cache_stats ==")
    t12 = text_of(12)
    print("  ", t12[:160])
    ok &= ("缓存命中" in t12 or "没有可解析" in t12)

    print()
    print("结果:", "全部通过" if ok else "有断言失败")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
