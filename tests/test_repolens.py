"""repolens test suite — core engine + the fixes it was hardened with."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess

import pytest

from repolens import (
    chunk,
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
    ruledoc,
    rulegen,
    schema,
    semantic,
    templates,
)


@pytest.fixture(autouse=True)
def _semantic_off_by_default(monkeypatch):
    # Keep the suite hermetic + deterministic whether or not the [semantic] extra is
    # installed: the REAL tier is off by default, so `find` stays lexical (matching the
    # pinned rankings) and no index build downloads a model. Semantic tests opt back in
    # with fake embeddings via _force_blob. The real embedding path is validated end-to-
    # end by the acceptance bake-off, not here.
    monkeypatch.setattr(semantic, "available", lambda *a, **k: False)


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


def test_find_root_ignores_install_dir(tmp_path, monkeypatch):
    # A dir with .git but no .repometa.toml, entered as cwd, must resolve to
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
    frag = hookgen.snippet()
    obj = json.loads(frag[frag.index("{") :])
    assert obj["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "repolens refresh"


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
    assert "repolens refresh" in cmds  # ours added
    assert data["otherKey"] == 1  # other keys untouched
    hookgen.install(tmp_path)  # idempotent
    data2 = json.loads(settings.read_text())
    cmds2 = [h["command"] for g in data2["hooks"]["SessionStart"] for h in g["hooks"]]
    assert cmds2.count("repolens refresh") == 1


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
    # are some"), and ours runs the `repolens refresh` change-detector.
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
    assert "repolens refresh" in cmds  # ours, the change-detector


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


def test_cmd_init_self_hosts_when_root_is_dotclaude(tmp_path, monkeypatch):
    # Installing INTO a `.claude` dir (e.g. ~/.claude): the root IS `.claude`, so the
    # hook + rule go in the root itself — not the nonexistent `<root>/.claude` that the
    # old check looked for and silently skipped.
    dc = tmp_path / ".claude"
    dc.mkdir()
    monkeypatch.setattr(root, "find_root", lambda *a, **k: dc)
    cli.main(["init"])
    assert (dc / "settings.json").is_file()  # hook wired into .claude itself
    assert (dc / "rules" / "repolens.md").is_file()  # rule too
    data = json.loads((dc / "settings.json").read_text())
    cmds = [h["command"] for g in data["hooks"]["SessionStart"] for h in g["hooks"]]
    assert "repolens refresh" in cmds


def test_self_rule_rel_handles_dotclaude_root(tmp_path):
    # The self-index skip must resolve the rule's real relpath in both layouts.
    assert index._self_rule_rel(tmp_path) == ".claude/rules/repolens.md"
    dc = tmp_path / ".claude"
    dc.mkdir()
    assert index._self_rule_rel(dc) == "rules/repolens.md"


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


def test_enrich_root_file_gets_no_domain(tmp_path, monkeypatch):
    # a repo-root file has no parent dir → no domain (the v0.6.1 bug fix)
    monkeypatch.setattr(enrich, "_ask", _fake_ask)
    _root, cfg = _repo(
        tmp_path,
        '[enrich]\nfields = ["description", "domain"]\n',
        {"CLAUDE.md": "# C\n\nx\n"},
    )
    enrich.enrich_repo(tmp_path, cfg)
    txt = (tmp_path / "CLAUDE.md").read_text()
    assert "description:" in txt
    assert "domain:" not in txt  # never "domain: CLAUDE.md"


def test_enrich_renames_output_fields(tmp_path, monkeypatch):
    # [enrich.keys] writes into the repo's own schema names
    monkeypatch.setattr(enrich, "_ask", _fake_ask)
    _root, cfg = _repo(
        tmp_path,
        '[enrich]\nfields = ["description", "tags"]\n'
        '[enrich.keys]\ndescription = "summary"\ntags = "keywords"\n',
        {"a.md": "# A\n\nx\n"},
    )
    enrich.enrich_repo(tmp_path, cfg)
    txt = (tmp_path / "a.md").read_text()
    assert "summary: A test document" in txt and "keywords: alpha" in txt
    assert "description:" not in txt and "tags:" not in txt  # imposed names not used


def test_enrich_respects_existing_renamed_field(tmp_path, monkeypatch):
    # a doc already carrying the renamed field is treated as present (not duplicated)
    monkeypatch.setattr(enrich, "_ask", _fake_ask)
    _root, cfg = _repo(
        tmp_path,
        '[enrich]\nfields = ["description"]\n[enrich.keys]\ndescription = "summary"\n',
        {"a.md": "---\nsummary: mine\n---\n# A\n\nx\n"},
    )
    docs, _c = enrich.enrich_repo(tmp_path, cfg)
    assert not docs  # already has summary → nothing to fill
    assert "summary: mine" in (tmp_path / "a.md").read_text()


def test_enrich_command_provider(tmp_path):
    # a command provider (any CLI on stdin) is used instead of HTTP
    fake = tmp_path / "m.sh"
    fake.write_text(
        "#!/bin/sh\ncat >/dev/null\nprintf 'DESCRIPTION: cmd doc.\\nTAGS: alpha, beta\\n'\n"
    )
    fake.chmod(0o755)
    doc = tmp_path / "a.md"
    doc.write_text("# A\n\nx\n")
    cfg = {"enrich": {"command": f"sh {fake}", "keys": {}}}
    enrich._enrich_doc(
        doc, "a.md", cfg, ["description", "tags"], dry=False, force=False
    )
    txt = doc.read_text()
    assert "description: cmd doc." in txt and "tags: alpha, beta" in txt


def test_enrich_code_force_does_not_stack_docstring(tmp_path, monkeypatch):
    # code is fill-only even under --force: an existing docstring is not doubled
    monkeypatch.setattr(enrich, "_ask", _fake_ask)
    _root, cfg = _repo(tmp_path, "", {"s.py": '"""existing purpose."""\nx = 1\n'})
    enrich.enrich_repo(tmp_path, cfg, code_only=True, force=True)
    src = (tmp_path / "s.py").read_text()
    assert src.count('"""') == 2  # one docstring (open+close), not stacked to 4
    assert "existing purpose." in src  # original kept


def test_enrich_force_preserves_other_keys(tmp_path, monkeypatch):
    # --force regenerates our fields but must NOT drop a doc's other frontmatter
    monkeypatch.setattr(enrich, "_ask", _fake_ask)
    _root, cfg = _repo(
        tmp_path,
        '[enrich]\nfields = ["description"]\n',
        {"a.md": "---\nid: 42\ndescription: old\n---\n# A\n\nx\n"},
    )
    enrich.enrich_repo(tmp_path, cfg, force=True)
    txt = (tmp_path / "a.md").read_text()
    assert "id: 42" in txt  # non-managed key preserved
    assert "description: A test document" in txt  # regenerated
    assert "description: old" not in txt  # old value replaced


# ── rule (the agent instruction doc — teach the agent to use find) ─
def test_rule_snippet_teaches_routing():
    s = ruledoc.snippet()
    assert templates.RULE_MARKER in s
    assert "repolens find" in s and "rg" in s


def test_rule_install_claude_repo_writes_dedicated_rule(tmp_path):
    (tmp_path / ".claude").mkdir()
    ruledoc.install(tmp_path)
    p = tmp_path / ".claude" / "rules" / "repolens.md"
    assert p.is_file() and templates.RULE_MARKER in p.read_text()


def test_rule_install_agents_md_when_no_claude(tmp_path):
    ruledoc.install(tmp_path)
    p = tmp_path / "AGENTS.md"
    assert p.is_file() and "repolens find" in p.read_text()


def test_rule_install_idempotent(tmp_path):
    (tmp_path / ".claude").mkdir()
    ruledoc.install(tmp_path)
    p = tmp_path / ".claude" / "rules" / "repolens.md"
    before = p.read_text()
    assert "already present" in ruledoc.install(tmp_path)
    assert p.read_text() == before  # no duplicate


def test_rule_appends_to_existing_agents_md(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# My agents\n\nexisting guidance\n")
    ruledoc.install(tmp_path)
    txt = (tmp_path / "AGENTS.md").read_text()
    assert "existing guidance" in txt  # preserved, not clobbered
    assert templates.RULE_MARKER in txt  # ours appended


def test_cmd_init_installs_rule_in_claude_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(root, "find_root", lambda *a, **k: tmp_path)
    (tmp_path / ".claude").mkdir()
    cli.main(["init", "--no-hook"])  # rule installs even with hook off
    assert (tmp_path / ".claude" / "rules" / "repolens.md").is_file()


def test_cmd_init_no_rule_skips(tmp_path, monkeypatch):
    monkeypatch.setattr(root, "find_root", lambda *a, **k: tmp_path)
    (tmp_path / ".claude").mkdir()
    cli.main(["init", "--no-hook", "--no-rule"])
    assert not (tmp_path / ".claude" / "rules" / "repolens.md").exists()


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
    assert "alpha.md" not in {rp for rp, _d in semantic.knn(con, "alpha", 5, cfg)}


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
    assert len({rp for rp, _d in hits}) == len(hits)  # per-DOC (rolled up), no dup docs
    semantic.delete_doc(con, "alpha.md")
    con.commit()
    assert "alpha.md" not in {rp for rp, _d in semantic.knn(con, "alpha", 5, cfg)}


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


def test_find_lexical_only_skips_dense(tmp_path, monkeypatch):
    _force_blob(monkeypatch)
    _root, cfg = _repo(tmp_path, "", {"a.md": "# a\n\nalpha fox\n"})
    index.build(tmp_path, cfg)
    called = []
    monkeypatch.setattr(semantic, "knn", lambda *a, **k: called.append(1) or [])
    hits = find.search(cfg, "alpha", 5, lexical_only=True)
    assert hits and hits[0]["relpath"] == "a.md"
    assert not called  # the dense half was never consulted


def test_rrf_scores_fuse_and_rank():
    # a doc ranked well by EITHER list should rank up; a doc in both wins.
    scores = find._rrf_scores(["a.md", "b.md", "c.md"], ["c.md", "a.md"])
    assert scores["a.md"] > scores["b.md"]  # a is in both, b in one
    assert scores["c.md"] > scores["b.md"]  # c is high in dense + in lexical


# ── v0.9 rule-as-artifact: generated sections + change-detector ────
def test_rulegen_change_key_stable_and_sensitive(tmp_path):
    _root, cfg = _repo(
        tmp_path, "", {"README.md": "# r\n\nx\n", "docs/a.md": "# a\n\nx\n"}
    )
    k1 = rulegen.change_key(tmp_path, cfg)
    assert k1 == rulegen.change_key(tmp_path, cfg)  # pure function — stable
    (tmp_path / "newdir").mkdir()
    (tmp_path / "newdir" / "n.md").write_text("# n\n\nx\n")
    assert rulegen.change_key(tmp_path, cfg) != k1  # a new folder flips the key


def test_rule_refresh_noop_then_regen_preserves_header(tmp_path, monkeypatch):
    monkeypatch.setattr(root, "find_root", lambda *a, **k: tmp_path)
    (tmp_path / ".claude").mkdir()
    (tmp_path / "README.md").write_text("# demo\n\nx\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("# a\n\nx\n")
    cli.main(["init", "--no-hook"])
    rule = tmp_path / ".claude" / "rules" / "repolens.md"
    text = rule.read_text()
    assert templates.RULE_MARKER in text and templates.GEN_MAP_START in text
    assert ruledoc.refresh(tmp_path) == ""  # nothing changed → no-op
    (tmp_path / "newdir").mkdir()
    (tmp_path / "newdir" / "n.md").write_text("# n\n\nx\n")
    assert "refreshed" in ruledoc.refresh(tmp_path)  # structural change → regenerate
    after = rule.read_text()
    assert "# RepoLens — demo" in after  # STATIC header preserved (not clobbered)
    assert "newdir/" in after  # Map regenerated with the new folder


def test_rule_refresh_wont_touch_foreign_file(tmp_path, monkeypatch):
    monkeypatch.setattr(root, "find_root", lambda *a, **k: tmp_path)
    rule = tmp_path / ".claude" / "rules" / "repolens.md"
    rule.parent.mkdir(parents=True)
    rule.write_text("# not ours\n\nhand-written\n")  # no RULE_MARKER
    msg = ruledoc.refresh(tmp_path)
    assert "not touching it" in msg
    assert rule.read_text() == "# not ours\n\nhand-written\n"  # untouched


def test_cmd_refresh_reports_up_to_date(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(root, "find_root", lambda *a, **k: tmp_path)
    (tmp_path / ".claude").mkdir()
    (tmp_path / "README.md").write_text("# demo\n\nx\n")
    cli.main(["init", "--no-hook"])
    capsys.readouterr()
    cli.main(["refresh"])
    assert "up to date" in capsys.readouterr().out


def test_index_skips_own_generated_rule(tmp_path):
    # repolens must NOT index its own generated rule (self-reference / map noise).
    _root, cfg = _repo(
        tmp_path,
        "",
        {
            ".claude/rules/repolens.md": "# RepoLens\n\ngenerated\n",
            "real.md": "# r\n\nx\n",
        },
    )
    index.build(tmp_path, cfg)
    con = sqlite3.connect(cfg["index_path"])
    rels = {r[0] for r in con.execute("SELECT relpath FROM docs")}
    con.close()
    assert "real.md" in rels
    assert ".claude/rules/repolens.md" not in rels
