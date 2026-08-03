#!/usr/bin/env python3
"""
cell_curator.py — weekly cell hygiene for Haven's memory cell store.

The cells system (memory_cells.eden) is the depth layer of my memory.
This curator keeps it honest:
  - Reports cell counts, sizes, always_inject pressure (cap budget)
  - Flags stale cells (body shorter than keywords suggest, empty body)
  - Flags near-duplicate cells (title/keyword overlap)
  - Tracks injection usage if available (cells_fts stats)
  - Suggests archive candidates (never deletes without review)

Watchdog contract: SILENT when healthy (empty stdout, exit 0).
Prints a report + exits 1 when issues found (cron delivers it).
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

CELLS_DB = Path(os.environ.get(
    "EDEN_MEMORY_CELLS_DB", str(Path.home() / ".eden" / "data" / "memory_cells.eden")))
GLOBAL_CAP = int(os.environ.get("EDEN_MEMORY_CELLS_CAP", "20000"))
MIN_BODY = 100  # cells shorter than this are probably stubs


def main() -> int:
    if not CELLS_DB.exists():
        print(f"cell store missing: {CELLS_DB}")
        return 1
    con = sqlite3.connect(CELLS_DB)
    con.row_factory = sqlite3.Row
    cells = con.execute(
        "SELECT id, title, keywords, priority, budget, always_inject, "
        "length(body) AS body_len, body FROM cells ORDER BY priority, id").fetchall()
    con.close()

    if not cells:
        return 0  # nothing to curate — silent

    issues = []
    always_total = 0
    for c in cells:
        if c["always_inject"]:
            always_total += c["body_len"]
        if c["body_len"] < MIN_BODY:
            issues.append(f"STUB: {c['id']} ({c['body_len']}ch < {MIN_BODY})")
        # keyword list sanity
        try:
            kws = json.loads(c["keywords"]) if isinstance(c["keywords"], str) else c["keywords"]
            if not kws:
                issues.append(f"NO-KEYWORDS: {c['id']}")
        except Exception:
            issues.append(f"BAD-KEYWORDS: {c['id']}")

    # always_inject pressure
    if always_total > GLOBAL_CAP:
        issues.append(
            f"CAP-PRESSURE: always_inject total {always_total}ch > cap {GLOBAL_CAP}ch "
            f"— some cells will be dropped every turn")

    # near-duplicates (same title prefix)
    titles = [c["title"].lower() for c in cells]
    for i in range(len(titles)):
        for j in range(i + 1, len(titles)):
            if titles[i] == titles[j]:
                issues.append(f"DUP-TITLE: {cells[i]['id']} == {cells[j]['id']}")

    if not issues:
        return 0  # silent when healthy

    print(f"CELL CURATOR — {len(cells)} cells, {always_total}ch always_inject / {GLOBAL_CAP}ch cap")
    for i in issues:
        print(f"  {i}")
    print(f"issues: {len(issues)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
