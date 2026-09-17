param(
    [string]$HealthUrl = "http://127.0.0.1:8000/api/health/",
    [string]$LogRoot = "C:\ProgramData\SysvarHub\logs"
)

$ErrorActionPreference = "SilentlyContinue"

function ServiceStatus($Name) {
    $service = Get-Service -Name $Name
    if ($service) { return $service.Status.ToString() }
    return "not-installed"
}

$health = "falhou"
try {
    $response = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 5
    if ($response.status -eq "ok" -and $response.service -eq "sysvar-hub") {
        $health = "ok"
    }
} catch {}

$ips = Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike "127.*" -and $_.PrefixOrigin -ne "WellKnown" } |
    Select-Object -ExpandProperty IPAddress

Write-Host "SysvarHub: $(ServiceStatus 'SysvarHub')"
Write-Host "SysvarHubMySQL: $(ServiceStatus 'SysvarHubMySQL')"
Write-Host "Health check: $health"
Write-Host "Porta 8000: $((Get-NetTCPConnection -LocalPort 8000 -State Listen).Count)"
Write-Host "Porta local 3307: $((Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 3307 -State Listen).Count)"
Write-Host "Versao: $(Get-Content -LiteralPath (Join-Path $PSScriptRoot '..\VERSION') -ErrorAction SilentlyContinue)"
Write-Host "Hostname: $env:COMPUTERNAME"
Write-Host "IPs LAN: $($ips -join ', ')"
Write-Host "Logs: $LogRoot"
