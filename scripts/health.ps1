$ErrorActionPreference = "Stop"

$ApiPort = if ($env:PAL_CHAT_API_PORT) { $env:PAL_CHAT_API_PORT } else { "8000" }
$WebPort = if ($env:PAL_CHAT_WEB_PORT) { $env:PAL_CHAT_WEB_PORT } else { "5173" }

Write-Host "== backend live =="
Invoke-RestMethod -Uri "http://127.0.0.1:$ApiPort/health/live" | ConvertTo-Json -Depth 4
Write-Host "== backend ready =="
Invoke-RestMethod -Uri "http://127.0.0.1:$ApiPort/health/ready" | ConvertTo-Json -Depth 4
Write-Host "== frontend =="
$response = Invoke-WebRequest -Uri "http://127.0.0.1:$WebPort" -UseBasicParsing
Write-Host "OK $($response.StatusCode) http://127.0.0.1:$WebPort"
