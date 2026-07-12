"""repolens.env — a tiny, OS-aware, present-only toolchain probe.

`repolens env` emits one compact line — the OS plus the PRESENT tools (with
versions) from a configured allowlist — fresh, for a SessionStart hook. It never
enumerates absent tools (absence is the default) and never hardcodes a toolchain:
the list comes from `[env].tools` in config, auto-seeded from the repo's manifests
at `init` (detect_stack). This is "own your context window": detect + inject, don't
hand-maintain a static fact-list that drifts and is wrong on the other machine.
Stdlib-only; the version probe can never hang or crash the caller.
"""

from __future__ import annotations

import pathlib
import platform
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor

__all__ = ["probe_env", "detect_stack"]

_OS = {"Darwin": "macOS", "Windows": "Windows", "Linux": "Linux"}
# First dotted-number run, NOT preceded by a digit/dot — so a `v` prefix
# (node `v25.8.0`) or trailing build hash (`v1.5.4 08e34c447b`) can't fool it.
# A plain `\b\d` anchor fails here: there's no word boundary between `v` and `25`,
# so it would skip the real version and grab a later token (`8.0`, `5.4`).
_VERSION_RE = re.compile(r"(?<![\d.])(\d+\.\d+(?:\.\d+)*)")

# manifest filename -> tool it implies (git is always probed)
_MANIFESTS = {
    "pyproject.toml": "python",
    "requirements.txt": "python",
    "setup.py": "python",
    "package.json": "node",
    "go.mod": "go",
    "Cargo.toml": "cargo",
    "Gemfile": "ruby",
}


# ═══════════════════════════════════════════════════════════════
# _version()
# ═══════════════════════════════════════════════════════════════
# Best-effort version token from `<tool> --version`. Captures BOTH
# streams (some tools print to stderr), times out, and NEVER raises —
# a tool that hangs/errors/prints nothing parseable just yields "".
# ═══════════════════════════════════════════════════════════════
def _version(tool: str) -> str:
    try:
        r = subprocess.run(
            [tool, "--version"],
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
        m = _VERSION_RE.search((r.stdout or "") + " " + (r.stderr or ""))
        return m.group(1) if m else ""
    except Exception:  # noqa: BLE001 — a probe must never take down the caller
        return ""


# ═══════════════════════════════════════════════════════════════
# probe_env()
# ═══════════════════════════════════════════════════════════════
# One compact line: the OS + each PRESENT tool from config['env_tools']
# (with a version when detectable, present-without-version otherwise).
# Absent tools are omitted. Order follows the configured list. The
# per-tool `--version` probes run CONCURRENTLY, so total wall-time is
# bounded by the slowest single tool (a heavy one like streamlit), not
# the sum — a SessionStart hook must stay fast regardless of list size.
# ═══════════════════════════════════════════════════════════════
def probe_env(config: dict) -> str:
    os_label = _OS.get(platform.system(), platform.system() or "unknown")
    rel = platform.release()
    parts = [f"{os_label} {rel}".strip()]
    present = [t for t in config.get("env_tools", []) if shutil.which(t)]
    if present:
        with ThreadPoolExecutor(max_workers=min(8, len(present))) as ex:
            versions = ex.map(_version, present)  # order-preserving
        parts += [f"{tool} {ver}".strip() for tool, ver in zip(present, versions)]
    return "[env] " + " · ".join(parts)


# ═══════════════════════════════════════════════════════════════
# detect_stack()
# ═══════════════════════════════════════════════════════════════
# Infer the repo's toolchain from its manifests (pyproject -> python,
# package.json -> node, ...). Always includes git. Returns a de-duped,
# order-stable list used by `init` to seed [env].tools — so the probe
# reflects THIS repo's real stack, not a generic guess.
# ═══════════════════════════════════════════════════════════════
def detect_stack(root: pathlib.Path) -> list[str]:
    tools = ["git"]
    for manifest, tool in _MANIFESTS.items():
        if (root / manifest).is_file() and tool not in tools:
            tools.append(tool)
    return tools
