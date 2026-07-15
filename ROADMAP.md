# Roadmap

repolens is intentionally small — "the agent-context freshness layer." This file tracks **direction**: what's still ahead and what's deliberately out of scope. For what's already shipped, see [CHANGELOG.md](CHANGELOG.md).

## Next

- **Cross-encoder reranker** over the fused top-N (highest-ROI retrieval upgrade;
  `fastembed` ships rerank models). Then **contextual retrieval** (a one-sentence
  per-chunk blurb before indexing) — deferred because it needs a per-chunk LLM call.

## Later / maybe

- **v1.0** — the stable, positioned release: hybrid find + lint + the self-maintaining rule + env + hook + enrich, all solid, published on PyPI.
- **`digest --format llms-txt`** — also emit an `llms.txt` (the emerging generated-map convention) alongside the stdout digest.
- **More DB integrations** — beyond SQLite (Postgres schema, etc.), all opt-in.
- **Richer purpose extraction** — a few more per-language rules.
- **A stdlib `.gitignore` fallback parser** — enforce ignore rules even outside a git repo (v0.8 warns; this would honor them). Deferred as non-trivial (negation, globs, nesting).

## Non-goals

- Not a `ripgrep` replacement (use `rg` for exhaustive literal/regex search).
- Not a whole-codebase semantic index for autocomplete like Cursor / Aider's repo-map — repolens's semantic tier is doc-level findability ("where does X live"), not code-completion context.
- Not a full RAG system or a knowledge-management app.
- **No `--watch` / file-event daemon.** `enrich` and `index` act on current state, so a scheduled/on-demand pass self-heals a missed run where an on-save hook would silently drop it. Cheap detection, no daemon — deliberate.
