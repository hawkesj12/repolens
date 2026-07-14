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
- **Frontmatter — any keys, schema-free.** Every YAML frontmatter key is indexed into a sparse `frontmatter(relpath, key, value)` table, so docs with _different_ conventions (`paths:`, `name/description:`, `sector:`) coexist in one repo — repolens imposes no schema and clobbers none. A total, dependency-free parser degrades nested/exotic YAML to searchable text.
- **Respects `.gitignore` by default (in a git repo)** — so secrets, `.env`, and anything you ignore stay out of the index. Enforcement uses `git`, so in a **non-git** directory a `.gitignore` can't be honored — repolens indexes everything there and **prints a warning** so you're never silently exposed. Opt into `include_gitignored` when you _want_ gitignored notes searchable (a personal-knowledge-repo mode). Ranks with **BM25** (degrading to a plain `LIKE` search — with a visible warning — if your SQLite lacks FTS5).
- The index is a **disposable, gitignored cache**: it updates **incrementally** (only changed files, by content hash) and can't drift. `repolens index --rebuild` is the full backstop; delete it and it regenerates.

**`repolens lint` — keep the knowledge base honest.**
Zero-LLM structural checks (dead links, empty files, malformed frontmatter, duplicate titles) **and** per-type field checks you declare in config (e.g. a doc in `meetings/` must carry a `**Date:**`). A bundled **pre-commit hook** runs it and blocks a commit on errors — hygiene enforced, not hoped for.

**`repolens digest` — a rich, fresh map for an agent's context.**
A budgeted (`--max-lines`) orientation read from the index — repo name + purpose, **root folders each with a one-line purpose** (from a folder's `description` frontmatter or its README), the **database with every table grouped by prefix** (`fin_*`, `health_*`, … + `core` + `views`), and a routing pointer — for injecting at session start. Rich via _selection + grouping_, not volume: `--full` adds per-folder docs; detail otherwise stays a pull (`find`).

**`repolens env` — the toolchain, detected not asserted.**
One line: the OS plus the **present** tools (with versions) from a config allowlist that `init` auto-seeds from your repo's manifests. Absent tools are simply omitted. So an agent knows what it can actually run — correct on every machine, no drift.

**`repolens hook` — wire it in without clobbering anything.**
Prints a SessionStart-hook snippet by default; `--install` **additively** merges it into your repo's `.claude/settings.json` (never overwrites an existing hook or key); `--check` dry-runs.

**`repolens enrich` — let the metadata write itself (bring your own model).**
Generates `description` + `tags` frontmatter (and a one-line purpose docstring for code) with a model, so the metadata that powers `find`/`digest` isn't hand-typed. Two providers, both stdlib-only: an **HTTP endpoint** (`[enrich].model`/`endpoint`, ollama by default) or a **command** (`[enrich].command`) — e.g. `command = "claude -p --model haiku"` runs on your Claude subscription (no API key, compute off your machine). It only **fills missing** fields (never clobbers; `--force` regenerates _and preserves_ your other keys), respects `.gitignore`, and `--dry` previews. It's the **one command that writes to your files**; everything else is read-only and offline.

**`repolens rule` — teach the agent to use repolens.**
A search tool the agent doesn't know to reach for is dead weight. `rule` writes a short routing instruction ("concept → `repolens find`; exact string → `rg`") where an agent actually reads it — `.claude/rules/repolens.md` (auto-loads every session in Claude Code) or `AGENTS.md` at the root. Non-destructive (skips if present, appends to an existing `AGENTS.md`); `init` installs it by default in a Claude Code repo.

## Who it's for

A repo that mixes **prose/knowledge with code** and is worked by an **agent that greps on demand rather than maintaining a semantic index** — [Claude Code](https://claude.com/claude-code) being the prime example. There, `repolens` gives a _ranked, described_ answer across docs + code + data (and, when you opt in, your gitignored notes), plus lightweight enforced hygiene.

## What it's _not_

Not a replacement for `ripgrep` (use `rg` for exhaustive literal/regex code search), not a semantic/embeddings index like Cursor or Aider's repo-map, not a RAG system, and not a knowledge-management app. It's a lexical findability + hygiene layer with one deliberate edge: it sees the _whole_ corpus — prose, code purpose-lines, and DB schema — and keeps it clean. It **respects `.gitignore` by default in a git repo** (a non-git directory can't enforce it, and repolens warns when that happens); opt into `include_gitignored` when you want your ignored notes searchable too.

### When to use `repolens find` vs `rg`

**Grep when you know the string; `repolens find` when you know the _concept_ but not the file.**

- **`rg` / grep** → you know the literal string or regex, or you need _every_ match. Fast, complete, literal.
- **`repolens find`** → _"where does X live / which file handles Y"_ — you want the _right few_ files, ranked and described, across docs + code purpose-lines + DB schema (plus your gitignored notes, when you opt in).

repolens is **lexical (BM25)**, not embeddings — deliberately: for code, lexical ranking often beats dense retrieval and costs nothing.

**Two things worth knowing about matching.** A multi-word query is **all-terms** (every word must appear in the same file); if that returns nothing, repolens automatically **broadens to any-term** and tells you on stderr — so `find "garmin deploy"` still surfaces the closest files even when no single doc has both words. And matching is **stemmed** (`ranking` finds `ranked`) but does **not** split identifiers — search `parse` or `frontmatter`, not `parseFrontmatter`, to match a `camelCase`/`snake_case` name.

## Install

```sh
pipx install repolens        # or: uv tool install repolens
```

Requires Python 3.11+.

## Quick start

```sh
cd your-repo
repolens init                 # writes .repometa.toml + .gitignore + the pre-commit hook, auto-discovers
                              # your DBs, seeds [env].tools, and — in a Claude Code repo — wires the
                              # SessionStart digest/env hook (additively). --no-hook opts out.
repolens index                # build the index (~fast; a disposable cache)
repolens find "where's the deploy config"
repolens lint
repolens digest               # tiny repo map (for a hook)
repolens env                  # OS + present toolchain (one line)
repolens hook                 # print the SessionStart-hook snippet (init already installs it)
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

`digest` and `env` just print to **stdout**, so they work with any agent or harness. For [Claude Code](https://claude.com/claude-code), a **SessionStart hook** injects their output into context fresh every session — a repo map and toolchain that regenerate rather than drift. **`repolens init` installs this hook for you** when the repo is a Claude Code repo (a `.claude/` dir exists); these commands are for adjusting it after the fact:

```sh
repolens hook --install          # additively (re)add the SessionStart hook to .claude/settings.json
repolens hook --install --no-env # digest only — skip the `repolens env` line (env is on by default)
repolens hook --check            # dry-run: show what it would add, write nothing
repolens hook                    # just print the snippet (no writes)
```

The install is **non-destructive** — it merges into your existing hooks, never overwrites them, and is idempotent. In a repo with no `.claude/`, `init` writes no agent config and prints a hint instead — it never presumes a harness that isn't there. (The SessionStart hook is one integration; the commands themselves are agent-agnostic.)

> The `digest`/`env` output is **local agent context** — repo/tool _names_ and versions, never file contents or secrets. It's for your agent, not a shareable artifact.

## Keep enrichment fresh (schedule it)

`enrich` is **fill-only** — it writes a `description`/`tags` only where one is missing. So running it on a schedule means every _new_ doc gets described automatically, and nothing you hand-wrote is ever touched. repolens ships the command; **you wire the trigger** — one line in whatever scheduler your OS already has. A nightly run is plenty (search itself never goes stale — the index re-reads changed files on every `find`; only the one-line summary lags, and only until the next run).

**macOS (launchd)** — or just add the line to a script a `launchd` job already runs:

```sh
# in your nightly job, after your own work:
cd /path/to/repo && repolens enrich
```

**Linux (cron)** — `crontab -e`, then:

```cron
# 04:10 nightly — describe any new docs. PATH so cron finds repolens + your model CLI.
10 4 * * *  cd /path/to/repo && PATH="$HOME/.local/bin:$PATH" repolens enrich >> /tmp/repolens-enrich.log 2>&1
```

That's the whole self-maintaining loop — no daemon, no file-watcher, no dependency. If a run is missed (laptop asleep, model server down), the next one just picks up whatever's still undescribed; it can't drift. When you _rewrite_ a doc enough that its summary is stale, refresh that one deliberately: `repolens enrich --force path/to/doc.md`.

> This is why `enrich` needs no `--watch` or OS file-events: a scheduled pass acting on _current state_ self-heals a missed run, where an on-save hook would silently drop it. Cheap detection, expensive work out of the hot path, triggered on your clock.

## How it works

The index (`.repometa/index.db`, gitignored) is a **cache derived from your files** — never the source of truth. It updates **incrementally**: a `files(relpath,size,mtime,hash)` table stat-gates each file and re-indexes only those whose content hash changed (so a `touch` or a fresh clone re-hashes but doesn't re-index), reconciling deletes, all in one WAL transaction. `repolens index --rebuild` is the always-correct full backstop (and what CI runs); the index can't go stale, and anything uncertain just rebuilds.

## Roadmap

See [ROADMAP.md](ROADMAP.md) — **v0.5** shipped incremental indexing, schema-agnostic frontmatter, and the rich digest; next is an optional semantic tier and `llms.txt` export.

## License

MIT © 2026 Justin Hawkes.
