"""repolens.purpose — portable one-line "what is this file" extractor.

Convention over parsing: ~6 tiny per-extension rules pull the file's own purpose
line (a docstring, a leading comment, an H1) — a hint for the index, not an AST
parse. Everything degrades to the filename; a missing purpose line is never an
error. This is what makes `find` return "what each file is for," not just paths.
"""

from __future__ import annotations

import re

__all__ = ["DOC_MAX_CHARS", "MAX_LEN", "extract_doc", "extract_purpose"]

MAX_LEN = 200  # a purpose line is a hint, not a paragraph
DOC_MAX_CHARS = (
    1500  # the indexed/embedded doc block is a summary surface, not the file
)


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

    m = re.search(r"^\*\*What this is:?\*\*\s*(.+)$", body, re.MULTILINE)
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
        m = re.match(r'(?:"""|\'\'\')(.*?)(?:"""|\'\'\')', head, re.DOTALL)
        if m and m.group(1).strip():
            return _first_sentence(m.group(1).strip().splitlines()[0])
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#!"):
            continue
        if s.startswith(("/**", "/*")):
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
# _comment_block()
# ═══════════════════════════════════════════════════════════════
# The FULL leading documentation block, where _comment_purpose keeps
# only the first sentence: a Python module docstring in its entirety,
# or the contiguous run of leading line-comments (`markers`), or a
# /* ... */ opener. Shebang-safe; letter-less banner/rule lines are
# dropped from the output but don't end the block; the first
# non-comment line does. Returns "" when there's nothing.
# ═══════════════════════════════════════════════════════════════
def _comment_block(text: str, markers: tuple[str, ...], docstring: bool = False) -> str:
    if docstring:
        head = text
        if head.startswith("#!"):
            head = head.split("\n", 1)[1] if "\n" in head else ""
        head = head.lstrip("\n \t")
        m = re.match(r'(?:"""|\'\'\')(.*?)(?:"""|\'\'\')', head, re.DOTALL)
        if m and m.group(1).strip():
            return m.group(1).strip()
    out: list[str] = []
    started = False
    in_c = False  # inside a /* ... */ opener
    for raw in text.splitlines():
        s = raw.strip()
        if not started and not in_c and (not s or s.startswith("#!")):
            continue  # leading blanks / shebang
        if in_c:
            piece = (s.split("*/", 1)[0] if "*/" in s else s).strip(" *")
            if piece and re.search(r"[A-Za-z]", piece):
                out.append(piece)
            if "*/" in s:
                break
            continue
        if s.startswith("/*"):
            started = in_c = True
            piece = (s.split("*/", 1)[0] if "*/" in s else s).lstrip("/*").strip()
            if piece and re.search(r"[A-Za-z]", piece):
                out.append(piece)
            if "*/" in s:
                break
            continue
        hit = next((mk for mk in markers if s.startswith(mk)), None)
        if hit is None:
            break  # first non-comment line (or blank after the block) ends it
        started = True
        cleaned = s[len(hit) :].strip()
        if cleaned and re.search(r"[A-Za-z]", cleaned):
            out.append(cleaned)
    return "\n".join(out).strip()


# ═══════════════════════════════════════════════════════════════
# extract_doc()
# ═══════════════════════════════════════════════════════════════
# The code file's full documentation block for INDEXING (BM25 body +
# embedding), capped at max_chars — the display line stays
# extract_purpose's one-liner. Dispatch mirrors extract_purpose; md
# returns "" (markdown is indexed full-text elsewhere). Never raises.
# ═══════════════════════════════════════════════════════════════
def extract_doc(relpath: str, text: str, max_chars: int = DOC_MAX_CHARS) -> str:
    ext = ("." + relpath.rsplit(".", 1)[-1].lower()) if "." in relpath else ""
    try:
        if ext == ".py":
            block = _comment_block(text, ("#",), docstring=True)
        elif ext in (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs"):
            block = _comment_block(text, ("//",))
        elif ext == ".sql":
            block = _comment_block(text, ("--",))
        elif ext in (".sh", ".bash", ".zsh", ".toml", ".yml", ".yaml", ".rb"):
            block = _comment_block(text, ("#",))
        elif ext == ".json":
            m = re.search(r'"_description"\s*:\s*"([^"]+)"', text)
            block = m.group(1) if m else ""
        else:
            block = ""
    except Exception:  # noqa: BLE001 — a hint extractor must never break the build
        return ""
    return block[:max_chars].rstrip()


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
