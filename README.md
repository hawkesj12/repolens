# repolens

**The agent-context freshness layer.** Ranked, described search over everything in a repo — docs, code, and data — a typed hygiene linter, and a **generated, always-current digest** an AI agent can load at the start of every session so its context never drifts.

Built for repos where an agent works alongside a growing pile of markdown, and where a plain `grep` leaves you sifting. Stdlib-only Python. No dependencies, no services, no keys.

> **Own your context window.** LLMs are stateless functions — you get the best output by building the _right_ context programmatically, not by hand-maintaining a static blob that drifts (see [12-Factor Agents, Factor 3](https://github.com/humanlayer/12-factor-agents/blob/main/content/factor-03-own-your-context-window.md)). repolens _generates_ the map. **What stays hand-written is everything that's judgment, not state.**

## What it does

**`repolens find "…"` — where does X live?**
Builds a local SQLite (FTS5) index and answers ranked queries with a one-line description per hit:

- **Markdown** — full text.
- **Code / config** — indexed by each file's _purpose line_ (from its docstring or leading comment), so `repolens find "garmin ingest"` returns `scripts/ingest_garmin.py — "Pulls Garmin biometrics into the DB"`, not a wall of matches.
- **Database tables** _(optional)_ — table + column names, so "where do trades live" resolves to a DB table.
- Covers **gitignored / private content** that `.gitignore`-respecting tools skip, and ranks with **BM25** (degrading to a plain `LIKE` search — with a visible warning — if your SQLite lacks FTS5).
- The index is a **disposable, gitignored cache**: it auto-rebuilds when files change and can't drift. Delete it and it regenerates.

**`repolens lint` — keep the knowledge base honest.**
Zero-LLM structural checks (dead links, empty files, malformed frontmatter, duplicate titles) **and** per-type field checks you declare in config (e.g. a doc in `meetings/` must carry a `**Date:**`). A bundled **pre-commit hook** runs it and blocks a commit on errors — hygiene enforced, not hoped for.

**`repolens digest` — a tiny, fresh map for an agent's context.**
A compact, budgeted (`--max-lines`) orientation read from the index — repo name, what's indexed, the busiest dirs, the DB tables, and a routing pointer — for injecting at session start. Orientation, never a dump: more context _degrades_ an agent, so detail stays a pull (`find`).

**`repolens env` — the toolchain, detected not asserted.**
One line: the OS plus the **present** tools (with versions) from a config allowlist that `init` auto-seeds from your repo's manifests. Absent tools are simply omitted. So an agent knows what it can actually run — correct on every machine, no drift.

**`repolens hook` — wire it in without clobbering anything.**
Prints a SessionStart-hook snippet by default; `--install` **additively** merges it into your repo's `.claude/settings.json` (never overwrites an existing hook or key); `--check` dry-runs.

## Who it's for

A repo that mixes **prose/knowledge with code** and is worked by an **agent that greps on demand rather than maintaining a semantic index** — [Claude Code](https://claude.com/claude-code) being the prime example. There, `repolens` gives a _ranked, described_ answer across docs + code + data including your private notes, plus lightweight enforced hygiene.

## What it's _not_

Not a replacement for `ripgrep` (use `rg` for exhaustive literal/regex code search), not a semantic/embeddings index like Cursor or Aider's repo-map, not a RAG system, and not a knowledge-management app. It's a lexical findability + hygiene layer with one deliberate edge: it sees the _whole_ corpus — prose, code purpose-lines, DB schema, and gitignored content — and keeps it clean.

### When to use `repolens find` vs `rg`

**Grep when you know the string; `repolens find` when you know the _concept_ but not the file.**

- **`rg` / grep** → you know the literal string or regex, or you need _every_ match. Fast, complete, literal.
- **`repolens find`** → _"where does X live / which file handles Y"_ — you want the _right few_ files, ranked and described, across docs + code purpose-lines + DB schema + gitignored content.

repolens is **lexical (BM25)**, not embeddings — deliberately: for code, lexical ranking often beats dense retrieval and costs nothing.

## Install

```sh
pipx install repolens        # or: uv tool install repolens
```

Requires Python 3.11+.

## Quick start

```sh
cd your-repo
repolens init                 # writes .repometa.toml + .gitignore entry + the pre-commit hook;
                              # auto-discovers your DBs and seeds [env].tools from your manifests
repolens index                # build the index (~fast; a disposable cache)
repolens find "where's the deploy config"
repolens lint
repolens digest               # tiny repo map (for a hook)
repolens env                  # OS + present toolchain (one line)
repolens hook                 # print a SessionStart-hook snippet (add --install to wire it in)
```

## Configure (`.repometa.toml`)

`repolens init` drops a commented starter. Declare your typed records by folder:

```toml
[types.meeting]
folder = "meetings"
recursive = true               # classify subfolders too
exclude = ["*draft*"]          # artifacts, not records
require = ["^\\*\\*Date:\\*\\*"]  # regex a conforming doc must contain (a warn if missing)

# SQLite integration — index table/column NAMES (schema only, read-only).
# `repolens init` AUTO-DISCOVERS the DBs in your repo (including gitignored
# ones, skipping *.bak* backups) and fills this in. One or many:
# [integrations.sqlite]
# paths = ["data/app.db", "data/other.db"]   # legacy `path = "..."` also works
```

An explicit `type:` in a doc's YAML frontmatter overrides the folder rule.

**SQLite auto-discovery.** `repolens init` scans for `*.db` / `*.sqlite` / `*.sqlite3` files — **including gitignored ones** (real DBs usually are), skipping backups and its own index cache — and writes the ones it finds into `[integrations.sqlite]` for you, so `repolens find "where do trades live"` resolves to a DB table with no hand-config. It reads **only table and column names** (via `sqlite_master` + `PRAGMA table_info`, opened read-only) — never row data. Pass `repolens init --no-db` to skip it, or edit the `paths` list by hand.

## Use with an agent (Claude Code SessionStart hook)

`digest` and `env` just print to **stdout**, so they work with any agent or harness. For [Claude Code](https://claude.com/claude-code), a **SessionStart hook** injects their output into context fresh every session — a repo map and toolchain that regenerate rather than drift:

```sh
repolens hook --install          # additively adds a SessionStart hook to .claude/settings.json
repolens hook --install --with-env   # also run `repolens env` in the hook
repolens hook --check            # dry-run: show what it would add, write nothing
```

The install is **non-destructive** — it merges into your existing hooks, never overwrites them, and is idempotent. (The SessionStart hook is one integration; the commands themselves are agent-agnostic.)

> The `digest`/`env` output is **local agent context** — repo/tool _names_ and versions, never file contents or secrets. It's for your agent, not a shareable artifact.

## How it works

The index (`.repometa/index.db`, gitignored) is a **cache derived from your files** — never the source of truth. A full rebuild is fast for small/medium repos and can't go stale. Staleness is a local fast-path; anything uncertain just rebuilds.

## Roadmap

See [ROADMAP.md](ROADMAP.md) — next up is **v0.4 incremental indexing** (only re-index changed files) for large repos.

## License

MIT © 2026 Justin Hawkes.
