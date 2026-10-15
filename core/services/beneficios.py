import uuid
from datetime import timedelta
from decimal import Decimal, ROUND_DOWN

from django.db import models, transaction
from django.db.models import Case, DecimalField, Sum, Value, When
from django.utils import timezone

from core.models import CashbackConfigHub, CashbackMovimentoHub, EventoSyncHub, PromocaoHub, ValeTrocaHub, ValeTrocaMovimentoHub, VendaDevolucaoHub


ZERO = Decimal("0.00")
ERRO_CASHBACK_VALE_TROCA = (
    "Cashback não pode ser usado junto com Vale-Troca. "
    "Realize a venda com Cashback separadamente da troca."
)
TIPOS_VALE_TROCA = {"TROCA", "VALE_TROCA"}


def money(valor):
    return Decimal(valor).quantize(Decimal("0.01"))


def cliente_padrao(venda):
    documento = "".join(ch for ch in str(venda.cliente_documento or "") if ch.isdigit())
    return venda.cliente_padrao or documento in ("", "00000000000")


def cashback_config_ativa(hub):
    return CashbackConfigHub.objects.filter(hub=hub, ativo=True).order_by("retaguarda_id").first()


def saldo_cashback(venda):
    return saldo_cashback_offline(venda)


def saldo_cashback_offline(venda, *, bloquear=False):
    if not (venda.cliente_retaguarda_id or venda.cliente_uuid):
        return ZERO
    hoje = timezone.localdate()
    qs = CashbackMovimentoHub.objects.filter(
        hub=venda.hub,
        status=CashbackMovimentoHub.STATUS_ATIVO,
        centralizado_em__isnull=True,
    )
    qs = _filtrar_cliente(qs, venda)
    qs_validos = qs.filter(models.Q(validade__isnull=True) | models.Q(validade__gte=hoje))
    if bloquear:
        list(qs_validos.select_for_update().order_by("id").values_list("id", flat=True))
    return money(
        qs_validos.aggregate(
            saldo=Sum(
                Case(
                    When(tipo=CashbackMovimentoHub.TIPO_CREDITO, then="valor"),
                    default=Value(Decimal("0.00")) - models.F("valor"),
                    output_field=DecimalField(max_digits=18, decimal_places=2),
                )
            )
        )
        .get("saldo")
        or ZERO
    )


def vales_abertos(venda, *, bloquear=False, apenas_utilizaveis_offline=False):
    hoje = timezone.localdate()
    qs = ValeTrocaHub.objects.filter(
        hub=venda.hub,
        status=ValeTrocaHub.STATUS_ABERTO,
        saldo__gt=0,
    )
    if bloquear:
        qs = qs.select_for_update()
    if apenas_utilizaveis_offline:
        qs = qs.filter(
            sincronizado_em__isnull=True, provisorio=True,
            devolucao__hub=venda.hub,
            devolucao__origem=VendaDevolucaoHub.ORIGEM_VENDA_LOCAL,
            devolucao__venda_origem__hub=venda.hub,
            devolucao__venda_troca_id=getattr(venda, "pk", None),
        ).exclude(devolucao__venda_troca__isnull=True)
    qs = _filtrar_cliente(qs, venda)
    return qs.filter(models.Q(validade__isnull=True) | models.Q(validade__gte=hoje)).order_by("criado_em", "id")


def saldo_vale_troca(venda):
    if not (venda.cliente_retaguarda_id or venda.cliente_uuid):
        return ZERO
    return money(vales_abertos(venda, apenas_utilizaveis_offline=True).aggregate(total=Sum("saldo")).get("total") or ZERO)


def validar_pagamento_beneficio(venda, forma, valor, autorizacao="", tipo=None, total_beneficio=None, bloquear_saldo=False, devolucao_uuid=None):
    tipo = (tipo or getattr(forma, "tipo", "") or "").upper()
    if tipo == "CASHBACK":
        return validar_cashback(venda, valor, total_cashback=total_beneficio, bloquear_saldo=bloquear_saldo)
    if tipo in ("TROCA", "VALE_TROCA"):
        vincular_credito_provisorio(venda, autorizacao, devolucao_uuid)
        return validar_vale_troca(venda, valor, autorizacao)
    return None


def validar_cashback(venda, valor, *, total_cashback=None, bloquear_saldo=False):
    config = cashback_config_ativa(venda.hub)
    valor = money(valor)
    total_cashback = money(total_cashback if total_cashback is not None else total_cashback_ativo(venda) + valor)
    total_cashback_local = money(total_cashback_local_ativo(venda) + valor)
    validar_cashback_vale_troca_exclusivos(venda, novo_tipo="CASHBACK")
    if not config:
        raise ValueError("Não existe regra de cashback ativa.")
    if cliente_padrao(venda) and not config.consumidor_final_participa:
        raise ValueError("Cashback exige cliente identificado.")
    if total_cashback_local > saldo_cashback_offline(venda, bloquear=bloquear_saldo):
        raise ValueError("O cashback informado é maior que o saldo disponível do cliente.")
    if total_cashback > money(venda.total):
        raise ValueError("O cashback não pode ser maior que o total da venda.")
    outros = _total_pago_outros(venda, "CASHBACK")
    if total_cashback > money(max(ZERO, venda.total - outros)):
        raise ValueError("Cashback não pode gerar troco; use apenas o saldo pendente da venda.")
    if valor > ZERO and config.valor_minimo_uso and valor < money(config.valor_minimo_uso):
        raise ValueError("O valor de cashback usado é menor que o mínimo configurado.")
    limite = money(venda.total * Decimal(config.limite_uso_percentual or 0) / Decimal("100"))
    if total_cashback > limite:
        raise ValueError("O cashback usado ultrapassa o limite permitido para a venda.")


def total_cashback_ativo(venda):
    return money(
        venda.pagamentos.filter(status="ATIVO", tipo="CASHBACK")
        .aggregate(total=models.Sum("valor"))
        .get("total")
        or ZERO
    )


def total_cashback_local_ativo(venda):
    return money(
        venda.pagamentos.filter(
            status="ATIVO",
            tipo="CASHBACK",
            cashback_origem="LOCAL",
        )
        .aggregate(total=models.Sum("valor"))
        .get("total")
        or ZERO
    )


def total_cashback_central_ativo(venda):
    return money(
        venda.pagamentos.filter(
            status="ATIVO",
            tipo="CASHBACK",
            cashback_origem="CENTRAL",
        )
        .aggregate(total=models.Sum("valor"))
        .get("total")
        or ZERO
    )


def validar_cashback_central(venda, valor):
    config = cashback_config_ativa(venda.hub)
    valor = money(valor)
    total_cashback = money(total_cashback_ativo(venda) + valor)
    validar_cashback_vale_troca_exclusivos(venda, novo_tipo="CASHBACK")
    if not config:
        raise ValueError("Não existe regra de cashback ativa.")
    if cliente_padrao(venda) and not config.consumidor_final_participa:
        raise ValueError("Cashback exige cliente identificado.")
    if total_cashback > money(venda.total):
        raise ValueError("O cashback não pode ser maior que o total da venda.")
    outros = _total_pago_outros(venda, "CASHBACK")
    if total_cashback > money(max(ZERO, venda.total - outros)):
        raise ValueError("Cashback não pode gerar troco; use apenas o saldo pendente da venda.")
    if config.valor_minimo_uso and valor < money(config.valor_minimo_uso):
        raise ValueError("O valor de cashback usado é menor que o mínimo configurado.")
    limite = money(venda.total * Decimal(config.limite_uso_percentual or 0) / Decimal("100"))
    if total_cashback > limite:
        raise ValueError("O cashback usado ultrapassa o limite permitido para a venda.")


def vincular_credito_provisorio(venda, documento, devolucao_uuid):
    documento = str(documento or "").strip().upper()
    vale = ValeTrocaHub.objects.filter(hub=venda.hub, documento=documento, provisorio=True).first()
    if not vale:
        raise ValueError("Crédito provisório inválido. Vales oficiais exigem validação online na Central.")
    try:
        identificador = uuid.UUID(str(devolucao_uuid))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Informe o UUID da devolução vinculada à troca.") from exc
    evento = EventoSyncHub.objects.select_for_update().filter(
        hub=venda.hub, chave_idempotencia=f"DEVOLUCAO:{identificador}:FINALIZADA",
    ).first()
    if evento and (evento.tentativas or evento.status != EventoSyncHub.STATUS_PENDENTE):
        raise ValueError("Devolução já enviada à Central. Aguarde o Vale-Troca oficial para realizar a troca online.")
    devolucao = VendaDevolucaoHub.objects.select_for_update().filter(
        pk=vale.devolucao_id, devolucao_uuid=identificador, hub=venda.hub,
        origem=VendaDevolucaoHub.ORIGEM_VENDA_LOCAL,
        status=VendaDevolucaoHub.STATUS_FINALIZADA,
        venda_origem__hub=venda.hub, venda_origem__status="FINALIZADA",
    ).first()
    if not devolucao or vale.origem_devolucao_uuid != identificador:
        raise ValueError("Crédito não pertence à devolução local informada.")
    if devolucao.confirmado_central_em:
        raise ValueError("Devolução já enviada à Central. Aguarde o Vale-Troca oficial para realizar a troca online.")
    if venda.status != "ABERTA" or venda.pk == devolucao.venda_origem_id:
        raise ValueError("Venda de troca inválida.")
    for registro in (devolucao, vale, devolucao.venda_origem):
        if (registro.cliente_uuid and registro.cliente_uuid != venda.cliente_uuid) or (
            registro.cliente_retaguarda_id and registro.cliente_retaguarda_id != venda.cliente_retaguarda_id
        ) or not (registro.cliente_uuid or registro.cliente_retaguarda_id):
            raise ValueError("Crédito provisório pertence a outro cliente.")
    if devolucao.venda_troca_id and devolucao.venda_troca_id != venda.pk:
        raise ValueError("Crédito provisório já vinculado a outra venda de troca.")
    if ValeTrocaMovimentoHub.objects.filter(vale=vale, tipo=ValeTrocaMovimentoHub.TIPO_USO).exclude(venda=venda).exists():
        raise ValueError("Crédito provisório já utilizado em outra venda.")
    if not devolucao.venda_troca_id:
        devolucao.venda_troca = venda
        devolucao.save(update_fields=["venda_troca"])
    return vale


def validar_vale_troca(venda, valor, autorizacao=""):
    validar_cashback_vale_troca_exclusivos(venda, novo_tipo="VALE_TROCA")
    if cliente_padrao(venda):
        raise ValueError("Troca exige cliente identificado.")
    documento = str(autorizacao or "").strip()
    if not documento:
        raise ValueError("Selecione um cupom de troca válido para o pagamento.")
    vale = vales_abertos(venda, bloquear=True, apenas_utilizaveis_offline=True).filter(documento=documento).first()
    if not vale:
        raise ValueError("Cupom de troca inválido para este cliente.")
    if money(valor) > money(vale.saldo):
        raise ValueError("O valor informado é maior que o saldo do cupom de troca selecionado.")
    if venda.pagamentos.filter(status="ATIVO", vale_troca_documento=documento).exists():
        raise ValueError("O mesmo crédito provisório não pode ser usado duas vezes na venda.")
    outros = _total_pago_outros(venda, "TROCA", "VALE_TROCA")
    if money(valor) > money(max(ZERO, venda.total - outros)):
        raise ValueError("Troca não pode gerar troco; use apenas o saldo pendente da venda.")


def aplicar_promocao(catalogo_item, quantidade):
    agora = timezone.now()
    candidatas = (
        PromocaoHub.objects.filter(
            hub=catalogo_item.hub,
            ativo=True,
        )
        .filter(models.Q(inicio__isnull=True) | models.Q(inicio__lte=agora))
        .filter(models.Q(fim__isnull=True) | models.Q(fim__gte=agora))
        .order_by("prioridade", "retaguarda_id")
    )
    promocao = next((p for p in candidatas if _promocao_aplica(p, catalogo_item)), None)
    preco = Decimal(catalogo_item.preco_venda)
    desconto = ZERO
    if not promocao:
        return preco, desconto, {}
    if promocao.tipo == PromocaoHub.TIPO_PERCENTUAL:
        desconto = money(preco * Decimal(quantidade) * Decimal(promocao.valor) / Decimal("100"))
    elif promocao.tipo == PromocaoHub.TIPO_VALOR_FIXO:
        desconto = money(promocao.valor) * Decimal(quantidade)
    elif promocao.tipo == PromocaoHub.TIPO_PRECO_FIXO:
        desconto = money(max(ZERO, (preco - Decimal(promocao.valor)) * Decimal(quantidade)))
    # O teto em centavos não pode arredondar para cima o bruto de quatro casas.
    bruto = (preco * Decimal(quantidade)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    desconto = min(money(desconto), bruto)
    return preco, desconto, {
        "promocao_retaguarda_id": promocao.retaguarda_id,
        "promocao_nome": promocao.nome,
        "promocao_tipo": promocao.tipo,
        "promocao_valor": promocao.valor,
        "promocao_acumula_cashback": promocao.acumula_cashback,
    }


def registrar_beneficios_venda(venda, pagamentos):
    with transaction.atomic():
        validar_cashback_vale_troca_exclusivos(venda, pagamentos=pagamentos)
        documentos_locais = [
            str(p.vale_troca_documento or p.autorizacao or "").strip().upper()
            for p in pagamentos
            if (p.tipo or "").upper() in TIPOS_VALE_TROCA and not p.vale_troca_reserva_id
        ]
        if len(documentos_locais) != len(set(documentos_locais)):
            raise ValueError("O mesmo crédito provisório não pode ser usado duas vezes na venda.")
        total_cashback_local = money(sum(
            (
                p.valor
                for p in pagamentos
                if (p.tipo or "").upper() == "CASHBACK"
                and getattr(p, "cashback_origem", "LOCAL") == "LOCAL"
            ),
            ZERO,
        ))
        if total_cashback_local > ZERO and not CashbackMovimentoHub.objects.filter(venda=venda, tipo=CashbackMovimentoHub.TIPO_DEBITO).exists():
            CashbackMovimentoHub.objects.create(
                hub=venda.hub,
                cliente_uuid=venda.cliente_uuid,
                cliente_retaguarda_id=venda.cliente_retaguarda_id,
                venda=venda,
                tipo=CashbackMovimentoHub.TIPO_DEBITO,
                valor=total_cashback_local,
                observacao=f"Uso na venda Hub {venda.venda_uuid}",
            )
        for pagamento in pagamentos:
            tipo = (pagamento.tipo or "").upper()
            if tipo in ("TROCA", "VALE_TROCA") and not getattr(pagamento, "vale_troca_reserva_id", None):
                _consumir_vale(venda, pagamento)
        _gerar_cashback(venda, pagamentos)


def _gerar_cashback(venda, pagamentos):
    config = cashback_config_ativa(venda.hub)
    if not config or (cliente_padrao(venda) and not config.consumidor_final_participa):
        return
    if any(pagamento_e_vale_troca(pagamento) for pagamento in pagamentos):
        return
    usado = sum((p.valor for p in pagamentos if (p.tipo or "").upper() == "CASHBACK"), ZERO)
    base_itens = sum(
        (item.total_item for item in venda.itens.all() if item.promocao_acumula_cashback),
        ZERO,
    )
    base = money(max(ZERO, base_itens - usado))
    if base < money(config.valor_minimo_geracao):
        return
    credito = money(base * Decimal(config.percentual or 0) / Decimal("100"))
    if credito <= ZERO or CashbackMovimentoHub.objects.filter(venda=venda, tipo=CashbackMovimentoHub.TIPO_CREDITO).exists():
        return
    CashbackMovimentoHub.objects.create(
        hub=venda.hub,
        cliente_uuid=venda.cliente_uuid,
        cliente_retaguarda_id=venda.cliente_retaguarda_id,
        venda=venda,
        tipo=CashbackMovimentoHub.TIPO_CREDITO,
        valor=credito,
        validade=timezone.localdate() + timedelta(days=int(config.validade_dias or 0)),
        observacao=f"Crédito gerado pela venda Hub {venda.venda_uuid}",
    )


def _consumir_vale(venda, pagamento):
    restante = money(pagamento.valor)
    documento = str(pagamento.vale_troca_documento or pagamento.autorizacao or "").strip()
    if not documento:
        raise ValueError("Selecione um cupom de troca válido para o pagamento.")
    devolucao = venda.devolucoes_troca.filter(vale_troca__documento=documento).first()
    vincular_credito_provisorio(venda, documento, devolucao.devolucao_uuid if devolucao else None)
    if ValeTrocaMovimentoHub.objects.filter(
        vale__hub=venda.hub, vale__documento=documento, venda=venda,
        tipo=ValeTrocaMovimentoHub.TIPO_USO,
    ).exists():
        return
    vale = vales_abertos(venda, bloquear=True, apenas_utilizaveis_offline=True).filter(documento=documento).first()
    if not vale:
        raise ValueError("Cupom de troca inválido para este cliente.")
    if restante > money(vale.saldo):
        raise ValueError("Saldo de troca insuficiente para concluir a venda.")
    outros = _total_pago_outros(venda, "TROCA", "VALE_TROCA")
    if restante > money(max(ZERO, venda.total - outros)):
        raise ValueError("Troca não pode gerar troco; use apenas o saldo pendente da venda.")
    vale.saldo = money(vale.saldo - restante)
    if vale.saldo <= ZERO:
        vale.status = ValeTrocaHub.STATUS_USADO
    vale.save(update_fields=["saldo", "status", "atualizado_em"])
    ValeTrocaMovimentoHub.objects.create(
        vale=vale,
        venda=venda,
        tipo=ValeTrocaMovimentoHub.TIPO_USO,
        valor=restante,
        saldo_apos=vale.saldo,
        observacao=f"Uso na venda Hub {venda.venda_uuid}",
    )


def _filtrar_cliente(qs, venda):
    if venda.cliente_retaguarda_id:
        return qs.filter(cliente_retaguarda_id=venda.cliente_retaguarda_id)
    return qs.filter(cliente_uuid=venda.cliente_uuid)


def pagamento_e_cashback(pagamento):
    return (getattr(pagamento, "tipo", "") or "").upper() == "CASHBACK"


def pagamento_e_vale_troca(pagamento):
    tipo = (getattr(pagamento, "tipo", "") or "").upper()
    return tipo in TIPOS_VALE_TROCA or bool(
        getattr(pagamento, "vale_troca_documento", "")
        or getattr(pagamento, "vale_troca_reserva_id", None)
    )


def validar_cashback_vale_troca_exclusivos(venda, *, pagamentos=None, novo_tipo=None):
    novo_tipo = (novo_tipo or "").upper()
    if pagamentos is None:
        pagamentos = list(venda.pagamentos.filter(status="ATIVO"))
    tem_cashback = any(pagamento_e_cashback(pagamento) for pagamento in pagamentos)
    tem_vale_troca = any(pagamento_e_vale_troca(pagamento) for pagamento in pagamentos)
    if novo_tipo == "CASHBACK":
        tem_cashback = True
    if novo_tipo in TIPOS_VALE_TROCA:
        tem_vale_troca = True
    if tem_cashback and tem_vale_troca:
        raise ValueError(ERRO_CASHBACK_VALE_TROCA)


def _total_pago_outros(venda, *tipos_excluidos):
    tipos = {tipo.upper() for tipo in tipos_excluidos}
    return money(
        venda.pagamentos.filter(status="ATIVO")
        .exclude(tipo__in=tipos)
        .aggregate(total=models.Sum("valor"))
        .get("total")
        or ZERO
    )


def _promocao_aplica(promocao, item):
    if promocao.escopo == PromocaoHub.ESCOPO_TODOS:
        return True
    if promocao.escopo == PromocaoHub.ESCOPO_PRODUTO:
        return item.retaguarda_produto_id in set(promocao.produto_ids or []) or promocao.produto_retaguarda_id == item.retaguarda_produto_id
    if promocao.escopo == PromocaoHub.ESCOPO_COLECAO:
        return bool(item.colecao_retaguarda_id and item.colecao_retaguarda_id in set(promocao.colecao_ids or []))
    if promocao.escopo == PromocaoHub.ESCOPO_GRUPO:
        return bool(item.grupo_retaguarda_id and item.grupo_retaguarda_id in set(promocao.grupo_ids or []))
    if promocao.escopo == PromocaoHub.ESCOPO_SUBGRUPO:
        return bool(item.subgrupo_retaguarda_id and item.subgrupo_retaguarda_id in set(promocao.subgrupo_ids or []))
    return False


def consultar_beneficios_cliente(hub, cliente_uuid):
    from core.models import ClienteHub

    cliente = ClienteHub.objects.filter(hub=hub, cliente_uuid=cliente_uuid, ativo=True, bloqueio=False).first()
    if not cliente:
        return {
            "cashback": {
                "saldo": "0.00",
                "saldo_retaguarda": "0.00",
                "saldo_offline_utilizavel": "0.00",
                "limite_uso_percentual": "0.0000",
                "valor_minimo_uso": "0.00",
            },
            "vales_troca": [],
        }
    base = type("VendaCliente", (), {
        "hub": hub,
        "cliente_uuid": cliente.cliente_uuid,
        "cliente_retaguarda_id": cliente.retaguarda_id,
        "cliente_padrao": cliente.cliente_padrao,
        "cliente_documento": cliente.documento,
    })()
    config = cashback_config_ativa(hub)
    saldo_offline = saldo_cashback_offline(base)
    vales = vales_abertos(base).filter(provisorio=False)
    return {
        "cashback": {
            "saldo": f"{saldo_offline:.2f}",
            "saldo_retaguarda": f"{cliente.cashback_saldo_retaguarda:.2f}",
            "saldo_offline_utilizavel": f"{saldo_offline:.2f}",
            "limite_uso_percentual": f"{config.limite_uso_percentual:.4f}" if config else "0.0000",
            "valor_minimo_uso": f"{config.valor_minimo_uso:.2f}" if config else "0.00",
        },
        "vales_troca": [
            {
                "documento": vale.documento,
                "saldo": f"{vale.saldo:.2f}",
                "validade": vale.validade.isoformat() if vale.validade else None,
                "utilizavel_offline": False,
            }
            for vale in vales
        ],
    }
