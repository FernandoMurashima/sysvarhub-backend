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

Write-Host "Limpeza operacional concluida. O Inno Setup removera os arquivos em Program Files. C:\ProgramData\SysvarHub foi preservado."
