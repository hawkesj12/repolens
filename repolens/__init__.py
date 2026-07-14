"""repolens — a ranked, described search index + typed hygiene linter for a repo.

Finds where things live across a repo's markdown (full text), code (by purpose
line), and optional DB tables — including gitignored content that .gitignore-
respecting tools skip — and lints a typed knowledge corpus for hygiene. Built for
repos where an agent (e.g. Claude Code) greps on demand rather than keeping a
semantic index. Stdlib-only.
"""

from __future__ import annotations

__version__ = "0.7.0"

# Bumped when the config or type-schema shape changes.
SCHEMA_VERSION = "1.2"
