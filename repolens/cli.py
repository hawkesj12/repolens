"""repolens.cli — the `repolens` command: init | index | find | bench | lint."""

from __future__ import annotations

import argparse
import json
import stat
import sys
import time

from . import (
    __version__,
    bench,
    discover,
    find,
    index,
    lint,
    log,
    root,
    templates,
)


def _ctx():
    r = root.find_root()
    return r, root.load_config(r)


# ═══════════════════════════════════════════════════════════════
# cmd_init()
# ═══════════════════════════════════════════════════════════════
# Scaffold a repo: write .repolens.toml (marks the root), auto-wire
# any SQLite DBs, gitignore the index cache, install the pre-commit
# lint hook if this is a git repo, then warm-build the index.
# Idempotent unless --force.
# ═══════════════════════════════════════════════════════════════
def cmd_init(args) -> int:
    r = root.find_root()
    cfg_path = r / root.CONFIG_NAME
    if cfg_path.exists() and not args.force:
        print(f"{root.CONFIG_NAME} already exists (use --force to overwrite)")
    else:
        cfg_path.write_text(templates.DEFAULT_CONFIG, encoding="utf-8")
        print(f"wrote {cfg_path.relative_to(r)}")
        # Auto-discover SQLite DBs and wire them in (only when we freshly wrote
        # the config, so a re-run can't append a duplicate [integrations.sqlite]).
        if not args.no_db:
            dbs = discover.discover_sqlite_dbs(r, root.load_config(r))
            for rel, n in dbs:
                print(f"found {rel} ({n} tables) — indexing its schema")
            if dbs:
                with open(cfg_path, "a", encoding="utf-8") as f:
                    f.write(templates.active_sqlite_block([rel for rel, _ in dbs]))
                print(f"wired {len(dbs)} database(s) into [integrations.sqlite]")

    # Warm build — build the index now (incl. embeddings when the [semantic] extra is
    # installed) so the first session / first `find` isn't cold.
    cfg = root.load_config(r)
    n, code, tables, ms = index.build(r, cfg)
    tbl = f" + {tables} db tables" if tables else ""
    print(f"built index: {n} docs + {code} code{tbl} in {ms:.0f} ms")

    gi = r / ".gitignore"
    line = ".repolens/"
    existing = gi.read_text(encoding="utf-8") if gi.exists() else ""
    if line not in existing:
        with open(gi, "a", encoding="utf-8") as f:
            f.write(
                ("" if existing.endswith("\n") or not existing else "\n") + line + "\n"
            )
        print(f"added '{line}' to .gitignore")

    hooks = r / ".git" / "hooks"
    if hooks.is_dir():
        hook = hooks / "pre-commit"
        if hook.exists() and not args.force:
            print("pre-commit hook exists (use --force to overwrite)")
        else:
            hook.write_text(templates.PRECOMMIT_HOOK, encoding="utf-8")
            hook.chmod(hook.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
            print("installed pre-commit lint hook")
    else:
        print("(no .git found — skipped pre-commit hook)")

    print('repolens initialized. Run `repolens index` then `repolens find "..."`.')
    return 0


def cmd_index(args) -> int:
    r, cfg = _ctx()
    if args.rebuild or not cfg["index_path"].exists():
        n, code, tables, ms = index.build(r, cfg)
        kb = cfg["index_path"].stat().st_size / 1024
        tbl = f" + {tables} db tables" if tables else ""
        print(
            f"built index: {n} docs + {code} code{tbl} in {ms:.0f} ms → {cfg['index_path'].relative_to(r)} ({kb:.0f} KB)"
        )
    else:
        changed, deleted, ms = index.build_incremental(r, cfg)
        print(f"incremental: {changed} changed · {deleted} removed in {ms:.0f} ms")
    if args.optimize:
        index.optimize(cfg)
        print("optimized index")
    return 0


# ═══════════════════════════════════════════════════════════════
# cmd_bench()
# ═══════════════════════════════════════════════════════════════
# Score hybrid vs lexical `find` on the committed gold set
# (benchmarks/acceptance.jsonl by default): recall@k + MRR per
# query-class, both modes against the same fresh index.
# ═══════════════════════════════════════════════════════════════
def cmd_bench(args) -> int:
    r, cfg = _ctx()
    gold_path = r / args.set
    if not gold_path.exists():
        print(f"gold set not found: {gold_path}", file=sys.stderr)
        return 1
    try:
        gold = bench.load_gold(gold_path)
    except ValueError as e:
        print(f"bad gold set: {e}", file=sys.stderr)
        return 1
    try:
        find.ensure_fresh(r, cfg)
    except Exception as e:  # noqa: BLE001 — degrade, don't crash the bench
        print(
            f"⚠ index refresh failed ({e}) — benchmarking the existing index",
            file=sys.stderr,
        )
    result = bench.run(cfg, gold, args.k)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(bench.format_report(result))
    return 0


def cmd_find(args) -> int:
    r, cfg = _ctx()
    query = " ".join(args.query)
    t0 = time.time()
    # A refresh failure (write-lock timeout, a down embedder endpoint, a model-load
    # error) must never deny the user the results already in the index — warn and
    # search what's there rather than crash with a traceback.
    try:
        status = find.ensure_fresh(r, cfg, refresh=not args.no_refresh)
    except Exception as e:  # noqa: BLE001 — degrade, don't crash `find`
        print(
            f"⚠ index refresh failed ({e}) — searching the existing index",
            file=sys.stderr,
        )
        status = "refresh failed — stale index"
    hits = find.search(cfg, query, args.k, lexical_only=args.lexical)
    ms = (time.time() - t0) * 1000
    log.event(
        cfg,
        "find",
        query=query,
        mode="lexical" if args.lexical else "hybrid",
        k=args.k,
        n_hits=len(hits),
        hits=[{"relpath": h["relpath"], "score": h["score"]} for h in hits],
        ms=round(ms),
    )
    if args.json:
        print(json.dumps({"query": query, "status": status, "hits": hits}, indent=2))
        return 0 if hits else 1
    note = f"  [{status}]" if status else ""
    print(f'find: "{query}"  —  {len(hits)} hits ({ms:.0f} ms){note}')
    if not hits:
        print("    (no matches — try broader terms)")
        return 1
    for h in hits:
        tag = "[DB]" if h["kind"] == "db-table" else "    "
        title = f"   — {h['title']}" if h["title"] else ""
        print(f"    {tag} {h['relpath']}{title}")
        snip = h.get("snippet", "")
        # Skip the passage when it just repeats the title (e.g. a code file whose only
        # indexed text is its one-line purpose) — no point echoing it.
        if snip and snip.strip() != (h["title"] or "").strip():
            print(f"           │ {snip}")
    return 0


def cmd_lint(args) -> int:
    r, cfg = _ctx()
    findings = lint.lint(r, cfg, stale_days=args.stale_days)
    if args.strict:  # hook mode: exit 1 on errors only, print them
        errs = [f for f in findings if f["severity"] == "error"]
        for f in errs:
            print(f"repolens ERROR: {f['path']} — {f['message']}", file=sys.stderr)
        return 1 if errs else 0
    if args.json:
        print(json.dumps(findings, indent=2))
        return 0
    counts = {"error": 0, "warn": 0, "info": 0}
    for f in findings:
        counts[f["severity"]] += 1
    icon = {"error": "✗", "warn": "▲", "info": "·"}
    print(
        f"lint — {counts['error']} error · {counts['warn']} warn · {counts['info']} info"
    )
    for f in findings:
        print(f"  {icon[f['severity']]} {f['check']}  {f['path']}  —  {f['message']}")
    return 1 if counts["error"] else 0


# ═══════════════════════════════════════════════════════════════
# main()
# ═══════════════════════════════════════════════════════════════
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="repolens", description="Ranked repo search + a typed corpus linter."
    )
    ap.add_argument("--version", action="version", version=f"repolens {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser(
        "init",
        help="scaffold .repolens.toml + gitignore + warm index + pre-commit lint hook",
    )
    p_init.add_argument("--force", action="store_true", help="overwrite existing files")
    p_init.add_argument(
        "--no-db", action="store_true", help="skip SQLite auto-discovery"
    )
    p_init.set_defaults(func=cmd_init)

    p_index = sub.add_parser(
        "index", help="update the search index (incremental; --rebuild for full)"
    )
    p_index.add_argument(
        "--rebuild", action="store_true", help="full rebuild from scratch (backstop)"
    )
    p_index.add_argument(
        "--optimize", action="store_true", help="compact the FTS5 index after"
    )
    p_index.set_defaults(func=cmd_index)

    p_find = sub.add_parser("find", help="ranked 'where does X live' search")
    p_find.add_argument("query", nargs="+")
    p_find.add_argument("--k", type=int, default=8, help="max results (default 8)")
    p_find.add_argument("--json", action="store_true")
    p_find.add_argument(
        "--no-refresh", action="store_true", help="skip the staleness rebuild"
    )
    p_find.add_argument(
        "--lexical",
        action="store_true",
        help="BM25-only (skip the semantic half of the hybrid search)",
    )
    p_find.set_defaults(func=cmd_find)

    p_bench = sub.add_parser(
        "bench", help="score hybrid vs lexical find on the committed gold set"
    )
    p_bench.add_argument(
        "--set",
        default="benchmarks/acceptance.jsonl",
        help="gold-set JSONL path, relative to the repo root (default benchmarks/acceptance.jsonl)",
    )
    p_bench.add_argument("--k", type=int, default=8, help="rank cutoff (default 8)")
    p_bench.add_argument("--json", action="store_true")
    p_bench.set_defaults(func=cmd_bench)

    p_lint = sub.add_parser("lint", help="corpus hygiene + typed-record checks")
    p_lint.add_argument("--json", action="store_true")
    p_lint.add_argument(
        "--strict", action="store_true", help="exit 1 on ERRORS only (hook mode)"
    )
    p_lint.add_argument("--stale-days", type=int, default=180)
    p_lint.set_defaults(func=cmd_lint)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
