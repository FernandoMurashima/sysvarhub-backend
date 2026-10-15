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

O SQL temporario e eliminado antes da publicacao. Somente apos todos os passos concluirem, a pasta inteira e renomeada atomicamente no mesmo volume para `full-<dataUTC>-<uuid>`, contendo gzip e `manifest.json`. Nao ha sobrescrita de pastas publicadas, ponteiro mutavel de ultimo backup ou retencao automatica. A trava compartilhada `.full-backup.lock` impede concorrencia entre FULL e capturas incrementais. Em falha, somente a area temporaria criada por aquela execucao e removida; se a limpeza falhar, permanece protegida e incompleta, sem publicacao. Backups anteriores nao sao removidos.

### Limites desta etapa

- E backup local do banco do Hub, incluindo filas/eventos e XMLs armazenados no banco. Arquivos externos, configuracoes, certificados e restauracao completa da instalacao permanecem fora desta etapa.
- Gzip compacta; a protecao local e por ACL, sem criptografia do arquivo nesta etapa.
- O esquema suportado e o do aplicativo, sem rotinas armazenadas/eventos SQL personalizados. Objetos nao homologados exigem revisao de cobertura e privilegios; a conta minima nao substitui uma auditoria administrativa de objetos criados fora do aplicativo.
- O snapshot InnoDB permite operacao concorrente sem parar servicos. `--source-data=2` requer um bloqueio global breve para obter coordenadas; dumps devem ocorrer fora de atualizacoes de schema e preferencialmente em menor movimento. Nao se promete ausencia absoluta de espera de escrita ou consumo de disco/CPU.
- A reserva de espaco e estimativa: maior entre 256 MiB e quatro vezes dados+indices mais 64 MiB. Crescimento concorrente pode causar falha; nesse caso nada e publicado.
- Coordenadas referenciam o inicio do snapshot. FULLs novos tambem registram `binlog.anchor`, uma impressao SHA-256 normalizada do prefixo de origem. O FULL sozinho nao preserva os binlogs posteriores; a captura manual esta descrita abaixo.
- A geracao e a recuperacao reais precisam de homologacao isolada autorizada; os testes desta etapa usam mocks, subprocessos ficticios e arquivos temporarios.

Testes direcionados da preparacao e do completo:

```powershell
powershell.exe -NoProfile -Command "Invoke-Pester -Script deploy/windows/tests/mysql-backup.Tests.ps1,deploy/windows/tests/mysql-full-backup.Tests.ps1 -EnableExit"
```


## Captura incremental manual delimitada

Em Windows PowerShell 5.1 **como Administrador**, selecionar explicitamente a pasta FULL de referencia:

```powershell
& 'C:\Program Files\Sysvar Hub\scripts\backup-incremental.ps1' `
    -FullBackupDirectory 'C:\ProgramData\SysvarHub\backup\full-<dataUTC>-<uuid>'
```

Os parametros opcionais `-InstallRoot`, `-ProgramDataRoot` e `-CaptureTimeoutSeconds` correspondem a instalacao oficial (timeout de decodificacao padrao: 1200 segundos). Nao existem parametros para host, porta, usuario ou banco alternativos. A conta `sysvarhub_backup` realiza todas as consultas SQL de identidade, privilegios, exclusividade e coordenadas. Nao ha conexao MySQL root, alteracao de dados, FLUSH, PURGE, RESET ou rotacao forcada.

### Delimitacao e binlog ativo

1. Revalidar o FULL e toda a cadeia ja publicada, incluindo SHA-256, sequencia, identidade e ponto confirmado.
2. Obter a posicao corrente por `SHOW MASTER STATUS` e a sequencia por `SHOW BINARY LOGS`.
3. Sob a mesma trava do FULL, ler apenas os prefixos locais necessarios ate o limite oficial. Os arquivos sao abertos somente para leitura com compartilhamento de leitura/escrita/exclusao; nao sao bloqueados para o MySQL. A origem e o datadir da instancia oficial ja validada, nunca o MySQL80 DEV.
4. Decodificar os snapshots finitos em **uma chamada** do `mysqlbinlog` homologado, com `--verify-binlog-checksum --force-if-open --base64-output=AUTO --database=sysvarhub_db --start-position=<inicio> --stop-position=<fim> --result-file=<SQL temporario>`. A primeira posicao e inclusiva e a ultima exclusiva; todos os arquivos intermediarios entram na ordem exata. O filtro `--database` e aplicado localmente, conforme o [manual mysqlbinlog](https://dev.mysql.com/doc/refman/8.0/en/mysqlbinlog.html).
5. Revalidar prefixos e identidade, compactar/verificar o SQL, publicar o gzip e manifesto juntos e confirmar o checkpoint.

O arquivo ativo **nao e declarado encerrado nem arquivado como binlog completo**. O artefato publicado e um segmento SQL executavel delimitado (`MYSQLBINLOG_SQL_BOUNDED_V1`), incluindo os eventos de linha em base64. Cabecalhos e contexto de decodificacao podem se repetir; as alteracoes ficam restritas ao intervalo `[inicio,fim)`. Os snapshots binarios temporarios podem incluir o prefixo anterior para permitir decodificacao nativa, mas sao protegidos por ACL e removidos antes da publicacao. Somente o trecho ainda nao confirmado e preservado no incremental.

Nao se usa data/hora para cortar transacoes, nem `--short-form`, `--force-read`, `--stop-never` ou captura de arquivo aberto ate um EOF movel. Novas transacoes posteriores ao ponto escolhido ficam para a proxima execucao. Sem avanco de posicao, ou com somente metadados de rotacao sem eventos relevantes, nenhum incremental e publicado e o ponto anterior permanece confirmado.

### Ancora adicionada ao FULL: necessidade e compatibilidade

Arquivo, posicao e UUID nao detectam sozinhos a recriacao do binlog com o mesmo nome/UUID. Por isso, **somente os FULLs gerados com esta versao** acrescentam `binlog.anchor` ao manifesto existente, sem remover ou renomear campos. FULLs antigos continuam preservados, mas nao iniciam uma cadeia incremental segura: gerar novo FULL manualmente apos a adequacao/homologacao autorizada. Nao ha preenchimento retroativo de uma ancora que nao foi registrada na origem.

A ancora `MYSQL8_CRC32_PREFIX_V1` calcula SHA-256 do prefixo ate a coordenada, normalizando apenas o bit `IN_USE` do evento FDE e seu CRC derivado, que podem mudar no fechamento normal. Os demais bytes permanecem vinculados. O checksum nativo CRC32 dos eventos e verificado pelo mysqlbinlog. O fim de cada captura recebe nova ancora; antes da seguinte, ela deve coincidir com a origem. Alteracao de UUID, prefixo recriado/truncado, arquivo expirado ou lacuna na numeracao bloqueia o avanco.

### Manifestos, checkpoint e retomada

Cada pasta publicada tem nome `incremental-<idFULL>-<sequencia8digitos>-<uuid>` e contem somente `binlog-segment.sql.gz` e `manifest.json`. O manifesto inclui instalacao/empresa/loja, ID e SHA do manifesto FULL, ID/SHA do predecessor, UUID do servidor, sequencia, inicio/fim e suas ancoras, limites das origens, datas UTC, tamanho, SHA-256, formato e status `VALIDO`.

O arquivo `incremental-head-<idFULL>.json` registra o ultimo ponto confirmado e o hash do manifesto correspondente. Sua substituicao e atomica, apos a publicacao da pasta. Se houver interrupcao entre essas duas etapas, a tentativa seguinte valida a extensao completa ja publicada e repara o checkpoint sem recapturar o mesmo intervalo. Um checkpoint apontando para manifesto ausente/alterado bloqueia a cadeia. Falhas antes da publicacao nao avancam o checkpoint nem removem FULLs/incrementais validos anteriores.

A publicacao, ACL, compactacao com verificacao de round-trip, escrita de JSON e limpeza limitada ao staging reaproveitam os mecanismos do FULL. Nao ha retencao, agendamento, transmissao ou restauracao nesta etapa.

### Limites e impacto

- Suporte homologado: MySQL 8.0.26+ da serie 8.0, instancia exclusiva do Hub, ROW, binlogs locais nao criptografados com CRC32 e posicoes ate 4 GiB por arquivo. Outros formatos/objetos administrativos exigem adequacao assistida.
- O acesso SQL usa somente a conta de backup. A leitura/decodificacao local nao autentica em outro servidor e exige acesso Windows administrativo ao datadir oficial; os prefixos sao validados pelo formato antes de uso.
- O ponto inicial ainda nao preservado precisa continuar disponivel no servidor. Expiracao/perda de continuidade exige novo FULL; nao se inventa uma posicao alternativa nem se purga qualquer arquivo.
- Arquivos SQL e binarios temporarios permanecem privados por ACL, sem impressao de conteudo ou credenciais. Gzip nao adiciona criptografia.
- Nao ha bloqueio global para a captura nem rotacao forcada. Ha custo de leitura local, hashing, decodificacao e compressao; o prefixo do arquivo corrente pode ser relido para validacao. A estimativa de espaco e conservadora, mas nao e uma reserva fisica contra crescimento concorrente.
- Revalidar todos os hashes da cadeia e do FULL custa leitura de disco proporcional aos arquivos preservados. A homologacao de captura/recuperacao real permanece pendente em ambiente isolado autorizado; nenhum backup real foi executado nos testes.
- O formato exige que a futura restauracao preserve a ordem dos segmentos e o contexto necessario em uma unica conexao, conforme o manual. Nao ha rotina de restauracao implementada aqui.

Testes direcionados (somente mocks, binlogs sinteticos e arquivos ficticios):

```powershell
powershell.exe -NoProfile -Command "Invoke-Pester -Script deploy/windows/tests/mysql-backup.Tests.ps1,deploy/windows/tests/mysql-full-backup.Tests.ps1,deploy/windows/tests/mysql-incremental-backup.Tests.ps1 -EnableExit"
```


## Controle local e agendamento preparado (Prompt 5)

`backup-run.ps1` executa **uma tentativa** do controlador `mysql-backup-control.ps1`.
A importacao dos helpers nao registra tarefas nem acessa o MySQL. O instalador do
Hub continua sem habilitar ou registrar o agendamento; os scripts adicionais sao
incluidos no pacote pelo mecanismo existente de copia de `*.ps1`.

O plano do Agendador usa SYSTEM (`ServiceAccount`), execucao sem login, PowerShell
5.1 sem perfil e sem interface, prioridade 7 (BelowNormal), repeticao indefinida
`PT15M`, `IgnoreNew` e recuperacao de disparo perdido. Nao depende de internet.
A tarefa nao termina a captura por atingir outro disparo; as ferramentas preservam
seus timeouts. O runner tambem reduz sua prioridade e compartilha **a mesma trava
exclusiva** usada pelos comandos manuais FULL/incremental durante toda a tentativa.
Nao e criado outro servico Windows.

### Selecao, continuidade e renovacao

- Seleciona automaticamente a pasta FULL publicada mais recente e valida manifesto,
  SHA-256, identidade/UUID e toda sua cadeia. Areas `.incomplete-*` nao sao candidatas.
  FULL mais recente invalido exige suporte; nao ha fallback silencioso para outro.
- Sem FULL, registra `INICIALIZACAO_PENDENTE`; o primeiro FULL continua sendo uma
  acao manual autorizada do suporte, usando `backup-full.ps1`.
- Persiste o nome e SHA do FULL ativo em `backup/backup-control.json`. A cadeia ativa
  e revalidada antes de adotar um FULL manual mais recente ou renovar o FULL.
  Desaparecimento, corrupcao, UUID divergente ou lacuna bloqueiam o avanco.
- A cada tentativa valida a origem e consulta a coordenada corrente. Sem avanco nao
  chama a captura; com somente metadados o helper incremental nao publica arquivo.
  Checkpoint e recuperacao de publicacao interrompida continuam nos helpers existentes.
- Renovacao padrao: FULL com pelo menos **7 dias**, na primeira execucao entre
  **03:00 (inclusive) e 04:00 (exclusive)** do horario local do Windows. A idade usa UTC;
  nao ha obrigacao de operador logado. Janelas que cruzam meia-noite sao aceitas.
- Havendo mudancas, preserva primeiro o incremental; depois revalida a continuidade
  antes da renovacao. So adota o novo FULL depois da publicacao e validacao completa.
  Nenhuma cadeia anterior e substituida ou apagada. Se o FULL falhar, preserva a selecao
  anterior e tenta novamente na proxima janela elegivel.
- Indisponibilidade MySQL/falha de captura produz `FALHA_REPETIR`. Problema de cadeia
  ou configuracao produz `INTERVENCAO_SUPORTE`, bloqueio persistente. Uma nova execucao
  agendada nao o remove e nao gera FULL para esconder perda de continuidade.

O suporte pode configurar `C:\ProgramData\SysvarHub\config\backup-policy.json`,
protegido pela ACL administrativa da instalacao. Se ausente, valem os padroes:

```json
{
  "version": 1,
  "renewal_days": 7,
  "window_start": "03:00",
  "window_end": "04:00",
  "minimum_free_bytes": 1073741824,
  "warning_storage_bytes": 68719476736,
  "timeout_seconds": 1200
}
```

As rotinas FULL/incremental continuam verificando suas margens de espaco especificas,
mesmo com o minimo de 1 GiB do controlador. O alerta de 64 GiB nao apaga arquivos.
Tempo, bytes publicados, espaco livre e armazenamento observado no inicio da tentativa
ficam no diagnostico. Os hashes de toda a cadeia sao relidos por seguranca a cada
tentativa; o resultado validado sob a trava e reutilizado na captura para nao duplicar
essa leitura. O inventario de armazenamento usa metadados e rejeita junctions.
Ha custo de disco proporcional ao tamanho da cadeia; baixa prioridade nao garante
latencia do PDV. A janela e os limites precisam de homologacao em hardware de loja.

### Ativacao futura pelo suporte — nao realizada nesta etapa

Em Windows PowerShell 5.1 elevado, **somente na instalacao oficial ja adequada**
(SysvarHubMySQL/3307, instancia exclusiva, ferramentas homologadas, conta e ACLs
preparadas, espaco disponivel e FULL inicial validado):

```powershell
# Apenas mostra o XML planejado; nao grava nem registra tarefa.
& 'C:\Program Files\Sysvar Hub\scripts\backup-task-install.ps1'

# Acao futura explicita: registra DESATIVADA, sem iniciar backup.
& 'C:\Program Files\Sysvar Hub\scripts\backup-task-install.ps1' -Register

# Consulta tarefa e diagnostico; nao modifica o agendamento.
& 'C:\Program Files\Sysvar Hub\scripts\backup-task-status.ps1' |
    ConvertTo-Json -Depth 12

# Somente depois da revisao/autorizacao do suporte: habilita os disparos futuros.
Enable-ScheduledTask -TaskName 'SysvarHubMySQLBackup' -TaskPath '\'

# Desativa disparos futuros; nao mata uma captura em andamento.
& 'C:\Program Files\Sysvar Hub\scripts\backup-task-disable.ps1'
```

O registro recusa qualquer tarefa homonima existente (sem `-Force`) e verifica
proprietario/permissoes do codigo que sera executado como SYSTEM. A desativacao exige
descricao, executavel, argumentos e identidade oficiais; uma tarefa divergente e
preservada para revisao assistida. Nenhum desses comandos foi executado contra o
Agendador nesta implementacao. Instalacoes antigas nao sao adaptadas automaticamente.

O diagnostico JSON inclui ultimo FULL, ultimo incremental valido (com seu FULL de
origem), tentativa UTC, sucesso UTC, duracao, tamanho, situacao, codigo de falha,
alertas e `support_required`. Nao armazena consultas, conteudo, senhas ou mensagens
nativas. `Get-ScheduledTaskInfo` complementa o resultado do processo; erros do runner
retornam codigo 1. Falta do FULL inicial e espera por outra execucao nao inventam sucesso.

Em bloqueio, suporte deve preservar arquivos/checkpoints e investigar a causa.
Apos reparar a **mesma cadeia**, pode executar manualmente `backup-run.ps1
-ResumeAfterSupport`; isso exige nova validacao completa e nao ignora divergencias.
Estado JSON invalido e preservado, sem reinicializacao automatica. Se a cadeia nao
puder ser reparada, um novo FULL e a substituicao **explicitamente assistida** da
selecao local exigem procedimento autorizado; nao ha botao de reset automatico.
Nao ha restauracao, transmissao, retencao ou remocao de backups neste prompt.

Testes adicionais isolados: `tests/mysql-backup-control.Tests.ps1` e
`tests/mysql-backup-task.Tests.ps1`, com mocks do MySQL/Agendador e arquivos ficticios.
As suites FULL/incremental tambem exercitam compartilhamento da trava, controlador,
checkpoint interrompido e retry sem duplicacao, sem backup real.

Referencia do plano: [schema oficial do Agendador Windows](https://learn.microsoft.com/en-us/windows/win32/taskschd/task-scheduler-schema).
