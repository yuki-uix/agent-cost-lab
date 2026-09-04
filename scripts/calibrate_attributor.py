#!/usr/bin/env python3
"""Calibrate the prefix-diff attributor against the captured cache signal.

Reads ``data/raw/capture.jsonl`` and, for every record whose
``injected_previous_message_id`` points at a previous record in the file, runs
``attribute()`` over that pair and compares the result against the official
"did the cache break" signal.

Two ground truths, two questions, deliberately not folded together (collapsing
them once scored "the API cannot tell you" as "nothing changed"):

*   *whether* the cache broke — ``ledger.broke_cache``, a conservation identity
    over provider-reported usage (``curr.cache_read < prev.cache_read +
    prev.cache_write``);
*   *which component* broke — ``diagnostics.cache_miss_reason.type`` (Anthropic's
    ``*_changed`` reasons), only when the API named one.

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

from agentcostlab.ledger import broke_cache as ledger_broke  # noqa: E402
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


INCONCLUSIVE = ("unavailable", "previous_message_not_found")


def official_component(curr: dict) -> str | None:
    """The component Anthropic blames, or ``None`` when it did not name one.

    ``unavailable`` and ``previous_message_not_found`` mean *the API could not
    determine a reason*. Mapping them through ``OFFICIAL_TO_COMPONENT`` yielded
    ``None`` — the same value that means "nothing changed" — so a verdict
    meaning "I cannot tell you" was scored as one meaning "no divergence", and
    the attributor's correct answer on capture-02 record 61 was charged against
    it as an over-report. They are excluded now, not counted as negatives.
    """
    diagnostics = curr.get("diagnostics")
    if not isinstance(diagnostics, dict):
        return None
    reason = diagnostics.get("cache_miss_reason")
    if not isinstance(reason, dict):
        return None
    return OFFICIAL_TO_COMPONENT.get(reason.get("type"))


def official_status(curr: dict) -> str:
    """``conclusive`` / ``inconclusive`` / ``clean`` / ``absent`` / ``unrecorded``.

    ``unrecorded`` and ``absent`` are kept apart deliberately. A record from
    before proxy.py grew `diagnostics_present` cannot say whether upstream
    returned the key; a record with it set to False says upstream did not. The
    first version of this function collapsed them, which is the same "unknown
    folded into no" that #25 added the field to prevent and #26 reports as
    UNDECIDABLE. All 63 pairs of the 2026-08-18 capture landed in `absent`,
    reading as "Anthropic sent nothing" when the truth is "the proxy did not
    record it yet".
    """
    present = curr.get("diagnostics_present")
    diagnostics = curr.get("diagnostics")
    if not isinstance(diagnostics, dict):
        if present is None:
            return "unrecorded"
        return "clean" if present else "absent"
    reason = diagnostics.get("cache_miss_reason")
    if not isinstance(reason, dict):
        return "inconclusive"
    kind = reason.get("type")
    return "inconclusive" if kind in INCONCLUSIVE else "conclusive"


def _refuse_single_class(label: str, buckets: Counter) -> bool:
    """Print a refusal instead of a rate a constant would match. True if refused."""
    if len(buckets) >= 2:
        return False
    only, n = next(iter(buckets.items()))
    print(f"  DEGENERATE GROUND TRUTH — no {label} rate reported.")
    print(f"    all {n} comparable pairs are `{only}`;"
          f" a constant-`{only}` answer scores {n}/{n} = 100.0%.")
    return True


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
    pairs = pair_by_previous(records)
    status = Counter(official_status(c) for _, c in pairs)

    print(f"records: {len(records)}   pairs (by injected_previous_message_id): {len(pairs)}")
    print("official diagnostics on those pairs:")
    for kind in ("conclusive", "inconclusive", "clean", "absent", "unrecorded"):
        if status[kind]:
            print(f"  {kind:14s} {status[kind]}")
    print()

    # Two different ground truths, two different questions. Collapsing them is
    # what produced the mis-scored pair: the ledger knows WHETHER the cache
    # broke on every pair, the official reason knows WHICH component — and only
    # when it names one.

    print("=== did the cache break?  (ground truth: the usage ledger) ===")
    ledger = [(p, c, b) for p, c in pairs if (b := ledger_broke(p, c)) is not None]
    print(f"  comparable pairs: {len(ledger)}")
    if not ledger:
        print("  no pair carries usage on both sides; nothing to compare.")
    else:
        buckets = Counter("break" if b else "no break" for *_, b in ledger)
        for k, v in buckets.most_common():
            print(f"    {k:10s} {v}")
        if not _refuse_single_class("break/no-break", buckets):
            agree = wrong = 0
            misses = []
            for prev, curr, broke in ledger:
                d = attribute(prev["request_body"], curr["request_body"])
                mine = d is not None and not d.suppressed
                if mine == broke:
                    agree += 1
                else:
                    wrong += 1
                    misses.append((prev, curr, broke, d))
            # Per class, never blended. 49/49 on 48 no-breaks and one break is
            # a number the majority class wrote; quoting it as "100% accurate"
            # is the metric error this repo exists to correct, and one already
            # made once here.
            hits = Counter()
            for prev, curr, broke in ledger:
                d = attribute(prev["request_body"], curr["request_body"])
                if (d is not None and not d.suppressed) == broke:
                    hits["break" if broke else "no break"] += 1
            for cls in ("break", "no break"):
                total = buckets[cls]
                if not total:
                    continue
                print(f"  {cls:9s} {hits[cls]}/{total} = {hits[cls]/total:.1%}"
                      + (f"   <- n={total}, not a rate" if total <= 5 else ""))

            # Order must not move this. #24 made suppression independent of the
            # segment order; asserting it here keeps that honest on real data.
            moved = [perm for perm in itertools.permutations(COMPONENTS)
                     if any((lambda d: d is not None and not d.suppressed)(
                         attribute(p["request_body"], c["request_body"], order=perm)) != b
                         for p, c, b in ledger)]
            print(f"  segment orders that change this verdict: {len(moved)} of 120"
                  + ("" if not moved else "   <- suppression is order-dependent again"))
            for prev, curr, broke, d in misses[:5]:
                print(f"    MISS {prev.get('response_id')} -> {curr.get('response_id')}"
                      f"  ledger={'break' if broke else 'no break'}"
                      f"  mine={'break' if d and not d.suppressed else 'no break'}")
            if wrong == 0:
                print("  NOTE: nothing disagreed. Per the standing rule that is"
                      " grounds for auditing")
                print("        this script for special-casing, not for"
                      " celebrating the numbers.")
    print()

    print("=== which component?  (ground truth: official cache_miss_reason) ===")
    conclusive = [(p, c, official_component(c)) for p, c in pairs
                  if official_status(c) == "conclusive"]
    if not conclusive:
        print(f"  0 conclusive verdicts"
              + (f" ({status['inconclusive']} inconclusive, excluded rather than"
                 " scored as negatives)" if status["inconclusive"] else "")
              + " — nothing to compare.")
        print("  Needs a capture where the API names a reason: switch model,")
        print("  /compact, add or remove an MCP server, or edit the system prompt.")
        return

    results = []
    for perm in itertools.permutations(COMPONENTS):
        agree = disagree = 0
        for prev, curr, official in conclusive:
            d = attribute(prev["request_body"], curr["request_body"], order=perm)
            mine = None if (d is None or d.suppressed) else d.component
            agree, disagree = (agree + 1, disagree) if mine == official else (agree, disagree + 1)
        results.append((agree / (agree + disagree), agree, disagree, perm))
    results.sort(key=lambda r: (-r[0], r[1]))
    print(f"  comparable pairs: {len(conclusive)}")
    if len({r[0] for r in results}) == 1:
        rate, agree, _, _ = results[0]
        print(f"  agreement: {agree}/{len(conclusive)} = {rate:.1%}"
              f"  (all 120 segment orders tie)")
    else:
        print("  agreement by segment order:")
        for rate, agree, _, perm in results:
            print(f"    {rate:6.1%}  ({agree}/{len(conclusive)})  {' -> '.join(perm)}")
        print("  NOTE: the orders differ, so this ranks a hypothesis on the same")
        print("        data it is scored against. Report the spread, not the best.")


if __name__ == "__main__":
    main()
