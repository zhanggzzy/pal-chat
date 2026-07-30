$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $PSScriptRoot
$RuntimeDir = Join-Path $RootDir ".dev/runtime"
$ApiPort = if ($env:PAL_CHAT_API_PORT) { $env:PAL_CHAT_API_PORT } else { "8000" }
$WebPort = if ($env:PAL_CHAT_WEB_PORT) { $env:PAL_CHAT_WEB_PORT } else { "5173" }
$CurrentDataRoot = Join-Path $RootDir "var/runtime-data/current"
$LegacyDataRoot = Join-Path $RootDir "var/data"
$ApiLog = Join-Path $RootDir "var/log/api.log"
$WebLog = Join-Path $RootDir "var/log/web.log"

New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
New-Item -ItemType Directory -Force -Path $CurrentDataRoot | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $RootDir "var/log") | Out-Null

$env:PAL_CHAT_DATA_DIR = $CurrentDataRoot
$env:PAL_CHAT_CORS_ORIGINS = "[`"http://127.0.0.1:$WebPort`",`"http://localhost:$WebPort`"]"

$api = Start-Process -FilePath "uv" -ArgumentList @("run","uvicorn","pal_chat_server.main:app","--app-dir","apps/server/src","--host","127.0.0.1","--port",$ApiPort) -WorkingDirectory $RootDir -RedirectStandardOutput $ApiLog -RedirectStandardError $ApiLog -PassThru
$web = Start-Process -FilePath "npm" -ArgumentList @("--prefix","apps/web","run","dev","--","--host","127.0.0.1","--port",$WebPort,"--strictPort") -WorkingDirectory $RootDir -RedirectStandardOutput $WebLog -RedirectStandardError $WebLog -PassThru

Set-Content -Path (Join-Path $RuntimeDir "api.pid") -Value $api.Id
Set-Content -Path (Join-Path $RuntimeDir "web.pid") -Value $web.Id

Write-Host "Started pal-chat."
Write-Host "Frontend: http://127.0.0.1:$WebPort"
Write-Host "Backend: http://127.0.0.1:$ApiPort"
Write-Host "Docs: http://127.0.0.1:$ApiPort/docs"
Write-Host "Data root: $CurrentDataRoot"
Write-Host "Legacy read-only root: $LegacyDataRoot"
