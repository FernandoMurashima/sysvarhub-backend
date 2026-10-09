from datetime import datetime, time, timedelta

from django.core.paginator import Paginator
from django.db.models import Prefetch, Q
from django.utils import timezone
from django.utils.dateparse import parse_date

from core.models import EventoSyncHub, NFCeHub, VendaHub, VendaPagamentoHub


PAGE_SIZE_PADRAO = 20
PAGE_SIZE_MAXIMO = 100


class ConsultaVendasValidationError(Exception):
    pass


def listar_vendas(hub, filtros):
    data_ini, data_fim = _periodo_datas(filtros.get("data_ini"), filtros.get("data_fim"))
    page = _int_param(filtros.get("page"), 1, minimo=1)
    page_size = _int_param(filtros.get("page_size"), PAGE_SIZE_PADRAO, minimo=1, maximo=PAGE_SIZE_MAXIMO)
    qs = _queryset_base(hub).filter(finalizada_em__gte=data_ini, finalizada_em__lt=data_fim)

    status_filtro = (filtros.get("status") or VendaHub.STATUS_FINALIZADA).strip().upper()
    status_validos = {status for status, _label in VendaHub.STATUS_CHOICES}
    if status_filtro not in status_validos:
        raise ConsultaVendasValidationError("Status de venda inválido.")
    qs = qs.filter(status=status_filtro)

    qs = _aplicar_filtros(qs, filtros)
    pagina = Paginator(qs.order_by("-finalizada_em", "-id"), page_size).get_page(page)
    vendas = list(pagina.object_list)
    eventos_venda = _eventos_por_chave(hub, _chaves_venda(vendas))

    return {
        "count": pagina.paginator.count,
        "page": pagina.number,
        "page_size": page_size,
        "total_pages": pagina.paginator.num_pages,
        "results": [serializar_venda_listagem(venda, eventos_venda.get(_chave_venda(venda))) for venda in vendas],
    }


def detalhar_venda(hub, venda_uuid):
    venda = _queryset_base(hub).filter(venda_uuid=venda_uuid).first()
    if not venda:
        return None

    chaves = [_chave_venda(venda)]
    nfce = _nfce(venda)
    if nfce:
        chaves.append(_chave_nfce(nfce))
    eventos = _eventos_por_chave(hub, chaves)
    return serializar_venda_detalhe(
        venda,
        eventos.get(_chave_venda(venda)),
        eventos.get(_chave_nfce(nfce)) if nfce else None,
    )


def serializar_venda_listagem(venda, evento_venda=None):
    nfce = _nfce(venda)
    return {
        "venda_uuid": str(venda.venda_uuid),
        "documento": venda.documento,
        "status": venda.status,
        "finalizada_em": _iso(venda.finalizada_em),
        "total": _decimal(venda.total),
        "subtotal": _decimal(venda.subtotal),
        "desconto_geral": _decimal(venda.desconto_geral),
        "cliente": _serializar_cliente(venda),
        "vendedor": _serializar_vendedor(venda),
        "terminal": _serializar_terminal(venda.terminal_finalizacao or venda.terminal),
        "caixa": _serializar_caixa(venda.sessao_caixa.caixa),
        "pagamentos": [_serializar_pagamento_resumo(pagamento) for pagamento in _pagamentos(venda)],
        "nfce": _serializar_nfce_resumo(nfce),
        "sincronizacao": _serializar_sync(evento_venda),
    }


def serializar_venda_detalhe(venda, evento_venda=None, evento_nfce=None):
    nfce = _nfce(venda)
    return {
        "venda_uuid": str(venda.venda_uuid),
        "documento": venda.documento,
        "status": venda.status,
        "criada_em": _iso(venda.criada_em),
        "finalizada_em": _iso(venda.finalizada_em),
        "subtotal": _decimal(venda.subtotal),
        "desconto_geral": _decimal(venda.desconto_geral),
        "total": _decimal(venda.total),
        "valor_recebido": _decimal(venda.valor_recebido),
        "troco": _decimal(venda.troco),
        "hub": _serializar_hub(venda.hub),
        "loja": _serializar_hub(venda.hub),
        "terminal": _serializar_terminal(venda.terminal_finalizacao or venda.terminal),
        "caixa": _serializar_caixa(venda.sessao_caixa.caixa),
        "operador_finalizacao": _serializar_operador(venda.operador_finalizacao),
        "vendedor": _serializar_vendedor(venda),
        "cliente": _serializar_cliente(venda),
        "itens": [_serializar_item(item) for item in venda.itens.all()],
        "pagamentos": [_serializar_pagamento_detalhe(pagamento) for pagamento in _pagamentos(venda)],
        "nfce": _serializar_nfce_detalhe(nfce),
        "sincronizacao": {
            "venda_finalizada": _serializar_sync(evento_venda),
            "nfce_atualizada": _serializar_sync(evento_nfce) if nfce else None,
        },
    }


def _queryset_base(hub):
    pagamentos_ativos = VendaPagamentoHub.objects.filter(status=VendaPagamentoHub.STATUS_ATIVO).prefetch_related(
        "parcelas_snapshot"
    )
    return (
        VendaHub.objects.filter(hub=hub)
        .select_related(
            "hub",
            "sessao_caixa__caixa",
            "terminal",
            "terminal_finalizacao",
            "operador_finalizacao",
            "nfce",
        )
        .prefetch_related(
            "itens",
            Prefetch("pagamentos", queryset=pagamentos_ativos, to_attr="pagamentos_ativos"),
        )
    )


def _aplicar_filtros(qs, filtros):
    documento = (filtros.get("documento") or "").strip()
    if documento:
        qs = qs.filter(documento__icontains=documento)

    cliente = (filtros.get("cliente") or "").strip()
    if cliente:
        qs = qs.filter(Q(cliente_nome__icontains=cliente) | Q(cliente_documento__icontains=cliente))

    vendedor = (filtros.get("vendedor") or "").strip()
    if vendedor:
        qs = qs.filter(_filtro_vendedor(vendedor))

    forma_pagamento = (filtros.get("forma_pagamento") or "").strip()
    if forma_pagamento:
        qs = qs.filter(pagamentos__status=VendaPagamentoHub.STATUS_ATIVO, pagamentos__codigo__iexact=forma_pagamento)

    nfce = (filtros.get("nfce") or "").strip()
    if nfce:
        qs = qs.filter(_filtro_nfce(nfce))

    return qs.distinct()


def _periodo_datas(data_ini_raw, data_fim_raw):
    hoje = timezone.localdate()
    data_ini = _parse_data(data_ini_raw, "data_ini") if data_ini_raw else hoje
    data_fim = _parse_data(data_fim_raw, "data_fim") if data_fim_raw else hoje
    if data_ini > data_fim:
        raise ConsultaVendasValidationError("data_ini não pode ser maior que data_fim.")
    tz = timezone.get_current_timezone()
    inicio = timezone.make_aware(datetime.combine(data_ini, time.min), tz)
    fim = timezone.make_aware(datetime.combine(data_fim + timedelta(days=1), time.min), tz)
    return inicio, fim


def _parse_data(valor, nome):
    data = parse_date(str(valor or ""))
    if data is None:
        raise ConsultaVendasValidationError(f"{nome} inválida. Use o formato YYYY-MM-DD.")
    return data


def _filtro_vendedor(valor):
    filtro = Q(vendedor_nome__icontains=valor) | Q(vendedor_matricula__iexact=valor)
    try:
        filtro |= Q(vendedor_retaguarda_id=int(valor))
    except (TypeError, ValueError):
        pass
    return filtro


def _filtro_nfce(valor):
    filtro = Q(nfce__chave_acesso__icontains=valor) | Q(nfce__protocolo__icontains=valor)
    try:
        filtro |= Q(nfce__numero=int(valor))
    except (TypeError, ValueError):
        pass
    return filtro


def _eventos_por_chave(hub, chaves):
    if not chaves:
        return {}
    return {
        evento.chave_idempotencia: evento
        for evento in EventoSyncHub.objects.filter(hub=hub, chave_idempotencia__in=chaves)
    }


def _chaves_venda(vendas):
    return [_chave_venda(venda) for venda in vendas]


def _chave_venda(venda):
    return f"VENDA:{venda.venda_uuid}:FINALIZADA"


def _chave_nfce(nfce):
    return f"NFCE:{nfce.nfce_uuid}:V:{nfce.sync_versao}"


def _pagamentos(venda):
    return getattr(venda, "pagamentos_ativos", None) or venda.pagamentos.filter(status=VendaPagamentoHub.STATUS_ATIVO)


def _nfce(venda):
    try:
        return venda.nfce
    except NFCeHub.DoesNotExist:
        return None


def _serializar_cliente(venda):
    return {
        "nome": venda.cliente_nome or "",
        "documento": venda.cliente_documento or "",
        "cliente_uuid": str(venda.cliente_uuid) if venda.cliente_uuid else None,
        "retaguarda_id": venda.cliente_retaguarda_id,
    }


def _serializar_vendedor(venda):
    return {
        "id": venda.vendedor_retaguarda_id,
        "retaguarda_id": venda.vendedor_retaguarda_id,
        "matricula": venda.vendedor_matricula,
        "nome": venda.vendedor_nome,
        "apelido": venda.vendedor_apelido,
    } if venda.vendedor_retaguarda_id else None


def _serializar_terminal(terminal):
    if terminal is None:
        return None
    return {
        "codigo": terminal.codigo,
        "nome": terminal.nome,
    }


def _serializar_caixa(caixa):
    if caixa is None:
        return None
    return {
        "codigo": caixa.codigo,
        "descricao": caixa.descricao,
        "nome": caixa.descricao,
    }


def _serializar_hub(hub):
    return {
        "hub_uuid": str(hub.hub_uuid),
        "empresa_id": hub.empresa_id,
        "empresa_nome": hub.empresa_nome,
        "loja_id": hub.loja_id,
        "loja_nome": hub.loja_nome,
        "loja_apelido": hub.loja_apelido,
    }


def _serializar_operador(operador):
    if operador is None:
        return None
    return {
        "id": operador.retaguarda_usuario_id,
        "codigo": operador.codigo,
        "nome": operador.nome,
    }


def _serializar_item(item):
    return {
        "item_uuid": str(item.item_uuid),
        "ean": item.ean13 or "",
        "referencia": item.referencia,
        "codigo_item_ref": item.codigo_item_ref,
        "descricao": item.descricao,
        "cor": item.cor_descricao,
        "tamanho": item.tamanho_descricao,
        "quantidade": item.quantidade,
        "preco_unitario": _decimal(item.preco_unitario, casas=4),
        "desconto": _decimal(item.desconto),
        "total_item": _decimal(item.total_item),
        "promocao": {
            "id": item.promocao_retaguarda_id,
            "nome": item.promocao_nome,
            "tipo": item.promocao_tipo,
            "valor": _decimal(item.promocao_valor, casas=4),
        } if item.promocao_retaguarda_id else None,
    }


def _serializar_pagamento_resumo(pagamento):
    return {
        "codigo": pagamento.codigo,
        "descricao": pagamento.descricao,
        "tipo": pagamento.tipo,
        "valor": _decimal(pagamento.valor),
    }


def _serializar_pagamento_detalhe(pagamento):
    return {
        "pagamento_uuid": str(pagamento.pagamento_uuid),
        "codigo": pagamento.codigo,
        "descricao": pagamento.descricao,
        "tipo": pagamento.tipo,
        "valor": _decimal(pagamento.valor),
        "autorizacao": pagamento.autorizacao,
        "prazo": {
            "codigo": pagamento.prazo_codigo,
            "descricao": pagamento.prazo_descricao,
            "retaguarda_id": pagamento.retaguarda_prazo_pagamento_id,
        },
        "num_parcelas": pagamento.num_parcelas,
        "taxa_percentual": _decimal(pagamento.taxa_percentual, casas=4),
        "taxa_fixa": _decimal(pagamento.taxa_fixa),
        "parcelas": [
            {
                "ordem": parcela.ordem,
                "dias": parcela.dias,
                "percentual": _decimal(parcela.percentual, casas=6) if parcela.percentual is not None else None,
                "valor_fixo": _decimal(parcela.valor_fixo) if parcela.valor_fixo is not None else None,
            }
            for parcela in pagamento.parcelas_snapshot.all()
        ],
        "vale_troca": {
            "documento": pagamento.vale_troca_documento,
            "retaguarda_id": pagamento.vale_troca_retaguarda_id,
            "reserva_id": pagamento.vale_troca_reserva_id,
            "valor_reservado": _decimal(pagamento.vale_troca_valor_reservado),
        } if pagamento.vale_troca_documento else None,
    }


def _serializar_nfce_resumo(nfce):
    if nfce is None:
        return None
    return {
        "numero": nfce.numero,
        "serie": nfce.serie,
        "status": nfce.status,
    }


def _serializar_nfce_detalhe(nfce):
    if nfce is None:
        return None
    return {
        "nfce_uuid": str(nfce.nfce_uuid),
        "modelo": nfce.modelo,
        "serie": nfce.serie,
        "numero": nfce.numero,
        "status": nfce.status,
        "chave_acesso": nfce.chave_acesso,
        "protocolo": nfce.protocolo,
        "tipo_emissao": nfce.tipo_emissao,
        "retorno_codigo": nfce.codigo_retorno,
        "retorno_mensagem": nfce.mensagem_retorno,
        "emitida_em": _iso(nfce.emitida_em),
        "autorizada_em": _iso(nfce.autorizada_em),
    }


def _serializar_sync(evento):
    if evento is None:
        return {
            "status": "SEM_EVENTO",
            "ultimo_erro": "",
            "tentativas": 0,
            "sincronizado_em": None,
        }
    return {
        "status": evento.status,
        "ultimo_erro": evento.ultimo_erro,
        "tentativas": evento.tentativas,
        "sincronizado_em": _iso(evento.sincronizado_em),
        "tipo": evento.tipo,
    }


def _iso(valor):
    return valor.isoformat() if valor else None


def _decimal(valor, *, casas=2):
    return f"{valor:.{casas}f}"


def _int_param(valor, default, *, minimo=None, maximo=None):
    try:
        numero = int(valor)
    except (TypeError, ValueError):
        numero = default
    if minimo is not None:
        numero = max(minimo, numero)
    if maximo is not None:
        numero = min(maximo, numero)
    return numero
