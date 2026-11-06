"""给插件 Host 侧调用的命令行接口：stdout 只输出 JSON。

为什么不让 Node 直接读 SQLite：
- DSH 跑在 Electron 里，node:sqlite 的可用性取决于 Electron 内置的 Node 版本，不可控；
- 复用 Python 侧已经验证过的检索/写入逻辑，只有一份真相。

用法：
  python cli.py state
  python cli.py projects
  python cli.py handoff   --project NAME
  python cli.py recent    --project NAME --limit 20
  python cli.py recall    --query TEXT --project NAME --limit 8
  python cli.py remember  --project NAME --kind decision --title T --content C
  python cli.py handoff_put --project NAME --content C
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from store import DB_PATH, Store  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")


def emit(obj) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.flush()


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("state")
    sub.add_parser("projects")
    sub.add_parser("overview")

    p_h = sub.add_parser("handoff")
    p_h.add_argument("--project", required=True)

    p_r = sub.add_parser("recent")
    p_r.add_argument("--project", default=None)
    p_r.add_argument("--limit", type=int, default=30)

    p_q = sub.add_parser("recall")
    p_q.add_argument("--query", required=True)
    p_q.add_argument("--project", default=None)
    p_q.add_argument("--limit", type=int, default=10)

    p_m = sub.add_parser("remember")
    p_m.add_argument("--project", required=True)
    p_m.add_argument("--kind", default="fact")
    p_m.add_argument("--title", required=True)
    p_m.add_argument("--content", required=True)

    p_p = sub.add_parser("handoff_put")
    p_p.add_argument("--project", required=True)
    p_p.add_argument("--content", required=True)

    p_u = sub.add_parser("update")
    p_u.add_argument("--id", type=int, required=True)
    p_u.add_argument("--title", default=None)
    p_u.add_argument("--content", default=None)

    # ---- 页面层（长文档：目录 + 块） ----
    p_pg = sub.add_parser("pages", help="长文档页面层：index/list/search/stats")
    p_pg.add_argument("--project", required=True)
    p_pg.add_argument("--dir", default=None, help="要灌的目录（index 时用）")
    p_pg.add_argument("--pattern", default="*.md")
    p_pg.add_argument("--query", default=None)
    p_pg.add_argument("--limit", type=int, default=20)
    p_pg.add_argument("--reindex", action="store_true", help="强制重算（忽略 sha）")

    p_g = sub.add_parser("get")
    p_g.add_argument("--id", type=int, required=True)

    p_pr = sub.add_parser("prune")
    p_pr.add_argument("--project", required=True)
    p_pr.add_argument("--confirm", action="store_true")

    # ---- v0.5：上下文包 / 结构化状态 / 缓存度量 ----
    p_pk = sub.add_parser("pack")
    p_pk.add_argument("--project", required=True)
    p_pk.add_argument("--budget", type=int, default=3000)
    p_pk.add_argument("--meta", action="store_true")

    p_cp = sub.add_parser("checkpoint")
    p_cp.add_argument("--project", required=True)
    p_cp.add_argument("--json", default=None, help="要写入的字段（JSON），省略则只读")
    p_cp.add_argument("--session-id", default=None)

    p_ca = sub.add_parser("cache")
    p_ca.add_argument("--project", default=None)
    p_ca.add_argument("--limit", type=int, default=20)

    p_rl = sub.add_parser("relay")
    p_rl.add_argument("--project", default=None)
    p_rl.add_argument("--limit", type=int, default=100)

    p_se = sub.add_parser("sessions")
    p_se.add_argument("--project", default=None)
    p_se.add_argument("--limit", type=int, default=20)

    sub.add_parser("schema")

    # ---- v0.6：体检 / 清理 / 恢复 / 草稿 ----
    p_doc = sub.add_parser("doctor")
    p_doc.add_argument("--probe", type=int, default=6)

    p_cl = sub.add_parser("cleanup")
    p_cl.add_argument("--apply", action="store_true", help="真的执行（默认只出计划）")
    p_cl.add_argument("--purge", action="store_true", help="硬删超期 archived（不可恢复）")
    p_cl.add_argument("--session-days", type=int, default=None)
    p_cl.add_argument("--hard-days", type=int, default=None)
    p_cl.add_argument("--keep-handoffs", type=int, default=None)
    p_cl.add_argument("--keep-superseded", type=int, default=None)
    p_cl.add_argument("--draft-days", type=int, default=None)
    p_cl.add_argument("--purge-days", type=int, default=None)

    p_rs = sub.add_parser("restore")
    p_rs.add_argument("--id", type=int, required=True)

    p_dr = sub.add_parser("draft")
    p_dr.add_argument("--project", required=True)
    p_dr.add_argument("--promote", action="store_true")
    p_dr.add_argument("--drop", action="store_true")

    p_hk = sub.add_parser("hook")
    p_hk.add_argument("--text", default="")
    p_hk.add_argument("--cwd", default="")

    # 变更记录 → 记忆（增量、不调模型）
    p_ing = sub.add_parser("ingest")
    p_ing.add_argument("--project", default="")
    p_ing.add_argument("--file", default="")
    p_ing.add_argument("--root", default="")
    p_ing.add_argument("--limit", type=int, default=0)
    p_ing.add_argument("--all-entries", action="store_true")
    p_ing.add_argument("--dry-run", action="store_true")

    args = ap.parse_args()
    s = Store()

    if args.cmd == "state":
        import embed

        emit({
            "ok": True,
            "stats": s.stats(),
            "projects": s.projects(),
            "db": str(DB_PATH),
            "model": embed.MODEL_NAME,
            "modelError": embed.error(),
            "migration": s.migration,
        })

    elif args.cmd == "projects":
        emit({"ok": True, "projects": s.projects()})

    elif args.cmd == "ingest":
        import changelog as cl

        root = Path(args.root) if args.root else None
        kw = dict(dry_run=args.dry_run, limit=args.limit,
                  all_entries=args.all_entries)
        if args.file:
            p = Path(args.file)
            proj = args.project or p.parent.name
            res = [{"project": proj, **cl.ingest(s, proj, p, **kw)}]
        elif args.project:
            p = cl.project_file(args.project, root)
            if not p:
                emit({"ok": False, "error": f"{args.project} 没有变更记录文件"})
                return 1
            res = [{"project": args.project, **cl.ingest(s, args.project, p, **kw)}]
        else:
            res = cl.ingest_all(s, root=root, **kw)
        emit({"ok": True, "result": res})

    elif args.cmd == "overview":
        emit({"ok": True, "projects": s.overview()})

    elif args.cmd == "handoff":
        norm = s.normalize_project(args.project)
        m = s.get_handoff(norm)
        emit({
            "ok": True,
            "project": norm,
            "mdPath": s.handoff_md_path(norm),
            "handoff": None if not m else {
                "id": m.id, "content": m.content,
                "updated_at": m.created_at, "status": m.status,
            },
        })

    elif args.cmd == "recent":
        emit({"ok": True, "items": [m.as_dict() for m in s.recent(args.project, args.limit)]})

    elif args.cmd == "recall":
        ms = s.recall(args.query, project=args.project, limit=args.limit)
        emit({"ok": True, "items": [m.as_dict() for m in ms]})

    elif args.cmd == "remember":
        mid, created = s.remember(args.project, args.kind, args.title, args.content)
        if created:
            try:
                import embed

                vecs = embed.embed([f"{args.title}\n{args.content}"])
                if vecs:
                    s.add_vector(mid, vecs[0], embed.MODEL_NAME)
            except Exception:  # noqa: BLE001
                pass
        emit({"ok": True, "id": mid, "created": created})

    elif args.cmd == "handoff_put":
        mid = s.put_handoff(args.project, args.content)
        norm = s.normalize_project(args.project)
        emit({"ok": True, "id": mid, "project": norm, "md": s.handoff_md_path(norm)})

    elif args.cmd == "get":
        m = s.get_memory(args.id)
        emit({"ok": True, "item": None if not m else m.as_dict()})

    elif args.cmd == "prune":
        norm = s.normalize_project(args.project)
        if not args.confirm:
            emit({"ok": False, "error": "需要 --confirm 才会真正删除", "project": norm})
            return 1
        n = s.delete_project(norm)
        emit({"ok": True, "project": norm, "deleted": n})

    elif args.cmd == "update":
        ok, msg = s.update_memory(args.id, args.title, args.content)
        if ok and msg == "已更新":
            # 改完必须重算向量，否则语义检索还在用旧内容的向量
            try:
                import embed

                m = s.get_memory(args.id)
                if m:
                    vecs = embed.embed([f"{m.title}\n{m.content}"])
                    if vecs:
                        s.add_vector(m.id, vecs[0], embed.MODEL_NAME)
            except Exception as exc:  # noqa: BLE001
                emit({"ok": True, "id": args.id, "message": msg,
                      "warning": f"向量未更新: {exc}"})
                return 0
        emit({"ok": ok, "id": args.id, "message": msg})

    elif args.cmd == "pack":
        pack = s.handoff_pack(args.project, budget_tokens=args.budget, with_meta=args.meta)
        emit({"ok": True, **pack})

    elif args.cmd == "checkpoint":
        if args.json is None:
            cp = s.get_checkpoint(args.project)
            emit({"ok": True, "checkpoint": None if not cp else {
                "id": cp.id, "updated_at": cp.updated_at, "meta": cp.meta,
            }})
        else:
            try:
                patch = json.loads(args.json)
            except ValueError as exc:
                emit({"ok": False, "error": f"--json 不是合法 JSON: {exc}"})
                return 1
            mid, created, card = s.checkpoint_put(
                args.project, patch, session_id=args.session_id
            )
            emit({"ok": True, "id": mid, "created": created, "card": card})

    elif args.cmd == "cache":
        import insight

        def resolver(sid: str) -> str | None:
            for row in s.sessions(limit=500):
                if row["session_id"] == sid:
                    return row.get("project")
            return None

        rep = insight.report(limit=args.limit, project=args.project, resolver=resolver)
        emit({"ok": True, **rep})

    elif args.cmd == "sessions":
        emit({"ok": True, "items": s.sessions(args.project, args.limit)})

    elif args.cmd == "relay":
        import insight

        rep = insight.relay_report(s, project=args.project, limit=args.limit)
        emit({"ok": True, **rep, "text": insight.format_relay(rep)})

    elif args.cmd == "schema":
        import schema

        emit({"ok": True, **schema.schema_info(s._conn)})

    elif args.cmd == "doctor":
        import doctor

        rep = doctor.run(s, session_probe=args.probe)
        emit({"ok": True, **rep, "text": doctor.format_report(rep)})

    elif args.cmd == "cleanup":
        import cleanup

        if args.purge:
            out = cleanup.purge(s, args.purge_days)
            emit({"ok": True, "purged": out})
        else:
            plan = cleanup.plan(
                s,
                session_days=args.session_days,
                hard_days=args.hard_days,
                keep_handoffs=args.keep_handoffs,
                keep_superseded=args.keep_superseded,
                draft_days=args.draft_days,
            )
            text = cleanup.format_plan(plan)
            if args.apply:
                done = cleanup.apply(s, plan)
                emit({"ok": True, "applied": done, **plan, "text": text})
            else:
                emit({"ok": True, "dryRun": True, **plan, "text": text})

    elif args.cmd == "restore":
        emit({"ok": True, "restored": s.restore_memory(args.id), "id": args.id})

    elif args.cmd == "draft":
        if args.promote:
            res = s.promote_draft(args.project)
            emit({"ok": True, "promoted": bool(res),
                  "checkpoint": None if not res else {"id": res[0], "created": res[1]}})
        elif args.drop:
            emit({"ok": True, "dropped": s.drop_draft(args.project)})
        else:
            d = s.get_draft(args.project)
            emit({"ok": True, "draft": None if not d else {
                "id": d.id, "updated_at": d.updated_at, "meta": d.meta, "content": d.content,
            }})

    elif args.cmd == "pages":
        import pages as pages_mod

        conn = s._conn
        if args.dir:
            res = pages_mod.ingest_dir(conn, __import__("pathlib").Path(args.dir),
                                       args.project, pattern=args.pattern,
                                       force=args.reindex)
            emit({"ok": True, "indexed": res, "stats": pages_mod.stats(conn)})
        elif args.query:
            rows = pages_mod.search_chunks(conn, args.query, args.project, args.limit)
            emit({"ok": True, "hits": rows})
        else:
            rows = pages_mod.index_list(conn, args.project, args.limit)
            emit({"ok": True, "items": rows, "stats": pages_mod.stats(conn)})

    elif args.cmd == "hook":
        # 会话开场钩子：只回答「这句话像不像在说某个已登记的项目」，
        # 不写库、不注入，由插件决定要不要把结果塞进 inbox。
        #
        # 2026-11-05 改：保留名（工作区/未归类）以前一律不匹配，理由是「零散会话」。
        # 实测 26 个会话里 25 个都是从 default-workspace 发起、9357 步全在这里，
        # 于是自动注入对用户真正的主战场全程失效（auto-inject.log 全是 unmatched）。
        # 现在保留名也走同一套判断：只要真有交接卡或 checkpoint，就照常给指针。
        # 副作用可控——没有卡时依旧不注入，不会平白多付 token。
        import archive

        text = args.text or ""
        project = s.normalize_project(archive.guess_project(text, args.cwd or ""))
        # 预算要和模型真正读的包一致（默认 3000）。用 400 时事实会挤占全部预算，
        # 交接卡被截成 0 token，报出去的 packVersion/packTokens 也就对不上了。
        pack = s.handoff_pack(project)
        if len(text.strip()) < 4 or not (pack.get("hasHandoff") or pack.get("checkpoint")):
            emit({"ok": True, "matched": False})
        else:
            emit({
                "ok": True,
                "matched": True,
                "project": project,
                "packVersion": pack["version"],
                "packTokens": pack["estTokens"],
                "hasHandoff": pack["hasHandoff"],
                "hasCheckpoint": pack["checkpoint"],
                "resume": (pack.get("resume") or {}).get("advice"),
            })

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        sys.stdout.write(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        sys.exit(1)
