"""repolens.find — ranked "where does X live?" over the index.

BM25 on an FTS5 index (path/title weighted so filename hits rank up); a LIKE
fallback ranked by term-match count when the sqlite build lacks FTS5 — and it
announces the degraded mode on stderr so it never silently loses ranking.
"""

from __future__ import annotations

import pathlib
import re
import sqlite3
import sys

from . import index as _index

# bm25 column weights: title >> relpath > frontmatter > body. relpath weighted
# (not 0) so a file whose PATH carries the terms ranks up — the "where does X live" job.
_WEIGHTS = "bm25(docs, 5.0, 10.0, 4.0, 1.0)"

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
# search()
# ═══════════════════════════════════════════════════════════════
# Return up to k hits (relpath/title/kind/score), best first. BM25 on
# FTS5; ranked LIKE on the plain fallback (announced on stderr).
# ═══════════════════════════════════════════════════════════════
def search(config: dict, query: str, k: int = 8) -> list[dict]:
    idx = config["index_path"]
    con = sqlite3.connect(f"file:{idx}?mode=ro", uri=True)
    try:
        if not _is_fts5(con):
            print(
                "⚠ FTS5 unavailable — degraded LIKE search (ranked by term-match count)",
                file=sys.stderr,
            )
            rows = _like_search(con, query, k)
        else:
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
    finally:
        con.close()
    return [
        {"relpath": r, "title": t, "kind": k_, "score": round(s, 3)}
        for r, t, k_, s in rows
    ]
