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

__all__ = ["snippet", "install", "refresh", "map_refresh", "target_for"]


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
# _refresh()
# ═══════════════════════════════════════════════════════════════
# Shared body for the two block-scoped detectors. Only the DEDICATED
# rule has generated sections (a shared AGENTS.md has no hook, so this
# is a no-op there). Guards against clobbering a non-repolens file at
# the dedicated path, then lets rulegen compare the split change-keys
# and regenerate only the requested block(s) when they differ (rulegen
# self-heals a missing or pre-split legacy file). Empty string = "no
# change" (the common no-op).
# ═══════════════════════════════════════════════════════════════
def _refresh(
    root: pathlib.Path,
    config: dict | None,
    do_env: bool,
    do_map: bool,
    force_map: bool,
    label: str,
) -> str:
    target, kind = target_for(root)
    if kind != "dedicated":
        return ""
    if config is None:
        config = _root.load_config(root)
    if target.is_file():
        text = target.read_text(encoding="utf-8", errors="ignore")
        if templates.RULE_MARKER not in text:
            return f"⚠ {target} exists but isn't repolens's — not touching it."
    changed, _keys = rulegen.refresh(
        target, root, config, do_env=do_env, do_map=do_map, force_map=force_map
    )
    return f"refreshed repolens rule ({label})" if changed else ""


# ═══════════════════════════════════════════════════════════════
# refresh() — the SessionStart detector
# ═══════════════════════════════════════════════════════════════
# Regenerate ENVIRONMENT on a toolchain change. Also regenerates the
# deterministic Map when NO `[map].command` is configured (the public
# default — one cheap hook does both). When a map command IS set, the
# Map is owned by the SessionEnd `map_refresh`, so this leaves it alone
# and never stomps a model-written map with the deterministic render.
# ═══════════════════════════════════════════════════════════════
def refresh(root: pathlib.Path, config: dict | None = None) -> str:
    if config is None:
        config = _root.load_config(root)
    has_cmd = bool(config.get("map", {}).get("command"))
    label = "Environment" if has_cmd else "Environment + Map"
    return _refresh(
        root, config, do_env=True, do_map=not has_cmd, force_map=False, label=label
    )


# ═══════════════════════════════════════════════════════════════
# map_refresh() — the SessionEnd detector (run via `repolens map`/`tidy`)
# ═══════════════════════════════════════════════════════════════
# Regenerate the MAP on a folder/DB change — model-written when
# `[map].command` is set, else deterministic. `force` ignores the
# map-key (a manual `repolens map --force`).
# ═══════════════════════════════════════════════════════════════
def map_refresh(
    root: pathlib.Path, config: dict | None = None, force: bool = False
) -> str:
    return _refresh(
        root, config, do_env=False, do_map=True, force_map=force, label="Map"
    )
