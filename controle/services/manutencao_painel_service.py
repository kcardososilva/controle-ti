"""
Painel de Manutenção — visão central do módulo de manutenção corretiva
(reparo externo com fornecedor, orçamentos, troca antecipada). Reúne o que
hoje fica disperso em telas sem cruzamento: fila de orçamentos aguardando
decisão do TI, e os lotes (Envio do Fornecedor / Remessa de Separação) ainda
em aberto — tudo com link direto para a tela de ação correspondente.
"""
from ProjetoEstoque.models import (
    LoteEnvioFornecedor,
    LoteSeparacao,
    OrdemManutencao,
    StatusLoteEnvioFornecedorChoices,
    StatusOrdemManutencaoChoices,
    StatusSeparacaoChoices,
)

S = StatusOrdemManutencaoChoices

_ORCAMENTO_PENDENTE = [S.AGUARDANDO_APROVACAO, S.TROCA_ANT_AGUARDANDO_APROVACAO]


class ManutencaoPainelService:

    @classmethod
    def fila_orcamentos_pendentes(cls, *, fornecedor_id=None, centro_custo_id=None):
        """OS aguardando decisão do TI sobre um orçamento proposto pelo
        fornecedor — a fila central de "o que precisa de análise agora"."""
        qs = (
            OrdemManutencao.objects
            .filter(status__in=_ORCAMENTO_PENDENTE)
            .select_related("item", "item__centro_custo", "fornecedor")
        )
        if fornecedor_id:
            qs = qs.filter(fornecedor_id=fornecedor_id)
        if centro_custo_id:
            qs = qs.filter(item__centro_custo_id=centro_custo_id)
        return qs.order_by("updated_at")

    @classmethod
    def lotes_envio_fornecedor_abertos(cls):
        """Lotes de Envio (fornecedor→TI) ainda em organização/rascunho."""
        return (
            LoteEnvioFornecedor.objects
            .filter(status=StatusLoteEnvioFornecedorChoices.ABERTO)
            .select_related("fornecedor")
            .order_by("-created_at")
        )

    @classmethod
    def lotes_separacao_abertos(cls):
        """Lotes de Remessa (TI→fornecedor, Envio e Devolução) ainda abertos."""
        return (
            LoteSeparacao.objects
            .filter(status=StatusSeparacaoChoices.ABERTO)
            .select_related("fornecedor")
            .order_by("-created_at")
        )
