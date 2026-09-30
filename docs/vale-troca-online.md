# Vale-Troca online

O Vale-Troca oficial e emitido pela Central. O Hub consulta, reserva e consome esse credito online, mantendo apenas o espelho local necessario para operacao e sincronizacao.

## Documento comercial

O documento comercial do Vale-Troca segue exatamente `VT` + 7 digitos numericos, por exemplo `VT0000001`. A sequencia pertence a empresa/tenant na Central, nao a loja. A loja continua vinculada ao Vale, mas nao compoe a numeracao.

O ultimo numero permitido e `VT9999999`. Ao esgotar a faixa, novas emissoes devem ser bloqueadas pela Central com erro funcional, sem reiniciar nem reutilizar numeros.

O Hub deve apresentar e trafegar o documento oficial retornado pela Central. Consultas manuais normalizam letras minusculas para maiusculas e exigem o formato `VT0000001`.

Identificador tecnico interno nao e numero comercial. UUIDs, ids locais, ids de retaguarda, chaves de idempotencia e documentos temporarios `HUB-*` podem existir para sincronizacao, mas nao devem aparecer para operador ou cliente como numero de Vale-Troca. A mesma diretriz deve ser revisada futuramente em venda/cupom, devolucoes, contas a receber, contas a pagar e demais documentos internos.

## Operacao

No PDV, com cliente identificado e Central online, o fluxo principal e selecionar um Vale aberto da lista do cliente. A digitacao manual fica como caminho secundario para consulta pontual.

Na finalizacao, pagamentos de Vale-Troca seguem como `tPag 05` Credito Loja. Antes de enviar `VENDA_FINALIZADA`, o Hub reserva os Vales na Central e, no consumo, usa o documento oficial e a identidade de retaguarda retornada pela Central.
