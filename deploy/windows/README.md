# Sysvar Hub no Windows

O runtime de producao do Sysvar Hub roda em um unico processo Windows com Django, Waitress e o frontend Angular compilado servido pelo proprio Hub.

## Arquitetura

- `http://<ip-da-loja>:8000/` atende o frontend Angular.
- `http://<ip-da-loja>:8000/api/...` atende a API local do Hub.
- `GET /api/health/` retorna o estado basico do servico para instalador e monitoramento.
- O banco local esperado e MySQL.

## Diretorios previstos

- `C:\Program Files\Sysvar Hub\app`: backend empacotado.
- `C:\Program Files\Sysvar Hub\frontend`: Angular compilado.
- `C:\ProgramData\SysvarHub\config`: arquivo `.env` da instalacao.
- `C:\ProgramData\SysvarHub\logs`: `hub.log` com rotacao.
- `C:\ProgramData\SysvarHub\backup`: backups futuros.
- `C:\ProgramData\SysvarHub\data`: dados auxiliares futuros.

Os caminhos podem ser alterados por parametros ou variaveis de ambiente.

## Configuracao

Use `sysvarhub.env.example` como modelo e preencha segredo, banco, hosts permitidos e origens confiaveis. Em producao, `DJANGO_DEBUG=False` exige `DJANGO_SECRET_KEY` real.

## Scripts

- `install-runtime.ps1`: cria diretorios base e copia o template de configuracao.
- `start-hub.ps1`: inicia `runtime/run_hub.py` com Waitress.
- `stop-hub.ps1`: encerra o processo ouvindo na porta informada.
- `check-hub.ps1`: consulta `/api/health/` e retorna exit code `0` quando saudavel.
- `build-frontend.ps1`: compila o Angular e copia o resultado para `frontend_dist`.
- `prepare-package.ps1`: monta `deploy\windows\staging\` com app, frontend, template e scripts.
- `build-installer.ps1`: gera o runtime PyInstaller, monta staging com frontend e MySQL e chama o Inno Setup.
- `install-hub.ps1`: cria configuracao segura, registra os servicos `SysvarHubMySQL` e `SysvarHub`, aplica migrations e libera firewall apenas para TCP 8000.
- `configurar-hub.ps1`: ferramenta de manutencao/suporte tecnico para executar `ativar_hub` fora da interface web.
- `diagnostico.ps1`: mostra estado dos servicos e portas sem exibir segredos.
- `uninstall-hub.ps1`: remove servicos e binarios preservando `C:\ProgramData\SysvarHub`.
- `purge-data.ps1`: remove dados preservados somente quando executado explicitamente.

## MySQL

O instalador usa MySQL Community Server 8.x x64 em ZIP extraido, informado no build por `-MySqlRoot`. Ele sera instalado em `C:\Program Files\Sysvar Hub\mysql` e configurado para escutar somente `127.0.0.1:3307`.

## Build e instalador

O build do frontend nao e versionado. O instalador final devera embutir runtime Python, dependencias, app, frontend compilado, scripts e template de configuracao, sem exigir Python, Node.js, Angular CLI, Git ou VS Code na maquina da loja.

## Fluxo normal de ativacao

1. Instalar o Hub.
2. Abrir o atalho `Ativar/Configurar Sysvar Hub`.
3. Informar a URL da Central e o codigo temporario gerado no painel da Central.
4. Concluir a ativacao pela interface local.

## Manutencao

`configurar-hub.ps1` e o management command `ativar_hub` continuam disponiveis apenas para suporte tecnico, desenvolvimento e recuperacao operacional. O fluxo normal do usuario nao exige PowerShell.

## Fluxo de primeira sincronizacao

1. Instalar o Hub.
2. Ativar o Hub pela interface local com a URL da Central e o codigo de ativacao.
3. O servico fica online enviando heartbeat para a Central.
4. A Central solicita a primeira sincronizacao pelo painel.
5. O Hub recebe o comando no heartbeat, executa a carga completa e informa o resultado.

## Preparacao do MySQL para backup

Instalacoes novas usam uma instancia dedicada `SysvarHubMySQL`, TCP `127.0.0.1:3307`, sem conectar ao MySQL DEV (`MySQL80`, porta 3306). O instalador escreve `my.ini` somente quando nao existem configuracao, credenciais, dados ou servico anteriores. Estados incompletos exigem adequacao assistida, sem recriar configuracao ou reinicializar dados existentes.

A configuracao nova explicita InnoDB, `log_bin`, `binlog_format=ROW`, `sync_binlog=1`, `innodb_flush_log_at_trx_commit=1`, `server_id` e `binlog_expire_logs_seconds`. Parametros do instalador: `-MySqlServerId` (padrao 1) e `-BinlogRetentionDays` (padrao 30; intervalo 1 a 365 dias). A retencao e por idade, nao um limite de espaco; precisa ser dimensionada antes da implantacao. Esses parametros nao alteram `my.ini` existente e nao provocam reinicio para aplicar configuracoes.

O build e o preflight do instalador exigem `mysqld.exe`, `mysql.exe`, `mysqldump.exe` e `mysqlbinlog.exe`, da mesma versao oficial MySQL **8.0.x**, a partir de **8.0.26** (contrato `--source-data`). A validacao usa metadados dos executaveis, sem iniciar servidor ou executar backups. Outras series, inclusive 8.4, exigem homologacao especifica. O pacote e validado antes da limpeza do build e novamente no staging.

### Conta exclusiva de backup

Somente na instancia recem-criada e apos confirmar que nao existem outros bancos de usuario, o instalador provisiona `sysvarhub_backup@127.0.0.1`. Nao reutiliza conta anterior nem altera suas credenciais. Os privilegios preparados sao:

- `SELECT`, `SHOW VIEW`, `TRIGGER` apenas no banco literal `sysvarhub_db` (escape do `_` conforme `partial_revokes`, sem ampliar acesso por curinga);
- `RELOAD`, `REPLICATION CLIENT`, `REPLICATION SLAVE` globais, necessarios para coordenadas/bloqueio breve do dump e leitura dos binlogs.

Nao sao concedidos `ALL PRIVILEGES`, `GRANT OPTION`, `FILE`, `PROCESS` ou permissoes gerais de escrita. O contrato previsto para o dump e `--single-transaction --quick --source-data=2 --no-tablespaces --set-gtid-purged=OFF --databases sysvarhub_db`. O provisionamento nao executa o dump; o acionamento manual esta descrito abaixo. Rotinas armazenadas/eventos SQL nao fazem parte do esquema atual; se introduzidos, revisar cobertura e privilegios antes de considerar o backup completo.

A credencial aleatoria fica em `config\mysql-backup.cnf`, com destino TCP fixo 3307. O arquivo e criado sem sobrescrita, recebe ACL restrita a SYSTEM/Administradores antes de gravar o segredo e nao e impresso. A conta administrativa e usada somente pelo provisionamento do instalador, nunca como identidade de um processo de backup. Falhas parciais exigem assistencia; nao ha tentativa automatica de apagar ou recriar contas.

**Escopo dos binlogs:** `REPLICATION SLAVE` nao pode ser limitado por banco. Uma filtragem do dump nao protege um binlog bruto de instancia compartilhada. Portanto, nao provisionar essa conta nem capturar logs no MySQL DEV ou em instancia com outros bancos. A futura rotina devera confirmar identidade, porta, exclusividade, versao e continuidade dos logs antes de iniciar/retomar captura; nao basta encontrar um arquivo de credenciais. Nao ha worker, captura incremental, transmissao ou restauracao automatica.

### Instalacoes existentes

Configuracao e conta de backup existentes permanecem intactas; o instalador emite aviso de adequacao assistida para backup. A atualizacao normal preexistente do instalador permanece separada dessa preparacao. Antes de habilitar backups em uma instalacao antiga, o suporte deve verificar configuracao efetiva, exclusividade do servidor, versao em execucao versus ferramentas, permissao minima, espaco e retencao. Eventual alteracao de configuracao/reinicio exige janela e autorizacao proprias; nao e realizada automaticamente pela preparacao de backup.

Testes isolados (nao executam o instalador nem conectam ao MySQL):

```powershell
powershell.exe -NoProfile -Command "Invoke-Pester -Script deploy/windows/tests/mysql-backup.Tests.ps1 -EnableExit"
```

Fontes tecnicas: [mysqldump](https://dev.mysql.com/doc/refman/8.0/en/mysqldump.html) e [mysqlbinlog](https://dev.mysql.com/doc/refman/8.0/en/mysqlbinlog.html).


## Backup completo manual do banco do Hub

Em Windows PowerShell 5.1 **como Administrador**, na instalacao oficial preparada:

```powershell
& 'C:\Program Files\Sysvar Hub\scripts\backup-full.ps1'
```

Para caminhos oficiais personalizados, informar `-InstallRoot` e `-ProgramDataRoot` correspondentes ao servico instalado. O comando nao aceita host, porta, usuario, banco ou argumentos livres de mysqldump. O timeout total do dump pode ser ajustado com `-DumpTimeoutSeconds` (padrao 1200 segundos). Nao executar no MySQL DEV: sem o servico dedicado `SysvarHubMySQL` e credencial protegida de `sysvarhub_backup@127.0.0.1:3307`, a operacao e bloqueada.

Antes do dump, o script valida ferramentas, servico/configuracao, versao efetiva versus binarios, basedir/datadir, conta/grants, identidade unica do Hub/empresa/loja, schema exclusivo, tabelas InnoDB, binlogs ROW sem filtros e espaco em disco local fixo. A consulta de schemas usa os privilegios globais estaticos da conta preparada; grants nao homologados e restricoes parciais sao rejeitados. Novas consultas apos a compactacao verificam identidade e disponibilidade da coordenada inicial.

O dump usa `--single-transaction --quick --source-data=2 --no-tablespaces --set-gtid-purged=OFF --databases sysvarhub_db`, mais `--hex-blob`. A saida vai diretamente para `--result-file`, evitando conversao UTF-16 pelo redirecionamento do PowerShell. Nenhuma senha e passada na linha de comando. O processo filho ignora `MYSQL_PWD` herdado e usa um login-path alternativo inexistente para nao ler `.mylogin.cnf` pessoal (sem alterar o ambiente pai). Stderr e falhas nativas nao sao reproduzidos no console. Ver [precedencia dos login paths](https://dev.mysql.com/doc/refman/8.0/en/mysql-config-editor.html).

Cada execucao usa uma pasta `.incomplete-<uuid>` dentro de `C:\ProgramData\SysvarHub\backup`, com ACL restrita a SYSTEM/Administradores. O SQL e compactado em streaming para `sysvarhub_db.sql.gz`; a leitura/descompactacao e comparada ao SHA-256 do SQL original. O manifesto registra versao do formato, UUID do backup, datas UTC, identidade da instalacao/loja, versao/banco/UUID/ID do servidor, arquivo/posicao de binlog extraidos do cabecalho do snapshot, tamanho, checksum SHA-256 do gzip e status `VALIDO`.

O SQL temporario e eliminado antes da publicacao. Somente apos todos os passos concluirem, a pasta inteira e renomeada atomicamente no mesmo volume para `full-<dataUTC>-<uuid>`, contendo gzip e `manifest.json`. Nao ha sobrescrita de pastas publicadas, ponteiro mutavel de ultimo backup ou retencao automatica. A trava `.full-backup.lock` evita dumps completos concorrentes. Em falha, somente a area temporaria criada por aquela execucao e removida; se a limpeza falhar, permanece protegida e incompleta, sem publicacao. Backups anteriores nao sao removidos.

### Limites desta etapa

- E backup local do banco do Hub, incluindo filas/eventos e XMLs armazenados no banco. Arquivos externos, configuracoes, certificados e restauracao completa da instalacao permanecem fora desta etapa.
- Gzip compacta; a protecao local e por ACL, sem criptografia do arquivo nesta etapa.
- O esquema suportado e o do aplicativo, sem rotinas armazenadas/eventos SQL personalizados. Objetos nao homologados exigem revisao de cobertura e privilegios; a conta minima nao substitui uma auditoria administrativa de objetos criados fora do aplicativo.
- O snapshot InnoDB permite operacao concorrente sem parar servicos. `--source-data=2` requer um bloqueio global breve para obter coordenadas; dumps devem ocorrer fora de atualizacoes de schema e preferencialmente em menor movimento. Nao se promete ausencia absoluta de espera de escrita ou consumo de disco/CPU.
- A reserva de espaco e estimativa: maior entre 256 MiB e quatro vezes dados+indices mais 64 MiB. Crescimento concorrente pode causar falha; nesse caso nada e publicado.
- Coordenadas referenciam o inicio do snapshot; nao ha copia/retencao de binlogs nesta etapa. O backup completo nao garante sozinho continuidade incremental apos a expiracao dos logs do servidor.
- A geracao e a recuperacao reais precisam de homologacao isolada autorizada; os testes desta etapa usam mocks, subprocessos ficticios e arquivos temporarios.

Testes direcionados da preparacao e do completo:

```powershell
powershell.exe -NoProfile -Command "Invoke-Pester -Script deploy/windows/tests/mysql-backup.Tests.ps1,deploy/windows/tests/mysql-full-backup.Tests.ps1 -EnableExit"
```
