"""repolens.templates — embedded starter files written by `repolens init`.

Embedded as strings (not package data) so the wheel stays trivially portable —
stdlib-only, nothing to bundle.
"""

from __future__ import annotations

DEFAULT_CONFIG = """\
# repolens config. The presence of this file marks the repo root.
# Docs: https://github.com/hawkesj12/repolens

[repolens]
# index_path = ".repolens/index.db"   # default; a gitignored, disposable cache
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
