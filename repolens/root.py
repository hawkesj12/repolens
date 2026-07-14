"""repolens.root — find the repo root and load its config.

Portability keystone: no path is ever hardcoded. The repo root is the nearest
ancestor of the working directory containing `.repometa.toml` (the config
marker), else `.git`, else the cwd itself. Resolution is anchored ONLY to the
user's cwd (never the install location) so an editable / venv-in-repo install
can't resolve the wrong repo. Config is parsed with stdlib tomllib (3.11+) and
merged over sane generic defaults — so an unconfigured repo still works and no
`private/`-style folder is ever assumed.
"""

from __future__ import annotations

import pathlib

try:
    import tomllib  # py3.11+
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

CONFIG_NAME = ".repometa.toml"

# Generic defaults — nothing repo-specific. Consumers EXTEND skip_dirs via config.
DEFAULT_INDEX_PATH = ".repometa/index.db"
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
    ".repometa",
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

# Generic default toolchain for `repolens env` when a repo sets no [env].tools
# and init found no manifests. Kept minimal — NOT an opinionated stack.
DEFAULT_ENV_TOOLS = ["git", "python", "node"]

# Files larger than this are skipped at index time — a guard against a stray
# huge file (a generated dump, a vendored blob) bloating the disposable index
# and reading unbounded bytes into memory. Config-overridable via max_file_bytes.
DEFAULT_MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB

__all__ = ["find_root", "load_config", "CONFIG_NAME"]


# ═══════════════════════════════════════════════════════════════
# find_root()
# ═══════════════════════════════════════════════════════════════
# Resolve the repo root: nearest ancestor of `start` (default the
# cwd) containing .repometa.toml, else .git, else the cwd. Anchored
# only to the user's location — NEVER __file__ (the install dir),
# so an editable/venv-in-repo install can't resolve the wrong repo.
# ═══════════════════════════════════════════════════════════════
def find_root(start: pathlib.Path | str | None = None) -> pathlib.Path:
    starts: list[pathlib.Path] = []
    if start is not None:
        starts.append(pathlib.Path(start).resolve())
    starts.append(pathlib.Path.cwd())

    for marker in (CONFIG_NAME, ".git"):
        for base in starts:
            for parent in [base, *base.parents]:
                if (parent / marker).exists():
                    return parent
    return pathlib.Path.cwd()


# ═══════════════════════════════════════════════════════════════
# load_config()
# ═══════════════════════════════════════════════════════════════
# Parse <root>/.repometa.toml and merge over defaults. Returns a dict:
#   index_path (Path, absolute), skip_dirs (set), skip_files (set),
#   code_exts (set), types (dict name->{folder,recursive,exclude}),
#   sqlite_paths (list[Path] — the optional DB-table integration, empty
#   unless [integrations.sqlite] sets `paths` and/or the legacy `path`),
#   env_tools (list[str] — the `repolens env` toolchain allowlist, from
#   [env].tools else DEFAULT_ENV_TOOLS).
# A missing config / no tomllib → pure defaults (repo still indexes).
# ═══════════════════════════════════════════════════════════════
def load_config(root: pathlib.Path | str | None = None) -> dict:
    root = pathlib.Path(root) if root is not None else find_root()
    data: dict = {}
    path = root / CONFIG_NAME
    if tomllib is not None and path.is_file():
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
        except (OSError, ValueError):
            data = {}

    rl = data.get("repolens", {}) if isinstance(data.get("repolens"), dict) else {}

    index_path = root / rl.get("index_path", DEFAULT_INDEX_PATH)
    skip_dirs = DEFAULT_SKIP_DIRS | set(rl.get("skip_dirs", []))
    skip_files = set(rl.get("skip_files", []))
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

    # Toolchain allowlist for `repolens env` — [env].tools, else the generic default.
    env = data.get("env", {})
    env_tools = (
        list(env.get("tools", DEFAULT_ENV_TOOLS))
        if isinstance(env, dict)
        else list(DEFAULT_ENV_TOOLS)
    )

    # `repolens enrich` — bring-your-own local model (ollama shape by default).
    # `keys` optionally renames the OUTPUT frontmatter field per kind, so enrich
    # writes into a repo's own schema (e.g. description -> summary) instead of
    # imposing its names. Defaults to the kind name.
    en = data.get("enrich", {})
    en = en if isinstance(en, dict) else {}
    raw_keys = en.get("keys") if isinstance(en.get("keys"), dict) else {}
    enrich = {
        "model": str(en.get("model", "llama3.2")),
        "endpoint": str(en.get("endpoint", "http://localhost:11434/api/generate")),
        "command": str(en["command"]) if en.get("command") else "",
        "fields": list(en.get("fields", ["description", "tags"])),
        "keys": {k: str(v) for k, v in raw_keys.items() if isinstance(v, str)},
    }

    return {
        "root": root,
        "index_path": index_path,
        "skip_dirs": skip_dirs,
        "skip_files": skip_files,
        "code_exts": code_exts,
        "types": types,
        "sqlite_paths": sqlite_paths,
        "env_tools": env_tools,
        "include_gitignored": include_gitignored,
        "max_file_bytes": max_file_bytes,
        "enrich": enrich,
    }
