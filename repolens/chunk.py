"""repolens.chunk — section-bounded chunking (stdlib-only).

Whole-doc embedding produces wrong top-1s (a long doc's single vector matches the
wrong sense), so the semantic layer embeds per-chunk. But a blind fixed-size window
splits mid-thought — so the split RESPECTS structure: it breaks on heading boundaries
first — across the prose formats `doc_exts` indexes: Markdown/AsciiDoc prefix headings
(`#`, `==`) and reStructuredText / setext underline headings (`Title` over `=====`) —
and a chunk **never crosses a heading**. A section that fits under
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

__all__ = ["CHARS_PER_TOKEN", "chunk_document"]

# ~4 characters per token is the standard English rule-of-thumb; good enough to size
# chunks under the model context (and the per-section cap) without a real tokenizer.
CHARS_PER_TOKEN = 4

# Separators for the recursive fallback, largest natural boundary first. "" is the
# hard fallback: split on raw char count when a single atom is still over the limit.
_SEPARATORS = ["\n\n", "\n", ". ", " ", ""]

# A prefix heading: up to 3 leading spaces, then a run of 1–6 '#' (Markdown ATX) or
# '=' (AsciiDoc `== Section`), then whitespace. Matches `# H`, `## SECTION: Foo`,
# `== Git Basics` — not a bare `#`, a `#tag`, or a `======` setext underline (no space).
_PREFIX_HEADING_RE = re.compile(r"^ {0,3}(#{1,6}|={1,6})\s")

# An underline heading (reStructuredText, and Markdown/AsciiDoc setext): a line that is
# ONLY a run of one punctuation char — the chars rst permits as a title adornment. It is
# a heading only when it sits directly under a non-blank TITLE line (checked in the loop),
# which is what separates a real `Title\n=====` from a `---` thematic break after a blank.
_UNDERLINE_RE = re.compile(r"""^ {0,3}([=\-~^"'#*+.:`<>_])\1+\s*$""")

# A code fence (CommonMark): up to 3 leading spaces, then ``` or ~~~. `#` lines inside
# a fenced block are code comments, not headings — the splitter must not break there.
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


# ═══════════════════════════════════════════════════════════════
# _split_sections()
# ═══════════════════════════════════════════════════════════════
# Split text into heading-delimited sections across the prose formats
# doc_exts indexes — Markdown, reStructuredText, AsciiDoc. Detects three
# heading styles so .rst/.adoc are section-bounded like .md, not dumped
# as one blind blob: prefix headings (`#` md, `==` adoc) AND underline
# headings (`Title` over `=====`/`-----`, used by rst and md-setext).
# Each heading starts a new section (heading stays with its body); preamble
# before the first heading is its own section. Suspended inside ```/~~~
# fences (a `#` comment isn't a boundary) and inside a leading `---` YAML
# frontmatter block (its closing `---` is not a setext underline). Returns
# [] when the doc has NO headings, signaling the recursive fallback.
# ═══════════════════════════════════════════════════════════════
def _split_sections(text: str) -> list[str]:
    lines = text.splitlines(keepends=True)
    sections: list[str] = []
    cur: list[str] = []
    saw_heading = False
    in_fence = False
    in_frontmatter = False

    def flush() -> None:
        if cur and "".join(cur).strip():
            sections.append("".join(cur))

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Leading YAML frontmatter (--- … ---) at the top of the doc: never a heading,
        # and its closing --- must not be read as a setext underline of the last key.
        if i == 0 and stripped == "---":
            in_frontmatter = True
            cur.append(line)
            continue
        if in_frontmatter:
            cur.append(line)
            if stripped == "---":
                in_frontmatter = False
            continue

        if _FENCE_RE.match(line):
            in_fence = not in_fence
            cur.append(line)
            continue
        if in_fence:
            cur.append(line)
            continue

        # Prefix heading (# / ==): starts a new section outright.
        if _PREFIX_HEADING_RE.match(line):
            saw_heading = True
            flush()
            cur = [line]
            continue

        # Underline heading: this line is an adornment run AND the line above it is a
        # real title (non-blank, not itself an adornment/prefix-heading), with the
        # adornment at least as long as the title — the reStructuredText rule, which
        # also rejects a short `---`/`===` thematic break sitting under a text line.
        if _UNDERLINE_RE.match(line) and cur:
            title = cur[-1]
            if (
                title.strip()
                and not _UNDERLINE_RE.match(title)
                and not _PREFIX_HEADING_RE.match(title)
                and len(stripped) >= len(title.strip())
            ):
                saw_heading = True
                cur.pop()  # the title belongs to the NEW section, not the old one
                flush()
                cur = [title, line]
                continue

        cur.append(line)

    flush()
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
