"""Command-line entry: ``record`` (start a capture) and ``diagnose`` (what it cost).

``record`` is a thin door into ``scripts/capture.sh`` — it keeps that script's
start/status/stop semantics, its never-overwrite protection, and its nohup
detachment, rather than re-implementing them. ``diagnose`` prints a money-first
diagnostic: what the cache saved, what the misses cost, and only then the cause
breakdown (which can be empty, or all ``ambiguous``, on a healthy capture).

The money numbers come from ``analysis.cost_by_cause`` — the same single entrance
the rest of the repo measures through — never recomputed here. The per-model read
breakdown is a display convenience; the authoritative totals are the ones
``cost_by_cause`` returns.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

from .analysis import (
    CauseBreakdown,
    MissingModel,
    NonAnthropicProvider,
    RecordRateMismatch,
    RepaidExceedsCapacity,
    cost_by_cause,
)
from .pricing import AmbiguousCacheWrite, UnverifiedRate, load_rates
from .providers import normalise

ROOT = Path(__file__).resolve().parents[2]
CAPTURE_SH = ROOT / "scripts" / "capture.sh"
RAW_DIR = ROOT / "data" / "raw"

_PER_MTOK = 1_000_000


def _load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _usd(x: float) -> str:
    return f"${x:.6f}"


def _turns(n: int) -> str:
    return "turn" if n == 1 else "turns"


def _median(values: list[int]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    if not n:
        return 0.0
    if n % 2:
        return float(ordered[n // 2])
    return (ordered[n // 2 - 1] + ordered[n // 2]) / 2.0


def _default_capture() -> Path:
    """The capture a bare ``diagnose`` should read: the highest-numbered one.

    Only ``capture-NN.jsonl`` participates, matched on the digits and compared
    numerically — never lexicographically, and never ``capture-attempt*`` or any
    other non-numeric suffix (those are named ``failed`` / ``pre-fix`` waste
    samples that a default diagnose must not silently pick).
    """
    numbered = sorted(
        (int(m.group(1)), p)
        for p in RAW_DIR.glob("capture-*.jsonl")
        if (m := re.fullmatch(r"capture-(\d+)\.jsonl", p.name))
    )
    return numbered[-1][1] if numbered else RAW_DIR / "capture.jsonl"


def _model_stats(records: list[dict], rates) -> list[dict]:
    """Per-model read-token and saving statistics for the money section.

    The rate table here is the same verified one ``cost_by_cause`` just used, and
    the per-model savings sum to ``cost_by_cause``'s ``hit_usd_saved`` — asserted
    by a test, not trusted.
    """
    reads: Counter[str, int] = Counter()
    saved: Counter[str, float] = Counter()
    prefixes: dict[str, list[int]] = {}
    for record in records:
        provider = record.get("provider")
        model = (record.get("request_body") or {}).get("model")
        rate = rates[f"{provider}/{model}"]
        usage = normalise(provider, record.get("usage"))
        reads[model] += usage.cache_read
        saved[model] += usage.cache_read * (rate.input_uncached - rate.cache_read) / _PER_MTOK
        if usage.cache_read:
            prefixes.setdefault(model, []).append(usage.cache_read)

    stats = []
    for model in sorted(reads):
        read_tokens = reads[model]
        rate = rates[f"anthropic/{model}"]
        # A median over an even-sized set is a .5 value. The prefix count shown to
        # the reader is an integer, and the dollar figures have to be reproducible
        # from *that* integer — otherwise "median prefix 100 tokens" is priced at
        # 100.5 and the printed number cannot be rederived. Round once, here, and
        # price from the same integer that is displayed.
        typical = int(round(_median(prefixes.get(model, []))))
        # If the prefix broke once, the typical prefix length would be re-billed
        # somewhere other than the read rate. This is a hypothetical break with no
        # real usage bucket to sweep, so it is a pure rate interval — cheapest
        # bucket (uncached) to most expensive (1h write) — never a point estimate.
        one_break_low = typical * (rate.input_uncached - rate.cache_read) / _PER_MTOK
        one_break_high = typical * (rate.cache_write_1h - rate.cache_read) / _PER_MTOK
        stats.append({
            "model": model,
            "read_tokens": read_tokens,
            "saved": saved[model],
            "typical_prefix": typical,
            "one_break_low": one_break_low,
            "one_break_high": one_break_high,
        })
    return stats


def _loss_totals(breakdown: CauseBreakdown) -> tuple[float, float, int, int]:
    buckets = [*breakdown.by_cause, breakdown.ambiguous, breakdown.unattributed]
    low = sum(c.usd_low for c in buckets)
    high = sum(c.usd_high for c in buckets)
    turns = sum(c.turns for c in buckets)
    tokens = sum(c.repaid_tokens for c in buckets)
    return low, high, turns, tokens


def diagnose_text(records: list[dict]) -> str:
    """The diagnostic text for a capture that ``cost_by_cause`` can price.

    Raises the same refusal errors as ``cost_by_cause`` (unknown/unverified rate,
    missing model, non-Anthropic provider); the command layer turns them into an
    exit code and a next step.
    """
    breakdown = cost_by_cause(records)
    rates = load_rates()
    stats = _model_stats(records, rates)

    low, high, turns, tokens = _loss_totals(breakdown)
    total_read = sum(s["read_tokens"] for s in stats)

    lines: list[str] = []

    # Money first: the headline is always the cache's savings, and the misses'
    # cost is the second line — never a cause table that might be empty.
    lines.append("Money")
    lines.append(f"  cache hits saved   {_usd(breakdown.hit_usd_saved)}"
                 f"  ({total_read:,} read tokens)")
    if turns:
        lines.append(f"  cache misses cost  {_usd(low)} – {_usd(high)}"
                     f"  ({turns} {_turns(turns)}, {tokens:,} tokens paid twice)")
    else:
        lines.append("  cache misses cost  $0.000000  (0 breaks)")

    lines.append("")
    lines.append("Read savings by model")
    for s in stats:
        lines.append(
            f"  {s['model']:<18s} {s['read_tokens']:,} read tokens"
            f" -> {_usd(s['saved'])} saved"
        )
        lines.append(
            f"    one break ~ {_usd(s['one_break_low'])} – {_usd(s['one_break_high'])}"
            f" (median prefix {s['typical_prefix']:,} tokens)"
        )

    lines.append("")
    lines.append("Cause breakdown  (attribution NOT yet validated on a real break"
                 " — #54 fault injection pending)")
    total_break = turns

    by_cause = breakdown.by_cause
    if by_cause:
        for c in by_cause:
            lines.append(
                f"  by_cause:  {c.cause:<10s} {c.turns} {_turns(c.turns)}"
                f" ({_pct(c.turns, total_break)} of {total_break} breaks),"
                f" {c.repaid_tokens:,} tokens, {_usd(c.usd_low)} – {_usd(c.usd_high)}"
            )
    else:
        lines.append("  by_cause:  none")

    amb = breakdown.ambiguous
    if amb.turns:
        cands = ", ".join(amb.candidates)
        lines.append(
            f"  ambiguous: {amb.turns} {_turns(amb.turns)}"
            f" ({_pct(amb.turns, total_break)} of {total_break} breaks),"
            f" {amb.repaid_tokens:,} tokens, {_usd(amb.usd_low)} – {_usd(amb.usd_high)}"
            f"  [candidates, not causes: {cands}]"
        )
    else:
        lines.append("  ambiguous: none")

    unatt = breakdown.unattributed
    if unatt.turns:
        lines.append(
            f"  unattributed: {unatt.turns} {_turns(unatt.turns)}"
            f" ({_pct(unatt.turns, total_break)} of {total_break} breaks),"
            f" {unatt.repaid_tokens:,} tokens, {_usd(unatt.usd_low)} – {_usd(unatt.usd_high)}"
        )
    else:
        lines.append("  unattributed: none")

    if breakdown.not_measured_turns:
        lines.append(f"  not measured: {breakdown.not_measured_turns}"
                     f" {_turns(breakdown.not_measured_turns)}")
    if breakdown.disputed_turns:
        lines.append(f"  disputed: {breakdown.disputed_turns}"
                     f" {_turns(breakdown.disputed_turns)}")

    return "\n".join(lines) + "\n"


def _pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "0%"


def run_diagnose(path: Path) -> tuple[int, str]:
    """Run ``diagnose`` over ``path`` and return ``(exit_code, text)``.

    A capture that cannot be priced returns a non-zero code and prints the
    refusal reason plus a concrete next step — never just "an error".
    """
    if not path.exists():
        return 1, (f"no capture at {path}\n"
                   f"Next step: run 'agentcostlab record' to start one, then "
                   f"'agentcostlab record stop' when done.")

    try:
        rows = _load(path)
    except (OSError, json.JSONDecodeError) as exc:
        return 1, f"could not read {path}: {exc}\n"

    try:
        return 0, diagnose_text(rows)
    except NonAnthropicProvider as exc:
        return 2, (f"refusing to diagnose: {exc}\n"
                   f"Next step: this tool prices Anthropic captures only; the "
                   f"cache-break conservation identity is not yet provider-neutral.")
    except RecordRateMismatch as exc:
        return 2, (f"refusing to diagnose: {exc}\n"
                   f"Next step: add the missing rate to fixtures/pricing.json, "
                   f"verify it against the official pricing page, and set verified: true.")
    except MissingModel as exc:
        return 2, (f"refusing to diagnose: {exc}\n"
                   f"Next step: re-capture with a proxy that records request_body.model "
                   f"on every record.")
    except UnverifiedRate as exc:
        return 2, (f"refusing to diagnose: {exc}\n"
                   f"Next step: check the official pricing page, fill in the numbers, "
                   f"then set verified: true.")
    except RepaidExceedsCapacity as exc:
        return 2, (f"refusing to diagnose: {exc}\n"
                   f"Next step: a turn re-billed more tokens than its own "
                   f"non-cache-read buckets carried, so the conservation identity's "
                   f"premise does not hold — usually an instrument anomaly. Check that "
                   f"turn's usage in this capture.")
    except AmbiguousCacheWrite as exc:
        return 2, (f"refusing to diagnose: {exc}\n"
                   f"Next step: a write arrived without a TTL and the two tiers are "
                   f"priced differently, so it cannot be priced. Check that capture's "
                   f"cache_creation breakdown.")


def _cmd_record(action: str) -> int:
    if not CAPTURE_SH.exists():
        print(f"capture script not found at {CAPTURE_SH}", file=sys.stderr)
        return 1
    # capture.sh defaults its python to `.venv/bin/python` (its own installed
    # venv); a pip-installed CLI must point it at the interpreter it is actually
    # running under, or `record` breaks outside the repo's own checkout.
    return subprocess.run(
        [str(CAPTURE_SH), action],
        env={**os.environ, "ACL_PYTHON": sys.executable},
    ).returncode


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agentcostlab")
    sub = ap.add_subparsers(dest="command", required=True)

    rec = sub.add_parser("record", help="start / check / stop the recording proxy")
    rec.add_argument("action", nargs="?", default="start",
                     choices=["start", "status", "stop"])

    diag = sub.add_parser("diagnose", help="print what a capture cost")
    diag.add_argument("path", nargs="?", default=None, type=Path)

    args = ap.parse_args(argv)

    if args.command == "record":
        return _cmd_record(args.action)

    path = args.path or _default_capture()
    code, text = run_diagnose(path)
    sys.stdout.write(text)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
