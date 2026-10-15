param([string]$InstallRoot='C:\Program Files\Sysvar Hub',[string]$ProgramDataRoot='C:\ProgramData\SysvarHub')
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'mysql-backup-task.ps1')
try { Get-HubBackupTaskStatus -InstallRoot $InstallRoot -ProgramDataRoot $ProgramDataRoot }
catch { Write-Error 'Operacao do agendamento bloqueada; verificar instalacao e identidade da tarefa com o suporte.' -ErrorAction Continue; exit 1 }
