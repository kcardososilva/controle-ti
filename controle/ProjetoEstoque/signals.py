"""
Signals do ProjetoEstoque.

Mantém o histórico de locação (LocacaoPeriodo) em dia: quando o status de um
Item locado muda entre Ativo/Backup e Pausado/Defeito, abre/fecha o período de
cobrança de aluguel. Conectado em apps.py (ready()).
"""
import logging

from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver

from .models import Item, Locacao

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
