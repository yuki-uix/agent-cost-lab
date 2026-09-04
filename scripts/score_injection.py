#!/usr/bin/env python3
"""Score an injected capture against the three independent ground truths.

E4's whole design rests on the answer coming from three places that do not share
reasoning:

* **A — the injected cause.** What we deliberately did. Strongest, because it is
  constructed rather than measured, and the *only* source with an opinion below
  component level (which tool, which message index, which field).
* **B — ``ledger.broke_cache``.** A conservation identity over provider-reported
  usage: a break is a *shortfall*, ``curr.cache_read < prev.cache_read +
  prev.cache_write``, where the write term sums all three write buckets of the
  normalised ``Usage``. Reading *more* than expected is not a break — on an
  implicit-cache provider the write side is never reported, so a growing prefix
  reads "more than expected" every healthy turn and only shrinkage counts. Which
  provider's keys to read is decided by the usage's *shape*, not its label. It
  shares no code with the attributor by design; see that module's docstring.
* **C — ``diagnostics.cache_miss_reason``.** Anthropic's own verdict, component
  level only, and absent on providers that do not implement the beta.

Agreement across all three is strong evidence. Any pairwise disagreement is a
finding in its own right and is printed rather than reconciled — the report is
supposed to carry them, not smooth them over.

    .venv/bin/python scripts/score_injection.py data/raw/capture-inject-i1.jsonl

Reads the campaign label the proxy wrote into each record (``injection``), so a
capture taken without an armed fault is rejected instead of being scored as a
baseline it never declared itself to be.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcostlab.attribute import attribute  # noqa: E402
from agentcostlab.ledger import broke_cache as ledger_broke  # noqa: E402

# Which component each fault is *supposed* to be blamed on. This is ground
# truth A at component level; the finer claim (which tool, which message) is
# checked against the injection's own `detail` string, which records what was
# actually touched.
EXPECTED_COMPONENT: dict[str, str | None] = {
    "i0": None,        # baseline: no divergence expected at all
    "i1": "system",
    "i2": "tools",
    "i3": "tools",
    "i4": "model",
    "i5": "messages",  # client-driven (/compact), observed rather than injected
}


def load(path: Path) -> list[dict]:
    with path.open() as fh:
        return [json.loads(line) for line in fh if line.strip()]


def pairs(records: list[dict]) -> list[tuple[dict, dict]]:
    """Consecutive turns of the same lineage, threaded the way the proxy wrote them.

    Uses ``injected_previous_message_id`` rather than file order: a capture can
    interleave lineages, and comparing across two conversations would diff two
    prefixes that were never meant to match — the #14 cross-lineage bug.
    """
    by_id = {r.get("response_id"): r for r in records if r.get("response_id")}
    out = []
    for curr in records:
        prev_id = curr.get("injected_previous_message_id")
        prev = by_id.get(prev_id) if prev_id else None
        if prev is not None:
            out.append((prev, curr))
    return out


def official(record: dict) -> str | None:
    """Component named by C, or ``None`` when C did not speak.

    ``diagnostics: null`` means "compared, cache hit" — a verdict of "nothing
    diverged", not an absent one. That distinction is why the proxy records
    ``diagnostics_present`` separately; conflating them once turned a healthy
    session into an apparently dead instrument.
    """
    if not record.get("diagnostics_present"):
        return None
    diag = record.get("diagnostics")
    if diag is None:
        return "none"
    reason = (diag or {}).get("cache_miss_reason") or {}
    kind = reason.get("type")
    if not kind:
        return "none"
    return kind.removesuffix("_changed")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", type=Path)
    args = ap.parse_args()

    records = load(args.path)
    labels = {json.dumps(r.get("injection"), sort_keys=True) for r in records if r.get("injection")}
    if not labels:
        print(
            f"{args.path}: no `injection` field on any record. This capture does not "
            "declare a campaign arm and cannot be scored as one. Re-capture with "
            "AGENTCOSTLAB_INJECT set (use i0 for the baseline).",
            file=sys.stderr,
        )
        return 2

    fault = json.loads(next(iter(labels)))["id"]
    expected = EXPECTED_COMPONENT.get(fault)
    print(f"capture : {args.path}")
    print(f"fault   : {fault}   expected component: {expected or '(none — baseline)'}")
    print(f"records : {len(records)}")

    tally = Counter()
    disagreements: list[str] = []

    for prev, curr in pairs(records):
        b = ledger_broke(prev, curr)          # B
        if b is None:
            tally["B: not measured"] += 1
            continue
        tally["B: broke" if b else "B: intact"] += 1

        result = attribute(prev.get("request_body"), curr.get("request_body"))
        divs = [d for d in getattr(result, "divergences", result) if not d.suppressed]
        blamed = divs[0].component if divs else None
        fired = bool((curr.get("injection") or {}).get("applied"))

        # A vs attributor, only on turns where the fault actually fired.
        if fired:
            tally[f"A: fired, blamed {blamed}"] += 1
            if expected and blamed != expected:
                disagreements.append(
                    f"  turn {curr.get('injection', {}).get('turn')}: injected {expected}, "
                    f"attributed {blamed}  [{(curr.get('injection') or {}).get('detail')}]"
                )
        elif blamed is not None:
            # A false positive candidate: nothing was injected on this turn, yet
            # the attributor found a break. 4.5 is scored on exactly these.
            tally[f"A: not fired, blamed {blamed}"] += 1

        # C, where available.
        c = official(curr)
        if c is not None:
            agree = (c == "none" and not b) or (c != "none" and blamed == c)
            tally["C: agrees" if agree else "C: disagrees"] += 1
            if not agree:
                disagreements.append(
                    f"  turn {curr.get('injection', {}).get('turn')}: official {c}, "
                    f"attributed {blamed}, ledger broke={b}"
                )

    print("\ntally")
    for k in sorted(tally):
        print(f"  {k:<34} {tally[k]}")

    if disagreements:
        print(f"\ndisagreements ({len(disagreements)}) — these belong in the report, not smoothed over")
        for line in disagreements:
            print(line)
    else:
        print("\nno pairwise disagreement")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
