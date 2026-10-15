# Funcoes puras ao importar: nenhuma tarefa, consulta MySQL ou backup e acionado.
. (Join-Path $PSScriptRoot 'mysql-backup.ps1')

function Get-HubBackupPolicy {
    param([string]$ProgramDataRoot)
    $policy=[pscustomobject]@{version=1;renewal_days=7;window_start='03:00';window_end='04:00';minimum_free_bytes=1GB;warning_storage_bytes=64GB;timeout_seconds=1200}
    $path=Assert-HubBackupLocalPath (Join-Path $ProgramDataRoot 'config\backup-policy.json')
    if (Test-Path -LiteralPath $path) { $policy=Read-HubBackupJson $path }
    if ($policy.version -ne 1 -or $policy.renewal_days -notmatch '^[0-9]+$' -or [int]$policy.renewal_days -lt 1 -or [int]$policy.renewal_days -gt 365 -or $policy.window_start -notmatch '^([01][0-9]|2[0-3]):[0-5][0-9]$' -or $policy.window_end -notmatch '^([01][0-9]|2[0-3]):[0-5][0-9]$' -or $policy.window_start -eq $policy.window_end -or $policy.minimum_free_bytes -notmatch '^[0-9]+$' -or [double]$policy.minimum_free_bytes -lt 256MB -or $policy.warning_storage_bytes -notmatch '^[0-9]+$' -or [double]$policy.warning_storage_bytes -lt 1MB -or $policy.timeout_seconds -notmatch '^[0-9]+$' -or [int]$policy.timeout_seconds -lt 1 -or [int]$policy.timeout_seconds -gt 86400) { throw 'Politica de backup invalida; adequacao assistida necessaria.' }
    return $policy
}

function Test-HubFullRenewalWindow {
    param($Policy,[DateTimeOffset]$LastFull,[DateTimeOffset]$Now)
    if (($Now.ToUniversalTime()-$LastFull.ToUniversalTime()).TotalDays -lt [int]$Policy.renewal_days) { return $false }
    $time=$Now.LocalDateTime.TimeOfDay
    $start=[TimeSpan]::ParseExact($Policy.window_start,'hh\:mm',[Globalization.CultureInfo]::InvariantCulture)
    $end=[TimeSpan]::ParseExact($Policy.window_end,'hh\:mm',[Globalization.CultureInfo]::InvariantCulture)
    if ($start -lt $end) { return ($time -ge $start -and $time -lt $end) }
    return ($time -ge $start -or $time -lt $end)
}

function Write-HubBackupControlState {
    param([string]$BackupRoot,$State)
    $path=Assert-HubBackupLocalPath (Join-Path $BackupRoot 'backup-control.json')
    $temp=Join-Path $BackupRoot ('.control-' + [guid]::NewGuid().ToString('N') + '.json')
    try {
        Write-HubBackupJson $temp $State
        if (Test-Path -LiteralPath $path) { [IO.File]::Replace($temp,$path,[System.Management.Automation.Language.NullString]::Value) }
        else { [IO.File]::Move($temp,$path) }
    } finally { if ([IO.File]::Exists($temp)) { [IO.File]::Delete($temp) } }
}

function Get-HubBackupStorageBytes {
    param([string]$BackupRoot)
    # Nao seguir junctions nem ler conteudo SQL/binlog. Somente metadados de arquivos.
    $total=0L
    foreach ($item in Get-ChildItem -LiteralPath $BackupRoot -Force) {
        $null=Assert-HubBackupLocalPath $item.FullName
        if ($item.PSIsContainer) { $total+=Get-HubBackupStorageBytes $item.FullName }
        else { $total+=$item.Length }
    }
    return $total
}

function Set-HubBackupChainDiagnostic {
    param($Diagnostic,[string]$FullDirectory,$Chain,[string]$BackupRoot)
    $Diagnostic.active_full=Split-Path $FullDirectory -Leaf
    $Diagnostic.active_full_sha256=$Chain.full_hash
    $Diagnostic.last_full=[ordered]@{directory=$Diagnostic.active_full;id=$Chain.full.backup_id;completed_at_utc=$Chain.full.completed_at_utc}
    if ($Chain.sequence -gt 0) {
        $name='incremental-' + $Chain.full.backup_id + '-' + ([int]$Chain.sequence).ToString('D8') + '-' + $Chain.last_id
        $manifest=Read-HubBackupJson (Join-Path (Join-Path $BackupRoot $name) 'manifest.json')
        $Diagnostic.last_incremental=[ordered]@{directory=$name;id=$Chain.last_id;full_backup_id=$Chain.full.backup_id;completed_at_utc=$manifest.completed_at_utc;sequence=$Chain.sequence}
    }
}

function Invoke-HubBackupControl {
    param([string]$InstallRoot='C:\Program Files\Sysvar Hub',[string]$ProgramDataRoot='C:\ProgramData\SysvarHub',[switch]$ResumeAfterSupport)
    $ErrorActionPreference='Stop'
    Assert-HubBackupOperator
    $InstallRoot=Assert-HubBackupLocalPath $InstallRoot
    $ProgramDataRoot=Assert-HubBackupLocalPath $ProgramDataRoot
    $backupRoot=Assert-HubBackupLocalPath (Join-Path $ProgramDataRoot 'backup')
    $mysqlRoot=Join-Path $InstallRoot 'mysql'; $credential=Join-Path $ProgramDataRoot 'config\mysql-backup.cnf'
    $null=New-Item -ItemType Directory -Path $backupRoot -Force
    Protect-SecretFile $backupRoot
    try { $lock=Enter-HubBackupLock $backupRoot }
    catch { return [pscustomobject]@{status='EM_EXECUCAO';support_required=$false;failure_code='LOCK_BUSY'} }
    $clock=[Diagnostics.Stopwatch]::StartNew(); $phase='STATE'; $canWrite=$true
    $now=[DateTimeOffset]::Now
    $diagnostic=[pscustomobject][ordered]@{version=1;active_full=$null;active_full_sha256=$null;last_full=$null;last_incremental=$null;last_attempt_utc=$now.UtcDateTime.ToString('o');last_success_utc=$null;status='EXECUTANDO';support_required=$false;failure_code=$null;elapsed_ms=0;published_bytes=0L;storage_bytes=$null;free_bytes=$null;warnings=@()}
    try {
        $path=Join-Path $backupRoot 'backup-control.json'
        if (Test-Path -LiteralPath $path) {
            $canWrite=$false
            $previous=Read-HubBackupJson $path
            if ($previous.version -ne 1 -or ($previous.active_full -and ($previous.active_full -notmatch '^full-[0-9]{8}T[0-9]{9}Z-[a-f0-9]{32}$' -or $previous.active_full_sha256 -notmatch '^[a-f0-9]{64}$'))) { throw 'Controle local invalido.' }
            $canWrite=$true
            foreach ($key in @('active_full','active_full_sha256','last_full','last_incremental','last_success_utc')) { $diagnostic.$key=$previous.$key }
            if ($previous.support_required -and -not $ResumeAfterSupport) {
                $diagnostic.status='INTERVENCAO_SUPORTE'; $diagnostic.support_required=$true; $diagnostic.failure_code='CHAIN_BLOCKED'
                return [pscustomobject]$diagnostic
            }
        }
        Write-HubBackupControlState $backupRoot $diagnostic
        $phase='MYSQL'; Assert-HubBackupService $mysqlRoot $ProgramDataRoot
        $phase='POLICY'; $policy=Get-HubBackupPolicy $ProgramDataRoot
        $diagnostic.free_bytes=Get-HubBackupFreeBytes $backupRoot
        $diagnostic.storage_bytes=Get-HubBackupStorageBytes $backupRoot
        if ($diagnostic.storage_bytes -ge [long]$policy.warning_storage_bytes) { $diagnostic.warnings+= 'ARMAZENAMENTO_ELEVADO_ACIONAR_SUPORTE' }
        if ($diagnostic.free_bytes -lt [long]$policy.minimum_free_bytes) { $diagnostic.status='ESPACO_INSUFICIENTE'; $diagnostic.failure_code='LOW_SPACE'; return [pscustomobject]$diagnostic }
        $fulls=@(Get-ChildItem -LiteralPath $backupRoot -Directory -Force | Where-Object { $_.Name -like 'full-*' } | Sort-Object Name -Descending)
        if (-not $diagnostic.active_full -and $fulls.Count -eq 0) {
            $diagnostic.status='INICIALIZACAO_PENDENTE'; $diagnostic.failure_code='FULL_REQUIRED'; return [pscustomobject]$diagnostic
        }
        $phase='MYSQL'
        $null=Assert-HubMySqlToolchain $mysqlRoot
        Assert-HubBackupCredential $credential
        $server=Get-HubBackupServerState $mysqlRoot $ProgramDataRoot $credential
        $phase='CHAIN'
        $fullDirectory=if ($diagnostic.active_full) { Join-Path $backupRoot $diagnostic.active_full } else { $fulls[0].FullName }
        $chain=Get-HubIncrementalChain $backupRoot $fullDirectory $server
        if ($diagnostic.active_full_sha256 -and $chain.full_hash -ne $diagnostic.active_full_sha256) { throw 'FULL ativo alterado.' }
        Assert-HubBinlogSource $server.datadir $chain.point $chain.anchor
        $window=Get-HubBinlogWindow $mysqlRoot $credential $chain.point
        # Nunca adotar outro FULL para esconder falha na cadeia ativa.
        if ($fulls.Count -gt 0 -and $fulls[0].Name -cgt (Split-Path $fullDirectory -Leaf)) {
            $candidate=Get-HubIncrementalChain $backupRoot $fulls[0].FullName $server
            Assert-HubBinlogSource $server.datadir $candidate.point $candidate.anchor
            $window=Get-HubBinlogWindow $mysqlRoot $credential $candidate.point
            $chain=$candidate; $fullDirectory=$fulls[0].FullName
        }
        Set-HubBackupChainDiagnostic $diagnostic $fullDirectory $chain $backupRoot
        # Persistir a identidade antes de qualquer captura: um retry nao troca de cadeia.
        Write-HubBackupControlState $backupRoot $diagnostic
        if ($chain.head_repair) { Write-HubIncrementalHead $backupRoot $chain; $chain.head_repair=$false }
        $phase='CAPTURE'
        if ($window.changed) {
            $result=Invoke-HubMySqlIncrementalBackup -FullBackupDirectory $fullDirectory -InstallRoot $InstallRoot -ProgramDataRoot $ProgramDataRoot -CaptureTimeoutSeconds $policy.timeout_seconds -BackupLock $lock -VerifiedChain $chain
            if ($result) {
                $manifest=Read-HubBackupJson (Join-Path $result 'manifest.json')
                $diagnostic.published_bytes+=[long]$manifest.archive.size_bytes
                # O helper altera a cadeia validada somente apos publicacao completa.
                Set-HubBackupChainDiagnostic $diagnostic $fullDirectory $chain $backupRoot
            }
        }
        $phase='CHAIN'
        $lastFull=[DateTimeOffset]::Parse($chain.full.completed_at_utc,[Globalization.CultureInfo]::InvariantCulture)
        if (Test-HubFullRenewalWindow $policy $lastFull $now) {
            # Revalidar a origem apos a captura; FULL novo nunca mascara uma lacuna.
            Assert-HubBinlogSource $server.datadir $chain.point $chain.anchor
            $null=Get-HubBinlogWindow $mysqlRoot $credential $chain.point
            $phase='RENEWAL'
            $newFull=Invoke-HubMySqlFullBackup -InstallRoot $InstallRoot -ProgramDataRoot $ProgramDataRoot -DumpTimeoutSeconds $policy.timeout_seconds -BackupLock $lock
            $phase='CHAIN'
            $newChain=Get-HubIncrementalChain $backupRoot $newFull $server
            Assert-HubBinlogSource $server.datadir $newChain.point $newChain.anchor
            $null=Get-HubBinlogWindow $mysqlRoot $credential $newChain.point
            Set-HubBackupChainDiagnostic $diagnostic $newFull $newChain $backupRoot
            $diagnostic.published_bytes+=[long]$newChain.full.archive.size_bytes
            $diagnostic.status='FULL_RENOVADO'
        } else { $diagnostic.status=if ($diagnostic.published_bytes -gt 0) { 'INCREMENTAL_VALIDO' } else { 'SEM_ALTERACOES' } }
        $diagnostic.last_success_utc=[DateTime]::UtcNow.ToString('o')
        return [pscustomobject]$diagnostic
    } catch {
        $blocked=($phase -in @('STATE','POLICY','CHAIN') -and $_.Exception.Data['HubBackupFailure'] -ne 'MYSQL')
        $diagnostic.status=if ($blocked) { 'INTERVENCAO_SUPORTE' } else { 'FALHA_REPETIR' }
        $diagnostic.support_required=$blocked
        $diagnostic.failure_code=if ($blocked) { 'CHAIN_OR_CONFIGURATION_INVALID' } else { 'MYSQL_OR_CAPTURE_FAILED' }
        # Nao persistir mensagens de excecao, consultas, caminhos nativos ou segredos.
        return [pscustomobject]$diagnostic
    } finally {
        $clock.Stop(); $diagnostic.elapsed_ms=$clock.ElapsedMilliseconds
        try { if ($canWrite) { Write-HubBackupControlState $backupRoot $diagnostic } }
        finally { $lock.Dispose() }
    }
}
