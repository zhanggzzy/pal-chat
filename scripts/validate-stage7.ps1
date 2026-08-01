$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $PSScriptRoot
$ApiPort = if ($env:PAL_CHAT_API_PORT) { $env:PAL_CHAT_API_PORT } else { "28000" }
$WebPort = if ($env:PAL_CHAT_WEB_PORT) { $env:PAL_CHAT_WEB_PORT } else { "28173" }

function Invoke-Stage7Script {
  param([string]$ScriptName)
  & (Join-Path $RootDir "scripts/$ScriptName") @args
}

try {
  $env:PAL_CHAT_API_PORT = $ApiPort
  $env:PAL_CHAT_WEB_PORT = $WebPort
  & (Join-Path $RootDir "scripts/stop.ps1") | Out-Null
  & (Join-Path $RootDir "scripts/bootstrap.ps1")
  & (Join-Path $RootDir "scripts/start.ps1")
  & (Join-Path $RootDir "scripts/health.ps1")
  & (Join-Path $RootDir "scripts/stop.ps1")

  $runtimeDir = Join-Path $RootDir ".dev/runtime"
  if ((Test-Path (Join-Path $runtimeDir "api.pid")) -or (Test-Path (Join-Path $runtimeDir "web.pid"))) {
    throw "PID files still exist after stop."
  }
  Write-Host "Stage 7 PowerShell runtime validation passed."
} finally {
  & (Join-Path $RootDir "scripts/stop.ps1") | Out-Null
}
