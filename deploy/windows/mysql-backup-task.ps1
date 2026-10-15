# Importacao sem registrar, habilitar ou consultar tarefas.
. (Join-Path $PSScriptRoot 'mysql-backup-control.ps1')

function New-HubBackupTaskXml {
    param([string]$InstallRoot,[string]$ProgramDataRoot)
    $InstallRoot=Assert-HubBackupLocalPath $InstallRoot; $ProgramDataRoot=Assert-HubBackupLocalPath $ProgramDataRoot
    $runner=Join-Path $InstallRoot 'scripts\backup-run.ps1'
    $exe=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $arguments='-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $runner + '" -InstallRoot "' + $InstallRoot + '" -ProgramDataRoot "' + $ProgramDataRoot + '"'
    $escapedArgs=[Security.SecurityElement]::Escape($arguments); $escapedExe=[Security.SecurityElement]::Escape($exe)
    $boundary=[DateTime]::Now.AddMinutes(1).ToString('yyyy-MM-ddTHH:mm:ss')
    # Sem Duration: repeticao indefinida. Desativada ate habilitacao explicita do suporte.
    return @"
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Sysvar Hub MySQL backup control v1</Description></RegistrationInfo>
  <Triggers><TimeTrigger><Repetition><Interval>PT15M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition><StartBoundary>$boundary</StartBoundary><Enabled>true</Enabled></TimeTrigger></Triggers>
  <Principals><Principal id="BackupSystem"><UserId>S-1-5-18</UserId><LogonType>ServiceAccount</LogonType><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><AllowHardTerminate>false</AllowHardTerminate><StartWhenAvailable>true</StartWhenAvailable><RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable><Enabled>false</Enabled><Hidden>true</Hidden><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><Priority>7</Priority></Settings>
  <Actions Context="BackupSystem"><Exec><Command>$escapedExe</Command><Arguments>$escapedArgs</Arguments></Exec></Actions>
</Task>
"@
}

function Assert-HubBackupTaskFiles {
    param([string]$InstallRoot)
    $scripts=Assert-HubBackupLocalPath (Join-Path $InstallRoot 'scripts')
    $paths=@($scripts)
    foreach ($name in @('backup-run.ps1','mysql-backup-control.ps1','mysql-backup.ps1')) {
        $path=Assert-HubBackupLocalPath (Join-Path $scripts $name)
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw 'Pacote de controle de backup incompleto.' }
        $paths+=$path
    }
    # SYSTEM so deve executar codigo protegido. Inclui ancestrais contra substituicao.
    $parent=Split-Path $scripts -Parent
    while ($parent -and $parent -ne [IO.Path]::GetPathRoot($parent)) { $paths+=$parent; $parent=Split-Path $parent -Parent }
    $trusted=@('S-1-5-18','S-1-5-32-544','S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464')
    $write=[Security.AccessControl.FileSystemRights]'Write, Delete, DeleteSubdirectoriesAndFiles, ChangePermissions, TakeOwnership'
    foreach ($path in $paths) {
        $acl=Get-Acl -LiteralPath $path
        if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -notin $trusted) { throw 'Codigo do backup exige proprietario administrativo.' }
        foreach ($rule in $acl.Access) {
            if ($rule.AccessControlType -eq 'Allow' -and ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) -eq 0 -and ($rule.FileSystemRights -band $write) -ne 0 -and $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -notin $trusted) { throw 'Codigo do backup permite escrita nao administrativa; adequacao assistida necessaria.' }
        }
    }
}

function Install-HubBackupTask {
    param([string]$InstallRoot='C:\Program Files\Sysvar Hub',[string]$ProgramDataRoot='C:\ProgramData\SysvarHub',[switch]$Register)
    $xml=New-HubBackupTaskXml $InstallRoot $ProgramDataRoot
    if (-not $Register) { return $xml }
    Assert-HubBackupOperator
    $InstallRoot=Assert-HubBackupLocalPath $InstallRoot; $ProgramDataRoot=Assert-HubBackupLocalPath $ProgramDataRoot
    Assert-HubBackupService (Join-Path $InstallRoot 'mysql') $ProgramDataRoot
    Assert-HubBackupTaskFiles $InstallRoot
    $null=Get-HubBackupPolicy $ProgramDataRoot
    # Nao sobrescrever tarefa existente, mesmo pertencendo a uma versao antiga do Hub.
    $existing=@(Get-ScheduledTask -TaskPath '\' -ErrorAction Stop | Where-Object { $_.TaskName -eq 'SysvarHubMySQLBackup' })
    if ($existing.Count -gt 0) { throw 'Agendamento existente preservado; revisar com o suporte.' }
    $null=Register-ScheduledTask -TaskName 'SysvarHubMySQLBackup' -TaskPath '\' -Xml $xml -ErrorAction Stop
    return 'Agendamento registrado DESATIVADO. Ativacao futura exige verificacao e acao explicita do suporte.'
}

function Get-HubBackupTaskStatus {
    param([string]$InstallRoot='C:\Program Files\Sysvar Hub',[string]$ProgramDataRoot='C:\ProgramData\SysvarHub')
    $InstallRoot=Assert-HubBackupLocalPath $InstallRoot; $ProgramDataRoot=Assert-HubBackupLocalPath $ProgramDataRoot
    $task=@(Get-ScheduledTask -TaskPath '\' -ErrorAction Stop | Where-Object { $_.TaskName -eq 'SysvarHubMySQLBackup' })
    $statePath=Join-Path $ProgramDataRoot 'backup\backup-control.json'
    $diagnostic=$null
    if (Test-Path -LiteralPath $statePath) { $diagnostic=Read-HubBackupJson $statePath }
    $info=$null
    if ($task.Count -eq 1) { $info=Get-ScheduledTaskInfo -TaskName 'SysvarHubMySQLBackup' -TaskPath '\' -ErrorAction Stop }
    return [pscustomobject]@{task_exists=($task.Count -eq 1);task_state=if($task.Count -eq 1){[string]$task[0].State}else{'NAO_REGISTRADA'};task_info=$info;diagnostic=$diagnostic}
}

function Disable-HubBackupTask {
    param([string]$InstallRoot='C:\Program Files\Sysvar Hub',[string]$ProgramDataRoot='C:\ProgramData\SysvarHub')
    Assert-HubBackupOperator
    $expected=[xml](New-HubBackupTaskXml $InstallRoot $ProgramDataRoot)
    $tasks=@(Get-ScheduledTask -TaskPath '\' -ErrorAction Stop | Where-Object { $_.TaskName -eq 'SysvarHubMySQLBackup' })
    if ($tasks.Count -eq 0) { return 'Agendamento nao registrado.' }
    $task=$tasks[0]; $actions=@($task.Actions)
    if ($task.Description -ne 'Sysvar Hub MySQL backup control v1' -or $actions.Count -ne 1 -or $actions[0].Execute -ine $expected.Task.Actions.Exec.Command -or $actions[0].Arguments -cne $expected.Task.Actions.Exec.Arguments -or $task.Principal.UserId -notin @('S-1-5-18','SYSTEM')) { throw 'Identidade da tarefa divergente; agendamento preservado.' }
    $null=Disable-ScheduledTask -TaskName 'SysvarHubMySQLBackup' -TaskPath '\' -ErrorAction Stop
    return 'Agendamento desativado; eventual captura em andamento nao foi interrompida.'
}
