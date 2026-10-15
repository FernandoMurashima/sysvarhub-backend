param(
    [Parameter(Mandatory=$true)][string]$FullBackupDirectory,
    [string]$InstallRoot='C:\Program Files\Sysvar Hub',
    [string]$ProgramDataRoot='C:\ProgramData\SysvarHub',
    [ValidateRange(1,86400)][int]$CaptureTimeoutSeconds=1200
)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'mysql-backup.ps1')
try {
    $destination=Invoke-HubMySqlIncrementalBackup -FullBackupDirectory $FullBackupDirectory -InstallRoot $InstallRoot -ProgramDataRoot $ProgramDataRoot -CaptureTimeoutSeconds $CaptureTimeoutSeconds
    if ($destination) { Write-Host "Incremental valido publicado em: $destination" }
    else { Write-Host 'Sem alteracoes relevantes; nenhum incremental publicado.' }
} catch {
    Write-Error 'Captura incremental nao confirmada. Verifique continuidade e pre-requisitos; cadeia valida anterior preservada.' -ErrorAction Continue
    exit 1
}
