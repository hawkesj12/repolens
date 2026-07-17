"""repolens.chunk — section-bounded chunking (stdlib-only).

Whole-doc embedding produces wrong top-1s (a long doc's single vector matches the
wrong sense), so the semantic layer embeds per-chunk. But a blind fixed-size window
splits mid-thought — so the split RESPECTS structure: it breaks on Markdown heading
boundaries first, and a chunk **never crosses a heading**. A section that fits under
`chunk_tokens` (the small ~512-token target that bge-base and other short-passage
retrievers are built for) is one clean chunk; a longer section is packed into
~512-token pieces on natural boundaries (paragraph → line → sentence → word) WITHIN
that section, with a small overlap. The preamble before the first heading is its own
chunk. A doc with no headings falls back to the same recursive packing (never one
giant chunk). Heading detection is fence-aware: a `#` line inside a ```/~~~ code
fence is code, never a section boundary. Code files contribute their module
docstring / leading comment block (purpose.extract_doc), which flows through the
same chunking.

Token count is estimated at ~4 chars/token (no tokenizer dependency).
"""

from __future__ import annotations

import re

__all__ = ["chunk_document", "CHARS_PER_TOKEN"]

# ~4 characters per token is the standard English rule-of-thumb; good enough to size
# chunks under the model context (and the per-section cap) without a real tokenizer.
CHARS_PER_TOKEN = 4

# Separators for the recursive fallback, largest natural boundary first. "" is the
# hard fallback: split on raw char count when a single atom is still over the limit.
_SEPARATORS = ["\n\n", "\n", ". ", " ", ""]

# An ATX Markdown heading: up to 3 leading spaces, 1–6 '#', then a space (CommonMark).
# Matches `# H`, `## SECTION: Foo`, `### bar` — not a bare `#` or a `#tag`.
_HEADING_RE = re.compile(r"^ {0,3}#{1,6}\s")

# A code fence (CommonMark): up to 3 leading spaces, then ``` or ~~~. `#` lines inside
# a fenced block are code comments, not headings — the splitter must not break there.
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


# ═══════════════════════════════════════════════════════════════
# _split_sections()
# ═══════════════════════════════════════════════════════════════
# Split text into heading-delimited sections: each heading line starts
# a new section (the heading stays with its body), and any preamble
# before the first heading is its own section. Heading detection is
# suspended inside ```/~~~ code fences — a `# comment` line in a fenced
# snippet is code, and splitting there shreds the block. Returns []
# when the doc has NO headings, signaling the recursive fallback.
# ═══════════════════════════════════════════════════════════════
def _split_sections(text: str) -> list[str]:
    sections: list[str] = []
    cur: list[str] = []
    saw_heading = False
    in_fence = False
    for line in text.splitlines(keepends=True):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            cur.append(line)
            continue
        if not in_fence and _HEADING_RE.match(line):
            saw_heading = True
            if cur and "".join(cur).strip():
                sections.append("".join(cur))
            cur = [line]
        else:
            cur.append(line)
    if cur and "".join(cur).strip():
        sections.append("".join(cur))
    return sections if saw_heading else []


# ═══════════════════════════════════════════════════════════════
# _atomize()
# ═══════════════════════════════════════════════════════════════
# Break `text` into atomic pieces each <= limit chars, recursing to
# the next-finer separator only when a piece is still too big. Empty
# pieces are dropped. The result is small, boundary-aligned fragments
# the merger then packs into overlapping chunks.
# ═══════════════════════════════════════════════════════════════
def _atomize(text: str, seps: list[str], limit: int) -> list[str]:
    if len(text) <= limit or not seps:
        return [text] if text.strip() else []
    sep = seps[0]
    if sep == "":  # hard fallback — cut on raw char count
        return [
            text[i : i + limit]
            for i in range(0, len(text), limit)
            if text[i : i + limit].strip()
        ]
    if sep not in text:
        return _atomize(text, seps[1:], limit)
    out: list[str] = []
    for part in text.split(sep):
        if not part.strip():
            continue
        if len(part) <= limit:
            out.append(part)
        else:
            out.extend(_atomize(part, seps[1:], limit))
    return out


# ═══════════════════════════════════════════════════════════════
# _merge()
# ═══════════════════════════════════════════════════════════════
# Greedily pack atomic pieces (each already <= limit) into windows
# <= limit chars. When a window fills, seed the next with the trailing
# `overlap` chars of the previous — but TRIM that seed so seed + piece
# still fits, so no emitted chunk ever exceeds the cap (a re-seeded
# window used to overshoot by ~overlap and get truncated by the model).
# ═══════════════════════════════════════════════════════════════
def _merge(pieces: list[str], limit: int, overlap: int) -> list[str]:
    chunks: list[str] = []
    cur = ""
    for p in pieces:
        if not cur:
            cur = p
            continue
        if len(cur) + 1 + len(p) <= limit:
            cur = cur + " " + p
            continue
        chunks.append(cur)  # cur is <= limit; flush it
        room = limit - 1 - len(p)  # chars left for an overlap seed once p is placed
        seed = (cur[-overlap:] if overlap else "") if room > 0 else ""
        if len(seed) > room:
            seed = seed[-room:]  # trim the tail so seed + " " + p <= limit
        cur = (seed + " " + p).strip() if seed else p
    if cur.strip():
        chunks.append(cur)
    return chunks


def _recursive(text: str, limit: int, overlap: int) -> list[str]:
    return _merge(_atomize(text, _SEPARATORS, limit), limit, overlap)


# ═══════════════════════════════════════════════════════════════
# chunk_document()
# ═══════════════════════════════════════════════════════════════
# Section-bounded chunking. Split on headings first (a chunk never
# crosses one); a section within `chunk_tokens` (~512) is one chunk, a
# longer section is packed into ~chunk_tokens pieces WITHIN the section;
# a no-heading doc falls back to recursive packing. Returns [(chunk_ix,
# text), ...]. overlap is a fraction (0.15) applied only within a section's
# sub-split — distinct sections are clean, non-overlapping units.
# ═══════════════════════════════════════════════════════════════
def chunk_document(
    text: str, chunk_tokens: int = 2000, overlap: float = 0.15
) -> list[tuple[int, str]]:
    if not text or not text.strip():
        return []
    cap = max(1, chunk_tokens) * CHARS_PER_TOKEN
    overlap = min(max(overlap, 0.0), 0.49)
    ov = int(cap * overlap)

    sections = _split_sections(text)
    chunks: list[str]
    if not sections:  # no headings → recursive fallback (never one giant chunk)
        chunks = _recursive(text, cap, ov)
    else:
        chunks = []
        for sec in sections:
            if len(sec) <= cap:
                chunks.append(sec.strip())  # whole section = one chunk
            else:
                chunks.extend(_recursive(sec, cap, ov))  # oversized → sub-split
    chunks = [c for c in chunks if c.strip()]
    return list(enumerate(chunks))
