"""repolens — a ranked, described search index + typed hygiene linter for a repo.

Finds where things live across a repo's markdown (full text), code (by purpose
line), and optional DB tables — including gitignored content that .gitignore-
respecting tools skip — and lints a typed knowledge corpus for hygiene. Built for
repos where an agent (e.g. Claude Code) greps on demand rather than keeping a
semantic index. Hybrid search ships by default (core deps: fastembed + sqlite-vec).
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

# Single source of truth: read the version from the installed package metadata
# (which pyproject.toml sets) rather than hardcoding it here. A hardcoded string
# silently drifts from pyproject on a release — 0.14.0 shipped reporting 0.13.1
# because only pyproject was bumped. This makes that drift structurally impossible.
try:
    __version__ = version("repolens-search")
except PackageNotFoundError:  # running from a source tree with no install metadata
    __version__ = "0.0.0+unknown"

# Bumped when the config or type-schema shape changes.
# 1.3: added the [semantic] config block (hybrid search) + the chunks/vectors tables.
SCHEMA_VERSION = "1.3"
