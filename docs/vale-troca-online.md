# Vale-Troca online

O Vale-Troca oficial e emitido pela Central. O Hub consulta, lista, reserva e consome esse credito online, mantendo apenas o espelho local necessario para operacao, reconciliacao e sincronizacao futura.

## Documento comercial

O documento comercial do Vale-Troca segue exatamente `VT` + 7 digitos numericos, por exemplo `VT0000001`. A sequencia pertence a empresa/tenant na Central, nao a loja. A loja continua vinculada ao Vale, mas nao compoe a numeracao.

O ultimo numero permitido e `VT9999999`. Ao esgotar a faixa, novas emissoes devem ser bloqueadas pela Central com erro funcional, sem reiniciar nem reutilizar numeros.

O Hub deve apresentar e trafegar o documento oficial retornado pela Central. Consultas manuais normalizam letras minusculas para maiusculas e exigem o formato `VT0000001`.

Quando a Central estiver online, a lista exibida no PDV deve vir sempre da Central. `ValeTrocaHub` e `beneficiosCliente.vales_troca` nao sao fonte da lista online e nao podem ser usados como fallback silencioso para Vales oficiais.

Quando a Central devolver um Vale oficial, o Hub pode canonicalizar o espelho local pelo `retaguarda_id`/id oficial, atualizando o documento para o valor canonico sem criar outro `ValeTrocaHub`. O texto do documento e apenas fallback de sincronizacao, nao identidade primaria.

Identificador tecnico interno nao e numero comercial. UUIDs, ids locais, ids de retaguarda, chaves de idempotencia e documentos temporarios `HUB-*` podem existir para sincronizacao, mas nao devem aparecer para operador ou cliente como numero de Vale-Troca. A mesma diretriz deve ser revisada futuramente em venda/cupom, devolucoes, contas a receber, contas a pagar e demais documentos internos.

## Operacao

No PDV, com cliente identificado e Central online, o fluxo principal e selecionar um Vale aberto da lista oficial do cliente. A digitacao manual fica como caminho secundario para consulta pontual. Offline, a consulta/lista de Vale-Troca oficial fica indisponivel; contingencia offline continua futura.

Na finalizacao, pagamentos de Vale-Troca seguem como `tPag 05` Credito Loja. Antes de enviar `VENDA_FINALIZADA`, o Hub reserva os Vales na Central e, no consumo, usa o documento oficial e a identidade de retaguarda retornada pela Central.
