# Benchmark

Each case lives in `cases/<name>/`:

| File | Purpose |
|---|---|
| `case.json` | Title, description, repo, head SHA, supplied CI results. Only title, description and CI results reach the model |
| `pr.diff` | The PR as a unified git diff |
| `files/` | Post-change content of changed files (used for location/evidence validation) |
| `context/` | Related unchanged files given to agents as context (e.g. existing tests) |
| `expected.json` | **Ground-truth labels**, written independently of any review output. Read only by the scorer |
| `mock_responses.json` | **Scripted** mock output for offline tests. Hand-written, not a model result |

## authorization-bypass

Taken read-only from `../the-payment-app` with:

```bash
git -C ../the-payment-app diff main...defect/authorization-bypass > cases/authorization-bypass/pr.diff
git -C ../the-payment-app show defect/authorization-bypass:src/accounts.ts > cases/authorization-bypass/files/src/accounts.ts
git -C ../the-payment-app show defect/authorization-bypass:test/api.test.ts > cases/authorization-bypass/context/test/api.test.ts
```

The seeded defect deletes the ownership check in `getOwnedAccount`, so any authenticated user can read or
transfer from another user's account (broken object-level authorization).

The PR title is a neutral "Simplify account lookup". The original commit subject ("introduce account
authorization bypass") named the defect and is not used, because the title is sent to the model.

Seeded-issue labels use `categories`, `concept_terms` (groups that must all be mentioned), `line_ranges` (old and
new side) with `line_tolerance`, and `evidence_terms` for findings without lines. See `matches_seeded` in
`app/evaluation.py`.

The scripted mock responses deliberately include a duplicate (code_quality repeats the security finding), a
finding in a file that is not in the PR, a finding at a line that does not exist, and a schema-invalid finding,
so that validation, aggregation and rejection are exercised.

## atomicity-rollback-removed (Code Quality)

Synthetic: `src/transfers.ts` from `the-payment-app` `main` (a9dc259) with the try/catch rollback removed, diffed
outside that repo (no branch exists). If anything fails after the debit, money leaves one account and never
arrives in the other. Title: "Simplify transfer execution". Context: the unchanged `test/api.test.ts`, which still
contains the rollback test.

## zero-amount-validation-test-removed (Testing)

Synthetic: `parseAmount` changed from `value < min` to `value < 0` (zero amounts pass), and the `zero` and
`negative zero` rows deleted from the invalid-amount test table so CI stays green. Two seeded issues: the
validation regression and the removed tests. Title: "Tidy up amount validation". Only code_quality and testing
run on this case: the risk classifier finds nothing security-sensitive in these paths.

## Adding a case

1. Create the folder and files above. Write `expected.json` **before** looking at any review output. Keep the
   title and description neutral: they must not name the defect.
2. Run `python -m app.cli evaluate` (mock) and, when a provider is configured, `python -m app.cli evaluate --live`.
