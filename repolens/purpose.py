"""repolens.purpose — portable one-line "what is this file" extractor.

Convention over parsing: ~6 tiny per-extension rules pull the file's own purpose
line (a docstring, a leading comment, an H1) — a hint for the index, not an AST
parse. Everything degrades to the filename; a missing purpose line is never an
error. This is what makes `find` return "what each file is for," not just paths.
"""

from __future__ import annotations

import re

__all__ = ["extract_purpose", "MAX_LEN"]

MAX_LEN = 200  # a purpose line is a hint, not a paragraph


def _first_sentence(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip().strip("*_`#>").strip()
    m = re.match(r"(.+?[.!?])(?:\s|$)", s)
    out = m.group(1) if m else s
    return out[:MAX_LEN].rstrip()


# ═══════════════════════════════════════════════════════════════
# _markdown_purpose()
# ═══════════════════════════════════════════════════════════════
# Prefer an explicit "What this is:" line, else the first real prose
# line after the H1 (skipping front-matter, field lines, blockquotes,
# lists, numbered enumerators, tables, and lead-in lines ending ":"),
# else the H1 text.
# ═══════════════════════════════════════════════════════════════
def _markdown_purpose(text: str) -> str:
    body = text
    if body.startswith("---"):
        end = body.find("\n---", 3)
        if end != -1:
            body = body[body.find("\n", end + 1) + 1 :]

    m = re.search(r"^\*\*What this is:?\*\*\s*(.+)$", body, re.M)
    if m:
        return _first_sentence(m.group(1))

    h1 = ""
    for line in body.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            if not h1:
                h1 = s.lstrip("#").strip()
            continue
        if s.startswith(("- ", "* ", "> ", "|", "---", "**", "```")):
            continue
        if re.match(r"^\d+[.)]\s", s):
            continue
        if s.endswith(":"):
            continue
        return _first_sentence(s)
    return _first_sentence(h1) if h1 else ""


# ═══════════════════════════════════════════════════════════════
# _comment_purpose()
# ═══════════════════════════════════════════════════════════════
# First meaningful leading comment for a code file. `markers` = the
# line-comment tokens. Skips shebangs, rule/banner lines (no letters),
# and a redundant filename "title" line (`skip`). Handles a Python
# module docstring (anchored at file start) and a /** */ opener.
# ═══════════════════════════════════════════════════════════════
def _comment_purpose(
    text: str, markers: tuple[str, ...], docstring: bool = False, skip: str = ""
) -> str:
    if docstring:
        head = text
        if head.startswith("#!"):
            head = head.split("\n", 1)[1] if "\n" in head else ""
        head = head.lstrip("\n \t")
        m = re.match(r'(?:"""|\'\'\')(.*?)(?:"""|\'\'\')', head, re.S)
        if m and m.group(1).strip():
            return _first_sentence(m.group(1).strip().splitlines()[0])
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#!"):
            continue
        if s.startswith("/**") or s.startswith("/*"):
            s = s.lstrip("/*").strip()
            if s and re.search(r"[A-Za-z]", s):
                return _first_sentence(s)
            continue
        matched = False
        for mk in markers:
            if s.startswith(mk):
                matched = True
                cleaned = s[len(mk) :].strip()
                if (
                    cleaned
                    and re.search(r"[A-Za-z]", cleaned)
                    and cleaned.lower() != skip.lower()
                ):
                    return _first_sentence(cleaned)
                break
        if not matched and not s.startswith(("/*", "*")):
            break
    return ""


# ═══════════════════════════════════════════════════════════════
# extract_purpose()
# ═══════════════════════════════════════════════════════════════
# Dispatch by extension; return a clean one-liner or "" (caller falls
# back to the filename). Never raises.
# ═══════════════════════════════════════════════════════════════
def extract_purpose(relpath: str, text: str) -> str:
    ext = ("." + relpath.rsplit(".", 1)[-1].lower()) if "." in relpath else ""
    base = relpath.rsplit("/", 1)[-1]
    try:
        if ext in (".md", ".markdown"):
            return _markdown_purpose(text)
        if ext == ".py":
            return _comment_purpose(text, ("#",), docstring=True, skip=base)
        if ext in (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs"):
            return _comment_purpose(text, ("//",), skip=base)
        if ext == ".sql":
            return _comment_purpose(text, ("--",), skip=base)
        if ext in (".sh", ".bash", ".zsh", ".toml", ".yml", ".yaml", ".rb"):
            return _comment_purpose(text, ("#",), skip=base)
        if ext == ".json":
            m = re.search(r'"_description"\s*:\s*"([^"]+)"', text)
            return _first_sentence(m.group(1)) if m else ""
    except Exception:  # noqa: BLE001 — a hint extractor must never break the build
        return ""
    return ""
