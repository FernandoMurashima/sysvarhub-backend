from django.core.paginator import Paginator
from django.db.models import CharField, Count, Q
from django.db.models.functions import Cast

from core.models import EventoSyncHub, NFCeHub, ValeTrocaHub, VendaHub
from core.services.central_status import calcular_status_central
from core.services.sync import SEGREDOS_BLOQUEADOS, sincronizar_eventos_pendentes


STATUS_OPERACIONAL_AGUARDANDO_DEPENDENCIA = "AGUARDANDO_DEPENDENCIA"
STATUS_PRIORIDADE = {
    EventoSyncHub.STATUS_CONFLITO: 1,
    EventoSyncHub.STATUS_ERRO: 2,
    STATUS_OPERACIONAL_AGUARDANDO_DEPENDENCIA: 3,
    EventoSyncHub.STATUS_PENDENTE: 4,
    EventoSyncHub.STATUS_PROCESSANDO: 5,
    EventoSyncHub.STATUS_SINCRONIZADO: 6,
}
STATUS_FILTROS = {choice[0] for choice in EventoSyncHub.STATUS_CHOICES}


def listar_pendencias_sync(hub, *, filtros):
    qs = EventoSyncHub.objects.filter(hub=hub)
    status_filtro = (filtros.get("status") or "").upper()
    tipo_filtro = (filtros.get("tipo") or "").strip().upper()
    busca = (filtros.get("q") or "").strip()
    somente_problemas = _bool_param(filtros.get("problemas"))
    page = _int_param(filtros.get("page"), 1, minimo=1)
    page_size = _int_param(filtros.get("page_size"), 20, minimo=1, maximo=100)

    if status_filtro in STATUS_FILTROS:
        qs = qs.filter(status=status_filtro)
    elif status_filtro == STATUS_OPERACIONAL_AGUARDANDO_DEPENDENCIA:
        qs = qs.filter(status__in=[EventoSyncHub.STATUS_PENDENTE, EventoSyncHub.STATUS_ERRO])
    if tipo_filtro:
        qs = qs.filter(tipo=tipo_filtro)
    if somente_problemas:
        qs = qs.exclude(status=EventoSyncHub.STATUS_SINCRONIZADO)
    if busca:
        qs = _aplicar_busca(qs, busca)

    eventos = [serializar_evento_operacional(evento) for evento in qs.order_by("-atualizado_em", "-criado_em", "-id")]
    if status_filtro == STATUS_OPERACIONAL_AGUARDANDO_DEPENDENCIA:
        eventos = [evento for evento in eventos if evento["bloqueado_por_dependencia"]]

    eventos.sort(key=_ordem_data, reverse=True)
    eventos.sort(key=lambda item: STATUS_PRIORIDADE.get(item["status_operacional"], 9))
    pagina = Paginator(eventos, page_size).get_page(page)
    return {
        "eventos": list(pagina.object_list),
        "resumo": resumo_eventos_sync(hub),
        "central": calcular_status_central(hub),
        "paginacao": {
            "page": pagina.number,
            "page_size": page_size,
            "total": pagina.paginator.count,
            "pages": pagina.paginator.num_pages,
        },
        "tipos": list(
            EventoSyncHub.objects.filter(hub=hub).order_by("tipo").values_list("tipo", flat=True).distinct()
        ),
    }


def resumo_eventos_sync(hub):
    base = {status: 0 for status in STATUS_FILTROS}
    contagens = EventoSyncHub.objects.filter(hub=hub).values("status").annotate(total=Count("id"))
    for linha in contagens:
        base[linha["status"]] = linha["total"]
    return {
        "pendentes": base[EventoSyncHub.STATUS_PENDENTE],
        "processando": base[EventoSyncHub.STATUS_PROCESSANDO],
        "erros": base[EventoSyncHub.STATUS_ERRO],
        "conflitos": base[EventoSyncHub.STATUS_CONFLITO],
        "sincronizados": base[EventoSyncHub.STATUS_SINCRONIZADO],
    }


def serializar_evento_operacional(evento):
    dependencia = _detalhar_dependencia(evento)
    bloqueado = bool(dependencia)
    status_operacional = STATUS_OPERACIONAL_AGUARDANDO_DEPENDENCIA if bloqueado else evento.status
    documento = _documento_evento(evento)
    return {
        "id": evento.id,
        "evento_uuid": str(evento.evento_uuid),
        "chave_idempotencia": evento.chave_idempotencia,
        "tipo": evento.tipo,
        "status": evento.status,
        "status_operacional": status_operacional,
        "criado_em": _iso(evento.criado_em),
        "atualizado_em": _iso(evento.atualizado_em),
        "sincronizado_em": _iso(evento.sincronizado_em),
        "tentativas": evento.tentativas,
        "ultimo_erro": evento.ultimo_erro,
        "proxima_tentativa_em": _iso(evento.proxima_tentativa_em),
        "documento": documento,
        "origem": _origem_evento(evento),
        "dependencia": dependencia,
        "bloqueado_por_dependencia": bloqueado,
        "mensagem_operacional": _mensagem_operacional(evento, status_operacional, dependencia, documento),
        "resposta_central": _sanitizar_json(evento.resposta or {}),
        "payload_tecnico": _sanitizar_json(evento.payload or {}),
        "acoes": {
            "retry_permitido": evento.status in [EventoSyncHub.STATUS_ERRO, EventoSyncHub.STATUS_PENDENTE] and not bloqueado,
            "sincronizar_agora_permitido": evento.status == EventoSyncHub.STATUS_PENDENTE and not bloqueado,
        },
    }


def tentar_reprocessar_evento(hub, evento_id, *, client=None):
    evento = EventoSyncHub.objects.filter(hub=hub, pk=evento_id).first()
    if not evento:
        return None, "Evento não encontrado."
    visao = serializar_evento_operacional(evento)
    if evento.status == EventoSyncHub.STATUS_CONFLITO:
        return visao, "Conflito exige revisão administrativa."
    if visao["bloqueado_por_dependencia"]:
        return visao, "Evento aguardando dependência; retry manual bloqueado."
    if evento.status not in [EventoSyncHub.STATUS_ERRO, EventoSyncHub.STATUS_PENDENTE]:
        return visao, "Evento não está elegível para retry manual."
    evento.proxima_tentativa_em = None
    evento.save(update_fields=["proxima_tentativa_em", "atualizado_em"])
    resultado = sincronizar_eventos_pendentes(hub=hub, client=client, limite=1, evento_ids=[evento.pk])
    evento.refresh_from_db()
    return serializar_evento_operacional(evento), "", resultado


def _documento_evento(evento):
    payload = evento.payload or {}
    if evento.tipo == "NFCE_ATUALIZADA":
        partes = []
        if payload.get("numero"):
            partes.append(f"NFC-e {payload.get('numero')}")
        if payload.get("serie"):
            partes.append(f"Série {payload.get('serie')}")
        if payload.get("venda_uuid"):
            partes.append(f"Venda {str(payload.get('venda_uuid'))[:8]}")
        return " · ".join(partes) or f"NFC-e {evento.evento_uuid}"
    if evento.tipo == "DEVOLUCAO_FINALIZADA":
        vale = payload.get("vale_troca") or {}
        partes = [payload.get("documento_venda"), payload.get("documento_central"), vale.get("documento")]
        texto = " · ".join(str(parte) for parte in partes if parte)
        return texto or f"Devolução {str(payload.get('devolucao_uuid') or evento.evento_uuid)[:8]}"
    if evento.tipo == "VENDA_FINALIZADA":
        return _documento_venda_finalizada(evento, payload)
    for chave in ["documento", "sessao_uuid", "caixa_codigo", "fechamento_uuid"]:
        if payload.get(chave):
            return str(payload[chave])
    return str(evento.evento_uuid)


def _origem_evento(evento):
    payload = evento.payload or {}
    partes = []
    if payload.get("terminal"):
        partes.append(f"Terminal {payload['terminal']}")
    if payload.get("caixa_retaguarda_id"):
        partes.append(f"Caixa {payload['caixa_retaguarda_id']}")
    if payload.get("origem"):
        partes.append(str(payload["origem"]))
    return " · ".join(partes) or "Hub local"


def _detalhar_dependencia(evento):
    if evento.tipo != "VENDA_FINALIZADA":
        return None
    deps = ((evento.payload or {}).get("dependencias") or {}).get("devolucoes_uuid") or []
    if not deps:
        return None
    vales = (
        ValeTrocaHub.objects.select_related("devolucao")
        .filter(hub=evento.hub, devolucao__devolucao_uuid__in=deps, provisorio=True)
        .order_by("id")
    )
    if not vales.exists():
        return None
    itens = []
    for vale in vales:
        devolucao = vale.devolucao
        itens.append(
            {
                "tipo": "DEVOLUCAO_FINALIZADA",
                "uuid": str(devolucao.devolucao_uuid) if devolucao else "",
                "documento": getattr(devolucao, "documento", "") or vale.documento,
                "status": getattr(devolucao, "status", "") or "PROVISORIO",
                "motivo": "Crédito provisório ainda não confirmado pela Central.",
            }
        )
    return {
        "resumo": "Venda aguardando confirmação da devolução provisória.",
        "itens": itens,
    }


def _mensagem_operacional(evento, status_operacional, dependencia, documento):
    if dependencia:
        return dependencia["resumo"]
    if evento.status == EventoSyncHub.STATUS_CONFLITO:
        return "Requer revisão administrativa."
    if evento.status == EventoSyncHub.STATUS_ERRO:
        return evento.ultimo_erro or "Falha sujeita a nova tentativa."
    if evento.status == EventoSyncHub.STATUS_PROCESSANDO:
        return "Evento em processamento pelo Hub."
    if evento.status == EventoSyncHub.STATUS_SINCRONIZADO:
        return "Confirmado pela Central."
    return f"{documento} aguardando sincronização."


def _aplicar_busca(qs, busca):
    qs = qs.annotate(payload_text=Cast("payload", CharField()), resposta_text=Cast("resposta", CharField()))
    return qs.filter(
        Q(tipo__icontains=busca)
        | Q(evento_uuid__icontains=busca)
        | Q(chave_idempotencia__icontains=busca)
        | Q(ultimo_erro__icontains=busca)
        | Q(payload_text__icontains=busca)
        | Q(resposta_text__icontains=busca)
    )


def _documento_venda_finalizada(evento, payload):
    venda_uuid = payload.get("venda_uuid")
    if venda_uuid:
        venda = VendaHub.objects.filter(hub=evento.hub, venda_uuid=venda_uuid).first()
        if venda:
            nfce = NFCeHub.objects.filter(hub=evento.hub, venda=venda).first()
            if nfce:
                partes = [f"NFC-e {nfce.numero}"]
                if nfce.serie:
                    partes.append(f"Série {nfce.serie}")
                return " · ".join(partes)
            return f"Venda local {str(venda.venda_uuid)[:8]}"
    return payload.get("documento") or payload.get("documento_venda") or f"Venda {str(venda_uuid or evento.evento_uuid)[:8]}"


def _ordem_data(item):
    return item["atualizado_em"] or item["criado_em"] or ""


def _sanitizar_json(valor):
    if isinstance(valor, dict):
        return {
            chave: "***"
            if str(chave).lower() in SEGREDOS_BLOQUEADOS
            else _sanitizar_json(item)
            for chave, item in valor.items()
        }
    if isinstance(valor, list):
        return [_sanitizar_json(item) for item in valor]
    return valor


def _iso(valor):
    return valor.isoformat() if valor else None


def _bool_param(valor):
    return str(valor or "").lower() in {"1", "true", "sim", "yes"}


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
