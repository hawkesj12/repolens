# repolens

**Ranked hybrid search + a typed hygiene linter for a repo's whole corpus.** One local index over your **docs, code purpose-lines, and database schema**, so you can ask _"where does X live?"_ in plain words and get the right few files back — ranked and described — instead of sifting a wall of `grep` hits.

Built for repos where an agent (e.g. [Claude Code](https://claude.com/claude-code)) works alongside a growing pile of markdown and greps on demand rather than keeping a semantic index. **The core is stdlib-only** — no dependencies, no services, no keys. Semantic search is one opt-in extra (`pip install 'repolens[semantic]'` → `fastembed` + `sqlite-vec`, both CPU, no service); without it, `find` is lexical-only (BM25) and everything else is unchanged.

## How it works

**Install once, index per repo, then just search.** Three steps — and the last one stays current on its own forever.

1. **Install once, globally.** `pipx install repolens` puts the `repolens` CLI on your PATH. One time, ever. (Semantic search is an opt-in extra: `pipx install 'repolens[semantic]'`.)

2. **`repolens init` once per repo.** Run it in each repo you want indexed. It creates two things:
   - **`.repometa.toml`** — the config: what to index, ranking + semantic settings. Auto-discovers any SQLite DBs and wires their schema in.
   - **`.repometa/index.db`** — the search index itself, a **disposable, gitignored cache** that self-refreshes and can't drift. Delete it and it rebuilds; it's never committed.

3. **`repolens find "…"` — self-refreshing.** Every search re-indexes any changed files first, then searches, so results are never stale. You never manually rebuild.

That's the whole model: **`pipx install` once → `repolens init` per repo → `repolens find`.**

## What it does

**`repolens find "…"` — where does X live?**
Builds a local SQLite index and answers ranked queries with a one-line description per hit. Ranking is **hybrid**: BM25 (FTS5) for exact-term/identifier precision, fused with dense semantic similarity for paraphrase/meaning recall, combined by Reciprocal Rank Fusion (RRF). With the `[semantic]` extra absent it's BM25-only (and says so once); `--lexical` forces BM25-only on demand.

- **Markdown** — full text.
- **Code / config** — indexed by each file's _purpose line_ (from its docstring or leading comment), so `repolens find "garmin ingest"` returns `scripts/ingest_garmin.py — "Pulls Garmin biometrics into the DB"`, not a wall of matches. The full module docstring is indexed too (BM25 + embedded), so a conceptual query finds the file even when your words aren't in its one-line purpose.
- **Database tables** _(optional)_ — table + column names, so "where do trades live" resolves to a DB table.
- **Frontmatter — any keys, schema-free.** Every YAML frontmatter key is indexed into a sparse `frontmatter(relpath, key, value)` table, so docs with _different_ conventions (`paths:`, `name/description:`, `sector:`) coexist in one repo — repolens imposes no schema and clobbers none. A total, dependency-free parser degrades nested/exotic YAML to searchable text.
- **Respects `.gitignore` by default (in a git repo)** — so secrets, `.env`, and anything you ignore stay out of the index. In a **non-git** directory a `.gitignore` can't be honored — repolens indexes everything there and **prints a warning** so you're never silently exposed. Opt into `include_gitignored` when you _want_ ignored notes searchable (a personal-knowledge-repo mode).
- The index is a **disposable, gitignored cache**: it updates **incrementally** (only changed files, by content hash) and can't drift. `repolens index --rebuild` is the full backstop; delete it and it regenerates.

**`repolens lint` — keep the knowledge base honest.**
Zero-LLM structural checks (dead links, empty files, malformed frontmatter, duplicate titles) **and** per-type field checks you declare in config (e.g. a doc in `meetings/` must carry a `**Date:**`). A bundled **pre-commit hook** runs it and blocks a commit on errors — hygiene enforced, not hoped for.

**`repolens bench` — prove hybrid earns its dependency.**
Scores hybrid vs lexical `find` on a committed gold set (`benchmarks/acceptance.jsonl`) and prints recall@k + MRR per query class, so the "semantic helps" claim is something you _run_, not something you trust. See below.

## Does it help? (measured — run `repolens bench` yourself)

Hybrid search earns its dependency only if it beats plain BM25 on _your_ corpus — so repolens ships a committed gold set (`benchmarks/acceptance.jsonl`: 24 query→gold-doc pairs across exact-term, conceptual, and paraphrase classes) and a scorer, `repolens bench`, that runs every query in BOTH modes against the same index and prints recall@k + MRR per class. On this repo's own corpus (bge-base, k=8): **overall recall@8 was 100% hybrid vs 46% lexical-only (MRR 0.640 vs 0.402)**. The conceptual and paraphrase classes are where the embeddings earn their keep (conceptual recall@8: 100% vs 38%), and the exact-term control class did not regress (hybrid MRR 1.000 vs 0.750). **Honestly, though:** that's one small corpus and 24 queries — a real, reproducible signal, not a statistically significant study — and hybrid is not universally better: on 3 of the 24 queries it ranked the gold doc one position below lexical. Rankers trade individual queries; the aggregate is what improved. Neither extra installed? You still get ranked BM25.

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
repolens init                 # writes .repometa.toml + .gitignore + a warm index + the pre-commit lint hook,
                              # and auto-discovers your DBs. --no-db opts out of discovery.
repolens index                # rebuild/update the index (incremental; a disposable cache)
repolens find "where's the deploy config"
repolens bench                # score hybrid vs lexical on the committed gold set (recall@k + MRR)
repolens lint                 # corpus hygiene + typed-record checks
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

## Use with an agent

repolens is a plain CLI, so any agent that can run a shell command can use it — nothing to wire up. The pattern that works well with [Claude Code](https://claude.com/claude-code) is a one-line routing rule in the repo (e.g. a `.claude/rules/` file or `AGENTS.md`):

> **concept / "where is X" → `repolens find "<idea>"`; exact known string / every occurrence → `rg`.**

Because every `find` re-indexes changed files first, the agent's answers are never stale, and because the index respects `.gitignore`, no file contents or secrets leave the machine — `find` returns repo-relative _paths_, which the agent then reads with its own tools.

## How the index stays correct

The index (`.repometa/index.db`, gitignored) is a **cache derived from your files** — never the source of truth. It updates **incrementally**: a `files(relpath,size,mtime,hash)` table stat-gates each file and re-indexes only those whose content hash changed (so a `touch` or a fresh clone re-hashes but doesn't re-index), reconciling deletes, all in one WAL transaction. WAL is what lets many agent sessions read (and the odd one refresh) the same index concurrently without locking each other out. With the `[semantic]` extra installed, the same content-hash drives embeddings — a changed file is re-chunked (**section-bounded**: chunks split on Markdown headings and never cross one, ~512 tokens each) and re-embedded, its vectors stored via `sqlite-vec` (or the numpy fallback); `find` fuses the BM25 and dense results with RRF, rolling per-chunk hits up to their parent document. A deleted file's chunks/vectors cascade away. `repolens index --rebuild` is the always-correct full backstop (and what CI runs); the index can't go stale, and anything uncertain just rebuilds.

## Roadmap

See [ROADMAP.md](ROADMAP.md). Next: a cross-encoder reranker over the fused top-N, then `llms.txt` export.

## License

MIT © 2026 Justin Hawkes.
