# Roadmap

repolens is intentionally small — "the agent-context freshness layer." These are the changes that earn their place.

## Shipped

- **v0.2 — SQLite auto-discovery.** `init` finds the repo's databases (incl. gitignored) and wires their schema into the index automatically; multi-DB support.
- **v0.3 — the freshness layer.** `repolens digest` (a tiny, budgeted repo map for a SessionStart hook), `repolens env` (OS-aware, manifest-seeded, present-only toolchain probe), `repolens hook` (non-destructive SessionStart installer). Positioned as "detect + inject, don't hand-maintain."
- **v0.4 — `.gitignore` respected by default.** The file corpus skips gitignored paths, so secrets/`.env`/build output stay out of the index; `include_gitignored = true` opts back in for personal-knowledge repos. DB schema discovery is unaffected (names only, opt-in).
- **v0.5 — incremental indexing + schema-agnostic frontmatter + rich digest.** `index` re-indexes only changed files (content-hash, stat-gated, WAL upsert + delete-reconcile; `--rebuild` backstop). A sparse `frontmatter(relpath,key,value)` EAV makes any frontmatter key queryable — no schema imposed (a total, zero-dep flat parser). `digest` gets rich: folders-with-purpose + all DB tables grouped by prefix, tiered (`--full`) and budgeted.

## Later / maybe

- **v0.3.1 — `digest --format llms-txt`** — also emit an `llms.txt` (the emerging generated-map convention) alongside the stdout digest.
- **v1.0** — the stable, positioned release: find + lint + digest + env + hook, all solid.
- **Semantic search** — optional embeddings for meaning-based recall (currently lexical/BM25 only).
- **More DB integrations** — beyond SQLite (Postgres schema, etc.), all opt-in.
- **Richer purpose extraction** — a few more per-language rules.
- **A watch mode** — rebuild on file-change events instead of on-demand.

## Non-goals

- Not a `ripgrep` replacement (use `rg` for exhaustive literal/regex search).
- Not a semantic code index like Cursor / Aider's repo-map.
- Not a RAG system or a knowledge-management app.
