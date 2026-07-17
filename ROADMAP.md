# Roadmap

repolens is intentionally small — **ranked hybrid search + a typed hygiene linter, one thing done well.** This file tracks **direction**: what's still ahead and what's deliberately out of scope. For what's already shipped, see [CHANGELOG.md](CHANGELOG.md).

## Next

- **Cross-encoder reranker** over the fused top-N (highest-ROI retrieval upgrade;
  `fastembed` ships rerank models). Then **contextual retrieval** (a one-sentence
  per-chunk blurb before indexing) — deferred because it needs a per-chunk LLM call.

## Later / maybe

- **v1.0** — the stable, positioned release: hybrid find + lint + bench, all solid, published on PyPI.
- **`find --format llms-txt`** — emit an `llms.txt` index of the corpus (the emerging convention).
- **More DB integrations** — beyond SQLite (Postgres schema, etc.), all opt-in.
- **Richer purpose extraction** — a few more per-language rules.
- **A stdlib `.gitignore` fallback parser** — enforce ignore rules even outside a git repo (today repolens warns; this would honor them). Deferred as non-trivial (negation, globs, nesting).

## Non-goals

- Not a `ripgrep` replacement (use `rg` for exhaustive literal/regex search).
- Not a whole-codebase semantic index for autocomplete like Cursor / Aider's repo-map — repolens's semantic tier is doc-level findability ("where does X live"), not code-completion context.
- Not a full RAG system or a knowledge-management app.
- **No agent-orientation / repo-map generation.** repolens finds files on demand (`repolens find`), always current — it does not generate or maintain a stored "what lives here" map (that drifts, and the retrieval layer already answers the question per query). Removed in the [Unreleased] strip; the old machinery lives on the `archive/map-machinery` branch.
- **No `--watch` / file-event daemon.** `index` acts on current state — every `find` re-indexes changed files first, so nothing goes stale without a daemon. Cheap detection, no daemon — deliberate.
