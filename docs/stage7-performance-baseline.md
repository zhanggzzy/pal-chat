# Stage 7 Performance Baseline

- Generated at: `2026-07-31T18:10:13.203500+00:00`
- Representative conversation: `01KYWNX74JJYRW6SS6CEZ56CVP`
- Dataset shape: 5 scripted agent replies + 1000 public messages path

## Exit Thresholds

| Metric | Threshold | Avg (ms) | P95 (ms) | Runs | Result |
| --- | ---: | ---: | ---: | ---: | --- |
| `ui_sidebar_select_1440x900` | <= 100 | 193.85 | 383.86 | 8 | FAIL |
| `message_commit_non_model` | <= 50 | 53.67 | 67.50 | 20 | FAIL |
| `messages_recent_1000` | <= 200 | 62.78 | 742.45 | 20 | FAIL |

## Auxiliary Samples

| Metric | Avg (ms) | P95 (ms) | Runs | Note |
| --- | ---: | ---: | ---: | --- |
| `ready_health` | 0.85 | 1.08 | 20 | Service health baseline under the seeded dataset. |
| `cost_breakdown` | 10.78 | 12.27 | 20 | Auxiliary monitoring endpoint on the same representative conversation. |

