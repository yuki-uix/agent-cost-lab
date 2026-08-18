#!/usr/bin/env python3
"""Calibrate the prefix-diff attributor against Anthropic's cache_miss_reason.

Reads ``data/raw/capture.jsonl`` (each record has ``request_body`` and the
official ``diagnostics``), runs ``attribute()`` over every pair of adjacent
requests, and compares against ``cache_miss_reason.type``.

The segment order is a hypothesis, so this tries *every* permutation of
``COMPONENTS`` and reports which one agrees best with the official reason. That
result is the deliverable — the attributor's default order must be changed to
match it, not left as a guess.

    .venv/bin/python scripts/calibrate_attributor.py [--path data/raw/capture.jsonl]
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcostlab.attribute import (  # noqa: E402
    COMPONENTS,
    attribute,
    diverging_components,
)

OFFICIAL_TO_COMPONENT = {
    "model_changed": "model",
    "system_changed": "system",
    "tools_changed": "tools",
    "messages_changed": "messages",
}

# Buckets for which the API produced no scoreable comparison (or one we must
# report apart from the agreement rate — AC2).
NON_COMPARABLE = {
    "first_turn",                 # previous_message_id was null: nothing to compare
    "cross_session",              # paired across a session boundary, not a real pair
    "pending",                    # cache_miss_reason null: comparison still running
    "previous_message_not_found", # no stored fingerprint for previous_message_id
    "unavailable",                # API couldn't pinpoint (params change / too long)
}


def load_records(path: Path) -> list[dict]:
    records = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records


def pair_bucket(prev: dict, curr: dict) -> str:
    """What did the official API say about this adjacent pair?"""
    # The proxy records which id it injected; the official comparison is only
    # valid against that exact previous message, not against whatever happens to
    # sit on the previous line.
    if curr.get("injected_previous_message_id") and prev.get("response_id"):
        if curr["injected_previous_message_id"] != prev["response_id"]:
            return "cross_session"

    if not curr.get("injected_previous_message_id"):
        return "first_turn"

    diagnostics = curr.get("diagnostics")
    if diagnostics is None:
        return "no_divergence"      # diagnostics null, and not the first turn
    reason = diagnostics.get("cache_miss_reason")
    if reason is None:
        return "pending"
    signal = reason.get("type")
    return signal if signal else "pending"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", default="data/raw/capture.jsonl")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    path = Path(args.path)
    if not path.exists():
        print(f"no capture file at {path}; nothing to calibrate against.")
        print("Point the proxy at an Anthropic session, then re-run:")
        print("  .venv/bin/python scripts/calibrate_attributor.py --path data/raw/capture.jsonl")
        sys.exit(1)

    records = load_records(path)
    if len(records) < 2:
        print(f"only {len(records)} record(s); need >= 2 to compare adjacent requests.")
        sys.exit(1)

    pairs = []
    for i in range(len(records) - 1):
        prev, curr = records[i], records[i + 1]
        pairs.append((prev, curr, pair_bucket(prev, curr)))

    print(f"records: {len(records)}   adjacent pairs: {len(pairs)}")
    print("official signal buckets:")
    for bucket, n in Counter(b for *_, b in pairs).most_common():
        print(f"  {bucket:26s} {n}")
    print()

    results = []
    for perm in itertools.permutations(COMPONENTS):
        agree = disagree = 0
        for prev, curr, bucket in pairs:
            if bucket in NON_COMPARABLE:
                continue
            official = None if bucket == "no_divergence" else OFFICIAL_TO_COMPONENT[bucket]
            mine = attribute(prev["request_body"], curr["request_body"], order=perm)
            mine_comp = None if mine is None else mine.component
            if mine_comp == official:
                agree += 1
            else:
                disagree += 1
        total = agree + disagree
        rate = agree / total if total else float("nan")
        results.append((rate, agree, disagree, total, perm))

    results.sort(key=lambda r: (-r[0], r[1], r[2]))

    print("segment-order agreement rate (comparable pairs only):")
    for rate, agree, disagree, total, perm in results:
        print(f"  {rate:6.1%}  ({agree}/{total})  {' → '.join(perm)}")
    print()

    best_rate, best_agree, best_disagree, best_total, best_perm = results[0]
    print(f"best order: {' → '.join(best_perm)}   "
          f"agreement {best_rate:.1%} ({best_agree}/{best_total})")
    print(f"AC1 (agreement rate on comparable pairs) = {best_rate:.1%}")
    print()

    # Detailed disagreement breakdown for the best order (AC2).
    print("disagreement breakdown (best order):")
    if best_disagree == 0:
        print("  none")
    else:
        disagree_rows = []
        for idx, (prev, curr, bucket) in enumerate(pairs):
            if bucket in NON_COMPARABLE:
                continue
            official = None if bucket == "no_divergence" else OFFICIAL_TO_COMPONENT[bucket]
            mine = attribute(prev["request_body"], curr["request_body"], order=best_perm)
            mine_comp = None if mine is None else mine.component
            if mine_comp == official:
                continue
            diverged = set(diverging_components(prev["request_body"], curr["request_body"]))
            if mine_comp is None:
                kind = "missed"               # API saw a change we did not
            elif official is None:
                kind = "over-reported"        # we saw a change the API did not
            elif official in diverged and mine_comp in diverged:
                kind = "both changed"         # order/difference in attribution
            elif official in diverged:
                kind = "official-early-only"  # we reported a later divergence
            else:
                kind = "mismatch"
            disagree_rows.append((idx, official, mine_comp, sorted(diverged), kind))

        for kind, n in Counter(r[4] for r in disagree_rows).most_common():
            print(f"  {kind:20s} {n}")
        print()
        for idx, official, mine_comp, diverged, kind in disagree_rows:
            print(f"  pair {idx:3d}  official={official!s:10s} mine={mine_comp!s:10s} "
                  f"diverged={diverged}  [{kind}]")
        print()

    # Known-reasonable differences are not errors, but must be reported apart.
    print("known-reasonable (not counted against AC1):")
    for bucket in ("unavailable", "previous_message_not_found", "pending",
                   "first_turn", "cross_session"):
        n = sum(1 for *_, b in pairs if b == bucket)
        if n:
            print(f"  {bucket:26s} {n}")

    # What did we find in the `unavailable` cases? The docs say params-only
    # changes surface as `unavailable`, so params should dominate.
    unavailable = [(p, c) for p, c, b in pairs if b == "unavailable"]
    if unavailable:
        found = Counter()
        for prev, curr in unavailable:
            mine = attribute(prev["request_body"], curr["request_body"], order=best_perm)
            found["none" if mine is None else mine.component] += 1
        print("  unavailable, broken down by our attribution:")
        for comp, n in found.most_common():
            print(f"    {comp:24s} {n}")

    if args.verbose:
        print("\nper-pair detail (best order):")
        for idx, (prev, curr, bucket) in enumerate(pairs):
            mine = attribute(prev["request_body"], curr["request_body"], order=best_perm)
            print(f"  {idx:3d}  {bucket:26s}  mine={mine!s}")


if __name__ == "__main__":
    main()
