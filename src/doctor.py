"""体检：把「看着还行」变成一组可复现的判断。

为什么需要它：
- 这个项目依赖三个外部事实：DSH 的会话日志格式、SQLite 的索引一致性、磁盘上的项目目录。
  任何一个漂了，程序都不会崩——它会静默地少记、少搜、少召回，几个月后才发现。
- 所以每次升级 DSH、迁移库、或每隔一阵，都该跑一遍。

检查项分三档：
    ok     正常
    warn   能用，但该处理（例如有草稿没人确认）
    error  已经坏了（例如 FTS 与主表不一致、会话日志解析不出东西）
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import schema

SESSIONS_ROOT = Path.home() / ".dsh" / "sessions"
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
AUTO_INJECT_LOG = LOG_DIR / "auto-inject.log"


def _check(name: str, level: str, detail: str) -> dict[str, Any]:
    return {"name": name, "level": level, "detail": detail}


def _parse_log_ts(line: str) -> datetime | None:
    """日志行形如 `[2026-11-10T18:24:46.123Z] matched ...`。"""
    if not line.startswith("[") or "]" not in line:
        return None
    raw = line[1:line.index("]")].replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _check_auto_inject(store) -> dict[str, Any]:
    """开场自动注入依赖 DSH 私有 API，失败是**静默**的——只能靠日志判断。

    判据不是"多久没注入"（用户可能一周没开会话），而是
    「最近有会话活动，但同期没有成功注入」——那才是链路断了。
    """
    disabled = (os.environ.get("DSH_RAG_AUTO_INJECT") or "").strip().lower() in ("off", "0", "false")
    if disabled:
        return _check("开场自动注入", "ok", "已关闭（DSH_RAG_AUTO_INJECT=off）")

    if not AUTO_INJECT_LOG.is_file():
        return _check("开场自动注入", "warn",
                      "没有 logs/auto-inject.log：插件没加载，或这台机器还没开过会话")

    try:
        lines = AUTO_INJECT_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-800:]
    except OSError as exc:
        return _check("开场自动注入", "warn", f"日志读不出来：{exc}")

    last_ok: datetime | None = None
    for line in reversed(lines):
        # 插件成功时写的是 `injected project=...`；`hook unmatched` 是没命中项目
        if "injected" in line or ("matched" in line and "unmatched" not in line):
            last_ok = _parse_log_ts(line)
            if last_ok:
                break
    if last_ok is None:
        return _check("开场自动注入", "warn",
                      "日志里一次成功注入都没有：检查插件是否启用、logs/auto-inject.log")

    now = datetime.now(last_ok.tzinfo)
    age_h = (now - last_ok).total_seconds() / 3600
    human = f"{age_h:.0f} 小时前" if age_h < 48 else f"{age_h / 24:.0f} 天前"

    # 有比注入更新的会话 → 说明后来开过会话却没注入成功
    try:
        sessions = store.sessions(limit=1)
        newest_started = sessions[0].get("started_at") if sessions else None
        if newest_started:
            # Python 3.10 的 fromisoformat 不认 "Z"，统一成 +00:00
            started = datetime.fromisoformat(str(newest_started).replace("Z", "+00:00"))
            if started.tzinfo is None:
                started = started.replace(tzinfo=last_ok.tzinfo)
            if started > last_ok and (started - last_ok).total_seconds() > 86400:
                return _check(
                    "开场自动注入", "warn",
                    f"最近一次成功注入是{human}，但之后又有会话开始——"
                    "链路可能已断（看 logs/auto-inject.log，或重启 DSH）",
                )
    except Exception:  # noqa: BLE001
        pass

    level = "warn" if age_h > 24 * 14 else "ok"
    return _check("开场自动注入", level,
                  f"最近一次成功注入 {human}" + ("（超过 14 天，确认是否还在用）" if level == "warn" else ""))


def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n:.1f} GB"


def run(store, session_probe: int = 6, verbose: bool = False) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    conn = store._conn

    # ---------------------------------------------------------------- 结构
    info = schema.schema_info(conn)
    if info["version"] == schema.SCHEMA_VERSION:
        checks.append(_check("结构版本", "ok", f"v{info['version']}"))
    else:
        checks.append(_check(
            "结构版本", "error",
            f"库是 v{info['version']}，代码要 v{schema.SCHEMA_VERSION}",
        ))

    leftovers = info.get("leftovers") or []
    checks.append(_check(
        "迁移残留", "warn" if leftovers else "ok",
        f"保留 {', '.join(leftovers)}（确认无误后可 DROP）" if leftovers else "无",
    ))

    db_path = store.db_path
    wal = Path(str(db_path) + "-wal")
    db_size = db_path.stat().st_size if db_path.exists() else 0
    wal_size = wal.stat().st_size if wal.exists() else 0
    checks.append(_check(
        "库体积", "warn" if wal_size > 8 * 1024 * 1024 else "ok",
        f"db {_human_bytes(db_size)} + wal {_human_bytes(wal_size)}",
    ))

    # ---------------------------------------------------------------- 索引一致性
    with store._lock:
        missing_fts = conn.execute(
            "SELECT COUNT(*) c FROM memories m LEFT JOIN memories_fts f ON f.rowid=m.id "
            "WHERE f.rowid IS NULL"
        ).fetchone()["c"]
        stray_fts = conn.execute(
            "SELECT COUNT(*) c FROM memories_fts f LEFT JOIN memories m ON m.id=f.rowid "
            "WHERE m.id IS NULL"
        ).fetchone()["c"]
        stray_vec = conn.execute(
            "SELECT COUNT(*) c FROM vectors v LEFT JOIN memories m ON m.id=v.memory_id "
            "WHERE m.id IS NULL"
        ).fetchone()["c"]

    checks.append(_check(
        "全文索引", "error" if (missing_fts or stray_fts) else "ok",
        f"缺 {missing_fts} 条、多 {stray_fts} 条" + ("（跑 scripts/rebuild_fts.py）" if missing_fts or stray_fts else ""),
    ))
    checks.append(_check(
        "向量孤儿", "warn" if stray_vec else "ok",
        f"{stray_vec} 条向量没有对应记忆" if stray_vec else "无",
    ))

    try:
        import embed

        missing_vec = store.missing_vectors(embed.MODEL_NAME)
        checks.append(_check(
            "向量覆盖", "warn" if missing_vec else "ok",
            f"{len(missing_vec)} 条生效记忆没有当前模型的向量（{embed.MODEL_NAME}）"
            if missing_vec else f"全部已向量化（{embed.MODEL_NAME}）",
        ))
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("向量覆盖", "warn", f"检查失败：{type(exc).__name__}: {exc}"))

    # ---------------------------------------------------------------- 会话日志格式
    # 这是最容易被 DSH 升级悄悄打断的一环：格式变了，归档会「成功但抽不到东西」。
    # 注意：**缺 zstandard 和格式变化的表现一样**（都是解析 0 条），
    # 所以先单独查依赖，别把「用错 Python」误报成「数据坏了」。
    try:
        import archive

        try:
            import zstandard  # noqa: F401

            has_zstd = True
        except Exception:  # noqa: BLE001
            has_zstd = bool(__import__("shutil").which("zstd"))
        if not has_zstd:
            checks.append(_check(
                "会话日志格式", "warn",
                "缺 zstandard（pip install zstandard，或用项目 .venv 的 python）——本次跳过"))
        else:
            files = sorted(
                SESSIONS_ROOT.glob("*/*/session.v4.jsonl.zstd"),
                key=lambda p: p.stat().st_mtime, reverse=True,
            )[:session_probe]
            parsed = 0
            failed: list[str] = []
            kinds: set[str] = set()
            metrics_ok = 0
            errs: list[str] = []
            for f in files:
                try:
                    sess = archive.parse_session(f)
                except Exception as exc:  # noqa: BLE001
                    sess = None
                    errs.append(f"{type(exc).__name__}: {exc}")
                if not sess:
                    failed.append(f.parent.name[:12])
                    continue
                parsed += 1
                kinds.update((sess.get("ignored") or {}).keys())
                if sess.get("metrics", {}).get("toolCalls") is not None:
                    metrics_ok += 1
            level = "ok" if parsed == len(files) and files else ("warn" if parsed else "error")
            if not files:
                level, detail = "warn", "没有找到会话日志（DSH 目录结构变了？）"
            else:
                detail = f"最近 {len(files)} 个会话解析成功 {parsed} 个"
                if failed:
                    detail += f"，失败 {', '.join(failed[:3])}"
                    if errs:
                        detail += f"（{errs[0][:80]}）"
                if kinds:
                    detail += f"（识别到的注入类型 {len(kinds)} 种）"
            checks.append(_check("会话日志格式", level, detail))

            if files and parsed and metrics_ok == 0:
                checks.append(_check("接力度量", "warn", "会话里没有 tool/call 事件，指标会是空的"))
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("会话日志格式", "warn", f"检查失败：{type(exc).__name__}: {exc}"))

    # ---------------------------------------------------------------- 注入链路
    # 这一项的意义在 README 的「已知限制」里写得很清楚：DSH 升级就可能改私有 API，
    # 而注入失败是静默的。没有这条检查，用户会一直以为记忆在工作。
    try:
        checks.append(_check_auto_inject(store))
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("开场自动注入", "warn", f"检查失败：{type(exc).__name__}: {exc}"))

    # ---------------------------------------------------------------- 内容健康度
    # sync=False：体检不该顺手写 projects 表（overview 默认会 sync）
    ov = store.overview(sync=False)
    # 已归档的项目没有目录是正常的（比如 agentd 并进了 qq-cloud-bot），不算孤儿
    archived = {r[0] for r in conn.execute(
        "SELECT name FROM projects WHERE archived_at IS NOT NULL")}
    orphans = [p for p in ov
               if p["project"] not in ("工作区", "未归类")
               and p["project"] not in archived and not p["hasDir"]]
    checks.append(_check(
        "孤儿项目", "warn" if orphans else "ok",
        "、".join(p["project"] for p in orphans) if orphans else "无",
    ))

    no_cp = [
        p for p in ov
        if p["hasDir"] and p["total"] > 0 and p["kinds"].get("checkpoint", 0) == 0
        and p["project"] not in ("工作区", "未归类") and p["project"] not in archived
    ]
    checks.append(_check(
        "结构化状态覆盖", "warn" if no_cp else "ok",
        "有记忆但没有 checkpoint：" + "、".join(p["project"] for p in no_cp) if no_cp else "全部项目都有 checkpoint",
    ))

    st = store.stats()
    if st.get("drafts"):
        checks.append(_check("待确认草稿", "warn", f"{st['drafts']} 份（用 cleanup 看，或 checkpoint_promote 采纳）"))
    else:
        checks.append(_check("待确认草稿", "ok", "无"))

    # ---------------------------------------------------------------- 数据体积
    with store._lock:
        row = conn.execute(
            """SELECT
                 SUM(CASE WHEN kind='session' AND status='active' THEN length(content) ELSE 0 END) sess,
                 SUM(CASE WHEN kind='handoff' AND status='superseded' THEN length(content) ELSE 0 END) old_card,
                 SUM(CASE WHEN status='archived' THEN length(content) ELSE 0 END) archived
               FROM memories"""
        ).fetchone()
    checks.append(_check(
        "内容体积", "warn" if (row["sess"] or 0) > 200_000 else "ok",
        f"会话摘要 {_human_bytes(row['sess'] or 0)}、失效交接卡 {_human_bytes(row['old_card'] or 0)}、"
        f"已归档 {_human_bytes(row['archived'] or 0)}",
    ))

    errors = sum(1 for c in checks if c["level"] == "error")
    warns = sum(1 for c in checks if c["level"] == "warn")
    return {
        "ok": errors == 0,
        "errors": errors,
        "warns": warns,
        "checks": checks,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "db": str(db_path),
    }


def format_report(rep: dict[str, Any]) -> str:
    icon = {"ok": "✓", "warn": "!", "error": "✗"}
    lines = [f"dsh-rag 体检：{len(rep['checks'])} 项，{rep['errors']} 个错误、{rep['warns']} 个警告"]
    for c in rep["checks"]:
        lines.append(f"  {icon[c['level']]} {c['name']}：{c['detail']}")
    if rep["errors"]:
        lines.append("")
        lines.append("有错误：先修数据一致性，再谈优化。")
    elif rep["warns"]:
        lines.append("")
        lines.append("没有硬错误；警告项可以用 cleanup / checkpoint_promote 处理。")
    return "\n".join(lines)
