# Live smoke test for the Phase 3 execution gateway.
[CmdletBinding()]
param(
    [string]$BaseUrl = "https://singularity-osd2.onrender.com"
)

$ErrorActionPreference = "Stop"

function Invoke-Post {
    param([string]$Path, [object]$Body)
    $json = $Body | ConvertTo-Json -Depth 8
    return Invoke-RestMethod -Uri "$BaseUrl$Path" -Method Post -ContentType "application/json" -Body $json
}

Write-Host "==> Gateway smoke against $BaseUrl" -ForegroundColor Cyan

Write-Host "`n--> health"
$health = Invoke-RestMethod -Uri "$BaseUrl/health" -Method Get
Write-Host "    status=$($health.status) indexed_tools=$($health.indexed_tools)"

Write-Host "`n--> weather London"
try {
    $r = Invoke-Post "/api/v1/execute" @{ provider_slug = "weather"; arguments = @{ location = "London" } }
    Write-Host "    ok=$($r.ok) | $($r.result.location) | $($r.result.temperature)$($r.result.temperature_unit) | $($r.result.condition) | audited=$($r.audited) | ${($r.latency_ms)}ms"
} catch {
    Write-Host "    FAILED: $($_.Exception.Message)" -ForegroundColor Red
}

Write-Host "`n--> npm_lookup express"
try {
    $r = Invoke-Post "/api/v1/execute" @{ provider_slug = "npms_lookup"; arguments = @{ package = "express" } }
    Write-Host "    ok=$($r.ok) | $($r.result.name)@$($r.result.version) | score_final=$($r.result.score_final) | audited=$($r.audited)"
} catch {
    Write-Host "    FAILED: $($_.Exception.Message)" -ForegroundColor Red
}

Write-Host "`n--> pypi_lookup requests"
try {
    $r = Invoke-Post "/api/v1/execute" @{ provider_slug = "pypi_lookup"; arguments = @{ package = "requests" } }
    Write-Host "    ok=$($r.ok) | $($r.result.name)@$($r.result.version) | num_versions=$($r.result.num_versions) | audited=$($r.audited)"
} catch {
    Write-Host "    FAILED: $($_.Exception.Message)" -ForegroundColor Red
}

Write-Host "`n--> web_search 'W3Schools'"
try {
    $r = Invoke-Post "/api/v1/execute" @{ provider_slug = "web_search"; arguments = @{ query = "W3Schools"; max_results = 2 } }
    Write-Host "    ok=$($r.ok) | count=$($r.result.count) | first=$($r.result.results[0].title) | audited=$($r.audited)"
} catch {
    Write-Host "    FAILED: $($_.Exception.Message)" -ForegroundColor Red
}

Write-Host "`n--> denied call (scope github:write on weather)"
try {
    $r = Invoke-Post "/api/v1/execute" @{ provider_slug = "weather"; arguments = @{ location = "London" }; scope = "github:write" }
    Write-Host "    UNEXPECTED success" -ForegroundColor Yellow
} catch {
    Write-Host "    correctly rejected HTTP $($_.Exception.Response.StatusCode.value__)" -ForegroundColor Green
}

Write-Host "`nDone."