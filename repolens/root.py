"""repolens.root — find the repo root and load its config.

Portability keystone: no path is ever hardcoded. The repo root is the nearest
ancestor of the working directory containing `.repolens.toml` (the config
marker), else `.git`, else the cwd itself. Resolution is anchored ONLY to the
user's cwd (never the install location) so an editable / venv-in-repo install
can't resolve the wrong repo. Config is parsed with stdlib tomllib (3.11+) and
merged over sane generic defaults — so an unconfigured repo still works and no
`private/`-style folder is ever assumed.
"""

from __future__ import annotations

import pathlib
import sys

try:
    import tomllib  # py3.11+
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

CONFIG_NAME = ".repolens.toml"
LEGACY_CONFIG_NAME = ".repometa.toml"  # pre-0.11 name — read with a deprecation warning
_LEGACY_WARNED = False  # warn about a legacy config once per process

# Generic defaults — nothing repo-specific. Consumers EXTEND skip_dirs via config.
DEFAULT_INDEX_PATH = ".repolens/index.db"
DEFAULT_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".repolens",
    ".idea",
    ".vscode",
}
DEFAULT_CODE_EXTS = {
    ".py",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".jsx",
    ".sql",
    ".sh",
    ".bash",
    ".zsh",
    ".toml",
    ".rb",
    ".go",
    ".rs",
}

# Semantic (hybrid) search defaults. Everything is opt-out-able but ON by default
# so `repolens[semantic]` "just works" once installed; with the extra ABSENT the
# subsystem's availability gate makes this inert (find stays lexical-only).
#
# Default: `BAAI/bge-base-en-v1.5` via fastembed (local, no service). Chunks are
# SECTION-BOUNDED and small (~512 tokens) — they never cross a Markdown heading, and
# a longer section is packed into ~512-token pieces (see chunk.py). bge-base is built
# for exactly this short-passage retrieval and its 512-token limit is a non-issue at
# this chunk size. `threads` throttles fastembed's CPU (0 = library default; a low
# value keeps a big first build from maxing the machine). `provider` is the escape
# hatch: "fastembed" (default) or "http" — any OpenAI-compatible /v1/embeddings
# endpoint (local Ollama/LM Studio, or a metered API), keyed via api_key_env.
DEFAULT_SEMANTIC = {
    "enabled": True,
    "provider": "fastembed",
    "model": "BAAI/bge-base-en-v1.5",
    "dims": 768,
    "chunk_tokens": 512,
    "overlap": 0.15,
    "threads": 2,
    "endpoint": "",
    "api_key_env": "",
}

# Files larger than this are skipped at index time — a guard against a stray
# huge file (a generated dump, a vendored blob) bloating the disposable index
# and reading unbounded bytes into memory. Config-overridable via max_file_bytes.
DEFAULT_MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB

__all__ = [
    "find_root",
    "load_config",
    "CONFIG_NAME",
]


# ═══════════════════════════════════════════════════════════════
# find_root()
# ═══════════════════════════════════════════════════════════════
# Resolve the repo root: nearest ancestor of `start` (default the
# cwd) containing .repolens.toml, else .git, else the cwd. Anchored
# only to the user's location — NEVER __file__ (the install dir),
# so an editable/venv-in-repo install can't resolve the wrong repo.
# ═══════════════════════════════════════════════════════════════
def find_root(start: pathlib.Path | str | None = None) -> pathlib.Path:
    starts: list[pathlib.Path] = []
    if start is not None:
        starts.append(pathlib.Path(start).resolve())
    starts.append(pathlib.Path.cwd())

    # ".repometa.toml" is the pre-0.11 config name — still recognized as a root marker so
    # an un-migrated repo resolves correctly (see load_config's back-compat read).
    for marker in (CONFIG_NAME, LEGACY_CONFIG_NAME, ".git"):
        for base in starts:
            for parent in [base, *base.parents]:
                if (parent / marker).exists():
                    return parent
    return pathlib.Path.cwd()


# ═══════════════════════════════════════════════════════════════
# load_config()
# ═══════════════════════════════════════════════════════════════
# Parse <root>/.repolens.toml and merge over defaults. Returns a dict:
#   index_path (Path, absolute), skip_dirs (set), skip_files (set),
#   code_exts (set), types (dict name->{folder,recursive,exclude}),
#   sqlite_paths (list[Path] — the optional DB-table integration, empty
#   unless [integrations.sqlite] sets `paths` and/or the legacy `path`),
#   semantic (dict — the hybrid-search tier config).
# A missing config / no tomllib → pure defaults (repo still indexes).
# ═══════════════════════════════════════════════════════════════
def load_config(root: pathlib.Path | str | None = None) -> dict:
    root = pathlib.Path(root) if root is not None else find_root()
    data: dict = {}
    path = root / CONFIG_NAME
    # Back-compat: read a pre-0.11 .repometa.toml when .repolens.toml is absent, with a
    # one-time deprecation warning — so an un-migrated repo (e.g. before `git mv`) keeps
    # its config (include_gitignored, types, ...) instead of silently falling to defaults.
    if not path.is_file():
        legacy = root / LEGACY_CONFIG_NAME
        if legacy.is_file():
            path = legacy
            global _LEGACY_WARNED
            if not _LEGACY_WARNED:
                _LEGACY_WARNED = True
                print(
                    f"⚠ {LEGACY_CONFIG_NAME} is deprecated — rename it to {CONFIG_NAME} "
                    f"(git mv {LEGACY_CONFIG_NAME} {CONFIG_NAME}); reading it for now",
                    file=sys.stderr,
                )
    if tomllib is not None and path.is_file():
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
        except (OSError, ValueError):
            data = {}

    rl = data.get("repolens", {}) if isinstance(data.get("repolens"), dict) else {}

    index_path = root / rl.get("index_path", DEFAULT_INDEX_PATH)
    skip_dirs = DEFAULT_SKIP_DIRS | set(rl.get("skip_dirs", []))
    # Never index repolens's own config file — it's tooling, not corpus, and otherwise
    # shows up as noise in the very first `find` a new user runs.
    skip_files = {CONFIG_NAME} | set(rl.get("skip_files", []))
    code_exts = set(rl.get("code_exts", DEFAULT_CODE_EXTS))
    # Respect .gitignore by DEFAULT — the file corpus skips gitignored paths
    # unless include_gitignored is set (opt-in for personal/knowledge repos).
    include_gitignored = bool(rl.get("include_gitignored", False))
    max_file_bytes = int(rl.get("max_file_bytes", DEFAULT_MAX_FILE_BYTES))

    types = {}
    for name, spec in (data.get("types") or {}).items():
        if isinstance(spec, dict) and "folder" in spec:
            types[name] = {
                "folder": str(spec["folder"]).strip("/"),
                "recursive": bool(spec.get("recursive", False)),
                "exclude": list(spec.get("exclude", [])),
                "require": list(spec.get("require", [])),
            }

    # SQLite integration accepts a `paths` LIST and a legacy singular `path`
    # (kept for backward-compat); both merge, resolved + deduped, preserving order.
    sqlite_paths: list[pathlib.Path] = []
    integ = data.get("integrations", {})
    if isinstance(integ, dict):
        sq = integ.get("sqlite", {})
        if isinstance(sq, dict):
            raw: list[str] = []
            if sq.get("path"):
                raw.append(str(sq["path"]))
            raw.extend(str(p) for p in (sq.get("paths") or []))
            seen: set[pathlib.Path] = set()
            for rel in raw:
                resolved = (root / rel).resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    sqlite_paths.append(root / rel)

    # `[semantic]` — the hybrid-search tier. Merged over DEFAULT_SEMANTIC so a
    # missing block gives working defaults and a partial block overrides only its
    # keys. Types are coerced defensively (a bad value falls back to the default).
    sm = data.get("semantic", {})
    sm = sm if isinstance(sm, dict) else {}
    semantic = dict(DEFAULT_SEMANTIC)
    if "enabled" in sm:
        semantic["enabled"] = bool(sm["enabled"])
    for key in (
        "model",
        "provider",
        "endpoint",
        "api_key_env",
        "query_prefix",
        "doc_prefix",
    ):
        if sm.get(key):
            semantic[key] = str(sm[key])
    for k in ("dims", "chunk_tokens", "threads"):
        try:
            if k in sm:
                semantic[k] = int(sm[k])
        except (TypeError, ValueError):
            pass
    try:
        if "overlap" in sm:
            semantic["overlap"] = float(sm["overlap"])
    except (TypeError, ValueError):
        pass

    # `[log]` — optional, private local event log (find + embed). Off by default; when
    # on, repolens.log appends JSONL to the gitignored .repolens/ cache dir.
    lg = data.get("log", {})
    lg = lg if isinstance(lg, dict) else {}
    logcfg = {"enabled": bool(lg.get("enabled", False))}

    return {
        "root": root,
        "index_path": index_path,
        "skip_dirs": skip_dirs,
        "skip_files": skip_files,
        "code_exts": code_exts,
        "types": types,
        "sqlite_paths": sqlite_paths,
        "include_gitignored": include_gitignored,
        "max_file_bytes": max_file_bytes,
        "semantic": semantic,
        "log": logcfg,
    }
