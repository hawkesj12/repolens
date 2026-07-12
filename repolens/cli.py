"""repolens.cli — the `repolens` command: init | index | find | lint | digest | env | hook."""

from __future__ import annotations

import argparse
import json
import stat
import sys
import time

from . import (
    __version__,
    digest,
    discover,
    env,
    find,
    hookgen,
    index,
    lint,
    root,
    templates,
)


def _ctx():
    r = root.find_root()
    return r, root.load_config(r)


# ═══════════════════════════════════════════════════════════════
# cmd_init()
# ═══════════════════════════════════════════════════════════════
# Scaffold a repo: write .repometa.toml (marks the root), gitignore
# the index cache, and install the pre-commit lint hook if this is a
# git repo. Idempotent unless --force.
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
        # Auto-seed the `repolens env` toolchain from this repo's manifests.
        stack = env.detect_stack(r)
        if stack:
            with open(cfg_path, "a", encoding="utf-8") as f:
                f.write(templates.active_env_block(stack))
            print(f"env toolchain: {', '.join(stack)}")

    gi = r / ".gitignore"
    line = ".repometa/"
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

    # SessionStart digest hook — wired by DEFAULT, but only when this is a Claude
    # Code repo (a `.claude/` dir already exists), so init never presumes a harness
    # that isn't here. The install is ADDITIVE: hookgen.install integrates with any
    # existing hooks and never clobbers them. --no-hook opts out.
    if not args.no_hook:
        if (r / ".claude").is_dir():
            print(hookgen.install(r, with_env=True))
        else:
            print(
                "(no .claude/ — skipped the SessionStart digest hook; "
                "run `repolens hook --install` if you use Claude Code)"
            )
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


def cmd_find(args) -> int:
    r, cfg = _ctx()
    query = " ".join(args.query)
    t0 = time.time()
    status = find.ensure_fresh(r, cfg, refresh=not args.no_refresh)
    hits = find.search(cfg, query, args.k)
    ms = (time.time() - t0) * 1000
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


def cmd_digest(args) -> int:
    r, cfg = _ctx()
    print(digest.build_digest(r, cfg, args.max_lines, full=args.full))
    return 0


def cmd_env(args) -> int:
    _r, cfg = _ctx()
    print(env.probe_env(cfg))
    return 0


def cmd_hook(args) -> int:
    r, _cfg = _ctx()
    with_env = not args.no_env
    if args.install or args.check:
        print(hookgen.install(r, with_env=with_env, check=args.check))
    else:
        print(hookgen.snippet(with_env=with_env))
    return 0


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
        help="scaffold .repometa.toml + gitignore + hooks (pre-commit lint + SessionStart digest/env)",
    )
    p_init.add_argument("--force", action="store_true", help="overwrite existing files")
    p_init.add_argument(
        "--no-db", action="store_true", help="skip SQLite auto-discovery"
    )
    p_init.add_argument(
        "--no-hook",
        action="store_true",
        help="don't install the SessionStart digest/env hook (auto-installed in Claude Code repos)",
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
    p_find.set_defaults(func=cmd_find)

    p_lint = sub.add_parser("lint", help="corpus hygiene + typed-record checks")
    p_lint.add_argument("--json", action="store_true")
    p_lint.add_argument(
        "--strict", action="store_true", help="exit 1 on ERRORS only (hook mode)"
    )
    p_lint.add_argument("--stale-days", type=int, default=180)
    p_lint.set_defaults(func=cmd_lint)

    p_digest = sub.add_parser(
        "digest", help="compact, hook-ready repo map (read from the index)"
    )
    p_digest.add_argument(
        "--max-lines", type=int, default=40, help="output budget (default 40)"
    )
    p_digest.add_argument(
        "--full", action="store_true", help="richer tier: per-folder docs + notes"
    )
    p_digest.set_defaults(func=cmd_digest)

    sub.add_parser(
        "env", help="OS + present toolchain, one line (for a SessionStart hook)"
    ).set_defaults(func=cmd_env)

    p_hook = sub.add_parser(
        "hook", help="print (or --install) a SessionStart hook running digest/env"
    )
    p_hook.add_argument(
        "--install",
        action="store_true",
        help="additively merge into the repo's .claude/settings.json (never clobbers)",
    )
    p_hook.add_argument(
        "--check", action="store_true", help="dry-run: show what --install would add"
    )
    p_hook.add_argument(
        "--no-env",
        action="store_true",
        help="don't also run `repolens env` in the hook (env is on by default)",
    )
    p_hook.set_defaults(func=cmd_hook)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
