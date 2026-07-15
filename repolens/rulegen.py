"""repolens.rulegen — build & refresh the dedicated rule's GENERATED sections.

The dedicated `.claude/rules/repolens.md` is a static header (the five questions,
written once) plus two generated blocks — **Environment** (the real toolchain) and
**Map** (where things live) — that a SessionStart change-detector regenerates only
when they'd actually differ. This module owns those blocks and the detector's
signal:

- **change_key** = `blake2b(folder-set + DB table/column-set + toolchain signature)`.
  Not folders alone: a new table in a DB isn't a folder change but must still refresh
  the Map, and a changed tool version must refresh Environment.
- **refresh** compares the stored key to a freshly computed one; unchanged → a ~no-op,
  changed → regenerate ONLY the delimiter-bounded blocks (the header is never touched)
  and rewrite atomically (temp + os.replace) so concurrent detectors can't corrupt it.

Reuses the existing map machinery — `digest._group_tables` for the DB map and
`env.probe_env` for the toolchain — rather than rebuilding it.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import re
import sqlite3

from . import digest as _digest
from . import env as _env
from . import find as _find
from . import templates

__all__ = ["change_key", "full_rule", "write_atomic", "refresh"]

_KEY_LINE_RE = re.compile(re.escape(templates.CHANGE_KEY_PREFIX) + r"([0-9a-f]+) -->")


# ═══════════════════════════════════════════════════════════════
# _snapshot()
# ═══════════════════════════════════════════════════════════════
# Refresh the index, then read from it everything the change-key AND
# the rendered sections need in one pass: repo name, top-level dir
# counts, the full folder-set (every ancestor dir — catches a new
# nested folder), authored `description` frontmatter, md paths, the DB
# table grouping + a schema signature, and the toolchain line.
# ═══════════════════════════════════════════════════════════════
def _snapshot(root: pathlib.Path, config: dict) -> dict:
    _find.ensure_fresh(root, config)
    name, _purpose = _digest._name_and_purpose(root)
    desc_key = (
        config.get("enrich", {}).get("keys", {}).get("description", "description")
    )

    con = sqlite3.connect(f"file:{config['index_path']}?mode=ro", uri=True)
    try:
        dir_counts: dict[str, int] = {}
        folder_set: set[str] = set()
        for (rel,) in con.execute(
            "SELECT relpath FROM docs WHERE kind IN ('md','code')"
        ):
            parts = rel.split("/")
            for i in range(1, len(parts)):
                folder_set.add("/".join(parts[:i]))
            if len(parts) > 1:
                dir_counts[parts[0]] = dir_counts.get(parts[0], 0) + 1
        descs: dict[str, str] = {}
        try:
            for rel, val in con.execute(
                "SELECT relpath, value FROM frontmatter WHERE key=?", (desc_key,)
            ):
                descs[rel] = val
        except sqlite3.OperationalError:
            pass  # index predates the frontmatter EAV
        md_paths = [
            r[0] for r in con.execute("SELECT relpath FROM docs WHERE kind='md'")
        ]
        dbs: dict[str, list[tuple[str, bool]]] = {}
        db_sig: list[str] = []
        for rel, title, body in con.execute(
            "SELECT relpath, title, body FROM docs WHERE kind='db-table' ORDER BY relpath"
        ):
            db = rel.split(" :: ")[0]
            is_view = f" view {title} " in f" {body} "
            dbs.setdefault(db, []).append((title, is_view))
            db_sig.append(
                f"{rel}|{body}"
            )  # names + columns => a schema change flips the key
    finally:
        con.close()

    return {
        "name": name,
        "dir_counts": dir_counts,
        "folder_set": folder_set,
        "descs": descs,
        "md_paths": md_paths,
        "dbs": dbs,
        "db_sig": db_sig,
        "env_line": _env.probe_env(config),
    }


def _key_from(snap: dict) -> str:
    h = hashlib.blake2b(digest_size=16)
    h.update("\n".join(sorted(snap["folder_set"])).encode("utf-8"))
    h.update(b"\x00")
    h.update("\n".join(sorted(snap["db_sig"])).encode("utf-8"))
    h.update(b"\x00")
    h.update(snap["env_line"].encode("utf-8"))
    return h.hexdigest()


# ═══════════════════════════════════════════════════════════════
# change_key()
# ═══════════════════════════════════════════════════════════════
# The detector's signal: a pure hash of (folder-set + DB schema +
# toolchain). Stable when nothing structural changed; flips on a new
# folder, a new table/column, or a changed tool version.
# ═══════════════════════════════════════════════════════════════
def change_key(root: pathlib.Path, config: dict) -> str:
    return _key_from(_snapshot(root, config))


def _folder_desc(folder: str, md_paths: list[str], descs: dict[str, str]) -> str:
    # An AUTHORED description only — a folder's README `description`, else any doc's;
    # never a title fallback (the map's structure carries orientation, not a guess).
    in_folder = sorted(p for p in md_paths if p.startswith(folder + "/"))
    readme = next((p for p in in_folder if p.lower().endswith("readme.md")), None)
    for cand in ([readme] if readme else []) + in_folder:
        if cand and descs.get(cand):
            return descs[cand]
    return ""


def _render_env(snap: dict) -> str:
    return "\n".join(
        [
            templates.GEN_ENV_START,
            "## Environment (this repo)",
            "",
            "<!-- generated by repolens — do not hand-edit; regenerates on toolchain change -->",
            "",
            snap["env_line"],
            templates.GEN_ENV_END,
        ]
    )


def _render_map(snap: dict) -> str:
    lines = [
        templates.GEN_MAP_START,
        "## Map — where things live",
        "",
        "<!-- generated by repolens — do not hand-edit; regenerates on folder/DB change -->",
        "",
    ]
    top = sorted(snap["dir_counts"].items(), key=lambda kv: (-kv[1], kv[0]))
    if top:
        width = max(len(d) for d, _ in top)
        lines.append("Folders (by indexed file count):")
        for d, cnt in top:
            desc = _folder_desc(d, snap["md_paths"], snap["descs"])
            tail = f" — {desc}" if desc else ""
            lines.append(f"- `{(d + '/').ljust(width + 1)}` ({cnt}){tail}")
    for db, rows in sorted(snap["dbs"].items()):
        lines.append("")
        lines.append(f"`{db}` (schema only):")
        for key, names in _digest._group_tables(rows):
            lines.append(f"- {key} ({len(names)}): {', '.join(names)}")
    lines.append(templates.GEN_MAP_END)
    return "\n".join(lines)


def _key_marker(key: str) -> str:
    return f"{templates.CHANGE_KEY_PREFIX}{key} -->"


# ═══════════════════════════════════════════════════════════════
# full_rule()
# ═══════════════════════════════════════════════════════════════
# The complete dedicated rule for a FIRST install: static header +
# generated Environment + generated Map + the change-key marker.
# Returns (content, key).
# ═══════════════════════════════════════════════════════════════
def full_rule(root: pathlib.Path, config: dict) -> tuple[str, str]:
    return _from_snapshot(_snapshot(root, config))


# ═══════════════════════════════════════════════════════════════
# write_atomic()
# ═══════════════════════════════════════════════════════════════
# Write via a temp file + os.replace so a concurrent session's
# detector can never observe a half-written rule.
# ═══════════════════════════════════════════════════════════════
def write_atomic(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _stored_key(text: str) -> str | None:
    m = _KEY_LINE_RE.search(text)
    return m.group(1) if m else None


def _replace_block(text: str, start: str, end: str, new: str) -> str:
    s = text.find(start)
    e = text.find(end)
    if s != -1 and e != -1 and e > s:
        return text[:s] + new + text[e + len(end) :]
    return text.rstrip() + "\n\n" + new + "\n"  # block absent (older file) → append


# ═══════════════════════════════════════════════════════════════
# refresh()
# ═══════════════════════════════════════════════════════════════
# The early-cutoff detector. Compute the current change-key; if it
# matches the one stored in the file, do nothing (the ~1ms no-op of a
# typical session). Otherwise regenerate ONLY the delimiter-bounded
# Environment + Map blocks (the static header is preserved), update the
# change-key, and write atomically. Returns (changed, key). A missing
# file is treated as changed (regenerated in place with a fresh header
# too, via full_rule) so the detector self-heals.
# ═══════════════════════════════════════════════════════════════
def refresh(path: pathlib.Path, root: pathlib.Path, config: dict) -> tuple[bool, str]:
    snap = _snapshot(root, config)
    key = _key_from(snap)
    if not path.exists():
        write_atomic(path, _from_snapshot(snap)[0])
        return True, key
    text = path.read_text(encoding="utf-8", errors="ignore")
    if _stored_key(text) == key:
        return False, key
    text = _replace_block(
        text, templates.GEN_ENV_START, templates.GEN_ENV_END, _render_env(snap)
    )
    text = _replace_block(
        text, templates.GEN_MAP_START, templates.GEN_MAP_END, _render_map(snap)
    )
    if _KEY_LINE_RE.search(text):
        text = _KEY_LINE_RE.sub(_key_marker(key), text, count=1)
    else:
        text = text.rstrip() + "\n\n" + _key_marker(key) + "\n"
    write_atomic(path, text)
    return True, key


def _from_snapshot(snap: dict) -> tuple[str, str]:
    key = _key_from(snap)
    content = (
        templates.rule_header(snap["name"])
        + "\n"
        + _render_env(snap)
        + "\n\n"
        + _render_map(snap)
        + "\n\n"
        + _key_marker(key)
        + "\n"
    )
    return content, key
