from decimal import Decimal, ROUND_HALF_UP

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from core.models import CatalogoItemHub, SessaoCaixaHub, Terminal, VendaEventoHub, VendaHub, VendaItemHub
from core.services.caixa import obter_caixa_terminal, obter_sessao_caixa_aberta
from core.services.operadores import serializar_operador


ZERO_2 = Decimal("0.00")
QUANTIDADE_DECIMAL = Decimal("1")


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


def calcular_disponivel_local(catalogo_item):
    reservas = calcular_reserva_sku(catalogo_item.hub, catalogo_item.retaguarda_sku_id)
    return catalogo_item.estoque_disponivel - reservas


def calcular_reserva_sku(hub, sku_id):
    queryset = VendaItemHub.objects.filter(
        venda__hub=hub,
        venda__status=VendaHub.STATUS_ABERTA,
        retaguarda_sku_id=sku_id,
    )
    total = queryset.aggregate(total=Sum("quantidade")).get("total") or 0
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


def serializar_venda(venda):
    if venda is None:
        return None

    itens = venda.itens.select_related("catalogo_item").order_by("criado_em", "id")
    return {
        "uuid": str(venda.venda_uuid),
        "status": venda.status,
        "criada_em": venda.criada_em.isoformat(),
        "subtotal": f"{venda.subtotal:.2f}",
        "desconto_itens": f"{venda.desconto_itens:.2f}",
        "desconto_geral": f"{venda.desconto_geral:.2f}",
        "total": f"{venda.total:.2f}",
        "operador_criacao": serializar_operador(venda.operador_criacao),
        "itens": [serializar_item(item) for item in itens],
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
