import logging

from django.db import transaction
from django.utils import timezone

from core.models import HubConfig, SincronizacaoRecebidaHub
from integracao.services.bootstrap import sincronizar_bootstrap
from integracao.services.catalogo import sincronizar_catalogo
from integracao.services.clientes import sincronizar_clientes
from integracao.services.formas_pagamento import sincronizar_formas_pagamento
from integracao.services.operadores import sincronizar_operadores
from integracao.services.retaguarda import RetaguardaClient, RetaguardaError
from integracao.services.tipos_despesa_pdv import sincronizar_tipos_despesa_pdv
from integracao.services.vendedores import sincronizar_vendedores


logger = logging.getLogger(__name__)

ETAPAS = [
    ("BOOTSTRAP", "bootstrap", sincronizar_bootstrap),
    ("CATALOGO", "catalogo", sincronizar_catalogo),
    ("OPERADORES", "operadores", sincronizar_operadores),
    ("VENDEDORES", "vendedores", sincronizar_vendedores),
    ("FORMAS_PAGAMENTO", "formas_pagamento", sincronizar_formas_pagamento),
    ("TIPOS_DESPESA", "tipos_despesa_pdv", sincronizar_tipos_despesa_pdv),
    ("CLIENTES", "clientes", sincronizar_clientes),
]


def executar_sincronizacao_comando(hub, comando, client=None):
    if not comando or not comando.get("id"):
        return None
    if int(comando.get("hub_id") or hub.retaguarda_hub_id or 0) not in (0, int(hub.retaguarda_hub_id or 0)):
        raise RetaguardaError("Solicitação pertence a outro Hub.")
    client = client or RetaguardaClient(hub.retaguarda_url)
    sincronizacao, _criada = SincronizacaoRecebidaHub.objects.get_or_create(
        hub=hub,
        retaguarda_solicitacao_id=comando["id"],
        defaults={"tipo": comando.get("tipo") or "COMPLETA"},
    )
    if sincronizacao.status == SincronizacaoRecebidaHub.STATUS_CONCLUIDA:
        try:
            _informar(client, hub, sincronizacao, SincronizacaoRecebidaHub.STATUS_CONCLUIDA)
        except RetaguardaError:
            logger.warning("Falha ao reenviar conclusão da sincronização à Central.", exc_info=True)
        return sincronizacao
    if sincronizacao.status == SincronizacaoRecebidaHub.STATUS_ERRO:
        _informar(client, hub, sincronizacao, SincronizacaoRecebidaHub.STATUS_ERRO, sincronizacao.mensagem_erro)
        return sincronizacao

    try:
        _marcar_processando(sincronizacao)
        _informar(client, hub, sincronizacao, SincronizacaoRecebidaHub.STATUS_PROCESSANDO)
        for etapa, metodo_client, funcao in ETAPAS:
            _atualizar_etapa(sincronizacao, etapa)
            _informar(client, hub, sincronizacao, SincronizacaoRecebidaHub.STATUS_PROCESSANDO)
            resposta = getattr(client, metodo_client)(token=hub.retaguarda_token)
            if etapa == "CATALOGO":
                funcao(hub, resposta, client=client)
            else:
                funcao(hub, resposta)
        _marcar_concluida(hub, sincronizacao)
        try:
            _informar(client, hub, sincronizacao, SincronizacaoRecebidaHub.STATUS_CONCLUIDA)
        except RetaguardaError:
            logger.warning("Carga concluída localmente, mas falhou ao informar conclusão à Central.", exc_info=True)
        return sincronizacao
    except Exception as exc:
        mensagem = _mensagem_controlada(exc)
        _marcar_erro(hub, sincronizacao, mensagem)
        try:
            _informar(client, hub, sincronizacao, SincronizacaoRecebidaHub.STATUS_ERRO, mensagem)
        except RetaguardaError:
            logger.warning("Falha ao informar erro da sincronização à Central.", exc_info=True)
        logger.exception("Falha na sincronização Central -> Hub.")
        return sincronizacao


def executar_carga_completa_tecnica(hub, client=None):
    comando = {"id": 0, "tipo": "COMPLETA", "hub_id": hub.retaguarda_hub_id}
    sync, _ = SincronizacaoRecebidaHub.objects.get_or_create(
        hub=hub,
        retaguarda_solicitacao_id=0,
        defaults={"tipo": "COMPLETA"},
    )
    if sync.status in (SincronizacaoRecebidaHub.STATUS_CONCLUIDA, SincronizacaoRecebidaHub.STATUS_ERRO):
        sync.status = SincronizacaoRecebidaHub.STATUS_PENDENTE
        sync.concluido_em = None
        sync.mensagem_erro = ""
        sync.save(update_fields=["status", "concluido_em", "mensagem_erro", "atualizado_em"])
    return executar_sincronizacao_comando(hub, comando, client=client)


def _informar(client, hub, sincronizacao, status, mensagem_erro=""):
    if sincronizacao.retaguarda_solicitacao_id == 0:
        return
    client.atualizar_status_sincronizacao(
        token=hub.retaguarda_token,
        sincronizacao_id=sincronizacao.retaguarda_solicitacao_id,
        status=status,
        etapa_atual=sincronizacao.etapa_atual,
        mensagem_erro=mensagem_erro,
    )


def _marcar_processando(sincronizacao):
    agora = timezone.now()
    sincronizacao.status = SincronizacaoRecebidaHub.STATUS_PROCESSANDO
    if sincronizacao.iniciado_em is None:
        sincronizacao.iniciado_em = agora
    sincronizacao.save(update_fields=["status", "iniciado_em", "atualizado_em"])


def _atualizar_etapa(sincronizacao, etapa):
    sincronizacao.etapa_atual = etapa
    sincronizacao.save(update_fields=["etapa_atual", "atualizado_em"])


@transaction.atomic
def _marcar_concluida(hub, sincronizacao):
    agora = timezone.now()
    sincronizacao.status = SincronizacaoRecebidaHub.STATUS_CONCLUIDA
    sincronizacao.concluido_em = agora
    sincronizacao.mensagem_erro = ""
    sincronizacao.save(update_fields=["status", "concluido_em", "mensagem_erro", "atualizado_em"])
    hub = HubConfig.objects.select_for_update().get(pk=hub.pk)
    hub.ultima_carga_central_em = agora
    hub.ultima_carga_central_status = SincronizacaoRecebidaHub.STATUS_CONCLUIDA
    hub.ultima_carga_central_erro = ""
    if hub.primeira_carga_concluida_em is None:
        hub.primeira_carga_concluida_em = agora
    hub.save(update_fields=["ultima_carga_central_em", "ultima_carga_central_status", "ultima_carga_central_erro", "primeira_carga_concluida_em", "atualizado_em"])


@transaction.atomic
def _marcar_erro(hub, sincronizacao, mensagem):
    agora = timezone.now()
    sincronizacao.status = SincronizacaoRecebidaHub.STATUS_ERRO
    sincronizacao.concluido_em = agora
    sincronizacao.mensagem_erro = mensagem
    sincronizacao.save(update_fields=["status", "concluido_em", "mensagem_erro", "atualizado_em"])
    HubConfig.objects.filter(pk=hub.pk).update(
        ultima_carga_central_status=SincronizacaoRecebidaHub.STATUS_ERRO,
        ultima_carga_central_erro=mensagem,
        atualizado_em=agora,
    )


def _mensagem_controlada(exc):
    texto = str(exc) or exc.__class__.__name__
    return texto.splitlines()[0][:500]
