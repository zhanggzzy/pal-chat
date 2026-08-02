$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $PSScriptRoot
$RuntimeDir = Join-Path $RootDir ".dev/runtime"

foreach ($name in @("api.pid", "web.pid")) {
  $path = Join-Path $RuntimeDir $name
  if (Test-Path $path) {
    $processId = Get-Content $path
    try {
      Stop-Process -Id $processId -Force -ErrorAction Stop
    } catch {
      Write-Warning "Failed to stop PID $processId from ${name}: $_"
    }
    Remove-Item $path -Force
  }
}

Write-Host "Stopped pal-chat runtime processes."
