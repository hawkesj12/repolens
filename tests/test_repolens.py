"""repolens test suite — core engine + the fixes it was hardened with."""

from __future__ import annotations

import json
import os
import sqlite3

from repolens import (
    cli,
    digest,
    discover,
    env,
    find,
    hookgen,
    index,
    lint,
    purpose,
    root,
    schema,
)


def _mkdb(path, tables):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    for t in tables:
        con.execute(f"CREATE TABLE {t} (id, val)")
    con.commit()
    con.close()


def _repo(tmp_path, toml="", files=None):
    (tmp_path / ".repometa.toml").write_text(toml)
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
    (tmp_path / ".repometa.toml").write_text("")
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert root.find_root(sub) == tmp_path.resolve()


def test_load_config_defaults_and_parse(tmp_path):
    _root, cfg = _repo(tmp_path, TYPES_TOML)
    assert cfg["index_path"] == tmp_path / ".repometa/index.db"
    assert ".git" in cfg["skip_dirs"]  # defaults present
    assert cfg["types"]["note"]["recursive"] is True
    assert cfg["types"]["note"]["require"] == ["^\\*\\*Date:\\*\\*"]
    assert cfg["sqlite_paths"] == []  # integration off by default


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


# ── index build ────────────────────────────────────────────────
def test_build_atomic_single_docs_table(tmp_path):
    _root, cfg = _repo(
        tmp_path, "", {"a.md": "# A\n\nhello world\n", "s.py": '"""does a thing."""\n'}
    )
    n, code, tables, ms = index.build(tmp_path, cfg)
    assert n >= 1 and code >= 1
    assert not list((tmp_path / ".repometa").glob("*.tmp-*"))
    con = sqlite3.connect(cfg["index_path"])
    names = {
        r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "docs" in names and "meta" not in names
    kinds = {r[0] for r in con.execute("SELECT DISTINCT kind FROM docs")}
    assert "md" in kinds and "code" in kinds
    con.close()


def test_corpus_newer_than_watches_code(tmp_path, monkeypatch):
    _root, cfg = _repo(tmp_path, "", {"s.py": '"""x."""\n'})
    codefile = tmp_path / "s.py"
    monkeypatch.setattr(
        index, "_walk", lambda r, c, code: iter([codefile]) if code else iter([])
    )
    assert index.corpus_newer_than(tmp_path, cfg, 0) is True
    monkeypatch.setattr(index, "_walk", lambda r, c, code: iter([]))
    assert index.corpus_newer_than(tmp_path, cfg, 0) is False


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
    cfg_text = (tmp_path / ".repometa.toml").read_text()
    assert 'paths = ["data/app.db"]' in cfg_text
    assert root.load_config(tmp_path)["sqlite_paths"] == [tmp_path / "data/app.db"]
    assert "found data/app.db (2 tables)" in capsys.readouterr().out


def test_cmd_init_no_db_skips_discovery(tmp_path, monkeypatch):
    monkeypatch.setattr(root, "find_root", lambda *a, **k: tmp_path)
    _mkdb(tmp_path / "app.db", ["t"])
    cli.main(["init", "--no-db"])
    # no ACTIVE block appended (the template's own block is commented: "# [")
    assert "\n[integrations.sqlite]" not in (tmp_path / ".repometa.toml").read_text()
    assert root.load_config(tmp_path)["sqlite_paths"] == []


def test_cmd_init_existing_config_no_append(tmp_path, monkeypatch):
    monkeypatch.setattr(root, "find_root", lambda *a, **k: tmp_path)
    (tmp_path / ".repometa.toml").write_text("[repolens]\n")
    _mkdb(tmp_path / "app.db", ["t"])
    cli.main(["init"])  # config exists, no --force → no discovery/append
    assert "[integrations.sqlite]" not in (tmp_path / ".repometa.toml").read_text()


# ── digest ─────────────────────────────────────────────────────
def test_digest_compact_reads_index_has_pointer(tmp_path):
    _root, cfg = _repo(
        tmp_path,
        '[integrations.sqlite]\npaths = ["d.db"]\n',
        {
            "README.md": "# myrepo\n\nThe purpose line.\n",
            "scripts/a.py": '"""does a thing."""\n',
            "scripts/b.py": '"""another."""\n',
        },
    )
    _mkdb(tmp_path / "d.db", ["trades", "accounts"])
    out = digest.build_digest(tmp_path, cfg, max_lines=12)
    lines = out.splitlines()
    assert len(lines) <= 12
    assert lines[0].startswith("[repolens] myrepo") and "The purpose line." in lines[0]
    assert any("indexed:" in ln for ln in lines)
    assert "scripts/ (2)" in out
    assert "trades" in out and "accounts" in out
    assert lines[-1].startswith("→")  # routing pointer is always last
    assert "does a thing" not in out  # no file BODIES leak into the digest


def test_digest_respects_max_lines(tmp_path):
    files = {"README.md": "# r\n\np\n"}
    for i in range(10):
        files[f"dir{i}/f.py"] = '"""x."""\n'
    _root, cfg = _repo(tmp_path, "", files)
    out = digest.build_digest(tmp_path, cfg, max_lines=5)
    assert len(out.splitlines()) <= 5
    assert out.splitlines()[-1].startswith("→")


# ── env ────────────────────────────────────────────────────────
def test_env_probe_present_only_with_version(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "faketool").write_text("#!/bin/sh\necho 'faketool 9.9.9'\n")
    (bindir / "faketool").chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    _root, cfg = _repo(tmp_path, '[env]\ntools = ["faketool", "not-a-real-tool-xyz"]\n')
    out = env.probe_env(cfg)
    assert out.startswith("[env]")
    assert "faketool 9.9.9" in out  # present, version parsed
    assert "not-a-real-tool-xyz" not in out  # absent omitted


def test_env_present_without_version_on_probe_failure(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "novertool").write_text("#!/bin/sh\nexit 1\n")  # --version fails
    (bindir / "novertool").chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    _root, cfg = _repo(tmp_path, '[env]\ntools = ["novertool"]\n')
    assert "novertool" in env.probe_env(cfg)  # present even though version probe failed


def test_env_detect_stack(tmp_path):
    assert env.detect_stack(tmp_path) == ["git"]
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "package.json").write_text("{}")
    stack = env.detect_stack(tmp_path)
    assert stack[0] == "git" and "python" in stack and "node" in stack


def test_load_config_env_tools_default_and_override(tmp_path):
    _root, cfg = _repo(tmp_path, "")
    assert cfg["env_tools"] == ["git", "python", "node"]
    _root, cfg = _repo(tmp_path, '[env]\ntools = ["go", "rust"]\n')
    assert cfg["env_tools"] == ["go", "rust"]


# ── hook (NON-DESTRUCTIVE) ─────────────────────────────────────
def test_hook_snippet_is_valid_json(tmp_path):
    frag = hookgen.snippet(with_env=False)
    obj = json.loads(frag[frag.index("{") :])
    assert obj["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "repolens digest"


def test_hook_check_writes_nothing(tmp_path):
    hookgen.install(tmp_path, check=True)
    assert not (tmp_path / ".claude" / "settings.json").exists()


def test_hook_install_additive_preserves_existing_and_idempotent(tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": "my-existing.sh"}]}
                    ]
                },
                "otherKey": 1,
            }
        )
    )
    hookgen.install(tmp_path)
    data = json.loads(settings.read_text())
    cmds = [h["command"] for g in data["hooks"]["SessionStart"] for h in g["hooks"]]
    assert "my-existing.sh" in cmds  # existing hook PRESERVED
    assert "repolens digest" in cmds  # ours added
    assert data["otherKey"] == 1  # other keys untouched
    hookgen.install(tmp_path)  # idempotent
    data2 = json.loads(settings.read_text())
    cmds2 = [h["command"] for g in data2["hooks"]["SessionStart"] for h in g["hooks"]]
    assert cmds2.count("repolens digest") == 1


def test_cmd_init_seeds_env_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(root, "find_root", lambda *a, **k: tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    cli.main(["init"])
    cfg_text = (tmp_path / ".repometa.toml").read_text()
    assert "[env]" in cfg_text and '"python"' in cfg_text
    assert root.load_config(tmp_path)["env_tools"] == ["git", "python"]
