"""repolens.root — find the repo root and load its config.

Portability keystone: no path is ever hardcoded. The repo root is the nearest
ancestor containing `.repometa.toml` (the config marker), else `.git`, else a
`__file__`-relative fallback. Config is parsed with stdlib tomllib (3.11+) and
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

__all__ = ["find_root", "load_config", "CONFIG_NAME"]


# ═══════════════════════════════════════════════════════════════
# find_root()
# ═══════════════════════════════════════════════════════════════
# Resolve the repo root: nearest ancestor of `start` (default cwd,
# then this file) containing .repometa.toml, else .git, else a
# __file__-relative fallback. No hardcoded paths.
# ═══════════════════════════════════════════════════════════════
def find_root(start: pathlib.Path | str | None = None) -> pathlib.Path:
    starts: list[pathlib.Path] = []
    if start is not None:
        starts.append(pathlib.Path(start).resolve())
    starts.append(pathlib.Path.cwd())
    starts.append(pathlib.Path(__file__).resolve())

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
#   unless [integrations.sqlite] sets `paths` and/or the legacy `path`).
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

    return {
        "root": root,
        "index_path": index_path,
        "skip_dirs": skip_dirs,
        "skip_files": skip_files,
        "code_exts": code_exts,
        "types": types,
        "sqlite_paths": sqlite_paths,
    }
