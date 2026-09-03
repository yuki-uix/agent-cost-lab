# agent-cost-lab

Measuring what coding-agent cost optimisations actually cost.

This is a follow-up to [《怎么优化 Coding Agent 的成本》](https://zhenjia.dev/posts/coding-agent-cost-optimization)
(Zhenjia Zhou, 2026-05-22) — a good survey whose author states plainly that it is
"调研结论，还没有在项目中实际落地". This repo supplies the missing half: the
measurements, plus what changed in the three months after it was written.

**Status (2026-09-03): one result, one blocked experiment, one undecided.**
A claim is only real once its row in [predictions.md](predictions.md) has an
`actual` column; the rest of this README describes the rig, not findings.

- **E3 — answered.** A `/compact` pays for itself in **18–19 turns**, not the
  2–4 that prediction 3.1 was locked on. n=1, and 18–19 is a *lower bound*:
  [report/05](report/05-e3-compact-payback.md) explains why the true payback can
  only be longer.
- **E1 — blocked on data, not analysis.** Across four captures the main one
  carries **zero** `cache_miss_reason` verdicts, because a healthy agent does not
  break its own prefix. The attributor has therefore only ever been checked
  against negatives. Fix in progress: [E4](docs/e4-injection-campaign.md) injects
  faults with a known cause so there is something positive to check against.
- **E2 — the question itself is unsettled.** The premise (tools mis-report their
  own savings) did not survive checking: the claim traced back to the survey
  rather than to the tool. Whether E2 measures tool overstatement or
  restatement drift is a decision that has to be made before money is spent —
  see [report/99](report/99-open-questions.md).

Where this is going, and what would make it a tool rather than a lab:
[docs/direction.md](docs/direction.md).

## Why this repo exists

Nearly every context-compression tool advertises a **token reduction** number.
Tokens are not money. A cached input token costs ~10% of a list-price one, and
compression works by rewriting history — which breaks the cached prefix and
reprices everything after it. So "40% fewer tokens" and "a bigger bill" are
entirely compatible outcomes.

Nobody publishes the dollar figure. This repo measures it.

## The three experiments

| | Question | Rig |
|---|---|---|
| **E1** | What actually causes cache misses in real sessions? | Proxy + Anthropic `cache-diagnosis`, cross-checked against a self-built prefix-diff attributor |
| **E2** | Do compression tools (RTK, lean-ctx, DCP) save money or just tokens? | OpenCode + DeepSeek, n≥5, median + (min–max) |
| **E3** | How many turns does it take to pay back one compaction? | Offline replay |

## Method commitments

These are the rules the numbers have to survive. They come from a previous
project's retrospective, where each was learned by being wrong first.

- **Cost is not token count.** Every number goes through one formula:
  `cache_read × read_rate + cache_write × write_rate + uncached_input × list_rate + output × output_rate`.
- **Three quantities, three limits.** Cost caps on dollars; circuit-breaking on
  call count or wall clock; context pressure separately. One number cannot do all three.
- **Mechanical vs behavioural.** Effects with no model randomness (bytes, prefix
  breaks) are measured by offline replay and valid at n=1. Anything involving a
  model's choices needs n≥5 and is reported as median + (min–max), never a point estimate.
- **Probes sit on the real call path.** The proxy records what was actually sent.
  It never reconstructs a request or re-implements provider logic.
- **Predictions are locked before measurement**, in git, and wrong ones stay.
- **Every rate is traceable.** `cost()` refuses to run on a rate that has not
  been verified against an official pricing page.

## Two gates, enforced by tests not checklists

- **Normalisation** (`providers.py`) — each provider reports cache usage
  differently. All of them become one 4-tuple. Registering a provider without a
  rate entry fails the suite.
- **Export** (`redact.py`) — raw captures contain source code, prompts and auth
  headers. Every captured field needs an explicit policy; an unclassified field
  raises rather than leaking. `tests/test_proxy_sse.py` runs a real capture
  through the gate, so a new field added to the proxy fails there too.

## Layout

```
src/agentcostlab/   proxy (the instrument) + the two gates + cost formula
tests/              gate coverage; SSE timing and cancellation
fixtures/           pricing.json — rates, each with source + retrieved_at
report/             the write-up, one file per chapter
data/raw/           gitignored. never publishable
data/redacted/      post-gate, safe to commit
docs/sources.md     primary sources, with access dates
```

## Running

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]" && .venv/bin/python -m pytest
```

Point an agent at the proxy:

```bash
.venv/bin/python -m uvicorn agentcostlab.proxy:app --port 8787
```

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude
```

## Credits

Built on the survey by [Zhenjia Zhou](https://zhenjia.dev/posts/coding-agent-cost-optimization).
Where this repo corrects or updates it, that is noted in
[report/01-whats-changed.md](report/01-whats-changed.md) — the field moves fast
and the original is explicit about its own shelf life.
