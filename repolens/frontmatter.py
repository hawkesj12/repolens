"""repolens.frontmatter — a total, dependency-free frontmatter parser.

Parses the leading ``---\\n … \\n---`` fence into flat ``key -> value`` strings for
sparse EAV indexing. STDLIB-ONLY by design — Python has no stdlib YAML and repolens
takes no dependency, so this is a minimal flat ``key: value`` parser that flattens
shallow list forms and DEGRADES anything nested / complex / malformed to the raw
text blob with no KV rows.

**Total by contract:** it never raises and never evals, so a caller can trust it on
any input. It covers the ~95% of real frontmatter (Jekyll / Hugo / Obsidian /
org-roam / the ~/.claude conventions) that is flat key/value; the rest is still
searchable via the raw block, just not queryable by key.
"""

from __future__ import annotations

import re

__all__ = ["parse_frontmatter", "split_frontmatter"]

_KV = re.compile(r"^([A-Za-z0-9_.\-]+):\s?(.*)$")


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def _flatten_inline_list(v: str) -> str:
    # "[a, b, c]" -> "a, b, c"; malformed brackets just fall through to the caller.
    inner = v.strip()[1:-1]
    parts = [_unquote(p) for p in inner.split(",")]
    return ", ".join(p for p in parts if p)


# ═══════════════════════════════════════════════════════════════
# split_frontmatter()
# ═══════════════════════════════════════════════════════════════
# Return (frontmatter_block, rest). The block is the inner text
# between a leading '---' fence and its closing '---'/'...'. Empty
# block (and the whole text as rest) when there is no valid fence —
# an unclosed fence degrades to "no frontmatter". Never raises.
# ═══════════════════════════════════════════════════════════════
def split_frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith("---"):
        return "", text
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return "", text
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            return "".join(lines[1:i]), "".join(lines[i + 1 :])
    return "", text  # unclosed fence -> degrade to no-frontmatter


# ═══════════════════════════════════════════════════════════════
# parse_frontmatter()
# ═══════════════════════════════════════════════════════════════
# Return (kv, raw_block): kv is a flat {key: value} of the top-level
# scalars + shallow lists; raw_block is the inner frontmatter text (for
# the FTS blob). Nested maps, malformed lines, and comments produce NO
# kv rows (they degrade to text). Total — never raises, never evals.
# ═══════════════════════════════════════════════════════════════
def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    block, _rest = split_frontmatter(text)
    kv: dict[str, str] = {}
    if not block:
        return kv, ""
    lines = block.splitlines()
    i, n = 0, len(lines)
    while i < n:
        line = lines[i].rstrip()
        i += 1
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line[:1] in (" ", "\t"):
            continue  # indented line with no recognized parent -> degrade
        m = _KV.match(line)
        if not m:
            continue  # not `key: value` -> degrade
        key, val = m.group(1), m.group(2).strip()
        if val == "":
            # empty scalar: a block list (- item) or a nested map on indented lines
            items: list[str] = []
            while i < n and lines[i][:1] in (" ", "\t"):
                sub = lines[i].strip()
                i += 1
                if sub.startswith("-"):
                    items.append(_unquote(sub[1:].lstrip()))
                # a nested `subkey: value` line -> ignored (degrade)
            joined = ", ".join(x for x in items if x)
            if joined:
                kv[key] = joined
        elif val.startswith("[") and val.endswith("]"):
            flat = _flatten_inline_list(val)
            if flat:
                kv[key] = flat
        else:
            uq = _unquote(val)
            if uq:
                kv[key] = uq
    return kv, block
