$ErrorActionPreference = "Stop"
$WindowsRoot = Split-Path $PSScriptRoot -Parent
. (Join-Path $WindowsRoot "mysql-backup.ps1")
# Importar apenas definicoes de funcoes; nunca executar o instalador.
$installPath = Join-Path $WindowsRoot "install-hub.ps1"
$ast = [System.Management.Automation.Language.Parser]::ParseFile($installPath, [ref]$null, [ref]$null)
foreach ($node in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $false)) {
    . ([scriptblock]::Create($node.Extent.Text))
}

Describe "Configuracao MySQL nova para backup" {
    It "usa instancia oficial isolada e durabilidade explicita" {
        $config = Get-HubMySqlNewConfig -InstallRoot 'C:\Program Files\Sysvar Hub' -MysqlData 'C:\ProgramData\SysvarHub\mysql\data'
        foreach ($line in @('port=3307','bind-address=127.0.0.1','default-storage-engine=INNODB','log_bin=C:/ProgramData/SysvarHub/mysql/data/sysvarhub-bin','binlog_format=ROW','server_id=1','sync_binlog=1','innodb_flush_log_at_trx_commit=1','binlog_expire_logs_seconds=2592000')) {
            ($config -contains $line) | Should Be $true
        }
        ($config -match '3306|MySQL80').Count | Should Be 0
    }
    It "permite ID e retencao controlados" {
        $config = Get-HubMySqlNewConfig -InstallRoot 'C:\Hub' -MysqlData 'C:\Data' -ServerId 17 -RetentionDays 7
        ($config -contains 'server_id=17') | Should Be $true
        ($config -contains 'binlog_expire_logs_seconds=604800') | Should Be $true
    }
    It "rejeita ID e retencao invalidos" {
        { Get-HubMySqlNewConfig -ServerId 0 } | Should Throw
        { Get-HubMySqlNewConfig -RetentionDays 0 } | Should Throw
        { Get-HubMySqlNewConfig -RetentionDays 366 } | Should Throw
    }
}

Describe "Deteccao de instalacoes existentes sem alteracoes" {
    BeforeEach {
        $script:caseRoot = Join-Path $TestDrive ([guid]::NewGuid().ToString('N'))
        $script:configRoot = Join-Path $caseRoot 'config'
        $script:mysqlData = Join-Path $caseRoot 'mysql\data'
        New-Item -ItemType Directory -Path $configRoot,$mysqlData -Force | Out-Null
    }
    It "aceita apenas diretorios vazios sem servico como instalacao nova" {
        Test-HubMySqlNewInstallation $configRoot $mysqlData $false | Should Be $true
    }
    It "nao considera o servico existente como instalacao nova" {
        Test-HubMySqlNewInstallation $configRoot $mysqlData $true | Should Be $false
    }
    foreach ($artifact in @('my.ini','sysvarhub.env','mysql-admin.cnf','mysql-backup.cnf')) {
        It "preserva e identifica o artefato $artifact" {
            $path = Join-Path $configRoot $artifact
            Set-Content -LiteralPath $path -Value 'sentinela de teste'
            $before = (Get-FileHash -LiteralPath $path).Hash
            Test-HubMySqlNewInstallation $configRoot $mysqlData $false | Should Be $false
            (Get-FileHash -LiteralPath $path).Hash | Should Be $before
        }
    }
    It "nao reinicializa dados preexistentes mesmo sem my.ini" {
        Set-Content -LiteralPath (Join-Path $mysqlData 'dados.ibd') -Value 'sentinela de teste'
        Test-HubMySqlNewInstallation $configRoot $mysqlData $false | Should Be $false
        (Get-Content -LiteralPath (Join-Path $mysqlData 'dados.ibd')) | Should Be 'sentinela de teste'
    }
}

Describe "Ferramentas obrigatorias do pacote" {
    BeforeEach {
        $script:toolRoot = Join-Path $TestDrive ([guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path (Join-Path $toolRoot 'bin') -Force | Out-Null
        foreach ($name in @('mysqld.exe','mysql.exe','mysqldump.exe','mysqlbinlog.exe')) {
            New-Item -ItemType File -Path (Join-Path $toolRoot "bin\$name") | Out-Null
        }
        Mock Get-HubMySqlBinaryVersion { [version]'8.0.46' }
    }
    It "aceita quatro ferramentas da mesma versao" {
        Assert-HubMySqlToolchain $toolRoot | Should Be (Resolve-Path $toolRoot).Path
        Assert-MockCalled Get-HubMySqlBinaryVersion -Scope It -Times 4 -Exactly
    }
    foreach ($tool in @('mysqld.exe','mysql.exe','mysqldump.exe','mysqlbinlog.exe')) {
        It "falha controladamente sem $tool" {
            Remove-Item -LiteralPath (Join-Path $toolRoot "bin\$tool")
            { Assert-HubMySqlToolchain $toolRoot } | Should Throw 'Ferramenta MySQL obrigatoria ausente'
        }
    }
    It "rejeita cliente de versao diferente do servidor" {
        Mock Get-HubMySqlBinaryVersion { [version]'8.0.45' } -ParameterFilter { $Path -like '*mysqlbinlog.exe' }
        { Assert-HubMySqlToolchain $toolRoot } | Should Throw 'incompativel'
    }
    It "rejeita versao sem suporte ao contrato source-data" {
        Mock Get-HubMySqlBinaryVersion { [version]'8.0.25' }
        { Assert-HubMySqlToolchain $toolRoot } | Should Throw 'Versao MySQL nao suportada'
    }
    It "nao assume compatibilidade com outra serie MySQL" {
        Mock Get-HubMySqlBinaryVersion { [version]'8.4.0' }
        { Assert-HubMySqlToolchain $toolRoot } | Should Throw 'Versao MySQL nao suportada'
    }
    It "rejeita arquivo que nao seja binario MySQL" {
        $helperAst = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $WindowsRoot 'mysql-backup.ps1'), [ref]$null, [ref]$null)
        $reader = $helperAst.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Get-HubMySqlBinaryVersion' }, $false)
        $body = [scriptblock]::Create($reader.Body.Extent.Text.TrimStart('{').TrimEnd('}'))
        { & $body -Path (Join-Path $toolRoot 'bin\mysql.exe') } | Should Throw 'nao reconhecido'
    }
}

Describe "Provisionamento exclusivo e seguro da conta de backup" {
    BeforeEach {
        $script:credential = Join-Path $TestDrive (([guid]::NewGuid().ToString('N')) + '.cnf')
        Mock Invoke-MySqlCommand { }
        Mock Invoke-MySqlCommand { } -ParameterFilter { $ReadOutput }
        Mock Invoke-MySqlCommand { "0" } -ParameterFilter { $ReadOutput -and $InputSql -eq "SELECT @@GLOBAL.partial_revokes;" }
        Mock New-Secret { 'credencial-ficticia-de-teste' }
        Mock Protect-SecretFile { (Get-Item -LiteralPath $Path).Length | Should Be 0 }
    }
    It "nao altera contas ou credenciais de instalacao existente" {
        Initialize-HubMySqlBackupAccount -NewInstallation $false -CredentialFile $credential -WarningAction SilentlyContinue | Should Be $false
        Assert-MockCalled Invoke-MySqlCommand -Scope It -Times 0 -Exactly
        Assert-MockCalled New-Secret -Scope It -Times 0 -Exactly
        Test-Path $credential | Should Be $false
    }
    It "bloqueia instancia com outros bancos antes de criar credenciais ou contas" {
        Mock Invoke-MySqlCommand { 'outro_banco' } -ParameterFilter { $ReadOutput }
        { Initialize-HubMySqlBackupAccount $true $credential } | Should Throw 'outros bancos'
        Assert-MockCalled New-Secret -Scope It -Times 0 -Exactly
        Assert-MockCalled Invoke-MySqlCommand -Scope It -Times 0 -Exactly -ParameterFilter { -not $ReadOutput }
        Test-Path $credential | Should Be $false
    }
    It "nao sobrescreve credencial preexistente" {
        Set-Content -LiteralPath $credential -Value 'sentinela'
        { Initialize-HubMySqlBackupAccount $true $credential } | Should Throw 'preexistente'
        (Get-Content -LiteralPath $credential) | Should Be 'sentinela'
        Assert-MockCalled Invoke-MySqlCommand -Scope It -Times 0 -Exactly
    }
    It "protege arquivo antes de gravar e concede somente privilegios de backup" {
        Initialize-HubMySqlBackupAccount $true $credential | Should Be $true
        Assert-MockCalled Protect-SecretFile -Scope It -Times 1 -Exactly
        $lines = Get-Content -LiteralPath $credential
        ($lines -contains 'user=sysvarhub_backup') | Should Be $true
        ($lines -contains 'port=3307') | Should Be $true
        ($lines -contains 'protocol=TCP') | Should Be $true
        ($lines -match 'user=root|3306').Count | Should Be 0
        Assert-MockCalled Invoke-MySqlCommand -Scope It -Times 1 -Exactly -ParameterFilter {
            -not $ReadOutput -and
            $InputSql.Contains('GRANT SELECT, SHOW VIEW, TRIGGER ON `sysvarhub\_db`.*') -and
            $InputSql.Contains('GRANT RELOAD, REPLICATION CLIENT, REPLICATION SLAVE ON *.*') -and
            $InputSql -notmatch 'ALL PRIVILEGES|GRANT OPTION|IF NOT EXISTS|DROP |INSERT |UPDATE |DELETE ' -and
            $Arguments -contains '--port=3307' -and $Arguments -notcontains '--port=3306'
        }
    }
    It "mantem nome literal do banco quando partial_revokes esta ativo" {
        Mock Invoke-MySqlCommand { "1" } -ParameterFilter { $ReadOutput -and $InputSql -eq "SELECT @@GLOBAL.partial_revokes;" }
        Initialize-HubMySqlBackupAccount $true $credential | Should Be $true
        Assert-MockCalled Invoke-MySqlCommand -Scope It -Times 1 -Exactly -ParameterFilter {
            -not $ReadOutput -and $InputSql.Contains('GRANT SELECT, SHOW VIEW, TRIGGER ON `sysvarhub_db`.*')
        }
    }
    It "bloqueia modo de privilegios desconhecido antes de gerar credencial" {
        Mock Invoke-MySqlCommand { "inesperado" } -ParameterFilter { $ReadOutput -and $InputSql -eq "SELECT @@GLOBAL.partial_revokes;" }
        { Initialize-HubMySqlBackupAccount $true $credential } | Should Throw 'Configuracao de privilegios MySQL desconhecida'
        Assert-MockCalled New-Secret -Scope It -Times 0 -Exactly
        Test-Path $credential | Should Be $false
    }
    It "interrompe sem gravar segredo nem criar conta se a ACL falhar" {
        Mock Protect-SecretFile { throw 'ACL indisponivel' }
        { Initialize-HubMySqlBackupAccount $true $credential } | Should Throw 'ACL indisponivel'
        (Get-Item -LiteralPath $credential).Length | Should Be 0
        Assert-MockCalled Invoke-MySqlCommand -Scope It -Times 0 -Exactly -ParameterFilter { -not $ReadOutput }
    }
    It "propaga falha de provisionamento sem trocar conta ou apagar credencial" {
        Mock Invoke-MySqlCommand { throw 'falha controlada' } -ParameterFilter { -not $ReadOutput }
        { Initialize-HubMySqlBackupAccount $true $credential } | Should Throw 'falha controlada'
        Test-Path $credential | Should Be $true
        Assert-MockCalled Invoke-MySqlCommand -Scope It -Times 1 -Exactly -ParameterFilter { -not $ReadOutput }
    }
}

Describe "Integracao dos scripts sem executar instalador" {
    It "mantem os scripts PowerShell validos inclusive para Windows PowerShell" {
        foreach ($name in @('mysql-backup.ps1','install-hub.ps1','build-installer.ps1')) {
            $parseErrors = $null
            $null = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $WindowsRoot $name), [ref]$null, [ref]$parseErrors)
            @($parseErrors).Count | Should Be 0
        }
    }
    It "valida pacote antes da limpeza e repete validacao no staging" {
        $text = Get-Content -LiteralPath (Join-Path $WindowsRoot 'build-installer.ps1') -Raw
        ($text.IndexOf('$ResolvedMySql = Assert-MySqlRoot') -lt $text.IndexOf('Remove-Item -LiteralPath $path')) | Should Be $true
        $text.Contains('Assert-HubMySqlToolchain (Join-Path $StagingRoot "mysql")') | Should Be $true
    }
    It "escreve my.ini apenas no ramo novo e sinaliza instalacao incompleta" {
        $text = Get-Content -LiteralPath $installPath -Raw
        $text | Should Match 'if \(\$IsNewMySqlInstallation\) \{\s+Get-HubMySqlNewConfig'
        ($text.IndexOf('Instalacao MySQL existente ou incompleta') -lt $text.IndexOf('New-Item -ItemType Directory')) | Should Be $true
        $text.Contains('if ($IsNewMySqlInstallation -and (Test-MySqlTcpReady 1))') | Should Be $true
        $text | Should Match 'Initialize-HubMySqlBackupAccount -NewInstallation \$IsNewMySqlInstallation'
    }
}

Describe "Execucao MySQL sem expor saidas ou erros" {
    BeforeEach {
        $script:mysqlExitCode = 0
        $script:Mysql = 'Invoke-TestMysqlNative'
        function Invoke-TestMysqlNative {
            'resultado de metadados ficticio'
            $global:LASTEXITCODE = $script:mysqlExitCode
        }
    }
    It "descarta saida por padrao" {
        @(Invoke-MySqlCommand -Arguments @() -InputSql 'SELECT 1;').Count | Should Be 0
    }
    It "retorna metadados somente quando solicitado" {
        Invoke-MySqlCommand -Arguments @() -InputSql 'SELECT 1;' -ReadOutput | Should Be 'resultado de metadados ficticio'
    }
    It "retorna erro funcional sem imprimir a saida capturada" {
        $script:mysqlExitCode = 1
        { Invoke-MySqlCommand -Arguments @() -InputSql 'SELECT 1;' -ReadOutput -FailureMessage 'erro funcional de teste' } | Should Throw 'erro funcional de teste'
        Invoke-MySqlCommand -Arguments @() -InputSql 'SELECT 1;' -Probe | Should Be $false
    }
}
