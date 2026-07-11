# repolens

**Ranked, described search over everything in a repo — docs, code, and data — plus a typed hygiene linter for your knowledge files.** Built for repos where an AI agent works alongside a growing pile of markdown, and where a plain `grep` leaves you sifting.

Stdlib-only Python. No dependencies, no services, no keys.

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

## Who it's for

A repo that mixes **prose/knowledge with code** and is worked by an **agent that greps on demand rather than maintaining a semantic index** — [Claude Code](https://claude.com/claude-code) being the prime example. There, `repolens` gives a _ranked, described_ answer across docs + code + data including your private notes, plus lightweight enforced hygiene.

## What it's _not_

Not a replacement for `ripgrep` (use `rg` for exhaustive literal/regex code search), not a semantic/embeddings index like Cursor or Aider's repo-map, not a RAG system, and not a knowledge-management app. It's a lexical findability + hygiene layer with one deliberate edge: it sees the _whole_ corpus — prose, code purpose-lines, DB schema, and gitignored content — and keeps it clean.

## Install

```sh
pipx install repolens        # or: uv tool install repolens
```

Requires Python 3.11+.

## Quick start

```sh
cd your-repo
repolens init                 # writes .repometa.toml + .gitignore entry + the pre-commit hook
repolens index                # build the index (~fast; a disposable cache)
repolens find "where's the deploy config"
repolens lint
```

## Configure (`.repometa.toml`)

`repolens init` drops a commented starter. Declare your typed records by folder:

```toml
[types.meeting]
folder = "meetings"
recursive = true               # classify subfolders too
exclude = ["*draft*"]          # artifacts, not records
require = ["^\\*\\*Date:\\*\\*"]  # regex a conforming doc must contain (a warn if missing)

# Optional — also index a SQLite DB's table/column names (off unless set):
# [integrations.sqlite]
# path = "data/app.db"
```

An explicit `type:` in a doc's YAML frontmatter overrides the folder rule.

## How it works

The index (`.repometa/index.db`, gitignored) is a **cache derived from your files** — never the source of truth. A full rebuild is fast for small/medium repos and can't go stale. Staleness is a local fast-path; anything uncertain just rebuilds.

## Roadmap

See [ROADMAP.md](ROADMAP.md) — notably **v0.2 incremental indexing** (only re-index changed files) for large repos.

## License

MIT © 2026 Justin Hawkes.
