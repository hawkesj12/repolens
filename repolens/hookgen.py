"""repolens.hookgen — wire repolens's change-detectors into Claude Code hooks.

Two event-driven hooks, no cron:

- **SessionStart → `repolens refresh`** — the cheap deterministic detector. Every
  session it compares the env-key (and, with no `[map].command`, the map-key) to the
  rule and regenerates the Environment (+ deterministic Map) in place on a change;
  unchanged is a ~no-op.
- **SessionEnd → `repolens tidy`** — installed ONLY when `[map].command` is set. On
  the way out it runs enrich (fill-only) then the model-written Map regen, so the
  session that changed the repo rebuilds the map and the next session opens fresh —
  the changer pays, readers never wait. The costly/model work stays off session start.

NON-DESTRUCTIVE by design. `repolens hook` PRINTS the snippet by default (zero
writes). `--install` merges our command(s) into `.claude/settings.json` ADDITIVELY —
reads the existing file, skips any hook already present (idempotent), appends, and
never overwrites another key or hook. `--check` is a dry-run. `repolens init` calls
install() by default in a Claude Code repo. Because Claude Code dedupes and runs each
event's hooks in parallel, appending is safe. Stdlib-only (json).
"""

from __future__ import annotations

import json
import pathlib

from . import root as _root

__all__ = ["command", "snippet", "install"]

# The SessionStart command (the deterministic Env/Map detector).
COMMAND = "repolens refresh"
# The SessionEnd command (enrich + model-written Map) — only when [map].command is set.
TIDY_COMMAND = "repolens tidy"


def command() -> str:
    """The SessionStart command string (kept for back-compat callers)."""
    return COMMAND


# ═══════════════════════════════════════════════════════════════
# _hooks_for()
# ═══════════════════════════════════════════════════════════════
# The (event, command) pairs to install for this repo. SessionStart
# refresh always; SessionEnd tidy only when a [map].command opts the
# repo into the model-written map (so the deterministic public default
# installs exactly one hook, unchanged from before the map feature).
# ═══════════════════════════════════════════════════════════════
def _hooks_for(config: dict | None) -> list[tuple[str, str]]:
    hooks = [("SessionStart", COMMAND)]
    if config and config.get("map", {}).get("command"):
        hooks.append(("SessionEnd", TIDY_COMMAND))
    return hooks


def _group(command: str) -> dict:
    return {"hooks": [{"type": "command", "command": command}]}


# ═══════════════════════════════════════════════════════════════
# snippet()
# ═══════════════════════════════════════════════════════════════
# The paste-ready settings.json fragment (default output — zero writes).
# ═══════════════════════════════════════════════════════════════
def snippet(config: dict | None = None) -> str:
    frag: dict = {"hooks": {}}
    for event, cmd in _hooks_for(config):
        frag["hooks"].setdefault(event, []).append(_group(cmd))
    return (
        "Add this to your .claude/settings.json (repo-scoped) so an agent's rule map "
        "stays fresh:\n\n" + json.dumps(frag, indent=2)
    )


def _already(session_list: list, cmd: str) -> bool:
    for grp in session_list:
        for h in (grp or {}).get("hooks", []) if isinstance(grp, dict) else []:
            if cmd in str(h.get("command", "")):
                return True
    return False


# ═══════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════
# Additively merge repolens's hook(s) into <root>/.claude/settings.json.
# Idempotent per (event, command) — skips one already present, preserves
# every existing key and hook, never overwrites. check=True is a dry-run.
# On an unparseable existing file, refuses to touch it and returns the
# snippet. config selects which hooks (see _hooks_for); loaded if omitted.
# ═══════════════════════════════════════════════════════════════
def install(root: pathlib.Path, check: bool = False, config: dict | None = None) -> str:
    if config is None:
        config = _root.load_config(root)
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
                "Add the hook(s) by hand:\n\n" + snippet(config)
            )

    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        return f"⚠ 'hooks' in {target} is not an object — not touching it."

    wanted = _hooks_for(config)
    to_add: list[tuple[str, str]] = []
    for event, cmd in wanted:
        arr = hooks.setdefault(event, [])
        if not isinstance(arr, list):
            return f"⚠ hooks.{event} in {target} is not a list — not touching it."
        if not _already(arr, cmd):
            to_add.append((event, cmd))

    if not to_add:
        return f"already installed in {target} (no change)."

    if check:
        preview = {ev: _group(cmd) for ev, cmd in to_add}
        return f"would add to {target} (existing hooks preserved):\n\n" + json.dumps(
            preview, indent=2
        )

    for event, cmd in to_add:
        hooks[event].append(_group(cmd))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    added = ", ".join(f"{ev}:{cmd}" for ev, cmd in to_add)
    return f"installed hook(s) → {target}  [{added}]"
