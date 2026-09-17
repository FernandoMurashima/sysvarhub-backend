param(
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"

$connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (-not $connections) {
    Write-Host "Nenhum processo ouvindo na porta $Port."
    exit 0
}

$processIds = $connections | Select-Object -ExpandProperty OwningProcess -Unique
foreach ($processId in $processIds) {
    Stop-Process -Id $processId -Force
}

Write-Host "Hub parado na porta $Port."
