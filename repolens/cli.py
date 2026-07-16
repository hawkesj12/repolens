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
    enrich,
    env,
    find,
    hookgen,
    index,
    lint,
    root,
    ruledoc,
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

    # Warm build — build the index now (incl. embeddings when the [semantic] extra is
    # installed) so the first session / first `find` isn't cold.
    cfg = root.load_config(r)
    n, code, tables, ms = index.build(r, cfg)
    tbl = f" + {tables} db tables" if tables else ""
    print(f"built index: {n} docs + {code} code{tbl} in {ms:.0f} ms")

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
        if root.is_claude_repo(r):
            print(hookgen.install(r))
        else:
            print(
                "(no .claude/ — skipped the SessionStart refresh hook; "
                "run `repolens hook --install` if you use Claude Code)"
            )

    # Agent instruction rule — TEACH the agent to use repolens (concept -> find).
    # Auto-written in a Claude Code repo (an auto-loading rule); elsewhere an opt-in
    # AGENTS.md via `repolens rule --install`. --no-rule opts out.
    if not args.no_rule:
        if root.is_claude_repo(r):
            print(ruledoc.install(r))
        else:
            print(
                "(no .claude/ — skipped the agent rule; "
                "run `repolens rule --install` to add an AGENTS.md instruction)"
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


def cmd_enrich(args) -> int:
    r, cfg = _ctx()
    docs, code = enrich.enrich_repo(
        r,
        cfg,
        dry=args.dry,
        force=args.force,
        docs_only=args.docs_only,
        code_only=args.code_only,
    )
    for line in docs + code:
        print(f"  {'[dry] ' if args.dry else ''}{line}")
    verb = "would enrich" if args.dry else "enriched"
    print(
        f"{verb}: {len(docs)} docs · {len(code)} code (model: {cfg['enrich']['model']})"
    )
    if not docs and not code:
        print(
            "  (nothing to fill — or no model server at "
            f"{cfg['enrich']['endpoint']}; start ollama / set [enrich].endpoint)"
        )
    return 0


def cmd_hook(args) -> int:
    r, cfg = _ctx()
    if args.install or args.check:
        print(hookgen.install(r, check=args.check, config=cfg))
    else:
        print(hookgen.snippet(cfg))
    return 0


def cmd_rule(args) -> int:
    r, cfg = _ctx()
    if args.install or args.check:
        print(ruledoc.install(r, check=args.check, config=cfg))
    else:
        print(ruledoc.snippet())
    return 0


def cmd_refresh(args) -> int:
    r, cfg = _ctx()
    msg = ruledoc.refresh(r, cfg)
    print(msg or "repolens rule up to date (no structural change).")
    return 0


def cmd_map(args) -> int:
    r, cfg = _ctx()
    msg = ruledoc.map_refresh(r, cfg, force=args.force)
    print(msg or "map up to date (no folder/DB change).")
    return 0


# ═══════════════════════════════════════════════════════════════
# cmd_tidy()
# ═══════════════════════════════════════════════════════════════
# SessionEnd maintenance, each step gated: enrich fill-only (only when
# an explicit [enrich].command provider is configured, so an
# unconfigured repo never attempts failed model calls at every exit),
# THEN map (model-written when [map].command is set, else deterministic;
# map-key gated). enrich runs first so the map-writer sees fresh
# descriptions. Both are ~no-ops when nothing changed.
# ═══════════════════════════════════════════════════════════════
def cmd_tidy(args) -> int:
    r, cfg = _ctx()
    if cfg["enrich"].get("command"):
        docs, code = enrich.enrich_repo(r, cfg)
        if docs or code:
            print(f"enrich: {len(docs)} docs · {len(code)} code")
    msg = ruledoc.map_refresh(r, cfg)
    print(msg or "map up to date (no folder/DB change).")
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
        help="scaffold .repometa.toml + gitignore + warm index + hooks (pre-commit lint + SessionStart refresh)",
    )
    p_init.add_argument("--force", action="store_true", help="overwrite existing files")
    p_init.add_argument(
        "--no-db", action="store_true", help="skip SQLite auto-discovery"
    )
    p_init.add_argument(
        "--no-hook",
        action="store_true",
        help="don't install the SessionStart refresh hook (auto-installed in Claude Code repos)",
    )
    p_init.add_argument(
        "--no-rule",
        action="store_true",
        help="don't install the agent instruction rule (auto-installed in Claude Code repos)",
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
        "hook",
        help="print (or --install) a SessionStart hook running `repolens refresh`",
    )
    p_hook.add_argument(
        "--install",
        action="store_true",
        help="additively merge into the repo's .claude/settings.json (never clobbers)",
    )
    p_hook.add_argument(
        "--check", action="store_true", help="dry-run: show what --install would add"
    )
    p_hook.set_defaults(func=cmd_hook)

    p_refresh = sub.add_parser(
        "refresh",
        help="regenerate the rule's Environment (+ deterministic Map) when structure changed (SessionStart hook)",
    )
    p_refresh.set_defaults(func=cmd_refresh)

    p_map = sub.add_parser(
        "map",
        help="regenerate the rule's Map when folders/DB changed — model-written if [map].command is set",
    )
    p_map.add_argument(
        "--force", action="store_true", help="regenerate even if unchanged"
    )
    p_map.set_defaults(func=cmd_map)

    p_tidy = sub.add_parser(
        "tidy",
        help="SessionEnd maintenance: enrich (if configured) then map — each gated",
    )
    p_tidy.set_defaults(func=cmd_tidy)

    p_enrich = sub.add_parser(
        "enrich",
        help="generate description/tags frontmatter + code purpose lines (local model)",
    )
    p_enrich.add_argument("--dry", action="store_true", help="preview, write nothing")
    p_enrich.add_argument(
        "--force", action="store_true", help="regenerate even existing fields"
    )
    p_enrich.add_argument("--docs-only", action="store_true")
    p_enrich.add_argument("--code-only", action="store_true")
    p_enrich.set_defaults(func=cmd_enrich)

    p_rule = sub.add_parser(
        "rule",
        help="print (or --install) an agent instruction rule (how to use repolens)",
    )
    p_rule.add_argument(
        "--install",
        action="store_true",
        help="write it to .claude/rules/repolens.md or AGENTS.md (non-destructive)",
    )
    p_rule.add_argument(
        "--check", action="store_true", help="dry-run: show what --install would write"
    )
    p_rule.set_defaults(func=cmd_rule)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
