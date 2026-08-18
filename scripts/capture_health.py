"""Is this capture usable for E1, or was the session wasted?

Three captures have already been thrown away — the proxy wasn't forwarding, then
gzip silently emptied the ledger, then lineages were cross-threaded. Every time
it was found by reading the file afterwards, after someone had spent real
working time producing it. This says so immediately.

    python scripts/capture_health.py data/raw/capture.jsonl
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agentcostlab.proxy import _lineage_key  # noqa: E402


def load(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def check(rows: list[dict]) -> tuple[list[str], list[str], dict]:
    ok, bad = [], []
    served = [r for r in rows if r.get("status_code") == 200]
    errs = [r for r in rows if r.get("error")]
    with_usage = [r for r in served if r.get("usage")]
    threaded = [r for r in rows if r.get("injected_previous_message_id")]
    verdicts = [r["diagnostics"]["cache_miss_reason"]["type"]
                for r in rows
                if isinstance(r.get("diagnostics"), dict)
                and isinstance(r["diagnostics"].get("cache_miss_reason"), dict)]

    lineages = collections.defaultdict(list)
    for i, r in enumerate(rows):
        lineages[_lineage_key(r.get("request_body", {}))].append(i)
    main = max(lineages.values(), key=len, default=[])

    # Each gate is a failure mode that actually happened, not a hypothetical.
    def gate(cond: bool, msg: str) -> None:
        (ok if cond else bad).append(msg)

    gate(bool(served), f"{len(served)}/{len(rows)} requests reached upstream")
    gate(len(with_usage) == len(served),
         f"usage recorded on {len(with_usage)}/{len(served)} served requests"
         + ("" if len(with_usage) == len(served) else "  <- gzip or parse failure"))
    # Instrument failure invalidates the data; a transient transport blip on one
    # request does not — that record is marked and can simply be excluded.
    instrument = [r for r in errs if "parse failed" in (r.get("error") or "")
                  or "ignored identity" in (r.get("error") or "")]
    transport = [r for r in errs if r not in instrument]
    gate(not instrument,
         f"{len(instrument)} instrument failures (parse / encoding)"
         + (f": {instrument[0]['error'][:60]}" if instrument else ""))
    gate(len(transport) <= max(1, len(rows) // 10),
         f"{len(transport)} transport errors, tolerable up to {max(1, len(rows) // 10)}")

    # The gate that would have caught the pre-#14 data: a request's
    # previous_message_id must name a response from its OWN lineage. Re-grouping
    # after the fact cannot detect this — only the recorded value can.
    owner = {}
    for i, r in enumerate(rows):
        if r.get("response_id"):
            owner[r["response_id"]] = _lineage_key(r.get("request_body", {}))
    crossed = [i for i, r in enumerate(rows)
               if (p := r.get("injected_previous_message_id"))
               and p in owner
               and owner[p] != _lineage_key(r.get("request_body", {}))]
    gate(not crossed,
         f"{len(crossed)} requests threaded across lineages"
         + ("" if not crossed else f" at {crossed[:5]}  <- captured before the #14 fix"))

    # "Cannot verify" is not "verified". A predecessor outside this file means
    # the proxy restarted mid-capture, or the file was concatenated or truncated
    # — the cross-lineage check above simply skips those rows.
    unverifiable = [i for i, r in enumerate(rows)
                    if (p := r.get("injected_previous_message_id")) and p not in owner]
    gate(len(unverifiable) <= 1,
         f"{len(unverifiable)} requests name a predecessor outside this capture"
         + ("" if len(unverifiable) <= 1 else f" at {unverifiable[:5]}  <- concatenated or restarted"))
    gate(len(threaded) >= 2,
         f"{len(threaded)} requests carried previous_message_id"
         + ("" if len(threaded) >= 2 else "  <- too short to compare anything"))
    # Presence of the field, not presence of a divergence. A session with zero
    # misses is legitimate data — 1.2 asks what share of turns diverge, and a
    # clean session is its denominator. Demanding a verdict would reject it, and
    # push whoever is capturing to re-run until a divergence happens, biasing
    # the very number this data is for. An absent field means the beta header
    # never took effect; null means compared and clean.
    answered = [r for r in rows
                if r.get("injected_previous_message_id") and "diagnostics" in r]
    gate(bool(answered),
         f"{len(answered)} threaded turns carried a diagnostics field"
         + ("" if answered else "  <- beta header never took effect"))
    # "Field present" is not "verdict obtained". These three mean the comparison
    # did not succeed: no stored fingerprint, could not be located, or still
    # running. A capture made entirely of them has zero comparable samples.
    #
    # The 30% threshold is not invented here — it is the kill criterion locked
    # into predictions.md before any experiment ran. Health check and
    # pre-registered plan are deliberately the same number; if an erratum ever
    # revises it there, this must follow.
    INCONCLUSIVE = ("previous_message_not_found", "unavailable")
    inconclusive = [r for r in rows
                    if isinstance(r.get("diagnostics"), dict)
                    and ((r["diagnostics"].get("cache_miss_reason") or {}).get("type")
                         in INCONCLUSIVE
                         or r["diagnostics"].get("cache_miss_reason") is None)]
    gate(not answered or len(inconclusive) <= 0.30 * len(answered),
         f"{len(inconclusive)}/{len(answered)} verdicts inconclusive"
         + ("" if not answered or len(inconclusive) <= 0.30 * len(answered)
            else "  <- over the 30% kill criterion in predictions.md"))

    gate(len(main) >= 10,
         f"largest lineage has {len(main)} turns"
         + ("" if len(main) >= 10 else "  <- need a longer session"))

    stats = {"lineages": len(lineages), "main_turns": len(main),
             "verdicts": collections.Counter(verdicts)}
    return ok, bad, stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", default="data/raw/capture.jsonl", type=Path)
    args = ap.parse_args()
    if not args.path.exists():
        print(f"no capture at {args.path}")
        return 1

    rows = load(args.path)
    ok, bad, stats = check(rows)
    for line in ok:
        print(f"  PASS  {line}")
    for line in bad:
        print(f"  FAIL  {line}")
    print(f"\n  lineages={stats['lineages']}  main_lineage_turns={stats['main_turns']}")
    if stats["verdicts"]:
        print(f"  verdicts: {dict(stats['verdicts'])}")
    print("\n" + ("USABLE for E1" if not bad else "NOT USABLE — do not spend analysis time on this"))
    return 0 if not bad else 2


if __name__ == "__main__":
    raise SystemExit(main())
