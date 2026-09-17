param(
    [string]$AppRoot = "C:\Program Files\Sysvar Hub\app",
    [string]$ConfigRoot = "C:\ProgramData\SysvarHub\config",
    [string]$LogRoot = "C:\ProgramData\SysvarHub\logs",
    [string]$DataRoot = "C:\ProgramData\SysvarHub\data",
    [string]$BackupRoot = "C:\ProgramData\SysvarHub\backup"
)

$ErrorActionPreference = "Stop"

New-Item -ItemType Directory -Force -Path $AppRoot, $ConfigRoot, $LogRoot, $DataRoot, $BackupRoot | Out-Null

$envTemplate = Join-Path $PSScriptRoot "sysvarhub.env.example"
$envTarget = Join-Path $ConfigRoot "sysvarhub.env"

if ((Test-Path $envTemplate) -and -not (Test-Path $envTarget)) {
    Copy-Item -LiteralPath $envTemplate -Destination $envTarget
}

Write-Host "Runtime preparado. Revise $envTarget antes de iniciar o Hub."
