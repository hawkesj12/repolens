"""repolens.rulegen — build & refresh the dedicated rule's GENERATED sections.

The dedicated `.claude/rules/repolens.md` is a static header (the five questions,
written once) plus two generated blocks — **Environment** (the real toolchain) and
**Map** (where things live) — each regenerated on ITS OWN change-key so they update
independently:

- **env-key** = blake2b(toolchain signature) → the SessionStart `repolens refresh`
  regenerates Environment when a tool version changes.
- **map-key** = blake2b(folder-set + DB table/column-set) → the SessionEnd `repolens
  map` (run via `repolens tidy`) regenerates the Map when structure changes.

Splitting the key means a tool-version bump never triggers a (possibly costly,
model-written) Map rebuild, and a new folder never rewrites Environment. The Map body
is deterministic by default; with `[map].command` set it is written by a model (see
mapgen) with a hard fallback to the deterministic render on any failure. Writes are
atomic (temp + os.replace) so concurrent detectors can't observe a half-written rule.
An older single-`change-key` rule self-heals: absence of the two new markers is
treated as changed and upgrades the file once.

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
from . import mapgen
from . import templates

__all__ = ["change_key", "full_rule", "write_atomic", "refresh"]

_ENV_KEY_RE = re.compile(re.escape(templates.ENV_KEY_PREFIX) + r"([0-9a-f]+) -->")
_MAP_KEY_RE = re.compile(re.escape(templates.MAP_KEY_PREFIX) + r"([0-9a-f]+) -->")


# ═══════════════════════════════════════════════════════════════
# _snapshot()
# ═══════════════════════════════════════════════════════════════
# Refresh the index, then read from it everything the change-keys AND
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


def _env_key_from(snap: dict) -> str:
    h = hashlib.blake2b(digest_size=16)
    h.update(snap["env_line"].encode("utf-8"))
    return h.hexdigest()


def _map_key_from(snap: dict) -> str:
    h = hashlib.blake2b(digest_size=16)
    h.update("\n".join(sorted(snap["folder_set"])).encode("utf-8"))
    h.update(b"\x00")
    h.update("\n".join(sorted(snap["db_sig"])).encode("utf-8"))
    return h.hexdigest()


# ═══════════════════════════════════════════════════════════════
# change_key()
# ═══════════════════════════════════════════════════════════════
# The combined (env + map) signal — kept for external callers; the
# internal refresh compares the two split keys independently.
# ═══════════════════════════════════════════════════════════════
def change_key(root: pathlib.Path, config: dict) -> str:
    snap = _snapshot(root, config)
    return _env_key_from(snap) + _map_key_from(snap)


def _folder_desc(folder: str, md_paths: list[str], descs: dict[str, str]) -> str:
    # An AUTHORED FOLDER-LEVEL description only — a folder's own README `description`.
    # NEVER borrow a random member doc's description: that mislabels the folder (e.g.
    # `skills/` reading as one skill's purpose). No README description => no line; the
    # tree's structure carries orientation on its own.
    in_folder = sorted(p for p in md_paths if p.startswith(folder + "/"))
    readme = next((p for p in in_folder if p.lower().endswith("readme.md")), None)
    return descs.get(readme, "") if readme else ""


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


# ═══════════════════════════════════════════════════════════════
# _render_map()
# ═══════════════════════════════════════════════════════════════
# The Map block. repolens owns the frame (heading, delimiters, the
# deterministic DB-schema block); the folder bullets are either
# model-written (when root+config are given and [map].command yields a
# body) or the deterministic authored-README render. The DB block is
# never model-written — it needs no enrichment.
# ═══════════════════════════════════════════════════════════════
def _render_map(
    snap: dict, root: pathlib.Path | None = None, config: dict | None = None
) -> str:
    top = sorted(snap["dir_counts"].items(), key=lambda kv: (-kv[1], kv[0]))
    model_bullets = None
    src = "deterministic"
    if root is not None and config is not None:
        model_bullets = mapgen.render_map_folders(snap, root, config)
        if model_bullets:
            src = "model-written"
    lines = [
        templates.GEN_MAP_START,
        "## Map — where things live",
        "",
        f"<!-- generated by repolens ({src}) — do not hand-edit; regenerates on folder/DB change -->",
        "",
    ]
    if top:
        lines.append("Folders (by indexed file count):")
        if model_bullets:
            lines.append(model_bullets)
        else:
            width = max(len(d) for d, _ in top)
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


def _env_marker(key: str) -> str:
    return f"{templates.ENV_KEY_PREFIX}{key} -->"


def _map_marker(key: str) -> str:
    return f"{templates.MAP_KEY_PREFIX}{key} -->"


# ═══════════════════════════════════════════════════════════════
# full_rule()
# ═══════════════════════════════════════════════════════════════
# The complete dedicated rule for a FIRST install: static header +
# generated Environment + generated Map + both change-key markers.
# map_command=True lets the Map use a configured [map].command (a
# deliberate install/manual run); the SessionStart self-heal passes
# False to keep the Map deterministic (cheap) until the next SessionEnd.
# Returns (content, (env_key, map_key)).
# ═══════════════════════════════════════════════════════════════
def full_rule(
    root: pathlib.Path, config: dict, map_command: bool = True
) -> tuple[str, tuple[str, str]]:
    return _from_snapshot(_snapshot(root, config), root, config, map_command)


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


def _stored(text: str, rx: re.Pattern[str]) -> str | None:
    m = rx.search(text)
    return m.group(1) if m else None


def _replace_block(text: str, start: str, end: str, new: str) -> str:
    s = text.find(start)
    e = text.find(end)
    if s != -1 and e != -1 and e > s:
        return text[:s] + new + text[e + len(end) :]
    return text.rstrip() + "\n\n" + new + "\n"  # block absent (older file) → append


def _set_marker(text: str, rx: re.Pattern[str], prefix: str, key: str) -> str:
    marker = f"{prefix}{key} -->"
    if rx.search(text):
        return rx.sub(lambda _m: marker, text, count=1)
    return text.rstrip() + "\n\n" + marker + "\n"


# ═══════════════════════════════════════════════════════════════
# refresh()
# ═══════════════════════════════════════════════════════════════
# The early-cutoff detector, block-scoped. Compute the two keys; for
# each requested block whose stored key differs (or force_map), regen
# ONLY that delimiter-bounded block and update its marker, then write
# atomically. do_env/do_map select which blocks this caller owns (the
# SessionStart refresh does Env, + Map only when no [map].command; the
# SessionEnd map does Map). A missing or pre-split (legacy single-key)
# file self-heals via a full regen. Returns (changed, (env_key, map_key)).
# ═══════════════════════════════════════════════════════════════
def refresh(
    path: pathlib.Path,
    root: pathlib.Path,
    config: dict,
    do_env: bool = True,
    do_map: bool = True,
    force_map: bool = False,
) -> tuple[bool, tuple[str, str]]:
    snap = _snapshot(root, config)
    env_key = _env_key_from(snap)
    map_key = _map_key_from(snap)
    text = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
    if not text or (not _ENV_KEY_RE.search(text) and not _MAP_KEY_RE.search(text)):
        # Missing, or a pre-split legacy rule → full regen once. Use the command for
        # the Map only when this caller owns the Map (SessionEnd); the SessionStart
        # self-heal keeps it deterministic (cheap) until the next SessionEnd.
        write_atomic(path, _from_snapshot(snap, root, config, map_command=do_map)[0])
        return True, (env_key, map_key)
    changed = False
    if do_env and _stored(text, _ENV_KEY_RE) != env_key:
        text = _replace_block(
            text, templates.GEN_ENV_START, templates.GEN_ENV_END, _render_env(snap)
        )
        text = _set_marker(text, _ENV_KEY_RE, templates.ENV_KEY_PREFIX, env_key)
        changed = True
    if do_map and (force_map or _stored(text, _MAP_KEY_RE) != map_key):
        text = _replace_block(
            text,
            templates.GEN_MAP_START,
            templates.GEN_MAP_END,
            _render_map(snap, root, config),
        )
        text = _set_marker(text, _MAP_KEY_RE, templates.MAP_KEY_PREFIX, map_key)
        changed = True
    if changed:
        write_atomic(path, text)
    return changed, (env_key, map_key)


def _from_snapshot(
    snap: dict,
    root: pathlib.Path,
    config: dict,
    map_command: bool = True,
) -> tuple[str, tuple[str, str]]:
    env_key = _env_key_from(snap)
    map_key = _map_key_from(snap)
    map_root, map_cfg = (root, config) if map_command else (None, None)
    content = (
        templates.rule_header(snap["name"])
        + "\n"
        + _render_env(snap)
        + "\n\n"
        + _render_map(snap, map_root, map_cfg)
        + "\n\n"
        + _env_marker(env_key)
        + "\n"
        + _map_marker(map_key)
        + "\n"
    )
    return content, (env_key, map_key)
