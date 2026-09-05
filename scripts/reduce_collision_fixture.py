#!/usr/bin/env python3
"""Reduce the polluted calibration capture into a committed regression fixture.

``data/raw/capture-calib-1.jsonl`` is the #72/#73 capture: two sessions shared
``messages[0]``, so ``_lineage_key`` merged them into one lineage, and record 13
(28 messages) is followed by record 14 (2 messages) — a splice that the ledger
witnesses as a cache break. It carries real prompts and source paths, so it is
gitignored. This script writes ``fixtures/lineage/collision-calib.json``, which
keeps everything the collision gate reads and replaces every free-text message
body with a placeholder.

Reduction rules:

* Keep ``response_id``, ``injected_previous_message_id``, ``status_code``,
  ``usage`` and ``injection`` verbatim. ``usage`` is what ``ledger.broke_cache``
  reads; the ids and threading are kept so the fixture is a faithful record shape
  and other health gates still behave. ``injection`` is ``i0`` with
  ``detail: null``, so it carries no free text.
* Keep ``len(messages)`` exactly — the splice is "28 -> 2".
* Replace each message's ``content`` with a placeholder, and drop every other
  free-text field (``system``, ``tools``, ``metadata``, ``model``, ``system_prompt``,
  timestamps, byte counts, ``diagnostics``). ``messages[0]`` becomes
  ``"lineage-<n>"`` where ``<n>`` is assigned by first appearance, so the two
  records that shared a first message still collapse to one lineage and the two
  lineages stay distinct; every other message becomes ``"msg-<position>"``.
* The reduced fixture must return the same verdict as the original: one splice,
  ``prev_i=13``, ``curr_i=14``, ``prev_n=28``, ``curr_n=2``, ``harmful=True``.
  The lineage *hash* changes (the placeholder is not the original prompt) — only
  the structure of the verdict is pinned, and it is pinned by a test in
  ``tests/test_capture_health.py``.

Run from the repo root:

    .venv/bin/python scripts/reduce_collision_fixture.py
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agentcostlab.proxy import _lineage_key  # noqa: E402


def _reduce_record(record: dict, lineage_index: int) -> dict:
    body = record.get("request_body") or {}
    messages = body.get("messages") or []
    reduced = []
    for pos, msg in enumerate(messages):
        if isinstance(msg, dict):
            role = msg.get("role", "user")
        else:
            role = "user"
        content = f"lineage-{lineage_index}" if pos == 0 else f"msg-{pos}"
        reduced.append({"role": role, "content": content})
    return {
        "response_id": record.get("response_id"),
        "injected_previous_message_id": record.get("injected_previous_message_id"),
        "status_code": record.get("status_code"),
        "usage": record.get("usage"),
        "injection": record.get("injection"),
        "request_body": {"messages": reduced},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src", nargs="?", type=Path,
                    default=Path("data/raw/capture-calib-1.jsonl"))
    ap.add_argument("dst", nargs="?", type=Path,
                    default=Path("fixtures/lineage/collision-calib.json"))
    args = ap.parse_args()

    rows = [json.loads(l) for l in args.src.read_text().splitlines() if l.strip()]

    # First appearance order, keyed by the real lineage identity. Assigning the
    # placeholder from this keeps the collision structure without copying text.
    lineage_ids: dict[str, int] = {}
    reduced = []
    for r in rows:
        key = _lineage_key(r.get("request_body", {}))
        if key not in lineage_ids:
            lineage_ids[key] = len(lineage_ids)
        reduced.append(_reduce_record(r, lineage_ids[key]))

    payload = {
        "_source": "reduced by scripts/reduce_collision_fixture.py from "
                   "data/raw/capture-calib-1.jsonl (the #72/#73 polluted run)",
        "_expected": "one splice: the merged lineage shrinks 28 -> 2 at records "
                     "13 -> 14, and ledger.broke_cache says the seam is harmful",
        "records": reduced,
    }
    args.dst.parent.mkdir(parents=True, exist_ok=True)
    args.dst.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote {args.dst} ({len(reduced)} records, {len(lineage_ids)} lineages)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
