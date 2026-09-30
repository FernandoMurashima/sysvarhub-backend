# Vale-Troca Online no PDV

O Hub usa Vale-Troca oficial somente com Central ONLINE. Consulta, reserva, cancelamento e consumo passam pelo Hub Backend, que autentica na Central com a credencial do Hub.

O pagamento especial `VALE_TROCA` nao depende de uma `FormaPagamentoHub` comercial. O pagamento local guarda documento, id do Vale na Central, id da reserva, valor reservado e `operacao_uuid`.

Ao finalizar a venda, o Hub reserva todos os Vales em lote antes de concluir localmente. Se alguma reserva falhar, a venda nao finaliza. A sincronizacao `VENDA_FINALIZADA` envia documento, operacao, reserva e valor para a Central consumir oficialmente de forma idempotente.

O espelho local de Vale-Troca continua existindo para contingencia offline futura. Nesta etapa, vales oficiais sincronizados nao sao consumidos offline e nao substituem a consulta/reserva Central.

Na NFC-e, o instrumento especial usa `tPag 05` Credito Loja. Como nao representa entrada de dinheiro, nao deve compor numerario de caixa.
