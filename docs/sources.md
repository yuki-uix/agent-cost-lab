# Primary sources

Every number in the report carries one of three tags:

- `[一手]` — official docs, papers, or pricing pages. Listed here with an access date.
- `[实测]` — measured in this repo. Cite the commit and the data file.
- `[二手·未核]` — quoted from elsewhere and not verified. Allowed, but must be labelled.

Accessed 2026-08-17 unless noted.

## Cache mechanics and diagnostics

| Source | What it establishes |
|---|---|
| [Cache diagnostics — Claude docs](https://platform.claude.com/docs/en/build-with-claude/cache-diagnostics) | `cache_miss_reason` union: `model_changed` / `system_changed` / `tools_changed` / `messages_changed`, plus `previous_message_not_found` and `unavailable`. Beta header `cache-diagnosis-2026-04-07`. Claude API only — not Bedrock or Vertex. Reports **earliest divergence only**. `cache_missed_input_tokens` is explicitly "a magnitude indicator rather than a billing number". |
| [Prompt caching — Claude docs](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) | Explicit `cache_control` breakpoints; 5-minute and 1-hour TTL. |
| [VS Code Cache Explorer](https://code.visualstudio.com/docs/agents/agent-troubleshooting/cache-explorer) | Editor-side UI for the same divergence-finding job. |
| [DeepSeek context caching](https://api-docs.deepseek.com/guides/kv_cache/) | Automatic prefix caching. Usage reports `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`. No write premium. |
| [OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching) | Automatic. `cached_tokens` under `prompt_tokens_details`. Cached-input discount is smaller than Anthropic's — verify current figure before use. |

## Literature

| Source | What it establishes |
|---|---|
| [arXiv 2601.06007 — Don't Break the Cache](https://arxiv.org/abs/2601.06007) | Prompt caching across three providers, 500+ agent sessions. Cost savings 41–80%, TTFT 13–31%. Finds naive full-context caching **can paradoxically increase costs**. |
| [arXiv 2606.17016 — TokenPilot](https://arxiv.org/abs/2606.17016) | States the sparsity-vs-cache-continuity trade-off directly: "unconstrained sequence mutations alter layouts, introducing prefix mismatches and cache invalidation." Evaluated on PinchBench and Claw-Eval. **Baselines not yet checked** — open question below. |
| [arXiv 2606.11213 — Beyond Compaction](https://arxiv.org/pdf/2606.11213) | Structured context eviction for long-horizon agents. Not yet read. |

## The survey this repo follows up

| Source | Note |
|---|---|
| [怎么优化 Coding Agent 的成本 — zhenjia.dev, 2026-05-22](https://zhenjia.dev/posts/coding-agent-cost-optimization) | Author states it is research notes, not yet applied, and warns about shelf life. No licence declared: cite and link, do not reproduce at length. |

## Open questions on sources

- TokenPilot's baselines: academic methods, or the actual community tools
  (RTK / lean-ctx / DCP / `/compact`)? Not established. Read the paper body.
- Whether `rtk-test`'s claimed failure modes for RTK and LeanCTX are reproducible.
- Star counts quoted in the 2026-05 survey look anomalously high for several
  projects. Verify each before citing.
