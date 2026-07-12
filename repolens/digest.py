"""repolens.digest — a compact, hook-ready map of the current repo.

`repolens digest` emits a tiny ORIENTATION digest (name, what's indexed, the most
active top-level dirs, the DB tables, and a routing pointer) read from the existing
index — never a dump. Built for a SessionStart hook so an agent opens every session
with a fresh, never-drifting map. The `--max-lines` budget is the context-rot guard:
more context degrades an agent, so this stays orientation, and detail is a pull
(`repolens find`). Stdlib-only.
"""

from __future__ import annotations

import pathlib
import sqlite3

from . import find as _find

__all__ = ["build_digest"]

# The routing rule, carried in the digest so the agent learns it every session.
_POINTER = "→ concept / where-is-X: repolens find  ·  known string / regex: rg"


# ═══════════════════════════════════════════════════════════════
# _name_and_purpose()
# ═══════════════════════════════════════════════════════════════
# Repo name (first H1) + one-line purpose (first non-heading, non-blank
# line) from README.md or AGENTS.md. Falls back to the directory name.
# ═══════════════════════════════════════════════════════════════
def _name_and_purpose(root: pathlib.Path) -> tuple[str, str]:
    for fn in ("README.md", "AGENTS.md", "readme.md"):
        p = root / fn
        if not p.is_file():
            continue
        name, purpose = "", ""
        for line in p.read_text(errors="ignore").splitlines():
            s = line.strip()
            if not s:
                continue
            if s.startswith("#") and not name:
                name = s.lstrip("#").strip()
            elif not s.startswith("#") and not purpose:
                purpose = s.lstrip("*_> ").strip()
            if name and purpose:
                break
        if name or purpose:
            return name or root.name, purpose
    return root.name, ""


# ═══════════════════════════════════════════════════════════════
# build_digest()
# ═══════════════════════════════════════════════════════════════
# Assemble the digest from the existing index (auto-refreshed if stale,
# via find.ensure_fresh — no new index). Returns a string capped to
# max_lines; the routing pointer is always the last line.
# ═══════════════════════════════════════════════════════════════
def build_digest(root: pathlib.Path, config: dict, max_lines: int = 12) -> str:
    _find.ensure_fresh(root, config)
    name, purpose = _name_and_purpose(root)

    con = sqlite3.connect(f"file:{config['index_path']}?mode=ro", uri=True)
    try:
        counts = dict(con.execute("SELECT kind, count(*) FROM docs GROUP BY kind"))
        tables = [
            r[0]
            for r in con.execute(
                "SELECT title FROM docs WHERE kind='db-table' ORDER BY relpath"
            )
        ]
        # databases = distinct '<db>.name' prefixes in the 'db :: table' relpath key
        dbs = {
            r[0].split(" :: ")[0]
            for r in con.execute("SELECT relpath FROM docs WHERE kind='db-table'")
        }
        # top-level dirs by indexed md+code file count (deterministic: count, then name)
        dir_counts: dict[str, int] = {}
        for (rel,) in con.execute(
            "SELECT relpath FROM docs WHERE kind IN ('md','code')"
        ):
            parts = rel.split("/", 1)
            if len(parts) == 2:  # only real subdirs (skip root-level files)
                dir_counts[parts[0]] = dir_counts.get(parts[0], 0) + 1
        top_dirs = sorted(dir_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:6]
    finally:
        con.close()

    header = f"[repolens] {name}" + (f" — {purpose}" if purpose else "")
    idx_line = (
        f"indexed: {counts.get('md', 0)} docs · {counts.get('code', 0)} code"
        f" · {counts.get('db-table', 0)} db-tables · {len(dbs)} databases"
    )

    lines = [header, idx_line]
    for d, n in top_dirs:
        lines.append(f"  {d}/ ({n})")
    if tables:
        shown = ", ".join(tables[:12]) + (" …" if len(tables) > 12 else "")
        lines.append(f"db tables: {shown}")

    # Budget: keep header + idx-line + as many dirs/tables as fit, always the pointer last.
    if len(lines) + 1 > max_lines:
        lines = lines[: max(2, max_lines - 1)]
    lines.append(_POINTER)
    return "\n".join(lines)
