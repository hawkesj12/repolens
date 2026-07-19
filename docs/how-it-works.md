# How repolens works

A detailed walk through the machinery, from the problem it solves down to the individual
data structures. The goal is that after reading this you could re-derive the design yourself.

---

## The one-sentence version

repolens turns a whole repository — prose, code, and database schema alike — into a single
searchable corpus, indexes it with **both** keyword and meaning-based retrieval fused
together, and keeps that index correct automatically so an AI agent (or you) can ask "where
does X live?" and get the right few files back, each with the passage that matched. A second,
smaller job rides along: a linter that keeps the corpus itself healthy.

The whole tool is five commands — `init`, `index`, `find`, `bench`, `lint` — over one SQLite
file.

---

## The problem it solves

An agent working in a repo greps. Grep (`ripgrep`, `git grep`) is perfect when you already
know the literal string you're looking for. It's useless when you know the _concept_ but not
the word the author used — "where's the retry logic" won't find a file that calls it
"exponential backoff," and grep can't rank, so it hands back an unordered pile of matches you
still have to read and judge.

repolens fills exactly that gap: **concept-to-location** search that returns a _ranked_ handful
of files with the matching text already extracted. It does not replace grep — it's the other
half. Grep for the known string; `repolens find` for the known idea.

---

## What repolens sees: three kinds of content, one index

The first non-obvious idea is that repolens flattens three very different things into one
uniform "document" so a single query can cross all of them:

1. **Prose documents** (markdown, text) are indexed in **full** — the whole body is searchable.
2. **Code files** do **not** get their bodies indexed. Instead repolens extracts a file's
   _purpose line_ — its module docstring, leading comment block, or top H1 — via a handful of
   tiny per-extension rules (`purpose.py`). A Python file that opens with `"""repolens.find —
ranked 'where does X live?' over the index."""` contributes exactly that sentence, capped at
   ~200 characters, plus up to ~1500 characters of its leading doc block. This is deliberate:
   indexing raw code bodies floods the index with variable names and boilerplate that drown the
   signal, whereas the purpose line is the English summary of what the file is _for_. A missing
   purpose line is never an error — it degrades to the filename.
3. **Database schema** is indexed as **table and column names only** (`discover.py`,
   `index.py`). repolens opens each SQLite database read-only, reads `sqlite_master` and
   `PRAGMA table_info`, and never touches a single row. So `find "where do trades live"` can
   resolve to a `fin_transactions` table, but no actual financial data ever enters the index.
   This "names, never rows" boundary is what makes it safe to point at a private database.

Every one of these becomes a row in the same `docs` table with a `kind` of `doc`, `code`, or a
DB entry. A query ranks across all three at once.

---

## The index is a derived cache, never the truth

The index lives at `.repolens/index.db` — a single SQLite file, gitignored, and treated as a
**disposable cache derived from your files**. This framing is load-bearing: because the index
is never the source of truth, anything uncertain can just be rebuilt from the real files, and a
stale or corrupt index is never a data-loss risk. `repolens index --rebuild` is the
always-correct backstop and is what CI runs.

Inside that one file are a few tables working together:

- **`docs`** — the searchable content. When the SQLite build supports it (most do), this is an
  **FTS5** virtual table; otherwise a plain table with a slower `LIKE` fallback. FTS5 is
  SQLite's built-in full-text search engine — it tokenizes text and can rank matches by
  **BM25**, the standard relevance formula that rewards rare query terms and penalizes very long
  documents. Columns: `relpath`, `title`, `frontmatter`, `body`, `kind`.
- **`files`** — one row per indexed file: `relpath`, `size`, `mtime`, and a content `hash`.
  This table is the engine of incremental updates (below).
- **`frontmatter`** — a flattened key/value table of YAML frontmatter, indexed so metadata
  queries are cheap.
- **`meta`** — small key/value state: a signature of the database schema, a signature of the
  embedding model, and an optimize counter.
- **The vectors** — the semantic half, stored via **sqlite-vec** (a vector-search extension) in
  a `vec0` virtual table when available, or as raw numpy blobs when it isn't.

---

## Chunking: why a whole document is the wrong unit

Before the semantic layer can embed anything, it has to decide _what_ to embed. The naive
answer — one vector per document — is quietly wrong. A long document is _about_ many things, so
collapsing it to a single vector produces a blurred average that matches the wrong sense of a
query. A page that covers both "authentication" and "rate limiting" gets one vector sitting
between the two, and a query about either lands weakly.

So repolens embeds **per chunk**, and the chunking (`chunk.py`) respects structure instead of
cutting blindly:

- It splits on **Markdown heading boundaries first**, and a chunk **never crosses a heading**.
  Each section becomes its own chunk, so its vector represents _that idea_ cleanly. The preamble
  before the first heading is its own chunk.
- Heading detection is **fence-aware**: a `#` line inside a ` ``` ` or `~~~` code fence is
  a code comment, not a section boundary — splitting there would shred a code snippet across
  chunks.
- A section that already fits under the target size (~512 tokens, the window short-passage
  retrieval models are actually built for) stays one clean chunk. A section that's too long is
  packed into ~512-token pieces on **natural boundaries** — paragraph, then line, then sentence,
  then word — with a small overlap so a thought split across a boundary still connects.
- A document with no headings falls back to the same recursive packing, so it's never one giant
  vector.

Token counts are estimated at ~4 characters per token — good enough to size chunks without
dragging in a real tokenizer dependency.

---

## The semantic tier: turning meaning into geometry

"Semantic" search works by turning text into an **embedding** — a list of numbers (a vector)
produced by a small neural model, positioned so that texts with similar _meaning_ land near
each other in space, even when they share no words. "exponential backoff" and "retry logic"
end up close; a keyword search would never connect them.

repolens can produce these vectors two ways (`semantic.py`), and the choice is purely about
performance, not quality:

- **`fastembed` (the default)** runs the model on your **CPU** via onnxruntime, downloading a
  ~200 MB model once. Zero setup, but two costs: CPU inference is slow, and because each
  `repolens find` is its own process, it **reloads the whole model every time** — that reload
  dominates query latency.
- **`http` provider → Ollama (recommended for heavy use)** points at a local Ollama endpoint,
  which runs the model on the **GPU** and keeps it **resident** in memory across calls. The
  first query is fast and every subsequent one skips the load entirely.

Two subtleties the code handles for you:

- **Query/document prefixes.** Many retrieval models require an _asymmetric instruction_ — a
  short prefix on queries that differs from documents — and silently lose accuracy without it
  (nomic wants `search_query:` / `search_document:`; mxbai and bge-v1.5 want `Represent this
sentence for searching relevant passages:`; bge-m3 wants none). repolens applies each model's
  correct prefix via `[semantic].query_prefix` / `doc_prefix`, with nomic auto-handled for
  back-compat. Feeding a model the wrong prefix, or none, is the single most common way to make
  a good model look bad.
- **Normalization.** Both stored vectors and query vectors are L2-normalized, which makes
  sqlite-vec's L2-distance ordering identical to cosine-similarity ordering — so the fast path
  and the correct answer coincide.

---

## Two ways to search, fused: hybrid retrieval

This is the heart of the tool. A single `repolens find` runs **two independent searches** and
combines them (`find.py`):

- **The lexical search (BM25 over FTS5)** — exact-term and identifier precision. Its columns are
  **weighted** so the results serve the "where does X live" job: `title` ×5, `relpath` ×10,
  `frontmatter` ×4, `body` ×1. The path is weighted _highest_ because a file whose _path_
  carries the query terms is very often the file you want.
- **The dense search (semantic KNN)** — meaning and paraphrase recall. It embeds the query,
  finds the **k nearest chunks** by vector distance ("KNN" = k-nearest-neighbors), and rolls
  those per-chunk hits up to their parent documents.

Then it fuses the two ranked lists with **Reciprocal Rank Fusion (RRF)**. RRF is elegantly
simple: a document's fused score is the sum, over each list it appears in, of `1 / (60 + its
rank in that list)`. The constant 60 is the field standard; it damps the influence of the very
top ranks so that a document ranked well by _either_ retriever surfaces, without anyone having
to normalize or calibrate the two very different score scales (BM25 scores and vector distances
aren't comparable — RRF sidesteps that entirely by using only _ranks_).

The mechanics around it: it over-fetches each side (a pool of `max(k×4, 30)`) because chunks
collapse into fewer parent documents, fuses, then returns the top `k`. The result is that exact
identifier matches and pure-meaning matches both make the final list, and neither retriever's
weakness sinks a good hit.

---

## Returning the passage, not just the path

A `find` result isn't a bare filename — each hit carries the **passage that actually matched**,
which is what lets an agent decide whether to open the file at all. The passage is chosen by
provenance: if the document won its place on the **dense** side, repolens shows the _winning
chunk's text_ (the specific section whose meaning matched); if it won on the **lexical** side,
it shows FTS5's `snippet()` — the window around the matched terms. Either way it's trimmed to a
readable ~220-character line. This is why the routing rule can say "the agent gets the text,
then reads the file with its own tools only if it needs more."

---

## Staying correct without re-reading everything: incremental indexing

An index that goes stale is worse than no index. repolens keeps itself current on the **read
path** — every `find` first runs a cheap incremental refresh — without re-reading the whole
repo each time (`index.py`, `build_incremental`).

The trick is a two-stage gate backed by the `files` table:

1. **Stat-gate.** For each file it compares the current `(size, mtime)` against the stored row.
   If they match, the file is assumed unchanged and is never even read. A clean no-op reads
   nothing but the `files` table.
2. **Hash-confirm.** For a file whose size or mtime _did_ change, it computes a **blake2b**
   content hash (16-byte digest) and compares that. Only if the _content_ hash differs does the
   file get re-indexed. This matters because `mtime` is unreliable across clones and machines —
   a fresh `git clone` rewrites every mtime, and a naive tool would re-index the entire repo. The
   content hash is machine-stable, so a clone re-hashes but doesn't re-embed.

Changed files are handled as `DELETE` + re-`INSERT` (which cascades their chunks and vectors),
deletes are reconciled, and it all happens in **one WAL transaction**. WAL (write-ahead logging)
is SQLite's concurrency mode: it lets many agent sessions _read_ the index while one occasionally
_refreshes_ it, without them locking each other out. The writer waits up to 30 seconds for the
lock rather than failing fast, and only takes the write lock (`BEGIN IMMEDIATE`) once it has
found real work — so the common "nothing changed" case never blocks a reader.

Two signatures in the `meta` table guard against subtler staleness:

- **`db_sig`** — a hash of the database schema, so a changed table set triggers re-indexing of
  the schema entries.
- **`embed_sig`** — the embedding model name and dimension. If you switch models (say nomic→mxbai,
  768→1024 dims), the stored vectors are meaningless under the new model; the mismatched signature
  forces a full backfill instead of silently mixing incompatible vectors.

The full `build()` — used by `--rebuild` and CI — writes to a **temp file and `os.replace`s** it
over the live index, an atomic swap so a reader never sees a half-built index. The design maxim
is "the changer pays, readers never wait": the process that edits a file absorbs the small
refresh cost, so every reader gets a fresh answer for free.

---

## Never crashing: graceful degradation

Retrieval is best-effort, and repolens is built to always return _something_ useful rather than
error out. The fallbacks are layered:

- **No semantic extra installed / disabled / `--lexical`** → lexical-only BM25, announced once
  on stderr.
- **The model fails to load** (e.g. offline, a black-holed download) → a persistent, TTL'd
  sentinel file records the failure so it degrades _immediately_ across processes instead of
  re-hanging on every call, and self-heals when the TTL expires.
- **The query embed fails at search time** (e.g. a bring-your-own HTTP endpoint is down) → that
  one search degrades to lexical and says so.
- **No FTS5 in the SQLite build** → a `LIKE` search ranked by distinct-term match count.
- **No sqlite-vec extension** (stock macOS SQLite lacks loadable extensions) → a numpy
  brute-force vector search, which is slower but identical in results.
- **A multi-word query with zero all-terms matches** → it broadens from implicit-AND to any-term
  (OR) once, rather than returning nothing, and tells you.
- **The index is locked by a concurrent writer** → `find` and `index` degrade with a message
  instead of a traceback.

The through-line: a missing capability lowers quality, never breaks the command.

---

## The hygiene half: lint

The second, smaller job is a **corpus linter** (`lint.py`, `schema.py`) — deterministic, zero
AI. It runs structural checks across the whole corpus (dead relative links, empty files, missing
headings, malformed frontmatter, duplicate titles) plus **typed-record validation**: you declare
in config that files under `meetings/` are of type `meeting` and must contain, say, a `**Date:**`
line, and lint flags any that don't. Findings are severity-tagged (`error` / `warn` / `info`),
and `lint --strict` (wired as a pre-commit hook by `init`) blocks a commit on any error — so the
corpus can't rot silently.

The classifier is entirely config-driven: a document's type comes from an explicit frontmatter
`type:` if present, else from its folder, minus `exclude` globs. Nothing about it is
repo-specific.

---

## The five commands

- **`init`** — writes a commented `.repolens.toml`, auto-discovers your SQLite databases and
  fills in `[integrations.sqlite]`, drops the pre-commit lint hook, and warms the first index.
- **`index`** — builds or incrementally refreshes the index; `--rebuild` forces the full atomic
  rebuild, `--threads N` overrides the CPU cap for a faster one-off.
- **`find`** — the hybrid ranked search, returning files + passages; `--k` sets the result count,
  `--json` for machine output.
- **`bench`** — scores a JSONL gold set three ways (grep / lexical / hybrid) with recall@k, MRR,
  and a bootstrap confidence interval, so "does it help?" is a number you run, not a claim.
- **`lint`** — the corpus hygiene report; `--strict` exits non-zero on errors.

---

## The design philosophy (the through-line)

Every decision above rhymes with a few principles:

- **One thing, done well.** Hybrid findability + typed hygiene over a whole repo. Not a RAG
  framework, not a knowledge-management app — the retrieval _layer_ an agent does RAG _with_.
- **The index is a derived cache, never the truth.** It's disposable, gitignored, and always
  rebuildable, so it can never be a liability.
- **Respect the boundary.** `.gitignore` is honored by default (gitignored _contents_ stay out),
  and databases give up names but never rows — so it's safe to point at private repos.
- **Stdlib core, optional power.** The lexical half, chunking, incremental engine, and linter are
  pure standard library; the semantic tier is the one dependency, and everything degrades to
  lexical if it's absent.
- **Best-effort, never brittle.** Every missing capability lowers quality instead of raising an
  exception.
- **The changer pays; readers never wait.** Freshness is maintained incrementally on the edit
  path, so every query is both cheap and current.
