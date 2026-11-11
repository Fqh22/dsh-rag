"""注入链路健康检查自测。

`doctor` 的这一项是用来把「静默失败」变成「可见降级」的，它自己不能是猜的。
用假日志 + 假 store 把四种状态钉死，不依赖本机真实日志。

跑法：.venv\\Scripts\\python.exe scripts\\selftest_doctor_inject.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

os.environ["DSH_RAG_EMBED"] = "off"

import doctor  # noqa: E402

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


class FakeStore:
    def __init__(self, started_at: str | None = None):
        self.started_at = started_at

    def sessions(self, limit: int = 20):
        return [{"started_at": self.started_at}] if self.started_at else []


def iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) - delta).isoformat().replace("+00:00", "Z")


def write_log(lines: list[str]) -> Path:
    p = Path(tempfile.mkdtemp(prefix="dshrag-inject-")) / "auto-inject.log"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def run_check(log: Path | None, store) -> dict:
    saved = doctor.AUTO_INJECT_LOG
    try:
        doctor.AUTO_INJECT_LOG = log if log else Path("/nonexistent/auto-inject.log")
        return doctor._check_auto_inject(store)
    finally:
        doctor.AUTO_INJECT_LOG = saved


def main() -> int:
    print("== 正常：最近注入过，之后没有新会话 ==")
    log = write_log([f"[{iso(timedelta(hours=2))}] injected project=demo pack=v1 sid=s1"])
    r = run_check(log, FakeStore(iso(timedelta(hours=3))))
    check("level=ok", r["level"] == "ok", r)
    check("detail 带时间", "小时前" in r["detail"], r["detail"])

    print("\n== 链路可能已断：注入之后又开过会话 ==")
    log = write_log([
        f"[{iso(timedelta(days=10))}] injected project=demo pack=v1 sid=s1",
        f"[{iso(timedelta(days=9))}] hook unmatched sid=s2",
    ])
    r = run_check(log, FakeStore(iso(timedelta(days=5))))
    check("level=warn", r["level"] == "warn", r)
    check("提示链路可能已断", "链路可能已断" in r["detail"], r["detail"])

    print("\n== 日志里从来没有成功记录 ==")
    log = write_log([f"[{iso(timedelta(hours=1))}] hook unmatched sid=s1"])
    r = run_check(log, FakeStore(None))
    check("level=warn", r["level"] == "warn", r)
    check("提示一次都没有", "一次成功注入都没有" in r["detail"], r["detail"])

    print("\n== 日志不存在 ==")
    r = run_check(None, FakeStore(None))
    check("level=warn", r["level"] == "warn", r)
    check("提示插件没加载", "插件没加载" in r["detail"], r["detail"])

    print("\n== 关了自动注入 ==")
    saved = os.environ.get("DSH_RAG_AUTO_INJECT")
    os.environ["DSH_RAG_AUTO_INJECT"] = "off"
    try:
        r = run_check(None, FakeStore(None))
    finally:
        if saved is None:
            os.environ.pop("DSH_RAG_AUTO_INJECT", None)
        else:
            os.environ["DSH_RAG_AUTO_INJECT"] = saved
    check("关闭时不报错", r["level"] == "ok" and "已关闭" in r["detail"], r)

    print("\n== 超过 14 天没用 ==")
    log = write_log([f"[{iso(timedelta(days=30))}] injected project=demo pack=v1 sid=s1"])
    r = run_check(log, FakeStore(None))
    check("level=warn", r["level"] == "warn", r)
    check("提示确认是否还在用", "还在用" in r["detail"], r["detail"])

    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
