"""repolens.log — optional, private JSONL event log for find + embed operations.

Off by default. When `[log].enabled` is set, appends one JSON line per event to
`.repolens/events.jsonl` — inside the gitignored index-cache dir, so the log stays
LOCAL and PRIVATE (never committed, never leaves the machine). Records `find` events
(query, mode, hits, timing) and `embed` events (file, chunk count, model, timing).
The find log especially is useful for growing a real benchmark from the queries you
actually run. Writes NEVER raise: a logging failure must never break find or embed.
"""

from __future__ import annotations

import json
from datetime import datetime


# ═══════════════════════════════════════════════════════════════
# event()
# ═══════════════════════════════════════════════════════════════
# Append one JSON line to .repolens/events.jsonl when [log].enabled.
# A no-op (one dict lookup) when disabled. Timestamp is ISO local time
# (portable — not tied to any one zone). Any failure is swallowed so a
# broken log path can't take down a `find`/embed.
# ═══════════════════════════════════════════════════════════════
def event(config: dict, kind: str, **fields) -> None:
    log = config.get("log") or {}
    if not log.get("enabled"):
        return
    try:
        path = config["index_path"].parent / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "type": kind,
            **fields,
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    # A bad log path (OSError) or an unserializable field (TypeError/ValueError)
    # must never take down the find/embed it was only observing.
    except (OSError, TypeError, ValueError):
        pass
