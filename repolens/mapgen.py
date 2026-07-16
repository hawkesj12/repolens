"""repolens.mapgen — OPT-IN model-written folder descriptions for the rule's Map.

The dedicated rule's Map is deterministic by default (rulegen renders the folder
tree). When a repo sets `[map].command`, THIS module hands a model the authoritative
folder facts (the indexed folder list + counts) and lets it WRITE a rich "what lives
here" line per folder by reading the folders itself — the same bring-your-own-model
command pattern as enrich: a CLI that takes a prompt on stdin and prints the answer on
stdout (e.g. `claude -p --model sonnet`).

Discipline — repolens stays the OWNER of the rule file. This module only PRODUCES the
folder-bullet body; rulegen frames it (heading, delimiters, the deterministic DB
block), splices it, writes atomically, and stamps the map-key. On ANY failure (no
command, empty/garbage output, timeout, crash) it returns None and the caller falls
back to the deterministic render — a configured command can never leave a broken or
empty rule. The DB-schema block is never sent to the model: it needs no enrichment.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

__all__ = ["render_map_folders"]

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

_INSTRUCTIONS = """\
You are writing the "Map — where things live" section of an AI agent's rule file for \
the repo at {root} (named "{name}"). Another agent will read ONLY your output to \
orient in this repo, so every line must say what a folder actually holds and why it \
matters — grounded in the real files, not a guess.

repolens has already indexed the repo. These are the AUTHORITATIVE folders, with \
indexed file counts. Cover EXACTLY these — no more, no fewer, same order:

{folders}

For EACH folder, read its real contents under {root} (open files; read frontmatter / \
purpose lines) and describe what lives there and its role. For a folder whose items are \
each a distinct invocable thing (skills, commands, workflows), name the headline items \
so the roster stays honest. Never describe a folder by one random file inside it, and \
never invent a folder.

Output ONLY the folder list — nothing else. No heading, no preamble, no closing \
remarks, no code fences. For each folder, emit its header line EXACTLY as given below \
(same order, keep the backtick folder token and the count), then break the description \
into 2–5 INDENTED sub-bullets on the following lines — four spaces then "- " — each a \
short, scannable piece (what lives there; the notable items/groups by name). One idea \
per sub-bullet, never one long run-on sentence. Example:

- `{example_token}` ({example_count})
    - short plain statement of what the folder is
    - a notable group or item, named: foo, bar, baz
    - another grouping, named — one line, scannable
"""


def _folder_facts(snap: dict) -> tuple[str, str, int]:
    """The (unpadded) folder header list for the prompt + a sample token/count for the
    format example. Unpadded because the output is now nested per folder — counts don't
    need column alignment, and clean `folder/` tokens read better as headers."""
    top = sorted(snap["dir_counts"].items(), key=lambda kv: (-kv[1], kv[0]))
    listing = "\n".join(f"- `{d}/` ({cnt})" for d, cnt in top)
    ex_tok, ex_cnt = (f"{top[0][0]}/", top[0][1]) if top else ("folder/", 1)
    return listing, ex_tok, ex_cnt


# ═══════════════════════════════════════════════════════════════
# render_map_folders()
# ═══════════════════════════════════════════════════════════════
# The Map's folder bullets, WRITTEN BY THE MODEL — one rich "what
# lives here" line per indexed folder. Returns the bullet block, or
# None to fall back to rulegen's deterministic render. Never raises.
# ═══════════════════════════════════════════════════════════════
def render_map_folders(snap: dict, root: pathlib.Path, config: dict) -> str | None:
    mp = config.get("map", {}) if isinstance(config.get("map"), dict) else {}
    cmd = mp.get("command", "") if mp.get("enabled", True) else ""
    if not cmd or not snap.get("dir_counts"):
        return None
    listing, ex_tok, ex_cnt = _folder_facts(snap)
    prompt = _INSTRUCTIONS.format(
        root=root,
        name=snap.get("name") or root.name,
        folders=listing,
        example_token=ex_tok,
        example_count=ex_cnt,
    )
    try:
        r = subprocess.run(
            cmd,
            shell=True,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=600,
            cwd=str(root),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = _ANSI.sub("", r.stdout or "").strip()
    # Strip an accidental ```-fence wrapper.
    if out.startswith("```"):
        out = out.split("\n", 1)[1] if "\n" in out else ""
    if out.rstrip().endswith("```"):
        out = out.rstrip()[:-3]
    out = out.strip()
    # Trim any preamble before the first folder bullet (a model sometimes prefixes a
    # "Now I'll write the map" line). This doubles as the sanity gate: no folder
    # bullet at all (an apology, empty output, prose) → fall back to deterministic.
    idx = out.find("- `")
    if idx == -1:
        return None
    return out[idx:].rstrip()
