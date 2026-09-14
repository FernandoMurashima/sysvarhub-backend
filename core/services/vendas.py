import re
import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.db import IntegrityError, transaction
from django.db.models import Prefetch, Sum
from django.utils import timezone

from core.models import (
    CatalogoItemHub,
    EstoqueMovimentoHub,
    FormaPagamentoHub,
    FormaPagamentoParcelaHub,
    SessaoCaixaHub,
    Terminal,
    VendaEventoHub,
    VendaHub,
    VendaItemHub,
    VendaPagamentoHub,
    VendaPagamentoParcelaHub,
)
from core.services.caixa import obter_caixa_terminal, obter_sessao_caixa_aberta
from core.services.operadores import serializar_operador


ZERO_2 = Decimal("0.00")
QUANTIDADE_DECIMAL = Decimal("1")
QUANTIDADE_ESTOQUE = Decimal("0.001")
VALOR_PAGAMENTO_MAXIMO = Decimal("9999999999999999.99")
DINHEIRO = "DINHEIRO"
ERRO_PAGAMENTO_ALTERAR_VENDA = "Remova os pagamentos antes de alterar a venda."
ERRO_OPERACAO_PAGAMENTO_DIVERGENTE = "Operação de pagamento já utilizada com dados diferentes."


class VendaError(Exception):
    """Erro de domínio controlado para operações de venda local."""


class VendaConflictError(VendaError):
    pass


class SaldoInsuficienteError(VendaConflictError):
    def __init__(self, mensagem, estoque_disponivel):
        super().__init__(mensagem)
        self.estoque_disponivel = estoque_disponivel


class VendaNotFoundError(VendaError):
    pass


class VendaValidationError(VendaError):
    pass


def obter_sessao_caixa_terminal(terminal):
    caixa = obter_caixa_terminal(terminal, exigir_ativo=True)
    sessao = obter_sessao_caixa_aberta(caixa)
    if not sessao:
        raise VendaConflictError("Caixa não está aberto.")
    return sessao


def obter_venda_aberta_terminal(terminal):
    return (
        VendaHub.objects.select_related(
            "hub",
            "sessao_caixa",
            "terminal",
            "operador_criacao",
            "sessao_operador_criacao",
        )
        .prefetch_related("itens")
        .filter(
            hub=terminal.hub,
            terminal=terminal,
            status=VendaHub.STATUS_ABERTA,
        )
        .first()
    )


def venda_atual(terminal):
    obter_sessao_caixa_terminal(terminal)
    return obter_venda_aberta_terminal(terminal)


def adicionar_item(terminal, operador, sessao_operador, *, sku_id, quantidade=1):
    quantidade = validar_quantidade(quantidade)
    sku_id = validar_sku_id(sku_id)

    with transaction.atomic():
        terminal_bloqueado = Terminal.objects.select_for_update().select_related("hub").get(pk=terminal.pk)
        sessao_caixa = obter_sessao_caixa_terminal(terminal_bloqueado)
        catalogo_item = obter_catalogo_item_bloqueado(terminal_bloqueado.hub, sku_id)
        validar_catalogo_vendavel(catalogo_item)
        venda = obter_venda_aberta_terminal(terminal_bloqueado)
        validar_venda_sem_pagamento_ativo(venda)
        validar_disponibilidade(catalogo_item, quantidade)
        venda, criada = obter_ou_criar_venda_aberta(
            terminal_bloqueado,
            operador,
            sessao_operador,
            sessao_caixa,
        )
        item = VendaItemHub.objects.select_for_update().filter(
            venda=venda,
            retaguarda_sku_id=sku_id,
        ).first()
        quantidade_anterior = item.quantidade if item else 0
        quantidade_nova = quantidade_anterior + quantidade

        if item:
            item.quantidade = quantidade_nova
            item.total_item = calcular_total_item(item.quantidade, item.preco_unitario, item.desconto)
            item.save(update_fields=["quantidade", "total_item", "atualizado_em"])
            tipo_evento = VendaEventoHub.TIPO_ITEM_QUANTIDADE_ALTERADA
        else:
            item = criar_item_venda(venda, catalogo_item, quantidade, operador, sessao_operador, terminal_bloqueado)
            tipo_evento = VendaEventoHub.TIPO_ITEM_ADICIONADO

        recalcular_totais(venda)
        registrar_evento(
            venda,
            tipo_evento,
            terminal_bloqueado,
            operador,
            sessao_operador,
            dados_item(item, quantidade_anterior, item.quantidade),
        )

    return venda, criada and quantidade_anterior == 0


def alterar_quantidade_item(terminal, operador, sessao_operador, *, item_uuid, quantidade):
    quantidade = validar_quantidade(quantidade)

    with transaction.atomic():
        terminal_bloqueado = Terminal.objects.select_for_update().select_related("hub").get(pk=terminal.pk)
        obter_sessao_caixa_terminal(terminal_bloqueado)
        venda = obter_venda_aberta_terminal(terminal_bloqueado)
        if not venda:
            raise VendaNotFoundError("Venda em andamento não encontrada.")
        validar_venda_sem_pagamento_ativo(venda)

        item = VendaItemHub.objects.select_for_update().filter(venda=venda, item_uuid=item_uuid).first()
        if not item:
            raise VendaNotFoundError("Item da venda não encontrado.")

        catalogo_item = CatalogoItemHub.objects.select_for_update().get(pk=item.catalogo_item_id)
        quantidade_anterior = item.quantidade
        delta = quantidade - quantidade_anterior
        if delta > 0:
            validar_catalogo_vendavel(catalogo_item)
            validar_disponibilidade(catalogo_item, delta)

        item.quantidade = quantidade
        item.total_item = calcular_total_item(item.quantidade, item.preco_unitario, item.desconto)
        item.save(update_fields=["quantidade", "total_item", "atualizado_em"])
        recalcular_totais(venda)
        registrar_evento(
            venda,
            VendaEventoHub.TIPO_ITEM_QUANTIDADE_ALTERADA,
            terminal_bloqueado,
            operador,
            sessao_operador,
            dados_item(item, quantidade_anterior, item.quantidade),
        )

    return venda


def remover_item(terminal, operador, sessao_operador, *, item_uuid):
    with transaction.atomic():
        terminal_bloqueado = Terminal.objects.select_for_update().select_related("hub").get(pk=terminal.pk)
        obter_sessao_caixa_terminal(terminal_bloqueado)
        venda = obter_venda_aberta_terminal(terminal_bloqueado)
        if not venda:
            raise VendaNotFoundError("Venda em andamento não encontrada.")
        validar_venda_sem_pagamento_ativo(venda)

        item = VendaItemHub.objects.select_for_update().filter(venda=venda, item_uuid=item_uuid).first()
        if not item:
            raise VendaNotFoundError("Item da venda não encontrado.")

        evento_dados = dados_item(item, item.quantidade, 0)
        item.delete()
        recalcular_totais(venda)
        registrar_evento(
            venda,
            VendaEventoHub.TIPO_ITEM_REMOVIDO,
            terminal_bloqueado,
            operador,
            sessao_operador,
            evento_dados,
        )

    return venda


def cancelar_venda(terminal, operador, sessao_operador):
    with transaction.atomic():
        terminal_bloqueado = Terminal.objects.select_for_update().select_related("hub").get(pk=terminal.pk)
        venda = obter_venda_aberta_terminal(terminal_bloqueado)
        if not venda:
            raise VendaConflictError("Venda em andamento não encontrada.")
        validar_venda_sem_pagamento_ativo(venda)

        venda.status = VendaHub.STATUS_CANCELADA
        venda.cancelada_em = timezone.now()
        venda.operador_cancelamento = operador
        venda.sessao_operador_cancelamento = sessao_operador
        venda.terminal_cancelamento = terminal_bloqueado
        venda.chave_venda_aberta_terminal = None
        venda.save(
            update_fields=[
                "status",
                "cancelada_em",
                "operador_cancelamento",
                "sessao_operador_cancelamento",
                "terminal_cancelamento",
                "chave_venda_aberta_terminal",
                "atualizada_em",
            ]
        )
        registrar_evento(
            venda,
            VendaEventoHub.TIPO_VENDA_CANCELADA,
            terminal_bloqueado,
            operador,
            sessao_operador,
            {},
        )

    return venda


def listar_formas_pagamento(terminal):
    parcelas_ordenadas = FormaPagamentoParcelaHub.objects.order_by("ordem", "id")
    formas = (
        FormaPagamentoHub.objects.filter(hub=terminal.hub, ativo=True)
        .prefetch_related(Prefetch("parcelas", queryset=parcelas_ordenadas))
        .order_by("codigo", "retaguarda_id")
    )
    return {
        "versao": terminal.hub.formas_pagamento_versao,
        "sincronizado_em": (
            terminal.hub.formas_pagamento_sincronizado_em.isoformat()
            if terminal.hub.formas_pagamento_sincronizado_em
            else None
        ),
        "formas": [serializar_forma_pagamento(forma) for forma in formas],
    }


def adicionar_pagamento(
    terminal,
    operador,
    sessao_operador,
    *,
    venda_uuid,
    operacao_uuid,
    forma_pagamento_id,
    valor,
    autorizacao="",
):
    venda_uuid = validar_uuid_obrigatorio(venda_uuid, "Venda inválida.")
    operacao_uuid = validar_uuid_obrigatorio(operacao_uuid, "Operação inválida.")
    forma_pagamento_id = validar_inteiro_positivo(forma_pagamento_id, "Forma de pagamento inválida.")
    valor = validar_valor_pagamento(valor)
    autorizacao = validar_autorizacao(autorizacao)

    with transaction.atomic():
        terminal_bloqueado = Terminal.objects.select_for_update().select_related("hub").get(pk=terminal.pk)
        obter_sessao_caixa_terminal(terminal_bloqueado)
        venda = obter_venda_terminal_por_uuid_bloqueada(terminal_bloqueado, venda_uuid)
        if venda.status != VendaHub.STATUS_ABERTA:
            raise VendaConflictError("Venda não está aberta.")

        pagamento_existente = (
            VendaPagamentoHub.objects.select_for_update()
            .filter(venda=venda, operacao_uuid=operacao_uuid)
            .first()
        )
        if pagamento_existente:
            validar_retry_pagamento(
                pagamento_existente,
                forma_pagamento_id=forma_pagamento_id,
                valor=valor,
                autorizacao=autorizacao,
            )
            return venda, False

        parcelas_ordenadas = FormaPagamentoParcelaHub.objects.order_by("ordem", "id")
        forma = (
            FormaPagamentoHub.objects.select_for_update()
            .prefetch_related(Prefetch("parcelas", queryset=parcelas_ordenadas))
            .filter(pk=forma_pagamento_id, hub=terminal_bloqueado.hub, ativo=True)
            .first()
        )
        if not forma:
            raise VendaValidationError("Forma de pagamento inválida.")
        if forma.tef_habilitado:
            raise VendaConflictError("Forma de pagamento exige integração TEF.")

        total_pago_atual = calcular_total_pago(venda)
        pendente = calcular_pendente(venda, total_pago_atual)
        if pendente <= ZERO_2:
            raise VendaConflictError("Venda já está paga.")
        if tem_troco(venda, total_pago_atual):
            raise VendaConflictError("Venda já possui troco.")
        if forma.tipo != DINHEIRO and valor > pendente:
            raise VendaConflictError("Valor do pagamento excede o valor pendente.")

        pagamento = VendaPagamentoHub.objects.create(
            operacao_uuid=operacao_uuid,
            venda=venda,
            forma_pagamento=forma,
            retaguarda_forma_pagamento_id=forma.retaguarda_id,
            codigo=forma.codigo,
            descricao=forma.descricao,
            tipo=forma.tipo,
            num_parcelas=forma.num_parcelas,
            adquirente=forma.adquirente,
            conta_liquidacao_retaguarda_id=forma.conta_liquidacao_retaguarda_id,
            gera_recebivel_bancario=forma.gera_recebivel_bancario,
            prazo_credito_dias=forma.prazo_credito_dias,
            taxa_percentual=forma.taxa_percentual,
            taxa_fixa=forma.taxa_fixa,
            valor=valor,
            autorizacao=autorizacao,
            origem_captura=VendaPagamentoHub.ORIGEM_MANUAL,
            status=VendaPagamentoHub.STATUS_ATIVO,
            terminal_inclusao=terminal_bloqueado,
            operador_inclusao=operador,
            sessao_operador_inclusao=sessao_operador,
        )
        for parcela in forma.parcelas.all():
            VendaPagamentoParcelaHub.objects.create(
                pagamento=pagamento,
                ordem=parcela.ordem,
                dias=parcela.dias,
                percentual=parcela.percentual,
                valor_fixo=parcela.valor_fixo,
            )
        registrar_evento(
            venda,
            VendaEventoHub.TIPO_PAGAMENTO_ADICIONADO,
            terminal_bloqueado,
            operador,
            sessao_operador,
            dados_pagamento(pagamento),
        )

    return venda, True


def remover_pagamento(terminal, operador, sessao_operador, *, pagamento_uuid):
    pagamento_uuid = validar_uuid_obrigatorio(pagamento_uuid, "Pagamento inválido.")
    with transaction.atomic():
        terminal_bloqueado = Terminal.objects.select_for_update().select_related("hub").get(pk=terminal.pk)
        pagamento = (
            VendaPagamentoHub.objects.select_for_update()
            .select_related("venda", "venda__terminal", "venda__hub")
            .filter(pagamento_uuid=pagamento_uuid, venda__hub=terminal_bloqueado.hub)
            .first()
        )
        if not pagamento:
            raise VendaNotFoundError("Pagamento não encontrado.")
        venda = pagamento.venda
        if venda.terminal_id != terminal_bloqueado.id:
            raise VendaNotFoundError("Pagamento não encontrado.")
        if venda.status == VendaHub.STATUS_FINALIZADA:
            raise VendaConflictError("Venda finalizada não permite remover pagamento.")
        if pagamento.status == VendaPagamentoHub.STATUS_REMOVIDO:
            return venda

        pagamento.status = VendaPagamentoHub.STATUS_REMOVIDO
        pagamento.removido_em = timezone.now()
        pagamento.terminal_remocao = terminal_bloqueado
        pagamento.operador_remocao = operador
        pagamento.sessao_operador_remocao = sessao_operador
        pagamento.save(
            update_fields=[
                "status",
                "removido_em",
                "terminal_remocao",
                "operador_remocao",
                "sessao_operador_remocao",
                "atualizado_em",
            ]
        )
        registrar_evento(
            venda,
            VendaEventoHub.TIPO_PAGAMENTO_REMOVIDO,
            terminal_bloqueado,
            operador,
            sessao_operador,
            dados_pagamento(pagamento),
        )
    return venda


def finalizar_venda(terminal, operador, sessao_operador, *, venda_uuid):
    venda_uuid = validar_uuid_obrigatorio(venda_uuid, "Venda inválida.")
    with transaction.atomic():
        terminal_bloqueado = Terminal.objects.select_for_update().select_related("hub").get(pk=terminal.pk)
        venda = obter_venda_terminal_por_uuid_bloqueada(terminal_bloqueado, venda_uuid)
        if venda.status == VendaHub.STATUS_FINALIZADA:
            return venda
        if venda.status == VendaHub.STATUS_CANCELADA:
            raise VendaConflictError("Venda cancelada não pode ser finalizada.")
        sessao_caixa = obter_sessao_caixa_terminal(terminal_bloqueado)
        if venda.sessao_caixa_id != sessao_caixa.id:
            raise VendaConflictError("Venda não pertence ao Caixa aberto.")

        itens = list(
            VendaItemHub.objects.select_for_update()
            .filter(venda=venda)
            .select_related("catalogo_item")
            .order_by("catalogo_item_id", "id")
        )
        if not itens:
            raise VendaConflictError("Venda sem itens.")
        if venda.total <= ZERO_2:
            raise VendaConflictError("Venda sem itens.")

        pagamentos = list(
            VendaPagamentoHub.objects.select_for_update().filter(
                venda=venda,
                status=VendaPagamentoHub.STATUS_ATIVO,
            )
        )
        if not pagamentos:
            raise VendaConflictError("Venda sem pagamento.")

        total_pago = calcular_total_pago_lista(pagamentos)
        if total_pago < venda.total:
            raise VendaConflictError("Pagamento insuficiente.")
        if total_pago > venda.total and not any(pagamento.tipo == DINHEIRO for pagamento in pagamentos):
            raise VendaConflictError("Valor do pagamento excede o valor pendente.")

        catalogo_ids = sorted({item.catalogo_item_id for item in itens})
        catalogo_por_id = {
            item.id: item
            for item in CatalogoItemHub.objects.select_for_update().filter(id__in=catalogo_ids).order_by("id")
        }
        for item in itens:
            catalogo_item = catalogo_por_id[item.catalogo_item_id]
            disponivel = calcular_disponivel_local(catalogo_item, excluir_venda=venda)
            if Decimal(item.quantidade) > disponivel:
                raise SaldoInsuficienteError("Saldo disponível insuficiente.", disponivel)

        for item in itens:
            EstoqueMovimentoHub.objects.get_or_create(
                venda_item=item,
                defaults={
                    "hub": venda.hub,
                    "venda": venda,
                    "catalogo_item": catalogo_por_id[item.catalogo_item_id],
                    "retaguarda_produto_id": item.retaguarda_produto_id,
                    "retaguarda_sku_id": item.retaguarda_sku_id,
                    "ean13": item.ean13,
                    "referencia": item.referencia,
                    "tipo": EstoqueMovimentoHub.TIPO_SAIDA_VENDA,
                    "quantidade": Decimal(item.quantidade).quantize(QUANTIDADE_ESTOQUE),
                },
            )

        agora = timezone.now()
        troco = calcular_troco(venda, total_pago)
        venda.status = VendaHub.STATUS_FINALIZADA
        venda.finalizada_em = agora
        venda.terminal_finalizacao = terminal_bloqueado
        venda.operador_finalizacao = operador
        venda.sessao_operador_finalizacao = sessao_operador
        venda.valor_recebido = total_pago
        venda.troco = troco
        venda.chave_venda_aberta_terminal = None
        venda.save(
            update_fields=[
                "status",
                "finalizada_em",
                "terminal_finalizacao",
                "operador_finalizacao",
                "sessao_operador_finalizacao",
                "valor_recebido",
                "troco",
                "chave_venda_aberta_terminal",
                "atualizada_em",
            ]
        )
        if not VendaEventoHub.objects.filter(venda=venda, tipo=VendaEventoHub.TIPO_VENDA_FINALIZADA).exists():
            registrar_evento(
                venda,
                VendaEventoHub.TIPO_VENDA_FINALIZADA,
                terminal_bloqueado,
                operador,
                sessao_operador,
                {
                    "valor_recebido": f"{total_pago:.2f}",
                    "troco": f"{troco:.2f}",
                },
            )

    return venda


def existe_venda_aberta_sessao_caixa(sessao_caixa):
    return VendaHub.objects.filter(
        sessao_caixa=sessao_caixa,
        status=VendaHub.STATUS_ABERTA,
    ).exists()


def obter_ou_criar_venda_aberta(terminal, operador, sessao_operador, sessao_caixa):
    venda = VendaHub.objects.select_for_update().filter(
        hub=terminal.hub,
        terminal=terminal,
        status=VendaHub.STATUS_ABERTA,
    ).first()
    if venda:
        return venda, False

    venda = VendaHub(
        hub=terminal.hub,
        sessao_caixa=sessao_caixa,
        terminal=terminal,
        status=VendaHub.STATUS_ABERTA,
        chave_venda_aberta_terminal=terminal.pk,
        operador_criacao=operador,
        sessao_operador_criacao=sessao_operador,
        subtotal=ZERO_2,
        desconto_itens=ZERO_2,
        desconto_geral=ZERO_2,
        total=ZERO_2,
    )
    try:
        with transaction.atomic():
            venda.save()
    except IntegrityError as exc:
        venda = VendaHub.objects.select_for_update().filter(
            hub=terminal.hub,
            terminal=terminal,
            status=VendaHub.STATUS_ABERTA,
        ).first()
        if venda:
            return venda, False
        raise exc

    registrar_evento(
        venda,
        VendaEventoHub.TIPO_VENDA_CRIADA,
        terminal,
        operador,
        sessao_operador,
        {},
    )
    return venda, True


def obter_catalogo_item_bloqueado(hub, sku_id):
    try:
        return CatalogoItemHub.objects.select_for_update().get(hub=hub, retaguarda_sku_id=sku_id)
    except CatalogoItemHub.DoesNotExist as exc:
        raise VendaValidationError("Produto não encontrado no catálogo local.") from exc


def validar_catalogo_vendavel(item):
    if not item.ativo:
        raise VendaConflictError("Produto inativo no catálogo local.")
    if not item.vendavel:
        raise VendaConflictError("Produto não vendável.")
    if item.preco_venda is None:
        raise VendaConflictError("Produto sem preço de venda.")


def validar_quantidade(quantidade):
    valor = validar_inteiro_positivo(quantidade, "Quantidade inválida.")
    return valor


def validar_sku_id(sku_id):
    valor = validar_inteiro_positivo(sku_id, "Produto inválido.")
    return valor


def validar_inteiro_positivo(valor, mensagem):
    if valor is None or isinstance(valor, bool):
        raise VendaValidationError(mensagem)
    if isinstance(valor, int):
        inteiro = valor
    elif isinstance(valor, str) and valor.isdecimal():
        inteiro = int(valor)
    else:
        raise VendaValidationError(mensagem)

    if inteiro <= 0:
        raise VendaValidationError(mensagem)
    return inteiro


def validar_disponibilidade(catalogo_item, quantidade_adicional):
    disponivel = calcular_disponivel_local(catalogo_item)
    if Decimal(quantidade_adicional) > disponivel:
        raise SaldoInsuficienteError("Saldo disponível insuficiente.", disponivel)


def calcular_disponivel_local(catalogo_item, *, excluir_venda=None):
    reservas = calcular_reserva_sku(
        catalogo_item.hub,
        catalogo_item.retaguarda_sku_id,
        excluir_venda=excluir_venda,
    )
    movimentos = calcular_movimento_sku(catalogo_item.hub, catalogo_item.retaguarda_sku_id)
    return catalogo_item.estoque_disponivel - movimentos - reservas


def calcular_reserva_sku(hub, sku_id, *, excluir_venda=None):
    queryset = VendaItemHub.objects.filter(
        venda__hub=hub,
        venda__status=VendaHub.STATUS_ABERTA,
        retaguarda_sku_id=sku_id,
    )
    if excluir_venda is not None:
        queryset = queryset.exclude(venda=excluir_venda)
    total = queryset.aggregate(total=Sum("quantidade")).get("total") or 0
    return Decimal(total)


def calcular_movimento_sku(hub, sku_id):
    total = (
        EstoqueMovimentoHub.objects.filter(
            hub=hub,
            retaguarda_sku_id=sku_id,
            reconciliado_em__isnull=True,
        ).aggregate(total=Sum("quantidade")).get("total")
        or Decimal("0.000")
    )
    return Decimal(total)


def criar_item_venda(venda, catalogo_item, quantidade, operador, sessao_operador, terminal):
    return VendaItemHub.objects.create(
        venda=venda,
        catalogo_item=catalogo_item,
        retaguarda_produto_id=catalogo_item.retaguarda_produto_id,
        retaguarda_sku_id=catalogo_item.retaguarda_sku_id,
        ean13=catalogo_item.ean13,
        referencia=catalogo_item.referencia,
        codigo_item_ref=catalogo_item.codigo_item_ref,
        descricao=catalogo_item.descricao,
        descricao_reduzida=catalogo_item.descricao_reduzida,
        cor_descricao=catalogo_item.cor_descricao,
        tamanho_descricao=catalogo_item.tamanho_descricao,
        unidade_codigo=catalogo_item.unidade_codigo,
        quantidade=quantidade,
        preco_unitario=catalogo_item.preco_venda,
        desconto=ZERO_2,
        total_item=calcular_total_item(quantidade, catalogo_item.preco_venda, ZERO_2),
        operador_inclusao=operador,
        sessao_operador_inclusao=sessao_operador,
        terminal_inclusao=terminal,
    )


def calcular_total_item(quantidade, preco_unitario, desconto):
    total = Decimal(quantidade) * preco_unitario - desconto
    return money(total)


def money(valor):
    return Decimal(valor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def recalcular_totais(venda):
    subtotal = venda.itens.aggregate(total=Sum("total_item")).get("total") or ZERO_2
    venda.subtotal = money(subtotal)
    venda.desconto_itens = ZERO_2
    venda.desconto_geral = ZERO_2
    venda.total = venda.subtotal
    venda.save(update_fields=["subtotal", "desconto_itens", "desconto_geral", "total", "atualizada_em"])


def validar_venda_sem_pagamento_ativo(venda):
    if venda and venda.pagamentos.filter(status=VendaPagamentoHub.STATUS_ATIVO).exists():
        raise VendaConflictError(ERRO_PAGAMENTO_ALTERAR_VENDA)


def validar_uuid_obrigatorio(valor, mensagem):
    if valor in (None, "") or isinstance(valor, bool):
        raise VendaValidationError(mensagem)
    try:
        return uuid.UUID(str(valor))
    except (TypeError, ValueError) as exc:
        raise VendaValidationError(mensagem) from exc


def validar_valor_pagamento(valor):
    if not isinstance(valor, str) or not re.fullmatch(r"^\d+\.\d{2}$", valor):
        raise VendaValidationError("Valor do pagamento inválido.")
    try:
        decimal = Decimal(valor)
    except InvalidOperation as exc:
        raise VendaValidationError("Valor do pagamento inválido.") from exc
    if not decimal.is_finite() or decimal <= ZERO_2 or decimal > VALOR_PAGAMENTO_MAXIMO:
        raise VendaValidationError("Valor do pagamento inválido.")
    return money(decimal)


def validar_autorizacao(valor):
    if valor is None:
        return ""
    if not isinstance(valor, str):
        raise VendaValidationError("Autorização inválida.")
    texto = valor.strip()
    if len(texto) > 120:
        raise VendaValidationError("Autorização inválida.")
    return texto


def validar_retry_pagamento(pagamento, *, forma_pagamento_id, valor, autorizacao):
    if (
        pagamento.forma_pagamento_id != forma_pagamento_id
        or pagamento.valor != valor
        or pagamento.autorizacao != autorizacao
    ):
        raise VendaConflictError(ERRO_OPERACAO_PAGAMENTO_DIVERGENTE)


def obter_venda_terminal_por_uuid_bloqueada(terminal, venda_uuid):
    venda = (
        VendaHub.objects.select_for_update()
        .select_related("hub", "terminal", "sessao_caixa", "operador_criacao")
        .filter(venda_uuid=venda_uuid, hub=terminal.hub, terminal=terminal)
        .first()
    )
    if not venda:
        raise VendaNotFoundError("Venda não encontrada.")
    return venda


def calcular_total_pago(venda):
    total = (
        venda.pagamentos.filter(status=VendaPagamentoHub.STATUS_ATIVO)
        .aggregate(total=Sum("valor"))
        .get("total")
        or ZERO_2
    )
    return money(total)


def calcular_total_pago_lista(pagamentos):
    total = sum((pagamento.valor for pagamento in pagamentos), ZERO_2)
    return money(total)


def calcular_pendente(venda, total_pago=None):
    if total_pago is None:
        total_pago = calcular_total_pago(venda)
    pendente = venda.total - total_pago
    return money(max(pendente, ZERO_2))


def calcular_troco(venda, total_pago=None):
    if total_pago is None:
        total_pago = calcular_total_pago(venda)
    troco = total_pago - venda.total
    return money(max(troco, ZERO_2))


def tem_troco(venda, total_pago=None):
    return calcular_troco(venda, total_pago) > ZERO_2


def registrar_evento(venda, tipo, terminal, operador, sessao_operador, dados):
    VendaEventoHub.objects.create(
        venda=venda,
        tipo=tipo,
        terminal=terminal,
        operador=operador,
        sessao_operador=sessao_operador,
        dados=dados,
    )


def dados_item(item, quantidade_anterior, quantidade_nova):
    return {
        "sku_id": item.retaguarda_sku_id,
        "item_uuid": str(item.item_uuid),
        "quantidade_anterior": quantidade_anterior,
        "quantidade_nova": quantidade_nova,
        "preco_unitario": f"{item.preco_unitario:.4f}",
        "total_item": f"{item.total_item:.2f}",
    }


def dados_pagamento(pagamento):
    return {
        "pagamento_uuid": str(pagamento.pagamento_uuid),
        "forma_id": pagamento.forma_pagamento_id,
        "codigo": pagamento.codigo,
        "tipo": pagamento.tipo,
        "valor": f"{pagamento.valor:.2f}",
        "status": pagamento.status,
    }


def serializar_venda(venda):
    if venda is None:
        return None

    itens = venda.itens.select_related("catalogo_item").order_by("criado_em", "id")
    pagamentos = venda.pagamentos.filter(status=VendaPagamentoHub.STATUS_ATIVO).order_by("criado_em", "id")
    total_pago = calcular_total_pago(venda)
    pendente = calcular_pendente(venda, total_pago)
    troco = venda.troco if venda.status == VendaHub.STATUS_FINALIZADA else calcular_troco(venda, total_pago)
    return {
        "uuid": str(venda.venda_uuid),
        "status": venda.status,
        "criada_em": venda.criada_em.isoformat(),
        "subtotal": f"{venda.subtotal:.2f}",
        "desconto_itens": f"{venda.desconto_itens:.2f}",
        "desconto_geral": f"{venda.desconto_geral:.2f}",
        "total": f"{venda.total:.2f}",
        "total_pago": f"{total_pago:.2f}",
        "pendente": f"{pendente:.2f}",
        "troco": f"{troco:.2f}",
        "operador_criacao": serializar_operador(venda.operador_criacao),
        "itens": [serializar_item(item) for item in itens],
        "pagamentos": [serializar_pagamento(pagamento) for pagamento in pagamentos],
    }


def serializar_item(item):
    return {
        "uuid": str(item.item_uuid),
        "produto_id": item.retaguarda_produto_id,
        "sku_id": item.retaguarda_sku_id,
        "ean13": item.ean13 or None,
        "referencia": item.referencia,
        "codigo_item_ref": item.codigo_item_ref,
        "descricao": item.descricao,
        "descricao_reduzida": item.descricao_reduzida,
        "cor": item.cor_descricao,
        "tamanho": item.tamanho_descricao,
        "unidade": item.unidade_codigo,
        "quantidade": item.quantidade,
        "preco_unitario": f"{item.preco_unitario:.4f}",
        "desconto": f"{item.desconto:.2f}",
        "total_item": f"{item.total_item:.2f}",
    }


def serializar_pagamento(pagamento):
    return {
        "uuid": str(pagamento.pagamento_uuid),
        "forma_pagamento_id": pagamento.forma_pagamento_id,
        "forma_retaguarda_id": pagamento.retaguarda_forma_pagamento_id,
        "codigo": pagamento.codigo,
        "descricao": pagamento.descricao,
        "tipo": pagamento.tipo,
        "num_parcelas": pagamento.num_parcelas,
        "valor": f"{pagamento.valor:.2f}",
        "autorizacao": pagamento.autorizacao,
        "origem_captura": pagamento.origem_captura,
        "criado_em": pagamento.criado_em.isoformat(),
    }


def serializar_forma_pagamento(forma):
    parcelas = forma.parcelas.all()
    return {
        "id": forma.id,
        "retaguarda_id": forma.retaguarda_id,
        "codigo": forma.codigo,
        "descricao": forma.descricao,
        "tipo": forma.tipo,
        "num_parcelas": forma.num_parcelas,
        "tef_habilitado": forma.tef_habilitado,
        "parcelas": [
            {
                "ordem": parcela.ordem,
                "dias": parcela.dias,
                "percentual": f"{parcela.percentual:.6f}" if parcela.percentual is not None else None,
                "valor_fixo": f"{parcela.valor_fixo:.2f}" if parcela.valor_fixo is not None else None,
            }
            for parcela in parcelas
        ],
    }
