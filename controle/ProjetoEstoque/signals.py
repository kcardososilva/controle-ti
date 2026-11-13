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
def _item_sair_de_reservado(sender, instance, created, **kwargs):
    """Rede de segurança da reserva: se o equipamento deixa de ser "Reservado"
    por um caminho que NÃO passou pelo fluxo de reserva (um reparo aberto
    direto, um ajuste de cadastro), a reserva não pode continuar "ativa" sobre
    um equipamento que já não está reservado. Ela vira "usada em outra
    necessidade", a vaga fica pendente de equipamento e — sem substituto em
    Backup — o TI é avisado. O fluxo normal (entrega, uso confirmado,
    liberação) fecha a reserva ANTES de salvar o item, então aqui não há
    reserva ativa e nada acontece."""
    old = getattr(instance, "_old_status", None)
    if created or old != StatusItemChoices.RESERVADO or instance.status == old:
        return
    try:
        from services.reserva_equipamento_service import item_saiu_de_reservado
        item_saiu_de_reservado(instance, old, getattr(instance, "atualizado_por", None))
    except Exception:
        # A reserva nunca pode quebrar o save do item.
        logger.warning("Falha ao desfazer a reserva do equipamento %s", instance.pk, exc_info=True)


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


# Status em que o computador está PARADO — a janela em que a licença presa a
# ele fica pagando sem uso. Entrar nela pede cancelamento; sair dela pede o
# religamento. Por isso o aviso dispara nos DOIS sentidos.
_STATUS_COMPUTADOR_PARADO = (
    StatusItemChoices.BACKUP, StatusItemChoices.RESERVADO, StatusItemChoices.PAUSADO,
)


@receiver(post_save, sender=Item)
def _item_notificar_computador_parado(sender, instance, created, **kwargs):
    """Notebook/desktop que ENTRA ou SAI de Backup/Pausado dispara a lista
    atualizada de computadores parados, com ele em destaque.

    Dispara nos dois sentidos de propósito: a ida é quando se cancela a
    licença, a volta é quando se religa — avisar só a ida deixaria máquina
    voltando a operar com licença cancelada, que é o chamado de segunda-feira.
    Transição ENTRE dois status parados (backup → pausado) não avisa: a
    decisão sobre a licença já foi tomada na entrada.

    Fire-and-forget em `transaction.on_commit`, como o aviso de Defeito —
    SMTP lento nunca pode travar o save do item."""
    old = getattr(instance, "_old_status", None)
    if created or old == instance.status:
        return

    saiu = old in _STATUS_COMPUTADOR_PARADO
    entrou = instance.status in _STATUS_COMPUTADOR_PARADO
    if not (saiu or entrou) or (saiu and entrou):
        return

    try:
        from services.email_alertas import eh_computador_parado
        # Na SAÍDA o item já não está parado, então o teste de situação não
        # serve — o que importa ali é só ser notebook/desktop.
        nome_subtipo = (instance.subtipo.nome if instance.subtipo_id else "") or ""
        eh_computador = any(
            termo in nome_subtipo.lower() for termo in ("notebook", "desktop")
        )
        if entrou and not eh_computador_parado(instance):
            return
        if not eh_computador:
            return
    except Exception:
        logger.warning("Falha ao classificar item para o aviso de computador parado", exc_info=True)
        return

    pk = instance.pk
    anterior = old or ""

    def _mail():
        try:
            from services.email_alertas import alerta_computadores_parados
            alerta_computadores_parados(item_destaque_pk=pk, status_anterior=anterior)
        except Exception:
            logger.warning("Falha ao enviar aviso de computador parado", exc_info=True)

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
