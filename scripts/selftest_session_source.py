"""会话源契约测试（P1）：把 DSH 会话格式的五层耦合钉死。

为什么要有这一层：`archive.py` 是整条链路里**最脆的耦合**——DSH 改一个字段名，
归档就静默变空，而"解析出 0 条"和"没有会话"在日志里长得一样。

fixture 放在 `tests/fixtures/sessions/*.jsonl`（纯文本，可 review、可 diff），
测试时压成 zstd 再交给 `archive.parse_session`——仓库里不放二进制。

覆盖的五层：
  1. 路径与压缩（单帧 / 多帧 / 坏文件）
  2. 事件类型（session / title / user / assistant / tool）
  3. source.kind 白名单（IGNORE_KINDS 与 compact-checkpoint）
  4. 度量字段（usage 有/无；tool/call 的读写分类）
  5. 时间（createdAt 毫秒；缺失时退回 mtime）

跑法：.venv\\Scripts\\python.exe scripts\\selftest_session_source.py
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

os.environ["DSH_RAG_EMBED"] = "off"

import archive  # noqa: E402
import zstandard  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "sessions"
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


def compress_fixture(name: str, dest_dir: Path, sid: str) -> Path:
    """把 .jsonl fixture 压成 DSH 的会话文件布局。"""
    raw = (FIXTURES / name).read_bytes()
    d = dest_dir / sid
    d.mkdir(parents=True, exist_ok=True)
    out = d / "session.v4.jsonl.zstd"
    out.write_bytes(zstandard.ZstdCompressor().compress(raw))
    return out


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dshrag-session-"))
    cache: dict[str, dict] = {}

    def parsed(name: str, sid: str) -> dict:
        if name not in cache:
            cache[name] = archive.parse_session(compress_fixture(name, tmp, sid))
        return cache[name]

    print("== 1 事件类型全覆盖 ==")
    s = parsed("minimal.jsonl", "sess-minimal")
    check("解析成功", s is not None)
    if s:
        check("标题来自 session/title", s["title"] == "最小会话", s["title"])
        check("user 事件收进 users", s["users"] == ["帮我看看 README"], str(s["users"]))
        check("assistant 事件收进 assistants", len(s["assistants"]) == 1)
        check("tool/call 收进 actions", len(s["actions"]) == 2, str(s["actions"]))
        check("session_id 来自 header", s["session_id"] == "sess-minimal")
        check("cwd 保留", s["cwd"] == "/work/demo")

    print("\n== 2 度量字段 ==")
    m = (s or {}).get("metrics", {})
    check("toolCalls", m.get("toolCalls") == 2, str(m.get("toolCalls")))
    check("readCalls", m.get("readCalls") == 2, str(m.get("readCalls")))
    check("writeCalls 为 0", m.get("writeCalls") == 0)
    check("stepsToFirstWrite 为 None", m.get("stepsToFirstWrite") is None)
    check("同一文件读两次 → repeatedReads=1", m.get("repeatedReads") == 1, str(m.get("repeatedReads")))
    check("uniqueFilesRead=1", m.get("uniqueFilesRead") == 1)
    check("从未读过 pack → packVersion 为 None", m.get("packVersion") is None)

    print("\n== 3 source.kind 白名单 ==")
    c = parsed("compact.jsonl", "sess-compact")
    check("compact-checkpoint 进 compacts", c is not None and len(c["compacts"]) == 1)
    if c and c["compacts"]:
        body = c["compacts"][0]
        check("压缩摘要去掉了引导语", "automatically generated checkpoint" not in body, body[:60])
        check("压缩摘要保留正文", "之前拆了 schema" in body, body[:60])
        check("compact 不当成 user 消息", c["users"] == ["重构存储层"], str(c["users"]))

    e = parsed("env-injected.jsonl", "sess-env")
    check("环境注入不进入 users", e is not None and e["users"] == ["真正的问题"], str((e or {}).get("users")))
    check("被忽略的 kind 有计数",
          e is not None and e["ignored"].get("agent-instructions") == 1
          and e["ignored"].get("skill-catalog") == 1, str((e or {}).get("ignored")))

    print("\n== 4 缺 usage 的旧会话 ==")
    n = parsed("no-usage.jsonl", "sess-nousage")
    check("不因缺 usage 而判失败", n is not None)
    check("assistant 仍被收下", n is not None and len(n["assistants"]) == 1)
    check("度量不依赖 usage（toolCalls 为 0）",
          n is not None and n["metrics"].get("toolCalls") == 0)

    print("\n== 5 未知事件与坏行 ==")
    u = parsed("unknown-event.jsonl", "sess-unknown")
    check("未知 type + 非法 JSON 不打断", u is not None and u["users"] == ["未知事件不应打断解析"])

    print("\n== 6 时间字段 ==")
    if s:
        expected = datetime.fromtimestamp(1761900000000 / 1000).astimezone().isoformat(timespec="seconds")
        check("createdAt 毫秒 → ISO", s["created_at"] == expected, f"{s['created_at']} vs {expected}")
    raw = (FIXTURES / "minimal.jsonl").read_text(encoding="utf-8").splitlines()
    no_ts = "\n".join(l for l in raw if '"createdAt"' not in l)
    d = tmp / "sess-nots"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "session.v4.jsonl.zstd"
    f.write_bytes(zstandard.ZstdCompressor().compress(no_ts.encode()))
    nt = archive.parse_session(f)
    check("缺 createdAt 时退回 mtime", nt is not None and nt["created_at"] != "")

    print("\n== 7 压缩边界 ==")
    multi = zstandard.ZstdCompressor()
    multi_path = tmp / "sess-multi" / "session.v4.jsonl.zstd"
    multi_path.parent.mkdir(parents=True, exist_ok=True)
    body = (FIXTURES / "minimal.jsonl").read_bytes()
    half = len(body) // 2
    multi_path.write_bytes(multi.compress(body[:half]) + multi.compress(body[half:]))
    mm = archive.parse_session(multi_path)
    check("多帧 zstd 能完整解出", mm is not None and mm["session_id"] == "sess-minimal")

    bad = tmp / "sess-bad" / "session.v4.jsonl.zstd"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"not a zstd file at all")
    check("坏文件返回 None（不抛到调用方）", archive.parse_session(bad) is None)

    print("\n== 8 扫描（iter_session_files）==")
    saved = archive.SESSIONS_ROOT
    scan_root = tmp / "scan"
    for ws, sid in (("--work-demo--", "sess-a"), ("--work-demo--", "sess-b"), ("--other--", "sess-c")):
        p = scan_root / ws / sid / "session.v4.jsonl.zstd"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(multi.compress(body))
    try:
        archive.SESSIONS_ROOT = scan_root
        all_files = sorted(p.parent.name for p in archive.iter_session_files())
        hinted = sorted(p.parent.name for p in archive.iter_session_files("work-demo"))
        check("扫描全部会话", all_files == ["sess-a", "sess-b", "sess-c"], str(all_files))
        check("workspace_hint 过滤", hinted == ["sess-a", "sess-b"], str(hinted))
    finally:
        archive.SESSIONS_ROOT = saved

    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
