# PR Quality Chain

AI-assisted first-pass review of GitHub pull requests. It classifies a PR's risk, runs up to three specialist
review agents through one provider-neutral model connector, validates every finding against the actual diff,
deduplicates and prioritizes with deterministic rules, and produces a Markdown report for a human reviewer.

**AI analyzes → tools validate → findings are prioritized → a human decides.** It never approves or merges a PR.

Requirements and evaluation method: [`../scope-and-evaluation.md`](../scope-and-evaluation.md).

## Quick start

Requires Python 3.10+ and `make` (Linux, macOS, or WSL on Windows).

```bash
git clone <repo-url> pr-quality-chain && cd pr-quality-chain
make
```

`make` creates `.venv`, installs everything, creates `.env` from `.env.example` (mock provider: offline and free),
runs the offline test suite and prints a demo review of the benchmark PR. No API key, no network calls to models.

| Command | What it does | Calls a real model? |
|---|---|---|
| `make` | setup + tests + demo | no |
| `make test` | offline test suite; any network attempt fails the run | no |
| `make lint` | Ruff lint and format check | no |
| `make demo` | scripted mock review of the benchmark PR | no |
| `make evaluate` | mock benchmark (tests the pipeline, not a model) | no |
| `make serve` | API on http://localhost:8000 | only if `.env` sets `LLM_PROVIDER=litellm` |
| `make live` | live benchmark, all 3 cases (8 calls); `CASE=<name>` for one; refuses to overwrite results (`RUN=2`) | **yes** (uses quota/credits) |
| `make clean` | remove `.venv` and caches (keeps `.env` and results) | no |

### Using Gemini (or another real model)

1. Create a key in Google AI Studio.
2. Edit `.env` (git-ignored, never commit it):
   ```
   LLM_PROVIDER=litellm
   LLM_MODEL=gemini/gemini-3.8-flash
   GEMINI_API_KEY=<your key>
   ```
   The timeouts in `.env.example` are already sized for Gemini 3.x (see the comments there).
3. `make live` makes at most 8 requests with those settings (3 cases; `make live CASE=atomicity-rollback-removed`
   runs one) and writes `live-eval-1.md`, `live-eval-1.log` and
   `live-results-1.db`. Exit codes: 0 complete, 3 partial, 1 nothing measured, 2 configuration error (for example
   a missing key; nothing is called or written).
4. To read the full review, findings included, from the database without another model call:
   ```bash
   .venv/bin/python -c "from app.store import Store; from app import report; s=Store('live-results-1.db'); print(report.render(s.get(s.recent(1)[0]['run_id'])))"
   ```

### Without make

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,llm]"
cp -n .env.example .env
pytest && ruff check app tests

python -m app.cli review --case authorization-bypass               # full preview (mock)
python -m app.cli review --case authorization-bypass --publishable # exactly what would be posted
git diff main...my-branch > my.diff
python -m app.cli review --diff my.diff --mock-script my_script.json  # mock without a script returns no findings
python -m app.cli evaluate                                          # mock benchmark
python -m app.cli evaluate --live --out r.md --db r.db 2> r.log     # real provider from .env
uvicorn app.main:create_app --factory --port 8000                   # API server
```

## Architecture

```
GitHub PR ─► webhook / manual trigger (app/main.py)
          ─► PR intake: GitHub adapter or supplied diff (app/github.py, app/diff.py)
          ─► orchestrator: risk classification, agent selection, bounded parallel runs (app/orchestrator.py)
               ├─ code_quality ─┐
               ├─ security ─────┼─► ModelConnector (timeouts, retries, logging) ─► provider adapter
               └─ testing ──────┘     (app/llm/connector.py)          mock | litellm (app/llm/*.py)
          ─► validation of file / lines / evidence + CI tool results (app/validation.py)
          ─► aggregation: dedupe, keep strongest, record conflicts (app/aggregate.py)
          ─► policy: priority, needs-human, recommendation (app/policy.py)
          ─► report: preview / publishable Markdown (app/report.py)
          ─► SQLite history (app/store.py) ─► optional, gated, idempotent GitHub comment
```

| Module | Responsibility |
|---|---|
| `app/config.py` | Settings from environment variables |
| `app/observability.py` | JSON logs; redaction of keys, tokens and secret env values on every line |
| `app/llm/base.py` | Provider contract: `ModelRequest`, `ModelResponse`, `Usage`, `ProviderError`, `ErrorCategory` |
| `app/llm/connector.py` | The only retry layer: timeout, backoff + jitter, `Retry-After`, attempt and time budgets |
| `app/llm/mock.py` | Deterministic offline provider with scripted responses and failure scenarios |
| `app/llm/litellm_provider.py` | Real providers via LiteLLM; error classification |
| `app/agents.py` | The three agents: prompts, file filters, output parsing |
| `app/orchestrator.py` | Risk classification, agent selection, parallel execution, run status |
| `app/diff.py` | Diff parsing, line-numbered patches, bounded and delimited context |
| `app/validation.py` | Location/evidence checks; CI issues → tool-confirmed findings |
| `app/aggregate.py` | Deduplication and conflict recording |
| `app/policy.py` | Priority and recommendation rules (documented in the module docstring) |
| `app/report.py` | Markdown report, sanitized against marker spoofing and @mentions |
| `app/store.py` | SQLite: runs, agent outcomes, findings, publications, webhook deliveries |
| `app/github.py` | GitHub read adapter, webhook signature check, idempotent comment publishing |
| `app/main.py` | FastAPI app |
| `app/evaluation.py`, `app/cli.py` | Benchmark harness and CLI |

### Agents and orchestration

The orchestrator classifies changed files by path and by changed-line keywords (`classify` in
`app/orchestrator.py`), then selects agents:

| Change | Agents |
|---|---|
| Security-sensitive path or code (auth, account, payment, owner, token, SQL, exec, ...) | all three (risk high) |
| Other source code | code_quality, testing |
| Dependencies or config | security (+ others if source changed) |
| Tests only | testing |
| Docs only | none (no model calls) |

Agents run concurrently up to `MAX_PARALLEL_AGENTS`, each within `AGENT_TIMEOUT_S`. The run is `complete`
(all agents succeeded), `partial` (some failed), `failed` (all failed) or `cancelled`. A partial or failed run is
always reported as such and recommends escalation; it is never presented as a clean review.

Every agent must return `{"findings": [...]}` matching `AgentFinding` (`app/models.py`): title, description,
severity, category, file, start/end line, verbatim evidence, impact, recommendation, confidence. Unparseable output
fails the agent (`malformed_response` / `schema_validation`); individual invalid findings are rejected and counted.

### Finding basis and validation

- **Observed facts**: computed by code from the diff (files changed, tests touched, CI results supplied).
- **AI hypothesis**: a model finding. Validation checks that the file is in the diff or provided context, the
  lines exist in a changed hunk or the file, and the evidence appears verbatim (≥ 10 chars).
- **Tool-confirmed**: an issue reported by a CI check (tests, linter, scanner) supplied with the PR or read from
  GitHub check runs. PR Quality Chain never executes PR code.

Only `valid` findings are published. Others stay visible in the preview and API, marked with the reason.

### Policy

See the docstring of `app/policy.py`. Summary: P0 = critical, or validated high security, or tool-confirmed high;
P1 = other high or medium security; P2 = medium; P3 = low/info. Recommendations are `request_changes`,
`escalate` or `proceed_with_human_review`. There is no approve value.

## Model providers

Agents only use `ModelConnector.complete()`. Providers implement one method,
`async complete(ModelRequest) -> ModelResponse`, and raise `ProviderError` for failures.

| `LLM_PROVIDER` | `LLM_MODEL` example | Key variable |
|---|---|---|
| `mock` | `mock-model` | none |
| `litellm` | `anthropic/claude-sonnet-5-5` | `ANTHROPIC_API_KEY` |
| `litellm` | `openai/gpt-5` | `OPENAI_API_KEY` |
| `litellm` | `gemini/gemini-2.5-pro` | `GEMINI_API_KEY` |
| `litellm` | any [LiteLLM-supported](https://docs.litellm.ai/docs/providers) `<provider>/<model>` | that provider's variable, or `LLM_API_KEY` + `LLM_BASE_URL` |

Switching providers is configuration only:

```bash
pip install -e ".[llm]"
export LLM_PROVIDER=litellm LLM_MODEL=anthropic/claude-sonnet-5-5 ANTHROPIC_API_KEY=...
python -m app.cli review --case authorization-bypass --live
```

**Local configuration (`.env`).** On startup the app loads `pr-quality-chain/.env` (git-ignored) into the
environment. Variables already set in your shell take precedence, and empty values count as unset.
`PRQ_ENV_FILE` points at a different file, and `PRQ_ENV_FILE=` (empty) disables loading; the test suite does this,
so tests never read your keys. The default provider stays `mock`, and real calls still require `--live`.

```bash
cp .env.example .env          # then edit .env:
# LLM_PROVIDER=litellm
# LLM_MODEL=gemini/gemini-3.8-flash
# GEMINI_API_KEY=<your key>
```

**Live mode never falls back to the mock.** `--live` checks the configuration before anything runs: if
`LLM_PROVIDER` is unset, `mock` or unknown, the command prints an error and exits with status 2, with no model
call, database write or report. Without `--live`, the CLI uses the scripted mock as before.

**Live evaluation outcome.** `evaluate --live` also checks the provider's credentials (offline) before it
writes anything: a missing key exits 2 with the model and variable name, and no output or database file is
created. Afterwards the exit code and report heading reflect what was actually measured:

| Exit | Heading | Meaning |
|---|---|---|
| 0 | `LIVE` | every review completed; a detection of 0/1 here is a real miss |
| 3 | `LIVE: PARTIAL (incomplete)` | some agents failed; available results are kept and scored, but the evaluation is incomplete |
| 1 | `LIVE: FAILED (not measured)` | no usable review result; detection is reported as N/A, never 0 |

Failed or cancelled reviews are left out of every metric denominator. Every attempt is still stored in SQLite with
its `evaluation_status`.

**Missing credentials fail fast.** Before each call the adapter asks LiteLLM (`validate_environment`, offline) which
variables the model needs. If one is missing, the call fails as `auth` / `missing_credentials` naming the model
and the variable, with no request and no retry. A gateway that needs no key still needs `LLM_API_KEY` set to any
placeholder value.

**LiteLLM settings.** `import app` sets two LiteLLM variables when they are unset or empty:

| Variable | Default set by the app | Why |
|---|---|---|
| `LITELLM_LOCAL_MODEL_COST_MAP` | `True` | Otherwise LiteLLM downloads its price list from GitHub on import. With `True`, cost uses the list bundled with the installed LiteLLM version, which can be out of date: upgrade LiteLLM, or set `False` in production if you accept the outbound request. |
| `LITELLM_LOG` | `ERROR` | LiteLLM's DEBUG logs include request parameters (prompts). The adapter also pins LiteLLM's loggers to ERROR, so prompts stay out of logs even if the root logger is set to DEBUG. Do not lower it outside a private debugging session. |

**What has been verified.** The LiteLLM adapter's error classification and response normalization are tested
against LiteLLM 1.106's real exception and response classes, with `acompletion` stubbed. **Live-verified:
`gemini/gemini-3.8-flash` only** (one benchmark run): valid JSON from all three agents, token usage and cost
reported, and client-side timeouts observed with a 60 s limit. Anthropic and OpenAI have not been called, and real
provider error payloads (rate limit, quota) have not been observed for any provider. Other model IDs in the table
are present in LiteLLM 1.106's bundled model list; whether each provider accepts them is only known after a call.

**Why LiteLLM.** One maintained library covers Anthropic, OpenAI, Gemini and ~100 others with OpenAI-style
exceptions that carry `status_code`, `llm_provider` and the raw response headers. It is an optional extra, so the
default install and tests do not need it.

**Provider-specific limitations.**
- Structured output is enforced by prompt + Pydantic validation, not by provider JSON modes, because support
  differs per provider. Prose or a code fence around the JSON object is tolerated (the first JSON object is
  parsed); anything else fails that agent with `malformed_response`, not retried. The schema is never relaxed.
- OpenAI reports a missing key as a 500 and Gemini as a connection error; both are classified as `auth`.
- Token usage comes from the provider response; cost uses LiteLLM's price table and is `null` when unknown.
- Gemini reports per-minute limits with quota/billing wording, so such 429s are classified as ambiguous.

**Adding an adapter.** Create a class with `name`, `simulated = False` and `async complete(request)`. Disable any
SDK-level retries, map SDK errors to `ProviderError` (see `classify_exception`), and register it in
`build_connector` in `app/llm/connector.py`.

### Errors and retries

| Category | Retried | Typical source |
|---|---|---|
| `rate_limit` | yes | 429 with rate wording |
| `rate_limit_or_quota` | yes (bounded) | 429 whose cause is unclear; reported as ambiguous, not as billing |
| `timeout`, `network`, `server` | yes | 408 / connection errors / 5xx, overloaded |
| `auth` | no | 401/403, missing credentials (checked before the call) |
| `quota_exhausted` | no | `insufficient_quota`, "credit balance", 402 |
| `invalid_request`, `unsupported_model` | no | 400/422, 404 |
| `empty_response`, `malformed_response`, `schema_validation` | no | unusable model output |
| `unknown` | no | anything else |

Retry ownership: `ModelConnector` is the only retry layer. The LiteLLM adapter passes `num_retries=0` and
`max_retries=0`. Delays use exponential backoff with full jitter (1s base, 30s cap), or exactly `Retry-After` when
the provider sends it. A retry happens only if attempts remain (`LLM_MAX_ATTEMPTS`) and the delay fits within
`LLM_TOTAL_BUDGET_S`.

### Logs

One JSON object per line on stderr, at `LOG_LEVEL` (default INFO, which includes successful calls). `model_call`
lines include run/correlation ID, agent, provider, model, attempt,
duration, outcome, error category, HTTP status, provider error code, retry decision and delay, token usage and
cost, and `usage_available`; `agent_finished` lines add the agent's attempts, tokens, cost and finding counts.
Prompts, diffs and model responses are never logged. Every line passes through
redaction of known key formats (including partially masked ones such as `sk-proj-****abcd`), Authorization /
API-key header values, configured secrets and any environment variable whose name contains KEY, TOKEN, SECRET or
PASSWORD. Provider error messages are redacted the same way before they are stored or returned.

## API

| Endpoint | Purpose |
|---|---|
| `GET /health` | Liveness |
| `GET /status` | Provider/model, whether simulated, GitHub/publish config, run and error counts, recent runs |
| `POST /reviews` | Review a supplied `PRInput` (diff, optional file contents, context files, CI results) |
| `POST /reviews/github` | `{"repo": "owner/name", "pr_number": 1}`: fetch from GitHub and review |
| `GET /reviews/{id}` | Stored result plus report preview |
| `GET /reviews/{id}/report?publishable=true` | Exactly what would be posted |
| `POST /reviews/{id}/publish` | `{"confirm": true}`. Requires `GITHUB_PUBLISH_ENABLED=true` |
| `POST /webhooks/github` | Signed `pull_request` events; reviews once per head commit; never auto-publishes |

Publishing creates or updates one issue comment per `repo#pr@head_sha`, tracked in SQLite and recoverable by a
hidden marker in comments written by the same account. It never submits a review or merges.

### GitHub permissions

Fine-grained token: **Pull requests: read, Contents: read, Checks: read**. Add **Pull requests: write** only
when you enable publishing.

## Safety boundaries

- PR content (code, titles, descriptions, related files) is wrapped in `<untrusted_pr_content>` delimiters that
  the content cannot close; system prompts tell agents never to follow instructions inside it.
- Bounded: files per PR, characters per file and per context, output tokens, per-call timeout, attempts, total
  retry budget, per-agent timeout, parallel agents.
- PR code is never executed. Test, lint and scanner evidence comes only from CI results.
- SQLite stores run metadata and findings (including short evidence snippets) but not diffs, prompts, raw model
  responses or credentials.

## Benchmark and evaluation

See [`benchmark/README.md`](benchmark/README.md). Three seeded cases, one aimed at each agent, all based on
`../the-payment-app`, a **separate Git repository** this project never modifies:

| Case | Seeded defect | Target agent | Source |
|---|---|---|---|
| `authorization-bypass` | ownership check removed (high) | Security | real branch `defect/authorization-bypass` |
| `atomicity-rollback-removed` | transfer rollback removed: debit without credit on failure (high) | Code Quality | synthetic diff against `main` |
| `zero-amount-validation-test-removed` | zero amounts accepted (medium) and the tests that caught it deleted (medium) | Testing | synthetic diff against `main` |

`python -m app.cli evaluate --case <name>` (repeatable) runs a subset.

`python -m app.cli evaluate [--live]` runs every case, prints a Markdown report and stores it in SQLite
(`evaluation_runs`, linked to the per-case `review_runs`). Every row records provider, model, review status, and
per agent: outcome, error category, attempts, duration, input/output tokens and cost. Values a provider did not
report are stored as `null` and printed as "unavailable", never as 0. A summary that mixes mock and live rows is
marked invalid.

Metrics (each reported with numerator and denominator):

| Metric | Definition |
|---|---|
| Findings generated | Findings returned by agents plus CI tool issues, before deduplication |
| Schema-rejected | Individual findings dropped for failing the finding schema |
| Accepted / withheld by validation | Aggregated findings that passed / failed location and evidence checks |
| Seeded detection | Seeded issues matched by at least one accepted finding ÷ seeded issues |
| Severity accuracy | Detected seeded issues where a matching finding has the labelled severity ÷ detected |
| Precision vs labels | Accepted findings matching a seeded or acceptable label ÷ accepted findings |
| Duplicate rate | Extra accepted findings for an already-matched label ÷ accepted findings |
| Validation acceptance | Accepted ÷ aggregated findings |

Published findings are always validated because invalid ones are filtered out before publishing. That is a design
guarantee, not a measurement, so it is not reported as meeting the 100% target.

A seeded issue only counts as detected when a finding (1) is in the right file with an allowed category, (2) uses
words from every concept group in the label (for the benchmark: an ownership term and an authorization term), and
(3) points at the defect's lines (old or new side, small tolerance), or, if it has no lines, quotes the defect in
its validated evidence. See `matches_seeded` in `app/evaluation.py`.

Labels (`expected.json`) are read only by the scorer and are never sent to a model; a test checks the exact
context each agent receives for the answer, label IDs, label descriptions and scripted findings.

Current mock result (1 case, scripted): 5 findings generated, 1 schema-rejected, 4 after deduplication, 2 accepted,
2 withheld; seeded detection 1/1; precision 2/2; 0 duplicates. **These are scripted responses, so they validate
the pipeline, not a model.**

First live results, Gemini 3.8 Flash, 1 case (reports in `evaluation-result/`):

| Run | Settings | Outcome |
|---|---|---|
| `failed/` | 60 s timeout | all 3 calls timed out; reported as *not measured*, detection N/A |
| `success/` | 180 s timeout, 1 attempt, sequential | complete; seeded issue detected 1/1, precision 1/1, 0 duplicates, severity mismatch (0/1); calls took 19-85 s; 15.6k input / 5.3k output tokens; about $0.03 at LiteLLM list prices |

**One case is an anecdote, not an evaluation.** The proposal targets (≥70% precision, ≥80% seeded high-severity
detection, <10% duplicates, 25% review-time reduction) need repeated `--live` runs on a larger labelled set that
includes clean control PRs, plus human timing studies.

## Limitations

- One benchmark case and no clean control case yet; a single successful live run (Gemini 3.8 Flash).
- Webhook reviews run in-process (FastAPI background tasks); a restart loses in-flight reviews.
- The GitHub adapter does not fetch unchanged related files (e.g. existing tests) as context; supply them via
  `context_files` in `POST /reviews`.
- Risk classification is keyword-based and will misclassify some changes.
- Deduplication is heuristic (same file, nearby lines, plus same category, overlapping evidence or similar title).
- Agent context is the changed hunks (with git's function-name header), up to 3 lines of surrounding code, related
  files you supply, and computed facts. The full changed file and its callers are not sent, so issues that need
  caller context (e.g. which routes use `getOwnedAccount`) depend on what the model can infer.
- Seeded-issue matching uses labelled term lists; a correct finding phrased with none of the listed terms is missed
  and needs human adjudication.
