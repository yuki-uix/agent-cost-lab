"""The health gate is itself a gate, so it needs its own coverage.

Real captures live under data/raw/ and are gitignored, so these use synthetic
records shaped like the three failures that actually occurred.
"""
from __future__ import annotations

import importlib.util
import json
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


def test_upstream_never_returning_a_diagnostics_key_is_not_by_itself_a_defect():
    """This test used to assert the opposite, and #25's version of the gate
    agreed with it: no key anywhere meant the beta header was dead.

    That judgement was withdrawn on evidence. No capture has ever recorded
    `diagnostics_present`, so nothing establishes that a healthy session carries
    the key at all — if the API returns it only when it has something to say, a
    clean session legitimately carries none. Condemning that would throw away a
    capture the operator spent a working session producing.

    What replaced it is a witness: see
    test_a_turn_that_lost_the_cache_without_a_key_condemns_the_beta.
    """
    rows = healthy()
    for r in rows:
        r["diagnostics"] = None
        r["diagnostics_present"] = False
    _, bad, stats = health.check(rows)
    assert not bad, bad
    assert stats["undecidable"], "silence must be reported as undecidable, not passed over"


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
    # Matched on the stable part of the line, not on the order the kinds are
    # listed in — that order comes from a constant and is not the behaviour
    # under test. test_every_counted_condition_is_named_in_the_message covers
    # the wording.
    assert any("threaded turns came back" in b for b in bad), bad


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


def _answered(rows, present=True):
    for r in rows:
        r["diagnostics"] = None
        r["diagnostics_present"] = present
    return rows


def test_a_clean_session_with_no_key_is_undecidable_not_condemned():
    """The remedy #26 asked for — a fraction floor on replied/answerable —
    would fail this capture. Nothing establishes that a healthy session carries
    the key at all: no capture has ever recorded diagnostics_present. If the API
    returns it only when it has something to say, a clean session legitimately
    scores zero, and a floor would throw away good data that cost the operator
    a working session."""
    rows = _answered(healthy(n=20), present=False)
    ok, bad, stats = health.check(rows)
    assert not bad, "a clean session must not be condemned on an unknown semantics"
    assert stats["undecidable"], stats
    assert any("identical from here" in u for u in stats["undecidable"])


def test_a_turn_that_lost_the_cache_without_a_key_condemns_the_beta():
    """The ledger witnesses it without knowing the semantics: a turn that
    demonstrably lost the cache had something to report, so silence there is
    the header being dead — not a clean session."""
    rows = _answered(healthy(), present=False)
    for r in rows:
        r["usage"] = {"input_tokens": 5, "cache_read_input_tokens": 9000}
    rows[6]["usage"] = {"input_tokens": 5, "cache_read_input_tokens": 0}
    _, bad, _ = health.check(rows)
    assert any("never took effect" in b for b in bad), bad


def test_one_reply_proves_the_beta_is_alive():
    """Evidence beats proportion: the key coming back at all is proof the
    header was honoured, whatever fraction of turns had something to say."""
    rows = _answered(healthy(n=20), present=False)
    rows[4]["diagnostics_present"] = True
    ok, bad, _ = health.check(rows)
    assert not bad, bad
    assert any("beta header alive" in line for line in ok), ok


def test_an_empty_usage_is_not_a_cache_break():
    """Record 46 of the 2026-08-18 capture carries `usage: {}`. Reading that as
    cache_read == 0 invents a witness that is really a missing measurement."""
    rows = _answered(healthy(), present=False)
    for r in rows:
        r["usage"] = {"input_tokens": 5, "cache_read_input_tokens": 9000}
    rows[6]["usage"] = {}
    _, bad, _ = health.check(rows)
    assert not any("never took effect" in b for b in bad), bad


def test_turns_that_could_not_answer_are_not_judged():
    """A 429 never reached the parser. Counting it as 'the beta did not reply'
    would blame the header for a transport failure."""
    rows = _answered(healthy())
    rows[5].update(status_code=429, usage=None, diagnostics_present=None)
    ok, bad, _ = health.check(rows)
    line = next(l for l in ok if "diagnostics key" in l)
    assert "/10 answerable" in line, line   # 11 threaded, one of them a 429


def test_partial_vintage_capture_is_reported_not_silently_narrowed():
    """Judging 1 turn and staying silent about the other 10 reads as full
    coverage of a capture that is 90% unjudgeable."""
    rows = _answered(healthy())
    for r in rows[2:]:
        r.pop("diagnostics_present", None)
    _, _, stats = health.check(rows)
    assert stats["undecidable"], "a mixed-vintage capture must say so"
    assert any("/11 answerable" in u for u in stats["undecidable"]), stats["undecidable"]


@pytest.mark.parametrize("diagnostics,kind", [
    ({"cache_miss_reason": {"type": "unavailable"}}, "unavailable"),
    ({"cache_miss_reason": {"type": "previous_message_not_found"}},
     "previous_message_not_found"),
    ({"cache_miss_reason": None}, "no reason returned"),
])
def test_every_counted_condition_is_named_in_the_message(diagnostics, kind):
    """The label is built from the predicate's own constants, so a fourth
    condition cannot be counted without appearing in the text."""
    rows = _answered(healthy())
    for r in rows:
        r["diagnostics"] = diagnostics
    _, bad, _ = health.check(rows)
    line = next(b for b in bad if "threaded turns came back" in b)
    assert kind in line, f"{kind!r} is counted but not named in: {line}"
    assert line.startswith("11/11"), line


def test_both_undecidable_conditions_survive_together():
    """They are independent: some turns predate the field, and the ones that
    do carry it never got a key. Assigning to a single slot let the second
    discard the first."""
    rows = _answered(healthy(n=20), present=False)
    for r in rows[10:]:
        r.pop("diagnostics_present", None)
    _, _, stats = health.check(rows)
    assert len(stats["undecidable"]) == 2, stats["undecidable"]
    assert any("identical from here" in u for u in stats["undecidable"])
    assert any("predate" in u for u in stats["undecidable"])


# --- the witness rule is the central ledger's, not a local copy ---------------

def _witness_pair(prev_usage, curr_usage):
    """Two threaded turns with no diagnostics: does the gate witness a break?"""
    prev = rec(0, rid="msg_p")
    prev["usage"] = prev_usage
    curr = rec(1, rid="msg_c", prev="msg_p")
    curr["usage"] = curr_usage
    return health._broke_cache(curr, {"msg_p": prev})


def test_the_witness_sees_a_partial_break_not_only_a_drop_to_zero():
    """The old local rule fired only when cache_read fell from non-zero to zero.
    A prefix that read 5,000 and wrote 800 should be readable as 5,800; reading
    5,600 loses 200 tokens and is a break the gate has to witness."""
    assert _witness_pair(
        {"cache_read_input_tokens": 5000, "cache_creation_input_tokens": 800,
         "cache_creation": {"ephemeral_5m_input_tokens": 800,
                            "ephemeral_1h_input_tokens": 0}},
        {"cache_read_input_tokens": 5600, "cache_creation_input_tokens": 0},
    ) is True


def test_the_witness_sees_a_shrink_in_a_non_anthropic_usage_shape():
    """The old rule read Anthropic's key names only, so a DeepSeek-shaped usage
    scored 0 on both sides and no break was ever witnessed."""
    assert _witness_pair(
        {"prompt_cache_hit_tokens": 9000, "prompt_cache_miss_tokens": 100},
        {"prompt_cache_hit_tokens": 2000, "prompt_cache_miss_tokens": 7000},
    ) is True


def test_the_witness_needs_a_demonstrated_break_not_an_absent_measurement():
    """``broke_cache`` returns None when a pair cannot be measured. A witness
    must be a break that happened, never a measurement that did not."""
    assert _witness_pair({}, {"cache_read_input_tokens": 100}) is False
    assert _witness_pair({"foo": 1}, {"foo": 2}) is False


# --- gate A: lineage collision (issue #72) -----------------------------------

def _shrink_rows(n_prev=28, n_curr=2, first="same-prompt"):
    """Two records that share ``messages[0]`` and whose list shrinks: a splice."""
    prev = rec(0, rid="msg_p")
    prev["request_body"]["messages"] = [{"role": "user", "content": first}] * n_prev
    curr = rec(1, rid="msg_c")
    curr["request_body"]["messages"] = [{"role": "user", "content": first}] * n_curr
    return prev, curr


def test_collision_is_detected_without_a_threading_pointer():
    """AC2: the judgement must not read ``injected_previous_message_id``. Neither
    record carries one, and the shrink is still found — the splice is caught by
    the message count alone."""
    prev, curr = _shrink_rows()
    prev["injected_previous_message_id"] = None
    curr["injected_previous_message_id"] = None
    findings = health._lineage_collisions([prev, curr])
    assert [f["prev_n"] for f in findings] == [28]
    assert [f["curr_n"] for f in findings] == [2]


def test_a_real_messages_changed_break_is_not_a_collision():
    """AC3: a real break rewrites an earlier message while the list still grows.
    The count does not shrink, so gate A must stay silent — judging "not an
    append" would misreport every genuine cache break as a splice."""
    prev = rec(0, rid="msg_p")
    prev["request_body"]["messages"] = [{"role": "user", "content": "seed"},
                                        {"role": "assistant", "content": "old"}]
    curr = rec(1, rid="msg_c")
    curr["request_body"]["messages"] = [{"role": "user", "content": "seed"},
                                        {"role": "assistant", "content": "rewritten"},
                                        {"role": "user", "content": "next"}]
    assert health._lineage_collisions([prev, curr]) == []


def test_a_shrink_that_does_not_break_the_cache_is_reported_not_failed():
    """Severity is graded by harm, not shape. A shrink whose seam the ledger does
    not witness as a break is reported in the ``----`` tier and must not FAIL —
    failing on shape would condemn the harmless ``<transcript>`` collisions."""
    prev, curr = _shrink_rows(n_prev=3, n_curr=1)
    prev["usage"] = {"cache_read_input_tokens": 100, "cache_creation_input_tokens": 0}
    curr["usage"] = {"cache_read_input_tokens": 200, "cache_creation_input_tokens": 0}
    _, bad, stats = health.check([prev, curr])
    assert not any("splices" in b for b in bad), bad
    assert any("splices" in u for u in stats["undecidable"]), stats["undecidable"]


def test_a_harmful_splice_fails_the_capture():
    """A shrink the ledger witnesses as a break has polluted the break count, so
    the capture is unusable and the FAIL line names the seam."""
    prev, curr = _shrink_rows()
    prev["usage"] = {"cache_read_input_tokens": 33408, "cache_creation_input_tokens": 0}
    curr["usage"] = {"cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    _, bad, _ = health.check([prev, curr])
    assert any("splices two conversations" in b and "28->2" in b and "polluted" in b
               for b in bad), bad


COLLISION_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "lineage" / "collision-calib.json"


def test_reduced_fixture_reproduces_the_calibration_splice():
    """The fixture stripped all free text but keeps the verdict: one splice, the
    same seam position, the same counts, the same harmful flag."""
    records = json.loads(COLLISION_FIXTURE.read_text())["records"]
    findings = health._lineage_collisions(records)
    assert len(findings) == 1, findings
    f = findings[0]
    assert (f["prev_i"], f["curr_i"], f["prev_n"], f["curr_n"], f["harmful"]) \
        == (13, 14, 28, 2, True)


def test_reduced_fixture_fails_the_health_gate_on_the_splice():
    """The integration path: ``check`` on the reduced fixture produces the FAIL
    line naming the seam, not merely the extracted findings."""
    records = json.loads(COLLISION_FIXTURE.read_text())["records"]
    _, bad, _ = health.check(records)
    assert any("splices two conversations" in b and "13->14" in b and "28->2" in b
               for b in bad), bad


# --- gate B: one campaign arm per capture (issue #73) -------------------------

def _arm(rows, arm="i0"):
    for r in rows:
        r["injection"] = {"id": arm, "applied": False, "detail": None, "turn": 1}
    return rows


def test_two_campaign_arms_in_one_capture_fails():
    rows = _arm(healthy())
    rows[3]["injection"] = {"id": "i1", "applied": False, "detail": None, "turn": 3}
    _, bad, _ = health.check(rows)
    assert any("campaign arms" in b and "i0" in b and "i1" in b for b in bad), bad


def test_a_single_campaign_arm_is_ok():
    _, bad, _ = health.check(_arm(healthy()))
    assert not any("campaign arms" in b for b in bad), bad


def test_no_injection_field_is_not_a_campaign_arm_mismatch():
    """Historical captures carry no ``injection`` at all. That is
    score_injection.py's gate to notice, not this one's — so it must not FAIL."""
    _, bad, _ = health.check(healthy())
    assert not any("campaign arms" in b for b in bad), bad


def test_partial_injection_labels_fail():
    """A capture where some records declare an arm and others do not can only be
    two proxy processes writing one file (#73): ``inject.apply()`` returns None
    when nothing is armed, so within one process the field is all-or-none.
    Counting only the labelled records would let this pass with a single arm."""
    rows = healthy()
    for r in rows[:3]:
        r["injection"] = {"id": "i3", "applied": False, "detail": None, "turn": 1}
    _, bad, _ = health.check(rows)
    assert any("3 records declare an arm, 9 carry none" in b for b in bad), bad
    # distinguishable from the two-arm FAIL, which this capture is not
    assert not any("must declare a single arm" in b for b in bad), bad


# --- the reverse defence, on the real captures (issue #72, AC3) --------------
#
# The three synthetic tests above show the criterion fires on a splice and stays
# quiet on a `messages_changed` break. Neither shows the property the criterion
# was chosen FOR: zero false positives across every real record this repo holds.
#
# That property is not decorative. Two other criteria were measured and rejected
# for failing it — threading pointers see one connected component, and "the
# successor is not an append" cuts at exactly the known break seams, 63/49/134
# spurious splices across these same three files. A checklist saying "verified by
# hand" is what the repo's own discipline says not to rely on; this is the same
# statement as a test that re-runs whenever the criterion is touched.
#
# Skips when data/raw/ is absent, like the other real-capture tests — those files
# are gitignored, so a clean checkout cannot see them.

REAL_CAPTURES = ("capture.jsonl", "capture-02.jsonl", "capture-03.jsonl")


@pytest.mark.parametrize("name", REAL_CAPTURES)
def test_no_collision_is_reported_on_a_real_capture(name):
    path = Path(__file__).resolve().parents[1] / "data" / "raw" / name
    if not path.exists():
        pytest.skip(f"{name} not present at {path}; the false-positive rate cannot be checked")
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    findings = health._lineage_collisions(rows)
    assert findings == [], f"{name}: {len(findings)} false splices, e.g. {findings[:3]}"
