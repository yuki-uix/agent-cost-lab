# E2 rig — delivery notes

## Deliverable

- `docs/e2-rig.md` — the facts table (AC1–AC6) with source links and an explicit
  "what remains unknown" section. Every number tagged `[一手]` / `[实测]` / `[二手·未核]`.
- This file — how each fact was checked, what could not be established, and the two
  places the issue's framing did not survive contact with the sources.

## Method

All code-level verdicts (AC2, AC3, AC6) came from **source tarballs pinned to a commit**,
not from READMEs. For each tool I downloaded the GitHub codeload tarball at a tag, then
grepped/read inside it with `tarfile` (no extraction to disk that I could leave behind).
SHAs and licences were resolved through the GitHub API (`repos/{r}`, `git/ref/tags/{t}`,
peeling annotated tags to commits). Star counts are live GitHub API reads, dated
2026-08-18. The TokenPilot verdict (AC5) came from the arXiv HTML full text, Appendix A.2.

Constraint honoured: no paid API calls, no `git push`, no `gh`. `src/`, `scripts/`,
`tests/`, `data/` untouched. `predictions.md` untouched.

## 1. What I could not establish

- **"Installs on this machine" for RTK / lean-ctx / DCP — unverified.** The sandbox
  blocks running downloaded binaries, `brew`/`cargo`/`npm` installs, and `git ls-remote`.
  I could only: download the RTK release binary and `file`-check it (correct platform),
  and `import cozempic.cli` inside a local venv (Python ≥ 3.10, succeeds). Everything else
  in the "Installs" column is honestly "not established", not "does not install".
- **DCP's published npm package name.** The survey names `@tarquinen/opencode-dcp`; the
  repo is `Opencode-DCP/opencode-dynamic-context-pruning`. I did not verify the npm
  registry entry, so I cannot confirm the install string.
- **`claude-code-cache-fix` licence.** The survey says MIT; the GitHub API reports
  `NOASSERTION` (no standard SPDX licence detectable). Left unverified rather than guessed.

## 2. Contradiction with the issue framing — lean-ctx is not purely preventive

The issue (and predictions.md 2.x) buckets lean-ctx with RTK as "preventive — compresses
output before it enters context, never rewrites history." The source disagrees:

- Preventive **read path**: yes — shell-hook + MCP + Tree-sitter read modes compress
  tool output before it lands in context.
- **But** there is also a wire proxy (`rust/src/proxy/`) that *rewrites request bodies*:
  `history_prune.rs` `prune_history_range()` mutates `tool_result` blocks in
  `messages[prune_start..prune_end]`, and `SECURITY.md:285` states it "reads and rewrites
  every request body (compression, history pruning)".

The saving grace, and the reason it is not a plain "retroactive" tool, is
`cache_safety.rs`: rewrites are confined to "the cache-safe frozen window
`[cached_prefix_len, boundary)`" — it computes the cached prefix length and only rewrites
prose *after* it. So lean-ctx is a **hybrid**: preventive on the read path, retroactive
cache-safely on the wire. This is material to E2's design: instrumenting lean-ctx as if
it never touched history would miss exactly the mechanism that makes it interesting.

## 3. Contradiction with the issue framing — AC3 is false, and the attribution is wrong

The issue asked me to find "the exact text where RTK's own README concedes 1.6% on a
50-turn/150k session against an advertised 80%" — and to say so plainly if it isn't there.

**It is not there.** A full-tarball grep of RTK `v0.45.0` for `1.6%`, `50-turn`, `150k`
variants returns zero hits. RTK's own docs advertise "Total saved: 1,872,800 tokens (80%)"
and separately warn the reduction "dilutes at every step" — i.e. it concedes the dilution,
but **not** the 1.6% figure.

The "1.6% on a 50-turn / 150k session" sentence lives in the **survey**
(zhenjia.dev, 2026-05-22), as the survey author's own estimate, placed right after their
summary of RTK's README benchmark. predictions.md 2.1's reasoning compresses that into
"RTK's own README concedes 1.6%" — a misattribution. I have reported it directly in
`docs/e2-rig.md` AC3 and left predictions.md alone (append-only, and I was told not to edit
it). Flagging for the errata mechanism.

## 4. AC4 note — one survey number is wrong in the dangerous direction

Seven of the eight quoted star counts are *under*-counts (ordinary growth; Headroom in
particular went from 1.8k to ~67k). One is an *over*-count: `claude-code-cache-fix` is
quoted at ~1,800 but has 418 stars — ~4.3× inflation. Over-stated credibility on a
"cache-fix" tool is the direction worth caring about, because the survey uses it as an
authoritative recommendation. Reported, not repaired.

## 5. AC5 — TokenPilot does not transfer to these tools

TokenPilot's baselines (Appendix A.2) are academic: LLMLingua-2, SelectiveContext,
Keep-Last-N, Summary, LCM, Pichay, MemoBrain, AgentSwing, MemOS, plus a Vanilla baseline.
RTK / lean-ctx / DCP / Cozempic are absent. So TokenPilot's cost numbers say nothing about
the community tools this repo's E2 actually measures — the comparison the issue's
reasoning implies does not exist in the paper.

## 6. AC6 — the rig cannot reuse the Claude-Code `ANTHROPIC_BASE_URL` path as-is

OpenCode does not read `ANTHROPIC_BASE_URL` at all (zero hits in source). It uses
`options.baseURL` in `opencode.json`. It does stream (`streamText`, 31 files). But its
DeepSeek path is OpenAI-compatible (`@ai-sdk/openai-compatible`), while this repo's proxy
records Anthropic Messages-format bodies keyed on `cache_control`. The mechanical arm
therefore has an open format decision (translate vs. drive OpenCode through its Anthropic
provider path) that is *not* answered by this document — it is a rig-design input, not a
fact I could resolve without the paid run.

## 7. Small uncertainties I am carrying

- Star counts are point-in-time (2026-08-18) and move; the AC4 deltas are only as good as
  that snapshot.
- I verified DCP/RTK/lean-ctx/Cozempic source at the tags listed; anything changed on the
  default branch after those tags is out of scope of this note.
- The `file+function` citations in `docs/e2-rig.md` AC2 are exact for the pinned SHAs; line
  numbers were captured where I had them and are given as anchors, not guarantees.
