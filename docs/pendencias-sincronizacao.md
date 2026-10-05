# Pendencias de Sincronizacao

## Finalidade

A tela `/pendencias-sincronizacao` acompanha a fila local `EventoSyncHub` do Hub. Ela nao substitui a Central como autoridade de processamento: o Hub mostra sua fila, suas tentativas, seus bloqueios locais e a resposta recebida da Central.

O frontend consulta apenas o Hub Backend em `/api/terminal/pendencias-sync/`.

## Estados exibidos

- `PENDENTE`: evento aguardando envio.
- `PROCESSANDO`: evento tomado pelo worker local.
- `SINCRONIZADO`: evento confirmado pela Central como processado ou duplicado idempotente.
- `ERRO`: falha operacional ou transitoria sujeita a retry.
- `CONFLITO`: falha de consistencia que exige revisao administrativa.
- `AGUARDANDO_DEPENDENCIA`: estado operacional derivado quando o status real continua `PENDENTE` ou `ERRO`, mas o evento depende de outra operacao.

## Dependencias

A tela interpreta `payload.dependencias.devolucoes_uuid` ja usado pelo fluxo de Vale-Troca provisorio. Quando uma venda depende de devolucao ainda provisoria, a API retorna `bloqueado_por_dependencia=true`, a dependencia amigavel e bloqueia retry manual.

Essa estrutura prepara a visibilidade operacional para o Prompt 8, mas nao corrige a contingencia offline de Devolucao/Troca. O Prompt 8 permanece pendente de homologacao/correcao funcional.

## Retry

O retry manual e permitido somente para eventos `ERRO` ou `PENDENTE` sem dependencia bloqueante. A acao apenas limpa `proxima_tentativa_em` e chama o worker real `sincronizar_eventos_pendentes`, preservando payload, chave idempotente e contador de tentativas.

Eventos `CONFLITO` nao possuem acao de ignorar, excluir, editar payload, marcar como resolvido ou forcar sincronizacao.

## Isolamento e seguranca

Os endpoints exigem terminal e operador autenticados via `TerminalOperadorAuthentication`, usam o Hub do terminal autenticado e nunca aceitam Hub por parametro. A serializacao sanitiza chaves sensiveis como senha, certificado, CSC, token e chave privada antes de expor payload/resposta tecnica.

## Central offline

A listagem da fila nao depende da Central online. A resposta inclui o status calculado da Central para a tela indicar indisponibilidade, mas a consulta das pendencias continua acessivel.
