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


def test_missing_diagnostics_field_fails():
    """Absent field means the beta header never took effect — that is a defect."""
    rows = healthy()
    for r in rows:
        r.pop("diagnostics", None)
    _, bad, _ = health.check(rows)
    assert any("diagnostics field" in b for b in bad)


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
    assert any("inconclusive" in b for b in bad)


def test_a_few_inconclusive_verdicts_are_tolerated():
    """Under the 30% kill criterion the capture is still usable."""
    rows = healthy()
    for r in rows:
        r["diagnostics"] = None
    rows[4]["diagnostics"] = {"cache_miss_reason": {"type": "unavailable"}}
    _, bad, _ = health.check(rows)
    assert not any("inconclusive" in b for b in bad), bad
