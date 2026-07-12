"""repolens.discover — find SQLite databases in a repo to auto-configure.

`repolens init` calls this to wire up the [integrations.sqlite] block without the
user hand-writing a path. It walks the repo (skip_dirs pruned, gitignored files
INCLUDED — real DBs are usually gitignored) and keeps files that are genuinely
SQLite with at least one table, skipping backups and repolens's own index cache.
No row data is ever read — validation only counts tables. Stdlib-only.
"""

from __future__ import annotations

import os
import pathlib
import sqlite3

_DB_EXTS = (".db", ".sqlite", ".sqlite3")

__all__ = ["discover_sqlite_dbs"]


# ═══════════════════════════════════════════════════════════════
# discover_sqlite_dbs()
# ═══════════════════════════════════════════════════════════════
# Walk `root` (pruning config['skip_dirs'], same as the indexer, so
# gitignored files ARE seen) and return the repo-relative path + table
# count of every file that is a real SQLite DB with at least one table.
# Skips *.bak* backups and repolens's own index cache. Read-only; only
# sqlite_master is queried, never row data. Result is sorted by path.
# ═══════════════════════════════════════════════════════════════
def discover_sqlite_dbs(root: pathlib.Path, config: dict) -> list[tuple[str, int]]:
    skip_dirs = config["skip_dirs"]
    index_path = config["index_path"].resolve()
    found: list[tuple[str, int]] = []
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in skip_dirs]
        for fn in fns:
            if not fn.lower().endswith(_DB_EXTS) or ".bak" in fn.lower():
                continue
            p = pathlib.Path(dp) / fn
            if p.resolve() == index_path:  # repolens's own disposable cache
                continue
            n = _table_count(p)
            if n > 0:
                found.append((str(p.relative_to(root)), n))
    found.sort()
    return found


# ═══════════════════════════════════════════════════════════════
# _table_count()
# ═══════════════════════════════════════════════════════════════
# Open read-only and count tables + views. Returns 0 for a non-SQLite
# file, an unreadable file, or an empty DB — anything not worth indexing.
# ═══════════════════════════════════════════════════════════════
def _table_count(db: pathlib.Path) -> int:
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            (n,) = con.execute(
                "SELECT count(*) FROM sqlite_master WHERE type IN ('table','view')"
            ).fetchone()
            return int(n)
        finally:
            con.close()
    except sqlite3.Error:
        return 0
