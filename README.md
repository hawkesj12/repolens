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
In a Claude Code repo, `rule` writes `.claude/rules/repolens.md`: a **static header** that teaches a fresh agent what repolens is and how to use it (written once), plus two **generated, delimited sections** that stay current on their own — **Environment** (the OS + present toolchain with versions) and **Map** (root folders each with a count and a "what lives here" description — deterministic by default, or [model-written](#let-a-model-write-the-map-map-opt-in) when you set `[map].command` — plus every DB table grouped by prefix — `fin_*`, `health_*`, … + `core` + `views`). It auto-loads every session like any rule, and unlike an injected blob it's a file you can open, read, and trust — this replaces the old invisible session-start digest, so the map is now **visible**. Outside a Claude Code repo it drops a short static routing instruction ("concept → `repolens find`; exact string → `rg`") into `AGENTS.md`. Non-destructive: skips if present, appends to an existing `AGENTS.md`, never clobbers a rule it didn't author. `init` installs it by default in a Claude Code repo.

**`repolens refresh` / `map` / `tidy` — the early-cutoff change-detectors (what the hooks run).**
The rule's two blocks regenerate independently, each gated by its own cheap change-key so neither triggers the other. **`refresh`** (the **SessionStart** hook) compares the `env-key` (`hash(toolchain)`) and rewrites **Environment** on a change — plus the deterministic **Map** when no `[map].command` is set. **`map`** compares the `map-key` (`hash(folder-set + DB schema)`) and rewrites **Map** — model-written or deterministic. **`tidy`** is the **SessionEnd** command (`enrich` fill-only → `map`, each gated) wired when a map command is set, so the session that changed the repo rebuilds the Map on the way out. Unchanged is a ~no-op; the static header is never touched; writes are atomic and catch removals for free.

**`repolens lint` — keep the knowledge base honest.**
Zero-LLM structural checks (dead links, empty files, malformed frontmatter, duplicate titles) **and** per-type field checks you declare in config (e.g. a doc in `meetings/` must carry a `**Date:**`). A bundled **pre-commit hook** runs it and blocks a commit on errors — hygiene enforced, not hoped for.

**`repolens enrich` — let the metadata write itself (bring your own model).**
Generates `description` + `tags` frontmatter (and a one-line purpose docstring for code) with a model, so the metadata that powers `find` isn't hand-typed. Two providers, both stdlib-only: an **HTTP endpoint** (`[enrich].model`/`endpoint`, ollama by default) or a **command** (`[enrich].command`) — e.g. `command = "claude -p --model haiku"` runs on your Claude subscription (no API key, compute off your machine). It only **fills missing** fields (never clobbers; `--force` regenerates _and preserves_ your other keys), respects `.gitignore`, and `--dry` previews. It's the **one command that writes to your files**; everything else is read-only and offline.

**`repolens env` / `repolens digest` / `repolens hook` — the plumbing.**
`env` prints the OS + present toolchain (one line); `digest` prints a budgeted repo map; both feed the generated rule sections and also work standalone (any agent/harness can read their stdout). `hook` prints the SessionStart-hook snippet (running `repolens refresh`) — `--install` **additively** merges it into `.claude/settings.json` (never overwrites an existing hook or key); `--check` dry-runs.

## Does it help? (measured — run `repolens bench` yourself)

Hybrid search earns its dependency only if it beats plain BM25 on _your_ corpus — so repolens ships a committed gold set (`benchmarks/acceptance.jsonl`: 24 query→gold-doc pairs across exact-term, conceptual, and paraphrase classes) and a scorer, `repolens bench`, that runs every query in BOTH modes against the same index and prints recall@k + MRR per class. On this repo's own corpus (bge-base, k=8): **overall recall@8 was 100% hybrid vs 46% lexical-only (MRR 0.640 vs 0.402)**. The conceptual and paraphrase classes are where the embeddings earn their keep (conceptual recall@8: 100% vs 38%), and the exact-term control class did not regress (hybrid MRR 1.000 vs 0.750). In a three-way comparison against ripgrep over full file contents, hybrid led overall (MRR 0.640 vs grep's 0.548, recall@8 100% vs 96%) — indexing code docstrings cut grep's once-decisive conceptual edge (MRR gap 0.185) to a sliver (0.057). **Honestly, though:** that's one small corpus and 24 queries — a real, reproducible signal, not a statistically significant study — and hybrid is not universally better: on 3 of the 24 queries it ranked the gold doc one position below lexical. Rankers trade individual queries; the aggregate is what improved. Neither extra installed? You still get ranked BM25.

## Who it's for

A repo that mixes **prose/knowledge with code** and is worked by an **agent that greps on demand rather than maintaining a semantic index** — [Claude Code](https://claude.com/claude-code) being the prime example. There, `repolens` gives a _ranked, described_ answer across docs + code + data (and, when you opt in, your gitignored notes), plus lightweight enforced hygiene.

## What it's _not_

Not a replacement for `ripgrep` (use `rg` for exhaustive literal/regex code search), not a full RAG _system_ (it's the retrieval layer an agent does RAG _with_ — it finds; the agent answers), and not a knowledge-management app. It's a hybrid findability + hygiene layer with one deliberate edge: it sees the _whole_ corpus — prose, code docstrings + purpose-lines, and DB schema — and keeps it clean.

### When to use `repolens find` vs `rg`

**Grep when you know the string; `repolens find` when you know the _concept_ but not the file.**

- **`rg` / grep** → you know the literal string or regex, or you need _every_ match. Fast, complete, literal.
- **`repolens find`** → _"where does X live / which file handles Y"_ — you want the _right few_ files, ranked and described, across docs + code docstrings/purpose-lines + DB schema (plus your gitignored notes, when you opt in).

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
repolens bench                # score hybrid vs lexical on the committed gold set (recall@k + MRR)
repolens lint
repolens refresh              # SessionStart: regenerate Environment (+ deterministic Map) if structure changed
repolens map                  # SessionEnd: regenerate the Map (model-written if [map].command set); --force ignores the gate
repolens tidy                 # SessionEnd: enrich (fill-only) then map — the one-command maintenance pass
repolens env                  # OS + present toolchain (one line)
repolens hook                 # print the hook snippet(s) (init already installs them)
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

## When everything runs (triggers & lifecycle)

repolens installs **only event-driven triggers — no cron, no daemon, no file-watcher.** Every trigger is a session lifecycle event, a git hook, or on-demand, so it's portable across OSes with nothing to schedule. The one thing that touches a model (the map writer, and `enrich`) is opt-in and runs off the hot path.

| Subsystem                     | When it runs                                                                                                                                              | Cost                                                      |
| ----------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------- |
| **index** (FTS5 + vectors)    | **Lazily, by its consumers** — before every `find`, at the start of each rule-gen (`refresh`/`map`), and `index --rebuild`. No timer.                     | incremental (content-hash); a changed file = a few chunks |
| **find**                      | On demand (concept search); refreshes the index first                                                                                                     | ms                                                        |
| **refresh** → **Environment** | **SessionStart** hook, `env-key` gated (deterministic)                                                                                                    | ~1 ms no-op                                               |
| **map** → **Map**             | **SessionEnd** hook (via `tidy`) or manual `repolens map`; `map-key` gated. Model-written when `[map].command` is set, else the deterministic folder tree | AI, only on a folder/DB change                            |
| **tidy** (`enrich` → `map`)   | **SessionEnd** hook — installed only when `[map].command` is set                                                                                          | rare (fill-only enrich + gated map)                       |
| **lint**                      | **pre-commit** git hook + manual `repolens lint`                                                                                                          | ms                                                        |
| **enrich**                    | Manual, your scheduler, or the SessionEnd `tidy`                                                                                                          | model calls                                               |

**Why two session events?** The **Environment** block is cheap and deterministic, so it refreshes at **SessionStart** and serves the current session. The **Map** can be expensive (a model writes it) and only matters for the _next_ session, so it regenerates at **SessionEnd** — the session that changed the repo rebuilds the map on the way out, and nothing waits at start. Two independent change-keys (`env-key` = toolchain, `map-key` = folder-set + DB schema) mean a tool-version bump never triggers a Map rebuild and a new folder never rewrites Environment.

```
 SESSION START ─► repolens refresh ─► ensure_fresh (index↺) ─► env-key changed?
                                                                ├─ yes → rewrite ENVIRONMENT
                                                                └─ no  → no-op (~1 ms)

   … during the session …
     repolens find ─────► ensure_fresh (index↺) ─► ranked hybrid search      (on demand)
     git commit ────────► repolens lint --strict  (pre-commit; blocks on errors)

 SESSION END ───► repolens tidy ─────► ensure_fresh (index↺)
                                    ─► enrich (fill-only; only if a model is configured)
                                    ─► map-key changed?
                                          ├─ yes → [map].command (model) → rewrite MAP
                                          └─ no  → no-op
```

The index never runs on a timer — it refreshes as a side effect of whatever reads it (`find`, `refresh`, `map` all call `ensure_fresh` first), so nothing ever queries a stale index, and the SessionEnd `map` sees the session's new files because it re-indexes before checking the map-key.

## Let a model write the Map (`[map]`, opt-in)

By default the **Map** is a deterministic folder tree — accurate, but it can only show folder names + counts. Set `[map].command` and repolens instead hands the folder facts to a model that **reads each folder and writes a rich "what lives here" description** — the same bring-your-own-model pattern as `enrich`:

```toml
[map]
command = "claude -p --model sonnet"   # any CLI: folder facts on stdin → Map body on stdout
```

The model only _produces_ the Map body; repolens still **owns the file** — it frames the section, splices the output, writes atomically, and stamps the map-key, with a **hard fallback to the deterministic render** on any failure (empty output, timeout, crash), so a configured command can never leave a broken rule. With the command set, `init`/`hook` also wire the SessionEnd `tidy` so the Map rebuilds itself on a folder change. Empty command (the default) = the deterministic Map, no model needed. `repolens map --force` regenerates on demand.

## Keep enrichment fresh (schedule it, or ride the SessionEnd hook)

`enrich` is **fill-only** — it writes a `description`/`tags` only where one is missing. So running it on a schedule means every _new_ doc gets described automatically, and nothing you hand-wrote is ever touched. repolens ships the command; **you wire the trigger**.

**The simplest trigger is no scheduler at all.** In a Claude Code repo with `[map].command` set, `enrich` rides the **SessionEnd `tidy`** hook — it runs fill-only on the way out of each session, so new docs get described without a cron, launchd job, or file-watcher. Because it's fill-only and level-triggered, a missed session-end just defers to the next one; nothing drifts. Prefer an explicit schedule? A nightly run is plenty (search itself never goes stale — the index re-reads changed files on every `find`; only the one-line summary lags, and only until the next run):

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
