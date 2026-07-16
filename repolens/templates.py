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

# Semantic (hybrid) search. ON by default when the `[semantic]` extra is installed
# (pip install 'repolens[semantic]'); inert otherwise (find stays lexical-only).
# Chunks are section-bounded and small (~512 tokens) — they never cross a Markdown
# heading. `threads` throttles fastembed's CPU (low = gentle on the machine). Vectors
# store in the same index (sqlite-vec fast path, numpy blob fallback). Nothing to
# configure to get started — this block only tunes it.
# [semantic]
# enabled = true
# model = "BAAI/bge-base-en-v1.5"   # short-passage retriever; fits the ~512 chunks
# dims = 768
# chunk_tokens = 512                # per-chunk target; a chunk never crosses a heading
# overlap = 0.15
# threads = 2                       # cap fastembed CPU threads (0 = all cores)
#
# Bring your own embedder instead of local fastembed — any OpenAI-compatible
# /v1/embeddings endpoint (local Ollama/LM Studio, or a metered API):
# provider = "http"
# endpoint = "http://localhost:11434/v1/embeddings"
# model = "nomic-embed-text"        # the model your endpoint serves
# api_key_env = "OPENAI_API_KEY"    # env var holding the key (never store it here)

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

# `[map]` — OPT-IN: let a model WRITE the rule's Map section instead of the
# deterministic render. `command` takes the folder facts on stdin and prints a rich
# "what lives here" map (e.g. `claude -p --model sonnet`). Empty (default) = the
# deterministic map, no model needed. When set, `repolens init`/`hook` also wires a
# SessionEnd `repolens tidy` (enrich + map) so the map rebuilds on a folder change.
# [map]
# command = "claude -p --model sonnet"   # any CLI: folder facts on stdin -> map body on stdout
# enabled = true
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


# ── The dedicated, self-maintaining rule (Claude Code repos) ──────────────────
# `.claude/rules/repolens.md` = a STATIC header (the five questions — written once)
# + two GENERATED sections (Environment + Map) that a SessionStart change-detector
# regenerates in place. The delimiters below bound the generated blocks so the
# static header is never clobbered. This one visible, openable file replaces both
# the old invisible digest-hook AND a hand-run structural scan.
GEN_ENV_START = "<!-- repolens:env:start -->"
GEN_ENV_END = "<!-- repolens:env:end -->"
GEN_MAP_START = "<!-- repolens:map:start -->"
GEN_MAP_END = "<!-- repolens:map:end -->"
# Two independent change-keys so each generated block regenerates on ITS OWN signal:
# env-key (toolchain) drives the SessionStart Env refresh; map-key (folder-set + DB
# schema) drives the SessionEnd Map regen. A tool-version bump never triggers a
# (possibly AI, possibly costly) Map rebuild, and a new folder never rewrites Env.
ENV_KEY_PREFIX = "<!-- repolens:env-key:"  # + <hash> + " -->"
MAP_KEY_PREFIX = "<!-- repolens:map-key:"  # + <hash> + " -->"
# Legacy single-key marker (pre-key-split rules) — still recognized so an older rule
# self-heals: absence of the two new markers is treated as "changed" and upgrades it.
CHANGE_KEY_PREFIX = "<!-- repolens:change-key:"  # + <hash> + " -->"

_RULE_HEADER_TMPL = """\
# RepoLens — {name}

You're working in a repo indexed by **repolens**. If you've never seen it, read this once:
repolens is a local command-line tool that keeps a fast, always-current search index and a
live map of THIS repo, so you can jump straight to the right file instead of guessing or
grepping the whole tree. This top section explains the tool; the two generated sections at
the bottom (Environment, Map) describe the actual repo you're in.

## What repolens is

One index over the repo's **docs, code purpose-lines, and database schema**, with search
and a map built on top. It runs locally, needs no server, and **self-maintains**: every
search first re-indexes any changed files, so results are never stale. Search is hybrid —
exact keyword ranking (BM25 / SQLite FTS5) and, when enabled, semantic vector search —
fused, so a query by *meaning* finds the right file even when your words aren't its text.

## How to use it

- **`repolens find "<what you're after>"`** — the default for "where does X live?" or
  "which file handles Y?". Say the concept in plain words; it returns the few most relevant
  files, ranked, each with a one-line description. Reach for this BEFORE reading around the
  tree or broad-grepping — it's more reliable than grep for "where is X".
- **`rg` (ripgrep) / grep** — when you already know the exact string, or need EVERY match.
  repolens ranks and narrows to the best few; rg enumerates them all. Different jobs.
- **`repolens lint`** — corpus hygiene (dead links, malformed frontmatter) before a commit.

**Routing rule: concept / "where is X" → `repolens find`; exact known string / every
occurrence → `rg`.**

## The two generated sections below (repolens refreshes them — don't hand-edit)

- **Environment** — the repo's real, present toolchain (languages, tools, versions), probed
  from the machine. Trust it over assumptions about what's installed here.
- **Map** — where things live: each indexed folder and what it holds. Regenerated whenever
  the repo's structure changes, so it reflects the repo as it is right now.

Environment tells you what you can run, Map tells you where to look, and `repolens find`
takes you to the exact file.

{marker}
"""


# ═══════════════════════════════════════════════════════════════
# rule_header()
# ═══════════════════════════════════════════════════════════════
# The STATIC top of the dedicated rule — the five questions, written
# once and never regenerated. Carries RULE_MARKER (idempotency / non-
# clobber) exactly like RULE_DOC.
# ═══════════════════════════════════════════════════════════════
def rule_header(name: str) -> str:
    return _RULE_HEADER_TMPL.format(name=name, marker=RULE_MARKER)
