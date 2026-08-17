# Predictions — locked before measuring

The point of this file is the git timestamp. Every prediction here is committed
**before** the corresponding experiment runs. Wrong predictions are never
deleted; they get an "actual" column and stay.

> Rule: if a row's `actual` is filled in, its commit history must show the
> `predicted` text landing in an earlier commit than the data it refers to.

Status: **not yet locked** — Yuki to fill the `predicted` column, then commit.

---

## E1 — distribution of cache miss causes in real sessions

| # | Question | Predicted | Actual | Verdict |
|---|---|---|---|---|
| 1.1 | Which `cache_miss_reason` type dominates? | _TBD_ | | |
| 1.2 | What share of turns miss at all? | _TBD_ | | |
| 1.3 | How many of the 6 "cache killers" in the 2026-05 survey show up in real data? | _TBD_ | | |
| 1.4 | Will my own prefix-diff attributor agree with Anthropic's official verdict? (target: ≥90% of turns) | _TBD_ | | |

**Kill criterion:** if `unavailable` / `previous_message_not_found` exceeds 30%
of turns, the official API is unusable at real session length — E1 falls back to
the self-built attributor only, and 1.4 becomes unanswerable.

## E2 — do compression tools save money or just tokens?

| # | Question | Predicted | Actual | Verdict |
|---|---|---|---|---|
| 2.1 | Token reduction for RTK on a long session | _TBD_ | | |
| 2.2 | **Dollar** change for the same session | _TBD_ | | |
| 2.3 | Cache hit-rate change | _TBD_ | | |
| 2.4 | Does any tool come out net-negative on cost? | _TBD_ | | |
| 2.5 | Does task quality drop? | _TBD_ | | |

**Kill criterion:** if the dollar delta is within the run-to-run spread
(min–max across n≥5), report "no measurable effect" — do not report a point
estimate.

## E3 — compaction payback period

| # | Question | Predicted | Actual | Verdict |
|---|---|---|---|---|
| 3.1 | Turns needed to recover the cold-start cost of one compaction | _TBD_ | | |
| 3.2 | Does that number depend on session length? | _TBD_ | | |

---

## Prior beliefs I am explicitly carrying in

From the RepoCoach retrospective, already measured once and worth re-testing here:

- Cost grows **super-linearly** with turns, not linearly.
- Reducing tool round-trips does **not** reduce cost (once: calls −28%, tokens +17%).
- Total prompt tokens overstates real cost by ~3x when cache hit rate is high.

If any of these fails to reproduce on a different harness and provider, that is
a finding, not an error to hide.
