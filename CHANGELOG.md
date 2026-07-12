# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/hawkesj12/repolens/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/hawkesj12/repolens/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/hawkesj12/repolens/releases/tag/v0.1.0
