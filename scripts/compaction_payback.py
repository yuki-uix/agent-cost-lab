#!/usr/bin/env python3
"""E3: how many turns does it take to recover the cost of one compaction?

Reads a capture, finds a `/compact`, and compares what the session actually
cost against what the same turns would have cost without it.

    .venv/bin/python scripts/compaction_payback.py [--path data/raw/capture-03.jsonl]

**The counterfactual is a model, and it is stated rather than hidden.** What the
session would have cost un-compacted cannot be observed. What can be observed is
every turn that actually happened after the compaction, and how many tokens the
compaction removed. The model holds the work fixed and puts the removed tokens
back:

*   the same turns happen, with the same new content per turn — this is what a
    payback question presumes, and without it there is nothing to compare
*   every post-compaction turn would have read `cut` more tokens from cache
*   the compaction turn itself would instead have been an ordinary turn: full
    context read, no summary rewritten

The first of those is an assumption about behaviour; the other two are
arithmetic on measured numbers. The one free parameter — what an ordinary turn
would have written on the compaction turn — is swept across the observed range
rather than picked, and the answer does not move.

**Which way the behavioural assumption pushes.** After a compaction the model
sees less context, so it may work worse and need *more* turns — turns that would
not have happened in the un-compacted session. Holding the turns fixed therefore
credits the compacted run with work it might not have got for free, and makes
the un-compacted side look more expensive than it would have been. The number
this produces is a **lower bound**: the real payback is at least this long.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcostlab.pricing import cost, load_rates  # noqa: E402
from agentcostlab.providers import Usage, normalise  # noqa: E402

COMPACT_MARKER = "<command-name>/compact"


def _seed(record: dict) -> str:
    messages = (record.get("request_body") or {}).get("messages") or []
    raw = json.dumps(messages[0] if messages else None, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:8]


def _text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _context(usage: dict) -> int:
    """Prefix size after this turn: what was read plus what was written."""
    return (usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0))


def find_compaction(rows: list[dict]) -> tuple[list[dict], list[dict]] | None:
    """The lineage carrying a /compact marker, and the one it continues from.

    Compaction starts a conversation with a new first message, so the two are
    separate lineages to the proxy — that is #42's accepted blind spot, and the
    reason this has to be found by marker rather than by threading.
    """
    groups: dict[str, list[dict]] = {}
    for record in rows:
        groups.setdefault(_seed(record), []).append(record)

    for seed, group in groups.items():
        head = (group[0].get("request_body") or {}).get("messages") or []
        if not any(COMPACT_MARKER in _text(m) for m in head[:6]):
            continue
        if len(group) < 3:
            continue  # too short to say anything about payback
        started = group[0]["t_start"]
        # The lineage it continues: the one that was live immediately before.
        before = [g for s, g in groups.items()
                  if s != seed and len(g) >= 2 and g[-1]["t_start"] < started]
        if not before:
            continue
        return max(before, key=lambda g: g[-1]["t_start"]), group
    return None


def payback(pre: list[dict], post: list[dict], model_key: str,
            turn1_write: int, rates) -> tuple[int | None, float, float, float]:
    """(turns until cumulative cost crosses, saving at the end, one-off, per-turn)."""
    context_before = _context(pre[-1]["usage"])
    cut = context_before - _context(post[0]["usage"])

    actual = counterfactual = 0.0
    crossed = None
    one_off = 0.0
    for turn, record in enumerate(post, start=1):
        usage = normalise("anthropic", record["usage"])
        actual += cost(usage, model_key, rates)
        if turn == 1:
            hypothetical = Usage(cache_read=context_before,
                                 input_uncached=usage.input_uncached,
                                 output=usage.output, cache_write_1h=turn1_write)
            # Computed here rather than rebuilt afterwards: the reported one-off
            # and the crossing point must come from the same arithmetic, or the
            # two drift and the report quotes a number the curve never used.
            one_off = cost(usage, model_key, rates) - cost(hypothetical, model_key, rates)
        else:
            hypothetical = Usage(cache_read=usage.cache_read + cut,
                                 input_uncached=usage.input_uncached,
                                 output=usage.output,
                                 cache_write_5m=usage.cache_write_5m,
                                 cache_write_1h=usage.cache_write_1h)
        counterfactual += cost(hypothetical, model_key, rates)
        if crossed is None and actual < counterfactual:
            crossed = turn

    per_turn = cut * rates[model_key].cache_read / 1e6
    return crossed, counterfactual - actual, one_off, per_turn


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default="data/raw/capture-03.jsonl", type=Path)
    ap.add_argument("--model", default="anthropic/claude-opus-5")
    args = ap.parse_args()

    if not args.path.exists():
        print(f"no capture at {args.path}")
        sys.exit(1)
    rows = [json.loads(l) for l in args.path.read_text().splitlines() if l.strip()]

    found = find_compaction(rows)
    if not found:
        print("no compaction found in this capture; nothing to measure.")
        print("A capture qualifies when a /compact runs mid-session and the")
        print("conversation continues for enough turns afterwards to cross over.")
        sys.exit(1)
    pre, post = found

    models = {r.get("model") for r in pre + post}
    if len(models) > 1:
        print(f"the two lineages span several models {models}; pricing would be wrong.")
        sys.exit(1)

    rates = load_rates()
    context_before = _context(pre[-1]["usage"])
    context_after = _context(post[0]["usage"])
    cut = context_before - context_after

    print(f"capture: {args.path}   model: {args.model}")
    print(f"  before the compaction: {len(pre)} turns, context {context_before:,}")
    print(f"  after:                 {len(post)} turns, context {context_after:,}")
    print(f"  cut: {cut:,} tokens = {cut / context_before:.0%}")
    print(f"  still read from cache on the compaction turn: "
          f"{post[0]['usage'].get('cache_read_input_tokens', 0):,}"
          "   <- not a cold start")
    print()

    # The one free parameter, swept across what was actually observed rather
    # than chosen: what an ordinary turn would have written instead.
    writes = [r["usage"].get("cache_creation_input_tokens", 0) for r in pre[1:]]
    sweep = sorted({0, int(statistics.median(writes)), max(writes)}) if writes else [0]
    print("turns to payback, over the observed range of that parameter:")
    results = []
    for w in sweep:
        turns, saving, one_off, per_turn = payback(pre, post, args.model, w, rates)
        results.append(turns)
        print(f"  counterfactual first-turn write {w:>6,}  ->  "
              f"{turns} turns, ${saving:.4f} saved by turn {len(post)}")
    print()
    _, _, one_off, per_turn = payback(pre, post, args.model, sweep[len(sweep) // 2], rates)
    print(f"  one-off cost of compacting: ${one_off:.4f}")
    print(f"  saving per later turn:      ${per_turn:.4f}"
          f"   ({cut:,} fewer cached tokens at ${rates[args.model].cache_read}/MTok)")
    print()
    lo, hi = min(r for r in results if r), max(r for r in results if r)
    span = f"{lo}" if lo == hi else f"{lo}-{hi}"
    print(f"PAYBACK: {span} turns   (n=1 — one compaction, one session)")
    print("Cost only. Whether the compacted session does worse work is a")
    print("separate axis this does not measure.")


if __name__ == "__main__":
    main()
