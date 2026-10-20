# Benchmark results: LIVE: FAILED (not measured)

> **Not measured.** No usable review result was produced (every selected agent failed; see the agent table). Detection metrics are N/A, not 0.

Provider/model: litellm/gemini/gemini-3.8-flash
Evaluation ID: 051185d6e2ca (stored in SQLite with per-case review runs)

| Case | Run | Status | Recommendation | Seeded detected | Generated | Schema-rejected | After dedupe | Accepted | Withheld | Labeled | Duplicates | Time (ms) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| authorization-bypass | e5453e74734c | failed | escalate | N/A | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 180036 |

| Case | Agent | Status | Error | Attempts | ms | Input tok | Output tok | Cost USD |
|---|---|---|---|---|---|---|---|---|
| authorization-bypass | code_quality | failed | timeout | 1 | 60020 | unavailable | unavailable | unavailable |
| authorization-bypass | security | failed | timeout | 1 | 60007 | unavailable | unavailable | unavailable |
| authorization-bypass | testing | failed | timeout | 1 | 60006 | unavailable | unavailable | unavailable |

- Cases: 1
- Seeded-issue detection (accepted findings only): N/A (nothing to measure)
- High-severity seeded detection: N/A (nothing to measure) (target ≥ 0.8)
- Severity accuracy of detected issues: N/A (nothing to measure)
- Precision vs labels: N/A (nothing to measure) (target ≥ 0.7; denominator = accepted findings; unlabeled ones count as false positives until a human adjudicates them)
- Duplicate rate: N/A (nothing to measure) (target < 0.1)
- Validation acceptance: N/A (nothing to measure) of aggregated findings passed location/evidence checks; the rest were withheld
- Published findings are all validated because invalid ones are filtered out before publishing; that is a design guarantee, not a measured result.
- Agent calls: 3, failed: 3; tokens in/out: unavailable/unavailable; cost USD: unavailable (3 agent call(s) without reported cost)

```json
{
  "rows": [
    {
      "case": "authorization-bypass",
      "run_id": "e5453e74734c",
      "provider": "litellm",
      "model": "gemini/gemini-3.8-flash",
      "simulated": false,
      "status": "failed",
      "recommendation": "escalate",
      "review_ms": 180036,
      "agents": [
        {
          "agent": "code_quality",
          "status": "failed",
          "error_category": "timeout",
          "http_status": null,
          "attempts": 1,
          "duration_ms": 60020,
          "input_tokens": null,
          "output_tokens": null,
          "cost_usd": null,
          "schema_rejected": 0
        },
        {
          "agent": "security",
          "status": "failed",
          "error_category": "timeout",
          "http_status": null,
          "attempts": 1,
          "duration_ms": 60007,
          "input_tokens": null,
          "output_tokens": null,
          "cost_usd": null,
          "schema_rejected": 0
        },
        {
          "agent": "testing",
          "status": "failed",
          "error_category": "timeout",
          "http_status": null,
          "attempts": 1,
          "duration_ms": 60006,
          "input_tokens": null,
          "output_tokens": null,
          "cost_usd": null,
          "schema_rejected": 0
        }
      ],
      "input_tokens": null,
      "output_tokens": null,
      "cost_usd": null,
      "findings_generated": 0,
      "schema_rejected": 0,
      "after_aggregation": 0,
      "accepted_by_validation": 0,
      "withheld_by_validation": 0,
      "seeded": 1,
      "seeded_detected": 0,
      "high_seeded": 1,
      "high_seeded_detected": 0,
      "severity_matches": 0,
      "labeled_accepted": 0,
      "unlabeled_accepted": [],
      "duplicates": 0
    }
  ],
  "summary": {
    "cases": 1,
    "mode": "LIVE",
    "providers": [
      "litellm/gemini/gemini-3.8-flash"
    ],
    "evaluation_status": "failed",
    "unmeasured_cases": [
      "authorization-bypass"
    ],
    "seeded_detection": {
      "value": null,
      "numerator": 0,
      "denominator": 0
    },
    "high_severity_detection": {
      "value": null,
      "numerator": 0,
      "denominator": 0
    },
    "severity_accuracy": {
      "value": null,
      "numerator": 0,
      "denominator": 0
    },
    "precision_vs_labels": {
      "value": null,
      "numerator": 0,
      "denominator": 0
    },
    "duplicate_rate": {
      "value": null,
      "numerator": 0,
      "denominator": 0
    },
    "validation_acceptance": {
      "value": null,
      "numerator": 0,
      "denominator": 0
    },
    "agent_calls": 3,
    "agent_failures": 3,
    "input_tokens": null,
    "output_tokens": null,
    "cost_usd": null,
    "agents_without_cost": 3,
    "totals": {
      "findings_generated": 0,
      "schema_rejected": 0,
      "after_aggregation": 0,
      "accepted_by_validation": 0,
      "withheld_by_validation": 0,
      "seeded": 0,
      "seeded_detected": 0,
      "high_seeded": 0,
      "high_seeded_detected": 0,
      "severity_matches": 0,
      "labeled_accepted": 0,
      "duplicates": 0
    },
    "evaluation_id": "051185d6e2ca"
  }
}
```
