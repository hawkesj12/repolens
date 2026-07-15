# repolens

**The agent-context freshness layer.** Ranked, described **hybrid** (lexical + semantic) search over everything in a repo — docs, code, and data — a typed hygiene linter, and a **generated, always-current rule** an AI agent loads at the start of every session so its context never drifts.

Built for repos where an agent works alongside a growing pile of markdown, and where a plain `grep` leaves you sifting. **The core is stdlib-only** — no dependencies, no services, no keys. Semantic search is one opt-in extra (`pip install 'repolens[semantic]'` → `fastembed` + `sqlite-vec`, both CPU, no service); without it, `find` is lexical-only and everything else is unchanged.

> **Own your context window.** LLMs are stateless functions — you get the best output by building the _right_ context programmatically, not by hand-maintaining a static blob that drifts (see [12-Factor Agents, Factor 3](https://github.com/humanlayer/12-factor-agents/blob/main/content/factor-03-own-your-context-window.md)). repolens _generates_ the map. **What stays hand-written is everything that's judgment, not state.**

## What it does

**`repolens find "…"` — where does X live?**
Builds a local SQLite index and answers ranked queries with a one-line description per hit. Ranking is **hybrid**: BM25 (FTS5) for exact-term/identifier precision, fused with dense semantic similarity for paraphrase/meaning recall, combined by Reciprocal Rank Fusion (RRF). With the `[semantic]` extra absent it's BM25-only (and says so once); `--lexical` forces BM25-only on demand.

- **Markdown** — full text.
- **Code / config** — indexed by each file's _purpose line_ (from its docstring or leading comment), so `repolens find "garmin ingest"` returns `scripts/ingest_garmin.py — "Pulls Garmin biometrics into the DB"`, not a wall of matches.
- **Database tables** _(optional)_ — table + column names, so "where do trades live" resolves to a DB table.
- **Frontmatter — any keys, schema-free.** Every YAML frontmatter key is indexed into a sparse `frontmatter(relpath, key, value)` table, so docs with _different_ conventions (`paths:`, `name/description:`, `sector:`) coexist in one repo — repolens imposes no schema and clobbers none. A total, dependency-free parser degrades nested/exotic YAML to searchable text.
- **Respects `.gitignore` by default (in a git repo)** — so secrets, `.env`, and anything you ignore stay out of the index. In a **non-git** directory a `.gitignore` can't be honored — repolens indexes everything there and **prints a warning** so you're never silently exposed. Opt into `include_gitignored` when you _want_ ignored notes searchable (a personal-knowledge-repo mode).
- The index is a **disposable, gitignored cache**: it updates **incrementally** (only changed files, by content hash) and can't drift. `repolens index --rebuild` is the full backstop; delete it and it regenerates.

**`repolens rule` — one visible, self-maintaining orientation file.**
In a Claude Code repo, `rule` writes `.claude/rules/repolens.md`: a **static header** (what / when / who / why / how — written once) plus two **generated, delimited sections** that stay current on their own — **Environment** (the OS + present toolchain with versions) and **Map** (root folders each with a count, plus every DB table grouped by prefix — `fin_*`, `health_*`, … + `core` + `views`). It auto-loads every session like any rule, and unlike an injected blob it's a file you can open, read, and trust — this replaces the old invisible session-start digest, so the map is now **visible**. Outside a Claude Code repo it drops a short static routing instruction ("concept → `repolens find`; exact string → `rg`") into `AGENTS.md`. Non-destructive: skips if present, appends to an existing `AGENTS.md`, never clobbers a rule it didn't author. `init` installs it by default in a Claude Code repo.

**`repolens refresh` — the early-cutoff change-detector (what the hook runs).**
Every session it compares a cheap change-key — `hash(folder-set + DB schema + toolchain)` — to the one stored in the rule. Unchanged (the common case) it's a ~no-op; on a real structural change it regenerates only the Map + Environment blocks (the static header is never touched) and writes atomically. Deterministic, incremental, catches removals for free.

**`repolens lint` — keep the knowledge base honest.**
Zero-LLM structural checks (dead links, empty files, malformed frontmatter, duplicate titles) **and** per-type field checks you declare in config (e.g. a doc in `meetings/` must carry a `**Date:**`). A bundled **pre-commit hook** runs it and blocks a commit on errors — hygiene enforced, not hoped for.

**`repolens enrich` — let the metadata write itself (bring your own model).**
Generates `description` + `tags` frontmatter (and a one-line purpose docstring for code) with a model, so the metadata that powers `find` isn't hand-typed. Two providers, both stdlib-only: an **HTTP endpoint** (`[enrich].model`/`endpoint`, ollama by default) or a **command** (`[enrich].command`) — e.g. `command = "claude -p --model haiku"` runs on your Claude subscription (no API key, compute off your machine). It only **fills missing** fields (never clobbers; `--force` regenerates _and preserves_ your other keys), respects `.gitignore`, and `--dry` previews. It's the **one command that writes to your files**; everything else is read-only and offline.

**`repolens env` / `repolens digest` / `repolens hook` — the plumbing.**
`env` prints the OS + present toolchain (one line); `digest` prints a budgeted repo map; both feed the generated rule sections and also work standalone (any agent/harness can read their stdout). `hook` prints the SessionStart-hook snippet (running `repolens refresh`) — `--install` **additively** merges it into `.claude/settings.json` (never overwrites an existing hook or key); `--check` dry-runs.

## Does it work? (measured, not asserted)

Semantic search is only worth its dependency if it beats plain BM25 on _your_ corpus — so repolens ships with a reproducible 24-query benchmark (a frozen query→gold-doc set across exact-term, conceptual, and paraphrase classes). On that set, hybrid search was **strictly better than lexical-only on the hard (conceptual/paraphrase) queries and never worse on any query** — it recovered cases where the gold doc shared _no words_ with the query and lexical missed it entirely, while tying on exact-term matches. That's the whole point of the hybrid: BM25 nails identifiers and exact terms; the embeddings add recall without ever demoting what BM25 already ranked well. (The win is measured on a same-corpus subset and rests on a handful of queries — see the bake-off for the honest numbers and stats; full-corpus confirmation is pending.) Neither extra installed? You still get ranked BM25.

## Who it's for

A repo that mixes **prose/knowledge with code** and is worked by an **agent that greps on demand rather than maintaining a semantic index** — [Claude Code](https://claude.com/claude-code) being the prime example. There, `repolens` gives a _ranked, described_ answer across docs + code + data (and, when you opt in, your gitignored notes), plus lightweight enforced hygiene.

## What it's _not_

Not a replacement for `ripgrep` (use `rg` for exhaustive literal/regex code search), not a full RAG _system_ (it's the retrieval layer an agent does RAG _with_ — it finds; the agent answers), and not a knowledge-management app. It's a hybrid findability + hygiene layer with one deliberate edge: it sees the _whole_ corpus — prose, code purpose-lines, and DB schema — and keeps it clean.

### When to use `repolens find` vs `rg`

**Grep when you know the string; `repolens find` when you know the _concept_ but not the file.**

- **`rg` / grep** → you know the literal string or regex, or you need _every_ match. Fast, complete, literal.
- **`repolens find`** → _"where does X live / which file handles Y"_ — you want the _right few_ files, ranked and described, across docs + code purpose-lines + DB schema (plus your gitignored notes, when you opt in).

**Two things worth knowing about matching.** A multi-word query is **all-terms** (every word must appear in the same file); if that returns nothing, repolens automatically **broadens to any-term** and tells you on stderr — so `find "garmin deploy"` still surfaces the closest files even when no single doc has both words. And matching is **stemmed** (`ranking` finds `ranked`) but does **not** split identifiers — search `parse` or `frontmatter`, not `parseFrontmatter`, to match a `camelCase`/`snake_case` name.

## Install

```sh
pipx install repolens                 # core — stdlib-only, lexical (BM25) find
pipx install 'repolens[semantic]'     # + hybrid semantic search (fastembed + sqlite-vec, CPU, no service)
# or: uv tool install repolens[semantic]
```

Requires Python 3.11+. The `[semantic]` extra adds `fastembed` (ONNX embeddings, no PyTorch/CUDA/service) and `sqlite-vec`; both run on CPU.

> **First hybrid build is a one-time cost.** With the extra installed, the first index build **embeds your whole corpus** — fastembed downloads the model (`BAAI/bge-base-en-v1.5`, ~0.2 GB) once, then embeds every doc's chunks on CPU. Budget **roughly a few seconds per document** (a few hundred large markdown files can take several minutes); `init` prints progress so it isn't a silent hang. It's throttled by default (`[semantic].threads = 2`) so it won't max your machine — raise it for speed on an idle box, or offload embedding entirely to a local GPU via the bring-your-own-embedder option below. After that it's incremental — only _changed_ files re-embed, so day-to-day use is instant. If you'd rather not pay it up front, skip the extra (lexical-only, no downloads) and add it later. On a build without loadable-extension support (`sqlite3` is compiled without it on some platforms — notably stock macOS), repolens automatically falls back from `sqlite-vec` to a numpy brute-force vector search; it announces which path is active.

## Quick start

```sh
cd your-repo
repolens init                 # writes .repometa.toml + .gitignore + a warm index + the pre-commit hook,
                              # auto-discovers your DBs, seeds [env].tools, and — in a Claude Code repo —
                              # writes the self-maintaining rule + wires the SessionStart refresh hook
                              # (additively). --no-hook / --no-rule opt out.
repolens index                # rebuild/update the index (incremental; a disposable cache)
repolens find "where's the deploy config"
repolens lint
repolens refresh              # regenerate the rule's Map/Environment if structure changed (what the hook runs)
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

# Semantic (hybrid) search — ON by default when the [semantic] extra is installed.
# [semantic]
# model = "BAAI/bge-base-en-v1.5"   # short-passage retriever; fits the ~512 section-bounded chunks
# enabled = true
# chunk_tokens = 512                # per-chunk target; a chunk never crosses a heading
# threads = 2                       # throttle fastembed's CPU (0 = all cores)
# provider = "http"                 # OR bring your own: OpenAI-compatible /v1/embeddings
# endpoint = "http://localhost:11434/v1/embeddings"   # e.g. local Ollama (GPU) or a metered API
# api_key_env = "OPENAI_API_KEY"    # env var holding the key (never stored here)

# SQLite integration — index table/column NAMES (schema only, read-only).
# `repolens init` AUTO-DISCOVERS the DBs in your repo (including gitignored
# ones, skipping *.bak* backups) and fills this in. One or many:
# [integrations.sqlite]
# paths = ["data/app.db", "data/other.db"]   # legacy `path = "..."` also works
```

An explicit `type:` in a doc's YAML frontmatter overrides the folder rule.

**SQLite auto-discovery.** `repolens init` scans for `*.db` / `*.sqlite` / `*.sqlite3` files — **including gitignored ones** (real DBs usually are), skipping backups and its own index cache — and writes the ones it finds into `[integrations.sqlite]` for you, so `repolens find "where do trades live"` resolves to a DB table with no hand-config. It reads **only table and column names** (via `sqlite_master` + `PRAGMA table_info`, opened read-only) — never row data. Pass `repolens init --no-db` to skip it, or edit the `paths` list by hand.

## Use with an agent (Claude Code SessionStart hook)

For [Claude Code](https://claude.com/claude-code), the visible rule (`.claude/rules/repolens.md`) auto-loads every session, and a **SessionStart hook running `repolens refresh`** keeps its Map + Environment current — regenerating only when the folder-set, DB schema, or toolchain actually changes (a ~no-op otherwise). **`repolens init` writes the rule and installs the hook for you** when the repo is a Claude Code repo (a `.claude/` dir exists); these commands adjust it after the fact:

```sh
repolens hook --install          # additively (re)add the SessionStart refresh hook to .claude/settings.json
repolens hook --check            # dry-run: show what it would add, write nothing
repolens hook                    # just print the snippet (no writes)
repolens refresh                 # run the detector by hand (regenerate the rule if structure changed)
```

The install is **non-destructive** — it merges into your existing hooks, never overwrites them, and is idempotent. In a repo with no `.claude/`, `init` writes no agent config and prints a hint instead — it never presumes a harness that isn't there. (The SessionStart hook is one integration; the commands themselves are agent-agnostic.)

> The generated rule (and `digest`/`env`) content is **local agent context** — repo/tool _names_ and versions, never file contents or secrets. It's for your agent, not a shareable artifact.

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

The index (`.repometa/index.db`, gitignored) is a **cache derived from your files** — never the source of truth. It updates **incrementally**: a `files(relpath,size,mtime,hash)` table stat-gates each file and re-indexes only those whose content hash changed (so a `touch` or a fresh clone re-hashes but doesn't re-index), reconciling deletes, all in one WAL transaction. WAL is what lets many agent sessions read (and the odd one refresh) the same index concurrently without locking each other out. With the `[semantic]` extra installed, the same content-hash drives embeddings — a changed file is re-chunked (**section-bounded**: chunks split on Markdown headings and never cross one, ~512 tokens each) and re-embedded, its vectors stored via `sqlite-vec` (or the numpy fallback); `find` fuses the BM25 and dense results with RRF, rolling per-chunk hits up to their parent document. A deleted file's chunks/vectors cascade away. `repolens index --rebuild` is the always-correct full backstop (and what CI runs); the index can't go stale, and anything uncertain just rebuilds.

## Roadmap

See [ROADMAP.md](ROADMAP.md) — **v0.9** shipped the semantic tier (hybrid BM25 + dense RRF `find`) and the self-maintaining rule (generated Map + Environment, refreshed by a change-detector hook). Next: a cross-encoder reranker over the fused top-N, then `llms.txt` export.

## License

MIT © 2026 Justin Hawkes.
