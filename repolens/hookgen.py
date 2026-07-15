"""repolens.hookgen — wire the change-detector into a Claude Code SessionStart hook.

The hook runs `repolens refresh` — the early-cutoff detector. Every session it
compares a cheap change-key (folder-set + DB schema + toolchain) to the one stored
in `.claude/rules/repolens.md`; unchanged (the common case) it's a ~no-op, and on a
real structural change it regenerates the rule's Map + Environment sections in place.
(This replaces the old `repolens digest && repolens env` pair — the rule IS the
digest now, visible and openable instead of injected invisibly.)

NON-DESTRUCTIVE by design. `repolens hook` PRINTS the snippet by default (zero
writes). `--install` merges our command into the repo's `.claude/settings.json`
ADDITIVELY — it reads the existing file, skips if already present (idempotent),
appends, and never overwrites another key or an existing hook. `--check` is a
dry-run. `repolens init` calls install() by default when the repo is a Claude Code
repo (a `.claude/` dir exists). Because Claude Code runs all SessionStart hooks in
parallel (deduped), appending is safe. Stdlib-only (json).
"""

from __future__ import annotations

import json
import pathlib

from . import root as _root

__all__ = ["command", "snippet", "install"]

# The SessionStart command this hook runs (the change-detector; see module docstring).
COMMAND = "repolens refresh"


def command() -> str:
    """The SessionStart command string this hook runs."""
    return COMMAND


def _group() -> dict:
    return {"hooks": [{"type": "command", "command": COMMAND}]}


# ═══════════════════════════════════════════════════════════════
# snippet()
# ═══════════════════════════════════════════════════════════════
# The paste-ready settings.json fragment (default output — zero writes).
# ═══════════════════════════════════════════════════════════════
def snippet() -> str:
    frag = {"hooks": {"SessionStart": [_group()]}}
    return (
        "Add this to your .claude/settings.json (repo-scoped) so an agent's rule map "
        "stays fresh every session:\n\n" + json.dumps(frag, indent=2)
    )


# ═══════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════
# Additively merge our SessionStart command into <root>/.claude/settings.json.
# Idempotent (skips if a 'repolens refresh' hook already exists), preserves every
# existing key and hook, never overwrites. check=True is a dry-run (no write).
# On an unparseable existing file, refuses to touch it and returns the snippet.
# ═══════════════════════════════════════════════════════════════
def install(root: pathlib.Path, check: bool = False) -> str:
    target = _root.claude_dir(root) / "settings.json"
    data: dict = {}
    if target.is_file():
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("settings.json is not a JSON object")
        except (ValueError, OSError):
            return (
                f"⚠ {target} exists but couldn't be parsed — not touching it. "
                "Add the hook by hand:\n\n" + snippet()
            )

    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        return f"⚠ 'hooks' in {target} is not an object — not touching it."
    session_start = hooks.setdefault("SessionStart", [])
    if not isinstance(session_start, list):
        return f"⚠ hooks.SessionStart in {target} is not a list — not touching it."

    # Idempotent: already installed if any existing SessionStart command runs refresh.
    for grp in session_start:
        for h in (grp or {}).get("hooks", []) if isinstance(grp, dict) else []:
            if "repolens refresh" in str(h.get("command", "")):
                return f"already installed in {target} (no change)."

    if check:
        return (
            f"would append this SessionStart hook to {target} "
            f"(existing hooks preserved):\n\n" + json.dumps(_group(), indent=2)
        )

    session_start.append(_group())
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return f"installed SessionStart hook → {target}"
