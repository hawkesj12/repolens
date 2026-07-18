"""repolens.bench — score grep vs lexical vs hybrid `find` on a committed gold set.

Reads a JSONL gold set (one `{"query": ..., "gold": [relpath...], "class": ...}` per
line), runs each query in THREE modes against the SAME corpus — a literal grep baseline,
lexical `find` (BM25), and hybrid `find` (BM25 + semantic) — and reports recall@k + MRR
per query-class and overall. This is the reproducible answer to "does ranking, and then
the semantic half, actually help?" — the progression grep → BM25 → hybrid — run
`repolens bench` in a repo whose gold set references its own files.

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
import random

from . import find
from . import index as _index
from . import semantic

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
# _grep_corpus() / _grep_hits()
# ═══════════════════════════════════════════════════════════════
# The grep baseline arm: a literal, unranked-tool comparison scored the
# same way as `find`. _grep_corpus reads the SAME files repolens indexes
# (reusing index._walk) ONCE — (relpath, lowercased text) — so the per-
# query cost is only a substring count, not a full corpus re-read (an
# O(queries × corpus_bytes) trap otherwise). _grep_hits then counts case-
# insensitive occurrences of any query term per file (grep -i -c
# semantics), ranks files with >=1 match by total count desc, and returns
# the top-k as {relpath} dicts — truncated to k so rank_of_gold is
# symmetric with find's. Stdlib only, no shell-out to `rg`.
# ═══════════════════════════════════════════════════════════════
def _grep_corpus(config: dict) -> list[tuple[str, str]]:
    root = config["root"]
    max_bytes = config.get("max_file_bytes", 0)
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for is_code in (False, True):
        for p in _index._walk(root, config, code=is_code):
            rel = str(p.relative_to(root))
            if rel in seen:
                continue
            seen.add(rel)
            try:
                if max_bytes and p.stat().st_size > max_bytes:
                    continue
                out.append(
                    (rel, p.read_text(encoding="utf-8", errors="ignore").lower())
                )
            except OSError:
                continue
    return out


def _grep_hits(corpus: list[tuple[str, str]], query: str, k: int) -> list[dict]:
    # Rank primarily by DISTINCT query terms matched, tiebreak by total occurrences — a
    # fair proxy for how a human reads grep output (a file hitting 2 of 3 query words beats
    # one hitting 1 word ten times). Sum-of-raw-counts understated this baseline ~30%.
    terms = [t for t in query.lower().split() if t]
    if not terms:
        return []
    scored: list[tuple[int, int, str]] = []
    for rel, text in corpus:
        distinct = sum(1 for t in terms if t in text)
        if distinct:
            scored.append((distinct, sum(text.count(t) for t in terms), rel))
    scored.sort(key=lambda s: (-s[0], -s[1], s[2]))
    return [{"relpath": rel} for _d, _t, rel in scored[:k]]


# ═══════════════════════════════════════════════════════════════
# run()
# ═══════════════════════════════════════════════════════════════
# Score every gold query in both modes against the current index and
# return a structured result: per-class + overall recall@k / MRR for
# lexical and hybrid, plus a per-query breakdown. Does NOT rebuild the
# index — the caller ensures freshness.
# ═══════════════════════════════════════════════════════════════
ARMS = ("grep", "lexical", "hybrid")


def run(config: dict, gold: list[dict], k: int = 8) -> dict:
    agg: dict[str, dict[str, list[tuple[bool, float]]]] = {
        c: {a: [] for a in ARMS} for c in CLASSES
    }
    overall: dict[str, list[tuple[bool, float]]] = {a: [] for a in ARMS}
    per_query: list[dict] = []
    corpus = _grep_corpus(config)  # read the corpus once, not per query
    for item in gold:
        q = item["query"]
        cls = item["class"]
        ranks = {
            "grep": rank_of_gold(_grep_hits(corpus, q, k), item["gold"]),
            "lexical": rank_of_gold(
                find.search(config, q, k, lexical_only=True), item["gold"]
            ),
            "hybrid": rank_of_gold(
                find.search(config, q, k, lexical_only=False), item["gold"]
            ),
        }
        for mode, rank in ranks.items():
            pair = (recall_at_k(rank, k), reciprocal_rank(rank))
            if cls in agg:
                agg[cls][mode].append(pair)
            overall[mode].append(pair)
        per_query.append(
            {
                "query": q,
                "class": cls,
                "gold": item["gold"],
                "grep_rank": ranks["grep"],
                "lexical_rank": ranks["lexical"],
                "hybrid_rank": ranks["hybrid"],
            }
        )
    classes = {
        c: {a: _summarize(agg[c][a]) for a in ARMS}
        for c in CLASSES
        if agg[c]["lexical"]
    }
    return {
        "k": k,
        "n": len(gold),
        "semantic_active": semantic.available(config),
        "classes": classes,
        "overall": {a: _summarize(overall[a]) for a in ARMS},
        "ci": _delta_ci(per_query),
        "per_query": per_query,
    }


# ═══════════════════════════════════════════════════════════════
# _bootstrap_ci() / _delta_ci()
# ═══════════════════════════════════════════════════════════════
# A DETERMINISTIC (fixed-seed) percentile bootstrap 95% CI for the mean of
# paired per-query reciprocal-rank deltas. n=18 is small — a bare point
# mean oversells; ship the interval so a reader sees the uncertainty. Seeded
# so the number is reproducible run-to-run (stdlib random + statistics only).
# ═══════════════════════════════════════════════════════════════
def _bootstrap_ci(
    deltas: list[float], iters: int = 2000, seed: int = 1234
) -> tuple[float, float]:
    n = len(deltas)
    if n == 0:
        return (0.0, 0.0)
    rng = random.Random(seed)
    means = sorted(
        sum(deltas[rng.randrange(n)] for _ in range(n)) / n for _ in range(iters)
    )
    return (round(means[int(0.025 * iters)], 3), round(means[int(0.975 * iters)], 3))


def _delta_ci(per_query: list[dict]) -> dict:
    def paired(a: str, b: str) -> dict:
        d = [
            reciprocal_rank(p[f"{a}_rank"]) - reciprocal_rank(p[f"{b}_rank"])
            for p in per_query
        ]
        mean = round(sum(d) / len(d), 3) if d else 0.0
        return {"delta": mean, "ci95": _bootstrap_ci(d)}

    return {
        "hybrid_minus_grep": paired("hybrid", "grep"),
        "hybrid_minus_lexical": paired("hybrid", "lexical"),
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
            "(reinstall repolens to benchmark the dense half)"
        )
    lines.append(
        f"bench: {result['n']} queries · recall@{k} + MRR · grep → lexical → hybrid"
    )
    lines.append(
        f"{'class':<12} {'n':>2}   {'grep R@k':<10} {'lex R@k':<10} {'hyb R@k':<10}  "
        f"{'grep MRR':>8} {'lex MRR':>7} {'hyb MRR':>7}   Δ(hyb-grep)"
    )

    def row(name: str, gr: dict, lx: dict, hy: dict) -> str:
        d = hy["mrr"] - gr["mrr"]  # total lift over plain grep
        return (
            f"{name:<12} {lx['n']:>2}   "
            f"{_recall_cell(gr['recall']):<10} {_recall_cell(lx['recall']):<10} "
            f"{_recall_cell(hy['recall']):<10}  "
            f"{gr['mrr']:>8.3f} {lx['mrr']:>7.3f} {hy['mrr']:>7.3f}   {d:+.3f}"
        )

    for c, d in result["classes"].items():
        lines.append(row(c, d["grep"], d["lexical"], d["hybrid"]))
    o = result["overall"]
    lines.append(row("overall", o["grep"], o["lexical"], o["hybrid"]))
    hg = result.get("ci", {}).get("hybrid_minus_grep")
    if hg:
        lo, hi = hg["ci95"]
        lines.append(
            f"Δ hybrid−grep MRR {hg['delta']:+.3f}  "
            f"95% CI [{lo:+.3f}, {hi:+.3f}]  (bootstrap, n={result['n']})"
        )
    return "\n".join(lines)
