# Dot-source has no side effects. Transmission remains opt-in.
. (Join-Path $PSScriptRoot 'mysql-backup.ps1')

function Get-HubBackupTransmissionPolicy {
    param([string]$ProgramDataRoot)
    $policy=[pscustomobject]@{version=1;enabled=$false;bandwidth_bytes_per_second=262144;cycle_seconds=120;request_timeout_seconds=30;finalize_timeout_seconds=300}
    $path=Assert-HubBackupLocalPath (Join-Path $ProgramDataRoot 'config\backup-transmission.json')
    if (Test-Path -LiteralPath $path) { $policy=Read-HubBackupJson $path }
    if ($policy.version -ne 1 -or $policy.enabled -isnot [bool]) { throw 'Politica de transmissao invalida.' }
    foreach ($rule in @(@('bandwidth_bytes_per_second',16384,16777216),@('cycle_seconds',1,900),@('request_timeout_seconds',1,120),@('finalize_timeout_seconds',1,3600))) {
        $value=$policy.($rule[0])
        if ($value -notmatch '^[0-9]+$' -or [long]$value -lt $rule[1] -or [long]$value -gt $rule[2]) { throw 'Limite de transmissao invalido.' }
    }
    return $policy
}

function Get-HubBackupTransmissionPlan {
    param([string]$InstallRoot,[string]$ProgramDataRoot,$Policy)
    $backupRoot=Assert-HubBackupLocalPath (Join-Path $ProgramDataRoot 'backup')
    $mysqlRoot=Join-Path $InstallRoot 'mysql'
    Assert-HubBackupService $mysqlRoot $ProgramDataRoot
    $null=Assert-HubMySqlToolchain $mysqlRoot
    $credential=Join-Path $ProgramDataRoot 'config\mysql-backup.cnf'
    Assert-HubBackupCredential $credential
    $server=Get-HubBackupServerState $mysqlRoot $ProgramDataRoot $credential
    $lock=Enter-HubBackupLock $backupRoot
    try {
        $directories=@(); $knownFulls=@{}
        foreach ($full in @(Get-ChildItem -LiteralPath $backupRoot -Directory -Force | Where-Object { $_.Name -like 'full-*' } | Sort-Object Name)) {
            # Reuse exact continuity/identity/archive/anchor validation from capture.
            $chain=Get-HubIncrementalChain $backupRoot $full.FullName $server
            $knownFulls[$chain.full.backup_id]=$true
            $directories+= $full.Name
            foreach ($entry in @(Get-ChildItem -LiteralPath $backupRoot -Directory -Force | Where-Object { $_.Name -like ('incremental-' + $chain.full.backup_id + '-*') } | Sort-Object Name)) { $directories+= $entry.Name }
        }
        foreach ($entry in @(Get-ChildItem -LiteralPath $backupRoot -Directory -Force | Where-Object { $_.Name -like 'incremental-*' })) {
            if ($entry.Name -notmatch '^incremental-([a-f0-9]{32})-' -or -not $knownFulls.ContainsKey($Matches[1])) { throw 'Incremental sem FULL; suporte necessario.' }
        }
        return [ordered]@{version=1;service='SysvarHubMySQL';port=3307;server_uuid=$server.server_uuid;backup_root=$backupRoot;policy=$Policy;directories=@($directories)}
    } finally { $lock.Dispose() }
}

function Invoke-HubBackupTransmissionRuntime {
    param([string]$InstallRoot,[string]$PlanPath,[switch]$ResumeAfterSupport)
    $runtime=Assert-HubBackupLocalPath (Join-Path $InstallRoot 'runtime\SysvarHubService.exe')
    if (-not (Test-Path -LiteralPath $runtime -PathType Leaf)) { throw 'Runtime oficial indisponivel.' }
    $arguments=@('manage','transmitir_backups_hub','--plan',$PlanPath)
    if ($ResumeAfterSupport) { $arguments+= '--resume-after-support' }
    # Credential is read internally through the existing HubConfig; never on argv/stdout.
    $output=@(& $runtime @arguments 2>$null)
    if ($LASTEXITCODE -ne 0) { throw 'Transmissor indisponivel.' }
    $status=[string]($output | Select-Object -Last 1)
    if ($status -notin @('DESATIVADO','BACKUP_LOCAL_VALIDO','ENVIO_PENDENTE','ENVIO_EM_ANDAMENTO','RECEBIDO_CENTRAL','FALHA_REPETIR','INTERVENCAO_SUPORTE')) { throw 'Resposta do transmissor invalida.' }
    return $status
}

function Invoke-HubBackupTransmission {
    param([string]$InstallRoot='C:\Program Files\Sysvar Hub',[string]$ProgramDataRoot='C:\ProgramData\SysvarHub',[switch]$ResumeAfterSupport)
    $ErrorActionPreference='Stop'
    $backupRoot=$null; $planPath=$null; $previousData=$env:SYSVARHUB_PROGRAMDATA
    try {
        $policy=Get-HubBackupTransmissionPolicy $ProgramDataRoot
        if (-not $policy.enabled) { return 'DESATIVADO' }
        Assert-HubBackupOperator
        [Diagnostics.Process]::GetCurrentProcess().PriorityClass=[Diagnostics.ProcessPriorityClass]::BelowNormal
        $InstallRoot=Assert-HubBackupLocalPath $InstallRoot
        $ProgramDataRoot=Assert-HubBackupLocalPath $ProgramDataRoot
        $backupRoot=Assert-HubBackupLocalPath (Join-Path $ProgramDataRoot 'backup')
        Protect-SecretFile $backupRoot
        if ((Get-HubBackupFreeBytes $backupRoot) -lt 16MB) { throw 'Espaco local insuficiente.' }
        $plan=Get-HubBackupTransmissionPlan $InstallRoot $ProgramDataRoot $policy
        $planPath=Join-Path $backupRoot ('.send-plan-' + [guid]::NewGuid().ToString('N') + '.json')
        Write-HubBackupJson $planPath $plan
        $env:SYSVARHUB_PROGRAMDATA=$ProgramDataRoot
        return Invoke-HubBackupTransmissionRuntime $InstallRoot $planPath -ResumeAfterSupport:$ResumeAfterSupport
    } catch {
        # Separate preflight diagnostic; never replaces local backup validity/checkpoint.
        if ($backupRoot -and (Test-Path -LiteralPath $backupRoot)) {
            try {
                $path=Assert-HubBackupLocalPath (Join-Path $backupRoot 'backup-transmission-preflight.json')
                $temp=Join-Path $backupRoot ('.send-error-' + [guid]::NewGuid().ToString('N') + '.json')
                Write-HubBackupJson $temp @{version=1;status='INTERVENCAO_SUPORTE';failure_code='PREFLIGHT';last_attempt_utc=[DateTime]::UtcNow.ToString('o')}
                if (Test-Path -LiteralPath $path) { [IO.File]::Replace($temp,$path,[System.Management.Automation.Language.NullString]::Value) } else { [IO.File]::Move($temp,$path) }
            } catch { }
        }
        return 'INTERVENCAO_SUPORTE'
    } finally {
        $env:SYSVARHUB_PROGRAMDATA=$previousData
        if ($planPath -and [IO.File]::Exists($planPath)) { [IO.File]::Delete($planPath) }
    }
}
