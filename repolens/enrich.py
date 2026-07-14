"""repolens.enrich — generate frontmatter (description + tags) and code purpose
lines with a model. The one command that WRITES to your source files.

Bring-your-own-model, two providers (stdlib-only, no Python dependency):
  • HTTP — POST to a local server (ollama `/api/generate` by default): `model` + `endpoint`.
  • COMMAND — run any CLI that reads the prompt on stdin and prints the answer, e.g.
    `command = "claude -p --model haiku"` (runs on your Claude subscription — no API
    key, compute off your machine). Takes precedence when set.
`find`/`lint`/`index`/`digest` never touch a model; if none is reachable enrich
degrades to a clear message, never a crash.

Discipline: it only FILLS MISSING fields — never clobbers a hand-written value, and
`--force` regenerates the managed fields while PRESERVING a doc's other frontmatter
keys. It respects `.gitignore` (same walk as the indexer), enriching only committable
files. `--dry` previews.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import urllib.request

from . import frontmatter
from . import index as _index

__all__ = ["enrich_repo"]

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


# ═══════════════════════════════════════════════════════════════
# _ask()
# ═══════════════════════════════════════════════════════════════
# POST a prompt to the configured local model endpoint (ollama's
# /api/generate shape, stream=false) via stdlib urllib. Returns the
# raw response text, or "" on any failure — never raises.
# ═══════════════════════════════════════════════════════════════
def _ask(config: dict, prompt: str) -> str:
    en = config.get("enrich", {})
    # COMMAND provider: run any CLI that takes the prompt on stdin and prints the
    # answer — e.g. `claude -p --model haiku` (runs on your Claude subscription, no
    # API key, compute off your machine). Takes precedence over the HTTP endpoint.
    cmd = en.get("command")
    if cmd:
        try:
            r = subprocess.run(
                cmd,
                shell=True,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=180,
            )
            return r.stdout
        except (OSError, subprocess.SubprocessError):
            return ""
    # HTTP provider (ollama /api/generate shape) — the default.
    payload = json.dumps(
        {"model": en.get("model", "llama3.2"), "prompt": prompt, "stream": False}
    ).encode()
    try:
        req = urllib.request.Request(
            en.get("endpoint", "http://localhost:11434/api/generate"),
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read()).get("response", "")
    except Exception:
        return ""


def _clean(s: str) -> str:
    s = _ANSI.sub("", s).replace("\r", "").strip().strip('"')
    return re.split(r"(?<=[.!?])\s", s, maxsplit=1)[0].strip()  # first sentence


# ═══════════════════════════════════════════════════════════════
# _clean_tags()
# ═══════════════════════════════════════════════════════════════
# 3–6 atomic, lowercase, hyphenated tags — never multi-word phrases
# (the precoordination trap: you can't re-query a compound by one atom).
# ═══════════════════════════════════════════════════════════════
def _clean_tags(s: str) -> str:
    seen: set[str] = set()
    out: list[str] = []
    for raw in re.split(r"[,\n]", _ANSI.sub("", s)):
        t = re.sub(r"[^a-z0-9\- ]", "", raw.strip().lower()).strip().replace(" ", "-")
        if t and t not in seen and 1 < len(t) <= 24:
            seen.add(t)
            out.append(t)
    return ", ".join(out[:6])


def _gen_doc(config: dict, text: str) -> tuple[str, str]:
    instr = (
        "Analyze this document. Output EXACTLY two lines:\n"
        "DESCRIPTION: <one plain sentence, max 18 words, what it contains>\n"
        "TAGS: <3 to 6 lowercase atomic keywords (single word or hyphenated), "
        "comma-separated — never multi-word phrases>\nOutput only those two lines."
    )
    resp = _ask(config, f"{instr}\n\n{text[:2800]}")
    desc = tags = ""
    for line in resp.splitlines():
        if m := re.match(r"\s*DESCRIPTION:\s*(.+)", line, re.I):
            desc = _clean(m.group(1))
        if m := re.match(r"\s*TAGS:\s*(.+)", line, re.I):
            tags = _clean_tags(m.group(1))
    return desc, tags


def _gen_code(config: dict, text: str) -> str:
    instr = (
        "Summarize what this code file does in ONE plain sentence "
        "(max 14 words). Output only the sentence."
    )
    return _clean(_ask(config, f"{instr}\n\n{text[:2800]}"))


def _domain(rel: str) -> str:
    parts = rel.split("/")
    return parts[0] if len(parts) > 1 else ""  # root-level file → no domain


def _has_key(fm: str, key: str) -> bool:
    return bool(re.search(rf"(?m)^{key}:", fm))


# ═══════════════════════════════════════════════════════════════
# _enrich_doc()
# ═══════════════════════════════════════════════════════════════
# Fill the configured missing frontmatter fields (description/tags via
# the model, domain derived) into one markdown file. Never clobbers an
# existing field unless force=True. Returns a summary line, or None.
# ═══════════════════════════════════════════════════════════════
def _enrich_doc(path, rel, config, fields, dry, force) -> str | None:
    text = path.read_text(errors="ignore")
    fm, body = frontmatter.split_frontmatter(text)
    keys = config.get("enrich", {}).get("keys", {})

    def oname(kind: str) -> str:  # the OUTPUT field name for a kind (renamable)
        return keys.get(kind, kind)

    # "missing" is judged against the OUTPUT name — so a repo's own field (whatever
    # it's renamed to) counts as present and isn't duplicated.
    want = {kind: (force or not _has_key(fm, oname(kind))) for kind in fields}
    if not any(want.values()):
        return None
    desc = tags = ""
    if want.get("description") or want.get("tags"):
        desc, tags = _gen_doc(config, text)
    parts = []
    if want.get("description") and desc:
        parts.append(f"{oname('description')}: {desc}")
    if want.get("domain") and (dom := _domain(rel)):
        parts.append(f"{oname('domain')}: {dom}")
    if want.get("tags") and tags:
        parts.append(f"{oname('tags')}: {tags}")
    if not parts:
        return None
    # Merge: keep every existing frontmatter line EXCEPT the keys we're (re)writing,
    # then append the fresh ones. So --force regenerates our fields WITHOUT dropping
    # a doc's other frontmatter keys, and fills-missing keeps what's there.
    written = {p.split(":", 1)[0].strip() for p in parts}
    if fm:
        kept = [
            ln
            for ln in fm.splitlines()
            if ln.strip() and ln.split(":", 1)[0].strip() not in written
        ]
        new = "---\n" + "\n".join([*kept, *parts]) + f"\n---\n{body}"
    else:
        new = "---\n" + "".join(p + "\n" for p in parts) + f"---\n{chr(10)}{text}"
    if not dry:
        path.write_text(new)
    return f"{rel} → {desc or 'tags: ' + tags}"


# ═══════════════════════════════════════════════════════════════
# _enrich_code()
# ═══════════════════════════════════════════════════════════════
# Insert a one-line purpose docstring/comment into a code file that
# lacks one — shebang-safe, never touching unparseable Python or a file
# that already self-describes (unless force). Returns a line, or None.
# ═══════════════════════════════════════════════════════════════
def _enrich_code(path, rel, config, dry, force) -> str | None:
    text = path.read_text(errors="ignore")
    if path.suffix == ".py":
        try:
            if ast.get_docstring(ast.parse(text)) and not force:
                return None
        except SyntaxError:
            return None
    elif re.match(r"^\s*#\s*purpose:", text) and not force:
        return None
    purpose = _gen_code(config, text)
    if not purpose:
        return None
    lines = text.splitlines(keepends=True)
    at = 1 if lines and lines[0].startswith("#!") else 0
    if len(lines) > at and re.match(r"^#.*coding[:=]", lines[at]):
        at += 1
    block = f'"""{purpose}"""\n' if path.suffix == ".py" else f"# purpose: {purpose}\n"
    new = "".join(lines[:at]) + block + "".join(lines[at:])
    if not dry:
        path.write_text(new)
    return f"{rel} → {purpose}"


# ═══════════════════════════════════════════════════════════════
# enrich_repo()
# ═══════════════════════════════════════════════════════════════
# Walk the committable corpus (the indexer's gitignore-respecting walk)
# and fill missing metadata. Returns (doc_lines, code_lines) of what was
# (or would be, when dry) written. Docs get description/domain/tags per
# config['enrich']['fields']; code gets a purpose line.
# ═══════════════════════════════════════════════════════════════
def enrich_repo(
    root, config, dry=False, force=False, docs_only=False, code_only=False
) -> tuple[list[str], list[str]]:
    fields = config.get("enrich", {}).get("fields", ["description", "tags"])
    docs: list[str] = []
    code: list[str] = []
    if not code_only:
        for p in _index._walk(root, config, code=False):
            rel = str(p.relative_to(root))
            if line := _enrich_doc(p, rel, config, fields, dry, force):
                docs.append(line)
    if not docs_only:
        for p in _index._walk(root, config, code=True):
            rel = str(p.relative_to(root))
            if line := _enrich_code(p, rel, config, dry, force):
                code.append(line)
    return docs, code
