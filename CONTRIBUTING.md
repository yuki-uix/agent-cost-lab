# Working agreement

Applies to everyone touching this repo, human or model.

## Issue first, PR closes it

1. Open an issue before writing code. It carries the **acceptance criteria**.
2. Branch: `feat/…`, `fix/…`, `chore/…`, `docs/…` off `main`.
3. **Problems, dead ends and decisions get written into the issue**, not left in
   a chat log. If a run failed, or an assumption turned out wrong, that belongs
   in a comment on the issue while it is still fresh.
4. PR body includes `Closes #X` and the actual numbers, not "all tests pass".
5. Never push to `main`. Never force-push.

The issue is the record. Six months from now the PR diff will not explain why a
threshold is 90% rather than 95%, but the issue thread will.

## Acceptance criteria are external facts, not green tests

A test suite passing is compatible with a config pointing at something that does
not exist. Every AC in this repo must name something checkable outside the code:
an agreement rate against an official API, a byte count, a file that must
resolve, a rate traceable to a pricing page.

When verifying delegated work, **read the payload, not the exit code.** A
headless process can exit 0, print `"subtype": "success"`, and carry
`is_error: true` in the same record with zero tools ever invoked.

## Tests must call the thing they test

A test that reimplements the logic under test passes forever, including after
someone adds a path that bypasses the gate entirely. Coverage of a category
(providers, redaction policies) is enforced by iterating the registry, so a new
member fails the suite until it is registered everywhere.

## Delegated agents

- **No `git push`. No opening PRs.** The reviewer does both, after verification.
- One task per git worktree. Do not run several headless jobs concurrently.
- Stay inside the files the task names. Do not "improve" adjacent modules.
- Report real numbers in the PR description. Do not claim success categorically.

## predictions.md is append-only once locked

Predictions are committed before the data they refer to exists; that ordering in
git history is the entire evidential value. Once locked:

- never edit a `predicted` cell
- never delete a row because it turned out wrong
- fill `actual` and `verdict` only

A wrong prediction with intact history is worth more than a right one with an
ambiguous timestamp.

## Numbers carry their provenance

Every figure in `report/` is tagged `[一手]` (official source, with access date
in `docs/sources.md`), `[实测]` (measured here, cite commit and data file), or
`[二手·未核]` (quoted, unverified). The third tag is allowed. Omitting it is not.

## Commits

`type: short description`, English only, types: feat / fix / chore / docs /
style / refactor / test.
