# repolens

[![CI](https://github.com/hawkesj12/repolens/actions/workflows/ci.yml/badge.svg)](https://github.com/hawkesj12/repolens/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/repolens-search.svg)](https://pypi.org/project/repolens-search/)
[![Python](https://img.shields.io/pypi/pyversions/repolens-search.svg)](https://pypi.org/project/repolens-search/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Ranked hybrid search + a typed hygiene linter for a repo's whole corpus.** One local index over your **docs, code purpose-lines, and database schema**, so you can ask _"where does X live?"_ in plain words and get the right few files back — ranked and described — instead of sifting a wall of `grep` hits.

Built for repos where an agent (e.g. [Claude Code](https://claude.com/claude-code)) works alongside a growing pile of markdown and greps on demand rather than keeping a semantic index. **Hybrid search ships by default** — `pip install repolens-search` gives you ranked BM25 fused with semantic embeddings out of the box (`fastembed` + `sqlite-vec`, both CPU, no service, no keys). It degrades to lexical-only (BM25) if the model can't load, and `--lexical` forces BM25 on demand. The first run downloads a ~200MB model once (cached durably); everything else is offline.

## How it works

**Install once, index per repo, then just search.** Three steps — and the last one stays current on its own forever.

1. **Install once, globally.** `pipx install repolens-search` puts the `repolens` CLI on your PATH, hybrid search included. One time, ever. (The PyPI package is `repolens-search` — the name `repolens` was taken; the command you run is still `repolens`.)

2. **`repolens init` once per repo.** Run it in each repo you want indexed. It creates two things:
   - **`.repolens.toml`** — the config: what to index, ranking + semantic settings. Auto-discovers any SQLite DBs and wires their schema in.
   - **`.repolens/index.db`** — the search index itself, a **disposable, gitignored cache** that self-refreshes and can't drift. Delete it and it rebuilds; it's never committed.

3. **`repolens find "…"` — self-refreshing.** Every search re-indexes any changed files first, then searches, so results are never stale. You never manually rebuild.

That's the whole model: **`pipx install` once → `repolens init` per repo → `repolens find`.**

> **Want the internals?** [`docs/how-it-works.md`](docs/how-it-works.md) is a detailed prose walk-through — the three-kinds-of-content corpus model, section-bounded chunking, the BM25 + semantic + RRF fusion, content-hash incremental indexing, and the design principle behind each choice.

## What it does

**`repolens find "…"` — where does X live?**
Builds a local SQLite index and answers ranked queries with a one-line description **and the passage that matched** per hit — so you get the relevant text, not just a file path. Ranking is **hybrid**: BM25 (FTS5) for exact-term/identifier precision, fused with dense semantic similarity for paraphrase/meaning recall, combined by Reciprocal Rank Fusion (RRF). If the embedding model can't load it degrades to BM25-only (and says so once); `--lexical` forces BM25-only on demand.

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

## Does it help? (don't trust me — run `repolens bench`)

Rather than claim it helps, repolens ships a benchmark you run. `repolens bench` scores every query **three ways against the same corpus** — a literal **grep** baseline (ranked by distinct query terms matched, a fair reading of grep output), **lexical** `find` (BM25), and **hybrid** `find` (BM25 + semantic) — and reports recall@k, MRR, **and a bootstrap 95% CI on the deltas**. The gold set is small and self-authored (`benchmarks/acceptance.jsonl`: 18 query→gold-doc pairs on this repo's own files), so read the numbers as a **directional signal on one corpus, not a general claim.**

On this repo (bge-base, k=8):

| arm                     | recall@8 | MRR  |
| ----------------------- | -------- | ---- |
| grep (distinct-term) \* | 94%      | 0.52 |
| lexical (BM25)          | 50%      | 0.34 |
| hybrid                  | 100%     | 0.67 |

Two honest takeaways, with the uncertainty attached:

- **Hybrid clearly beats BM25** — ΔMRR **+0.33, 95% CI [+0.17, +0.50]** (excludes zero). Lexical is what you fall back to _without_ the model, so this is the result that justifies shipping semantic on by default.
- **Against a fair grep, hybrid wins on recall (100% vs 94%) but its MRR edge is within the noise** — ΔMRR **+0.16, 95% CI [−0.01, +0.33]** (crosses zero at n=18). Grep is a stronger literal baseline than most tools admit; hybrid's real advantage is recall and the meaning-based queries grep can't reach, not a ranking blowout.

> **\* The benchmark is charitable to grep — and that _under_-credits repolens.** Real `grep`/`rg` **doesn't rank**: it returns matches in file order, unranked. The grep arm here is given a relevance ranking (by distinct query terms) that grep doesn't natively have, so the MRR comparison already flatters it. In actual use the gap is wider than the number shows: grep hands back an **unranked pile of matches that you — or your agent — must read and judge** (a real round-trip in time and tokens the benchmark charges grep nothing for), whereas repolens returns the files **already ranked, each with the matching passage**. That "ranked + passage vs sift-it-yourself" difference is a genuine repolens advantage the retrieval MRR doesn't measure — kept separate here on purpose, but real.

Run `repolens bench` on your own repo — that's the number that matters, not this one.

## Which embedding model — and does the provider matter?

The hybrid tier works with any embedder, so a second benchmark asks _which one_. On a **123-page prose corpus** with a **30-query gold set phrased without each target page's own vocabulary** — so only _meaning_ finds them, the case where the embedding model actually earns its keep — six setups were scored:

| model                                    | provider        | dims | overall MRR | conceptual | exact | build |
| ---------------------------------------- | --------------- | ---- | ----------- | ---------- | ----- | ----- |
| **mxbai-embed-large** ⭐                 | Ollama (GPU)    | 1024 | 0.621       | **0.540**  | 0.875 | 319s  |
| snowflake-arctic-embed2                  | Ollama (GPU)    | 1024 | 0.614       | 0.540      | 1.000 | 373s  |
| bge-m3                                   | Ollama (GPU)    | 1024 | 0.604       | 0.515      | 1.000 | 368s  |
| bge-base-en-v1.5 _(used optimally)_      | fastembed (CPU) | 768  | 0.637       | 0.531      | 1.000 | —     |
| bge-base-en-v1.5 _(zero-config default)_ | fastembed (CPU) | 768  | 0.610       | 0.528      | 0.875 | 1257s |
| nomic-embed-text                         | Ollama (GPU)    | 768  | 0.568       | 0.510      | 0.750 | 134s  |

`conceptual` = the 20 meaning-only queries (the real signal); `exact` = 4 literal-term controls (n=4, noisy); overall n=30. **Every model got its correct query prefix** (mxbai/bge `Represent this sentence…`, nomic `search_query:`, arctic `query:`, bge-m3 none) — a prefix-blind harness measures "which model tolerates being used _wrong_," not which is best (see `[semantic].query_prefix` under [Configure](#configure-repolenstoml)).

**Two takeaways:**

1. **On quality, the top five tie.** They cluster at **0.60–0.64 overall / 0.53–0.54 conceptual** — inside one query's worth of noise at n=30; only nomic clearly trails. The embedding model barely moves quality here, and the **zero-config fastembed default ties the field.**
2. **The real gap is speed, not quality.** fastembed runs on **CPU** — the full build took **~21 minutes** vs Ollama's **2–6 on the GPU** — and that's before fastembed's per-`find` cold reload (~1–2s) vs Ollama's resident **~275ms**.

So the guidance is about the _provider_, not a quality gap:

- **Zero-config → `fastembed` + `bge-base-en-v1.5`** (the default). Competitive quality, nothing to install; CPU-slow to build, reloads per query. Fine for a small or one-shot repo.
- **Query-heavy / agent use → Ollama + `mxbai-embed-large`.** Top conceptual score (tied), English-specialized (no wasted multilingual capacity), resident on the GPU. Set `provider = "http"` + the model's `query_prefix`.

The gold set + `repolens bench --set <gold>` reproduce every row.

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
pipx install repolens-search   # PyPI package name; the command it installs is `repolens`
# or: uv tool install repolens-search
```

Requires Python 3.11+. repolens depends on `fastembed` (ONNX embeddings, no PyTorch/CUDA/service), `sqlite-vec`, and `numpy` — all CPU-only, no service. (`fastembed` pulls `onnxruntime`, a prebuilt binary; on the rare platform where that won't install, repolens still runs lexical-only.)

> **First run has a one-time cost.** The first index build **embeds your whole corpus** — fastembed downloads the model (`BAAI/bge-base-en-v1.5`, ~0.2 GB) once (cached under `~/.cache/repolens`, override with `REPOLENS_CACHE_DIR`), then embeds every doc's chunks on CPU. Budget **roughly a few seconds per document** (a few hundred large markdown files can take several minutes). It's throttled by default (`[semantic].threads = 2`) so it won't max your machine — raise it (`repolens index --threads 0` for all cores, or set `[semantic].threads`) for a faster one-off rebuild. After that it's incremental — only _changed_ files re-embed, so day-to-day use is instant. Prefer no model? Set `[semantic].enabled = false` for lexical-only (BM25). On a build without loadable-extension support (`sqlite3` is compiled without it on some platforms — notably stock macOS), repolens automatically falls back from `sqlite-vec` to a numpy brute-force vector search; it announces which path is active.

> **Heavy use? Point it at Ollama.** With the default local fastembed, each `repolens find` runs as its own process and **reloads the model every time** — that model load is most of a hybrid query's latency. For an agent firing many queries, keep the model **resident**: set `[semantic].provider = "http"` and `endpoint` to a local [Ollama](https://ollama.com) (or any OpenAI-compatible `/v1/embeddings`) — it holds the model in memory across calls, so each `find` is just the embed + search. (Ollama's own core cap is server-side via `OLLAMA_NUM_THREAD`, not `[semantic].threads`.) fastembed stays the zero-config default; this is the upgrade for query-heavy workloads.

## Quick start

```sh
cd your-repo
repolens init                 # writes .repolens.toml + .gitignore + a warm index + the pre-commit lint hook,
                              # and auto-discovers your DBs. --no-db opts out of discovery.
repolens index                # rebuild/update the index (incremental; a disposable cache)
repolens find "where's the deploy config"
repolens bench                # score hybrid vs lexical on the committed gold set (recall@k + MRR)
repolens lint                 # corpus hygiene + typed-record checks
```

## Configure (`.repolens.toml`)

`repolens init` drops a commented starter. Declare your typed records by folder:

```toml
[types.meeting]
folder = "meetings"
recursive = true               # classify subfolders too
exclude = ["*draft*"]          # artifacts, not records
require = ["^\\*\\*Date:\\*\\*"]  # regex a conforming doc must contain (a warn if missing)

# Semantic (hybrid) search — ON by default. Set enabled = false for lexical-only.
# [semantic]
# model = "BAAI/bge-base-en-v1.5"   # short-passage retriever; fits the ~512 section-bounded chunks
# enabled = true
# chunk_tokens = 512                # per-chunk target; a chunk never crosses a heading
# threads = 2                       # throttle fastembed's CPU (0 = all cores)
# provider = "http"                 # OR bring your own: OpenAI-compatible /v1/embeddings
# endpoint = "http://localhost:11434/v1/embeddings"   # e.g. local Ollama (GPU) or a metered API
# api_key_env = "OPENAI_API_KEY"    # env var holding the key (never stored here)
# query_prefix / doc_prefix         # asymmetric instruction some models require on
#                                   # queries vs docs. nomic auto-applies its own; set
#                                   # these for others (wrong/missing prefix tanks quality):
#   mxbai-embed-large, bge-*-en-v1.5 → query_prefix = "Represent this sentence for searching relevant passages: "
#   snowflake-arctic-embed2          → query_prefix = "query: "
#   bge-m3 and most others           → none (leave unset)

# SQLite integration — index table/column NAMES (schema only, read-only).
# `repolens init` AUTO-DISCOVERS the DBs in your repo (including gitignored
# ones, skipping *.bak* backups) and fills this in. One or many:
# [integrations.sqlite]
# paths = ["data/app.db", "data/other.db"]   # legacy `path = "..."` also works
```

An explicit `type:` in a doc's YAML frontmatter overrides the folder rule.

**SQLite auto-discovery.** `repolens init` scans for `*.db` / `*.sqlite` / `*.sqlite3` files — **including gitignored ones** (real DBs usually are), skipping backups and its own index cache — and writes the ones it finds into `[integrations.sqlite]` for you, so `repolens find "where do trades live"` resolves to a DB table with no hand-config. It reads **only table and column names** (via `sqlite_master` + `PRAGMA table_info`, opened read-only) — never row data. Pass `repolens init --no-db` to skip it, or edit the `paths` list by hand.

## Use with an agent

repolens is a plain CLI, so any agent that can run a shell command can use it — nothing to wire up. Drop a short routing rule into the repo (a `.claude/rules/` file for [Claude Code](https://claude.com/claude-code), or `AGENTS.md`) so the agent reaches for it:

> **Default to `repolens find "<what you're after>"` to locate anything. Use `rg` / grep only for an exact string you need _every_ match of, or a regex.**

A ready-to-drop version is in [`docs/agent-rule-template.md`](docs/agent-rule-template.md). Every `find` re-indexes changed files first, so the agent's answers are never stale, and each hit comes back with the passage that matched — the agent gets the text, then reads the file with its own tools if it needs more. Because the index respects `.gitignore`, no ignored file contents leave the machine.

## How the index stays correct

The index (`.repolens/index.db`, gitignored) is a **cache derived from your files** — never the source of truth. It updates **incrementally**: a `files(relpath,size,mtime,hash)` table stat-gates each file and re-indexes only those whose content hash changed (so a `touch` or a fresh clone re-hashes but doesn't re-index), reconciling deletes, all in one WAL transaction. WAL is what lets many agent sessions read (and the odd one refresh) the same index concurrently without locking each other out. The same content-hash drives embeddings (hybrid is on by default) — a changed file is re-chunked (**section-bounded**: chunks split on Markdown headings and never cross one, ~512 tokens each) and re-embedded, its vectors stored via `sqlite-vec` (or the numpy fallback); `find` fuses the BM25 and dense results with RRF, rolling per-chunk hits up to their parent document. A deleted file's chunks/vectors cascade away. `repolens index --rebuild` is the always-correct full backstop (and what CI runs); the index can't go stale, and anything uncertain just rebuilds.

## Private logging (opt-in)

Set `[log].enabled = true` and repolens appends one JSON line per `find` and per embed to `.repolens/events.jsonl` — inside the gitignored cache dir, so the log is **local and private** (never committed, never leaves your machine). Off by default.

A `find` event records the query, mode, the matched **file paths + scores**, and timing; an `embed` event records the file, chunk count, model, and timing. It's **metadata only — the matched passage text is never written to the log** (relevant if you enable `include_gitignored` over private notes: the log captures which files matched and your query, not their contents). Writes never raise, so logging can't break a search. Beyond debugging retrieval, the find log accumulates the **real queries** you and your agents run against the repo — far better data for growing `benchmarks/acceptance.jsonl` than hand-written gold.

## Roadmap

See [ROADMAP.md](ROADMAP.md). Next: a cross-encoder reranker over the fused top-N, then `llms.txt` export.

## License

MIT © 2026 Justin Hawkes.
