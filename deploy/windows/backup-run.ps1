param([string]$InstallRoot='C:\Program Files\Sysvar Hub',[string]$ProgramDataRoot='C:\ProgramData\SysvarHub',[switch]$ResumeAfterSupport)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'mysql-backup-control.ps1')
try {
    [Diagnostics.Process]::GetCurrentProcess().PriorityClass=[Diagnostics.ProcessPriorityClass]::BelowNormal
    $result=Invoke-HubBackupControl -InstallRoot $InstallRoot -ProgramDataRoot $ProgramDataRoot -ResumeAfterSupport:$ResumeAfterSupport
    Write-Output $result.status
    if ($result.status -in @('FALHA_REPETIR','INTERVENCAO_SUPORTE','ESPACO_INSUFICIENTE')) { exit 1 }
} catch { Write-Error 'Controle de backup indisponivel; suporte deve verificar instalacao, permissoes e diagnostico local.' -ErrorAction Continue; exit 1 }
