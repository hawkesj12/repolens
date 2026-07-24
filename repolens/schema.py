"""repolens.schema — deterministic, config-driven classifier + typed validation.

Zero AI. A doc's type comes from an explicit frontmatter `type:` if present, else
from its folder (per `.repolens.toml`, recursively, minus `exclude` globs). Each
type may declare `require` — regex patterns a conforming doc must contain (e.g.
`^\\*\\*Date:\\*\\*` for a meeting note, or `^status:` for a frontmatter key). None
of this is repo-specific: the types and their requirements live entirely in config.

Findings are (severity, check, message) tuples; the caller adds the path.
"""

from __future__ import annotations

import fnmatch
import re

from . import SCHEMA_VERSION
from . import root as _root

__all__ = ["SCHEMA_VERSION", "classify", "type_from_folder", "validate_doc"]

# Scaffolding files that live in a typed folder but are not records.
_NON_RECORDS = {"_TEMPLATE.md", "README.md"}


def _types(config: dict | None) -> dict:
    return (config if config is not None else _root.load_config())["types"]


# ═══════════════════════════════════════════════════════════════
# type_from_folder()
# ═══════════════════════════════════════════════════════════════
# Config-driven folder classifier: a doc is type T if it lives under
# T's folder — recursively when the type sets recursive=true — minus
# `exclude` globs (artifacts). None for a non-record or a scaffold.
# ═══════════════════════════════════════════════════════════════
def type_from_folder(relpath: str, config: dict | None = None) -> str | None:
    rel = relpath.replace("\\", "/")
    name = rel.rsplit("/", 1)[-1]
    if name in _NON_RECORDS:
        return None
    parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
    for tname, spec in _types(config).items():
        folder = spec["folder"]
        under = (
            rel.startswith(folder + "/") if spec.get("recursive") else parent == folder
        )
        if not under:
            continue
        if any(
            fnmatch.fnmatch(name, g) or fnmatch.fnmatch(rel, g)
            for g in spec.get("exclude", [])
        ):
            return None
        return tname
    return None


def _frontmatter_type(text: str) -> str | None:
    if not text or not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    m = re.search(r"^type:\s*(.+)$", text[3:end], re.MULTILINE)
    return m.group(1).strip() if m else None


# ═══════════════════════════════════════════════════════════════
# classify()
# ═══════════════════════════════════════════════════════════════
# The full classifier: an explicit frontmatter `type:` wins; else the
# config-driven folder rule. Callers with the doc text use this.
# ═══════════════════════════════════════════════════════════════
def classify(
    relpath: str, text: str | None = None, config: dict | None = None
) -> str | None:
    if text:
        ft = _frontmatter_type(text)
        if ft:
            return ft
    return type_from_folder(relpath, config)


# ═══════════════════════════════════════════════════════════════
# validate_doc()
# ═══════════════════════════════════════════════════════════════
# Validate a doc against its type's `require` regex patterns (from
# config). Returns (severity, check, message) tuples; empty = clean
# or not a typed record. Pure source-file validation, no DB.
# ═══════════════════════════════════════════════════════════════
def validate_doc(
    relpath: str, text: str, config: dict | None = None
) -> list[tuple[str, str, str]]:
    dtype = classify(relpath, text, config)
    if dtype is None:
        return []
    spec = _types(config).get(dtype, {})
    findings: list[tuple[str, str, str]] = []
    for pattern in spec.get("require", []):
        try:
            if not re.search(pattern, text, re.MULTILINE):
                findings.append(
                    (
                        "warn",
                        "missing-field",
                        f"{dtype}: missing required pattern /{pattern}/",
                    )
                )
        except re.error:
            findings.append(
                (
                    "warn",
                    "bad-require-pattern",
                    f"{dtype}: invalid require regex /{pattern}/",
                )
            )
    return findings
