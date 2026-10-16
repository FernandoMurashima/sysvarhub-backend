# Backup e recuperacao — ensaio integrado do Prompt 12

**Integracao simulada aprovada; homologacao operacional real PENDENTE.**
Nenhum MySQL80 DEV/operacional, servico Windows, agendamento ou configuracao
operacional foi alterado. Nenhuma restauracao SQL real foi executada.

O parecer completo esta no Backend Central em
`docs/hub-backup-homologacao-isolada.md`, incluindo etapas, falhas, metricas,
condicoes para ativacao futura e roteiro de desastre. O procedimento assistido
preparado permanece em [RECUPERACAO.md](RECUPERACAO.md).

O novo fixture `tests/backup-integrated-fixture.Tests.ps1` chama os geradores FULL
e incremental existentes, compactacao, ancoras, continuidade e travas. Somente
mysqldump/mysqlbinlog, consultas/servicos Windows e metadados MySQL sao simulados.
Exporta dados ficticios em TEMP para a API Central real sob HTTPS loopback/SQLite.
Nao e outro gerador operacional de backup. Recusa exportacao fora de pasta TEMP
exclusiva de teste e nao executa ferramentas MySQL ou servicos reais.

O teste integrado encontrou/corrigiu no Hub dois contratos:

- Rotas de progresso/partes/finalizar agora usam UUID canonico com hifens, exigido
  pelo roteador Django da Central. Manifestos e checkpoints conservam seus IDs.
- O comparador de coordenadas preserva arquivo/posicao/ancora e nao confunde o
  campo descritivo reference=SNAPSHOT_START do FULL com uma lacuna incremental.
  Hashes do pacote/arquivos continuam exigidos; ancoras diferentes sao rejeitadas.

O simulador HTTP antigo foi alinhado ao converter UUID real, com assercao mais
estrita; os asserts de idempotencia/streaming/predecessores foram preservados.

Validacao: 14 cenarios integrados Central, 59 testes direcionados Hub e um fixture
Pester novo aprovados. Escritas de ate 64 KiB; para entradas ficticias de 1 KiB,
128 KiB e 2 MiB, picos tracemalloc de aproximadamente 0,47/0,76/2,48 MiB durante
exportacao + replay simulado. Nao mede MySQL, memoria nativa ou desempenho operacional.
Os quatro erros antigos de mocks do worker permanecem separados e sem alteracoes.

Reproducao do fluxo completo, no checkout Central, com ambos os repositorios atuais:

```powershell
.\venv\Scripts\python.exe -B -m hub.run_backup_integration_tests --hub-root C:\SysvarHub\Backend
```

O runner configura SQLite em memoria antes de inicializar Django; nao usar o banco
DEV. No Hub:

```powershell
.\.venv\Scripts\python.exe -B -m unittest integracao.tests_backup_transmission runtime.tests_recovery
powershell.exe -NoProfile -Command "Invoke-Pester -Script deploy/windows/tests/backup-integrated-fixture.Tests.ps1 -EnableExit"
```

Usar Windows PowerShell 5.1 real para Pester. O runner de integracao carrega seu
modulo Utility nativo, evitando heranca indevida de modulos pwsh 7. Nao habilitar
nenhum recurso operacional para executar esses testes.

Antes de qualquer ativacao: laboratorio MySQL real autorizado, build do instalador,
contas/ACLs/servicos, transacoes/rotacao nativas e restauracao/conferencia real devem
ser homologados. Aplicacao SQL e CHECK TABLE/FKs foram simulados nesta etapa. O Hub
restaurado permanece bloqueado ate revisao do suporte, conciliacao, reautorizacao
e aprovacao explicita. Certificados, arquivos externos e dados posteriores ao ultimo
ponto nao sao garantidos; nao reenviar eventos com identidades ou payloads alterados.
