# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- **Errors are no longer swallowed indiscriminately.** Several `except Exception:`
  handlers caught every possible failure and moved on silently. Each now catches only what
  can actually occur there — `OSError` when reading a file, `(OSError, TypeError,
ValueError)` when writing the optional event log, `(ImportError, AttributeError)` when
  quieting the embedder's logging. Anything unexpected now surfaces instead of hiding.
  The model-load handler still catches broadly on purpose: its job is to degrade to
  lexical-only search rather than let a bad model break `find`.

- **Windows: repo-relative paths are now always stored in posix form.** They are
  identifiers — written into the index, into `repolens.toml`, and compared against
  `git ls-files` output, which is always forward-slashed. On Windows the native
  separator leaked in, so stored keys stopped matching (frontmatter lookups silently
  returned nothing), `repolens init` wrote `paths = ["data\app.db"]` into TOML where
  `\a` is an escape sequence, and — worst — every file compared unequal to the
  gitignore allowlist, so **indexing inside a git repo produced an empty index.**
- **Windows: text I/O is explicitly UTF-8.** Python defaults text reads to the system
  locale there (usually cp1252), which maps all 256 bytes and so never raises — it
  silently mojibakes. Indexing a document containing an em-dash or curly quotes stored
  mangled text with no error.

### Changed

- **CI now pins its own tools.** The test matrix installed `ruff`, `pytest`, and `mypy`
  unpinned, so a new upstream release could turn the build red on a commit that changed
  nothing — which is exactly what happened when ruff 0.16 enabled new rules. Versions are
  now explicit and upgraded deliberately. No effect on the published package.
- **CI now runs on Windows** as well as Linux and macOS (9 jobs). The two Windows bugs
  above were found by the first run of that matrix.

## [0.13.1] - 2026-07-23

### Fixed

- **Changelog history and release metadata.** The 0.10.0 (`find` shows the matching
  passage) and 0.11.0 (semantic search as a default dependency) entries had been
  mis-filed inside a single overloaded 0.12.0 section; they are restored to their own
  dated sections. The version compare-links in the footer, which stopped at v0.7.2, now
  cover every release through v0.13.1, and git tags v0.8.0–v0.13.0 (previously missing)
  were published. No code changes — this is a documentation/release-hygiene patch so the
  packaged history matches the repo.

## [0.13.0] - 2026-07-20

### Added

- **`repolens init` now scaffolds a Claude Code agent routing rule** at
  `.claude/rules/repolens.md` — a small, provider-agnostic rule telling an AI agent
  working in the repo to prefer `repolens find` (ranked, passage-returning) over a blind
  grep, and to fall back to `rg` only for exact-string / regex needs. It lands **only in
  repos you've `init`'d**, so repos without repolens keep grepping normally — no global
  assumption. Parallels the existing pre-commit-hook and DB auto-wiring: init sets up the
  index _and_ teaches the agent to use it. `--no-claude` opts out; an existing file is
  preserved unless `--force`. New `templates.claude_rule()` + tests.

## [0.12.0] - 2026-07-19

### Added

- **Configurable per-model embedding prefixes (`[semantic].query_prefix` / `doc_prefix`).**
  Retrieval models need different asymmetric query/document instructions; previously only
  nomic's were applied and every other model (HTTP or fastembed) got raw text. nomic still
  auto-defaults its `search_query:` / `search_document:` prefixes for back-compat; bge and
  most others correctly stay raw when unset; mxbai/arctic/E5-family models now declare their
  own. Tests cover the prefix matrix (`semantic.py`, `root.py`).
- **Embedding-model bakeoff in the README** ("Which embedding model — and does the provider
  matter?"). A 6-way comparison (mxbai-embed-large · arctic-embed2 · bge-m3 · bge-base ·
  nomic) on a 123-page prose corpus with a **vocabulary-independent 30-query gold set**.
  Finding: retrieval quality is a near-tie across the top models (including the zero-config
  fastembed default); the real differentiator is **provider speed** — Ollama (GPU + resident)
  vs fastembed (CPU + per-query reload). Recommendation: Ollama + `mxbai-embed-large` for
  query-heavy/agent use; fastembed + `bge-base-en-v1.5` stays the zero-config default.
- **`repolens index --threads N`** — override `[semantic].threads` (the fastembed CPU-core
  cap) for one build; `--threads 0` uses all cores for a fast one-off rebuild.
- **Back-compat read of a pre-0.11 `.repometa.toml`.** When `.repolens.toml` is absent,
  repolens reads a legacy `.repometa.toml` with a one-time deprecation warning, so an
  un-migrated repo keeps its config instead of silently falling back to defaults.
- **Private, opt-in event logging (`[log].enabled`).** When on, repolens appends one
  JSON line per `find` (query, mode, hits + scores, timing) and per embed (file, chunk
  count, model, timing) to `.repolens/events.jsonl` — inside the gitignored cache dir,
  so it stays local and is never committed. Off by default; writes never raise (a
  logging failure can't break a search). The find log accumulates the real queries run
  against a repo — future material for growing the benchmark from actual usage. New
  `repolens/log.py`; hooked in `cmd_find` and `semantic.embed_doc`.
- **`repolens bench` + a committed gold set (`benchmarks/acceptance.jsonl`).** The
  reproducible answer to "does ranking, and then the semantic half, actually help?":
  18 query→gold-doc pairs across exact / conceptual / paraphrase classes, scored as
  recall@k + MRR in THREE modes against the same corpus — a literal **grep** baseline,
  **lexical** (BM25), and **hybrid** (BM25 + semantic), so the table reads as the
  progression grep → BM25 → hybrid. Measured on this repo's own corpus (bge-base, k=8):
  overall recall@8 grep 78% / lexical 50% / hybrid 100%; MRR 0.39 / 0.34 / 0.67. Grep
  beats the lexical arm (it reads full file bodies, which the code index reduces to a
  purpose-line + docstring), but hybrid beats grep decisively.
- **Code docstrings are indexed and embedded.** A code file used to be searchable by
  ONE line (the extracted purpose-line); a three-way bench against ripgrep showed
  grep beat hybrid on conceptual queries (MRR 0.518 vs 0.333) solely because grep
  reads the docstrings the index threw away. `purpose.extract_doc` now keeps the
  full module docstring / leading comment block (capped at 1500 chars) as the code
  file's BM25 body + embedded text; the one-line purpose stays the display title.
  After the change hybrid leads grep overall (MRR 0.640 vs 0.548, recall@8 100% vs
  96%) and on the paraphrase (0.442 vs 0.233) and exact (1.000 vs 0.875) classes;
  grep keeps a slim conceptual-MRR edge (0.536 vs 0.479), cut from a 0.185 gap to
  0.057. Existing indexes need one `repolens index --rebuild` to pick this up (a
  content hash can't detect an extraction-rule change).
- **Code purpose-lines are embedded** (one short chunk per code file). Dense retrieval
  previously covered markdown only, so on a code repo the semantic half could only
  surface prose docs and RRF demoted correct code hits (e.g. "nearest neighbor vector
  lookup" ranked `semantic.py` #1 lexical but #4 hybrid; it now ranks #1 hybrid). A
  code file with no extractable purpose-line keeps its BM25 path/filename signal and
  simply gets no vector.

### Fixed

- **`find` no longer hangs behind a proxy that black-holes HuggingFace.** A network that
  accepts the connection then drops it made the first model load hang with no timeout, and
  the per-process failure flag re-hung every subsequent `find`. Now: a HuggingFace
  read-timeout fails a stalled connection fast (without aborting a slow-but-flowing
  download), a wall-clock backstop (env `REPOLENS_MODEL_LOAD_TIMEOUT`) catches any other
  stall, and the failure is **persisted to a TTL sentinel file** so sibling processes
  degrade to lexical immediately instead of re-hanging — self-healing after the TTL.
- **`repolens index` degrades on a locked index instead of crashing.** Under write-lock
  contention it raised an uncaught `database is locked` traceback (while `find` already
  degraded); it now prints "index busy" and exits non-zero.
- **No thundering-herd re-embed under concurrency.** One changed file made every concurrent
  `find` redundantly re-embed it off a stale pre-lock snapshot; the write phase now
  re-reads the current stored hash under the lock and skips a file a peer already committed.
- **Enabling semantic on a lexically-built index now backfills embeddings.** An index
  built lexical-first (or after a model/dims change) silently kept running lexical while
  reporting hybrid, because incremental indexing only touches _changed_ files. An
  embedding signature (`model:dims`) is now stored in `meta`; on a mismatch (including an
  index with no vectors, or one whose embeds silently failed), `build_incremental` forces
  a full backfill so hybrid actually runs.
- **A model-load failure degrades once instead of a per-doc retry storm.** `_model()` only
  cached on success, so an offline/cold-cache box re-attempted the load for _every_ doc —
  an effective hang. The failure is now memoized (`available()` flips to lexical), announced
  once, and fastembed's loguru noise is quieted so a graceful degrade doesn't read as a crash.
- **A no-op incremental refresh no longer takes the write lock.** It used to `BEGIN
IMMEDIATE` + commit even when nothing changed, serializing concurrent `find`-refreshers;
  it now detects "nothing changed" under a read connection and returns without a write.
- **repolens no longer indexes its own `.repolens.toml`.** The config file is tooling,
  not corpus, but it was being indexed and showed up as noise in results (the very first
  `find` a new user runs). Added `CONFIG_NAME` to the default `skip_files`.
- **`find` no longer crashes when the embedding endpoint is down at query time.** The
  pre-flight availability check is config-only, so a dead bring-your-own http endpoint
  used to surface as a raw `EmbeddingError` traceback from the dense KNN; `find` now
  degrades to lexical-only for that search and says so once on stderr.
- **Chunking is fence-aware.** The heading splitter treated `#` comment lines inside
  ` ``` `/`~~~` code fences as Markdown headings and cut fenced snippets apart
  mid-block, degrading their embeddings (this repo's own CHANGELOG chunked into 14%
  fewer, cleaner pieces after the fix). Heading detection is now suspended inside a
  fence.

### Changed

- **Fairer grep baseline + reported uncertainty in `bench`.** The grep arm now ranks by
  **distinct query terms matched** (not sum-of-raw-counts, which understated a reasonable
  grep by ~30%), and `bench` prints a deterministic bootstrap **95% CI** on the MRR deltas.
  The honest result: hybrid clearly beats BM25 (ΔMRR +0.33, CI [+0.17, +0.50], excludes
  zero — this is what justifies default-on), but against a fair grep its MRR edge is within
  noise (ΔMRR +0.16, CI [−0.01, +0.33]); hybrid's real win over grep is recall (100% vs
  94%) and the meaning-based queries grep can't reach.
- **Benchmark framing softened + Ollama recommended for heavy use.** The README no longer
  leads with "100% recall@8" (which flatters a 22-file corpus); it leads with MRR + the
  k-stable margin and frames the gold set as a directional signal, not a claim. Added
  guidance: for query-heavy agent use, point `[semantic].provider = "http"` at a resident
  Ollama so the model isn't reloaded per `find` (most of a hybrid query's latency).
- **mypy is now part of CI** (the repo's own type gate; previously ruff + pytest only).
- **Renamed the on-disk footprint `.repometa` → `.repolens`.** The config file is now
  `.repolens.toml` and the disposable index cache is `.repolens/` (was `.repometa.toml`
  / `.repometa/`) — one consistent name matching the tool. The cache is gitignored and
  regenerates, so no data migration is needed; an existing repo just needs its config
  renamed (`git mv .repometa.toml .repolens.toml`) and the stale `.repometa/` removed on
  the next `repolens index`.

### Removed

- **The agent-orientation machinery — `repolens` is now search + lint, one thing done
  well.** Removed the generated-rule / repo-map subsystem and everything that fed it:
  the `refresh`, `map`, `rule`, `hook`, `enrich`, `digest`, and `env` subcommands; the
  `[map]` and `[enrich]` config blocks; and the SessionStart/SessionEnd hook install
  path. The map tried to keep a frozen "what lives here" blob fresh via a change-key
  gate, which drifted (folder-granular key vs file-granular content) and — with a
  `[map].command` set — shelled out to a model from committed config (a code-execution
  surface). The retrieval half already does this better on demand: `repolens find`
  answers "where does X live" per query, always current, no stored artifact to rot.
  `enrich` (model-written metadata) went with it — the semantic tier makes hand-filled
  descriptions unnecessary for recall. `init` no longer installs any agent config; it
  scaffolds the config, index, and pre-commit lint hook. The removed code is preserved
  on the `archive/map-machinery` branch. Distribution is a plain CLI (pipx/PyPI) — not
  a Claude Code plugin. The bench gold set dropped its 6 queries that targeted the
  removed modules (24 → 18), so its numbers reflect only the surface that still ships.

## [0.11.0] - 2026-07-17

### Changed

- **Semantic search is now a default dependency — `pip install repolens` gives hybrid
  out of the box.** `fastembed` + `sqlite-vec` + `numpy` moved from the opt-in
  `[semantic]` extra into the core dependencies, because a new user who missed the extra
  silently got the weaker lexical-only experience. Lexical (BM25) remains — as the
  degrade-fallback when the model can't load and as the benchmark's control — and
  `[semantic].enabled = false` still opts out of the model. The `[semantic]` extra is
  kept as an empty alias so existing install commands still resolve. The core is no
  longer stdlib-only; first run downloads a ~200MB model once (cached under
  `~/.cache/repolens`).

## [0.10.0] - 2026-07-17

### Added

- **`find` shows the matching passage with each hit.** A result now carries the
  passage that actually matched, not just the file path — a semantic hit shows its
  best-matching chunk, a lexical hit shows the FTS5 excerpt around the terms, trimmed
  to one line. The winning chunk was already found and stored; the per-doc rollup was
  discarding it. `cmd_find` prints it under each hit; `--json` gains a `snippet` field.

## [0.9.0] — 2026-07-15

The semantic release: `find` becomes hybrid, and the invisible session digest
becomes one visible, self-maintaining rule. Strictly additive to the existing
SQLite/FTS5 index — no storage migration, and the core stays stdlib-only.

### Added

- **Hybrid `find` (BM25 + semantic, RRF).** Dense retrieval fuses with the existing
  BM25 ranking via Reciprocal Rank Fusion (k=60): BM25 carries exact-term/identifier
  precision, embeddings carry paraphrase/meaning recall, and RRF combines the two
  per-document ranked lists with no score normalization. `--lexical` forces BM25-only.
  (No committed benchmark yet: the hybrid's paraphrase-recall benefit is a directional
  signal from early hand-checks on a docs-heavy corpus, not a measured result, and RRF
  can occasionally re-rank a strong BM25 hit rather than only adding to it — a
  reproducible query→gold benchmark with a runnable scorer is the next step.)
- **Semantic tier as an opt-in extra (`pip install 'repolens[semantic]'`).** Embeddings
  via `fastembed` (ONNX, CPU, no service — no Ollama), default model
  `BAAI/bge-base-en-v1.5` (768-dim, built for short-passage retrieval). Vectors store
  in the same index through `sqlite-vec` (fast `vec0` KNN) with a **numpy brute-force
  cosine fallback** when a Python `sqlite3` build can't load the extension — so semantic
  search works everywhere; the active path is announced.
- **Section-bounded chunking.** Docs split on Markdown heading boundaries — a chunk
  never crosses a heading; a section within ~512 tokens is one chunk, a longer one is
  packed into ~512-token pieces within the section. Chunks embed and roll up to their
  best (min-distance) parent document, so per-doc BM25 and per-chunk vectors are
  fusable. Only changed files re-embed (keyed off the existing content hash); a deleted
  doc's chunks/vectors cascade away.
- **CPU throttle + bring-your-own embedder.** `[semantic].threads` caps fastembed's CPU
  so a big first build stays gentle. `[semantic].provider = "http"` routes embedding to
  any OpenAI-compatible `/v1/embeddings` endpoint (local Ollama/LM Studio or a metered
  API) via stdlib `urllib` — key from an env var, never stored in config. Alternate
  models (`nomic-embed-text-v1.5`, `-Q`) get their required task prefixes automatically.
- **The self-maintaining rule (`.claude/rules/repolens.md`).** A static header (what /
  when / who / why / how) plus generated, delimited **Environment** (toolchain) and
  **Map** (folder tree + DB schema) sections — a visible, openable file that replaces
  the old invisible SessionStart digest.
- **`repolens refresh` — the early-cutoff change-detector.** The SessionStart hook now
  runs this: it compares a `hash(folder-set + DB schema + toolchain)` change-key to the
  one stored in the rule and regenerates only the Map/Environment blocks on a real
  change (a ~no-op otherwise, atomic write). The static header is never touched.
- **`init` warm-build.** `repolens init` now builds the index immediately (including
  embeddings when the extra is installed), so the first session isn't cold.

### Changed

- **SessionStart hook command is now `repolens refresh`** (was `repolens digest &&
repolens env`); the rule carries the map + toolchain, so the map is visible instead
  of injected. `digest` and `env` remain as standalone probes.
- repolens no longer indexes its own generated rule file (`.claude/rules/repolens.md`).
- `SCHEMA_VERSION` → 1.3 (added the `[semantic]` config block + chunks/vectors tables).

## [0.8.0] — 2026-07-14

Hardening pass from an independent three-lens review, ahead of a PyPI release.

### Fixed

- **Root resolution no longer leaks the wrong repo (blocker).** `find_root()` was
  anchored partly to the install location (`__file__`), so an editable / venv-in-repo
  install could resolve, index, search — and, via `enrich`, **write to** — a different
  repo than your working directory. Resolution is now anchored only to your cwd (or an
  explicit start path). Safe to `pip install` however you like, not just via `pipx`.

### Security

- **`.gitignore` boundary is honest outside a git repo.** Ignore rules are enforced via
  `git`, so in a **non-git** directory a `.gitignore` was silently not honored. repolens
  now prints a clear stderr warning when it indexes a non-git directory that has a
  `.gitignore`, instead of exposing ignored files without notice.
- **Symlinks are no longer followed out of the repo.** A file symlink (e.g. to
  `/etc/passwd` or `~/.ssh/id_rsa`) was read and indexed — a real leak for a tool that
  feeds an agent's context. Symlinked files are now skipped during indexing.

### Added

- **`max_file_bytes` (default 5 MB).** Files larger than the cap are skipped at index
  time, guarding against a stray huge file (a generated dump, a vendored blob) bloating
  the disposable index and reading unbounded bytes into memory. Config-overridable.

### Changed

- **Stemmed search.** The FTS5 index now uses the `porter unicode61` tokenizer, so
  `find "ranking"` matches a file whose text says `ranked`. (Identifiers aren't split —
  search `parse`, not `parseFrontmatter`.) Run `repolens index --rebuild` once to pick up
  stemming on an existing index.
- **Any-term broaden on a zero all-term match.** A multi-word `find` is implicit-AND
  (every term must appear in one file); when that yields nothing, repolens now retries as
  any-term (OR) and says so on stderr, instead of silently returning no hits.

## [0.7.2] — 2026-07-14

### Fixed

- **`enrich --force` no longer stacks a second docstring on code files.** Code
  purpose lines are fill-only even under `--force` — an existing docstring is
  authoritative, so `--force` regenerates only DOC frontmatter (which enrich owns)
  and leaves code docstrings alone, instead of prepending a duplicate.

## [0.7.1] — 2026-07-14

### Added

- **`enrich` command provider** — `[enrich].command` runs any CLI that reads the
  prompt on stdin and prints the answer (takes precedence over the HTTP endpoint).
  Set `command = "claude -p --model haiku"` to enrich on your **Claude subscription**
  — no API key, and the compute runs off your machine, not a local model pegging
  your CPU. Any other prompt→text CLI works too. Still stdlib-only.

### Fixed

- **`enrich --force` preserves a doc's other frontmatter keys** — it regenerates the
  managed fields (description/domain/tags) by key-merge now, instead of rewriting the
  whole block, so unrelated keys survive a force pass.
- Rule doc: header calls out "lexical/BM25"; the `<!-- repolens:rule -->` marker moved
  to the bottom so the file opens with its heading.

## [0.7.0] — 2026-07-14

### Added

- **`repolens rule` — teach the agent to _use_ repolens.** A search tool the agent
  doesn't know to reach for is dead weight; repolens shipped the capability but never
  the instruction. `rule` writes a short routing rule ("concept → `repolens find`;
  exact string → `rg`") where an agent actually reads it: `.claude/rules/repolens.md`
  (auto-loads every session in Claude Code) or `AGENTS.md` at the repo root (the
  cross-agent convention). Idempotent + **non-destructive** — skips if already
  present, appends to an existing `AGENTS.md`, never clobbers. `repolens init`
  installs it by default in a Claude Code repo (`--no-rule` opts out); `rule --check`
  dry-runs. This is the missing instruction half of the tool.

## [0.6.1] — 2026-07-14

### Fixed

- **`enrich` no longer writes a bogus `domain` on repo-root files** — a file with
  no parent dir (`CLAUDE.md`, `README.md`) has no domain, so the field is now
  omitted instead of set to the filename (`domain: CLAUDE.md`).

### Added

- **`[enrich.keys]` — write into your own frontmatter schema.** Rename the output
  field per kind (e.g. `description = "summary"`, `tags = "keywords"`), so `enrich`
  fills a repo's _existing_ field names instead of imposing its own — and treats a
  doc that already carries the renamed field as present (no duplicate). `digest`
  reads the same renamed key for folder purposes. Defaults to the kind name, so
  it's zero-config unless you need it.

## [0.6.0] — 2026-07-12

### Added

- **`repolens enrich`** — generate `description` + `tags` frontmatter (and a
  one-line purpose docstring/comment for code) with a **local model**, so the
  metadata that powers `find`/`digest` writes itself. **Bring your own model:** a
  `[enrich]` config block sets `model` (default `llama3.2`), `endpoint` (ollama's
  `/api/generate` shape by default), and `fields` (default `["description","tags"]`;
  add `"domain"`, derived from the top dir, if you want it). It talks to the model
  over **stdlib HTTP** — no Python dependency, and `find`/`lint`/`index`/`digest`
  never touch a model. This is the **one command that writes to your source files**:
  it only FILLS MISSING fields (never clobbers; `--force` regenerates), respects
  `.gitignore` (same walk as the indexer), and `--dry` previews. No model server →
  a clear message, never a crash.

## [0.5.0] — 2026-07-12

### Added

- **Incremental indexing.** `repolens index` now re-indexes only changed files —
  a `files(relpath,size,mtime,hash)` table drives a stat-gate → blake2b hash →
  `DELETE`/`INSERT` upsert → delete-reconcile pass, in one WAL transaction. A
  `touch` or a fresh clone (which resets mtimes) does **not** re-index unchanged
  content (the hash confirms). `repolens index --rebuild` is the always-correct
  full backstop; `--optimize` compacts on demand; FTS5 auto-optimizes every ~200
  changes. Read-path (`find`/`digest`) refreshes are now incremental.
- **Schema-agnostic frontmatter indexing.** A sparse EAV table
  `frontmatter(relpath, key, value)` makes _any_ frontmatter key queryable — a doc
  has no row for a key it lacks, so different conventions (e.g. `paths:`,
  `name/description:`, `sector:`) coexist in one repo with **no schema imposed or
  clobbered**. Parsed by a **total, stdlib-only** flat-frontmatter parser
  (`frontmatter.py`) that degrades nested/malformed YAML to searchable text and
  never raises — no `pyyaml` dependency. The flattened block stays in the FTS index
  for full-text.

### Changed

- **Richer `digest`.** The session-start map now lists **root folders each with a
  one-line purpose** (from a folder's `description` frontmatter or its README/H1)
  and the **database name with every table grouped by prefix** (`fin_*`,
  `health_*`, … + `core` + `views`) instead of a flat list truncated at 12. A new
  `--full` tier adds per-folder docs with their descriptions; `--max-lines`
  (default raised to 40) stays the budget guard; it degrades gracefully with no DB
  / no frontmatter / no README. Richness via selection + grouping, not volume.

## [0.4.3] — 2026-07-12

### Changed

- **`repolens env` now probes tool versions concurrently** — a SessionStart hook
  must stay fast regardless of allowlist size. Sequential probing made total time
  the _sum_ of every tool's `--version` (a heavy one like `streamlit`, or a tool
  whose `--version` hangs to the timeout, dominated). Now the per-tool probes run
  in a thread pool, so wall-time is bounded by the _slowest single_ tool, and the
  per-probe timeout is tightened to 1.5s. Order is preserved.

## [0.4.2] — 2026-07-12

### Fixed

- **`repolens env` now parses `v`-prefixed and hash-suffixed versions correctly.**
  The version regex used a `\b\d` anchor, which has no word boundary between the
  `v` and the digits in `node v25.8.0` — so it skipped the real version and grabbed
  a later token (reported `node 8.0`, `duckdb 5.4`). Now anchored on "a dotted
  number not preceded by a digit/dot," so `v25.8.0 → 25.8.0`, `v1.5.4 …hash → 1.5.4`.

## [0.4.1] — 2026-07-12

### Changed

- **`repolens init` now installs the SessionStart hook by default** — when the
  repo is a Claude Code repo (a `.claude/` dir exists). The install is additive
  (it integrates with any existing hooks and never clobbers them), so the fresh
  repo map is the default payoff of `init`, not a hidden second command. In a
  non-Claude repo, `init` writes no agent config and prints a one-line hint
  instead — it never presumes a harness that isn't there. `init --no-hook` opts out.
- **The SessionStart hook now runs `repolens digest && repolens env`** — the
  freshness pair (the repo map _and_ the real toolchain), env on by default.
  `repolens hook --no-env` drops env. (Replaces the old `--with-env` opt-in.)

## [0.4.0] — 2026-07-12

### Changed

- **`.gitignore` is now respected by default.** The file corpus (markdown + code)
  skips any path git ignores — so secrets, `.env`, and ignored build output stay
  out of the index. Detected via `git ls-files -co --exclude-standard`; a repo
  with no git (or no `git` on PATH) still indexes everything, unchanged.
- Set `include_gitignored = true` under `[repolens]` to index gitignored file
  content too — the personal/knowledge-repo mode (notes you ignore but want
  searchable). SQLite **schema** discovery is unaffected either way: table/column
  names are indexed regardless of gitignore (names only, opt-in via
  `[integrations.sqlite]`), since databases are usually — and safely — ignored.

## [0.3.0] — 2026-07-12

### Added

- **`repolens digest`** — a compact, budgeted (`--max-lines`) repo map read from the
  index (name, what's indexed, busiest dirs, DB tables, and a `find`-vs-`rg` routing
  pointer), for injecting at an agent's session start. Orientation, never a dump.
- **`repolens env`** — an OS-aware, present-only toolchain probe: the OS plus the
  installed tools (with versions) from a `[env].tools` allowlist that `init`
  auto-seeds from the repo's manifests (`pyproject`→python, `package.json`→node, …).
  Robust version probe (timeout, both streams, present-without-version fallback).
- **`repolens hook`** — prints a Claude Code SessionStart-hook snippet by default;
  `--install` **additively** merges it into the repo's `.claude/settings.json`
  (never clobbers an existing hook or key, idempotent); `--check` dry-runs.
- README repositioned as "the agent-context freshness layer" (own-your-context;
  judgment-not-state; agent-agnostic; a `find` vs `rg` routing section).

## [0.2.0] — 2026-07-12

### Added

- **SQLite auto-discovery** on `repolens init` — scans for `*.db` / `*.sqlite` /
  `*.sqlite3` (including gitignored files, skipping `*.bak*` backups and the
  index cache), validates each is a real SQLite database, and writes the ones it
  finds into `[integrations.sqlite]` automatically. Schema-only + read-only (only
  table/column names, never row data). `repolens init --no-db` opts out.
- **Multiple databases** — `[integrations.sqlite]` now accepts a `paths` list;
  the index covers every configured DB's table/column names.

### Changed

- `[integrations.sqlite]` is now a `paths` list. The legacy singular
  `path = "..."` is still accepted (parsed into a one-item list) — no action
  needed on upgrade.

## [0.1.0] — 2026-07-11

### Added

- Initial release.
- `repolens find` — ranked (BM25) search over markdown (full text), code/config
  (by purpose line), and, optionally, SQLite table names; includes gitignored
  content. Plain-`LIKE` fallback (announced) when FTS5 is unavailable.
- `repolens lint` — structural corpus checks (dead links, empty files, malformed
  frontmatter, duplicate titles, staleness) plus config-declared per-type field
  requirements. Enforced by a bundled pre-commit hook (`repolens lint --strict`).
- `repolens index` — build the disposable, gitignored SQLite index (atomic
  rebuild via a temp file + `os.replace`). Full rebuild (incremental is on the
  roadmap for v0.2).
- `repolens init` — scaffold `.repometa.toml`, gitignore the index cache, and
  install the pre-commit hook.
- Config-driven throughout (`.repometa.toml`): repo-root marker, folder→type
  classification with `recursive`/`exclude`/`require`, frontmatter `type:`
  override, configurable skip lists and code extensions, and an optional
  off-by-default SQLite integration.
- Stdlib-only; Python 3.11+.

[Unreleased]: https://github.com/hawkesj12/repolens/compare/v0.13.1...HEAD
[0.13.1]: https://github.com/hawkesj12/repolens/compare/v0.13.0...v0.13.1
[0.13.0]: https://github.com/hawkesj12/repolens/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/hawkesj12/repolens/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/hawkesj12/repolens/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/hawkesj12/repolens/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/hawkesj12/repolens/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/hawkesj12/repolens/compare/v0.7.2...v0.8.0
[0.7.2]: https://github.com/hawkesj12/repolens/compare/v0.7.1...v0.7.2
[0.7.1]: https://github.com/hawkesj12/repolens/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/hawkesj12/repolens/compare/v0.6.1...v0.7.0
[0.6.1]: https://github.com/hawkesj12/repolens/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/hawkesj12/repolens/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/hawkesj12/repolens/compare/v0.4.3...v0.5.0
[0.4.3]: https://github.com/hawkesj12/repolens/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/hawkesj12/repolens/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/hawkesj12/repolens/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/hawkesj12/repolens/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/hawkesj12/repolens/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/hawkesj12/repolens/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/hawkesj12/repolens/releases/tag/v0.1.0
