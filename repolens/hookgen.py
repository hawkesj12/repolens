"""repolens.hookgen — wire the digest/env into a Claude Code SessionStart hook.

NON-DESTRUCTIVE by design. `repolens hook` PRINTS the snippet by default (zero
writes). `--install` merges our command into the repo's `.claude/settings.json`
ADDITIVELY — it reads the existing file, skips if already present (idempotent),
appends, and never overwrites another key or an existing hook. `--check` is a
dry-run. Because Claude Code runs all SessionStart hooks in parallel (deduped),
appending is safe. Stdlib-only (json).
"""

from __future__ import annotations

import json
import pathlib

__all__ = ["command", "snippet", "install"]


def command(with_env: bool) -> str:
    """The SessionStart command string this hook runs."""
    return "repolens digest && repolens env" if with_env else "repolens digest"


def _group(with_env: bool) -> dict:
    return {"hooks": [{"type": "command", "command": command(with_env)}]}


# ═══════════════════════════════════════════════════════════════
# snippet()
# ═══════════════════════════════════════════════════════════════
# The paste-ready settings.json fragment (default output — zero writes).
# ═══════════════════════════════════════════════════════════════
def snippet(with_env: bool = False) -> str:
    frag = {"hooks": {"SessionStart": [_group(with_env)]}}
    return (
        "Add this to your .claude/settings.json (repo-scoped) so an agent gets a "
        "fresh repo map every session:\n\n" + json.dumps(frag, indent=2)
    )


# ═══════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════
# Additively merge our SessionStart command into <root>/.claude/settings.json.
# Idempotent (skips if a 'repolens digest' hook already exists), preserves every
# existing key and hook, never overwrites. check=True is a dry-run (no write).
# On an unparseable existing file, refuses to touch it and returns the snippet.
# ═══════════════════════════════════════════════════════════════
def install(root: pathlib.Path, with_env: bool = False, check: bool = False) -> str:
    target = root / ".claude" / "settings.json"
    data: dict = {}
    if target.is_file():
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("settings.json is not a JSON object")
        except (ValueError, OSError):
            return (
                f"⚠ {target} exists but couldn't be parsed — not touching it. "
                "Add the hook by hand:\n\n" + snippet(with_env)
            )

    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        return f"⚠ 'hooks' in {target} is not an object — not touching it."
    session_start = hooks.setdefault("SessionStart", [])
    if not isinstance(session_start, list):
        return f"⚠ hooks.SessionStart in {target} is not a list — not touching it."

    # Idempotent: already installed if any existing SessionStart command runs digest.
    for grp in session_start:
        for h in (grp or {}).get("hooks", []) if isinstance(grp, dict) else []:
            if "repolens digest" in str(h.get("command", "")):
                return f"already installed in {target} (no change)."

    if check:
        return (
            f"would append this SessionStart hook to {target} "
            f"(existing hooks preserved):\n\n" + json.dumps(_group(with_env), indent=2)
        )

    session_start.append(_group(with_env))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return f"installed SessionStart hook → {target}"
