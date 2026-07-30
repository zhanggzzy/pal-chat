# Stage 7 Cutover And Rollback

## Runtime roots

- New runtime data root: `var/runtime-data/current`
- Legacy local root kept for inspection only: `var/data`
- User-home fallback root from older development flows: `${XDG_DATA_HOME:-~/.local/share}/pal-chat`

This repository does **not** migrate legacy data forward and does **not**
support reverse-compat replay into older layouts. The stage-7 cutover path is a
fresh runtime root plus a documented rollback to the previous local root.

## Recommended local cutover

1. Stop any existing local runtime with `./scripts/stop.sh`.
2. Preserve the prior root as read-only evidence:
   - `chmod -R a-w var/data` on POSIX if you need local protection.
   - On Windows, remove write access in Explorer or archive the directory.
3. Bootstrap the reproducible toolchain:
   - POSIX: `./scripts/bootstrap.sh`
   - PowerShell: `./scripts/bootstrap.ps1`
4. Start the fresh runtime:
   - POSIX: `./scripts/start.sh`
   - PowerShell: `./scripts/start.ps1`
5. Verify health:
   - POSIX: `./scripts/health.sh`
   - PowerShell: `./scripts/health.ps1`

## Rollback

Rollback here means stopping the stage-7 runtime and returning to the previous
local entry flow, not data migration.

1. Stop the stage-7 runtime with `./scripts/stop.sh` or `./scripts/stop.ps1`.
2. Keep `var/runtime-data/current` for postmortem comparison.
3. Restore the older launch path only if you explicitly want the prior local
   debug flow:
   - `./scripts/dev.sh`
   - or the WSL pair `./scripts/serve-wsl.sh` and `./scripts/stop-wsl.sh`
4. Point `PAL_CHAT_DATA_DIR` back to your prior local root if needed.

## Known limits

- No old-data migration is performed.
- No reverse compatibility is promised between the new runtime root and older
  entry scripts.
- Cross-platform scripts are delivered together, but this repository currently
  validates only the POSIX path locally unless PowerShell is available on the
  validation host.
