# Roadmap

repolens is intentionally small. These are the changes that would earn their place.

## v0.2 — incremental indexing (the big one)

Today `repolens index` does a **full rebuild** — it re-reads every file. That's simple and can't drift, and it's fast for small/medium repos (sub-second to ~1–2s at a few thousand files). But it scales O(files-per-change), so on a large repo (tens of thousands of files) editing one file triggers a multi-second rebuild.

**v0.2 makes indexing incremental** — re-index only what changed:

- store each file's **content hash** in the index (mtime is unreliable across machines/clones, so hashing is the correctness signal);
- on rebuild, stat-walk to find changed files, re-read + upsert only those, and **reconcile deletes** (drop rows for files that vanished);
- keep the full rebuild as `repolens index --rebuild` — the always-correct ground truth, and what CI runs.

Result: a two-file edit re-indexes two files in milliseconds, regardless of repo size, with full-rebuild as the backstop.

## Later / maybe

- **Semantic search** — optional embeddings for meaning-based recall (currently lexical/BM25 only).
- **More DB integrations** — beyond SQLite (Postgres schema, etc.), all opt-in.
- **Richer purpose extraction** — a few more per-language rules.
- **A watch mode** — rebuild on file-change events instead of on-demand.

## Non-goals

- Not a `ripgrep` replacement (use `rg` for exhaustive literal/regex search).
- Not a semantic code index like Cursor / Aider's repo-map.
- Not a RAG system or a knowledge-management app.
