param(
    [string]$ProgramDataRoot = "C:\ProgramData\SysvarHub"
)

$ErrorActionPreference = "Stop"

Write-Host "Este script remove dados, configuracao, logs e banco local do Sysvar Hub."
Write-Host "Execute manualmente apenas quando tiver certeza."

if (Test-Path $ProgramDataRoot) {
    Remove-Item -LiteralPath $ProgramDataRoot -Recurse -Force
}
