"""
LocacaoService — congelamento do aluguel por status do equipamento.

Regra (ver LocacaoPeriodo):
  • Item locado fica Ativo/Backup  → abre um período (conta do 0).
  • Item locado vai p/ Pausado/Defeito → fecha o período aberto (congela).
  • Reativar abre um NOVO período do zero; os períodos fechados ficam no histórico.

Acionado pelo signal de Item (ProjetoEstoque/signals.py) sempre que o status muda.
"""
import logging

from django.utils import timezone

from ProjetoEstoque.models import Locacao, LocacaoPeriodo

logger = logging.getLogger(__name__)

# Em manutenção o aluguel NÃO congela (o equipamento segue locado, só está sendo
# reparado). Congela quando Pausado (item substituído também vira Pausado),
# Descarte (equipamento descartado — o aluguel encerra em definitivo) ou
# Devolvido (fim de contrato de Locação — o equipamento saiu em definitivo).
ATIVOS = {"ativo", "backup", "manutencao"}
CONGELADOS = {"pausado", "descarte", "devolvido"}


def _hoje():
    return timezone.localdate()


def _periodo_aberto(item):
    return (
        item.locacao_periodos
        .filter(data_fim__isnull=True)
        .order_by("-data_inicio", "-id")
        .first()
    )


def _valor_mensal(item):
    try:
        loc = item.locacao
    except Locacao.DoesNotExist:
        return None
    return loc.valor_mensal if loc else None


def congelar(item, user=None):
    """Fecha o período aberto (se houver). Idempotente."""
    aberto = _periodo_aberto(item)
    if not aberto:
        return None
    aberto.data_fim = _hoje()
    aberto.motivo_fim = item.status
    if user is not None and hasattr(aberto, "atualizado_por"):
        aberto.atualizado_por = user
    aberto.save(update_fields=["data_fim", "motivo_fim", "atualizado_por", "updated_at"])
    return aberto


def descongelar(item, user=None):
    """Abre um novo período (conta do 0). Não duplica se já houver um aberto.

    `item._locacao_data_inicio_override`, quando setado explicitamente ANTES do
    `item.save()` (ver `LoteEnvioFornecedorService.confirmar_equipamento_novo`),
    substitui a data de início padrão (hoje) — usado para equipamento novo do
    fornecedor cuja cobrança de aluguel só deve começar em 1º de janeiro do ano
    seguinte, ou na data em que o fornecedor cadastrou o item."""
    if _periodo_aberto(item):
        return None
    data_inicio = getattr(item, "_locacao_data_inicio_override", None) or _hoje()
    periodo = LocacaoPeriodo(
        item=item,
        valor_mensal=_valor_mensal(item),
        data_inicio=data_inicio,
    )
    if user is not None:
        periodo.criado_por = user
        periodo.atualizado_por = user
    periodo.save()
    return periodo


def sincronizar(item, old_status, user=None):
    """Abre/fecha período conforme a transição de status do item locado."""
    if str(item.locado) != "sim":
        return

    novo = item.status
    era_ativo = (old_status in ATIVOS) if old_status else False
    era_congelado = (old_status in CONGELADOS) if old_status else False

    if novo in ATIVOS and not era_ativo:
        descongelar(item, user)
    elif novo in CONGELADOS and not era_congelado:
        congelar(item, user)
