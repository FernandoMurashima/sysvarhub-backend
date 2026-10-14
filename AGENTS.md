# AGENTS.md — Sysvar Hub Backend

Backend Django local da loja. O Hub é a camada operacional do Sysvar na loja (não é um ERP independente): mantém cópias locais, estado operacional e filas, atende os terminais/PDVs na LAN e sincroniza com a Central.

```text
Central  ↕ Internet / APIs autenticadas
Hub      ↕ LAN
Terminais / PDVs
```

Repositórios relacionados: `sysvarhub-frontend` (Hub), `sysvarbackend` (Central). Mudanças de contrato Hub ↔ Central exigem conferir o lado da Central.

Contexto específico: `C:\Takeshi\takeshi\10 Projetos\Sysvar\Contexto do Projeto\Sysvar Hub.md` (busque o trecho do assunto) e as notas `Mapa Técnico - Sysvar Hub - *` e `Riscos e Cuidados - Sysvar Hub - *`.

## Estrutura

- `core/`: operação local (caixa, vendas, pagamentos, NFC-e, operadores, vendedores, fechamento). Serviços em `core/services/`; testes em `core/tests*.py`.
- `integracao/`: comunicação e sincronização com a Central.
- `sysvarhub/`: configuração. `deploy/`, `runtime/`: instalação e execução.
- `docs/`: só documentação técnica acoplada a este código.

## Comandos

```text
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py test core.tests_<assunto>
.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
```

Rode os testes do arquivo do assunto alterado (ex.: `core.tests_caixa`, `core.tests_nfce`). Ambiente DEV completo: `iniciar-hub-dev.ps1` (exige o serviço Windows `SysvarHub` parado).

## Cuidados deste repositório

- **Idempotência e retry:** todo envio à Central deve ser seguro para reenvio; nada pode duplicar venda, pagamento, movimento de caixa ou documento fiscal.
- **Produção:** não executar `migrate`, migrations ou alterações de esquema em banco de produção sem autorização explícita.
- **Contingência:** a loja continua operando sem a Central; pendências ficam em fila e são reconciliadas.
- **Eventos já transmitidos:** preserve `evento_uuid`, chave de idempotência e snapshot/payload depois da primeira tentativa de transmissão. Reenvios não devem reescrever o evento enviado; mudanças de contrato exigem análise de compatibilidade.
- **Operador, Terminal e Caixa são conceitos distintos.** Login de operador não abre caixa; sair do PDV não fecha caixa nem cancela venda.
- Credenciais do Hub com a Central nunca chegam ao navegador.
- Alterações em caixa, pagamentos, vendas, NFC-e ou sincronização exigem teste.

## Documentação oficial do Sysvar

A documentação oficial (regras de negócio, decisões, riscos, homologações) fica no cofre:

- local: `C:\Takeshi\takeshi\10 Projetos\Sysvar`
- repositório: `FernandoMurashima/sysvar-vault` (`main`)

A skill `sysvar-contexto` centraliza o procedimento detalhado de recuperação das fontes do projeto; não duplique seu roteiro neste arquivo.

Antes de alterar código:

1. Use a skill `sysvar-contexto`, se estiver instalada e disponível, para localizar as fontes vigentes do módulo.
2. Se indisponível, consulte no cofre somente os documentos pertinentes e existentes; não crie documentos ausentes para completar um padrão.
3. Leia o código atual relacionado antes de criar qualquer coisa.

Não leia as notas gerais grandes (`Sysvar.md`, `Riscos e Cuidados.md`, `Mapa Técnico.md`) inteiras: busque nelas o trecho do módulo.

## Regras de trabalho

- **Pré-verificação:** antes de alterar, confira branch, HEAD e `git status --short`. Preserve alterações preexistentes; nunca sobrescreva trabalho alheio.
- **Tarefa somente de análise:** auditorias, levantamentos e diagnósticos não autorizam editar arquivos, criar commits ou fazer push.
- **Reaproveitar → refatorar → estender → criar.** Não construa do zero o que já existe. Antes de criar model, endpoint, serializer, service, componente ou helper, procure um equivalente.
- **Não invente regra de negócio.** Se houver dúvida que o prompt, a documentação e o código não resolvem, preserve o comportamento atual e registre a dúvida no relatório final.
- **Hierarquia de fontes:** decisão aprovada no prompt > documentação vigente > código atual > inferência. Seções datadas e checkpoints são histórico.
- **Escopo fechado.** Altere só o que a tarefa pede. Correção pequena não vira refatoração.
- **Backend é a autoridade** de validações, cálculos e permissões. O frontend pode antecipar, nunca substituir.
- **Multiempresa:** todo acesso a dados respeita tenant, empresa e loja no backend.
- **Testes proporcionais ao escopo**, incluindo o caso negativo da regra alterada. Execute testes direcionados por padrão; suíte completa somente se a abrangência da mudança justificar ou a tarefa solicitar. Proibido: `skip` sem necessidade, comentar teste, enfraquecer assert, mudar regra aprovada para o teste passar.
- **Segredos:** não versionar nem copiar `.env`, tokens, XML de cliente, dumps, logs ou filas SQLite.

## Encerramento de toda tarefa

1. Em tarefas com alterações autorizadas, executar somente os testes, verificações e builds pertinentes; não rodar a suíte completa por padrão. Em auditorias e diagnósticos, não executar validações invasivas sem solicitação.
2. Somente quando a tarefa autorizar modificações e versionamento: revisar o diff, executar `git add` apenas dos arquivos intencionais, criar `git commit` com resumo curto, imperativo, em português (ex.: `Protege idempotencia de evento de devolucao Hub`) e executar `git push origin main` se autorizado. Nunca incluir modificações preexistentes não relacionadas.
3. Sem mensagens intermediárias. Entregar um único relatório final com: resumo; documentação consultada; código reaproveitado; arquivos alterados; migrations; testes e builds pertinentes (ou motivo da não execução); SHA e confirmação do push quando houver; `git status` final; pendências reais e dúvidas de negócio. Em tarefas somente de leitura, informar expressamente que não houve alterações nem commit/push.
