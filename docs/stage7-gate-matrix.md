# Stage 7 Gate Matrix Baseline

This document records the current phase-6 baseline before the final stage-7
hardening sweep. It is intentionally conservative: items listed here are backed
by concrete tests or scripts in this repository, and gaps remain gaps until
closed by code and executable checks.

## Existing Gate Coverage

| Gate | Current coverage |
| --- | --- |
| `H-01` | `tests/test_phase2_public_messages.py::test_h01_duplicate_message_request_is_idempotent` |
| `H-02` | `tests/test_phase2_public_messages.py::test_h02_concurrent_commits_keep_contiguous_sequence` |
| `H-03` | `tests/test_phase3_worker_processes.py::test_h03_live_direct_mention_obligates_single_agent` |
| `H-04` | No dedicated test yet; stage-7 work still needed |
| `H-05` | `tests/test_phase3_worker_processes.py::test_h05_live_all_mention_keeps_both_agent_obligations` |
| `H-06` | `tests/test_phase3_worker_processes.py::test_h06_live_interruption_invalidates_stale_draft_and_cancels_typing` |
| `H-07` | `tests/test_phase3_worker_processes.py::test_h07_live_episode_budget_stops_after_four_two_two` |
| `H-08` | No dedicated test yet; stage-7 work still needed |
| `H-09` | No dedicated test yet; stage-7 work still needed |
| `H-10` | No dedicated test yet; stage-7 work still needed |
| `H-11` | No dedicated test yet; stage-7 work still needed |
| `H-12` | `tests/test_phase2_public_messages.py::test_h12_failed_projection_does_not_consume_sequence` |
| `H-13` | `tests/test_phase2_public_messages.py::test_h13_reconnect_replays_missing_events_without_duplicate_dispatch` |
| `H-14` | `tests/test_phase6_analysis.py::test_h14_history_index_and_raw_fallback_stay_read_only` |
| `H-15` | `tests/test_phase6_analysis.py::test_h15_analysis_export_zip_is_versioned_redacted_and_downloadable` |
| `H-16` | No dedicated test yet; stage-7 work still needed |

## Stage-7 Exit Checks

The new reproducible runtime scripts and cutover notes live in:

- `scripts/bootstrap.sh` / `scripts/bootstrap.ps1`
- `scripts/start.sh` / `scripts/start.ps1`
- `scripts/stop.sh` / `scripts/stop.ps1`
- `scripts/health.sh` / `scripts/health.ps1`
- `scripts/perf-baseline.py`
- `docs/cutover-and-rollback.md`

## Performance Baseline Procedure

Run a live conversation first, then sample:

```bash
python3 scripts/perf-baseline.py --conversation-id <conversation_id>
```

The script reports `avg_ms` and `p95_ms` for:

- `/health/ready`
- `/api/v1/conversations/{id}/messages?limit=1000`
- `/api/v1/conversations/{id}/metrics/cost-breakdown`

Browser-side interaction latency and Playwright coverage are still open
stage-7 tasks and are not claimed by this baseline document.
