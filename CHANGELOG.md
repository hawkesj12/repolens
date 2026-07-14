# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/hawkesj12/repolens/compare/v0.7.1...HEAD
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
