param(
    [string]$InstallRoot = 'C:\Program Files\Sysvar Hub',
    [string]$ProgramDataRoot = 'C:\ProgramData\SysvarHub',
    [ValidateRange(1,86400)][int]$DumpTimeoutSeconds = 1200
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'mysql-backup.ps1')
try {
    $destination = Invoke-HubMySqlFullBackup -InstallRoot $InstallRoot -ProgramDataRoot $ProgramDataRoot -DumpTimeoutSeconds $DumpTimeoutSeconds
    Write-Host "Backup completo valido publicado em: $destination"
} catch {
    Write-Error 'Backup completo nao publicado. Verifique os pre-requisitos documentados; backups validos anteriores foram preservados.' -ErrorAction Continue
    exit 1
}
