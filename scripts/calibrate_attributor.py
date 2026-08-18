#!/usr/bin/env python3
"""Calibrate the prefix-diff attributor against the captured cache signal.

Reads ``data/raw/capture.jsonl`` and, for every record whose
``injected_previous_message_id`` points at a previous record in the file, runs
``attribute()`` over that pair and compares the result against the official
"did the cache break" signal.

The official signal comes from whichever source the capture actually recorded:

*   ``diagnostics.cache_miss_reason.type`` (Anthropic's ``*_changed`` reasons)
    when present;
*   otherwise ``usage.cache_read_input_tokens > 0`` — a turn that reads tokens
    from cache did not break the prefix, so it is "no divergence". This is the
    signal available in the 75-record Claude Code capture: it never breaks
    (``cache_read_input_tokens`` climbs monotonically), so every comparable turn
    is "no divergence".

The segment order is a hypothesis, so this still tries *every* permutation of
``COMPONENTS`` and reports which agrees best. Suppression ("did the cache
break") is decided in a fixed cache-layout order, independent of the swept
segment order, so the agreement rate is expected to tie across all permutations;
the sweep still discriminates *which component* to blame first when several
diverge.

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


def load_records(path: Path) -> list[dict]:
    records = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records


def pair_by_previous(records: list[dict]) -> list[tuple[dict, dict]]:
    """Pair each record with the *actual* previous message it names.

    The capture interleaves sub-conversations (main agent, title/summary
    auxiliaries, …) on adjacent lines, so "previous line" is not the previous
    message. ``injected_previous_message_id`` is: it names the exact
    ``response_id`` whose request the cache was compared against.
    """
    by_response_id = {r.get("response_id"): r for r in records if r.get("response_id")}
    pairs = []
    for curr in records:
        ipm = curr.get("injected_previous_message_id")
        if not ipm:
            continue  # first turn of a conversation: nothing to compare
        prev = by_response_id.get(ipm)
        if prev is None:
            continue  # previous message not in this capture
        pairs.append((prev, curr))
    return pairs


def official_signal(curr: dict) -> str | None:
    """"no_divergence", a ``*_changed`` component, or None (no usable signal)."""
    diagnostics = curr.get("diagnostics")
    if diagnostics:
        reason = diagnostics.get("cache_miss_reason")
        if reason and reason.get("type"):
            return reason["type"]
        if reason is not None:
            return "pending"  # cache_miss_reason present but null
        # diagnostics present without a miss reason: cache was hit.
        return "no_divergence"

    usage = curr.get("usage") or {}
    cache_read = usage.get("cache_read_input_tokens", 0)
    cache_creation = usage.get("cache_creation_input_tokens", 0)
    input_tokens = usage.get("input_tokens", 0)
    if cache_read == 0 and cache_creation == 0 and input_tokens == 0:
        # No usage recorded (e.g. a request that errored before billing): the
        # pair carries no ground truth, so it is not comparable.
        return None
    return "no_divergence" if cache_read > 0 else "messages_changed"


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
    pairs = [(p, c, official_signal(c)) for p, c in pair_by_previous(records)]

    has_miss_reason = any(r.get("diagnostics") for r in records)
    signal_name = (
        "diagnostics.cache_miss_reason.type"
        if has_miss_reason
        else "usage.cache_read_input_tokens > 0"
    )

    comparable = [(p, c, s) for p, c, s in pairs if s is not None]
    print(f"records: {len(records)}   pairs (by injected_previous_message_id): {len(pairs)}")
    print(f"official signal: {signal_name}")
    print(f"comparable pairs (signal present): {len(comparable)}")
    print("signal buckets:")
    for bucket, n in Counter(s for *_, s in comparable).most_common():
        print(f"  {bucket:26s} {n}")
    print()

    results = []
    for perm in itertools.permutations(COMPONENTS):
        agree = disagree = 0
        for prev, curr, signal in comparable:
            official = None if signal == "no_divergence" else OFFICIAL_TO_COMPONENT.get(signal)
            mine = attribute(prev["request_body"], curr["request_body"], order=perm)
            mine_comp = None if (mine is None or mine.suppressed) else mine.component
            if mine_comp == official:
                agree += 1
            else:
                disagree += 1
        total = agree + disagree
        rate = agree / total if total else float("nan")
        results.append((rate, agree, disagree, total, perm))

    results.sort(key=lambda r: (-r[0], r[1], r[2]))
    distinct = {r[0] for r in results}
    if len(distinct) == 1:
        rate, agree, disagree, total, perm = results[0]
        print(f"agreement rate: {rate:.1%}  ({agree}/{total})  (all {len(results)} "
              f"segment orders tie — suppression is decided in cache-layout order)")
        best_perm = perm
        best_disagree = disagree
    else:
        print("segment-order agreement rate (comparable pairs only):")
        for rate, agree, disagree, total, perm in results:
            print(f"  {rate:6.1%}  ({agree}/{total})  {' → '.join(perm)}")
        print()
        best_rate, best_agree, best_disagree, best_total, best_perm = results[0]
        print(f"best order: {' → '.join(best_perm)}   "
              f"agreement {best_rate:.1%} ({best_agree}/{best_total})")
        print()

    suppressed = 0
    for prev, curr, _ in comparable:
        mine = attribute(prev["request_body"], curr["request_body"], order=best_perm)
        if mine is not None and mine.suppressed:
            suppressed += 1
    print(f"suppressed (text diverged after the last cache_control block, cache intact): "
          f"{suppressed}")
    print()

    # Disagreement breakdown: what did we report that the cache signal did not,
    # and vice versa? (On a full tie every order disagrees identically, so the
    # choice below is immaterial.)
    print("disagreement breakdown:")
    if best_disagree == 0:
        print("  none")
    else:
        disagree_rows = []
        for prev, curr, signal in comparable:
            official = None if signal == "no_divergence" else OFFICIAL_TO_COMPONENT.get(signal)
            mine = attribute(prev["request_body"], curr["request_body"], order=best_perm)
            mine_comp = None if (mine is None or mine.suppressed) else mine.component
            if mine_comp == official:
                continue
            diverged = set(diverging_components(prev["request_body"], curr["request_body"]))
            if mine_comp is None:
                kind = "missed"
            elif official is None:
                kind = "over-reported"
            elif official in diverged and mine_comp in diverged:
                kind = "both changed"
            elif official in diverged:
                kind = "official-early-only"
            else:
                kind = "mismatch"
            disagree_rows.append((prev, curr, official, mine_comp, sorted(diverged), kind))

        for kind, n in Counter(r[5] for r in disagree_rows).most_common():
            print(f"  {kind:20s} {n}")
        print()
        for prev, curr, official, mine_comp, diverged, kind in disagree_rows:
            prev_id = prev.get("response_id", "?")[:12]
            print(f"  {prev_id} → {curr.get('response_id', '?')[:12]:12s} "
                  f"official={official!s:10s} mine={mine_comp!s:10s} "
                  f"diverged={diverged}  [{kind}]")
        print()

    if args.verbose:
        print("per-pair detail (best order):")
        for prev, curr, signal in comparable:
            mine = attribute(prev["request_body"], curr["request_body"], order=best_perm)
            print(f"  {prev.get('response_id', '?')[:12]:12s} → "
                  f"{curr.get('response_id', '?')[:12]:12s}  {signal:22s}  mine={mine!s}")


if __name__ == "__main__":
    main()
