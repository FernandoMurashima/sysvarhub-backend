# Recuperacao assistida do Hub — Prompt 11

Implementacao preparada e testada **somente com mocks e arquivos ficticios**.
Nao foi restaurado banco real nem homologada a recuperacao operacional. Nenhum
servico ou tarefa e ativado por esta entrega. Este procedimento futuro exige
suporte autorizado, janela propria e notebook substituto isolado da LAN/Internet
operacionais ate a conferencia final. Nao executar no MySQL80/3306 DEV.

## Pacote e identidade

O suporte obtem previamente na Central o pacote autorizado do Prompt 10. Nao ha
novo cliente de download, dependencia do token do notebook perdido ou consulta da
Central durante a restauracao. Revogar o Hub antigo na Central e impedir que dois
notebooks da mesma loja operem simultaneamente e responsabilidade do suporte.

Guardar **somente o objeto `manifesto`** da resposta da Central em `pacote.json`,
em UTF-8, junto dos `.sql.gz` individuais com os nomes indicados. Nao salvar o token
de autorizacao junto do pacote. O manifesto pode mudar de indentacao: o SHA oficial
e calculado sobre JSON ASCII com chaves ordenadas, separadores `,` e `:` sem espacos,
conforme o contrato Central. Preservar os arquivos gzip byte a byte.

Obter `manifesto_sha256`, UUID do ponto, empresa, loja, instalacao e UUID MySQL de
origem da autorizacao confiavel do suporte, por canal independente dos arquivos
locais. Um hash recalculado apenas do pacote entregue **nao comprova autorizacao**.
O ponto informado deve ser exatamente o ultimo arquivo do pacote, nunca um sufixo
arbitrario ou conjunto incompleto. O SHA fixado vincula tambem Hub, datas, ordem,
coordenadas/ancoras e hashes dos manifestos originais exportados pela Central.

Restringir previamente a pasta e seus arquivos a SYSTEM e Administradores Windows,
com heranca protegida na pasta. Nao usar Downloads compartilhado, rede, links ou
juncoes. O script confere ACLs; nao troca permissoes nem modifica os originais.
Os arquivos recebidos sao gzip contendo SQL sensivel, ja descriptografado pela
Central; manter em volume privado protegido (por exemplo BitLocker), nao publicar
nem registrar seu conteudo. Nao ha spool SQL em claro durante a importacao.

## Preparacao do notebook

Usar um **novo build** do instalador com este codigo e as ferramentas oficiais
MySQL 8.0.26+ homologadas, todas da mesma versao. O modo normal permanece igual.
O novo modo e explicito: `SysvarHubSetup.exe /RECOVERY=1 /CONFIRMRECOVERY=1`.
Sem a segunda confirmacao, o instalador recusa a preparacao de recuperacao.

Com os arquivos oficiais ja instalados, a entrada equivalente e
`install-hub.ps1 -RecoveryMode -ConfirmRecoveryPreparation`, em Windows PowerShell
**5.1 elevado**, com InstallRoot/ProgramDataRoot oficiais. Nao executar ambos como
rotina repetitiva: escolher a entrada apropriada ao estado do notebook.

O modo reutiliza bootstrap/configuracao/contas do instalador e prepara somente
MySQL oficial, porta 3307 e schema vazio `sysvarhub_db`. Nao aplica migrations,
nao executa `preparar_instalacao`, nao instala/inicia o PDV, nem provisiona backup
ou agendamento. Credenciais externas sao novas; nao copiar `.env` ou credenciais
administrativas do notebook antigo. O servico MySQL pode ser iniciado durante a
preparacao futura autorizada; o servico **SysvarHub** permanece parado/desativado.

O marcador privado `config/recovery-block.json` e criado antes da preparacao. Ele
bloqueia instalador normal, inicializacao Django/Waitress e ciclos do worker antes
de ler credenciais ou sincronizar. Nao e removido automaticamente, inclusive se
houver erro. Se o servico SysvarHub ja estiver executando, a entrada PowerShell
recusa e exige parada assistida previa. Manter tambem tarefas antigas desativadas
e nenhum processo alternativo usando o MySQL; o preflight rejeita outras sessoes.

Se uma instalacao padrao ja criou tabelas/dados, a recuperacao **recusa o destino**.
Preservar essa instalacao para diagnostico e preparar outra instancia oficial vazia
com suporte; nao ha limpeza automatica, DROP DATABASE, RESET ou PURGE. O importador
reproduz o SQL original do dump/segmentos, incluindo o DDL gerado pelo mysqldump,
exclusivamente depois de comprovar que nao existem objetos no banco de destino.

## Conta administrativa temporaria

Provisionar, por canal administrativo local seguro, **sysvarhub_restore@127.0.0.1**
exclusiva para a operacao. Nunca reutilizar root ou sysvarhub_backup para importar.
Nao ha provisionamento automatico dessa conta nem alteracao de conta DEV.

Privilegios globais minimos: `PROCESS`, `SHOW DATABASES`, `SESSION_VARIABLES_ADMIN`
e `REPLICATION_APPLIER`. Os dois primeiros permitem conferir sessoes e exclusividade
sem ocultar bancos; os demais permitem aplicar eventos e controlar a sessao sem
SUPER/BINLOG_ADMIN. No schema literal `sysvarhub_db`: SELECT, INSERT, UPDATE, DELETE,
CREATE, ALTER, DROP, INDEX, LOCK TABLES, REFERENCES, TRIGGER, CREATE VIEW, SHOW VIEW e
CREATE TEMPORARY TABLES. Nao conceder acesso a outros bancos, papeis ou GRANT OPTION.
Escapar `_` no GRANT se partial_revokes=OFF, reutilizando a convencao do instalador.
O verificador tambem aceita ALL PRIVILEGES somente nesse schema literal.

Criar `config/mysql-restore.cnf`, com secao `[client]` e somente os campos user,
password, host=127.0.0.1, port=3307, protocol=TCP. User fixo sysvarhub_restore;
senha aleatoria com caracteres A-Z/a-z/0-9/_/-, gerada/guardada pelo suporte sem
linha de comando. Proteger arquivo para SYSTEM/Administradores, sem heranca.
A credencial nao e impressa, copiada para plano ou passada como argumento; o
cliente MySQL le diretamente esse arquivo. Variaveis MYSQL_PWD e login-path pessoal
sao isolados no subprocesso. Revogar a conta temporaria depois da conferencia,
por procedimento administrativo separado.

## Pre-verificacao e execucao futuras

Na pasta `scripts` instalada, PowerShell 5.1 elevado:

```powershell
. .\mysql-recovery.ps1
$op = @{
    PackageDirectory = 'C:\RecuperacaoPrivada\pacote'
    ManifestSha256 = '<sha256 oficial fornecido pelo suporte>'
    PointUuid = '<uuid do ponto autorizado>'
    EmpresaId = 1
    LojaId = 20
    InstallationUuid = '<uuid da instalacao restaurada>'
    SourceServerUuid = '<uuid MySQL de origem>'
}
Invoke-HubMySqlRecovery @op
```

Os valores sao ilustrativos. Substituir pelos dados oficiais, nunca aceitar
empresa/loja por suposicao. InstallRoot/ProgramDataRoot podem ser informados se
forem os caminhos verificados do servico oficial. Se ProgramDataRoot for
personalizado, o suporte deve configurar e conferir SYSVARHUB_PROGRAMDATA
persistentemente no ambiente do servico e dos comandos antes da preparacao,
para que todos usem o mesmo marcador; preferir os caminhos oficiais padrao.
O runtime `recover` e interno;
usar a entrada PowerShell, que valida servico, binarios, ACLs e privilegios.

DRY-RUN e o padrao: confere pacote inteiro, gzip/CRC, SHA de cada arquivo, ordem,
predecessores, coordenadas/ancoras, ponto, identidade, FULL e versao. Usa apenas
consultas de metadados MySQL, sem importar SQL nem criar tabelas; uma trava de
controle local impede execucoes simultaneas. Nao inicia servicos nem escreve estado
de tentativa. Exige MySQL vazio/exclusivo, diretorios oficiais, porta 3307, nenhuma
outra sessao e espaco livre com margem de duas vezes o SQL expandido + 256 MiB.

A versao 8.0.x do servidor destino e das ferramentas deve ser **igual** a indicada
no cabecalho do FULL; outras versoes exigem homologacao separada. O notebook novo
deve ter UUID MySQL novo; nao copiar auto.cnf nem a identidade fisica do servidor
perdido. A identidade logica da loja/Hub e preservada no banco.

Somente apos DRY-RUN aprovado e autorizacao explicita para aquele ponto/hash:

```powershell
$confirmacao = "RESTAURAR:$($op.PointUuid):$($op.ManifestSha256)"
Invoke-HubMySqlRecovery @op -Execute -Confirmation $confirmacao
```

FULL primeiro e todos os incrementais seguintes passam por **uma unica conexao**
MySQL, em blocos de ate 64 KiB, preservando a ordem e o contexto SQL. Na propria
conexao de importacao, confere UUID, conta, porta e banco sem objetos antes do FULL.
Antes de cada
segmento, SQL_LOG_BIN e desativado somente nessa sessao; a confirmacao verifica que
continua desativado. Nao muda configuracao global. Nao ha reconnect ou --force;
qualquer erro ou timeout mata o cliente e impede o envio do proximo segmento.
Timeout padrao 7200 s, configuravel entre 60 e 86400. Nao e garantia de rollback:
DDL/transacoes anteriores podem ja ter sido aplicados quando uma falha acontece.

O motor confirma cada segmento, verifica o UUID de destino, identidade logica,
presenca das tabelas essenciais, CHECK TABLE QUICK nas tabelas e ausencia de orfaos
nas chaves estrangeiras (incluindo compostas). Nao reescreve filas, UUIDs, hashes,
status de eventos, vendas ou credenciais restauradas. Conferencia comercial/fiscal
e reconciliacao com a Central continuam sendo etapas do suporte.

Diagnosticos privados: `config/recovery-state.json` e historico append-only
`config/recovery-history.jsonl`, com ponto, coordenada, etapa, UTC, resultado e
codigos seguros. Nao registram SQL, senhas, tokens ou stderr nativo. O pacote e
preservado. Resultado final **RESTAURADO_BLOQUEADO**, pdv_liberado=false e
restauracao_homologada=false; nunca significa liberacao operacional.

## Falhas, retomada e liberacao

Qualquer falha mantem marcador, servicos parados/desativados e dados parciais para
analise. Nao repetir sobre destino parcialmente preenchido, nao pular segmentos
nem calcular outro UUID para contornar a falha. `-ResumeAfterSupport` permite nova
tentativa apenas depois de intervencao explicita e nova verificacao de destino
vazio, com o mesmo pacote/ponto autorizado. Nao e retomada no meio de SQL e nao
limpa banco. Uma recuperacao concluida nao pode ser reaplicada pelo comando.

Antes de considerar liberacao, suporte deve conferir vendas, caixa, estoque,
filas e eventos pendentes com a Central, mantendo evento_uuid, chave_idempotencia e
payload originais. A reconciliacao existente da Central continua responsavel por
reconhecer eventos ja recebidos; nao e executada nesta etapa. Preparar novo
pareamento/reautorizacao se necessario, revogando o token antigo e conferindo a
identidade da mesma loja/Hub. **Nao iniciar com credenciais antigas sem essa revisao.**

Nao ha comando de liberacao automatica: alteracao das credenciais, retirada do
marcador e reativacao do servico dependem de aprovacao explicita do suporte apos
conferencia, com isolamento de rede mantido ate entao. Nao executar instalador
normal para contornar o bloqueio ou aplicar migrations antes de avaliar a versao
do schema restaurado. Implantacao, carga e restauracao real ainda precisam de
homologacao autorizada; esta entrega nao realiza essas acoes nem o Prompt 12.

Certificados, configuracoes externas, anexos fora do banco, binarios e operacoes
posteriores ao ultimo ponto incremental nao estao garantidos por este pacote.
CHECK TABLE/FKs nao comprovam consistencia comercial ou restauracao homologada.

Fontes tecnicas: [mysqlbinlog e sessao unica](https://dev.mysql.com/doc/refman/8.0/en/mysqlbinlog.html),
[privilegios de BINLOG](https://dev.mysql.com/doc/refman/8.0/en/binlog.html) e
[controle de variaveis da sessao](https://dev.mysql.com/doc/refman/8.0/en/replication-options-binary-log.html).
