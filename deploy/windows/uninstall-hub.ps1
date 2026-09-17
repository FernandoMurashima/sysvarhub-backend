param(
    [string]$InstallRoot = "C:\Program Files\Sysvar Hub"
)

$ErrorActionPreference = "Stop"

foreach ($service in @("SysvarHub", "SysvarHubMySQL")) {
    $existing = Get-Service -Name $service -ErrorAction SilentlyContinue
    if ($existing) {
        if ($existing.Status -ne "Stopped") {
            Stop-Service -Name $service -Force -ErrorAction SilentlyContinue
        }
        sc.exe delete $service | Out-Null
    }
}

Get-NetFirewallRule -DisplayName "Sysvar Hub" -ErrorAction SilentlyContinue | Remove-NetFirewallRule

if (Test-Path $InstallRoot) {
    Remove-Item -LiteralPath $InstallRoot -Recurse -Force
}

Write-Host "Sysvar Hub removido. C:\ProgramData\SysvarHub foi preservado."
