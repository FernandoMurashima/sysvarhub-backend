# Devolucao / Troca online

O modulo Devolucao / Troca pertence ao Sysvar Hub, mas em operacao online a Central e a fonte oficial para localizar vendas, pesquisar clientes, listar historico, validar saldo devolvivel, registrar a devolucao, gerar o Vale-Troca e controlar o saldo.

O fluxo online e sempre:

Hub Frontend -> Hub Backend local -> Central Backend -> Hub Backend local -> Hub Frontend.

O Frontend nunca acessa a Central diretamente e nunca recebe o token de retaguarda do Hub.

## Vendas de outra loja

A consulta online pode localizar vendas finalizadas de qualquer loja permitida pela mesma empresa/tenant do Hub. A venda original permanece vinculada a loja de origem. A devolucao registrada na Central usa a loja do Hub autenticado como loja de recebimento fisico.

Exemplo: uma venda da Loja Centro devolvida na Loja Barra mantem `VendaDevolucao.venda` apontando para a venda original da Loja Centro, mas grava `VendaDevolucao.loja` como Loja Barra. A entrada de estoque ocorre na Loja Barra.

## Vale-Troca

O Vale-Troca online nasce oficialmente na Central, vinculado a devolucao e a cliente identificada. O Hub apenas guarda um espelho local confirmado para continuidade operacional e exibicao.

## Espelho local e contingencia

`VendaDevolucaoHub` preserva a estrutura local para devolucoes online espelhadas e para contingencia offline. Para devolucoes online, o espelho pode representar venda remota sem criar uma `VendaHub` artificial. Campos de origem central guardam documento, loja de origem e identificadores de retaguarda.

Devolucao online confirmada nao enfileira `DEVOLUCAO_FINALIZADA`, evitando reenvio e duplicidade.

## Offline

Quando a Central esta OFFLINE, o Hub pode registrar devolucao provisoria com cliente identificado, itens do catalogo local e valor liquido original. A entrada de estoque ocorre imediatamente na loja do Hub recebedor. O credito local usa `ValeTrocaHub` com `provisorio=True` e documento tecnico `VT-HUB-*`, mas a interface deve apresenta-lo como Credito provisorio, sem tratar esse identificador como documento comercial.

O evento `DEVOLUCAO_FINALIZADA` carrega os dados da venda local quando ela existe ou os dados declarados do documento original para venda de outra loja. Vendas que consumirem credito provisorio recebem dependencia explicita da devolucao e so podem sincronizar apos a Central materializar a devolucao e retornar o Vale-Troca oficial. Em conflito, a devolucao e o credito permanecem registrados localmente para resolucao administrativa posterior.
