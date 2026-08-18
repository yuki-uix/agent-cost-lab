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
``COMPONENTS`` and reports which agrees best. On the current capture only
``messages`` ever diverges, so the rate is order-independent.

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
    from_official = sum(1 for _, c, _ in comparable if c.get("diagnostics"))
    buckets = Counter(s for *_, s in comparable)
    print(f"records: {len(records)}   pairs (by injected_previous_message_id): {len(pairs)}")
    print(f"official signal: {signal_name}")
    print(f"comparable pairs (signal present): {len(comparable)}")
    print(f"  from official diagnostics: {from_official}"
          f"   from the usage fallback: {len(comparable) - from_official}")
    print("signal buckets:")
    for bucket, n in buckets.most_common():
        print(f"  {bucket:26s} {n}")
    print()

    # A ground truth with one class cannot rank anything. On the 2026-08-18
    # capture every comparable pair is "no_divergence", so a function that
    # always answers "did not break" scores 100% — and that number was reported
    # as the attributor's agreement rate. Refuse to print a rate rather than
    # print one that a constant would match.
    if len(buckets) < 2:
        only, n = next(iter(buckets.items()))
        print("DEGENERATE GROUND TRUTH — no agreement rate reported.")
        print(f"  all {n} comparable pairs are `{only}`.")
        print("  a constant-`{}` attributor scores {}/{} = 100.0% against this,"
              .format(only, n, n))
        print("  so any rate computed here measures nothing about the attributor.")
        if not from_official:
            print("  none of it came from official diagnostics: this capture has 0"
                  " verdicts.")
        print("\nWhat this capture CAN show, run under the default order:")
        d = Counter()
        for prev, curr, _ in comparable:
            mine = attribute(prev["request_body"], curr["request_body"])
            d["no divergence" if mine is None
              else "suppressed (cache intact)" if mine.suppressed
              else f"reported break: {mine.component}"] += 1
        for k, v in d.most_common():
            print(f"  {v:>4}  {k}")
        print("\nTo calibrate against `cache_miss_reason`, capture a session in"
              " which the cache")
        print("actually breaks: switch model, /compact, add or remove an MCP"
              " server, edit the")
        print("system prompt, or idle past the cache TTL.")
        return

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
              f"segment orders tie — only `messages` ever diverges)")
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

    # Disagreement breakdown for the best order: what did we report that the
    # cache signal did not, and vice versa?
    print("disagreement breakdown (best order):")
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
