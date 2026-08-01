$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $PSScriptRoot
$ApiPort = if ($env:PAL_CHAT_API_PORT) { $env:PAL_CHAT_API_PORT } else { "8000" }
$WebPort = if ($env:PAL_CHAT_WEB_PORT) { $env:PAL_CHAT_WEB_PORT } else { "5173" }
$CurrentDataRoot = if ($env:PAL_CHAT_DATA_DIR) { $env:PAL_CHAT_DATA_DIR } else { Join-Path $RootDir "var/runtime-data/current" }
$LegacyDataRoot = Join-Path $RootDir "var/data"

$pythonVersion = & py -3.12 --version
if ($LASTEXITCODE -ne 0) {
  throw "Python 3.12 is required."
}

$nodeMajor = node -p "process.versions.node.split('.')[0]"
if ($nodeMajor -ne "22") {
  throw "Node 22.x is required."
}

New-Item -ItemType Directory -Force -Path $CurrentDataRoot | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $RootDir ".dev/runtime") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $RootDir "var/log") | Out-Null

if (!(Test-Path (Join-Path $RootDir ".env"))) {
  Copy-Item (Join-Path $RootDir ".env.example") (Join-Path $RootDir ".env")
}

Push-Location $RootDir
 $env:PAL_CHAT_DATA_DIR = $CurrentDataRoot
 $env:PAL_CHAT_SERVER_BASE_URL = "http://127.0.0.1:$ApiPort"
uv sync --group dev
npm --prefix apps/web ci
uv run alembic upgrade head
Pop-Location

Write-Host "Bootstrap complete."
Write-Host "Python: $pythonVersion"
Write-Host "Node: $(node --version)"
Write-Host "Data root: $CurrentDataRoot"
Write-Host "Legacy read-only root (not migrated): $LegacyDataRoot"
