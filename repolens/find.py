"""repolens.find — ranked "where does X live?" over the index.

Hybrid retrieval: BM25 on an FTS5 index (path/title weighted so filename hits rank
up) fused with dense semantic KNN via Reciprocal Rank Fusion (RRF, k=60) — BM25
carries exact-term/identifier precision, embeddings carry paraphrase/meaning recall,
and RRF fuses the two ranked lists with zero calibration (no score normalization).
When the [semantic] extra isn't installed (or is disabled / --lexical), it degrades
to lexical-only and says so once on stderr. A LIKE fallback ranked by term-match
count covers a sqlite build that lacks FTS5, announced on stderr too.
"""

from __future__ import annotations

import pathlib
import re
import sqlite3
import sys

from . import index as _index
from . import semantic

# bm25 column weights: title >> relpath > frontmatter > body. relpath weighted
# (not 0) so a file whose PATH carries the terms ranks up — the "where does X live" job.
_WEIGHTS = "bm25(docs, 5.0, 10.0, 4.0, 1.0)"

# RRF constant. 60 is the field-standard default (BM25+dense+RRF(60)); it damps the
# top-rank dominance so a doc ranked well by EITHER retriever surfaces. No weights.
_RRF_K = 60

_lexical_note_shown = False
_dense_failed_note_shown = False

__all__ = ["ensure_fresh", "search"]


# ═══════════════════════════════════════════════════════════════
# ensure_fresh()
# ═══════════════════════════════════════════════════════════════
# Keep the index current on the read path. Missing → full build;
# REPOLENS_FORCE → full rebuild; otherwise an INCREMENTAL pass (re-index
# only changed files). CI/--rebuild do the full backstop.
# ═══════════════════════════════════════════════════════════════
def ensure_fresh(root: pathlib.Path, config: dict, refresh: bool = True) -> str:
    import os

    idx = config["index_path"]
    if os.environ.get("REPOLENS_FORCE"):
        _index.build(root, config)
        return "rebuilt (forced)"
    if not idx.exists():
        _index.build(root, config)
        return "built (was missing)"
    if refresh:
        # Incremental: re-index only changed files (it stat-gates internally, so
        # this is cheap when nothing changed). A full rebuild is --rebuild / forced.
        changed, deleted, _ = _index.build_incremental(root, config)
        if changed or deleted:
            return f"incremental ({changed} changed, {deleted} removed)"
    return ""


def _is_fts5(con: sqlite3.Connection) -> bool:
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='docs'"
    ).fetchone()
    return bool(row and row[0] and "fts5" in row[0].lower())


def _like_search(con: sqlite3.Connection, query: str, k: int) -> list:
    terms = [t.lower() for t in re.split(r"\s+", query.strip()) if t]
    if not terms:
        return []
    clause = " OR ".join(
        ["(lower(relpath) LIKE ? OR lower(title) LIKE ? OR lower(body) LIKE ?)"]
        * len(terms)
    )
    params: list = []
    for t in terms:
        like = f"%{t}%"
        params += [like, like, like]
    rows = con.execute(
        "SELECT relpath, title, kind, lower(relpath || ' ' || title || ' ' || COALESCE(body,'')) AS blob "
        f"FROM docs WHERE {clause}",
        params,
    ).fetchall()
    scored = [
        (rp, ti, ki, sum(1 for t in terms if t in blob)) for rp, ti, ki, blob in rows
    ]
    scored.sort(key=lambda r: -r[3])
    return scored[:k]


# ═══════════════════════════════════════════════════════════════
# _lexical_rows()
# ═══════════════════════════════════════════════════════════════
# The BM25 (or LIKE-fallback) per-doc ranked list: (relpath, title,
# kind, score), best first. Preserves the FTS5 syntax-error retry and
# the zero-hit AND->OR broaden, both announced on stderr. This is the
# lexical half of the hybrid AND the whole result in lexical-only mode.
# ═══════════════════════════════════════════════════════════════
def _lexical_rows(con: sqlite3.Connection, query: str, k: int) -> list:
    if not _is_fts5(con):
        print(
            "⚠ FTS5 unavailable — degraded LIKE search (ranked by term-match count)",
            file=sys.stderr,
        )
        return _like_search(con, query, k)
    try:
        rows = con.execute(
            f"SELECT relpath, title, kind, {_WEIGHTS} AS score "
            "FROM docs WHERE docs MATCH ? ORDER BY score LIMIT ?",
            (query, k),
        ).fetchall()
    except sqlite3.OperationalError as e:
        rows = (
            con.execute(
                f"SELECT relpath, title, kind, {_WEIGHTS} AS score "
                "FROM docs WHERE docs MATCH ? ORDER BY score LIMIT ?",
                ('"' + query.replace('"', "") + '"', k),
            ).fetchall()
            if "syntax error" in str(e).lower()
            else []
        )
    # FTS5 whitespace = implicit AND, so a multi-word query needs EVERY term in one
    # doc. On a zero-hit AND, broaden to any-term (OR) once rather than silently
    # return nothing — announced on stderr.
    terms = [t for t in re.split(r"\s+", query.strip()) if t]
    if not rows and len(terms) > 1:
        try:
            rows = con.execute(
                f"SELECT relpath, title, kind, {_WEIGHTS} AS score "
                "FROM docs WHERE docs MATCH ? ORDER BY score LIMIT ?",
                (" OR ".join(terms), k),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        if rows:
            print(
                "⚠ no exact (all-term) match — broadened to any-term",
                file=sys.stderr,
            )
    return rows


def _rrf_scores(*ranked_lists: list[str]) -> dict[str, float]:
    scores: dict[str, float] = {}
    for lst in ranked_lists:
        for rank, rp in enumerate(lst, 1):
            scores[rp] = scores.get(rp, 0.0) + 1.0 / (_RRF_K + rank)
    return scores


def _note_lexical_only() -> None:
    global _lexical_note_shown
    if _lexical_note_shown:
        return
    _lexical_note_shown = True
    print(
        "ℹ semantic tier enabled but not installed — lexical-only find "
        "(pip install 'repolens[semantic]' for hybrid search)",
        file=sys.stderr,
    )


def _note_dense_failed(err: Exception) -> None:
    # The tier IS installed but embedding the query failed at search time (e.g. a
    # bring-your-own http endpoint is down). Degrade to lexical for this search and say
    # so once — distinct from _note_lexical_only (which means "extra not installed").
    global _dense_failed_note_shown
    if _dense_failed_note_shown:
        return
    _dense_failed_note_shown = True
    print(
        f"ℹ semantic query-embedding failed ({err}) — lexical-only for this search",
        file=sys.stderr,
    )


# ═══════════════════════════════════════════════════════════════
# search()
# ═══════════════════════════════════════════════════════════════
# Return up to k hits (relpath/title/kind/score), best first. Hybrid
# when the semantic tier is on: BM25 per-doc + dense KNN rolled up to
# parent docs, fused by RRF (k=60). Lexical-only otherwise (the extra
# absent, [semantic].enabled false, or lexical_only=True) — announced
# once on stderr when semantic was wanted but isn't installed.
# ═══════════════════════════════════════════════════════════════
def search(
    config: dict, query: str, k: int = 8, lexical_only: bool = False
) -> list[dict]:
    idx = config["index_path"]
    sem_cfg = config.get("semantic", {})
    want_dense = (not lexical_only) and bool(sem_cfg.get("enabled"))
    use_dense = want_dense and semantic.available(config)

    con = sqlite3.connect(f"file:{idx}?mode=ro", uri=True)
    try:
        if not use_dense:
            if want_dense and not semantic.available(config):
                _note_lexical_only()
            rows = _lexical_rows(con, query, k)
            return [
                {"relpath": r, "title": t, "kind": k_, "score": round(s, 3)}
                for r, t, k_, s in rows
            ]

        # Hybrid: over-fetch each side (chunks collapse to fewer parent docs), then
        # fuse. Both lists are already per-doc, so RRF is well-defined.
        pool = max(k * 4, 30)
        lex_rows = _lexical_rows(con, query, pool)
        try:
            dense = semantic.knn(con, query, pool, config)  # [(relpath, distance)]
        except Exception as e:  # noqa: BLE001 — query-time embed failure => degrade
            # available() passed but the actual embed failed (e.g. a down http
            # endpoint) — fall back to the same lexical result the pre-flight
            # branch produces, re-fetched with k (lex_rows above used the pool).
            _note_dense_failed(e)
            rows = _lexical_rows(con, query, k)
            return [
                {"relpath": r, "title": t, "kind": k_, "score": round(s, 3)}
                for r, t, k_, s in rows
            ]

        meta = {r[0]: (r[1], r[2]) for r in lex_rows}  # relpath -> (title, kind)
        scores = _rrf_scores([r[0] for r in lex_rows], [rp for rp, _d in dense])
        ordered = sorted(scores, key=lambda rp: (-scores[rp], rp))[:k]

        missing = [rp for rp in ordered if rp not in meta]
        if missing:
            for rp, ti, ki in con.execute(
                f"SELECT relpath, title, kind FROM docs WHERE relpath IN ({','.join('?' * len(missing))})",
                missing,
            ):
                meta[rp] = (ti, ki)
    finally:
        con.close()

    hits = []
    for rp in ordered:
        ti, ki = meta.get(rp, ("", ""))
        hits.append(
            {"relpath": rp, "title": ti, "kind": ki, "score": round(scores[rp], 4)}
        )
    return hits
