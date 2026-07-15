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

from . import root as _root
from . import rulegen, templates

__all__ = ["snippet", "install", "refresh", "target_for"]


def snippet() -> str:
    return templates.RULE_DOC


# ═══════════════════════════════════════════════════════════════
# target_for()
# ═══════════════════════════════════════════════════════════════
# Where the instruction goes: a dedicated auto-loading rule in a
# Claude Code repo, else the agent-agnostic AGENTS.md at the root.
# ═══════════════════════════════════════════════════════════════
def target_for(root: pathlib.Path) -> tuple[pathlib.Path, str]:
    if _root.is_claude_repo(root):
        return _root.claude_dir(root) / "rules" / "repolens.md", "dedicated"
    return root / "AGENTS.md", "shared"


# ═══════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════
# Write the instruction to its target. Idempotent (skips if the
# repolens marker is already there), additive for a shared AGENTS.md,
# and refuses to overwrite a non-repolens file at the dedicated path.
# The DEDICATED Claude-Code rule is the full self-maintaining artifact:
# static header + generated Environment + generated Map (via rulegen).
# The shared AGENTS.md stays the short static routing rule (no hook to
# refresh it). check=True is a dry-run (no write). config is loaded from
# the repo when not supplied.
# ═══════════════════════════════════════════════════════════════
def install(root: pathlib.Path, check: bool = False, config: dict | None = None) -> str:
    target, kind = target_for(root)
    exists = target.is_file()
    if exists and templates.RULE_MARKER in target.read_text(
        encoding="utf-8", errors="ignore"
    ):
        return f"already present in {target} (no change)."

    if kind == "dedicated":
        if check:
            return (
                f"would write the repolens rule (static header + generated "
                f"Environment + Map) → {target}"
            )
        if exists:  # a repolens.md we didn't author — never clobber
            return f"⚠ {target} exists but isn't repolens's — not touching it."
        if config is None:
            config = _root.load_config(root)
        content, _key = rulegen.full_rule(root, config)
        rulegen.write_atomic(target, content)
        return f"installed agent rule → {target}"

    # shared AGENTS.md — create, or additively append our static section
    if check:
        how = "append to" if exists else "write"
        return f"would {how} {target}:\n\n{templates.RULE_DOC}"
    target.parent.mkdir(parents=True, exist_ok=True)
    prior = target.read_text(encoding="utf-8") if exists else ""
    sep = "" if not prior else ("\n" if prior.endswith("\n") else "\n\n")
    target.write_text(prior + sep + templates.RULE_DOC, encoding="utf-8")
    return f"installed agent rule → {target}"


# ═══════════════════════════════════════════════════════════════
# refresh()
# ═══════════════════════════════════════════════════════════════
# The SessionStart change-detector's action. Only the DEDICATED rule
# has generated sections (a shared AGENTS.md has no hook to refresh it,
# so this is a no-op there). Missing rule → write it. A pre-generated-
# sections rule (older repolens) → upgrade in full once. Otherwise let
# rulegen compare the change-key and regenerate ONLY the Map/Env blocks
# when structure changed; empty string means "no change" (the common no-op).
# ═══════════════════════════════════════════════════════════════
def refresh(root: pathlib.Path, config: dict | None = None) -> str:
    target, kind = target_for(root)
    if kind != "dedicated":
        return ""
    if config is None:
        config = _root.load_config(root)
    if not target.is_file():
        content, _key = rulegen.full_rule(root, config)
        rulegen.write_atomic(target, content)
        return f"wrote repolens rule → {target}"
    text = target.read_text(encoding="utf-8", errors="ignore")
    if templates.RULE_MARKER not in text:
        return f"⚠ {target} exists but isn't repolens's — not touching it."
    if templates.GEN_MAP_START not in text:  # older static rule → upgrade once
        content, _key = rulegen.full_rule(root, config)
        rulegen.write_atomic(target, content)
        return "upgraded repolens rule to generated sections"
    changed, _key = rulegen.refresh(target, root, config)
    return "refreshed repolens rule (Map/Environment)" if changed else ""
