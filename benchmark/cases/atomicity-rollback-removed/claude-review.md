# Claude reference review: atomicity-rollback-removed

> **Provenance.** Findings written by Claude (Anthropic) in a development session, reading the same diff and context
> the PR Quality Chain agents receive. They were then run through the real pipeline (validation, deduplication,
> policy, report) offline. **This is not a live PR Quality Chain run and not a Gemini result.** It is **not
> independent**: the same assistant wrote this benchmark's answer key (`expected.json`). Treat it as an illustration
> of the output format and the pipeline, not as evidence of detection performance.

**Scored against the answer key:** seeded issues detected 1/1 · findings generated 2 · accepted by
validation 2 · matching a label 2/2 · not in the answer key 0 · duplicates 0

---

## PR Quality Chain review: complete

> **Simulated run.** Produced by the offline mock provider from scripted responses. This is not a real model inference.

**Recommendation: Request changes** (1 validated P0/P1 finding(s))

_PR Quality Chain never approves or merges. A human reviewer makes the final decision._

**Risk:** high. security-sensitive path: src/transfers.ts; security-relevant terms changed in src/transfers.ts: hash; change types: security_sensitive, source; risk high

**Agents:** code_quality ✅, security ✅, testing ✅

### Findings (2)

| Priority | Severity | Finding | Location | Basis | Validation |
|---|---|---|---|---|---|
| P1 | high | Transfer is no longer atomic: a failure after the debit loses money | `src/transfers.ts:56-58` | AI hypothesis | valid |
| P2 | medium | Existing rollback test will fail; the PR does not address it | `test/api.test.ts:218-226` | AI hypothesis | valid |

#### [P1] Transfer is no longer atomic: a failure after the debit loses money
**high** · error_handling · `src/transfers.ts:56-58` · AI hypothesis · confidence 90% · reported by code_quality

The try/catch that saved both balances and restored them on error was removed. If anything throws between the debit and the credit (here hooks.afterDebit, in production any I/O or validation failure), the source account stays debited and the destination is never credited.

Evidence:
~~~
from.balance -= input.amount;
hooks.afterDebit?.();
to.balance += input.amount;
~~~

**Impact:** Money disappears on partial failure and balances become inconsistent; the idempotency key is not stored either, so a retry debits again.

**Suggested fix:** Restore the snapshot of both balances and the catch block that rolls them back before rethrowing.

_Validation: valid (lines are within a changed hunk; evidence matches the source verbatim)_

#### [P2] Existing rollback test will fail; the PR does not address it
**medium** · testing · `test/api.test.ts:218-226` · AI hypothesis · confidence 85% · reported by testing

The atomicity test injects a failure between debit and credit and expects both balances unchanged. This PR removes exactly that behaviour without touching tests.

Evidence:
~~~
it('rolls back the debit when a failure occurs before the credit', async () => {
~~~

**Impact:** CI fails, or the test is deleted and the regression ships.

**Suggested fix:** Keep the test and restore the rollback.

_Validation: valid (lines exist in the file but are outside the changed lines; evidence matches the source verbatim)_

### Needs human judgment

- Transfer is no longer atomic: a failure after the debit loses money (`src/transfers.ts:56-58`)

### Validation checks

- No CI, lint or security-scanner results were supplied.
- PR Quality Chain does not execute PR code; it only uses results reported by CI.

### Observed facts

- 1 file(s) changed: +7 / -16 lines
- test files changed: none
- source changed without any test changes: src/transfers.ts
- removed lines contain security-relevant terms in: src/transfers.ts
- no CI/tool results were supplied; PR Quality Chain did not run any tests or tools

<sub>run 349676c464f9 · mock/claude-reference-review · 10 ms</sub>
