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
| 3.1 | Turns needed to recover the cold-start cost of one compaction | **2–4 turns** | **18–19** (capture-03, Opus 5, n=1) | WRONG |
| 3.2 | Does that number depend on session length? | Payback in *turns* roughly constant; savings after it scale with how much was cut | one compaction observed; two session lengths needed | not yet answerable |

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

## E4 — deliberate fault injection, as the attributor's acceptance test

Unlike E1–E3, these rows are not bets about the world. They are bets about **this
repo's own instrument**: five faults are injected on purpose, the cause of each
is therefore known in advance, and the question is whether the attributor names
it. A diagnostic tool that has only ever been checked against negatives has not
been checked.

| # | Question | Predicted | Falsified if | Actual | Verdict |
|---|---|---|---|---|---|
| 4.1 | Does every injected fault actually break the prefix, as judged by the ledger? | **Yes** — each of I1–I5 produces at least 1 turn the ledger calls a break | any one fault runs its full turn budget with no break | | |
| 4.2 | Component-level attribution: how many of the five injected causes does the attributor name correctly? | **≥ 4 of 5** | ≤ 3 of 5 | | |
| 4.3 | Detail-level attribution (which tool / message index / field): how many does it point at correctly? | **≥ 3 of 5** | ≤ 2 of 5 | | |
| 4.4 | On turns carrying an official `cache_miss_reason`, how often does the self-built attributor agree at component level? | **≥ 90%** | < 90% | | |
| 4.5 | Does the I0 control produce false breaks? | **No** — 0 turns judged broken | ≥ 1 break that cannot be attributed to TTL churn or a known external cause | | |
| 4.6 | Is I2 (tool-schema key order) intermittent rather than always-on or never? | break rate lands in **20%–80%** at n ≥ 5 | the rate is 0% or 100% | | |

**Reasoning**

**4.1 is mechanical, hence the high confidence.** The faults are constructed to
change bytes inside the cached prefix, and the ledger judges breaks from billed
usage rather than from the attributor's own reasoning. If a deliberate prefix
edit produced no billing change, the instrument — not the prediction — would be
what failed.

**4.2 is the row this whole campaign exists for.** "The cache broke" is something
the provider already reports; "it broke because of this component" is the only
thing this repo adds. `docs/direction.md` §6 makes 4.2 the gate for the
diagnoser: if the attributor cannot name a cause it was *told* in advance, then
naming causes is not a capability this tool has.

**4.3 is a strictly harder claim than 4.2 and is ranked least confident for that
reason.** Component-level attribution has 5 candidates; detail-level has to pick
the right tool out of ~50, or the right message index out of a long history. The
attributor's `detail`/`path` fields have never been checked against a known
answer at all.

**4.5 is middling, not high, and the reason is already in the data.** Both real
breaks this repo has captured were driven by `cache_control` TTL changes with no
content edit (capture-02 record 61; the three breaks in capture-03). A control
arm that sits idle is therefore not obviously break-free: TTL churn is a live
mechanism that fires without anyone changing a prompt. The falsification band
excludes TTL specifically so that the row measures *false attribution*, not
Anthropic's cache lifecycle.

**4.6 is least confident because the mechanism is uncertain.** Whether shuffling
`input_schema` key order breaks the prefix depends on whether the client
re-serialises tools with stable ordering; it may turn out to be always-on (if
every request reshuffles) or never (if the serialiser canonicalises). n ≥ 5 is
thin for anything with run-to-run variance, and a rate at either extreme is more
likely to mean "the mechanism is not what we thought" than "the band was wrong".

**Scoring denominators are fixed here, before the data.** 4.2 and 4.3 are scored
out of **five** faults, I1–I5. I5 (history compaction) cannot be injected by the
proxy — it is client-driven — and how it is captured is still undecided
(`docs/e4-tasks.md` §3). **If I5 is not run, or its capture cannot be scored, it
counts as not-matched; the denominator stays 5 and the threshold does not move.**
Rescoring against however many arms happened to run is the escape hatch this
file exists to close: it would let a 3-of-4 result be reported as passing.

**4.4 is scored only on turns where an official verdict exists.** Anthropic
returns `unavailable` beyond its comparison horizon, and those turns are not
evidence either way — they are excluded from the denominator and reported
separately, not counted as agreement.

**Kill criterion.** If 4.2 is falsified, the finding is that component-level
attribution is unreliable — not that the campaign failed. The dollar line
(ledger conservation + verified rates) does not depend on the attributor and is
not scored by these rows; see #55.

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

- **Most confident:** 2.1, 2.3, 4.1, P3 — mechanical, follow from how caching
  works; 4.1 because a deliberate edit inside the cached prefix must show up in
  billed usage.
- **Middling:** 1.1, 1.2, 1.4, 2.2, 2.4, 4.2, 4.4, 4.5, P1, P2 — plausible
  mechanisms, real chance of surprise. 4.5 sits here rather than higher because
  TTL churn breaks prefixes without anyone editing a prompt.
- **Least confident:** 1.3, 2.5, 3.1, 3.2, 4.3, 4.6 — 3.1 is arithmetic on an
  assumed compaction ratio and 3.2 inherits that weakness; 2.5 may be
  under-powered at n=5 rather than truly null; 4.3 asks for a needle (one tool
  among ~50) where 4.2 asks for a haystack; 4.6 depends on a serialisation
  detail that has not been established.

All 20 rows appear exactly once above. If a row is added later it must be ranked
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

**2026-08-19** — E2's reasoning supports 2.1 with "RTK's own README conceding
1.6% on a 50-turn/150k session against an advertised 80%". → **That text is not
in RTK's README.** Grepped at the pinned commit
`b34be37caf3796b69a50952a28e60e32b5daad43` for `1.6`, `1.6 `, `50-turn`,
`50 turn`, `150k`, `150,000`, `150_000`: zero hits, re-derived independently by
the reviewer. The 1.6% figure is the survey author's own estimate, not a
concession by the tool. The README's headline is also **90% of bash output**,
not 80% savings. The 2.1 prediction cell (2–5%) is unaffected and stands; what
this corrects is the evidence cited for it. Established in #30.

**2026-08-19** — E2's stated headline is that "the two kinds have opposite cost
profiles and **the ecosystem reports them with the same metric**", and 2.1
assumes RTK conflates token reduction with dollar savings. → **RTK draws the
distinction itself, in its README's second paragraph**, naming bash output as
what it measures and stating that this is not the same as cutting the bill by
the same ratio. For this tool the premise is false: the conflation belongs to
the survey summarising it, not to the tool. Whether E2 is testing "the tools
overclaim" or "the summaries of them do" is therefore undetermined, and the two
are different experiments. Established in #30. (What to do about it is not this
file's business — see report/99-open-questions.md.)

**2026-08-19** — E2's reasoning states that preventive tools "(RTK, lean-ctx)
compress output *before* it enters context. They never rewrite history, so
**they cannot break the cached prefix**." → **lean-ctx does rewrite history.**
At the pinned commit `8a3d23b317c98b39704543c9acb8b7cc8992c63d`,
`rust/src/proxy/history_prune.rs` defines `prune_history_range()`, which mutates
`tool_result` blocks in messages already sent, and `SECURITY.md` states the
proxy "reads and rewrites every request body". What keeps it cache-safe is a
different mechanism, not the absence of rewriting: `rust/src/proxy/cache_safety.rs`
confines rewrites to a window computed by `cached_prefix_len()`. Verified at
those files by the reviewer. The preventive/retroactive binary has no slot for
this third case, and 2.3 reasons from that classification. Established in #30.

**2026-08-19** — 1.1 asks which `cache_miss_reason` type dominates, and nothing
in this file warns that one type cannot appear in the answer. → **Misses caused
by `/compact` are structurally unobservable through this repo's proxy.** It
identifies a conversation by `messages[0]`, and compaction starts one with a new
first message, so the post-compact request carries no `previous_message_id` and
Anthropic returns no verdict. Measured on capture-03 (the `/compact` marker is in
lineage `fec6a430`, message[2]). The blind spot was accepted on 2026-08-19 (#42)
rather than fixed, because both candidate fixes trade a real correctness risk for
an observation whose value is not yet established. Consequence for reading 1.1:
whatever distribution it eventually reports is **conditional on the miss not
being a compaction**. Compaction's share is **unknown** and this file does not
guess at it: across all three captures the five observed real misses were caused
by `cache_control` ttl churn (four) and a system-prompt edit (one), none by any
of the survey's six "cache killers" — and the dominant observed cause is not on
that list at all. The decision is provisional (#42, "for now"); it is reopened if
a capture ever shows compaction misses carrying material cost.

**2026-08-19** — 1.2's `actual` cell reads "0% (0 of **63** comparable turns)",
and the results header reads "63 comparable turns". → The capture holds 63
*pairs* and **62 comparable** ones. Record 46 carries `usage: {}` and yields no
signal; `scripts/calibrate_attributor.py` reports `pairs: 63 / comparable: 62`.
The 0% is unaffected.

**2026-08-19** — 1.2 asks "what share of *turns* diverge at all?" and was filled
before the product decision in #22 settled that the attributor reports **whether
the cache broke**, not whether text changed. → The recorded 0% counts turns
where the cache broke. Under a plain reading of "diverge", the same capture
gives **26 of 62 turns (42%)** carrying real text divergence that left the cache
intact. **Both fall outside the predicted 10–20%, so 1.2 is WRONG under either
reading** — recorded here because the definition was settled after the row was
filled, by a decision this file's author proposed, and the verdict not turning
on that choice is what makes the erratum a clarification rather than a
convenience. The number stands; this pins which of the two it counts.

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

**3.1 was wrong by 5x, and the arithmetic was not the problem.** Replaying the
reasoning above with measured inputs one at a time: the real context (93,656)
leaves it at 2.8 turns; the real cut (**24%**, not the assumed 75%) sends it to
36; the fact that 69% of the post-compaction prefix was **still cached** — never
a cold start — pulls it back to 10.4; the real write tier (**1h at 2x**, not 5m
at 1.25x) pushes it to 17.8, against 18–19 measured turn by turn. Three wrong
inputs, partially cancelling, with the compaction ratio dominating.

**Compaction saves on the cheapest line item.** Cached reads already cost 0.1x,
so removing 22,186 tokens saved $0.0111 a turn, while the rewrite was billed at
2x. It pays the list price to save a discounted one. That is the shape of the
result, and it is why the payback is long rather than a matter of a few turns.

**1.1 has no samples.** With zero divergences there is no distribution to report.
Answering it needs captures containing compaction, MCP changes, or long idle
gaps.
