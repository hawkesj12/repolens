"""repolens test suite — core engine + the fixes it was hardened with."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys

import pytest

from repolens import (
    bench,
    chunk,
    cli,
    discover,
    find,
    frontmatter,
    index,
    lint,
    log,
    purpose,
    root,
    schema,
    semantic,
)


@pytest.fixture(autouse=True)
def _semantic_off_by_default(monkeypatch):
    # Keep the suite hermetic + deterministic whether or not the [semantic] extra is
    # installed: the REAL tier is off by default, so `find` stays lexical (matching the
    # pinned rankings) and no index build downloads a model. Semantic tests opt back in
    # with fake embeddings via _force_blob. These tests cover the tier's WIRING (fusion,
    # KNN, chunking, cascade) with a fake embedder; retrieval QUALITY is not measured here
    # and has no committed benchmark yet (see CHANGELOG 0.9.0 "Honest status").
    monkeypatch.setattr(semantic, "available", lambda *a, **k: False)


def _mkdb(path, tables):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    for t in tables:
        con.execute(f"CREATE TABLE {t} (id, val)")
    con.commit()
    con.close()


def _repo(tmp_path, toml="", files=None):
    (tmp_path / ".repolens.toml").write_text(toml)
    for rel, body in (files or {}).items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return tmp_path, root.load_config(tmp_path)


TYPES_TOML = """\
[types.note]
folder = "notes"
recursive = true
exclude = ["*draft*"]
require = ["^\\\\*\\\\*Date:\\\\*\\\\*"]

[types.contact]
folder = "people"
recursive = false
"""


# ── root + config ──────────────────────────────────────────────
def test_find_root_via_marker(tmp_path):
    (tmp_path / ".repolens.toml").write_text("")
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert root.find_root(sub) == tmp_path.resolve()


def test_load_config_defaults_and_parse(tmp_path):
    _root, cfg = _repo(tmp_path, TYPES_TOML)
    assert cfg["index_path"] == tmp_path / ".repolens/index.db"
    assert ".git" in cfg["skip_dirs"]  # defaults present
    assert cfg["types"]["note"]["recursive"] is True
    assert cfg["types"]["note"]["require"] == ["^\\*\\*Date:\\*\\*"]
    assert cfg["sqlite_paths"] == []  # integration off by default


def test_find_root_ignores_install_dir(tmp_path, monkeypatch):
    # A dir with .git but no .repolens.toml, entered as cwd, must resolve to
    # ITSELF — never to the repolens install dir (the __file__ footgun). This
    # is the panel's confirmed blocker: an editable/venv-in-repo install must
    # not leak the wrong repo.
    target = tmp_path / "otherrepo"
    (target / ".git").mkdir(parents=True)
    monkeypatch.chdir(target)
    assert root.find_root() == target.resolve()


def test_load_config_max_file_bytes(tmp_path):
    _root, cfg = _repo(tmp_path, "")
    assert cfg["max_file_bytes"] == root.DEFAULT_MAX_FILE_BYTES  # default
    _root, cfg = _repo(tmp_path, "[repolens]\nmax_file_bytes = 1024\n")
    assert cfg["max_file_bytes"] == 1024  # override


def test_load_config_sqlite_backward_compat_singular_path(tmp_path):
    _root, cfg = _repo(tmp_path, '[integrations.sqlite]\npath = "data/app.db"\n')
    assert cfg["sqlite_paths"] == [tmp_path / "data/app.db"]


def test_load_config_sqlite_paths_list_and_merge_dedup(tmp_path):
    _root, cfg = _repo(
        tmp_path, '[integrations.sqlite]\npaths = ["data/a.db", "data/b.db"]\n'
    )
    assert cfg["sqlite_paths"] == [tmp_path / "data/a.db", tmp_path / "data/b.db"]
    # legacy `path` + `paths` merge, deduped, order preserved
    _root, cfg = _repo(
        tmp_path,
        '[integrations.sqlite]\npath = "data/a.db"\npaths = ["data/a.db", "data/b.db"]\n',
    )
    assert cfg["sqlite_paths"] == [tmp_path / "data/a.db", tmp_path / "data/b.db"]


# ── classification ─────────────────────────────────────────────
def test_classify_recursive_exclude_and_frontmatter(tmp_path):
    _root, cfg = _repo(tmp_path, TYPES_TOML)
    assert schema.type_from_folder("notes/a.md", cfg) == "note"
    assert schema.type_from_folder("notes/2026/sub.md", cfg) == "note"  # recursive
    assert schema.type_from_folder("notes/my-draft.md", cfg) is None  # excluded
    assert schema.type_from_folder("people/x/deep.md", cfg) is None  # non-recursive
    assert schema.type_from_folder("notes/README.md", cfg) is None  # scaffold
    assert (
        schema.classify("elsewhere/x.md", "---\ntype: contact\n---\n", cfg) == "contact"
    )


def test_validate_doc_require(tmp_path):
    _root, cfg = _repo(tmp_path, TYPES_TOML)
    assert schema.validate_doc("notes/a.md", "# A\n\n**Date:** 2026-07-11\n", cfg) == []
    bad = schema.validate_doc("notes/b.md", "# B\n\nno date here\n", cfg)
    assert any("missing required pattern" in m for _s, _c, m in bad)


# ── purpose extractor ──────────────────────────────────────────
def test_purpose_skips_and_docstring():
    assert purpose.extract_purpose("x.md", "# T\n\n1. item\n") != "1. item"
    md = "# T\n\nThree things:\n\nThe real summary.\n"
    assert purpose.extract_purpose("y.md", md) == "The real summary."
    py = '#!/usr/bin/env python3\n"""Ingest Garmin data."""\ndef f():\n    """helper."""\n'
    assert "Garmin" in purpose.extract_purpose("g.py", py)


def test_extract_doc_full_docstring_and_blocks():
    # .py: the FULL module docstring survives, not just sentence one (extract_purpose
    # keeps the one-liner; extract_doc is what the index/embedder sees).
    py = '"""First line.\n\nSecond paragraph explains the whole design.\n"""\nx = 1\n'
    doc = purpose.extract_doc("m.py", py)
    assert "First line." in doc and "whole design" in doc
    assert purpose.extract_purpose("m.py", py) == "First line."
    # .py without a docstring: the contiguous leading # block; banner lines dropped;
    # the first non-comment line ends the block.
    py2 = "#!/bin/sh\n# ═══════════\n# alpha tool\n# does beta things\ncode()\n# later comment\n"
    doc2 = purpose.extract_doc("t.sh", py2)
    assert doc2 == "alpha tool\ndoes beta things"
    # .js: // block
    assert purpose.extract_doc("a.js", "// one\n// two\nlet x\n") == "one\ntwo"
    # cap enforced
    long_py = '"""' + ("words " * 600) + '"""\n'
    assert len(purpose.extract_doc("l.py", long_py)) <= purpose.DOC_MAX_CHARS
    # garbage / no comments / unknown ext → "" and never raises
    assert purpose.extract_doc("b.py", "x = 1\n") == ""
    assert purpose.extract_doc("noext", "\x00\x01junk") == ""


# ── index build ────────────────────────────────────────────────
def test_build_atomic_single_docs_table(tmp_path):
    _root, cfg = _repo(
        tmp_path, "", {"a.md": "# A\n\nhello world\n", "s.py": '"""does a thing."""\n'}
    )
    n, code, tables, ms = index.build(tmp_path, cfg)
    assert n >= 1 and code >= 1
    assert not list((tmp_path / ".repolens").glob("*.tmp-*"))
    con = sqlite3.connect(cfg["index_path"])
    names = {
        r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    # v0.5 schema: docs + the incremental/frontmatter bookkeeping tables
    assert {"docs", "files", "frontmatter", "meta"} <= names
    assert not list((tmp_path / ".repolens").glob("*.tmp-*"))  # temp swapped away
    kinds = {r[0] for r in con.execute("SELECT DISTINCT kind FROM docs")}
    assert "md" in kinds and "code" in kinds
    con.close()


def test_config_file_is_not_indexed(tmp_path):
    # repolens's own .repolens.toml is tooling, not corpus — it must never show up in
    # results (it did, as noise in a fresh user's first `find`).
    _root, cfg = _repo(tmp_path, "", {"a.md": "# A\n\nhello world\n"})
    index.build(tmp_path, cfg)
    con = sqlite3.connect(cfg["index_path"])
    rels = {r[0] for r in con.execute("SELECT relpath FROM docs")}
    con.close()
    assert ".repolens.toml" not in rels
    assert "a.md" in rels


# ── find: LIKE fallback ranked + loud ──────────────────────────
def _plain_index(cfg):
    cfg["index_path"].parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(cfg["index_path"])
    con.execute("CREATE TABLE docs (relpath, title, frontmatter, body, kind)")
    con.executemany(
        "INSERT INTO docs VALUES (?,?,?,?,?)",
        [
            ("a.md", "alpha", "", "garmin only", "md"),
            ("b.md", "beta", "", "garmin ingest both", "md"),
        ],
    )
    con.commit()
    con.close()


def test_like_search_ranks_and_announces(tmp_path, capsys):
    _root, cfg = _repo(tmp_path, "")
    _plain_index(cfg)
    hits = find.search(cfg, "garmin ingest", 5)
    assert hits[0]["relpath"] == "b.md"  # 2 terms > 1
    assert "FTS5 unavailable" in capsys.readouterr().err


def test_fts5_ranks_path_title_hit_first(tmp_path):
    # The headline claim: a file whose PATH/title carries the term outranks one
    # where the term is only in the body. Pins bm25 ORDER on the real FTS5 path
    # (existing tests only assert membership; the one order test is on LIKE).
    if not index.has_fts5():
        return  # FTS5-less sqlite build → skip (LIKE order is tested separately)
    _root, cfg = _repo(
        tmp_path,
        "",
        {
            "garmin.md": "# unrelated\n\nplain prose with no query terms here\n",
            "notes.md": "# notes\n\nthis body merely mentions garmin once\n",
        },
    )
    index.build(tmp_path, cfg)
    hits = find.search(cfg, "garmin")
    assert hits[0]["relpath"] == "garmin.md"  # path hit beats a body-only hit


def test_fts5_porter_stemming(tmp_path):
    # porter tokenizer: querying "ranking" finds a doc that says "ranked".
    if not index.has_fts5():
        return
    _root, cfg = _repo(tmp_path, "", {"a.md": "# A\n\nresults are ranked by score\n"})
    index.build(tmp_path, cfg)
    assert any(h["relpath"] == "a.md" for h in find.search(cfg, "ranking"))


def test_fts5_broadens_to_or_on_zero_and_hits(tmp_path, capsys):
    # Multi-word FTS5 is implicit-AND; on a zero all-term match, broaden to
    # any-term (OR) and announce it, rather than silently returning nothing.
    if not index.has_fts5():
        return
    _root, cfg = _repo(
        tmp_path,
        "",
        {"a.md": "# A\n\nonly garmin here\n", "b.md": "# B\n\nonly deploy here\n"},
    )
    index.build(tmp_path, cfg)
    hits = find.search(cfg, "garmin deploy")  # no single doc has both
    rels = {h["relpath"] for h in hits}
    assert rels == {"a.md", "b.md"}  # broadened OR found both
    assert "broadened to any-term" in capsys.readouterr().err


def test_find_returns_matching_passage(tmp_path):
    # Every hit carries a `snippet` — the passage that matched. In lexical mode
    # (semantic off by default here) it's the FTS5 excerpt from the body.
    _root, cfg = _repo(
        tmp_path,
        "",
        {
            "deploy.md": "# Deploy\n\nThe deploy config lives in the staging pipeline "
            "manifest, not in the app repo.\n"
        },
    )
    index.build(tmp_path, cfg)
    hits = find.search(cfg, "deploy config", 5)
    assert hits and hits[0]["relpath"] == "deploy.md"
    assert "snippet" in hits[0]
    snip = hits[0]["snippet"].lower()
    assert "deploy" in snip and "config" in snip  # the excerpt shows the matched text
    assert len(hits[0]["snippet"]) <= 240  # trimmed for display, one line
    assert "\n" not in hits[0]["snippet"]


def test_ensure_fresh_force(tmp_path, monkeypatch):
    _root, cfg = _repo(tmp_path, "")
    monkeypatch.setenv("REPOLENS_FORCE", "1")
    calls = []
    monkeypatch.setattr(index, "build", lambda *a, **k: calls.append(1))
    assert find.ensure_fresh(tmp_path, cfg) == "rebuilt (forced)"
    assert calls


# ── lint ───────────────────────────────────────────────────────
def test_lint_finds_real_issues(tmp_path):
    _root, cfg = _repo(
        tmp_path,
        TYPES_TOML,
        {
            "empty.md": "   \n",
            "notes/n.md": "# N\n\nno date\n",  # require-miss (warn)
            "d.md": "# D\n\n[x](missing.md)\n",  # dead link (warn)
        },
    )
    findings = lint.lint(tmp_path, cfg)
    checks = {f["check"] for f in findings}
    assert "empty-file" in checks
    assert "dead-link" in checks
    assert "missing-field" in checks
    assert lint.has_errors(findings) is True  # the empty file is an error


# ── sqlite auto-discovery ──────────────────────────────────────
def test_discover_finds_real_skips_backup_cache_and_nonsqlite(tmp_path):
    _root, cfg = _repo(tmp_path, "")
    _mkdb(tmp_path / "data/app.db", ["users", "orders"])  # real, 2 tables
    _mkdb(tmp_path / "data/app.db.bak-20260101", ["users"])  # backup → skip
    (tmp_path / "data/notes.db").write_text("not a database")  # non-sqlite → skip
    _mkdb(cfg["index_path"], ["docs"])  # repolens's own cache → skip
    assert discover.discover_sqlite_dbs(tmp_path, cfg) == [("data/app.db", 2)]


def test_discover_multiple_sorted(tmp_path):
    _root, cfg = _repo(tmp_path, "")
    _mkdb(tmp_path / "data/b.sqlite", ["t2", "t3"])
    _mkdb(tmp_path / "data/a.db", ["t1"])
    assert discover.discover_sqlite_dbs(tmp_path, cfg) == [
        ("data/a.db", 1),
        ("data/b.sqlite", 2),
    ]


def test_build_indexes_multiple_dbs_schema_only(tmp_path):
    _root, cfg = _repo(tmp_path, '[integrations.sqlite]\npaths = ["a.db", "b.db"]\n')
    con = sqlite3.connect(tmp_path / "a.db")
    con.execute("CREATE TABLE alpha (id, secret_col)")
    con.execute("INSERT INTO alpha VALUES (1, 'TOPSECRET_ROW_VALUE')")
    con.commit()
    con.close()
    _mkdb(tmp_path / "b.db", ["beta"])
    _n, _code, tables, _ms = index.build(tmp_path, cfg)
    assert tables == 2
    con = sqlite3.connect(cfg["index_path"])
    rows = con.execute(
        "SELECT relpath, body FROM docs WHERE kind='db-table'"
    ).fetchall()
    con.close()
    assert sorted(r[0] for r in rows) == ["a.db :: alpha", "b.db :: beta"]
    blob = " ".join(r[1] for r in rows)
    assert "secret_col" in blob  # COLUMN name indexed (schema)
    assert "TOPSECRET_ROW_VALUE" not in blob  # ROW data never indexed


def test_cmd_init_wires_discovered_db(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(root, "find_root", lambda *a, **k: tmp_path)
    _mkdb(tmp_path / "data/app.db", ["users", "orders"])
    cli.main(["init"])
    cfg_text = (tmp_path / ".repolens.toml").read_text()
    assert 'paths = ["data/app.db"]' in cfg_text
    assert root.load_config(tmp_path)["sqlite_paths"] == [tmp_path / "data/app.db"]
    assert "found data/app.db (2 tables)" in capsys.readouterr().out


def test_cmd_init_no_db_skips_discovery(tmp_path, monkeypatch):
    monkeypatch.setattr(root, "find_root", lambda *a, **k: tmp_path)
    _mkdb(tmp_path / "app.db", ["t"])
    cli.main(["init", "--no-db"])
    # no ACTIVE block appended (the template's own block is commented: "# [")
    assert "\n[integrations.sqlite]" not in (tmp_path / ".repolens.toml").read_text()
    assert root.load_config(tmp_path)["sqlite_paths"] == []


def test_cmd_init_existing_config_no_append(tmp_path, monkeypatch):
    monkeypatch.setattr(root, "find_root", lambda *a, **k: tmp_path)
    (tmp_path / ".repolens.toml").write_text("[repolens]\n")
    _mkdb(tmp_path / "app.db", ["t"])
    cli.main(["init"])  # config exists, no --force → no discovery/append
    assert "[integrations.sqlite]" not in (tmp_path / ".repolens.toml").read_text()


# ── hook (NON-DESTRUCTIVE) ─────────────────────────────────────


# ── gitignore-respecting default ───────────────────────────────
def _git_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)


def test_index_respects_gitignore_by_default(tmp_path):
    _git_repo(tmp_path)
    _root, cfg = _repo(
        tmp_path,
        "",
        {
            "README.md": "# a\n\np\n",
            "public.md": "visible\n",
            "SECRET.md": "hidden\n",
            ".gitignore": "SECRET.md\n",
        },
    )
    index.build(tmp_path, cfg)
    con = sqlite3.connect(cfg["index_path"])
    md = {r[0] for r in con.execute("SELECT relpath FROM docs WHERE kind='md'")}
    con.close()
    assert "public.md" in md and "README.md" in md
    assert "SECRET.md" not in md  # gitignored → skipped by default


def test_index_include_gitignored_opt_in(tmp_path):
    _git_repo(tmp_path)
    _root, cfg = _repo(
        tmp_path,
        "[repolens]\ninclude_gitignored = true\n",
        {"SECRET.md": "hidden\n", ".gitignore": "SECRET.md\n"},
    )
    assert cfg["include_gitignored"] is True
    index.build(tmp_path, cfg)
    con = sqlite3.connect(cfg["index_path"])
    md = {r[0] for r in con.execute("SELECT relpath FROM docs WHERE kind='md'")}
    con.close()
    assert "SECRET.md" in md  # opt-in indexes gitignored content


def test_index_all_when_not_a_git_repo(tmp_path):
    # No `git init` — nothing to respect, so everything indexes (backward-compat).
    _root, cfg = _repo(tmp_path, "", {"a.md": "# a\n\np\n", ".gitignore": "a.md\n"})
    index.build(tmp_path, cfg)
    con = sqlite3.connect(cfg["index_path"])
    md = {r[0] for r in con.execute("SELECT relpath FROM docs WHERE kind='md'")}
    con.close()
    assert "a.md" in md  # not a git repo → no gitignore to respect


def test_warns_when_gitignore_unenforced_outside_git(tmp_path, capsys):
    # A .gitignore in a non-git dir is NOT honored — warn loudly so a user
    # can't trust a boundary that isn't enforced (panel security finding).
    _root, cfg = _repo(tmp_path, "", {"a.md": "# a\n\np\n", ".gitignore": "secret\n"})
    index.build(tmp_path, cfg)
    assert ".gitignore is NOT enforced" in capsys.readouterr().err


def test_no_warn_when_no_gitignore_outside_git(tmp_path, capsys):
    # No .gitignore → nothing to enforce → no noise.
    _root, cfg = _repo(tmp_path, "", {"a.md": "# a\n\np\n"})
    index.build(tmp_path, cfg)
    assert ".gitignore is NOT enforced" not in capsys.readouterr().err


def test_index_skips_symlinks(tmp_path):
    # A file symlink must never be followed out of the repo — else a link to
    # /etc/passwd or ~/.ssh/id_rsa lands in an agent's context (panel security).
    outside = tmp_path.parent / "outside_secret.md"
    outside.write_text("# secret\n\nleaked_via_symlink token\n")
    _root, cfg = _repo(tmp_path, "", {"real.md": "# real\n\nlegit body\n"})
    (tmp_path / "link.md").symlink_to(outside)
    index.build(tmp_path, cfg)
    con = sqlite3.connect(cfg["index_path"])
    rels = {r[0] for r in con.execute("SELECT relpath FROM docs")}
    con.close()
    assert "real.md" in rels
    assert "link.md" not in rels  # symlink skipped
    assert not any(h["relpath"] == "link.md" for h in find.search(cfg, "leaked"))


def test_index_skips_oversized_file(tmp_path):
    # A file above max_file_bytes is skipped (unbounded-read / bloat guard).
    _root, cfg = _repo(
        tmp_path,
        "[repolens]\nmax_file_bytes = 64\n",
        {"small.md": "# s\n\ntiny\n", "big.md": "# big\n\n" + "x " * 100 + "\n"},
    )
    index.build(tmp_path, cfg)
    con = sqlite3.connect(cfg["index_path"])
    rels = {r[0] for r in con.execute("SELECT relpath FROM docs")}
    con.close()
    assert "small.md" in rels
    assert "big.md" not in rels  # over the 64-byte cap → skipped


# ── frontmatter parser (total, stdlib) ─────────────────────────
def test_frontmatter_parses_flat_kv():
    kv, block = frontmatter.parse_frontmatter(
        "---\ntitle: Hello\ntags: [a, b]\ndomain: x\n---\n# H\n\nbody\n"
    )
    assert kv["title"] == "Hello" and kv["tags"] == "a, b" and kv["domain"] == "x"
    assert "title: Hello" in block
    kv2, _ = frontmatter.parse_frontmatter("---\naliases:\n  - one\n  - two\n---\nx")
    assert kv2["aliases"] == "one, two"  # block list flattened


def test_frontmatter_degrades_nested_to_text():
    kv, block = frontmatter.parse_frontmatter(
        "---\nmeta:\n  a: 1\n  b: 2\ntop: ok\n---\nbody\n"
    )
    assert kv.get("top") == "ok"
    assert "meta" not in kv  # nested map → no KV row (degraded)
    assert "a: 1" in block  # still in the raw block for full-text


def test_frontmatter_never_raises_on_malformed():
    for bad in [
        "---\nunclosed fence\n# no close",
        "---\n: : :\n---\n",
        "not frontmatter at all",
        "---\n---\n",
        "",
    ]:
        kv, block = frontmatter.parse_frontmatter(bad)
        assert isinstance(kv, dict) and isinstance(block, str)


# ── frontmatter EAV indexing (schema-agnostic) ─────────────────
def test_index_frontmatter_eav_sparse_arbitrary_keys(tmp_path):
    # Two DIFFERENT frontmatter conventions in one repo — both index, sparsely.
    _root, cfg = _repo(
        tmp_path,
        "",
        {
            "rules/x.md": "---\npaths: ['*.py']\ntype: rule\n---\n# X\n\nbody\n",
            "mem/y.md": "---\nname: y\ndescription: a memo\nstatus: active\n---\n# Y\n\nb\n",
        },
    )
    index.build(tmp_path, cfg)
    con = sqlite3.connect(cfg["index_path"])
    rows = {
        (r[0], r[1]): r[2]
        for r in con.execute("SELECT relpath, key, value FROM frontmatter")
    }
    con.close()
    assert rows[("rules/x.md", "type")] == "rule"
    assert rows[("rules/x.md", "paths")] == "*.py"
    assert rows[("mem/y.md", "description")] == "a memo"
    assert ("rules/x.md", "description") not in rows  # sparse: absent key = no row


def test_index_keeps_fts_frontmatter_blob(tmp_path):
    _root, cfg = _repo(
        tmp_path, "", {"a.md": "---\ndomain: retirement\n---\n# A\n\nbody\n"}
    )
    index.build(tmp_path, cfg)
    hits = find.search(cfg, "retirement")  # a frontmatter value is full-text searchable
    assert any(h["relpath"] == "a.md" for h in hits)


# ── incremental indexing ───────────────────────────────────────
def test_incremental_reindexes_only_changed(tmp_path):
    _root, cfg = _repo(
        tmp_path, "", {"a.md": "# A\n\nalpha\n", "b.md": "# B\n\nbeta\n"}
    )
    index.build(tmp_path, cfg)
    (tmp_path / "a.md").write_text("# A2\n\nalphachanged\n")
    changed, deleted, _ms = index.build_incremental(tmp_path, cfg)
    assert changed == 1 and deleted == 0
    assert any(h["relpath"] == "a.md" for h in find.search(cfg, "alphachanged"))


def test_incremental_detects_add(tmp_path):
    _root, cfg = _repo(tmp_path, "", {"a.md": "# A\n\nalpha\n"})
    index.build(tmp_path, cfg)
    (tmp_path / "c.md").write_text("# C\n\ngamma\n")
    changed, deleted, _ms = index.build_incremental(tmp_path, cfg)
    assert changed == 1 and deleted == 0
    assert any(h["relpath"] == "c.md" for h in find.search(cfg, "gamma"))


def test_incremental_reconciles_delete(tmp_path):
    _root, cfg = _repo(
        tmp_path, "", {"a.md": "# A\n\nalpha\n", "b.md": "# B\n\nbeta\n"}
    )
    index.build(tmp_path, cfg)
    (tmp_path / "b.md").unlink()
    changed, deleted, _ms = index.build_incremental(tmp_path, cfg)
    assert deleted == 1
    assert not any(h["relpath"] == "b.md" for h in find.search(cfg, "beta"))
    con = sqlite3.connect(cfg["index_path"])
    assert not con.execute("SELECT 1 FROM files WHERE relpath='b.md'").fetchone()
    con.close()


def test_rebuild_full_still_works(tmp_path):
    _root, cfg = _repo(tmp_path, "", {"a.md": "# A\n\nalpha\n"})
    index.build(tmp_path, cfg)
    (tmp_path / "a.md").write_text("# A\n\nalpha delta\n")
    index.build_incremental(tmp_path, cfg)
    n, code, _t, _ms = index.build(tmp_path, cfg)  # full rebuild parity
    assert n == 1
    assert any(h["relpath"] == "a.md" for h in find.search(cfg, "delta"))


# ── rich, tiered digest ────────────────────────────────────────


def test_cmd_index_rebuild_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(root, "find_root", lambda *a, **k: tmp_path)
    _repo(tmp_path, "", {"a.md": "# A\n\nx\n"})
    cli.main(["index", "--rebuild"])
    assert "built index" in capsys.readouterr().out


# ── enrich (local-model metadata generation; model call mocked) ─
def _fake_ask(config, prompt):
    if "DESCRIPTION" in prompt:
        return "DESCRIPTION: A test document about things.\nTAGS: Alpha, beta, gamma!"
    return "Does a test thing quickly."


# ── v0.9 semantic tier: config, chunking, vector store, hybrid ─────
def test_load_config_semantic_defaults_and_override(tmp_path):
    _root, cfg = _repo(tmp_path, "")
    assert cfg["semantic"]["enabled"] is True
    assert cfg["semantic"]["model"] == "BAAI/bge-base-en-v1.5"
    assert cfg["semantic"]["dims"] == 768 and cfg["semantic"]["chunk_tokens"] == 512
    assert (
        cfg["semantic"]["provider"] == "fastembed" and cfg["semantic"]["threads"] == 2
    )
    _root, cfg = _repo(
        tmp_path,
        '[semantic]\nprovider = "http"\nmodel = "nomic-embed-text"\n'
        'endpoint = "http://localhost:11434/v1/embeddings"\napi_key_env = "MY_KEY"\n'
        "threads = 4\nchunk_tokens = 400\n",
    )
    assert cfg["semantic"]["provider"] == "http"
    assert cfg["semantic"]["endpoint"] == "http://localhost:11434/v1/embeddings"
    assert cfg["semantic"]["api_key_env"] == "MY_KEY"
    assert cfg["semantic"]["threads"] == 4 and cfg["semantic"]["chunk_tokens"] == 400


def test_chunk_one_chunk_per_heading_section():
    doc = "preamble text here\n\n# Alpha\n\nalpha body\n\n## Beta\n\nbeta body\n"
    chunks = chunk.chunk_document(doc)
    texts = [t for _ix, t in chunks]
    assert len(chunks) == 3  # preamble + Alpha + Beta
    assert texts[0].startswith("preamble")
    assert "# Alpha" in texts[1] and "alpha body" in texts[1]
    assert "## Beta" in texts[2] and "beta body" in texts[2]
    assert [ix for ix, _c in chunks] == [0, 1, 2]


def test_chunk_short_headed_doc_single_chunk():
    chunks = chunk.chunk_document("# Title\n\nA short paragraph about foxes.\n")
    assert len(chunks) == 1 and "foxes" in chunks[0][1]


def test_chunk_oversized_section_subsplits_under_cap():
    # A single heading-section far larger than the cap must sub-split (never truncate).
    cap_tokens = 50  # cap = 200 chars
    body = "# Big\n\n" + "\n\n".join(f"paragraph {i} filler words" for i in range(60))
    chunks = chunk.chunk_document(body, chunk_tokens=cap_tokens)
    assert len(chunks) > 1
    assert all(len(c) <= cap_tokens * chunk.CHARS_PER_TOKEN for _ix, c in chunks)


def test_chunk_no_headings_recursive_fallback():
    # No headings → recursive packing, never one giant chunk.
    body = "\n\n".join(f"line {i} with filler" for i in range(60))
    chunks = chunk.chunk_document(body, chunk_tokens=50)
    assert len(chunks) > 1
    assert all(len(c) <= 50 * chunk.CHARS_PER_TOKEN for _ix, c in chunks)


def test_chunk_fenced_code_comments_are_not_headings():
    # `#` comment lines inside a ```/~~~ fence are code, not section boundaries —
    # the splitter used to cut fenced blocks apart at them (finding #383).
    doc = (
        "# Real heading\n\nintro prose\n\n"
        "```python\n# not a heading\nx = 1\n## also not\n```\n\n"
        "tail prose\n\n## Second heading\n\nmore\n"
    )
    chunks = [c for _ix, c in chunk.chunk_document(doc, 512)]
    # the fenced block survives intact, inside exactly one chunk
    assert sum("# not a heading" in c and "## also not" in c for c in chunks) == 1
    # heading detection resumed after the closing fence
    assert any(c.startswith("## Second heading") for c in chunks)
    # and the fake headings never started a section
    assert not any(
        c.startswith("# not a heading") or c.startswith("## also not") for c in chunks
    )


def test_chunk_never_exceeds_cap():
    # The reproduced overshoot: a re-seeded chunk (overlap tail + next piece) must NOT
    # exceed the cap, or a model at exactly its context limit truncates the tail.
    cap = 50
    limit = cap * chunk.CHARS_PER_TOKEN  # 200 chars
    # ~180-char paragraphs: the case that produced a 211-char chunk before the fix.
    body = "\n\n".join("word " * 36 for _ in range(40))
    for doc in (body, "# H\n\n" + body, "pre\n\n## S\n\n" + body):
        chunks = chunk.chunk_document(doc, chunk_tokens=cap, overlap=0.15)
        assert chunks, doc[:20]
        assert all(len(c) <= limit for _ix, c in chunks), max(
            len(c) for _ix, c in chunks
        )


# Fake, dependency-free embeddings so the vector store / KNN / fusion are testable
# without downloading a real model. A 5-word vocab; a text -> normalized count vector.
_VOCAB = ["alpha", "beta", "gamma", "fox", "hound"]


def _fake_embed(config, texts):
    import numpy as np

    out = []
    for t in texts:
        tl = t.lower()
        v = np.array([float(tl.count(w)) for w in _VOCAB], dtype="float32")
        if v.sum() == 0:
            v = np.ones(len(_VOCAB), dtype="float32")
        n = float(np.linalg.norm(v))
        out.append(v / n if n else v)
    return out


def _force_blob(monkeypatch):
    # Exercise the portable path everywhere: pretend the extra is installed, force the
    # numpy blob backend (no sqlite-vec), and swap in the fake embedder.
    monkeypatch.setattr(semantic, "available", lambda *a, **k: True)
    monkeypatch.setattr(semantic, "active_path", lambda *a, **k: "blob")
    monkeypatch.setattr(semantic, "_embed_texts", _fake_embed)


class _FakeResp:
    def __init__(self, body):
        self._b = body

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen(req, timeout=None):
    # An OpenAI-compatible /v1/embeddings server, faked: read the request's `input`,
    # return one deterministic vector per text (same _VOCAB scheme as _fake_embed).
    payload = json.loads(req.data.decode())
    data = []
    for i, t in enumerate(payload["input"]):
        tl = t.lower()
        vec = [float(tl.count(w)) for w in _VOCAB]
        if sum(vec) == 0:
            vec = [1.0] * len(_VOCAB)
        data.append({"index": i, "embedding": vec})
    return _FakeResp(json.dumps({"data": data}).encode())


def test_http_provider_embeds_via_endpoint(tmp_path, monkeypatch):
    # provider="http" routes embedding to an OpenAI-compatible endpoint (mocked), with
    # NO fastembed. Force the blob store + swap urlopen; the real _embed_http dispatch runs.
    import urllib.request

    monkeypatch.setattr(semantic, "active_path", lambda *a, **k: "blob")
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    _root, cfg = _repo(
        tmp_path,
        '[semantic]\nprovider = "http"\n'
        'endpoint = "http://localhost:11434/v1/embeddings"\nmodel = "m"\n',
    )
    con = sqlite3.connect(tmp_path / "http.db")
    semantic.ensure_schema(con, cfg)
    semantic.embed_doc(con, "alpha.md", "alpha alpha fox", cfg)
    semantic.embed_doc(con, "beta.md", "beta hound", cfg)
    con.commit()
    hits = semantic.knn(con, "alpha", 5, cfg)
    assert hits and hits[0][0] == "alpha.md"  # http-embedded vectors retrieve correctly


def test_http_provider_available_needs_endpoint(monkeypatch):
    # available() is provider-aware: http needs an endpoint, and the check must not
    # require fastembed. Drop the autouse off-switch to exercise the real function.
    monkeypatch.undo()
    try:
        import numpy  # noqa: F401
    except Exception:
        pytest.skip("numpy not installed")
    cfg_no = {"semantic": {"provider": "http", "endpoint": ""}}
    cfg_yes = {"semantic": {"provider": "http", "endpoint": "http://x/v1/embeddings"}}
    assert semantic.available(cfg_no) is False
    assert semantic.available(cfg_yes) is True


# ── read-path containment (a semantic-tier failure must NOT crash find/build) ──
def test_embed_http_malformed_raises(monkeypatch):
    import urllib.request

    cfg = {
        "semantic": {
            "provider": "http",
            "endpoint": "http://x/v1/embeddings",
            "model": "m",
            "api_key_env": "",
        }
    }
    # a 200 with a body missing "data"/"embedding" must raise EmbeddingError, not KeyError
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda req, timeout=None: _FakeResp(b'{"oops": 1}')
    )
    with pytest.raises(semantic.EmbeddingError):
        semantic._embed_http(cfg, ["hello"])


def test_knn_empty_query_batch_returns_empty(tmp_path, monkeypatch):
    _force_blob(monkeypatch)
    monkeypatch.setattr(
        semantic, "_embed_texts", lambda *a, **k: []
    )  # embed yields nothing
    _root, cfg = _repo(tmp_path, "")
    con = sqlite3.connect(tmp_path / "e.db")
    semantic.ensure_schema(con, cfg)
    con.execute("INSERT INTO chunks(relpath, chunk_ix, text) VALUES ('a.md',0,'x')")
    con.commit()
    assert semantic.knn(con, "anything", 5, cfg) == []  # no dense hits, not IndexError


def test_build_survives_embed_failure(tmp_path, monkeypatch, capsys):
    _force_blob(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("endpoint down")

    monkeypatch.setattr(semantic, "embed_doc", _boom)
    _root, cfg = _repo(
        tmp_path, "", {"a.md": "# A\n\nalpha\n", "b.md": "# B\n\nbeta\n"}
    )
    index.build(tmp_path, cfg)  # must NOT raise
    con = sqlite3.connect(cfg["index_path"])
    rels = {r[0] for r in con.execute("SELECT relpath FROM docs WHERE kind='md'")}
    con.close()
    assert {"a.md", "b.md"} <= rels  # docs still lexically indexed
    assert "embed skipped" in capsys.readouterr().err


def test_cmd_find_degrades_on_refresh_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(root, "find_root", lambda *a, **k: tmp_path)
    _repo(tmp_path, "", {"a.md": "# A\n\nalpha content\n"})
    index.build(tmp_path, root.load_config(tmp_path))  # an index already exists
    monkeypatch.setattr(
        find,
        "ensure_fresh",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("locked")),
    )
    rc = cli.main(["find", "alpha"])  # refresh raises → must still search, not crash
    out = capsys.readouterr()
    assert rc == 0 and "a.md" in out.out
    assert "refresh failed" in out.err


def test_vec0_backend_roundtrip(tmp_path, monkeypatch):
    # The SHIPPED-DEFAULT path (sqlite-vec vec0) — skipped cleanly where the extension
    # isn't installed (CI), exercised for real where it is. Fake vectors (no model), so
    # it needs only sqlite-vec + numpy: force available() True and let active_path do
    # the REAL vec0 storage probe.
    pytest.importorskip("sqlite_vec")
    pytest.importorskip("numpy")
    monkeypatch.setattr(semantic, "available", lambda *a, **k: True)
    monkeypatch.setattr(semantic, "_embed_texts", _fake_embed)
    if semantic.active_path() != "vec0":
        pytest.skip("sqlite3 build can't load extensions here")
    # dims=5 so the real vec0 table (declared float[dims]) matches _fake_embed's 5-dim vectors
    _root, cfg = _repo(tmp_path, "[semantic]\ndims = 5\n")
    con = sqlite3.connect(tmp_path / "vec0.db")
    semantic.ensure_schema(con, cfg)  # creates the real vec0 virtual table
    semantic.embed_doc(con, "alpha.md", "alpha alpha fox", cfg)
    semantic.embed_doc(con, "beta.md", "beta hound", cfg)
    con.commit()
    hits = semantic.knn(
        con, "alpha", 5, cfg
    )  # MATCH ... ORDER BY distance + rowid join
    assert hits and hits[0][0] == "alpha.md"
    semantic.delete_doc(con, "alpha.md")  # cascade to vec_chunks
    con.commit()
    assert "alpha.md" not in {h[0] for h in semantic.knn(con, "alpha", 5, cfg)}


def test_semantic_blob_roundtrip_rollup_and_delete(tmp_path, monkeypatch):
    _force_blob(monkeypatch)
    _root, cfg = _repo(tmp_path, "")
    con = sqlite3.connect(tmp_path / "vec.db")
    semantic.ensure_schema(con, cfg)
    semantic.embed_doc(con, "alpha.md", "alpha alpha fox\n\nmore alpha here", cfg)
    semantic.embed_doc(con, "beta.md", "beta hound content", cfg)
    con.commit()
    hits = semantic.knn(con, "alpha", 5, cfg)
    assert hits and hits[0][0] == "alpha.md"  # best-chunk rollup ranks the alpha doc
    assert "alpha" in hits[0][2]  # the winning chunk's TEXT rides along (for snippets)
    assert len({h[0] for h in hits}) == len(hits)  # per-DOC (rolled up), no dup docs
    semantic.delete_doc(con, "alpha.md")
    con.commit()
    assert "alpha.md" not in {h[0] for h in semantic.knn(con, "alpha", 5, cfg)}


def test_index_embeds_and_cascade_deletes_chunks(tmp_path, monkeypatch):
    _force_blob(monkeypatch)
    _root, cfg = _repo(tmp_path, "", {"a.md": "# a\n\nalpha fox\n"})
    index.build(tmp_path, cfg)
    con = sqlite3.connect(cfg["index_path"])
    assert (
        con.execute("SELECT count(*) FROM chunks WHERE relpath='a.md'").fetchone()[0]
        >= 1
    )
    con.close()
    (tmp_path / "a.md").unlink()
    index.build_incremental(tmp_path, cfg)  # reconcile-delete must cascade to chunks
    con = sqlite3.connect(cfg["index_path"])
    assert (
        con.execute("SELECT count(*) FROM chunks WHERE relpath='a.md'").fetchone()[0]
        == 0
    )
    con.close()


def test_available_does_not_import_fastembed():
    # The eager-import fix: available() answers via importlib.find_spec, never
    # `import fastembed` — so `find`'s per-search refresh doesn't pay a ~0.3s ONNX
    # import for a boolean. Run in a fresh subprocess so sys.modules is clean
    # regardless of other tests' real-embed paths. Invariant holds whether or not
    # fastembed is installed (find_spec never executes the module).
    code = (
        "import sys; from repolens import semantic;"
        "semantic.available({'semantic': {'enabled': True}});"
        "assert 'fastembed' not in sys.modules, 'available() imported fastembed';"
        "print('OK')"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "OK" in out.stdout


def test_build_incremental_releases_lock_on_error(tmp_path, monkeypatch):
    # A mid-transaction failure must roll back + close the write connection, not leak
    # the WAL write lock — else concurrent sessions' refreshes would hang. After a
    # forced error, a fresh build_incremental on the same index must succeed.
    _root, cfg = _repo(tmp_path, "", {"a.md": "# A\n\none\n"})
    index.build(tmp_path, cfg)
    (tmp_path / "b.md").write_text("# B\n\ntwo\n")  # a change to force an insert
    real_insert = index._insert_doc

    def _boom(*a, **k):
        raise RuntimeError("simulated disk-full mid-transaction")

    monkeypatch.setattr(index, "_insert_doc", _boom)
    with pytest.raises(RuntimeError):
        index.build_incremental(tmp_path, cfg)
    monkeypatch.setattr(index, "_insert_doc", real_insert)  # restore
    # The lock must have been released — a subsequent incremental succeeds and indexes b.
    changed, _deleted, _ms = index.build_incremental(tmp_path, cfg)
    assert changed >= 1
    con = sqlite3.connect(cfg["index_path"])
    assert (
        con.execute("SELECT count(*) FROM docs WHERE relpath='b.md'").fetchone()[0] == 1
    )
    con.close()


def test_cache_dir_is_durable_and_env_overridable(monkeypatch, tmp_path):
    # Fix #4: the model cache must NOT land in the OS-purgeable temp dir (fastembed's
    # default), and REPOLENS_CACHE_DIR must override it.
    import tempfile

    monkeypatch.delenv("REPOLENS_CACHE_DIR", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    d = semantic._cache_dir()
    assert tempfile.gettempdir() not in d  # not the purgeable temp dir
    assert d.endswith(os.path.join("repolens", "fastembed"))
    monkeypatch.setenv("REPOLENS_CACHE_DIR", str(tmp_path / "mycache"))
    assert semantic._cache_dir() == str(tmp_path / "mycache")


def test_incremental_backfills_when_semantic_enabled(tmp_path, monkeypatch):
    # #1: an index built lexical-first, then upgraded to semantic, must BACKFILL
    # embeddings on the next incremental — not silently stay lexical while reporting
    # hybrid. The embed-sig mismatch ("" -> "model:dims") forces a full rebuild.
    _root, cfg = _repo(tmp_path, "", {"a.md": "# a\n\nalpha fox\n"})
    index.build(tmp_path, cfg)  # semantic OFF (autouse fixture) → no chunk table
    con = sqlite3.connect(cfg["index_path"])
    tables = {
        r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    con.close()
    assert "chunks" not in tables  # a lexical index has no vectors
    _force_blob(monkeypatch)  # now semantic is available (fake embedder)
    index.build_incremental(tmp_path, cfg)  # detects the embed-sig mismatch → backfill
    con = sqlite3.connect(cfg["index_path"])
    n = con.execute("SELECT count(*) FROM chunks").fetchone()[0]
    con.close()
    assert n > 0  # embeddings were backfilled, not skipped


def test_model_load_failure_memoized_no_retry_storm(monkeypatch):
    # #2: a failing fastembed load sets the sentinel + raises; a SECOND call short-circuits
    # without re-attempting the load (no per-doc retry storm on an offline/cold-cache box).
    fastembed = pytest.importorskip("fastembed")
    monkeypatch.setattr(semantic, "_MODEL_LOAD_FAILED", False)
    monkeypatch.setattr(semantic, "_MODELS", {})
    calls = {"n": 0}

    class _BoomModel:
        def __init__(self, *a, **k):
            calls["n"] += 1
            raise RuntimeError("cannot load model")

    monkeypatch.setattr(fastembed, "TextEmbedding", _BoomModel)
    cfg = {"semantic": {"enabled": True, "model": "m", "dims": 5, "threads": 0}}
    with pytest.raises(semantic.EmbeddingError):
        semantic._model(cfg)
    with pytest.raises(semantic.EmbeddingError):
        semantic._model(cfg)  # second call must NOT retry the load
    assert semantic._MODEL_LOAD_FAILED is True
    assert calls["n"] == 1  # loaded once, then memoized → no storm


def test_noop_incremental_takes_no_write_lock(tmp_path):
    # #8: a no-op incremental must NOT take the write lock (else it serializes concurrent
    # find-refreshers). Hold the single WAL write lock on another connection; a no-op
    # refresh must still complete rather than block on it.
    import threading

    _root, cfg = _repo(tmp_path, "", {"a.md": "# a\n\nx\n"})
    index.build(tmp_path, cfg)
    c = sqlite3.connect(cfg["index_path"])
    c.execute("PRAGMA journal_mode=WAL")  # persist WAL so reader+writer coexist
    c.close()
    blocker = sqlite3.connect(cfg["index_path"])
    blocker.execute("PRAGMA journal_mode=WAL")
    blocker.execute("BEGIN IMMEDIATE")  # hold the write lock
    out: list = []
    t = threading.Thread(
        target=lambda: out.append(index.build_incremental(tmp_path, cfg)), daemon=True
    )
    t.start()
    t.join(timeout=5)
    blocker.rollback()
    blocker.close()
    assert not t.is_alive(), "no-op refresh blocked on a held write lock"
    assert out and out[0][:2] == (0, 0)


def test_cmd_index_threads_flag_overrides(tmp_path, monkeypatch):
    # #7: --threads overrides [semantic].threads for the build.
    _root, cfg = _repo(tmp_path, "", {"a.md": "# a\n\nx\n"})
    index.build(tmp_path, cfg)
    captured = {}

    def _fake_bi(root_, config):
        captured["threads"] = config["semantic"]["threads"]
        return (0, 0, 0.0)

    monkeypatch.setattr(root, "find_root", lambda *a, **k: tmp_path)
    monkeypatch.setattr(index, "build_incremental", _fake_bi)
    cli.main(["index", "--threads", "0"])
    assert captured["threads"] == 0


def test_legacy_repometa_config_read_with_warning(tmp_path, monkeypatch, capsys):
    # #4: a pre-0.11 .repometa.toml (no .repolens.toml) is still read, with a one-time
    # deprecation warning — so an un-migrated repo keeps its config, not defaults.
    monkeypatch.setattr(root, "_LEGACY_WARNED", False)
    (tmp_path / ".repometa.toml").write_text("[repolens]\nmax_file_bytes = 4242\n")
    cfg = root.load_config(tmp_path)
    assert cfg["max_file_bytes"] == 4242  # legacy config honored
    assert "deprecated" in capsys.readouterr().err.lower()


def test_hybrid_find_fuses_lexical_and_dense(tmp_path, monkeypatch):
    _force_blob(monkeypatch)
    _root, cfg = _repo(
        tmp_path,
        "",
        {"alpha.md": "# alpha\n\nalpha fox\n", "beta.md": "# beta\n\nbeta hound\n"},
    )
    index.build(tmp_path, cfg)
    hits = find.search(cfg, "alpha", 5)
    assert hits[0]["relpath"] == "alpha.md"  # BM25 + dense both favor it → RRF top


def test_code_purpose_line_is_embedded(tmp_path, monkeypatch):
    # B2: a code file's purpose-line must be embedded into the dense index (was md-only,
    # so code got 0 vectors and hybrid demoted code hits). A file with no extractable
    # purpose-line gets no vector (the `if pl` guard).
    _force_blob(monkeypatch)
    _root, cfg = _repo(
        tmp_path,
        "",
        {
            # multi-line docstring: sentence one is the title, the LATER lines must
            # still reach the body + the embedder (docstring indexing).
            "vec.py": (
                '"""Alpha lookup.\n\nDeeper prose: the gamma rollup packs fox '
                'vectors.\n"""\n\ndef f():\n    pass\n'
            ),
            "bare.py": "def g():\n    pass\n",  # no comment/docstring => empty purpose
            "notes.md": "# notes\n\nbeta hound\n",
        },
    )
    index.build(tmp_path, cfg)
    con = sqlite3.connect(cfg["index_path"])
    try:
        counts = dict(
            con.execute(
                "SELECT relpath, COUNT(*) FROM chunks GROUP BY relpath"
            ).fetchall()
        )
        body = con.execute("SELECT body FROM docs WHERE relpath='vec.py'").fetchone()[0]
        chunk_text = " ".join(
            t for (t,) in con.execute("SELECT text FROM chunks WHERE relpath='vec.py'")
        )
        title = con.execute("SELECT title FROM docs WHERE relpath='vec.py'").fetchone()[
            0
        ]
    finally:
        con.close()
    assert counts.get("vec.py", 0) >= 1  # code got a vector (was 0 pre-B2)
    assert counts.get("bare.py", 0) == 0  # nothing extractable => no dense vector
    assert "gamma rollup" in body  # later-docstring text is BM25-searchable
    assert "gamma rollup" in chunk_text  # ...and embedded
    assert title == "Alpha lookup."  # display line stays the one-sentence purpose
    # a query on later-docstring vocabulary finds the code file
    assert any(h["relpath"] == "vec.py" for h in find.search(cfg, "gamma rollup", 5))


def test_find_lexical_only_skips_dense(tmp_path, monkeypatch):
    _force_blob(monkeypatch)
    _root, cfg = _repo(tmp_path, "", {"a.md": "# a\n\nalpha fox\n"})
    index.build(tmp_path, cfg)
    called = []
    monkeypatch.setattr(semantic, "knn", lambda *a, **k: called.append(1) or [])
    hits = find.search(cfg, "alpha", 5, lexical_only=True)
    assert hits and hits[0]["relpath"] == "a.md"
    assert not called  # the dense half was never consulted


def test_find_degrades_when_query_embed_fails(tmp_path, monkeypatch):
    # available() passes at pre-flight but the actual query embed raises (e.g. a down
    # http endpoint): find must degrade to lexical for this search, not crash.
    _force_blob(monkeypatch)
    _root, cfg = _repo(tmp_path, "", {"alpha.md": "# alpha\n\nalpha fox\n"})
    index.build(tmp_path, cfg)

    def _boom(*a, **k):
        raise semantic.EmbeddingError("endpoint down")

    monkeypatch.setattr(semantic, "knn", _boom)
    hits = find.search(cfg, "alpha", 5)  # must not raise
    assert hits and hits[0]["relpath"] == "alpha.md"


def test_rrf_scores_fuse_and_rank():
    # a doc ranked well by EITHER list should rank up; a doc in both wins.
    scores = find._rrf_scores(["a.md", "b.md", "c.md"], ["c.md", "a.md"])
    assert scores["a.md"] > scores["b.md"]  # a is in both, b in one
    assert scores["c.md"] > scores["b.md"]  # c is high in dense + in lexical


# ── bench: the committed gold-set scorer (hybrid vs lexical) ───────


def test_bench_metric_primitives():
    hits = [{"relpath": "a.md"}, {"relpath": "b.md"}, {"relpath": "c.md"}]
    assert bench.rank_of_gold(hits, ["b.md"]) == 2
    assert bench.rank_of_gold(hits, ["z.md"]) is None
    assert bench.rank_of_gold(hits, ["c.md", "a.md"]) == 1  # first gold hit wins
    assert bench.recall_at_k(2, 5) and not bench.recall_at_k(None, 5)
    assert not bench.recall_at_k(6, 5)
    assert bench.reciprocal_rank(4) == 0.25 and bench.reciprocal_rank(None) == 0.0


def test_bench_load_gold_validates(tmp_path):
    p = tmp_path / "gold.jsonl"
    p.write_text(
        '{"query": "q", "gold": "a.md"}\n'
        "\n"
        '{"query": "r", "gold": ["b.md"], "class": "exact"}\n'
    )
    gold = bench.load_gold(p)
    assert gold[0]["gold"] == ["a.md"]  # bare string is wrapped
    assert gold[0]["class"] == "conceptual"  # default class
    assert gold[1]["class"] == "exact"
    p.write_text('{"query": "q"}\n')  # missing gold
    with pytest.raises(ValueError):
        bench.load_gold(p)


def test_bench_run_scores_both_modes(tmp_path, monkeypatch):
    _force_blob(monkeypatch)
    _root, cfg = _repo(
        tmp_path,
        "",
        {"alpha.md": "# alpha\n\nalpha fox\n", "beta.md": "# beta\n\nbeta hound\n"},
    )
    index.build(tmp_path, cfg)
    gold = [
        {"query": "alpha", "gold": ["alpha.md"], "class": "exact"},
        {"query": "hound", "gold": ["beta.md"], "class": "conceptual"},
    ]
    result = bench.run(cfg, gold, k=5)
    assert result["n"] == 2 and result["semantic_active"]
    o = result["overall"]
    assert o["lexical"]["recall"] == 1.0 and o["hybrid"]["recall"] == 1.0
    assert o["hybrid"]["mrr"] == 1.0  # both golds rank #1 in hybrid
    assert "grep" in o  # the grep baseline arm is scored too
    assert set(result["classes"]) == {"exact", "conceptual"}
    report = bench.format_report(result)
    assert "recall@5" in report and "overall" in report


def test_bench_grep_arm(tmp_path, monkeypatch):
    # The grep baseline is literal: it finds a query whose words appear verbatim and
    # misses one that doesn't — scored identically to find, present in every result slice.
    _force_blob(monkeypatch)
    _root, cfg = _repo(
        tmp_path,
        "",
        {
            "alpha.md": "# alpha\n\nalpha fox trots\n",
            "beta.md": "# beta\n\nbeta hound\n",
        },
    )
    index.build(tmp_path, cfg)
    gold = [
        {
            "query": "alpha fox",
            "gold": ["alpha.md"],
            "class": "exact",
        },  # literal → found
        {
            "query": "zzznope",
            "gold": ["beta.md"],
            "class": "paraphrase",
        },  # absent → miss
    ]
    result = bench.run(cfg, gold, k=5)
    assert "grep" in result["overall"]
    assert all("grep" in slot for slot in result["classes"].values())
    grep_rank = {p["query"]: p["grep_rank"] for p in result["per_query"]}
    assert grep_rank["alpha fox"] == 1  # both terms in alpha.md → ranked #1
    assert grep_rank["zzznope"] is None  # literal grep can't find an absent term
    report = bench.format_report(result)
    assert "grep R@k" in report and "hyb R@k" in report


def test_log_event_writes_valid_jsonl_when_enabled(tmp_path):
    _root, cfg = _repo(tmp_path, "[log]\nenabled = true\n")
    log.event(cfg, "find", query="hello", mode="hybrid", n_hits=2)
    logpath = cfg["index_path"].parent / "events.jsonl"
    assert logpath.exists()
    rec = json.loads(logpath.read_text().strip())
    assert rec["type"] == "find" and rec["query"] == "hello" and rec["n_hits"] == 2
    assert "ts" in rec  # ISO-local timestamp present


def test_log_event_noop_when_disabled(tmp_path):
    _root, cfg = _repo(tmp_path, "")  # no [log] block → default off
    assert cfg["log"]["enabled"] is False
    log.event(cfg, "find", query="x")
    assert not (cfg["index_path"].parent / "events.jsonl").exists()


def test_log_event_never_raises_on_bad_path(tmp_path):
    _root, cfg = _repo(tmp_path, "[log]\nenabled = true\n")
    blocker = tmp_path / "blocker"
    blocker.write_text("x")  # a FILE where the log dir would need to be
    cfg["index_path"] = blocker / "sub" / "index.db"  # parent mkdir must fail
    log.event(cfg, "embed", relpath="a.md")  # swallowed — must not raise


def test_cmd_find_logs_the_query(tmp_path, monkeypatch):
    _root, cfg = _repo(
        tmp_path, "[log]\nenabled = true\n", {"a.md": "# A\n\nhello world\n"}
    )
    index.build(tmp_path, cfg)
    monkeypatch.setattr(root, "find_root", lambda *a, **k: tmp_path)
    cli.main(["find", "hello"])
    events = [
        json.loads(line)
        for line in (tmp_path / ".repolens" / "events.jsonl").read_text().splitlines()
    ]
    finds = [e for e in events if e["type"] == "find"]
    assert finds and finds[-1]["query"] == "hello"
