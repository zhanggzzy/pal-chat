$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $PSScriptRoot
$RuntimeDir = Join-Path $RootDir ".dev/runtime"

foreach ($name in @("api.pid", "web.pid")) {
  $path = Join-Path $RuntimeDir $name
  if (Test-Path $path) {
    $pid = Get-Content $path
    try {
      Stop-Process -Id $pid -Force -ErrorAction Stop
    } catch {
      Write-Warning "Failed to stop PID $pid from ${name}: $_"
    }
    Remove-Item $path -Force
  }
}

Write-Host "Stopped pal-chat runtime processes."
