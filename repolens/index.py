"""repolens.index — build the search index (a disposable SQLite cache).

One `docs` table: one row per markdown file (full text), per code/config file
(purpose line only), and per DB table (schema only). FTS5 is preferred; falls back
to a plain table when the sqlite build lacks FTS5. A `frontmatter` EAV table
(relpath, key, value) makes any frontmatter key queryable — sparse (a doc has no
row for a key it lacks), schema-imposing NOTHING. A `files` table (relpath, size,
mtime, hash) drives INCREMENTAL indexing: `build_incremental()` stat-gates → hashes
→ upserts only changed files and reconciles deletes, in one WAL transaction. Full
`build()` (temp file + os.replace) stays the always-correct `--rebuild` backstop.

All paths/skip-lists/extensions come from config — nothing repo-specific here.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import sqlite3
import subprocess
import sys
import time

from . import frontmatter, purpose, semantic


def _first_heading(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return ""


# ═══════════════════════════════════════════════════════════════
# _not_ignored()
# ═══════════════════════════════════════════════════════════════
# The set of repo-relative paths git does NOT ignore (tracked +
# untracked-but-not-ignored), via `git ls-files -co --exclude-standard`.
# Returns None when include_gitignored is set, or the root isn't a git
# repo / git is missing — meaning "no gitignore to respect, index all".
# Memoized on config so a build's ~4 walks share one git call. When the
# git call fails but a .gitignore EXISTS, warn once on stderr — the
# ignore rules are NOT being honored, a real leak surface for a tool
# that feeds an agent's context.
# ═══════════════════════════════════════════════════════════════
def _not_ignored(root: pathlib.Path, config: dict):
    if config.get("include_gitignored"):
        return None
    if "_not_ignored_cache" in config:
        return config["_not_ignored_cache"]
    result = None
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-co", "--exclude-standard"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        result = {ln for ln in out.stdout.splitlines() if ln}
    except (OSError, subprocess.SubprocessError):
        result = None  # not a git repo / git missing → index everything
        if (root / ".gitignore").exists():
            print(
                "⚠ not a git repo — .gitignore is NOT enforced; all files indexed "
                "(set include_gitignored=true to silence, or run inside a git repo)",
                file=sys.stderr,
            )
    config["_not_ignored_cache"] = result
    return result


def _walk(root: pathlib.Path, config: dict, code: bool):
    skip_dirs, skip_files = config["skip_dirs"], config["skip_files"]
    exts = config["code_exts"]
    allowed = _not_ignored(root, config)  # None = index all (see docstring)
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in skip_dirs]
        for fn in fns:
            is_code = os.path.splitext(fn)[1].lower() in exts
            if (code and is_code) or (not code and fn.endswith(".md")):
                p = pathlib.Path(dp) / fn
                if p.is_symlink():
                    continue  # never follow a symlink out of the repo (leak surface)
                rel = str(p.relative_to(root))
                if rel in skip_files:
                    continue
                if allowed is not None and rel not in allowed:
                    continue  # gitignored — skipped by default (see _not_ignored)
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
# _content_hash()
# ═══════════════════════════════════════════════════════════════
# A cross-machine-stable change signal (blake2b, 16-byte digest) — the
# correctness backstop behind the cheap size+mtime stat-gate. mtime is
# unreliable across clones/machines; the hash is not.
# ═══════════════════════════════════════════════════════════════
def _content_hash(p: pathlib.Path) -> str:
    h = hashlib.blake2b(digest_size=16)
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _stat(p: pathlib.Path) -> tuple[int, float]:
    st = p.stat()
    return st.st_size, st.st_mtime


def _db_sig(config: dict) -> str:
    """A signature of the configured DBs' mtimes — so db-table rows are
    refreshed only when a DB file actually changed."""
    parts = []
    for sq in config["sqlite_paths"]:
        try:
            parts.append(f"{sq}:{sq.stat().st_mtime}")
        except OSError:
            continue
    return "|".join(parts)


def _embed_sig(config: dict) -> str:
    """A signature of the ACTIVE embedding config (model:dims) — stored in `meta` so a
    later model/dims change, or enabling semantic on a lexically-built index, is detected
    and forces a backfill. Empty when the tier is off/unavailable — including after a
    model-load failure (semantic.available() flips False), so a build whose embeddings
    silently didn't happen is correctly seen as 'not embedded' next time."""
    sm = config.get("semantic", {})
    if not (sm.get("enabled") and semantic.available(config)):
        return ""
    return f"{sm.get('model', '')}:{sm.get('dims', '')}"


# ═══════════════════════════════════════════════════════════════
# _create_schema()
# ═══════════════════════════════════════════════════════════════
# docs (FTS5 or plain) + the sparse frontmatter EAV + the files
# bookkeeping table + a tiny meta kv, then the semantic chunk/vector
# tables (a no-op when the [semantic] extra isn't installed). Nothing
# repo-specific.
# ═══════════════════════════════════════════════════════════════
def _create_schema(con: sqlite3.Connection, config: dict) -> None:
    if has_fts5():
        # porter stemming so `find "ranking"` matches "ranked" — closes a real
        # recall gap. (Won't split identifiers; camelCase/snake_case stays literal.)
        con.execute(
            "CREATE VIRTUAL TABLE docs USING fts5(relpath, title, frontmatter, body, "
            'kind UNINDEXED, tokenize="porter unicode61")'
        )
    else:
        con.execute("CREATE TABLE docs (relpath, title, frontmatter, body, kind)")
    con.execute("CREATE TABLE frontmatter (relpath TEXT, key TEXT, value TEXT)")
    con.execute("CREATE INDEX fm_key ON frontmatter(key, value)")
    con.execute("CREATE INDEX fm_rel ON frontmatter(relpath)")
    con.execute(
        "CREATE TABLE files (relpath TEXT PRIMARY KEY, size INTEGER, mtime REAL, hash TEXT)"
    )
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    if config["semantic"]["enabled"] and semantic.available(config):
        semantic.ensure_schema(con, config)


# ═══════════════════════════════════════════════════════════════
# _insert_doc()
# ═══════════════════════════════════════════════════════════════
# Index one file into `docs` (+ frontmatter EAV rows for markdown, +
# per-chunk embeddings for markdown when the [semantic] tier is on).
# Caller owns the `files` bookkeeping row and must have cleared any
# prior chunks for this relpath (see semantic.delete_doc). Returns True
# if indexed. Files larger than max_bytes are skipped (unbounded-read /
# index-bloat guard) — 0 means no cap.
# ═══════════════════════════════════════════════════════════════
def _maybe_embed(con: sqlite3.Connection, rel: str, text: str, config: dict) -> None:
    # Semantic tier: embed `text` for this doc (chunks + vectors). No-op when the extra
    # is absent, [semantic].enabled is false, or text is empty. A single doc's embed
    # failure (endpoint down, model load) must not abort the whole build — log and skip;
    # the doc stays lexically indexed (still findable).
    if not (text and config["semantic"]["enabled"] and semantic.available(config)):
        return
    try:
        semantic.embed_doc(con, rel, text, config)
    except Exception as e:  # noqa: BLE001 — isolate a per-doc embed failure
        print(f"⚠ embed skipped for {rel}: {e}", file=sys.stderr)


def _insert_doc(
    con: sqlite3.Connection,
    root: pathlib.Path,
    p: pathlib.Path,
    is_code: bool,
    config: dict,
    max_bytes: int = 0,
) -> bool:
    rel = str(p.relative_to(root))
    try:
        if max_bytes and p.stat().st_size > max_bytes:
            return False  # too large — skip (see max_file_bytes)
        text = p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    if is_code:
        pl = purpose.extract_purpose(rel, text)
        # The searchable text for code is the FULL doc block (module docstring /
        # leading comment run, capped — purpose.extract_doc); the one-line purpose
        # stays the display title. BM25 body + the embedder both see the block,
        # closing the "grep can read the docstring but the index can't" gap. No
        # block => the one-liner; neither => path/filename signal only, no vector.
        doc = purpose.extract_doc(rel, text) or pl
        con.execute(
            "INSERT INTO docs VALUES (?,?,?,?,?)", (rel, pl or p.name, "", doc, "code")
        )
        _maybe_embed(con, rel, doc, config)
    else:
        kv, block = frontmatter.parse_frontmatter(text)
        con.execute(
            "INSERT INTO docs VALUES (?,?,?,?,?)",
            (rel, _first_heading(text), block.replace("\n", " "), text, "md"),
        )
        if kv:
            con.executemany(
                "INSERT INTO frontmatter VALUES (?,?,?)",
                [(rel, k, v) for k, v in kv.items()],
            )
        _maybe_embed(con, rel, text, config)  # embed the full markdown body
    return True


# ═══════════════════════════════════════════════════════════════
# _index_db_tables()
# ═══════════════════════════════════════════════════════════════
# Insert one docs row per table/view of each configured SQLite DB —
# schema only (names), read-only. Returns the table count.
# ═══════════════════════════════════════════════════════════════
def _index_db_tables(con: sqlite3.Connection, config: dict) -> int:
    tables = 0
    for sq in config["sqlite_paths"]:
        if not sq.exists():
            continue
        try:
            ext = sqlite3.connect(f"file:{sq}?mode=ro", uri=True)
            for name, typ in ext.execute(
                "SELECT name, type FROM sqlite_master WHERE type IN ('table','view') "
                "AND name NOT LIKE 'sqlite_%'"  # skip internal bookkeeping tables
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
                        f"{sq.name} {typ} {name} columns: {cols}",
                        "db-table",
                    ),
                )
                tables += 1
            ext.close()
        except sqlite3.Error:
            pass
    return tables


# ═══════════════════════════════════════════════════════════════
# build()
# ═══════════════════════════════════════════════════════════════
# FULL rebuild from scratch into a temp DB, then atomically
# os.replace() it over config["index_path"]. The always-correct
# `--rebuild` backstop. Returns (n_docs, n_code, n_tables, elapsed_ms).
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
    _create_schema(con, config)

    n = code = 0
    max_bytes = config.get("max_file_bytes", 0)
    for is_code in (False, True):
        for p in _walk(root, config, code=is_code):
            if not _insert_doc(con, root, p, is_code, config, max_bytes):
                continue
            try:
                size, mtime = _stat(p)
                con.execute(
                    "INSERT OR REPLACE INTO files VALUES (?,?,?,?)",
                    (str(p.relative_to(root)), size, mtime, _content_hash(p)),
                )
            except OSError:
                pass
            code += is_code
            n += not is_code

    tables = _index_db_tables(con, config)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('db_sig', ?)", (_db_sig(config),))
    con.execute(
        "INSERT OR REPLACE INTO meta VALUES ('embed_sig', ?)", (_embed_sig(config),)
    )
    con.execute("INSERT OR REPLACE INTO meta VALUES ('writes_since_optimize','0')")
    if has_fts5():
        con.execute("INSERT INTO docs(docs) VALUES('optimize')")
    con.commit()
    con.close()
    os.replace(tmp, db_path)
    return n, code, tables, (time.time() - t0) * 1000


# ═══════════════════════════════════════════════════════════════
# build_incremental()
# ═══════════════════════════════════════════════════════════════
# Re-index ONLY changed files against the live index, in one WAL
# transaction. Stat-gate (size+mtime) → hash-confirm → DELETE+INSERT
# upsert on the regular FTS5 table → delete-reconcile vanished files.
# db-tables refresh only when a DB file's mtime changed. optimize() every
# ~200 writes. Falls back to a full build() when the index is missing or
# predates this schema. Returns (changed, deleted, elapsed_ms).
# ═══════════════════════════════════════════════════════════════
def build_incremental(root: pathlib.Path, config: dict) -> tuple[int, int, float]:
    t0 = time.time()
    db_path = config["index_path"]
    if not db_path.exists():
        n, code, tables, _ = build(root, config)
        return n + code + tables, 0, (time.time() - t0) * 1000
    con = sqlite3.connect(
        db_path, timeout=30
    )  # 30s busy window from the first statement
    con.execute("PRAGMA journal_mode=WAL")
    # Wait up to 30s for the single WAL writer lock rather than the implicit 5s — a slow
    # incremental (many changed files with embedding) can hold it past 5s, and a bare
    # default makes every concurrent session's `find` refresh crash. cmd_find also
    # catches the timeout and degrades to a read-only search (see cli.cmd_find).
    con.execute("PRAGMA busy_timeout=30000")
    have = {
        r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if not {"files", "frontmatter", "meta"} <= have:
        con.close()  # index predates the incremental schema → full rebuild
        n, code, tables, _ = build(root, config)
        return n + code + tables, 0, (time.time() - t0) * 1000

    max_bytes = config.get("max_file_bytes", 0)
    sem_on = config["semantic"]["enabled"] and semantic.available(config)

    # BACKFILL trigger: semantic is on but this index's embeddings don't match the active
    # config — built lexical-first (embed_sig ""), a model/dims change, or a build whose
    # embeddings silently didn't happen. A plain incremental only touches CHANGED files, so
    # it would never backfill and would silently serve lexical while reporting hybrid. Force
    # a full rebuild so hybrid actually runs. (embed_sig alone is loop-safe: after a rebuild
    # the stored sig matches, even for a corpus that legitimately produces zero chunks.)
    if sem_on:
        stored_embed = (
            con.execute("SELECT value FROM meta WHERE key='embed_sig'").fetchone()
            or ("",)
        )[0]
        if stored_embed != _embed_sig(config):
            con.close()
            n, code, tables, _ = build(root, config)
            return n + code + tables, 0, (time.time() - t0) * 1000

    # DETECTION (read-only — no write lock yet): stat-gate the corpus against the files
    # table. Only files whose (size, mtime) differ need hashing; a clean no-op reads nothing.
    stored = {
        r[0]: (r[1], r[2], r[3])
        for r in con.execute("SELECT relpath, size, mtime, hash FROM files")
    }
    seen: set[str] = set()
    stat_changed: list[tuple[str, pathlib.Path, int, float, bool]] = []
    for is_code in (False, True):
        for p in _walk(root, config, code=is_code):
            rel = str(p.relative_to(root))
            seen.add(rel)
            try:
                size, mtime = _stat(p)
            except OSError:
                continue
            prev = stored.get(rel)
            if prev and prev[0] == size and abs(prev[1] - mtime) < 1e-6:
                continue  # stat-gate: unchanged, no read/hash
            stat_changed.append((rel, p, size, mtime, is_code))
    to_delete = set(stored) - seen
    cur_db_sig = _db_sig(config)
    prev_db_sig = (
        con.execute("SELECT value FROM meta WHERE key='db_sig'").fetchone() or ("",)
    )[0]

    # TRUE no-op — nothing stat-changed, nothing deleted, DB unchanged. Take NO write lock
    # (a deferred/immediate BEGIN would still fsync a WAL write and serialize concurrent
    # find-refreshers). This is the common agent case: find repeatedly, nothing changed.
    if not stat_changed and not to_delete and cur_db_sig == prev_db_sig:
        con.close()
        return 0, 0, (time.time() - t0) * 1000

    # WRITE phase — there's real work, so now acquire the write lock. BEGIN IMMEDIATE takes
    # the RESERVED lock up front; a deferred BEGIN would read-then-upgrade, and SQLite
    # fast-fails that upgrade with SQLITE_BUSY *without* honoring busy_timeout when another
    # session holds the lock — so IMMEDIATE is what makes busy_timeout actually serialize.
    changed = deleted = 0
    if sem_on:
        # An index built before the semantic tier has no chunk tables yet; create them
        # (idempotent) so incremental embedding has somewhere to write.
        semantic.ensure_schema(con, config)
    con.execute("BEGIN IMMEDIATE")
    try:
        for rel, p, size, mtime, is_code in stat_changed:
            h = _content_hash(p)
            # Re-read the CURRENT stored hash UNDER THE LOCK, not the pre-lock snapshot: while
            # this process waited for the write lock, a peer (thundering herd — every
            # concurrent `find` saw the same changed file) may have already committed it. If
            # the stored hash now matches, skip the redundant delete+re-embed. Also covers the
            # plain touched-but-identical case (same content, new mtime).
            cur = con.execute(
                "SELECT hash FROM files WHERE relpath=?", (rel,)
            ).fetchone()
            if cur and cur[0] == h:
                con.execute("UPDATE files SET mtime=? WHERE relpath=?", (mtime, rel))
                continue
            con.execute("DELETE FROM docs WHERE relpath=?", (rel,))
            con.execute("DELETE FROM frontmatter WHERE relpath=?", (rel,))
            if sem_on:
                semantic.delete_doc(con, rel)  # drop the doc's old chunk-set first
            if _insert_doc(con, root, p, is_code, config, max_bytes):
                con.execute(
                    "INSERT OR REPLACE INTO files VALUES (?,?,?,?)",
                    (rel, size, mtime, h),
                )
                changed += 1

        for rel in to_delete:  # delete-reconcile
            con.execute("DELETE FROM docs WHERE relpath=?", (rel,))
            con.execute("DELETE FROM frontmatter WHERE relpath=?", (rel,))
            con.execute("DELETE FROM files WHERE relpath=?", (rel,))
            if sem_on:
                semantic.delete_doc(con, rel)  # cascade-delete orphaned chunks/vectors
            deleted += 1

        if cur_db_sig != prev_db_sig:
            con.execute("DELETE FROM docs WHERE kind='db-table'")
            _index_db_tables(con, config)
            con.execute(
                "INSERT OR REPLACE INTO meta VALUES ('db_sig', ?)", (cur_db_sig,)
            )

        w = int(
            (
                con.execute(
                    "SELECT value FROM meta WHERE key='writes_since_optimize'"
                ).fetchone()
                or ("0",)
            )[0]
        )
        w += changed + deleted
        if w >= 200 and has_fts5():
            con.execute("INSERT INTO docs(docs) VALUES('optimize')")
            w = 0
        con.execute(
            "INSERT OR REPLACE INTO meta VALUES ('writes_since_optimize', ?)", (str(w),)
        )
        con.commit()
    except Exception:
        con.rollback()  # release the write lock on any mid-transaction failure
        raise
    finally:
        con.close()
    return changed, deleted, (time.time() - t0) * 1000


# ═══════════════════════════════════════════════════════════════
# optimize()
# ═══════════════════════════════════════════════════════════════
# Manually compact the FTS5 index (merge b-trees) and reset the
# writes-since-optimize counter. No-op without FTS5 / a missing index.
# ═══════════════════════════════════════════════════════════════
def optimize(config: dict) -> None:
    db_path = config["index_path"]
    if not db_path.exists() or not has_fts5():
        return
    con = sqlite3.connect(db_path)
    try:
        con.execute("INSERT INTO docs(docs) VALUES('optimize')")
        con.execute("INSERT OR REPLACE INTO meta VALUES ('writes_since_optimize','0')")
        con.commit()
    finally:
        con.close()
