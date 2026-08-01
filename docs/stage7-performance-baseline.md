# Stage 7 Performance Baseline

- Generated at: `2026-08-01T03:13:08.181696+00:00`
- Representative conversation: `01KYXMZANDEZE2PT7NV85T8R9H`
- Dataset shape: 5 scripted agent replies + 1000 public messages path

## Exit Thresholds

| Metric | Threshold | Avg (ms) | P95 (ms) | Runs | Result |
| --- | ---: | ---: | ---: | ---: | --- |
| `ui_sidebar_select_1440x900` | <= 100 | 527.97 | 3318.26 | 8 | FAIL |
| `message_commit_non_model` | <= 50 | 31.16 | 41.26 | 20 | PASS |
| `messages_recent_1000` | <= 200 | 16.37 | 48.60 | 20 | PASS |

## Auxiliary Samples

| Metric | Avg (ms) | P95 (ms) | Runs | Note |
| --- | ---: | ---: | ---: | --- |
| `ready_health` | 1.16 | 2.22 | 20 | Service health baseline under the seeded dataset. |
| `cost_breakdown` | 51.86 | 70.54 | 20 | Auxiliary monitoring endpoint on the same representative conversation. |
| `message_commit_ws_visible` | 3.61 | 26.94 | 20 | Elapsed time from POST return to matching message.committed becoming visible on WS. |

