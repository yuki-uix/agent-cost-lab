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


def _broke_cache(record: dict, by_response_id: dict) -> bool:
    """Did this turn demonstrably lose the cache, judged only by the ledger?

    Independent of `diagnostics`, so it can be used as a witness against it.
    Requires the predecessor to be in this capture and to have read cached
    tokens: without that there is nothing to have lost. An empty or absent
    usage is "not recorded", not "read zero" — record 46 of the 2026-08-18
    capture has `usage: {}` and is not a cache break.
    """
    prev = by_response_id.get(record.get("injected_previous_message_id"))
    usage, prev_usage = record.get("usage"), (prev or {}).get("usage")
    if not usage or not prev_usage:
        return False
    return (usage.get("cache_read_input_tokens", 0) == 0
            and prev_usage.get("cache_read_input_tokens", 0) > 0)


def check(rows: list[dict]) -> tuple[list[str], list[str], dict]:
    ok, bad = [], []
    undecidable = None
    served = [r for r in rows if r.get("status_code") == 200]
    errs = [r for r in rows if r.get("error")]
    with_usage = [r for r in served if r.get("usage")]
    threaded = [r for r in rows if r.get("injected_previous_message_id")]
    by_response_id = {r["response_id"]: r for r in rows if r.get("response_id")}
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
    # A client that cancels mid-stream leaves no usage and nothing is wrong.
    # Only a request whose body was read to the end and still has no usage
    # indicates the ledger losing data. Captures predating stream_complete fall
    # back to tolerating a small fraction.
    lost = [r for r in served if not r.get("usage") and r.get("stream_complete", False)]
    aborted = [r for r in served if not r.get("usage") and not r.get("stream_complete", False)]
    legacy = all("stream_complete" not in r for r in served)
    if legacy:
        gate(len(aborted) <= max(1, len(served) // 20),
             f"usage recorded on {len(with_usage)}/{len(served)} served requests"
             " (pre-stream_complete capture, judged by fraction)")
    else:
        gate(not lost,
             f"usage recorded on {len(with_usage)}/{len(served)} served requests"
             f", {len(aborted)} client-aborted"
             + ("" if not lost else f", {len(lost)} lost after a complete stream  <- ledger failure"))
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
    # Did upstream return a `diagnostics` key at all?
    #
    # This gate used to read `"diagnostics" in r`, which is true for every
    # record ever written: proxy.py initialises the key to None when it builds
    # the record, before the request is even sent. It could not fail. Worse,
    # both parsers stored `msg.get("diagnostics")`, so "upstream omitted the
    # key" and "upstream said null" arrived here as the same None — and those
    # mean opposite things: beta header dead, versus compared and cache hit.
    #
    # proxy.py now records `diagnostics_present` separately. Captures made
    # before that carry None and cannot be judged either way; say so rather
    # than passing them.
    # Judged only over turns where a reply was possible at all: served, and
    # parsed far enough to have usage. A 429 or an aborted stream never reached
    # the parser, so its `diagnostics_present` is None for a reason that has
    # nothing to do with the beta header.
    answerable = [r for r in threaded
                  if r.get("status_code") == 200 and r.get("usage")]
    recorded = [r for r in answerable if r.get("diagnostics_present") is not None]
    unrecorded = [r for r in answerable if r.get("diagnostics_present") is None]
    replied = [r for r in recorded if r["diagnostics_present"]]

    # Whether the beta is alive is decided by evidence, never by a rate.
    #
    # #26 asked for a fraction threshold to replace `bool(replied)`, and that
    # remedy does not hold up: it assumes a healthy session carries the key on
    # nearly every answerable turn, and NO capture has ever carried
    # `diagnostics_present` at all, so nothing establishes that. If the API
    # returns the key only when it has something to say, then replied/answerable
    # IS the miss rate — low is healthy — and any floor on it condemns good
    # data. Captures cost the operator a working session; three have already
    # been discarded, and a false FAIL here inverts this file's whole purpose.
    #
    # The usage ledger can witness it without knowing the semantics. A turn that
    # demonstrably lost the cache should have had something to report:
    witnesses = [r for r in recorded
                 if not r["diagnostics_present"] and _broke_cache(r, by_response_id)]
    if replied:
        gate(True, f"beta header alive: a diagnostics key came back on"
                   f" {len(replied)}/{len(recorded)} answerable turns")
    elif witnesses:
        gate(False, f"{len(witnesses)} turns lost the cache and still carried no"
                    " diagnostics key  <- beta header never took effect")
    elif recorded:
        undecidable = ("beta effectiveness UNDECIDABLE: no answerable turn"
                       " carried a diagnostics key, and none of them lost the"
                       " cache either — a silent beta and a clean session look"
                       " identical from here")
    # Reported whenever ANY answerable turn predates the field, not only when
    # they all do. Judging 1 turn and staying silent about the other 10 reads as
    # full coverage of a capture that is 90% unjudgeable.
    if unrecorded:
        undecidable = (f"beta effectiveness UNDECIDABLE on {len(unrecorded)}"
                       f"/{len(answerable)} answerable turns: they predate"
                       " diagnostics_present, and a null diagnostics is"
                       " indistinguishable from an absent one")

    # "Key returned" is not "verdict obtained", and a verdict is not a miss.
    # A session with zero misses is legitimate data — 1.2 asks what share of
    # turns diverge, and a clean session is its denominator. Demanding a
    # divergence would push whoever is capturing to re-run until something
    # breaks, biasing the very number the data is for.
    #
    # The 30% threshold and its denominator are NOT chosen here. predictions.md
    # locked "if unavailable / previous_message_not_found exceeds 30% *of
    # turns*" before any experiment ran, so the denominator is threaded turns.
    # Recomputing it over some other population after seeing the data is the
    # exact move that file exists to prevent.
    INCONCLUSIVE = ("previous_message_not_found", "unavailable")
    NO_REASON = "no reason returned"

    def _inconclusive_kind(r: dict) -> str | None:
        """Which inconclusive condition this turn is in, or None.

        Returns the label rather than a bool so the printed message is built
        from the same source as the predicate. When these were two independent
        pieces of text the message named two of the three counted conditions,
        and a reader tallying by hand could not reproduce the number.
        """
        d = r.get("diagnostics")
        if not isinstance(d, dict):
            return None         # null diagnostics is a clean hit, not a failure
        reason = d.get("cache_miss_reason")
        if reason is None:
            return NO_REASON
        kind = (reason or {}).get("type")
        return kind if kind in INCONCLUSIVE else None

    KINDS = " / ".join((*INCONCLUSIVE, NO_REASON))

    # Counted over `threaded`, not over `rows`. Iterating all rows against a
    # threaded denominator let the ratio exceed 1 (a first turn carrying an
    # inconclusive verdict entered the numerator but not the denominator) —
    # the same mixed-population error this file was just rewritten to remove.
    inconclusive = [r for r in threaded if _inconclusive_kind(r)]
    over = bool(threaded) and len(inconclusive) > 0.30 * len(threaded)
    gate(not over,
         f"{len(inconclusive)}/{len(threaded)} threaded turns came back {KINDS}"
         + ("  <- over the 30% kill criterion in predictions.md" if over else ""))

    gate(len(main) >= 10,
         f"largest lineage has {len(main)} turns"
         + ("" if len(main) >= 10 else "  <- need a longer session"))

    # What this capture can and cannot answer. One "USABLE" bit was too coarse:
    # a clean session fully supports 1.2 and supports 1.1 and #10 not at all,
    # and printing a single verdict hid a capture with zero official verdicts
    # behind ten green lines.
    # `verdicts` includes `unavailable` and `previous_message_not_found`, which
    # this same file defines as the comparison having failed. Counting them as
    # support would green-light 1.1 and #10 on a capture holding zero usable
    # reasons — the wrong-population error this rewrite exists to remove.
    conclusive = [v for v in verdicts if v not in INCONCLUSIVE]
    has_verdict = bool(conclusive)
    _why = (f"needs >=1 conclusive official verdict, has {len(conclusive)}"
            + (f" ({len(verdicts) - len(conclusive)} inconclusive)"
               if len(verdicts) != len(conclusive) else ""))
    supports = [
        ("E1 1.2  divergence rate", bool(threaded),
         f"{len(threaded)} threaded turns; a clean session is legitimate data"),
        ("E1 1.1  miss-cause distribution", has_verdict, _why),
        ("#10     calibration vs official", has_verdict, _why),
    ]
    stats = {"lineages": len(lineages), "main_turns": len(main),
             "threaded": len(threaded), "verdicts": collections.Counter(verdicts),
             "n_verdicts": len(conclusive), "supports": supports,
             "undecidable": undecidable}
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
    if stats["undecidable"]:
        print(f"  ----  {stats['undecidable']}")
    print(f"\n  lineages={stats['lineages']}  main_lineage_turns={stats['main_turns']}")
    # Printed unconditionally. When this was inside `if stats["verdicts"]`, the
    # one number that mattered — zero — was the one number that never appeared.
    print(f"  official verdicts obtained: {stats['n_verdicts']}"
          f" of {stats['threaded']} threaded turns"
          + (f"  {dict(stats['verdicts'])}" if stats["verdicts"] else ""))
    if not stats["n_verdicts"] and stats["threaded"]:
        print("      zero verdicts is consistent with a clean session AND with a"
              " dead beta header;")
        print("      diagnostics_present (proxy.py) is what tells them apart.")

    print("\n  supports:")
    for label, yes, why in stats["supports"]:
        print(f"    {'YES' if yes else 'NO '}  {label:34s} {why}")

    if bad:
        print("\nNOT USABLE — do not spend analysis time on this")
        return 2
    usable = [label for label, yes, _ in stats["supports"] if yes]
    blocked = [label for label, yes, _ in stats["supports"] if not yes]
    print("\nUSABLE for " + ", ".join(l.split("  ")[0] for l in usable)
          + ("" if not blocked else
             "  —  not for " + ", ".join(l.split("  ")[0] for l in blocked)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
