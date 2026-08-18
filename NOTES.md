# NOTES — lineage-keyed `previous_message_id` threading

## AC3 — real-data replay through the real `_lineage_key`

Replay of `data/raw/capture.jsonl` (16 records), grouping each record's
`request_body` by `_lineage_key` imported from `agentcostlab.proxy`:

```
total records: 16
distinct lineages (by _lineage_key): 6

lineage 62c1db5c0bde…  n=11  records=[2, 3, 5, 6, 8, 10, 11, 12, 13, 14, 15]
    model=claude-sonnet-5  tools=51  has_system=True
    messages[0] preview: "[{'type': 'text', 'text': '<system-reminder>\\nAs"

lineage 5b0df66faca6…  n=1  records=[4]
    model=claude-sonnet-5  tools=0  has_system=True
    messages[0] preview: '## Pre-gathered recon (mechanically collected — '

lineage dab21d7868ce…  n=1  records=[7]
    model=claude-sonnet-5  tools=0  has_system=True
    messages[0] preview: '[{\'type\': \'text\', \'text\': "<session>\\n同级folder中有'

lineage a43569e8075a…  n=1  records=[9]
    model=claude-sonnet-5  tools=0  has_system=True
    messages[0] preview: "[{'type': 'text', 'text': 'The following is the "

lineage cfade248d565…  n=1  records=[0]    model=claude-opus-5    m0='x'     (status 401)
lineage 505f151832d6…  n=1  records=[1]    model=claude-sonnet-5  m0='quota' (ConnectTimeout)
```

The 11 main-agent requests (`[2,3,5,6,8,10,11,12,13,14,15]`) collapse into one
lineage; the three auxiliaries (`[4]`, `[7]`, `[9]`) each get their own. Records
`[0]` and `[1]` are auth/quota probes, not conversation turns — they land as
singletons and were never threaded even before this change (401 / timeout, no
response id).

## The bug, concretely

The old global `LAST_ID` produced exactly four bogus `system_changed` flags, one
at each switch between lineages (prev id comes from the *other* lineage):

```
[4]  prev=msg_011Ce9bMBVEMNwBAAsZEi2p8 (main [2])  -> aux  → system_changed
[7]  prev=msg_011Ce9bgitdscvFJkw9Pafok (main [6])  -> aux  → system_changed
[9]  prev=msg_011Ce9bpEaXERi4eeJqxgzAg (main [8])  -> aux  → system_changed
[10] prev=msg_011Ce9bpY7DTbmzQFXpbkRFV (aux [9])   -> main → system_changed
```

Within the main lineage itself `cache_read_input_tokens` climbs monotonically
`0 → 68384 → 69842 → 70314 → 70364 → 70417 → 70892 → 71013 → 71064 → 71323 →
71425` with no `system_changed`. All four flags are cross-lineage artefacts; the
true cache-break count in this session is 0.

## Keys considered and rejected

- **`hash(model, system, tools)`** — identical grouping on this dataset, wrong in
  principle. A mid-conversation `system` change is the very cache-killer E1 is
  built to measure; keying on it turns each change into a fresh lineage whose
  next request sends `previous_message_id: null` and gets no diagnostics. The
  instrument erases the thing it exists to record.
- **`metadata.user_id` / `session_id`** — all four groups share one session id,
  so it cannot separate them.
- **`hash(model, messages)`** (full history) — changes every turn, so it can
  never thread. `messages[0]` is the fixed seed.
- **Python builtin `hash()`** — salted by `PYTHONHASHSEED` (unstable across
  processes) and cannot hash a dict. Used `sha256` over a canonical JSON encoding
  of `[model, messages[0]]` instead.

## Uncertainties / open questions

1. The key assumes the client never mutates `messages[0]` mid-conversation. On
   this dataset all 11 main turns share an identical first message (verified by
   hash), but I have not confirmed that invariant holds across other sessions —
   e.g. context compaction that rewrites the first message would split a lineage.
2. `hash(model, messages[0])` carries no session discriminator: two fresh
   sessions with an identical model + first message would share one lineage slot.
   No better discriminator is available — `metadata.user_id.session_id` is shared
   across the four groups and cannot be used.
3. The two probes (`[0]` 401, `[1]` timeout) are counted as singletons in the
   replay; they sit outside the "11 + 3" the AC names. Not special-cased.
