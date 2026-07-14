"""repolens.templates — embedded starter files written by `repolens init`.

Embedded as strings (not package data) so the wheel stays trivially portable —
stdlib-only, nothing to bundle.
"""

from __future__ import annotations

DEFAULT_CONFIG = """\
# repolens config. The presence of this file marks the repo root.
# Docs: https://github.com/hawkesj12/repolens

[repolens]
# index_path = ".repometa/index.db"   # default; a gitignored, disposable cache
# skip_dirs = ["build", "vendor"]      # ADDED to sensible defaults (.git, node_modules, ...)
# skip_files = ["CHANGELOG.md"]
# code_exts = [".py", ".ts", ".go"]    # override the default code-file extensions
# include_gitignored = true            # index gitignored file CONTENT too (default: false —
                                       # .gitignore is respected, so secrets/.env stay out).
                                       # Turn on for a personal/knowledge repo whose notes are
                                       # gitignored and you WANT searchable. (DB schema is
                                       # indexed regardless — names only, via [integrations.sqlite].)

# Typed records: folder -> type. `recursive` classifies subfolders too.
# `exclude` globs drop artifacts. `require` = regex patterns a conforming doc must
# contain (a warn if missing). An explicit frontmatter `type:` overrides the folder.
#
# [types.doc]
# folder = "docs"
# recursive = true
# exclude = ["*draft*"]
# require = ["^# "]        # e.g. must have an H1

# Optional: also index SQLite DB table/column names (schema only, read-only).
# `repolens init` AUTO-DISCOVERS databases and fills this in; edit by hand too.
# [integrations.sqlite]
# paths = ["data/app.db", "data/other.db"]   # one or many (legacy `path = "..."` also works)

# Toolchain `repolens env` reports as PRESENT (with versions). `repolens init`
# auto-seeds this from your manifests (pyproject -> python, package.json -> node,
# ...). Edit freely — absence is the default, so only list what matters.
# [env]
# tools = ["git", "python", "node"]

# `repolens enrich` — generate description/tags frontmatter (+ code purpose lines)
# with a LOCAL model. BRING YOUR OWN MODEL: point `model` at anything your endpoint
# serves (ollama by default). It only FILLS MISSING fields (never clobbers; --force
# to regenerate) and writes to source files. This is the one command that needs a
# model server; everything else is stdlib + offline.
# [enrich]
# model = "llama3.2"                                  # e.g. gemma2:9b, qwen2.5, ...
# endpoint = "http://localhost:11434/api/generate"    # ollama's API shape
# command = "claude -p --model haiku"                 # OR: any CLI that takes the prompt
#                                                     # on stdin + prints the answer (takes
#                                                     # precedence; runs on your Claude sub,
#                                                     # no API key, compute off your machine)
# fields = ["description", "tags"]                     # add "domain" (from top dir) if you want it
# [enrich.keys]                                        # write into YOUR schema's field names
# description = "summary"                              # (default: the kind name)
# tags = "keywords"
"""


# ═══════════════════════════════════════════════════════════════
# active_sqlite_block()
# ═══════════════════════════════════════════════════════════════
# The ACTIVE [integrations.sqlite] block `repolens init` appends when it
# discovers databases. Rendered as valid TOML (a paths list of quoted,
# repo-relative strings). Appended after DEFAULT_CONFIG, whose own sqlite
# block is commented — so there is never a duplicate table.
# ═══════════════════════════════════════════════════════════════
def active_sqlite_block(paths: list[str]) -> str:
    inner = ", ".join(f'"{p}"' for p in paths)
    return (
        "\n# auto-discovered by `repolens init` (schema only, read-only)\n"
        "[integrations.sqlite]\n"
        f"paths = [{inner}]\n"
    )


# ═══════════════════════════════════════════════════════════════
# active_env_block()
# ═══════════════════════════════════════════════════════════════
# The ACTIVE [env] block `repolens init` appends when detect_stack finds
# the repo's toolchain. Valid TOML; appended after DEFAULT_CONFIG (whose
# own [env] block is commented) so there is never a duplicate table.
# ═══════════════════════════════════════════════════════════════
def active_env_block(tools: list[str]) -> str:
    inner = ", ".join(f'"{t}"' for t in tools)
    return (
        "\n# auto-seeded from this repo's manifests by `repolens init`\n"
        "[env]\n"
        f"tools = [{inner}]\n"
    )


PRECOMMIT_HOOK = """\
#!/bin/sh
# repolens pre-commit — block a commit when the corpus lint finds ERRORS.
# Installed by `repolens init`. Bypass once with: git commit --no-verify
repolens lint --strict || {
    echo "repolens: corpus lint found errors (above). Fix, or 'git commit --no-verify'." >&2
    exit 1
}
exit 0
"""


# The instruction doc that TEACHES an agent to use repolens — the missing half:
# tools are useless if the agent doesn't know to reach for them. Written by
# `repolens rule --install` (and `init`) to .claude/rules/repolens.md (auto-loads
# in Claude Code) or AGENTS.md. Deliberately short — it loads every session. The
# marker sits at the BOTTOM (idempotency + non-clobber) so the file opens with the H1.
RULE_MARKER = "<!-- repolens:rule -->"
RULE_DOC = f"""\
# Using repolens in this repo

This repo is indexed by **repolens** — lexical/BM25 ranked search + hygiene over its
docs, code purpose-lines, and DB schema. The index self-refreshes, so results are
always current.

- **`repolens find "<what you're after>"`** — when you know the _concept_ but not the
  file ("where does X live", "which file handles Y"). Returns the right few files,
  ranked and described — reach for this before reading around or broad-grepping.
- **`rg` / grep** — when you already know the exact string, or need _every_ match.
- **`repolens lint`** — corpus hygiene (dead links, malformed frontmatter) before a commit.

Routing rule: **concept → `repolens find`; exact string → `rg`.**

{RULE_MARKER}
"""
