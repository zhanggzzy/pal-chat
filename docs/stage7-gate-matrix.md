# Stage 7 Gate Matrix

Last updated: `2026-08-01`

This matrix records the final stage-7 candidate evidence on top of commit
`b1db796fddf56d4cee81600682462ea177fc60f3` plus the changes in this working
tree. Every row below is backed by an executable test or script in this
repository.

## H-01 Through H-16

| Gate | Evidence |
| --- | --- |
| `H-01` | `tests/test_phase2_public_messages.py::test_h01_duplicate_message_request_is_idempotent` |
| `H-02` | `tests/test_phase2_public_messages.py::test_h02_concurrent_commits_keep_contiguous_sequence` |
| `H-03` | `tests/test_phase3_worker_processes.py::test_h03_live_direct_mention_obligates_single_agent` |
| `H-04` | `tests/test_phase3_worker_processes.py::test_h04_live_non_target_agent_may_stay_silent` |
| `H-05` | `tests/test_phase3_worker_processes.py::test_h05_live_all_mention_keeps_both_agent_obligations` |
| `H-06` | `tests/test_phase3_worker_processes.py::test_h06_live_interruption_invalidates_stale_draft_and_cancels_typing` |
| `H-07` | `tests/test_phase3_worker_processes.py::test_h07_live_episode_budget_stops_after_four_two_two` |
| `H-08` | `tests/test_phase3_worker_processes.py::test_h08_pause_blocks_new_public_messages_and_worker_restart` |
| `H-09` | `tests/test_phase3_worker_processes.py::test_h09_running_conversation_rejects_profile_edits` |
| `H-10` | `tests/test_phase3_worker_processes.py::test_h10_guardrails_reload_only_at_next_run_checkpoint` |
| `H-11` | `tests/test_phase3_worker_processes.py::test_h11_live_retryable_attempts_are_persisted_and_eventually_commit` and `tests/test_phase3_worker_processes.py::test_h11_live_non_retryable_failure_converges_without_public_commit` |
| `H-12` | `tests/test_phase2_public_messages.py::test_h12_failed_projection_does_not_consume_sequence` |
| `H-13` | `tests/test_phase2_public_messages.py::test_h13_reconnect_replays_missing_events_without_duplicate_dispatch` |
| `H-14` | `tests/test_phase6_analysis.py::test_h14_history_index_and_raw_fallback_stay_read_only` |
| `H-15` | `tests/test_phase6_analysis.py::test_h15_analysis_export_zip_is_versioned_redacted_and_downloadable` |
| `H-16` | `tests/test_phase3_worker_processes.py::test_h16_live_conversation_budgets_reject_followup_attempts_without_commit` |

## Executed Validation Commands

| Surface | Command | Latest result |
| --- | --- | --- |
| Backend lint | `uv run ruff check .` | PASS |
| Backend typing | `uv run mypy apps/server/src tests` | PASS |
| Backend regression | `uv run pytest` | PASS, `54 passed` |
| Frontend unit | `cd apps/web && npm test` | PASS, `3 passed` |
| Frontend build | `cd apps/web && npm run build` | PASS |
| Browser acceptance | `cd apps/web && npm run test:e2e` | PASS, `2 passed` |
| POSIX bootstrap/start/health/stop | `./scripts/validate-stage7.sh` | PASS |
| Performance baseline | `./scripts/run-stage7-perf.sh --runs 20 --ui-runs 8` | PASS for hard gates |

## Performance Exit Checks

The latest `docs/stage7-performance-baseline.md` / `var/metrics/stage7-performance-baseline.json`
were regenerated on `2026-08-01` with the reproducible perf harness.

| Metric | Threshold | Latest P95 (ms) | Status |
| --- | ---: | ---: | --- |
| `message_commit_non_model` | `< 50` | `41.26` | PASS |
| `messages_recent_1000` | `< 200` | `48.60` | PASS |
| `ui_sidebar_select_1440x900` | record only | `3318.26` | RECORDED, non-blocking |

## Runtime Cleanliness

The candidate was re-checked after the final validation run:

- `.dev/runtime` contains only `runtime.env`; no lingering `api.pid` or `web.pid`.
- `var/runtime-data/current/catalog.sqlite` has no `running` or `paused` conversations.
- `var/runtime-data/perf-baseline/catalog.sqlite` has no `running` or `paused` conversations.
- Transcript scans for both runtime roots found zero `RUNNING` agent runs, zero non-idle typing
  authority snapshots, zero pending outbox rows, and no `conversation_seq` gaps
  (`COUNT(conversation_seq) == MAX(conversation_seq)`).

## Related Artifacts

- Runtime scripts: `scripts/bootstrap.sh`, `scripts/start.sh`, `scripts/stop.sh`, `scripts/health.sh`
- Windows entrypoints: `scripts/bootstrap.ps1`, `scripts/start.ps1`, `scripts/stop.ps1`, `scripts/health.ps1`
- Cutover notes: `docs/cutover-and-rollback.md`
- Performance report: `docs/stage7-performance-baseline.md`
