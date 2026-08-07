"""
Signals do ProjetoEstoque.

Mantém o histórico de locação (LocacaoPeriodo) em dia: quando o status de um
Item locado muda entre Ativo/Backup e Pausado/Defeito, abre/fecha o período de
cobrança de aluguel. Também dispara o e-mail de "equipamento em Defeito" ao
fornecedor responsável. Conectado em apps.py (ready()).
"""
import logging

from django.db import transaction
from django.db.models.signals import pre_save, post_save, post_delete
from django.dispatch import receiver

from .models import Item, ItemPadraoDatasul, Locacao, RequisicaoItem, StatusItemChoices, Usuario

logger = logging.getLogger(__name__)


@receiver(pre_save, sender=Item)
def _item_capturar_status_antigo(sender, instance, **kwargs):
    if instance.pk:
        instance._old_status = (
            Item.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
        )
    else:
        instance._old_status = None


@receiver(post_save, sender=Item)
def _item_sincronizar_locacao(sender, instance, created, **kwargs):
    old = getattr(instance, "_old_status", None)
    if not created and old == instance.status:
        return
    try:
        from services.locacao_service import sincronizar
        sincronizar(instance, old)
    except Exception:
        # O histórico de locação nunca pode quebrar o save do item.
        logger.warning("Falha ao sincronizar período de locação", exc_info=True)


@receiver(post_save, sender=Item)
def _item_sincronizar_preventivas(sender, instance, created, **kwargs):
    """Contagem de preventiva só corre para item ATIVO: qualquer transição de
    status pausa (sai de ativo) ou retoma reiniciando o intervalo (volta a
    ativo) as preventivas do item — ver sincronizar_preventivas_com_status."""
    old = getattr(instance, "_old_status", None)
    if created or old == instance.status:
        return
    try:
        from .models import sincronizar_preventivas_com_status
        sincronizar_preventivas_com_status(instance)
    except Exception:
        # A sincronização de preventivas nunca pode quebrar o save do item.
        logger.warning("Falha ao sincronizar pausa de preventivas", exc_info=True)


@receiver(post_save, sender=Item)
def _item_notificar_defeito(sender, instance, created, **kwargs):
    """Ao equipamento TRANSICIONAR para Defeito (não na criação), avisa por
    e-mail o(s) login(s) do fornecedor configurados para receber esse aviso
    (`PerfilFornecedor.notificar_defeito_email`). Fire-and-forget via
    `transaction.on_commit` — nunca trava o save do item nem o request."""
    old = getattr(instance, "_old_status", None)
    if created or old == instance.status or instance.status != StatusItemChoices.DEFEITO:
        return
    if not instance.fornecedor_id:
        return

    pk = instance.pk

    def _mail():
        try:
            from services.email_alertas import alerta_item_defeito
            alerta_item_defeito(pk)
        except Exception:
            logger.warning("Falha ao notificar fornecedor sobre item em Defeito", exc_info=True)

    transaction.on_commit(_mail)


@receiver(post_save, sender=Usuario)
def _usuario_sincronizar_fts(sender, instance, **kwargs):
    """Mantém a tabela virtual FTS5 (usuario_busca_fts) em dia para a busca
    por relevância em /usuarios/ — ver services/busca_fts.py."""
    try:
        from services.busca_fts import sincronizar_usuario_fts
        sincronizar_usuario_fts(instance)
    except Exception:
        # Índice de busca é auxiliar: nunca pode quebrar o save do usuário.
        logger.warning("Falha ao sincronizar usuário no índice FTS", exc_info=True)


@receiver(post_delete, sender=Usuario)
def _usuario_remover_fts(sender, instance, **kwargs):
    try:
        from services.busca_fts import remover_usuario_fts
        remover_usuario_fts(instance.pk)
    except Exception:
        logger.warning("Falha ao remover usuário do índice FTS", exc_info=True)


@receiver(post_save, sender=RequisicaoItem)
def _requisicao_item_sincronizar_fts(sender, instance, **kwargs):
    """Mantém a tabela virtual FTS5 (requisicaoitem_busca_fts) em dia para a
    busca por relevância em /requisicoes/itens/ — ver services/busca_fts.py."""
    try:
        from services.busca_fts import sincronizar_requisicao_item_fts
        sincronizar_requisicao_item_fts(instance)
    except Exception:
        logger.warning("Falha ao sincronizar item de requisição no índice FTS", exc_info=True)


@receiver(post_delete, sender=RequisicaoItem)
def _requisicao_item_remover_fts(sender, instance, **kwargs):
    try:
        from services.busca_fts import remover_requisicao_item_fts
        remover_requisicao_item_fts(instance.pk)
    except Exception:
        logger.warning("Falha ao remover item de requisição do índice FTS", exc_info=True)


@receiver(post_save, sender=ItemPadraoDatasul)
def _item_padrao_sincronizar_fts(sender, instance, **kwargs):
    """Mantém a tabela virtual FTS5 (itempadraodatasul_busca_fts) em dia para
    a busca por relevância em /requisicoes/catalogo/ — ver services/busca_fts.py."""
    try:
        from services.busca_fts import sincronizar_item_padrao_fts
        sincronizar_item_padrao_fts(instance)
    except Exception:
        logger.warning("Falha ao sincronizar item padrão no índice FTS", exc_info=True)


@receiver(post_delete, sender=ItemPadraoDatasul)
def _item_padrao_remover_fts(sender, instance, **kwargs):
    try:
        from services.busca_fts import remover_item_padrao_fts
        remover_item_padrao_fts(instance.pk)
    except Exception:
        logger.warning("Falha ao remover item padrão do índice FTS", exc_info=True)


@receiver(post_save, sender=Locacao)
def _locacao_sincronizar_periodo(sender, instance, **kwargs):
    """Mantém o valor mensal do período aberto alinhado ao contrato de Locação
    (cobre o caso de a Locacao ser criada/editada após o item)."""
    item = instance.equipamento
    if item is None or str(getattr(item, "locado", "")) != "sim":
        return
    try:
        from services.locacao_service import _periodo_aberto
        periodo = _periodo_aberto(item)
        if periodo and periodo.valor_mensal != instance.valor_mensal:
            periodo.valor_mensal = instance.valor_mensal
            periodo.save(update_fields=["valor_mensal", "updated_at"])
    except Exception:
        logger.warning("Falha ao alinhar valor do período de locação", exc_info=True)
