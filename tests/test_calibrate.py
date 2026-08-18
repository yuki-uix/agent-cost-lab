"""The calibration script reported `agreement 100.0% (62/62)` on a capture whose
ground truth had exactly one class, where a constant "did not break" function
scores the same 100%. These tests run the real entry point — importing and
re-implementing its logic would pass just as happily after someone adds a path
around it.
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

# "agreement 71.4%" (split) or "agreement rate: 71.4%" (tie) — the phrasing
# differs by branch and these tests are about the number, not the wording.
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


def test_single_class_ground_truth_reports_no_rate(tmp_path, monkeypatch, capsys):
    """The shape of the 2026-08-18 capture: the cache never broke, so every
    comparable pair is `no_divergence`."""
    rows = [rec(0)] + [rec(i, prev=f"msg_{i-1}") for i in range(1, 8)]
    out = run(monkeypatch, capsys, write(tmp_path, rows))

    assert "DEGENERATE GROUND TRUTH" in out
    # "no agreement rate reported" legitimately contains the word; what must be
    # absent is a *percentage* attached to it. Matched loosely on purpose: the
    # script prints the rate as "agreement N%" on a split and "agreement rate:
    # N%" on a tie, and this assertion must not depend on which.
    assert not re.search(RATE, out), \
        "a rate a constant would match must not be printed"
    assert "best order" not in out.lower(), "no order can be ranked on one class"
    assert "constant-`no_divergence` attributor scores" in out


def test_it_says_when_none_of_the_truth_came_from_the_official_api(
        tmp_path, monkeypatch, capsys):
    rows = [rec(0)] + [rec(i, prev=f"msg_{i-1}") for i in range(1, 8)]
    out = run(monkeypatch, capsys, write(tmp_path, rows))
    assert "from official diagnostics: 0" in out
    assert "this capture has 0 verdicts" in out


def test_two_classes_still_get_a_rate(tmp_path, monkeypatch, capsys):
    """The refusal is about a degenerate ground truth, not about caution. Once
    the cache actually breaks, the script must go back to scoring."""
    rows = [rec(0)] + [rec(i, prev=f"msg_{i-1}") for i in range(1, 8)]
    # One turn where the system prompt changed and the cache did not survive.
    rows[5]["request_body"] = body(system="a different prompt", turns=6)
    rows[5]["usage"]["cache_read_input_tokens"] = 0

    out = run(monkeypatch, capsys, write(tmp_path, rows))
    assert "DEGENERATE GROUND TRUTH" not in out
    assert re.search(RATE, out), out


def test_official_diagnostics_are_preferred_over_the_usage_fallback(
        tmp_path, monkeypatch, capsys):
    rows = [rec(0)] + [rec(i, prev=f"msg_{i-1}") for i in range(1, 8)]
    rows[5]["request_body"] = body(system="a different prompt", turns=6)
    rows[5]["usage"]["cache_read_input_tokens"] = 0
    rows[5]["diagnostics"] = {"cache_miss_reason": {"type": "system_changed",
                                                    "cache_missed_input_tokens": 40000}}
    out = run(monkeypatch, capsys, write(tmp_path, rows))
    assert "official signal: diagnostics.cache_miss_reason.type" in out
    assert "from official diagnostics: 1" in out
