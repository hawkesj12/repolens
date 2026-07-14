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
import re
import sqlite3

from . import find as _find

__all__ = ["build_digest"]

# The routing rule, carried in the digest so the agent learns it every session.
_POINTER = "→ concept / where-is-X: repolens find  ·  known string / regex: rg"


def _clean_purpose(s: str) -> str:
    """Strip markdown emphasis and clip to the first sentence (~120 chars) so
    a long tagline doesn't blow the header line."""
    s = re.sub(r"[*_`]", "", s.lstrip(">#*_ ")).strip()
    first = re.split(r"(?<=[.!?])\s", s, maxsplit=1)
    if first and len(first[0]) >= 20:
        s = first[0]
    return s[:117].rsplit(" ", 1)[0] + "…" if len(s) > 120 else s


# ═══════════════════════════════════════════════════════════════
# _name_and_purpose()
# ═══════════════════════════════════════════════════════════════
# Repo name (first H1) + one-line purpose (first non-heading, non-blank
# line, cleaned) from README.md or AGENTS.md. Falls back to the dir name.
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
                purpose = _clean_purpose(s)
            if name and purpose:
                break
        if name or purpose:
            return name or root.name, purpose
    return root.name, ""


# ═══════════════════════════════════════════════════════════════
# _folder_purpose()
# ═══════════════════════════════════════════════════════════════
# A one-line purpose for a folder: the `description` frontmatter of its
# README, else of its shortest-path doc, else that doc's H1 title, else "".
# ═══════════════════════════════════════════════════════════════
def _folder_purpose(
    folder: str, docs: list[str], descs: dict[str, str], titles: dict[str, str]
) -> str:
    in_folder = sorted(d for d in docs if d.startswith(folder + "/"))
    if not in_folder:
        return ""
    readme = next((d for d in in_folder if d.lower().endswith("readme.md")), None)
    for cand in ([readme] if readme else []) + in_folder:
        if cand and descs.get(cand):
            return descs[cand]
    return titles.get(in_folder[0], "")


# ═══════════════════════════════════════════════════════════════
# _group_tables()
# ═══════════════════════════════════════════════════════════════
# Group a DB's table/view names for a compact, complete listing: a
# `prefix_*` group for each shared `x_` prefix (>=2 tables), a `views`
# group, and a `core` group for the unprefixed rest. Grouping IS the
# compression — every table shown, in ~a handful of lines.
# ═══════════════════════════════════════════════════════════════
def _group_tables(rows: list[tuple[str, str]]) -> list[tuple[str, list[str]]]:
    # rows: (table_name, is_view). Prefix = text before the first '_'.
    prefix_counts: dict[str, int] = {}
    for name, _v in rows:
        if "_" in name:
            prefix_counts[name.split("_", 1)[0]] = (
                prefix_counts.get(name.split("_", 1)[0], 0) + 1
            )
    shared = {p for p, c in prefix_counts.items() if c >= 2}
    groups: dict[str, list[str]] = {}
    for name, is_view in sorted(rows):
        if is_view:
            key = "views"
        elif "_" in name and name.split("_", 1)[0] in shared:
            key = name.split("_", 1)[0] + "_*"
        else:
            key = "core"
        groups.setdefault(key, []).append(name)
    # order: prefix groups (alpha), then core, then views
    ordered = sorted(k for k in groups if k.endswith("_*"))
    for tail in ("core", "views"):
        if tail in groups:
            ordered.append(tail)
    return [(k, groups[k]) for k in ordered]


# ═══════════════════════════════════════════════════════════════
# build_digest()
# ═══════════════════════════════════════════════════════════════
# A rich, tiered orientation map read from the existing index (auto-
# refreshed via find.ensure_fresh — no new index): header, root folders
# each with a one-line purpose, and every DB's tables grouped by prefix.
# `full` adds per-folder docs with their descriptions. Richness via
# SELECTION + GROUPING + notes, never a body dump; capped at max_lines,
# the routing pointer always last. Degrades gracefully (no db / no
# frontmatter / no readme). Stdlib-only.
# ═══════════════════════════════════════════════════════════════
def build_digest(
    root: pathlib.Path, config: dict, max_lines: int = 40, full: bool = False
) -> str:
    _find.ensure_fresh(root, config)
    name, purpose = _name_and_purpose(root)

    con = sqlite3.connect(f"file:{config['index_path']}?mode=ro", uri=True)
    try:
        counts = dict(con.execute("SELECT kind, count(*) FROM docs GROUP BY kind"))
        md_paths = [
            r[0] for r in con.execute("SELECT relpath FROM docs WHERE kind='md'")
        ]
        titles = {
            r[0]: r[1]
            for r in con.execute("SELECT relpath, title FROM docs WHERE kind='md'")
        }
        descs: dict[str, str] = {}
        # honor a renamed description key so digest tracks enrich's schema
        desc_key = (
            config.get("enrich", {}).get("keys", {}).get("description", "description")
        )
        try:
            for rel, val in con.execute(
                "SELECT relpath, value FROM frontmatter WHERE key=?", (desc_key,)
            ):
                descs[rel] = val
        except sqlite3.OperationalError:
            pass  # index predates the frontmatter EAV
        # db-tables per database: {db.name: [(table, is_view)]}
        dbs: dict[str, list[tuple[str, str]]] = {}
        for rel, title, body in con.execute(
            "SELECT relpath, title, body FROM docs WHERE kind='db-table'"
        ):
            db = rel.split(" :: ")[0]
            is_view = f" view {title} " in f" {body} "
            dbs.setdefault(db, []).append((title, is_view))
        # top-level dirs by indexed md+code count (deterministic: count, then name)
        dir_counts: dict[str, int] = {}
        for (rel,) in con.execute(
            "SELECT relpath FROM docs WHERE kind IN ('md','code')"
        ):
            parts = rel.split("/", 1)
            if len(parts) == 2:
                dir_counts[parts[0]] = dir_counts.get(parts[0], 0) + 1
    finally:
        con.close()

    lines = [f"[repolens] {name}" + (f" — {purpose}" if purpose else "")]
    idx = f"indexed: {counts.get('md', 0)} docs · {counts.get('code', 0)} code"
    if dbs:
        idx += f" · {len(dbs)} database" + ("s" if len(dbs) != 1 else "")
    lines.append(idx)

    # folders, most-populated first, each with a one-line purpose
    top_dirs = sorted(dir_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    if top_dirs:
        lines.append("folders:")
        width = max(len(d) for d, _ in top_dirs)
        for d, cnt in top_dirs:
            purp = _folder_purpose(d, md_paths, descs, titles)
            tail = f" — {purp}" if purp else ""
            lines.append(f"  {(d + '/').ljust(width + 1)} ({cnt}){tail}")
            if full:
                for doc in sorted(x for x in md_paths if x.startswith(d + "/"))[:6]:
                    note = f": {descs[doc]}" if descs.get(doc) else ""
                    lines.append(f"      {doc}{note}")

    # databases: every table, grouped by prefix (never truncated)
    for db, rows in sorted(dbs.items()):
        lines.append(f"{db}:")
        for key, names in _group_tables(rows):
            lines.append(f"  {key} ({len(names)}): {', '.join(names)}")

    lines.append(_POINTER)

    # Budget guard: drop from the END (keep header+idx+start), always pointer last.
    if len(lines) > max_lines:
        lines = lines[: max_lines - 1] + [_POINTER]
    return "\n".join(lines)
