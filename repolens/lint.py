"""repolens.lint — deterministic corpus hygiene. Zero LLM, milliseconds.

Structural checks (dead relative links, empty files, missing heading, malformed
frontmatter, duplicate titles, staleness) PLUS typed-record validation via
schema (each type's `require` patterns). Reads source files only — never the
index. Config drives the walk scope; nothing repo-specific here.
"""

from __future__ import annotations

import re
import time

from . import index as _index
from . import schema

SEV = {"error": 0, "warn": 1, "info": 2}
_WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:\|[^\]]+)?\]\]")
_MDLINK = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)]+)\)")
_HEADING = re.compile(r"^#{1,6}\s+\S")

__all__ = ["lint", "has_errors"]


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.strip().lower()).strip("-")


def _first_heading(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return ""


def _malformed_frontmatter(text: str) -> bool:
    return text.startswith("---") and re.search(r"\n---\s*(\n|$)", text[3:]) is None


# ═══════════════════════════════════════════════════════════════
# lint()
# ═══════════════════════════════════════════════════════════════
# Walk the markdown corpus (config skip scope) and return findings as
# dicts {severity, check, path, message}. Mechanical checks + per-type
# schema validation. `stale_days` gates the (info-level) staleness check.
# ═══════════════════════════════════════════════════════════════
def lint(root, config: dict, stale_days: int = 180) -> list[dict]:
    files = list(_index._walk(root, config, code=False))
    stems = {p.stem.lower() for p in files}
    abspaths = {p.resolve() for p in files}
    titles: dict[str, list[str]] = {}
    findings: list[dict] = []
    now = time.time()

    def add(sev, check, p, msg):
        findings.append(
            {
                "severity": sev,
                "check": check,
                "path": str(p.relative_to(root)),
                "message": msg,
            }
        )

    for p in files:
        try:
            text = p.read_text(errors="ignore")
        except Exception:
            continue
        rel = str(p.relative_to(root))
        stripped = text.strip()

        if not stripped:
            add("error", "empty-file", p, "file is empty or whitespace-only")
            continue

        title = _first_heading(text)
        if not any(_HEADING.match(ln) for ln in text.splitlines()):
            add(
                "warn",
                "no-heading",
                p,
                "no markdown heading — hard to identify in results",
            )
        elif title:
            titles.setdefault(title, []).append(rel)

        if _malformed_frontmatter(text):
            add(
                "warn",
                "malformed-frontmatter",
                p,
                "opens a '---' frontmatter fence it never closes",
            )

        for m in _MDLINK.finditer(text):
            target = m.group(1).strip().split()[0]
            if re.match(r"^[a-z]+://", target) or target.startswith(
                ("mailto:", "#", "tel:")
            ):
                continue
            local = target.split("#", 1)[0]
            if not local or not local.endswith((".md", ".markdown")):
                continue
            resolved = (p.parent / local).resolve()
            if resolved not in abspaths and not resolved.exists():
                add("warn", "dead-link", p, f"relative link to missing file: {target}")

        for m in _WIKILINK.finditer(text):
            slug = _slug(m.group(1))
            if slug and slug not in stems and m.group(1).strip().lower() not in stems:
                add(
                    "info",
                    "dangling-wikilink",
                    p,
                    f"[[{m.group(1).strip()}]] has no file yet",
                )

        try:
            age = (now - p.stat().st_mtime) / 86400
            if age > stale_days:
                add(
                    "info",
                    "stale",
                    p,
                    f"untouched for {int(age)} days (> {stale_days})",
                )
        except OSError:
            pass

        for sev, check, msg in schema.validate_doc(rel, text, config):
            add(sev, check, p, msg)

    for title, paths in titles.items():
        if len(paths) > 1:
            for pth in paths:
                findings.append(
                    {
                        "severity": "info",
                        "check": "duplicate-title",
                        "path": pth,
                        "message": f"title '{title}' shared by {len(paths)} files",
                    }
                )

    findings.sort(key=lambda f: (SEV[f["severity"]], f["check"], f["path"]))
    return findings


def has_errors(findings: list[dict]) -> bool:
    return any(f["severity"] == "error" for f in findings)
