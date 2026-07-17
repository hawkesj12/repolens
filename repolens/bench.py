"""repolens.bench — score hybrid vs lexical `find` on a committed gold set.

Reads a JSONL gold set (one `{"query": ..., "gold": [relpath...], "class": ...}` per
line), runs each query through `find.search()` in BOTH hybrid and lexical modes against
the SAME index, and reports recall@k + MRR per query-class and overall. This is the
reproducible answer to "does the semantic half actually help?" — run `repolens bench` in
a repo whose gold set references its own files.

Metric definitions (rank = 1-based position of the first gold doc in the hit list):
  • recall@k — fraction of queries whose gold doc appears in the top-k hits.
  • MRR      — mean of 1/rank (0 when no gold doc is found), the standard mean
               reciprocal rank.

The caller owns index freshness (`cmd_bench` calls `find.ensure_fresh` first); `run()`
only reads via `find.search()`.
"""

from __future__ import annotations

import json
import pathlib

from . import find, semantic

CLASSES = ("exact", "conceptual", "paraphrase")


# ═══════════════════════════════════════════════════════════════
# load_gold()
# ═══════════════════════════════════════════════════════════════
# Parse a JSONL gold set into a list of {query, gold:[...], class}.
# Blank lines are skipped; a malformed line or a missing key raises
# ValueError naming the 1-based line number.
# ═══════════════════════════════════════════════════════════════
def load_gold(path: str | pathlib.Path) -> list[dict]:
    items: list[dict] = []
    for lineno, line in enumerate(pathlib.Path(path).read_text().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"{path}:{lineno}: invalid JSON ({e})") from e
        if not obj.get("query") or not obj.get("gold"):
            raise ValueError(f"{path}:{lineno}: each line needs 'query' and 'gold'")
        obj.setdefault("class", "conceptual")
        if isinstance(obj["gold"], str):
            obj["gold"] = [obj["gold"]]
        items.append(obj)
    return items


# ═══════════════════════════════════════════════════════════════
# rank_of_gold() / recall_at_k() / reciprocal_rank()
# ═══════════════════════════════════════════════════════════════
# The metric primitives. `hits` is find.search()'s best-first list of
# {relpath,...}; rank is the 1-based position of the first hit whose
# relpath is in the gold set, or None if no gold doc was returned.
# ═══════════════════════════════════════════════════════════════
def rank_of_gold(hits: list[dict], gold) -> int | None:
    goldset = set(gold)
    for i, h in enumerate(hits, 1):
        if h["relpath"] in goldset:
            return i
    return None


def recall_at_k(rank: int | None, k: int) -> bool:
    return rank is not None and rank <= k


def reciprocal_rank(rank: int | None) -> float:
    return 1.0 / rank if rank else 0.0


def _summarize(rows: list[tuple[bool, float]]) -> dict:
    n = len(rows)
    if not n:
        return {"n": 0, "recall": 0.0, "mrr": 0.0}
    return {
        "n": n,
        "recall": sum(1 for rec, _rr in rows if rec) / n,
        "mrr": sum(rr for _rec, rr in rows) / n,
    }


# ═══════════════════════════════════════════════════════════════
# run()
# ═══════════════════════════════════════════════════════════════
# Score every gold query in both modes against the current index and
# return a structured result: per-class + overall recall@k / MRR for
# lexical and hybrid, plus a per-query breakdown. Does NOT rebuild the
# index — the caller ensures freshness.
# ═══════════════════════════════════════════════════════════════
def run(config: dict, gold: list[dict], k: int = 8) -> dict:
    agg = {c: {"lexical": [], "hybrid": []} for c in CLASSES}
    overall = {"lexical": [], "hybrid": []}
    per_query = []
    for item in gold:
        q = item["query"]
        cls = item["class"]
        lex_rank = rank_of_gold(
            find.search(config, q, k, lexical_only=True), item["gold"]
        )
        hyb_rank = rank_of_gold(
            find.search(config, q, k, lexical_only=False), item["gold"]
        )
        for mode, rank in (("lexical", lex_rank), ("hybrid", hyb_rank)):
            pair = (recall_at_k(rank, k), reciprocal_rank(rank))
            if cls in agg:
                agg[cls][mode].append(pair)
            overall[mode].append(pair)
        per_query.append(
            {
                "query": q,
                "class": cls,
                "gold": item["gold"],
                "lexical_rank": lex_rank,
                "hybrid_rank": hyb_rank,
            }
        )
    classes = {
        c: {
            "lexical": _summarize(agg[c]["lexical"]),
            "hybrid": _summarize(agg[c]["hybrid"]),
        }
        for c in CLASSES
        if agg[c]["lexical"]
    }
    return {
        "k": k,
        "n": len(gold),
        "semantic_active": semantic.available(config),
        "classes": classes,
        "overall": {
            "lexical": _summarize(overall["lexical"]),
            "hybrid": _summarize(overall["hybrid"]),
        },
        "per_query": per_query,
    }


# ═══════════════════════════════════════════════════════════════
# format_report()
# ═══════════════════════════════════════════════════════════════
# Render run()'s result as a human table: recall@k (with a 5-block
# bar) + MRR for lexical vs hybrid, per class and overall.
# ═══════════════════════════════════════════════════════════════
def _bar(frac: float) -> str:
    filled = round(frac * 5)
    return "█" * filled + "░" * (5 - filled)


def _recall_cell(frac: float) -> str:
    return f"{frac * 100:3.0f}% {_bar(frac)}"


def format_report(result: dict) -> str:
    k = result["k"]
    lines = []
    if not result["semantic_active"]:
        lines.append(
            "⚠ semantic tier not available — hybrid == lexical "
            "(install 'repolens[semantic]' to benchmark the dense half)"
        )
    lines.append(f"bench: {result['n']} queries · recall@{k} + MRR · lexical → hybrid")
    lines.append(
        f"{'class':<12} {'n':>2}   {'lex R@k':<10} {'hyb R@k':<10}  "
        f"{'lex MRR':>7} {'hyb MRR':>7}   Δmrr"
    )

    def row(name: str, lx: dict, hy: dict) -> str:
        d = hy["mrr"] - lx["mrr"]
        return (
            f"{name:<12} {lx['n']:>2}   {_recall_cell(lx['recall']):<10} "
            f"{_recall_cell(hy['recall']):<10}  {lx['mrr']:>7.3f} {hy['mrr']:>7.3f}   "
            f"{d:+.3f}"
        )

    for c, d in result["classes"].items():
        lines.append(row(c, d["lexical"], d["hybrid"]))
    o = result["overall"]
    lines.append(row("overall", o["lexical"], o["hybrid"]))
    return "\n".join(lines)
