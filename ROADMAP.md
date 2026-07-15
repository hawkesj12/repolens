# Roadmap

repolens is intentionally small — "the agent-context freshness layer." This file tracks **direction**: what's still ahead and what's deliberately out of scope. For what's already shipped, see [CHANGELOG.md](CHANGELOG.md).

## Later / maybe

- **v1.0** — the stable, positioned release: find + lint + digest + env + hook + enrich, all solid, published on PyPI.
- **`digest --format llms-txt`** — also emit an `llms.txt` (the emerging generated-map convention) alongside the stdout digest.
- **Semantic search** — optional embeddings for meaning-based recall (currently lexical/BM25 only).
- **More DB integrations** — beyond SQLite (Postgres schema, etc.), all opt-in.
- **Richer purpose extraction** — a few more per-language rules.
- **A stdlib `.gitignore` fallback parser** — enforce ignore rules even outside a git repo (v0.8 warns; this would honor them). Deferred as non-trivial (negation, globs, nesting).

## Non-goals

- Not a `ripgrep` replacement (use `rg` for exhaustive literal/regex search).
- Not a semantic code index like Cursor / Aider's repo-map.
- Not a RAG system or a knowledge-management app.
- **No `--watch` / file-event daemon.** `enrich` and `index` act on current state, so a scheduled/on-demand pass self-heals a missed run where an on-save hook would silently drop it. Cheap detection, no daemon — deliberate.
