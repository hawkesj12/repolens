"""repolens.semantic — the opt-in hybrid-search tier (embeddings + vector KNN).

Everything here lazy-imports its heavy deps (fastembed, sqlite-vec, numpy) so the
stdlib-only core never pays for them: importing `repolens.semantic` is free, and
`available(config)` is the gate every caller checks before touching a vector. With the
`[semantic]` extra absent, the whole subsystem is inert and `find` stays lexical.

Two embedder providers (config `[semantic].provider`): **fastembed** (default — local
ONNX, `threads`-throttleable) and **http** (bring-your-own OpenAI-compatible
`/v1/embeddings` endpoint — local Ollama/LM Studio or a metered API — via stdlib
urllib, key from an env var). Both feed the same vector storage + KNN below.

Two storage backends, chosen by probe at first use:
  • **vec0** (fast path) — `sqlite-vec`'s virtual table; ms-scale KNN. Needs a
    python `sqlite3` built with loadable-extension support.
  • **blob** (fallback) — float32 vectors in a plain table + numpy brute-force
    cosine. Still milliseconds at repo scale, and works on any sqlite3 build.
Vectors are L2-normalized on store, so vec0's default L2 ordering and the blob
path's cosine ordering rank identically — and only the RANK matters downstream
(RRF fusion in find.py ignores raw distances).

Granularity: FTS5 ranks whole docs but vectors are per-chunk, so `knn()` rolls each
doc up to its single best (min-distance) chunk before returning — the parent-
document retrieval pattern that makes the two sides fusable.
"""

from __future__ import annotations

import os
import sqlite3
import sys

__all__ = [
    "available",
    "active_path",
    "ensure_schema",
    "embed_doc",
    "delete_doc",
    "knn",
]

_MODELS: dict = {}  # model_name -> loaded fastembed TextEmbedding (expensive; cached)
_VEC_BACKEND: str | None = (
    None  # cached 'vec0' | 'blob' (STORAGE probe; config-independent)
)
_ANNOUNCED = False
_CACHE_LOGGED = False
_MODEL_LOAD_FAILED = False  # set once if fastembed's model can't load → degrade to lexical (this process)
# A recent model-load failure is also persisted to a sentinel FILE in the cache dir, so the
# degrade survives ACROSS processes (each `repolens find` is its own process) — otherwise a
# black-holed network re-hangs every single call. Self-heals after the TTL.
_MODEL_FAIL_TTL = 900  # seconds a load-failure sentinel suppresses retries (15 min)
_HF_READ_TIMEOUT = (
    15  # HuggingFace download read-timeout — a stalled connection fails fast
)
_MODEL_LOAD_TIMEOUT = (
    120  # wall-clock ceiling on a load (env REPOLENS_MODEL_LOAD_TIMEOUT)
)


class EmbeddingError(RuntimeError):
    """An embedding call failed (endpoint down, bad response, model load). Callers on
    the read/build path catch this and degrade instead of crashing `find`."""


def _sem(config: dict | None) -> dict:
    return (config or {}).get("semantic", {}) if config else {}


# ═══════════════════════════════════════════════════════════════
# available()
# ═══════════════════════════════════════════════════════════════
# Can we embed at all? numpy is always needed (vector storage / KNN).
# Then it's provider-specific: the "http" provider needs an endpoint
# configured (the remote model does the embedding); the default
# "fastembed" provider needs fastembed importable. config is optional
# so legacy no-arg callers still get the fastembed check.
# ═══════════════════════════════════════════════════════════════
def available(config: dict | None = None) -> bool:
    # Answer "is the tier installed?" WITHOUT importing the heavy stack. `find` calls
    # this on every refresh (even --lexical, even a no-op), so a real `import fastembed`
    # here taxes every search ~0.3s just to return a boolean. find_spec locates the
    # module without executing it; the actual numpy/fastembed imports happen where
    # they're used (_model, _normalize, knn), reached only when something truly embeds.
    import importlib.util

    if _MODEL_LOAD_FAILED or _sentinel_recent():
        return False  # a recent model-load failure (this process or another) → stay lexical
    if importlib.util.find_spec("numpy") is None:
        return False
    sm = _sem(config)
    if sm.get("provider") == "http":
        return bool(sm.get("endpoint"))
    return importlib.util.find_spec("fastembed") is not None


# ═══════════════════════════════════════════════════════════════
# _vec_backend()
# ═══════════════════════════════════════════════════════════════
# The vector STORAGE backend, independent of the embedder: 'vec0'
# (sqlite-vec fast path) or 'blob' (numpy brute-force fallback when the
# sqlite3 build can't load the extension). Probed once, cached.
# ═══════════════════════════════════════════════════════════════
def _vec_backend() -> str:
    global _VEC_BACKEND
    if _VEC_BACKEND is not None:
        return _VEC_BACKEND
    try:
        import sqlite_vec

        c = sqlite3.connect(":memory:")
        c.enable_load_extension(True)
        sqlite_vec.load(c)
        c.execute("CREATE VIRTUAL TABLE _probe USING vec0(embedding float[4])")
        c.close()
        _VEC_BACKEND = "vec0"
    except Exception:  # noqa: BLE001 — no loadable-extension support => blob fallback
        _VEC_BACKEND = "blob"
    return _VEC_BACKEND


# ═══════════════════════════════════════════════════════════════
# active_path()
# ═══════════════════════════════════════════════════════════════
# 'off' when the tier can't run (see available), else the storage
# backend ('vec0' | 'blob'). config selects the provider for the
# availability check.
# ═══════════════════════════════════════════════════════════════
def active_path(config: dict | None = None) -> str:
    return _vec_backend() if available(config) else "off"


def _announce() -> None:
    global _ANNOUNCED
    if _ANNOUNCED:
        return
    _ANNOUNCED = True
    b = _vec_backend()
    if b == "vec0":
        print("ℹ semantic: sqlite-vec (vec0) fast path", file=sys.stderr)
    elif b == "blob":
        print(
            "ℹ semantic: numpy brute-force cosine (sqlite-vec extension not loadable)",
            file=sys.stderr,
        )


def _load_vec(con: sqlite3.Connection) -> None:
    # Load sqlite-vec exactly ONCE per connection: re-initializing the extension on a
    # connection that already has it (ensure_schema, embed_doc, and knn each call this
    # on the same connection) raises "error during initialization". sqlite3.Connection
    # isn't weakref-able and takes no custom attributes, so we detect the loaded state
    # by probing for a vec function instead of tracking connections.
    try:
        con.execute("SELECT vec_version()")
        return  # already loaded on this connection
    except sqlite3.OperationalError:
        pass
    import sqlite_vec

    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)


def _has_chunks(con: sqlite3.Connection) -> bool:
    return _table_exists(con, "chunks")


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    return bool(
        con.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
            (name,),
        ).fetchone()
    )


# ═══════════════════════════════════════════════════════════════
# _cache_dir()
# ═══════════════════════════════════════════════════════════════
# A DURABLE model-cache dir. fastembed defaults to $TMPDIR, which the OS
# purges — so the "one-time" ~200MB model download silently recurs after a
# temp sweep. Resolve a stable, cross-platform location instead: an explicit
# REPOLENS_CACHE_DIR wins outright; else XDG_CACHE_HOME (or %LOCALAPPDATA%
# on Windows, else ~/.cache) + /repolens/fastembed. stdlib only.
# ═══════════════════════════════════════════════════════════════
def _cache_dir() -> str:
    override = os.environ.get("REPOLENS_CACHE_DIR")
    if override:
        return override
    base = os.environ.get("XDG_CACHE_HOME")
    if not base:
        base = (
            os.environ.get("LOCALAPPDATA")
            if os.name == "nt"
            else os.path.expanduser("~/.cache")
        ) or os.path.expanduser("~/.cache")
    return os.path.join(base, "repolens", "fastembed")


# ═══════════════════════════════════════════════════════════════
# _sentinel_path() / _sentinel_recent() / _write_sentinel()
# ═══════════════════════════════════════════════════════════════
# A load-failure marker file next to the model cache. Its MTIME is the
# signal: within _MODEL_FAIL_TTL, every process treats the tier as down and
# stays lexical without re-attempting the (possibly hanging) load; after the
# TTL it's ignored, so a transient network black-hole self-heals.
# ═══════════════════════════════════════════════════════════════
def _sentinel_path() -> str:
    return os.path.join(os.path.dirname(_cache_dir()), ".model-load-failed")


def _sentinel_recent() -> bool:
    import time

    try:
        return (time.time() - os.path.getmtime(_sentinel_path())) < _MODEL_FAIL_TTL
    except OSError:
        return False  # no sentinel → no recent failure


def _write_sentinel() -> None:
    try:
        p = _sentinel_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w"):
            pass  # touch — the mtime carries the timestamp
    except OSError:
        pass  # best-effort; the in-process flag still degrades this run


# ═══════════════════════════════════════════════════════════════
# _load_with_timeout()
# ═══════════════════════════════════════════════════════════════
# Load the model in a DAEMON thread bounded by a wall-clock timeout, so a
# stalled download can neither hang `find` forever nor block process exit.
# The env override lets a genuinely slow first download raise the ceiling.
# ═══════════════════════════════════════════════════════════════
def _load_with_timeout(name: str, threads, cache: str):
    import threading

    from fastembed import TextEmbedding

    timeout = int(
        os.environ.get("REPOLENS_MODEL_LOAD_TIMEOUT", str(_MODEL_LOAD_TIMEOUT))
    )
    box: dict = {}

    def _load() -> None:
        try:
            box["model"] = TextEmbedding(
                model_name=name, threads=threads, cache_dir=cache
            )
        except Exception as e:  # noqa: BLE001 — surfaced to the caller below
            box["error"] = e

    t = threading.Thread(target=_load, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        raise TimeoutError(f"model load exceeded {timeout}s (network stalled?)")
    if "error" in box:
        raise box["error"]
    return box["model"]


# ═══════════════════════════════════════════════════════════════
# _model()
# ═══════════════════════════════════════════════════════════════
# Load (once, cached) the configured fastembed model. `threads` throttles
# ONNX intra-op parallelism — a low value (default 2) keeps a big first
# build from pinning every core; 0 = library default (all cores). If the
# load FAILS (no network + cold cache, corrupt model, unusable onnxruntime)
# we set _MODEL_LOAD_FAILED once and raise EmbeddingError — so every later
# call short-circuits to lexical instead of re-attempting the load per doc
# (which turns an offline build into an effective hang). fastembed's own
# loguru output is quieted so a graceful degrade doesn't look like a crash.
# ═══════════════════════════════════════════════════════════════
def _model(config: dict):
    name = config["semantic"]["model"]
    if name not in _MODELS:
        global _MODEL_LOAD_FAILED, _CACHE_LOGGED
        if _MODEL_LOAD_FAILED or _sentinel_recent():
            raise EmbeddingError(
                "fastembed model load recently failed — staying lexical"
            )
        # A stalled/black-holed HuggingFace connection would otherwise hang the download
        # forever. The read-timeout fails a dead connection fast without aborting a slow-
        # but-flowing one; _load_with_timeout is the wall-clock backstop for any other stall.
        os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", str(_HF_READ_TIMEOUT))
        try:
            try:  # best-effort: silence fastembed's loguru ERROR spam on a degrade
                from loguru import logger

                logger.disable("fastembed")
            except Exception:  # noqa: BLE001 — quieting is optional, never fatal
                pass
            cache = _cache_dir()
            os.makedirs(cache, exist_ok=True)
            if not _CACHE_LOGGED:
                _CACHE_LOGGED = True
                print(f"ℹ semantic: model cache at {cache}", file=sys.stderr)
            threads = int(config["semantic"].get("threads", 0)) or None
            _MODELS[name] = _load_with_timeout(name, threads, cache)
        except Exception as e:  # noqa: BLE001 — memoize + PERSIST the failure, degrade
            _MODEL_LOAD_FAILED = True
            _write_sentinel()  # so sibling processes degrade instead of re-hanging
            print(
                f"ℹ semantic: model failed to load ({e}) — lexical-only "
                f"(retries suppressed ~{_MODEL_FAIL_TTL // 60} min)",
                file=sys.stderr,
            )
            raise EmbeddingError(f"fastembed model load failed: {e}") from e
    return _MODELS[name]


# ═══════════════════════════════════════════════════════════════
# _doc_prefix() / _query_prefix()
# ═══════════════════════════════════════════════════════════════
# nomic-embed-text REQUIRES task prefixes ("search_document: " on a
# passage, "search_query: " on a query) — fastembed does NOT add them,
# and omitting them quietly tanks retrieval quality (the #1 nomic
# mistake). Applied ONLY for nomic models; bge/others want raw text, so
# a prefix there would hurt. The prefix is embedding-only — the chunk
# table always stores the ORIGINAL text.
# ═══════════════════════════════════════════════════════════════
def _is_nomic(config: dict) -> bool:
    return "nomic" in str(config["semantic"]["model"]).lower()


def _doc_prefix(config: dict, texts: list[str]) -> list[str]:
    return [f"search_document: {t}" for t in texts] if _is_nomic(config) else texts


def _query_prefix(config: dict, query: str) -> str:
    return f"search_query: {query}" if _is_nomic(config) else query


# ═══════════════════════════════════════════════════════════════
# _embed_texts()
# ═══════════════════════════════════════════════════════════════
# Return L2-normalized float32 vectors for a batch of texts, via the
# configured provider: "fastembed" (local ONNX, default) or "http" (a
# bring-your-own OpenAI-compatible endpoint). Normalizing on BOTH store
# and query makes vec0's L2 order == cosine order.
# ═══════════════════════════════════════════════════════════════
def _embed_texts(config: dict, texts: list[str]):
    if config["semantic"].get("provider") == "http":
        return _embed_http(config, texts)
    return _embed_fastembed(config, texts)


def _normalize(vectors):
    import numpy as np

    out = []
    for v in vectors:
        v = np.asarray(v, dtype="float32")
        n = float(np.linalg.norm(v))
        out.append(v / n if n > 0 else v)
    return out


def _embed_fastembed(config: dict, texts: list[str]):
    return _normalize(_model(config).embed(list(texts)))


# ═══════════════════════════════════════════════════════════════
# _embed_http()
# ═══════════════════════════════════════════════════════════════
# The bring-your-own-embedder path: POST to an OpenAI-compatible
# /v1/embeddings endpoint (local Ollama / LM Studio / llama.cpp, or a
# metered API). stdlib urllib only — no new dependency. The API key, when
# needed, is read from the env var named by api_key_env (never stored in
# config). Results are re-ordered by the response `index` to be safe.
# ═══════════════════════════════════════════════════════════════
def _embed_http(config: dict, texts: list[str]):
    import json
    import os
    import urllib.error
    import urllib.request

    sm = config["semantic"]
    texts = list(texts)
    payload = json.dumps({"model": sm["model"], "input": texts}).encode("utf-8")
    req = urllib.request.Request(
        sm["endpoint"], data=payload, headers={"Content-Type": "application/json"}
    )
    key_env = sm.get("api_key_env") or ""
    if key_env and os.environ.get(key_env):
        req.add_header("Authorization", f"Bearer {os.environ[key_env]}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        # endpoint down / slow / non-2xx / unparseable body — surface a clear error the
        # read+build path catches and degrades on, instead of a raw traceback.
        raise EmbeddingError(f"embedding endpoint {sm['endpoint']} failed: {e}") from e
    items = sorted(body.get("data") or [], key=lambda it: it.get("index", 0))
    try:
        vecs = [it["embedding"] for it in items]
    except (TypeError, KeyError) as e:
        raise EmbeddingError(f"malformed embeddings response: {e}") from e
    if len(vecs) != len(texts):
        raise EmbeddingError(
            f"embeddings count {len(vecs)} != inputs {len(texts)} from {sm['endpoint']}"
        )
    return _normalize(vecs)


# ═══════════════════════════════════════════════════════════════
# ensure_schema()
# ═══════════════════════════════════════════════════════════════
# Create the chunk table + the vector store for the active backend
# (idempotent). `chunks.rowid` is the join key shared by both the
# vec0 table and the blob-fallback table. No-op when the tier is off.
# ═══════════════════════════════════════════════════════════════
def ensure_schema(con: sqlite3.Connection, config: dict) -> None:
    if active_path(config) == "off":
        return
    con.execute(
        "CREATE TABLE IF NOT EXISTS chunks "
        "(rowid INTEGER PRIMARY KEY, relpath TEXT, chunk_ix INTEGER, text TEXT)"
    )
    con.execute("CREATE INDEX IF NOT EXISTS chunks_rel ON chunks(relpath)")
    dims = int(config["semantic"]["dims"])
    if active_path(config) == "vec0":
        _load_vec(con)
        con.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(embedding float[{dims}])"
        )
    else:
        con.execute(
            "CREATE TABLE IF NOT EXISTS vectors (rowid INTEGER PRIMARY KEY, embedding BLOB)"
        )


# ═══════════════════════════════════════════════════════════════
# embed_doc()
# ═══════════════════════════════════════════════════════════════
# Chunk a doc, embed each chunk, and store chunk rows + their vectors
# (rowid-linked). Caller is responsible for having deleted any prior
# chunks for this relpath (see delete_doc) — this only inserts.
# No-op when the tier is off/disabled or the doc is empty.
# ═══════════════════════════════════════════════════════════════
def embed_doc(con: sqlite3.Connection, relpath: str, text: str, config: dict) -> None:
    if active_path(config) == "off" or not config["semantic"]["enabled"]:
        return
    from . import chunk as _chunk

    chunks = _chunk.chunk_document(
        text, config["semantic"]["chunk_tokens"], config["semantic"]["overlap"]
    )
    if not chunks:
        return
    _announce()
    import time as _time

    from . import log as _log

    t0 = _time.time()
    vecs = _embed_texts(config, _doc_prefix(config, [c for _ix, c in chunks]))
    vec0 = active_path(config) == "vec0"
    if vec0:
        _load_vec(con)
    for (ix, ctext), v in zip(chunks, vecs):
        cur = con.execute(
            "INSERT INTO chunks(relpath, chunk_ix, text) VALUES (?,?,?)",
            (relpath, ix, ctext),
        )
        blob = v.astype("<f4").tobytes()
        if vec0:
            con.execute(
                "INSERT INTO vec_chunks(rowid, embedding) VALUES (?,?)",
                (cur.lastrowid, blob),
            )
        else:
            con.execute(
                "INSERT INTO vectors(rowid, embedding) VALUES (?,?)",
                (cur.lastrowid, blob),
            )
    _log.event(
        config,
        "embed",
        relpath=relpath,
        chunks=len(chunks),
        model=config["semantic"]["model"],
        ms=round((_time.time() - t0) * 1000),
    )


# ═══════════════════════════════════════════════════════════════
# delete_doc()
# ═══════════════════════════════════════════════════════════════
# Cascade-delete a doc's whole chunk-set (chunks + vectors). The unit
# of update is the doc, not the chunk — a doc's chunk count changes when
# it's edited, so re-embedding is always delete-then-insert. No-op when
# the tier is off or the chunk table doesn't exist yet.
# ═══════════════════════════════════════════════════════════════
def delete_doc(con: sqlite3.Connection, relpath: str) -> None:
    # Config-independent: branch on whichever vector table actually exists, so this
    # works from callers that don't hold config and regardless of the active provider.
    if not _has_chunks(con):
        return
    ids = [
        r[0]
        for r in con.execute("SELECT rowid FROM chunks WHERE relpath=?", (relpath,))
    ]
    if not ids:
        return
    con.execute("DELETE FROM chunks WHERE relpath=?", (relpath,))
    if _table_exists(con, "vec_chunks"):
        _load_vec(con)
        con.executemany("DELETE FROM vec_chunks WHERE rowid=?", [(i,) for i in ids])
    elif _table_exists(con, "vectors"):
        con.executemany("DELETE FROM vectors WHERE rowid=?", [(i,) for i in ids])


# ═══════════════════════════════════════════════════════════════
# knn()
# ═══════════════════════════════════════════════════════════════
# Dense retrieval for one query, returned as a per-DOC ranked list
# [(relpath, distance, chunk_text), ...] — each doc scored by its single
# best (min-distance) chunk (parent-document rollup), and carrying THAT
# chunk's text so the caller can show the passage that actually matched.
# We over-fetch chunk hits (many chunks share a doc) then roll up + trim
# to k. Empty when the tier is off/disabled or nothing is indexed yet.
# ═══════════════════════════════════════════════════════════════
def knn(
    con: sqlite3.Connection, query: str, k: int, config: dict
) -> list[tuple[str, float, str]]:
    if (
        active_path(config) == "off"
        or not config["semantic"]["enabled"]
        or not _has_chunks(con)
    ):
        return []
    _announce()
    batch = _embed_texts(config, [_query_prefix(config, query)])
    if not batch:  # a failed/empty query embed → no dense hits (not an IndexError)
        return []
    qv = batch[0]
    over = max(k * 8, 40)  # over-fetch: chunks collapse to fewer parent docs
    rows: list[tuple[str, float, str]] = []  # (relpath, distance, chunk_text)
    if active_path(config) == "vec0":
        _load_vec(con)
        hits = con.execute(
            "SELECT rowid, distance FROM vec_chunks WHERE embedding MATCH ? "
            "ORDER BY distance LIMIT ?",
            (qv.astype("<f4").tobytes(), over),
        ).fetchall()
        if hits:
            ids = [h[0] for h in hits]
            chunkmap = {
                r[0]: (r[1], r[2])  # rowid -> (relpath, text)
                for r in con.execute(
                    f"SELECT rowid, relpath, text FROM chunks WHERE rowid IN ({','.join('?' * len(ids))})",
                    ids,
                )
            }
            rows = [
                (chunkmap[r][0], d, chunkmap[r][1]) for r, d in hits if r in chunkmap
            ]
    else:
        import numpy as np

        data = con.execute(
            "SELECT c.relpath, c.text, x.embedding FROM vectors x JOIN chunks c ON c.rowid=x.rowid"
        ).fetchall()
        if data:
            mat = np.frombuffer(b"".join(e for _r, _t, e in data), dtype="<f4").reshape(
                len(data), -1
            )
            dists = 1.0 - (mat @ qv)  # normalized vectors => cosine distance
            order = np.argsort(dists)[:over]
            rows = [(data[i][0], float(dists[i]), data[i][1]) for i in order]

    # parent-document rollup: each doc keeps its single best (min-distance) chunk —
    # and that chunk's TEXT, so the caller can show the passage that matched.
    best: dict[str, tuple[float, str]] = {}
    for relpath, dist, text in rows:
        if relpath not in best or dist < best[relpath][0]:
            best[relpath] = (dist, text)
    return [
        (rp, d, txt)
        for rp, (d, txt) in sorted(best.items(), key=lambda kv: kv[1][0])[:k]
    ]
