"""The health gate is itself a gate, so it needs its own coverage.

Real captures live under data/raw/ and are gitignored, so these use synthetic
records shaped like the three failures that actually occurred.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "capture_health", Path(__file__).resolve().parents[1] / "scripts" / "capture_health.py")
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)


def rec(idx, *, lineage="seed", rid=None, prev=None, status=200, usage=True, error=None):
    return {"status_code": status,
            "usage": {"input_tokens": 1} if usage else None,
            "error": error,
            "response_id": rid or f"msg_{idx}",
            "injected_previous_message_id": prev,
            "request_body": {"model": "m", "messages": [{"role": "user", "content": lineage}]}}


def healthy(n=12):
    rows = [rec(0)]
    for i in range(1, n):
        rows.append(rec(i, prev=f"msg_{i-1}"))
    rows[3]["diagnostics"] = {"cache_miss_reason": {"type": "system_changed"}}
    return rows


def test_a_good_capture_passes():
    ok, bad, _ = health.check(healthy())
    assert not bad, bad


def test_nothing_reached_upstream_fails():
    _, bad, _ = health.check([rec(i, status=None, usage=False) for i in range(12)])
    assert any("reached upstream" in b for b in bad)


def test_usage_lost_to_compression_fails():
    rows = healthy()
    for r in rows:
        r["usage"] = None
    _, bad, _ = health.check(rows)
    assert any("usage recorded" in b for b in bad)


def test_cross_lineage_threading_fails():
    """The pre-#14 failure: a turn threaded onto another conversation's id."""
    rows = healthy()
    rows[5]["request_body"]["messages"] = [{"role": "user", "content": "other-lineage"}]
    _, bad, _ = health.check(rows)
    assert any("threaded across lineages" in b for b in bad)


def test_one_transport_blip_is_tolerated_but_a_parse_failure_is_not():
    rows = healthy()
    rows[2]["error"] = "ConnectTimeout: "
    _, bad, _ = health.check(rows)
    assert not any("transport" in b for b in bad), "a single blip must not void the set"

    rows[4]["error"] = "parse failed: ValueError: x"
    _, bad, _ = health.check(rows)
    assert any("instrument failures" in b for b in bad)


def test_too_short_a_session_fails():
    _, bad, _ = health.check([rec(0), rec(1, prev="msg_0")])
    assert any("longer session" in b for b in bad)


def test_a_session_with_zero_divergences_is_usable():
    """Zero misses is data, not a defect. Rejecting it would push the capturer
    to re-run until something diverges, biasing the divergence rate itself."""
    rows = healthy()
    for r in rows:
        r["diagnostics"] = None          # compared, clean
    del rows[3]["diagnostics"]
    rows[3]["diagnostics"] = None
    _, bad, _ = health.check(rows)
    assert not bad, bad


def test_upstream_never_returning_a_diagnostics_key_fails():
    """Upstream omitting the key means the beta header never took effect.

    The old version of this test popped `diagnostics` off the record and
    asserted the gate caught it — but proxy.py initialises that key when it
    builds the record, so no real capture can ever be missing it. The test
    exercised a shape the instrument cannot produce, and the gate it guarded
    could not fail on real data.
    """
    rows = healthy()
    for r in rows:
        r["diagnostics"] = None
        r["diagnostics_present"] = False
    _, bad, _ = health.check(rows)
    assert any("diagnostics key" in b for b in bad), bad


def test_a_null_diagnostics_with_the_key_present_is_a_clean_hit():
    """null means compared-and-hit, not broken. Proven in the wild: capture
    attempt3 carries null and dict verdicts in one file, and every null record
    has a healthy cache_read_input_tokens."""
    rows = healthy()
    for r in rows:
        r["diagnostics"] = None
        r["diagnostics_present"] = True
    _, bad, _ = health.check(rows)
    assert not bad, bad


def test_a_capture_predating_the_field_is_undecidable_not_passing():
    """Silence is not a pass. Records with no `diagnostics_present` at all
    cannot say whether the beta took effect, and must not claim to."""
    rows = healthy()
    for r in rows:
        r["diagnostics"] = None
        r.pop("diagnostics_present", None)
    ok, bad, stats = health.check(rows)
    assert stats["undecidable"], "a legacy capture must be reported as undecidable"
    assert not any("diagnostics key" in line for line in ok), \
        "must not report the beta as verified when it cannot be"


def test_zero_verdicts_blocks_1_1_and_10_but_not_1_2():
    """One USABLE bit was too coarse: this is the shape of the 2026-08-18
    capture, which supports the divergence rate and nothing that needs an
    official reason."""
    rows = healthy()
    for r in rows:
        r["diagnostics"] = None
        r["diagnostics_present"] = True
    _, bad, stats = health.check(rows)
    assert not bad, bad
    assert stats["n_verdicts"] == 0
    supports = {label.split("  ")[0]: yes for label, yes, _ in stats["supports"]}
    assert supports["E1 1.2"] is True
    assert supports["E1 1.1"] is False
    assert supports["#10"] is False


def test_predecessor_outside_the_capture_is_not_silently_accepted():
    rows = healthy()
    for i in (5, 6, 7):
        rows[i]["injected_previous_message_id"] = f"msg_from_elsewhere_{i}"
    _, bad, _ = health.check(rows)
    assert any("outside this capture" in b for b in bad)


@pytest.mark.parametrize("reason", ["previous_message_not_found", "unavailable", None])
def test_all_inconclusive_verdicts_fail(reason):
    """The field being present is not a verdict being obtained. None of these
    three yielded a comparison, so the capture has zero comparable samples."""
    rows = healthy()
    for r in rows:
        r["diagnostics"] = {"cache_miss_reason": {"type": reason} if reason else None}
    _, bad, _ = health.check(rows)
    assert any("unavailable / previous_message_not_found" in b for b in bad)


def test_a_few_inconclusive_verdicts_are_tolerated():
    """Under the 30% kill criterion the capture is still usable."""
    rows = healthy()
    for r in rows:
        r["diagnostics"] = None
    rows[4]["diagnostics"] = {"cache_miss_reason": {"type": "unavailable"}}
    _, bad, _ = health.check(rows)
    assert not any("over the 30% kill criterion" in b for b in bad), bad


def test_client_aborted_request_is_not_counted_as_lost_usage():
    """Cancelling mid-stream leaves no usage and nothing is wrong. Only a fully
    read body with no usage means the ledger dropped something."""
    rows = healthy()
    rows[4]["usage"] = None
    rows[4]["stream_complete"] = False
    for r in rows:
        r.setdefault("stream_complete", True)
    _, bad, _ = health.check(rows)
    assert not any("usage recorded" in b for b in bad), bad


def test_usage_lost_after_a_complete_stream_fails():
    rows = healthy()
    for r in rows:
        r["stream_complete"] = True
    rows[4]["usage"] = None
    _, bad, _ = health.check(rows)
    assert any("lost after a complete stream" in b for b in bad)


def test_inconclusive_verdicts_do_not_count_as_support():
    """`unavailable` and `previous_message_not_found` mean the comparison did
    not succeed. Counting them as verdicts green-lights 1.1 and #10 on a
    capture holding zero usable reasons — the wrong-population error this
    whole gate was rewritten to remove, reproduced in the replacement."""
    rows = healthy()
    for r in rows:
        r["diagnostics"] = None
        r["diagnostics_present"] = True
    rows[3]["diagnostics"] = {"cache_miss_reason": {"type": "unavailable"}}
    rows[4]["diagnostics"] = {"cache_miss_reason": {"type": "previous_message_not_found"}}

    _, bad, stats = health.check(rows)
    assert not bad, "2 of 11 is under the 30% kill criterion; this is not a failure"
    assert stats["n_verdicts"] == 0, "neither is a usable reason"
    supports = {label.split("  ")[0]: yes for label, yes, _ in stats["supports"]}
    assert supports["E1 1.1"] is False
    assert supports["#10"] is False


def test_the_inconclusive_ratio_cannot_exceed_one():
    """Numerator over all rows, denominator over threaded ones, printed as
    'N/M threaded turns'. A first turn carrying an inconclusive verdict made
    that ratio 12/11."""
    rows = healthy()
    for r in rows:
        r["diagnostics"] = {"cache_miss_reason": None}
        r["diagnostics_present"] = True

    _, bad, _ = health.check(rows)
    line = next(b for b in bad if "threaded turns came back" in b)
    num, denom = line.split(" ")[0].split("/")
    assert int(num) <= int(denom), line
    assert int(denom) == sum(1 for r in rows if r["injected_previous_message_id"])
