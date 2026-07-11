"""repolens.index — build the search index (a disposable SQLite cache).

One `docs` table: one row per markdown file (full text), per code/config file
(purpose line only — low noise, low leak), and, IF a `[integrations.sqlite]` DB
is configured, per table in it. FTS5 is preferred; falls back to a plain table
(find.py searches it with LIKE) when the sqlite build lacks FTS5. Built to a temp
file then os.replace()'d in atomically. Full rebuild (v0.1); incremental is v0.2.

All paths/skip-lists/extensions come from config — nothing repo-specific here.
"""

from __future__ import annotations

import os
import pathlib
import sqlite3
import time

from . import purpose


def _first_heading(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return ""


def _frontmatter_blob(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[3:end].replace("\n", " ")
    return ""


def _walk(root: pathlib.Path, config: dict, code: bool):
    skip_dirs, skip_files = config["skip_dirs"], config["skip_files"]
    exts = config["code_exts"]
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in skip_dirs]
        for fn in fns:
            is_code = os.path.splitext(fn)[1].lower() in exts
            if (code and is_code) or (not code and fn.endswith(".md")):
                p = pathlib.Path(dp) / fn
                if str(p.relative_to(root)) not in skip_files:
                    yield p


# ═══════════════════════════════════════════════════════════════
# has_fts5()
# ═══════════════════════════════════════════════════════════════
# Whether this sqlite build supports FTS5 (a compile-time option).
# Detect rather than assume; build() falls back to a plain table.
# ═══════════════════════════════════════════════════════════════
def has_fts5() -> bool:
    try:
        c = sqlite3.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE _t USING fts5(x)")
        c.close()
        return True
    except sqlite3.OperationalError:
        return False


# ═══════════════════════════════════════════════════════════════
# corpus_newer_than()
# ═══════════════════════════════════════════════════════════════
# Stat-only staleness check: True if any indexed file (md, code, or
# the optional sqlite DB) is newer than mtime. A LOCAL fast-path — CI
# and `--rebuild` always rebuild regardless.
# ═══════════════════════════════════════════════════════════════
def corpus_newer_than(root: pathlib.Path, config: dict, mtime: float) -> bool:
    sq = config["sqlite_path"]
    if sq and sq.exists() and sq.stat().st_mtime > mtime:
        return True
    for code in (False, True):
        for p in _walk(root, config, code):
            try:
                if p.stat().st_mtime > mtime:
                    return True
            except OSError:
                continue
    return False


# ═══════════════════════════════════════════════════════════════
# build()
# ═══════════════════════════════════════════════════════════════
# (Re)build the index from scratch into a temp DB, then atomically
# os.replace() it over config["index_path"]. Returns (n_docs, n_code,
# n_tables, elapsed_ms).
# ═══════════════════════════════════════════════════════════════
def build(
    root: pathlib.Path, config: dict, db_path: pathlib.Path | None = None
) -> tuple[int, int, int, float]:
    t0 = time.time()
    db_path = db_path or config["index_path"]
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_name(f"{db_path.name}.tmp-{os.getpid()}")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    if has_fts5():
        con.execute(
            "CREATE VIRTUAL TABLE docs USING fts5(relpath, title, frontmatter, body, kind UNINDEXED)"
        )
    else:
        con.execute("CREATE TABLE docs (relpath, title, frontmatter, body, kind)")

    n = 0
    for p in _walk(root, config, code=False):
        try:
            text = p.read_text(errors="ignore")
        except Exception:
            continue
        rel = str(p.relative_to(root))
        con.execute(
            "INSERT INTO docs VALUES (?,?,?,?,?)",
            (rel, _first_heading(text), _frontmatter_blob(text), text, "md"),
        )
        n += 1

    code = 0
    for p in _walk(root, config, code=True):
        try:
            text = p.read_text(errors="ignore")
        except Exception:
            continue
        rel = str(p.relative_to(root))
        pl = purpose.extract_purpose(rel, text)
        con.execute(
            "INSERT INTO docs VALUES (?,?,?,?,?)", (rel, pl or p.name, "", pl, "code")
        )
        code += 1

    tables = 0
    sq = config["sqlite_path"]
    if sq and sq.exists():  # OPTIONAL integration — off unless configured
        try:
            ext = sqlite3.connect(f"file:{sq}?mode=ro", uri=True)
            for (name,) in ext.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            ):
                cols = ", ".join(
                    c[1] for c in ext.execute(f"PRAGMA table_info('{name}')")
                )
                con.execute(
                    "INSERT INTO docs VALUES (?,?,?,?,?)",
                    (
                        f"{sq.name} :: {name}",
                        name,
                        "",
                        f"{sq.name} table {name} columns: {cols}",
                        "db-table",
                    ),
                )
                tables += 1
            ext.close()
        except sqlite3.Error:
            pass

    con.commit()
    con.close()
    os.replace(tmp, db_path)
    return n, code, tables, (time.time() - t0) * 1000
