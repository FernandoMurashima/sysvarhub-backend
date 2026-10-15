$ErrorActionPreference='Stop'
. (Join-Path (Split-Path $PSScriptRoot -Parent) 'mysql-backup-task.ps1')

Describe 'Plano de agendamento Windows isolado' {
    BeforeEach {
        $script:installRoot='C:\Program Files\Sysvar Hub'; $script:dataRoot=Join-Path $TestDrive 'state'
        $script:exists=$false; $script:wrong=$false
        Mock Assert-HubBackupOperator { }
        Mock Assert-HubBackupService { }
        Mock Assert-HubBackupTaskFiles { }
        Mock Get-ScheduledTask {
            if ($script:exists) {
                $xml=[xml](New-HubBackupTaskXml $script:installRoot $script:dataRoot)
                [pscustomobject]@{TaskName='SysvarHubMySQLBackup';Description=if($script:wrong){'Outra tarefa'}else{'Sysvar Hub MySQL backup control v1'};State='Disabled';Actions=@([pscustomobject]@{Execute=[string]$xml.Task.Actions.Exec.Command;Arguments=[string]$xml.Task.Actions.Exec.Arguments});Principal=[pscustomobject]@{UserId='S-1-5-18'}}
            }
        }
        Mock Register-ScheduledTask { }
        Mock Disable-ScheduledTask { }
        Mock Enable-ScheduledTask { throw 'Ativacao real proibida' }
        Mock Start-ScheduledTask { throw 'Execucao real proibida' }
        Mock Get-ScheduledTaskInfo { [pscustomobject]@{LastTaskResult=0} }
    }
    It 'prepara repeticao indefinida a cada 15 minutos desativada por padrao' {
        $xml=[xml](Install-HubBackupTask $installRoot $dataRoot)
        $xml.Task.Triggers.TimeTrigger.Repetition.Interval | Should Be 'PT15M'
        $xml.Task.Triggers.TimeTrigger.Repetition.Duration | Should BeNullOrEmpty
        $xml.Task.Settings.Enabled | Should Be 'false'
        $xml.Task.Settings.MultipleInstancesPolicy | Should Be 'IgnoreNew'
        Assert-MockCalled Register-ScheduledTask -Times 0 -Exactly -Scope It
        Assert-MockCalled Get-ScheduledTask -Times 0 -Exactly -Scope It
    }
    It 'usa SYSTEM sem login, interface oculta, prioridade baixa e sem rede obrigatoria' {
        $xml=[xml](New-HubBackupTaskXml $installRoot $dataRoot)
        $xml.Task.Principals.Principal.UserId | Should Be 'S-1-5-18'
        $xml.Task.Principals.Principal.LogonType | Should Be 'ServiceAccount'
        $xml.Task.Settings.Hidden | Should Be 'true'
        $xml.Task.Settings.Priority | Should Be 7
        $xml.Task.Settings.RunOnlyIfNetworkAvailable | Should Be 'false'
        $xml.Task.Settings.AllowHardTerminate | Should Be 'false'
        $xml.Task.Actions.Exec.Arguments | Should Match '-NoProfile -NonInteractive -WindowStyle Hidden'
        $xml.Task.Actions.Exec.Arguments | Should Match 'backup-run.ps1'
        $xml.Task.Actions.Exec.Arguments | Should Not Match 'password|3306|--user=root|FullBackupDirectory'
    }
    It 'registro futuro exige opt-in e permanece desativado sem iniciar nada' {
        $null=Install-HubBackupTask $installRoot $dataRoot -Register
        Assert-MockCalled Register-ScheduledTask -Times 1 -Exactly -Scope It -ParameterFilter { ([xml]$Xml).Task.Settings.Enabled -eq 'false' -and $TaskName -eq 'SysvarHubMySQLBackup' }
        Assert-MockCalled Enable-ScheduledTask -Times 0 -Exactly -Scope It
        Assert-MockCalled Start-ScheduledTask -Times 0 -Exactly -Scope It
    }
    It 'nao substitui agendamento existente' {
        $script:exists=$true
        { Install-HubBackupTask $installRoot $dataRoot -Register } | Should Throw 'existente preservado'
        Assert-MockCalled Register-ScheduledTask -Times 0 -Exactly -Scope It
    }
    It 'consulta tarefa ausente sem registrar ou modificar' {
        (Get-HubBackupTaskStatus $installRoot $dataRoot).task_exists | Should Be $false
        Assert-MockCalled Register-ScheduledTask -Times 0 -Exactly -Scope It
        Assert-MockCalled Disable-ScheduledTask -Times 0 -Exactly -Scope It
    }
    It 'consulta tarefa e diagnostico existentes somente para leitura' {
        $script:exists=$true
        $root=Join-Path $dataRoot 'backup'; $null=New-Item -ItemType Directory -Path $root -Force
        $path=Join-Path $root 'backup-control.json'
        Write-HubBackupJson $path @{version=1;status='SEM_ALTERACOES'}
        $hash=(Get-FileHash $path).Hash
        $result=Get-HubBackupTaskStatus $installRoot $dataRoot
        $result.task_state | Should Be 'Disabled'
        $result.diagnostic.status | Should Be 'SEM_ALTERACOES'
        (Get-FileHash $path).Hash | Should Be $hash
    }
    It 'desativa somente tarefa com identidade correta sem interromper captura' {
        $script:exists=$true
        $null=Disable-HubBackupTask $installRoot $dataRoot
        Assert-MockCalled Disable-ScheduledTask -Times 1 -Exactly -Scope It
        Assert-MockCalled Start-ScheduledTask -Times 0 -Exactly -Scope It
    }
    It 'preserva tarefa homonima divergente' {
        $script:exists=$true; $script:wrong=$true
        { Disable-HubBackupTask $installRoot $dataRoot } | Should Throw 'divergente'
        Assert-MockCalled Disable-ScheduledTask -Times 0 -Exactly -Scope It
    }
    It 'desativacao de tarefa ausente nao modifica agendamentos' {
        $null=Disable-HubBackupTask $installRoot $dataRoot
        Assert-MockCalled Disable-ScheduledTask -Times 0 -Exactly -Scope It
    }
    It 'escapa XML e rejeita caminhos que injetariam argumentos' {
        $xml=[xml](New-HubBackupTaskXml 'C:\Program Files\Hub & Loja' $dataRoot)
        $xml.Task.Actions.Exec.Arguments | Should Match 'Hub & Loja'
        { New-HubBackupTaskXml 'C:\Hub" -Command perigo' $dataRoot } | Should Throw
    }
    It 'bloqueia registro em instancia DEV ou pacote inseguro' {
        Mock Assert-HubBackupService { throw 'MySQL80 DEV proibido' }
        { Install-HubBackupTask $installRoot $dataRoot -Register } | Should Throw 'DEV'
        Assert-MockCalled Register-ScheduledTask -Times 0 -Exactly -Scope It
    }
}

Describe 'Protecao dos scripts executados como SYSTEM' {
    BeforeEach {
        $script:app=Join-Path $TestDrive ([guid]::NewGuid().ToString('N'))
        $script:scripts=Join-Path $app 'scripts'; $null=New-Item -ItemType Directory -Path $scripts
        foreach ($name in @('backup-run.ps1','mysql-backup-control.ps1','mysql-backup.ps1')) { Set-Content (Join-Path $scripts $name) '# fixture sem efeito' }
        $script:untrusted=$false; $script:untrustedOwner=$false
        Mock Get-Acl {
            $sid=if ($script:untrusted) { 'S-1-5-32-545' } else { 'S-1-5-32-544' }
            $acl=New-Object Security.AccessControl.DirectorySecurity
            $ownerSid=if ($script:untrustedOwner) { 'S-1-5-32-545' } else { 'S-1-5-32-544' }
            $acl.SetOwner((New-Object Security.Principal.SecurityIdentifier($ownerSid)))
            $rule=New-Object Security.AccessControl.FileSystemAccessRule((New-Object Security.Principal.SecurityIdentifier($sid)),[Security.AccessControl.FileSystemRights]::Modify,[Security.AccessControl.AccessControlType]::Allow)
            $acl.AddAccessRule($rule)
            return $acl
        }
    }
    It 'aceita pacote completo protegido contra escrita comum' {
        { Assert-HubBackupTaskFiles $app } | Should Not Throw
    }
    It 'rejeita escrita por Usuarios inclusive em ancestrais' {
        $script:untrusted=$true
        { Assert-HubBackupTaskFiles $app } | Should Throw 'escrita nao administrativa'
    }
    It 'rejeita proprietario nao administrativo' {
        $script:untrustedOwner=$true
        { Assert-HubBackupTaskFiles $app } | Should Throw 'proprietario administrativo'
    }
    It 'rejeita pacote incompleto sem registrar tarefa' {
        $empty=Join-Path $TestDrive 'empty'; $null=New-Item -ItemType Directory -Path (Join-Path $empty 'scripts')
        { Assert-HubBackupTaskFiles $empty } | Should Throw 'incompleto'
    }
}

Describe 'Sintaxe Windows PowerShell 5.1 do controle e agendamento' {
    It 'analisa scripts sem executar tarefas, instaladores ou MySQL' {
        foreach ($name in @('mysql-backup.ps1','mysql-backup-control.ps1','mysql-backup-task.ps1','backup-run.ps1','backup-task-install.ps1','backup-task-status.ps1','backup-task-disable.ps1')) {
            $errors=$null
            $null=[Management.Automation.Language.Parser]::ParseFile((Join-Path (Split-Path $PSScriptRoot -Parent) $name),[ref]$null,[ref]$errors)
            @($errors).Count | Should Be 0
        }
    }
}
