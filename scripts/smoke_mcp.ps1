param(
    [string]$BaseUrl = "http://127.0.0.1:8124"
)
$ErrorActionPreference = "Stop"
$headers = @{ "Accept" = "application/json, text/event-stream"; "Content-Type" = "application/json" }
$base = $BaseUrl.TrimEnd('/') + "/mcp"

function Rpc($body) {
    (Invoke-RestMethod -Method Post -Uri $base -Headers $headers -Body ($body | ConvertTo-Json -Depth 8))
}

Write-Output "Target: $base"
$init = Rpc @{
    jsonrpc = "2.0"; id = 1; method = "initialize"
    params = @{ protocolVersion = "2024-11-05"; capabilities = @{}
                clientInfo = @{ name = "smoke"; version = "0" } }
}
Write-Output ("SERVER : {0} v{1}  (protocol {2})" -f $init.result.serverInfo.name, $init.result.serverInfo.version, $init.result.protocolVersion)

$tools = Rpc @{ jsonrpc = "2.0"; id = 2; method = "tools/list"; params = @{} }
Write-Output "TOOLS  :"
foreach ($t in $tools.result.tools) {
    Write-Output ("  - {0}" -f $t.name)
}

$call = Rpc @{
    jsonrpc = "2.0"; id = 3; method = "tools/call"
    params = @{ name = "find_solutions"; arguments = @{ problem = "I need to track my team's expenses and submit receipts"; top_k = 3 } }
}
Write-Output "CALL find_solutions ->"
foreach ($c in $call.result.content) { Write-Output $c.text }
