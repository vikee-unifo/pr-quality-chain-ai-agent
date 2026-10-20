# Benchmark results: LIVE

Provider/model: litellm/gemini/gemini-3.8-flash
Evaluation ID: 34c35dd42c39 (stored in SQLite with per-case review runs)

| Case | Run | Status | Recommendation | Seeded detected | Generated | Schema-rejected | After dedupe | Accepted | Withheld | Labeled | Duplicates | Time (ms) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| authorization-bypass | 596c1b74e676 | complete | escalate | 1/1 | 3 | 0 | 1 | 1 | 0 | 1 | 0 | 183663 |

| Case | Agent | Status | Error | Attempts | ms | Input tok | Output tok | Cost USD |
|---|---|---|---|---|---|---|---|---|
| authorization-bypass | code_quality | succeeded | - | 1 | 79853 | 5207 | 1733 | 0.010404 |
| authorization-bypass | security | succeeded | - | 1 | 18717 | 5221 | 1424 | 0.009256 |
| authorization-bypass | testing | succeeded | - | 1 | 85090 | 5206 | 2119 | 0.011851 |

- Cases: 1
- Seeded-issue detection (accepted findings only): 1.0 (1/1)
- High-severity seeded detection: 1.0 (1/1) (target ≥ 0.8)
- Severity accuracy of detected issues: 0.0 (0/1)
- Precision vs labels: 1.0 (1/1) (target ≥ 0.7; denominator = accepted findings; unlabeled ones count as false positives until a human adjudicates them)
- Duplicate rate: 0.0 (0/1) (target < 0.1)
- Validation acceptance: 1.0 (1/1) of aggregated findings passed location/evidence checks; the rest were withheld
- Published findings are all validated because invalid ones are filtered out before publishing; that is a design guarantee, not a measured result.
- Agent calls: 3, failed: 0; tokens in/out: 15634/5276; cost USD: 0.03151 (0 agent call(s) without reported cost)

```json
{
  "rows": [
    {
      "case": "authorization-bypass",
      "run_id": "596c1b74e676",
      "provider": "litellm",
      "model": "gemini/gemini-3.8-flash",
      "simulated": false,
      "status": "complete",
      "recommendation": "escalate",
      "review_ms": 183663,
      "agents": [
        {
          "agent": "code_quality",
          "status": "succeeded",
          "error_category": null,
          "http_status": null,
          "attempts": 1,
          "duration_ms": 79853,
          "input_tokens": 5207,
          "output_tokens": 1733,
          "cost_usd": 0.010404,
          "schema_rejected": 0
        },
        {
          "agent": "security",
          "status": "succeeded",
          "error_category": null,
          "http_status": null,
          "attempts": 1,
          "duration_ms": 18717,
          "input_tokens": 5221,
          "output_tokens": 1424,
          "cost_usd": 0.00925575,
          "schema_rejected": 0
        },
        {
          "agent": "testing",
          "status": "succeeded",
          "error_category": null,
          "http_status": null,
          "attempts": 1,
          "duration_ms": 85090,
          "input_tokens": 5206,
          "output_tokens": 2119,
          "cost_usd": 0.01185075,
          "schema_rejected": 0
        }
      ],
      "input_tokens": 15634,
      "output_tokens": 5276,
      "cost_usd": 0.0315105,
      "findings_generated": 3,
      "schema_rejected": 0,
      "after_aggregation": 1,
      "accepted_by_validation": 1,
      "withheld_by_validation": 0,
      "seeded": 1,
      "seeded_detected": 1,
      "high_seeded": 1,
      "high_seeded_detected": 1,
      "severity_matches": 0,
      "labeled_accepted": 1,
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
    "evaluation_status": "complete",
    "unmeasured_cases": [],
    "seeded_detection": {
      "value": 1.0,
      "numerator": 1,
      "denominator": 1
    },
    "high_severity_detection": {
      "value": 1.0,
      "numerator": 1,
      "denominator": 1
    },
    "severity_accuracy": {
      "value": 0.0,
      "numerator": 0,
      "denominator": 1
    },
    "precision_vs_labels": {
      "value": 1.0,
      "numerator": 1,
      "denominator": 1
    },
    "duplicate_rate": {
      "value": 0.0,
      "numerator": 0,
      "denominator": 1
    },
    "validation_acceptance": {
      "value": 1.0,
      "numerator": 1,
      "denominator": 1
    },
    "agent_calls": 3,
    "agent_failures": 0,
    "input_tokens": 15634,
    "output_tokens": 5276,
    "cost_usd": 0.0315105,
    "agents_without_cost": 0,
    "totals": {
      "findings_generated": 3,
      "schema_rejected": 0,
      "after_aggregation": 1,
      "accepted_by_validation": 1,
      "withheld_by_validation": 0,
      "seeded": 1,
      "seeded_detected": 1,
      "high_seeded": 1,
      "high_seeded_detected": 1,
      "severity_matches": 0,
      "labeled_accepted": 1,
      "duplicates": 0
    },
    "evaluation_id": "34c35dd42c39"
  }
}
```
