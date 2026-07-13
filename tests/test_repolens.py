"""repolens test suite — core engine + the fixes it was hardened with."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess

from repolens import (
    cli,
    digest,
    discover,
    enrich,
    env,
    find,
    frontmatter,
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
    # v0.5 schema: docs + the incremental/frontmatter bookkeeping tables
    assert {"docs", "files", "frontmatter", "meta"} <= names
    assert not list((tmp_path / ".repometa").glob("*.tmp-*"))  # temp swapped away
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


def test_env_version_regex_handles_v_prefix_and_trailing_hash():
    # Real --version strings that a `\b\d` anchor mis-parsed: a `v` prefix has no
    # word boundary before the digits (grabbed a later token), and a build hash
    # after the version must not win.
    cases = {
        "v25.8.0": "25.8.0",  # node — was mis-parsed as "8.0"
        "v1.5.4 (Variegata) 08e34c447b": "1.5.4",  # duckdb — was "5.4"
        "ripgrep 14.1.1 (rev 63bb0ca0da)": "14.1.1",
        "git version 2.50.1": "2.50.1",
        "Python 3.12.7": "3.12.7",
        "jq-1.6": "1.6",
    }
    for raw, want in cases.items():
        m = env._VERSION_RE.search(raw)
        assert m and m.group(1) == want, f"{raw!r} → {m and m.group(1)} (want {want})"


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


def test_cmd_init_installs_session_hook_when_claude_repo(tmp_path, monkeypatch):
    # A Claude Code repo (has .claude/) → init wires the SessionStart hook by
    # DEFAULT, ADDITIVELY: an existing unrelated hook survives ("even if there
    # are some"), and ours runs digest AND env.
    monkeypatch.setattr(root, "find_root", lambda *a, **k: tmp_path)
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": "memory.sh"}]}
                    ]
                }
            }
        )
    )
    cli.main(["init"])
    data = json.loads(settings.read_text())
    cmds = [h["command"] for g in data["hooks"]["SessionStart"] for h in g["hooks"]]
    assert "memory.sh" in cmds  # existing hook PRESERVED, not clobbered
    assert "repolens digest && repolens env" in cmds  # ours, WITH env by default


def test_cmd_init_no_hook_skips_session_hook(tmp_path, monkeypatch):
    monkeypatch.setattr(root, "find_root", lambda *a, **k: tmp_path)
    (tmp_path / ".claude").mkdir()
    cli.main(["init", "--no-hook"])
    assert not (tmp_path / ".claude" / "settings.json").exists()


def test_cmd_init_no_claude_dir_never_presumes_harness(tmp_path, monkeypatch, capsys):
    # Not a Claude Code repo (no .claude/) → init writes no agent config.
    monkeypatch.setattr(root, "find_root", lambda *a, **k: tmp_path)
    cli.main(["init"])
    assert not (tmp_path / ".claude" / "settings.json").exists()
    assert "no .claude/" in capsys.readouterr().out


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
def test_digest_folders_have_purpose(tmp_path):
    _root, cfg = _repo(
        tmp_path,
        "",
        {
            "README.md": "# myrepo\n\nthe repo purpose.\n",
            "money/a.md": "---\ndescription: tracks the budget\n---\n# Budget\n\nx\n",
            "money/b.md": "# B\n\ny\n",
        },
    )
    out = digest.build_digest(tmp_path, cfg)
    assert out.startswith("[repolens] myrepo")
    assert "money/" in out and "tracks the budget" in out  # folder purpose from EAV


def test_digest_tables_grouped_by_prefix_all(tmp_path):
    _mkdb(
        tmp_path / "d.db", ["fin_transactions", "fin_rules", "health_sleep", "accounts"]
    )
    _root, cfg = _repo(
        tmp_path, '[integrations.sqlite]\npaths = ["d.db"]\n', {"a.md": "# A\n\nx\n"}
    )
    out = digest.build_digest(tmp_path, cfg)
    assert "fin_* (2):" in out  # shared prefix grouped
    assert "fin_transactions" in out and "fin_rules" in out
    assert (
        "accounts" in out and "health_sleep" in out
    )  # unprefixed shown, not truncated


def test_digest_full_tier(tmp_path):
    _root, cfg = _repo(
        tmp_path,
        "",
        {"money/a.md": "---\ndescription: the budget doc\n---\n# A\n\nx\n"},
    )
    out = digest.build_digest(tmp_path, cfg, full=True)
    assert "money/a.md" in out and "the budget doc" in out  # per-doc note in --full


def test_digest_degrades_no_db_no_frontmatter(tmp_path):
    _root, cfg = _repo(
        tmp_path, "", {"a.md": "# A\n\nno frontmatter\n", "sub/b.md": "# B\n\nx\n"}
    )
    out = digest.build_digest(tmp_path, cfg)
    assert out.startswith("[repolens]")
    assert "database" not in out  # no DB → no DB section
    assert out.rstrip().endswith("rg")  # routing pointer still last


def test_cmd_index_rebuild_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(root, "find_root", lambda *a, **k: tmp_path)
    _repo(tmp_path, "", {"a.md": "# A\n\nx\n"})
    cli.main(["index", "--rebuild"])
    assert "built index" in capsys.readouterr().out


def test_cmd_digest_full_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(root, "find_root", lambda *a, **k: tmp_path)
    _repo(tmp_path, "", {"money/a.md": "---\ndescription: d\n---\n# A\n\nx\n"})
    cli.main(["digest", "--full"])
    assert "money/a.md" in capsys.readouterr().out


# ── enrich (local-model metadata generation; model call mocked) ─
def _fake_ask(config, prompt):
    if "DESCRIPTION" in prompt:
        return "DESCRIPTION: A test document about things.\nTAGS: Alpha, beta, gamma!"
    return "Does a test thing quickly."


def test_enrich_config_defaults(tmp_path):
    _root, cfg = _repo(tmp_path, "")
    en = cfg["enrich"]
    assert en["model"] == "llama3.2"
    assert "11434" in en["endpoint"]
    assert en["fields"] == ["description", "tags"]


def test_enrich_fills_description_and_tags(tmp_path, monkeypatch):
    monkeypatch.setattr(enrich, "_ask", _fake_ask)
    _root, cfg = _repo(tmp_path, "", {"notes/a.md": "# A\n\nbody\n"})
    docs, _code = enrich.enrich_repo(tmp_path, cfg)
    txt = (tmp_path / "notes/a.md").read_text()
    assert "description: A test document about things." in txt
    assert "tags: alpha, beta, gamma" in txt  # cleaned: lowercase, atomic, deduped
    assert txt.startswith("---")  # frontmatter prepended


def test_enrich_never_clobbers_without_force(tmp_path, monkeypatch):
    monkeypatch.setattr(enrich, "_ask", _fake_ask)
    _root, cfg = _repo(
        tmp_path, "", {"a.md": "---\ndescription: mine\n---\n# A\n\nx\n"}
    )
    enrich.enrich_repo(tmp_path, cfg)
    txt = (tmp_path / "a.md").read_text()
    assert "description: mine" in txt  # existing kept
    assert "A test document" not in txt  # not clobbered
    assert "tags:" in txt  # the missing field IS filled


def test_enrich_code_shebang_safe(tmp_path, monkeypatch):
    import ast as _ast

    monkeypatch.setattr(enrich, "_ask", _fake_ask)
    _root, cfg = _repo(tmp_path, "", {"s.py": "#!/usr/bin/env python3\nx = 1\n"})
    enrich.enrich_repo(tmp_path, cfg, code_only=True)
    src = (tmp_path / "s.py").read_text()
    lines = src.splitlines()
    assert lines[0] == "#!/usr/bin/env python3"  # shebang stays line 1
    assert lines[1].startswith('"""')  # docstring inserted after
    _ast.parse(src)  # still valid Python


def test_enrich_dry_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(enrich, "_ask", _fake_ask)
    _root, cfg = _repo(tmp_path, "", {"a.md": "# A\n\nx\n"})
    before = (tmp_path / "a.md").read_text()
    docs, _code = enrich.enrich_repo(tmp_path, cfg, dry=True)
    assert docs and (tmp_path / "a.md").read_text() == before


def test_enrich_domain_field_opt_in(tmp_path, monkeypatch):
    monkeypatch.setattr(enrich, "_ask", _fake_ask)
    _root, cfg = _repo(
        tmp_path,
        '[enrich]\nfields = ["description", "domain", "tags"]\n',
        {"finances/a.md": "# A\n\nx\n"},
    )
    enrich.enrich_repo(tmp_path, cfg)
    assert "domain: finances" in (tmp_path / "finances/a.md").read_text()
