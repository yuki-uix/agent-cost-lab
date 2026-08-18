# E2 rig — established facts

Scope: the factual foundations for E2's compression-tool rig, before any paid run.
This task spent nothing — no paid API calls were made. Every number is tagged per
[`sources.md`](sources.md):

- `[一手]` — official docs / papers / the tool's own source, accessed this session.
- `[实测]` — measured this session (GitHub API / a tarball grep / an import).
- `[二手·未核]` — quoted from the survey or elsewhere, not independently re-derived.

All star counts and SHAs were observed **2026-08-18** unless noted. "README is not
evidence about code" — the AC2 verdicts below cite file + function from the
tagged source tarballs, not documentation.

## Which of this was re-derived by the reviewer

This document was produced under a tool allowlist that denied 22 calls, including
the `curl` loops and `git ls-remote` used to resolve SHAs and star counts. The
author could not fully self-verify. Everything below that a second party re-ran
independently is marked `✔re`; everything else rests on the author's run alone.
Both are honest — they are not equally checked, and collapsing them would drop
the whole table to the weaker of the two.

| Claim | Re-derived by the reviewer? |
|---|---|
| AC1 — all four repos resolve, licences, star counts | `✔re` HTTP 200 ×4, licences exact, stars within 1 |
| AC1 — all four SHAs | `✔re` each tag resolved via the API, annotated tags dereferenced, 4/4 match |
| AC2 — lean-ctx is hybrid | `✔re` `history_prune.rs` at the pinned SHA: `cached_prefix_len()` L51, `prune_history_range()` L105; `cache_safety.rs` L4 carries the quoted sentence |
| AC2 — RTK / DCP / Cozempic verdicts | not re-derived |
| AC3 — the 1.6% text is absent from RTK's README | `✔re` README fetched at `b34be37c`, six patterns, zero hits |
| AC4 — Headroom (the 37× outlier) | `✔re` 66,757 observed |
| AC4 — the other six rows | not re-derived |
| AC5 — TokenPilot baselines | **partial** — paper and title confirmed at arXiv 2606.17016; the abstract names no baselines and no community tool, consistent with the conclusion. The verbatim Appendix A.2 quote was **not** independently confirmed |
| AC6 — no `ANTHROPIC_BASE_URL`; `baseURL` per provider | `✔re` `providers.mdx` at the pinned SHA: 0 occurrences; `baseURL` documented at L34/42 |

---

## AC1 — identity facts for the four tools

| Tool | Canonical repo | Stars (observed) | Release / commit | SHA actually resolved | Licence | Installs on this machine |
|---|---|---|---|---|---|---|
| RTK | [rtk-ai/rtk](https://github.com/rtk-ai/rtk) | 76,485 `[实测]` | tag `v0.45.0` | `b34be37caf3796b69a50952a28e60e32b5daad43` `[一手]` | Apache-2.0 `[一手]` | **not verified** — release binary downloaded and `file`-checked, but not executed (sandbox blocks running downloaded binaries) |
| lean-ctx | [yvgude/lean-ctx](https://github.com/yvgude/lean-ctx) | 3,590 `[实测]` | tag `v3.9.19` | `8a3d23b317c98b39704543c9acb8b7cc8992c63d` `[一手]` | Apache-2.0 `[一手]` | **not verified** — `brew`/`cargo` install not run (needs network + build) |
| DCP | [Opencode-DCP/opencode-dynamic-context-pruning](https://github.com/Opencode-DCP/opencode-dynamic-context-pruning) | 4,011 `[实测]` | tag `v3.1.15` | `11f6517780a502512a3467645074be447cb0369e` `[一手]` | AGPL-3.0 `[一手]` | **not verified** — `npm` install not run |
| Cozempic | [Ruya-AI/cozempic](https://github.com/Ruya-AI/cozempic) | 371 `[实测]` | tag `v1.8.39` | `5840f52377bf5cf07aa73b2d8faf9ccdb2850923` `[一手]` | MIT `[一手]` | **partial** — `import cozempic.cli` succeeds in a local venv (Python ≥ 3.10) `[实测]`; the `cozempic` CLI itself was not run |

Caveat: "installs on this machine" is answered **negatively or partially for all four** —
not because they fail, but because the sandbox blocks the install/run paths. Treat
these cells as "not established", not "does not install".

---

## AC2 — preventive vs retroactive, decided from source

The load-bearing distinction: does the tool rewrite messages that were **already sent**
(retroactive, risks breaking the cached prefix), or only shape content **before it
enters context** (preventive)?

| Tool | Verdict | Evidence (file + function, at the SHA above) |
|---|---|---|
| RTK | **Preventive** (clean) | `src/core/runner.rs` — `emit_guarded()` writes the filtered command output to stdout *before* it reaches the harness/context. `src/main.rs` `long_about` = "filter and summarize system outputs before they reach your LLM context". |
| lean-ctx | **Hybrid** — preventive on the read path, *and* a wire proxy that rewrites history cache-safely | Preventive: shell-hook + MCP + Tree-sitter read-mode compression. Retroactive-but-cache-safe: `rust/src/proxy/history_prune.rs` — `prune_history_range()` mutates `tool_result` blocks in `messages[prune_start..prune_end]`; `cached_prefix_len()` computes the frozen prefix; `rust/src/proxy/cache_safety.rs` states the proxy "only ever rewrites prose inside the cache-safe frozen window `[cached_prefix_len, boundary)`"; `SECURITY.md:285` — the proxy "reads and rewrites every request body (compression, history pruning)". |
| DCP | **Retroactive** (request-time; disk untouched) | `index.ts:63` registers the `experimental.chat.messages.transform` hook; `lib/hooks.ts:107` `createChatMessageTransformHandler` calls `prune(state, logger, config, output.messages)`; `lib/messages/prune.ts` replaces already-produced tool outputs with `PRUNED_TOOL_OUTPUT_REPLACEMENT = "[Output removed to save context - information superseded or no longer needed]"`. Rewrites the outgoing request's message list, not the on-disk session. |
| Cozempic | **Retroactive** (on-disk) | `src/cozempic/session.py` — "Session discovery and I/O for Claude Code JSONL files", `save_messages()` writes the session back; `src/cozempic/executor.py` `execute_actions()` applies remove/replace actions to the session file. |

**Flagged contradiction:** predictions.md frames lean-ctx as purely preventive
("they never rewrite history"). Its source shows a wire-level proxy that *does* rewrite
request-body history — but only inside a cache-safe window it computes from the cached
prefix. That is a different mechanism than "never rewrites", and it matters for how E2
must instrument it. See `docs/delegation/06-e2-rig-NOTES.md` §2.

---

## AC3 — the "1.6% on a 50-turn / 150k session" claim is **not in RTK's README**

**The text predictions.md 2.1 attributes to "RTK's own README" is not there.**

- A full-tarball grep of RTK at `v0.45.0` (`b34be37c…`) for `1.6%`, `1.6 `, `50-turn`,
  `50 turn`, `150k`, `150,000`, `150_000` returns **zero hits** `[实测]`.
- What RTK's own docs *do* say, at that commit `[一手]`:
  - `docs/usage/FEATURES.md` (~line 1046): "Total saved: 1,872,800 tokens (80%)".
  - `docs/guide/resources/savings-explained.md`: the reduction "dilutes at every step"
    — an explicit caveat that per-command compression is **not** the same as cutting
    the whole bill by the same ratio.
- The "在一個 50 轮 / 150k token 的 session 里实际只省约 1.6%" sentence is the
  **survey author's own estimate**, not a concession in RTK's README — it appears in
  [zhenjia.dev, *怎么优化 Coding Agent 的成本*, 2026-05-22](https://zhenjia.dev/posts/coding-agent-cost-optimization),
  immediately after the survey summarises RTK's README benchmark
  (`[二手·未核]`, quoted for identification only, not reproduced at length).

**Conclusion (stated plainly, no softening):** the "advertised 80%" half of the claim is
real (RTK advertises 80% session-total in `docs/usage/FEATURES.md`). The "RTK's own README
concedes 1.6%" half is **false** — that number is the survey's, and predictions.md's 2.1
reasoning misattributes it. This is errata-worthy. predictions.md is not edited here (it is
append-only).

### Added in review: RTK's README draws the distinction itself

Verified independently at the same SHA `[一手]`. The README's headline figure is **90% of
bash output**, not 80% savings, and its second paragraph (line 60) states:

> "RTK cuts **up to 90% of the bash output** your agent reads. That is what RTK measures,
> and it is not the same as cutting your bill by 90%."

This matters more than the misattribution. E2's framing in predictions.md is that "the two
kinds have opposite cost profiles and **the ecosystem reports them with the same metric**".
For RTK that is false: it names its own metric, and explicitly refuses the inference to the
bill — the distinction this repo was built to make, made by the tool under test, in its
second paragraph.

So the target may not be "the tools overclaim". On this evidence it is closer to "the tool
was precise and the survey summarising it was not". Which of those E2 is testing changes
what the experiment is for, and it should be settled before any paid run.

---

## AC4 — survey star counts vs observed

Survey quotes (2026-05-22) are `[二手·未核]`; observed values are `[实测 2026-08-18]`
via the GitHub API. The survey is **not repaired** — this table only reports the delta.

| Tool | Survey quoted | Observed | Direction |
|---|---|---|---|
| RTK | 49.2k | 76,485 | under-counted (grew ~1.6×) |
| Context Mode | ~15k | 19,946 | under-counted |
| lean-ctx | 1.7k | 3,590 | under-counted (grew ~2.1×) |
| Headroom | 1.8k | 66,756 | under-counted (grew ~37×) |
| DCP | 2.8k | 4,011 | under-counted |
| Cozempic | 300 | 371 | under-counted |
| Magic Context | 636 | 1,787 | under-counted (grew ~2.8×) |
| claude-code-cache-fix | ~1,800 | 418 | **over-stated ~4.3×** — the only one the survey inflates |

Takeaway: seven of eight quoted counts are *lower* than today's value (ordinary growth
since May, plus a viral jump for Headroom). One — `claude-code-cache-fix` — is quoted at
~4.3× its actual size. Repo identities were confirmed against their descriptions;
`claude-code-cache-fix`'s licence could not be auto-asserted by the API (the survey calls
it MIT; the API reports no standard SPDX licence).

---

## AC5 — TokenPilot's baselines are academic methods, not the community tools

Paper: [arXiv 2606.17016, *TokenPilot*](https://arxiv.org/abs/2606.17016) `[一手]`.

Appendix A.2, "Implementation Details", states verbatim:

> We compare TokenPilot against compression methods (LLMLingua-2, SelectiveContext,
> Keep-Last-N) and dynamic paging or summarization approaches (Summary, LCM, Pichay,
> MemoBrain, AgentSwing, MemOS).

Plus a `Vanilla` no-context-management baseline. **None of RTK, lean-ctx, DCP, or
Cozempic appears.** TokenPilot is benchmarked against **academic** baselines only — so its
numbers do not transfer to a claim about the community tools this repo is measuring. This
closes the open question in `sources.md`.

---

## AC6 — OpenCode against DeepSeek via this repo's proxy

Identity: [anomalyco/opencode](https://github.com/anomalyco/opencode) (the org was renamed
from SST), 198,727 stars `[实测]`, MIT `[一手]`, branch `dev` → `9b0dd36cda0b9accb429a7f9f9ad9b054a27d04a`
`[一手]`.

1. **Install** `[一手]`: `curl -fsSL https://opencode.ai/install | bash`, or
   `npm install -g opencode-ai`, or `brew install anomalyco/tap/opencode`.
2. **Config file** `[一手]`: `opencode.json` / `opencode.jsonc` in the project root or
   `.opencode/`, or globally at `~/.config/opencode/opencode.json`
   (`packages/core/src/plugin/skill/customize-opencode.md:42-43`).
3. **Does it honour `ANTHROPIC_BASE_URL`? No.** Zero occurrences of `ANTHROPIC_BASE_URL`
   across the full source tarball `[实测]`. The equivalent knob is `options.baseURL` in
   `opencode.json` (`packages/web/src/content/docs/providers.mdx:34`: "customize the base
   URL for any provider by setting the `baseURL` option. This is useful when using proxy
   services or custom endpoints.").
4. **DeepSeek** `[一手]`: native via `/connect` → search "DeepSeek" → enter API key →
   `/models` → "DeepSeek V4 Pro" (`providers.mdx:706-725`). It is reached through
   `@ai-sdk/openai-compatible`, not a dedicated DeepSeek SDK — `packages/opencode/package.json`
   lists `@ai-sdk/openai-compatible` (no `@ai-sdk/deepseek`).
5. **Does it stream? Yes.** 31 files use `streamText` / `stream:` / `toTextStreamResponse`,
   including `packages/llm/src/protocols/{anthropic-messages,openai-chat,openai-responses}.ts`
   `[实测]`.

**Wire-format gap (unresolved):** this repo's proxy records Anthropic Messages-format
request bodies (it keys on `cache_control` breakpoints). OpenCode's DeepSeek provider
speaks OpenAI-compatible format. Pointing `options.baseURL` at this proxy therefore does
**not** answer the format question by itself — the mechanical arm must decide whether to
translate, or to drive OpenCode through its Anthropic provider path instead. Not settled
here; flagged for the rig design, not prescribed.

---

## What remains unknown

- **Install/run status of all four tools** — blocked by the sandbox, not established.
- **Which npm package DCP actually ships as** — the survey names `@tarquinen/opencode-dcp`;
  the repo is `Opencode-DCP/opencode-dynamic-context-pruning`. The published package name
  was not verified against npm.
- **`claude-code-cache-fix` licence** — the survey says MIT; the GitHub API cannot assert
  a standard SPDX licence for it. Unverified.
- **The OpenCode↔proxy wire-format decision** — see AC6 above.
- Everything downstream of these facts (the before/after request bodies through
  `attribute()`, the dollar numbers) is the rig run itself and is **not** in this document.
