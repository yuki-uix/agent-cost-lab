"""The calibration script has twice reported a number that meant nothing.

First `agreement 100.0% (62/62)` on a ground truth with one class, where a
constant "did not break" scores the same. Then, once a real break existed, it
scored that break as an over-report: `unavailable` — the API saying it could
not determine a reason — was mapped to the same value as "nothing changed".

These tests run the real entry point. Importing and re-implementing its logic
would pass just as happily after someone adds a path around it.
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

# Matched loosely on purpose: these tests are about whether a rate is printed
# at all, never about the wording around it.
RATE = re.compile(r"agreement[^\n]*?\d+\.\d%")

spec = importlib.util.spec_from_file_location(
    "calibrate_attributor",
    Path(__file__).resolve().parents[1] / "scripts" / "calibrate_attributor.py")
calib = importlib.util.module_from_spec(spec)
spec.loader.exec_module(calib)


def body(*, system="you are a helper", turns=1):
    """A body carrying a cache_control breakpoint, as Claude Code sends."""
    messages = []
    for i in range(turns):
        messages.append({"role": "user", "content": [
            {"type": "text", "text": f"turn {i}",
             **({"cache_control": {"type": "ephemeral"}} if i == 0 else {})}]})
    return {"model": "claude-sonnet-5", "system": system,
            "tools": [], "messages": messages, "max_tokens": 100}


def rec(idx, *, prev=None, cache_read=5000, b=None):
    return {"response_id": f"msg_{idx}", "injected_previous_message_id": prev,
            "request_body": b or body(turns=idx + 1),
            "usage": {"input_tokens": 10, "cache_read_input_tokens": cache_read,
                      "cache_creation_input_tokens": 0}}


def write(tmp_path, rows) -> Path:
    p = tmp_path / "capture.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return p


def run(monkeypatch, capsys, path) -> str:
    monkeypatch.setattr("sys.argv", ["calibrate_attributor.py", "--path", str(path)])
    calib.main()
    return capsys.readouterr().out


def chain(n=8, **overrides):
    rows = [rec(0)] + [rec(i, prev=f"msg_{i-1}") for i in range(1, n)]
    return rows


def test_single_class_ground_truth_reports_no_rate(tmp_path, monkeypatch, capsys):
    """The shape of the 2026-08-18 capture: the cache never broke, so every
    comparable pair is the same class."""
    out = run(monkeypatch, capsys, write(tmp_path, chain()))
    assert "DEGENERATE GROUND TRUTH" in out
    assert not re.search(RATE, out), "a rate a constant would match must not be printed"


def test_a_partial_break_is_recognised(tmp_path, monkeypatch, capsys):
    """capture-02 record 61's shape. `cache_read` stayed at 155,169 — non-zero,
    so the old `cache_read > 0` rule called it clean while 2,822 tokens were
    being paid for twice. The identity against the predecessor sees it."""
    rows = chain()
    rows[4]["usage"] = {"input_tokens": 10, "cache_read_input_tokens": 5000,
                        "cache_creation_input_tokens": 800}
    # reads the same 5000 back, so the predecessor's 800 were lost
    rows[5]["usage"] = {"input_tokens": 900, "cache_read_input_tokens": 5000,
                        "cache_creation_input_tokens": 0}
    rows[5]["request_body"] = body(system="a different prompt", turns=6)

    out = run(monkeypatch, capsys, write(tmp_path, rows))
    assert "DEGENERATE GROUND TRUTH" not in out, out
    # Reported per class, never blended: a rate over 5 no-breaks and 1 break is
    # a number the majority class wrote.
    assert re.search(r"^\s+break\s+1/1 = ", out, re.M), out
    assert not re.search(r"agreement:\s+\d+/\d+", out.split("which component")[0]), \
        "the ledger section must not print a blended rate"


def test_an_inconclusive_verdict_is_excluded_not_scored_as_a_negative(
        tmp_path, monkeypatch, capsys):
    """`unavailable` means the API could not determine a reason. Mapping it to
    the same value as "nothing changed" charged the attributor's correct answer
    against it as an over-report."""
    rows = chain()
    rows[5]["diagnostics"] = {"cache_miss_reason": {"type": "unavailable"}}
    rows[5]["diagnostics_present"] = True
    out = run(monkeypatch, capsys, write(tmp_path, rows))

    assert "inconclusive" in out
    assert "excluded rather than scored as negatives" in out
    assert "0 conclusive verdicts" in out


def test_a_conclusive_verdict_is_still_compared(tmp_path, monkeypatch, capsys):
    """Excluding the inconclusive ones must not exclude the useful ones."""
    rows = chain()
    rows[5]["request_body"] = body(system="a different prompt", turns=6)
    rows[5]["usage"] = {"input_tokens": 900, "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0}
    rows[5]["diagnostics"] = {"cache_miss_reason": {"type": "system_changed",
                                                    "cache_missed_input_tokens": 40000}}
    rows[5]["diagnostics_present"] = True
    out = run(monkeypatch, capsys, write(tmp_path, rows))
    assert "conclusive     1" in out, out
    assert "which component?" in out
    assert "0 conclusive verdicts" not in out, out


def test_one_break_is_labelled_as_n_equals_1(tmp_path, monkeypatch, capsys):
    """A 49/49 built from 48 no-breaks and one break is not a rate about
    breaking. The script must say so rather than let the number stand alone."""
    rows = chain()
    rows[4]["usage"] = {"input_tokens": 10, "cache_read_input_tokens": 5000,
                        "cache_creation_input_tokens": 800}
    rows[5]["usage"] = {"input_tokens": 900, "cache_read_input_tokens": 5000,
                        "cache_creation_input_tokens": 0}
    rows[5]["request_body"] = body(system="a different prompt", turns=6)
    out = run(monkeypatch, capsys, write(tmp_path, rows))
    assert "n=1, not a rate" in out, out


def test_the_real_capture_is_no_longer_a_disagreement(monkeypatch, capsys):
    """capture-02 record 61 was reported as `over-reported` — the attributor
    being right, counted against it."""
    capture = Path(__file__).resolve().parents[1] / "data" / "raw" / "capture-02.jsonl"
    if not capture.exists():
        pytest.skip(f"capture-02 not present at {capture}; nothing to assert")
    out = run(monkeypatch, capsys, capture)
    assert "MISS" not in out, out
    assert "segment orders that change this verdict: 0 of 120" in out, out


def test_the_first_capture_still_refuses(monkeypatch, capsys):
    """#25's refusal must not regress: capture.jsonl has one class."""
    capture = Path(__file__).resolve().parents[1] / "data" / "raw" / "capture.jsonl"
    if not capture.exists():
        pytest.skip(f"capture not present at {capture}; nothing to assert")
    out = run(monkeypatch, capsys, capture)
    assert "DEGENERATE GROUND TRUTH" in out, out


@pytest.mark.parametrize("record,expected", [
    ({"diagnostics": None}, "unrecorded"),
    ({"diagnostics": None, "diagnostics_present": True}, "clean"),
    ({"diagnostics": None, "diagnostics_present": False}, "absent"),
    ({"diagnostics": {"cache_miss_reason": {"type": "unavailable"}},
      "diagnostics_present": True}, "inconclusive"),
    ({"diagnostics": {"cache_miss_reason": {"type": "previous_message_not_found"}},
      "diagnostics_present": True}, "inconclusive"),
    ({"diagnostics": {"cache_miss_reason": None}, "diagnostics_present": True},
     "inconclusive"),
    ({"diagnostics": {"cache_miss_reason": {"type": "system_changed"}},
      "diagnostics_present": True}, "conclusive"),
])
def test_official_status_keeps_unknown_apart_from_no(record, expected):
    """`unrecorded` and `absent` are different facts and were briefly the same
    label. A record predating `diagnostics_present` cannot say whether upstream
    returned the key; one with it False says upstream did not. Collapsing them
    reported all 63 pairs of the 2026-08-18 capture as "Anthropic sent nothing"
    when the truth was "the proxy did not record it yet" — the same unknown-as-no
    that #25 added the field to prevent."""
    assert calib.official_status(record) == expected


def test_a_pre_field_capture_is_reported_as_unrecorded(tmp_path, monkeypatch, capsys):
    out = run(monkeypatch, capsys, write(tmp_path, chain()))
    assert "unrecorded" in out, out
    assert "absent" not in out, "records predating the field are not evidence of absence"
