"""repolens.ruledoc — teach an agent to USE repolens (the missing instruction half).

A search tool the agent doesn't know to reach for is dead weight. This writes a
short routing rule ("concept -> repolens find; exact string -> rg") where the agent
will actually see it: `.claude/rules/repolens.md` (auto-loads every session in Claude
Code) or `AGENTS.md` at the repo root (the cross-agent convention). Idempotent +
NON-DESTRUCTIVE: skips if already present, appends to an existing AGENTS.md, and never
clobbers a file it didn't write. Stdlib-only.
"""

from __future__ import annotations

import pathlib

from . import templates

__all__ = ["snippet", "install", "target_for"]


def snippet() -> str:
    return templates.RULE_DOC


# ═══════════════════════════════════════════════════════════════
# target_for()
# ═══════════════════════════════════════════════════════════════
# Where the instruction goes: a dedicated auto-loading rule in a
# Claude Code repo, else the agent-agnostic AGENTS.md at the root.
# ═══════════════════════════════════════════════════════════════
def target_for(root: pathlib.Path) -> tuple[pathlib.Path, str]:
    if (root / ".claude").is_dir():
        return root / ".claude" / "rules" / "repolens.md", "dedicated"
    return root / "AGENTS.md", "shared"


# ═══════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════
# Write the instruction to its target. Idempotent (skips if the
# repolens marker is already there), additive for a shared AGENTS.md,
# and refuses to overwrite a non-repolens file at the dedicated path.
# check=True is a dry-run (no write).
# ═══════════════════════════════════════════════════════════════
def install(root: pathlib.Path, check: bool = False) -> str:
    target, kind = target_for(root)
    exists = target.is_file()
    if exists and templates.RULE_MARKER in target.read_text(
        encoding="utf-8", errors="ignore"
    ):
        return f"already present in {target} (no change)."
    if check:
        how = "append to" if (exists and kind == "shared") else "write"
        return f"would {how} {target}:\n\n{templates.RULE_DOC}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if kind == "dedicated":
        if exists:  # a repolens.md we didn't author — never clobber
            return f"⚠ {target} exists but isn't repolens's — not touching it."
        target.write_text(templates.RULE_DOC, encoding="utf-8")
    else:  # AGENTS.md — create, or additively append our section
        prior = target.read_text(encoding="utf-8") if exists else ""
        sep = "" if not prior else ("\n" if prior.endswith("\n") else "\n\n")
        target.write_text(prior + sep + templates.RULE_DOC, encoding="utf-8")
    return f"installed agent rule → {target}"
