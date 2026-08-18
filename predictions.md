# Predictions — locked before measuring

The point of this file is the git timestamp. Every prediction here is committed
**before** the corresponding experiment runs. Wrong predictions are never
deleted; they get an "actual" column and stay.

> Rule: if a row's `actual` is filled in, its commit history must show the
> `predicted` text landing in an earlier commit than the data it refers to.

Status: **LOCKED 2026-08-17.** Drafted with Claude, reviewed and accepted by
Yuki before any experiment ran. Reasoning is recorded so that a wrong prediction
is diagnosable rather than merely wrong.

From here the file is append-only, stated as an allowlist rather than a list of
protected columns: **once locked, the only writable cells are `actual` and
`verdict`.** Everything else — `predicted`, `Falsified if`, the operational
definitions, the reasoning prose, the confidence ranking, the kill criteria — is
frozen, and rows are never deleted.

Naming protected columns one at a time does not work: the falsification bands
and the scoring rules live in prose, and a rule that guards `predicted` alone
still permits widening a `Falsified if` band after the number is known. That is
the same escape hatch in a different cell.

Two additions are permitted, and only these two:

1. **New rows**, arriving with their confidence ranking in the same commit.
2. **Dated errata notes**, appended to the Errata section at the end of this
   file. Errors found after locking are corrected by appending an erratum, never
   by editing the original in place. The wrong text stands, with the correction
   beside it.

The errata route exists because a freeze with no lawful way to fix a mistake
does not get obeyed — it gets quietly violated, which costs more than the
mistake did. This file took five review rounds to lock, each of which found a
real defect; assuming the sixth does not exist would be unearned. And the
provision has to be written now: adding it after the lock would itself be an
edit to frozen text.

---

## Operational definitions

Fixed here so that "predicted" and "actual" are measured in the same unit. A
prediction whose terms are ambiguous cannot be scored.

- **turn** = one provider API call (one request recorded by the proxy). Not one
  user message — a single user message routinely triggers 15–25 calls, and cache
  prefixes are compared per call, so the API call is the unit the cache sees.
- **long session** = ≥ 50 turns **and** ≥ 150k cumulative input tokens. Chosen to
  match the scenario in which RTK's own README concedes 1.6%, so the comparison
  is like-for-like rather than against a shorter, friendlier session.
- **reproduces** (P1–P3) = the effect holds in the same direction *and* within
  the falsification band stated on that row. Direction alone is not enough.

---

## E1 — distribution of cache miss causes in real sessions

| # | Question | Predicted | Actual | Verdict |
|---|---|---|---|---|
| 1.1 | Which `cache_miss_reason` type dominates? | `messages_changed` | no samples — 0 divergences in this capture | not yet answerable |
| 1.2 | What share of *turns* diverge at all? | 10–20% | **0%** (0 of 63 comparable turns) | WRONG |
| 1.3 | How many of the survey's 6 "cache killers" appear in real Claude Code data? | 2 of 6 | **1** (compaction only); 4 of the 6 were checkable from this capture | WRONG |
| 1.4 | Will the self-built attributor hit ≥90% agreement on its first run? | **No** — 70–85%, needs one iteration | **1.6%** (1 of 63) | CORRECT — direction, per the locked scoring rule; the magnitude was far off |

**Reasoning**

- **1.1** In a healthy session `messages` is append-only and should never diverge.
  It will anyway: tool results get re-serialised on resend, compaction rewrites
  history rather than appending, and Claude Code injects dynamic
  `system-reminder` blocks *into the message stream*. Competing hypothesis worth
  keeping in mind: `tools_changed`, because deferred tool loading (ToolSearch)
  and skill loading mutate the tool list mid-session. If `tools_changed` wins,
  1.1 is wrong for an interesting reason, not a boring one.
- **1.2** The survey cites claude-code-cache-fix moving hit rate 82.3% → 95.5%,
  implying misses are a minority event. Note the unit trap: that is a *token*
  hit rate, and this row asks about *turns*. They can differ a lot.
- **1.3** That list was written for self-built agents. Anthropic has likely fixed
  the obvious ones in its own client, so most should not reproduce.
- **1.4** Segment order is unknown, and `tool_result` re-serialisation is fiddly.
  Predicting a first-pass miss is the honest call.
  **Scoring rule: direction beats the interval.** The claim is "it will not clear
  90% first time"; 70–85% is the expected magnitude, not part of the claim. A
  measured 88% scores as correct, 92% as wrong. Fixed here so the row cannot be
  re-read favourably after the number is known.
  **Conflict of interest, declared:** the same party that wrote this prediction
  also wrote issue #2's 90% gate and will verify the delegated work — lowering
  the gate would make the prediction come true. The 90% threshold was committed
  to issue #2 on 2026-08-17 *before* this file was locked, and must not be
  changed on account of this row. If the first run does clear 90%, the correct
  response is to audit the calibration script for special-casing, not to
  celebrate.

**Kill criterion:** if `unavailable` / `previous_message_not_found` exceeds 30%
of turns, the official API is unusable at real session length — E1 falls back to
the self-built attributor only, and 1.4 becomes unanswerable.
*(30% is an estimate, not a sourced figure. It is set where the remaining sample
would be too thin to characterise a distribution, and is recorded as a judgement
call so a later reader does not mistake it for a documented limit.)*

## E2 — do compression tools save money or just tokens?

| # | Question | Predicted | Actual | Verdict |
|---|---|---|---|---|
| 2.1 | Token reduction for RTK on a **long** session | 2–5%, not the advertised 80% | | |
| 2.2 | **Dollar** change for the same session | −2% to −5% (a real but small saving) | | |
| 2.3 | Cache hit-rate change under RTK | ≈ unchanged, within noise | | |
| 2.4 | Does any tool come out net-negative on cost? | **Yes** — a retroactive one (DCP or `/compact`), in sessions shorter than its payback period | | |
| 2.5 | Does task quality drop measurably? | Not detectable at n=5, even though failure modes exist | | |

**Reasoning**

The load-bearing distinction, and the one this whole repo is about:

- **Preventive** tools (RTK, lean-ctx) compress output *before* it enters
  context. They never rewrite history, so **they cannot break the cached
  prefix**. Their saving is small but real.
- **Retroactive** tools (DCP, Cozempic, `/compact`) rewrite existing history.
  That is exactly what invalidates the prefix, so they are where a
  "fewer tokens, bigger bill" result can actually occur.

If that framing is right, the headline finding is not "compression tools are a
scam" but "**the two kinds have opposite cost profiles and the ecosystem reports
them with the same metric**". 2.1 draws on RTK's own README conceding 1.6% on a
50-turn/150k session against an advertised 80%.

**Kill criterion:** if the dollar delta is within the run-to-run spread
(min–max across n≥5), report "no measurable effect" — do not report a point
estimate.

## E3 — compaction payback period

| # | Question | Predicted | Actual | Verdict |
|---|---|---|---|---|
| 3.1 | Turns needed to recover the cold-start cost of one compaction | **2–4 turns** | | |
| 3.2 | Does that number depend on session length? | Payback in *turns* roughly constant; savings after it scale with how much was cut | | |

**Reasoning**

Back-of-envelope at 4:1 compaction (100k context → 25k), cache read at 0.1x and
write at 1.25x: the first post-compaction turn costs 25k × 1.25 = 31.25k-
equivalent against 10k-equivalent for an un-compacted cached turn, so it loses
21.25k. Every turn after saves 7.5k (2.5k vs 10k). Payback ≈ **2.8 turns**.

Payback is sharply sensitive to the compaction ratio, and **the 2–4 band is
two-sided — more compaction pays back faster, not slower**:

| compaction ratio | payback |
|---|---|
| 2.5:1 | 6.7 turns |
| 3:1 | 4.8 turns |
| **3.5:1** | **3.6 turns** |
| **4:1** | **2.8 turns** |
| 5:1 | 1.9 turns |
| 10:1 | 0.3 turns |

Solving for the band: 2–4 turns corresponds to a compaction ratio of
**3.3:1 – 4.8:1**, and either side of that window falsifies the row. **If the
measured payback lands outside 2–4, check the observed ratio before blaming the
cost model**: outside 3.3–4.8:1 this row is wrong because the assumed ratio was
wrong, which is a different and less interesting failure than the mechanism
being wrong.

**If this holds it contradicts the survey's advice** ("尽量延迟 compact",
"预防比压缩重要"). Stating that up front so the finding cannot be quietly
reframed afterwards. The claim being tested is narrow — payback is *fast in cost
terms*. Quality loss from compaction is a separate axis and is not what this row
measures.

---

## Prior beliefs carried in from RepoCoach

Measured once already, on a different harness and provider. Re-testing them here
is a genuine replication, not a formality.

| # | Belief | Predicted here | Falsified if | Actual | Verdict |
|---|---|---|---|---|---|
| P1 | Cost grows **super-linearly** with turns | Reproduces | cost/turn is flat or falling across a long session | cost/turn ~flat (1.12x first→last quartile over 46 turns); cumulative 46% at midpoint vs 50% for linear | **FALSIFIED** |
| P2 † | Reducing tool round-trips does **not** reduce cost | Reproduces | a ≥20% cut in call count yields a ≥10% cost drop | | |
| P3 | Total prompt tokens overstates real cost ~3x at high hit rate | Reproduces; multiplier is provider-specific | the multiplier falls outside **2x–5x** at a hit rate ≥60% | **6.75x** at 95.7% hit rate | **FALSIFIED** |

If any of these fails to reproduce, that is a finding, not an error to hide.

The `Falsified if` column exists because "reproduces, but provider-specific" is
otherwise unfalsifiable — any outcome could be waved through as consistent. A
prediction that nothing can contradict is worse than no prediction: it dilutes
the rows that can be scored.

**† P2 is not judged this round, deliberately.** Testing it needs a controlled arm that
varies *call count* while holding the task fixed, and none of E1–E3 does that:
E1 observes without intervening, E2 varies the compression tool, E3 varies
compaction timing. Call counts will drift across E2's arms, but that drift is a
side effect of tools that change result size and call count at once — an
unpinned variable, so reading cost against it would be the kind of "measurement"
that is worse than none. If E2's data suggests call count is worth isolating,
that earns a separate experiment; it does not earn a verdict here.

---

## Confidence

Ranked, so that being wrong is informative:

- **Most confident:** 2.1, 2.3, P3 — mechanical, follow from how caching works.
- **Middling:** 1.1, 1.2, 1.4, 2.2, 2.4, P1, P2 — plausible mechanisms, real
  chance of surprise.
- **Least confident:** 1.3, 2.5, 3.1, 3.2 — 3.1 is arithmetic on an assumed
  compaction ratio and 3.2 inherits that weakness; 2.5 may be under-powered at
  n=5 rather than truly null.

All 14 rows appear exactly once above. If a row is added later it must be ranked
in the same commit.

P2 is ranked here for completeness, but carries † — it is **not judged this
round**, so its confidence cannot be scored against an outcome yet. Where a row
has no verdict this round the wording is "not judged" (a deliberate scope
decision); "unanswerable" is reserved for a row killed by a kill criterion
firing. The two are not interchangeable.

---

## Errata

Corrections made after the lock. Append only, in date order; never edit an
existing entry, and never edit the text an entry corrects.

Format: `**YYYY-MM-DD** — <what was wrong> → <correction>`

_None yet._

---

## Results — capture of 2026-08-18 (75 records, 63 comparable turns, 46-turn main lineage)

Filled per the lock rule: `actual` and `verdict` only. No `predicted` cell was touched.

**P3 was the wrong shape, not the wrong number.** The multiplier is not a
constant; it is a function of the hit rate, `1 / (1 - 0.9h)`:

| hit rate | multiplier |
|---|---|
| 65.6% | 2.44x  (RepoCoach's measurement) |
| 95.7% | 7.21x  (this capture) |

RepoCoach measured ~3x at 65.6%. Writing that down as a constant made
falsification inevitable the moment a session cached better.

**P1 failed for a mechanism worth stating.** RepoCoach's super-linear growth came
from resending the whole conversation at list price every call. At 95.7% cache
hit that resend costs 0.1x, so the quadratic term is largely neutralised and the
curve flattens to roughly linear. Caching does not just reduce the constant, it
changes the shape.

**1.2 was off by its whole range** — predicted 10–20% of turns diverging,
measured zero in 63. One session is not a rate; what it does establish is that a
well-behaved Claude Code session can run 46 turns with an unbroken prefix.

**1.1 has no samples.** With zero divergences there is no distribution to report.
Answering it needs captures containing compaction, MCP changes, or long idle
gaps.
