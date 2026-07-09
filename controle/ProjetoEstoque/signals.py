"""
Signals do ProjetoEstoque.

Mantém o histórico de locação (LocacaoPeriodo) em dia: quando o status de um
Item locado muda entre Ativo/Backup e Pausado/Defeito, abre/fecha o período de
cobrança de aluguel. Também dispara o e-mail de "equipamento em Defeito" ao
fornecedor responsável. Conectado em apps.py (ready()).
"""
import logging

from django.db import transaction
from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver

from .models import Item, Locacao, StatusItemChoices

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
