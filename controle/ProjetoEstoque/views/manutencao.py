"""
Manutenção externa — lado TI (interno).

Caixa de entrada das Ordens de Manutenção (Portal do Fornecedor):
  • pendentes de ação do TI (REPARADO → confirmar retorno; SUBSTITUTO_ENVIADO → receber)
  • histórico completo com filtros (fornecedor / status / busca)
  • detalhe da OS: acompanha o processo de manutenção conduzido pelo fornecedor
A transição (efeitos colaterais) fica no OrdemManutencaoService.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import render, redirect, get_object_or_404

from ..models import (
    CentroCusto,
    Fornecedor,
    LoteEnvioFornecedor,
    LoteEnvioFornecedorItem,
    OrdemManutencao,
    OrdemManutencaoAnexo,
    StatusItemChoices,
    StatusLoteEnvioFornecedorChoices,
    StatusOrdemManutencaoChoices,
    TipoItemLoteEnvioChoices,
)

S = StatusOrdemManutencaoChoices

# TI precisa agir: aprovar/reprovar orçamento de reparo, aprovar/reprovar o
# contrato do substituto (troca), aprovar/reprovar a avaliação de descarte,
# receber o reparado/substituto, receber e armazenar p/ descarte um equipamento
# devolvido, aprovar/recusar um pedido de descarte local, ou, na troca
# antecipada, receber o substituto, enviar o defeituoso, e aprovar/reprovar a
# proposta de reparo.
_PENDENTES_TI = [
    S.AGUARDANDO_APROVACAO, S.DEVOLVIDO, S.SUBSTITUTO_ENVIADO,
    S.TROCA_AGUARDANDO_APROVACAO, S.SEM_CONDICOES,
    S.DEVOLVIDO_DESCARTE, S.DESCARTE_LOCAL_SOLICITADO,
    S.TROCA_ANT_SUBSTITUTO_ENVIADO, S.TROCA_ANT_SUBSTITUTO_RECEBIDO,
    S.TROCA_ANT_AGUARDANDO_APROVACAO,
]
# Estados em que a ação do TI é "concluir" a ordem (receber item de volta).
# SUBSTITUTO_ENVIADO NÃO conclui direto: primeiro o TI decide a cobrança pelo
# equipamento danificado (ver pode_decidir_dano_substituicao / TROCA_DANO_*).
_CONCLUIVEIS_TI = [S.DEVOLVIDO]

_STATUS_RETORNO_OPCOES = [
    (StatusItemChoices.BACKUP.value, StatusItemChoices.BACKUP.label),
    (StatusItemChoices.ATIVO.value, StatusItemChoices.ATIVO.label),
]


@login_required
def manutencao_recebimentos(request):
    base = OrdemManutencao.objects.select_related("item", "item_substituto", "fornecedor", "devolucao_localidade")

    f_fornecedor = (request.GET.get("fornecedor") or "").strip()
    f_status = (request.GET.get("status") or "").strip()
    q = (request.GET.get("q") or "").strip()

    pendentes = base.filter(status__in=_PENDENTES_TI)
    todas = base

    if f_fornecedor.isdigit():
        pendentes = pendentes.filter(fornecedor_id=int(f_fornecedor))
        todas = todas.filter(fornecedor_id=int(f_fornecedor))
    if f_status:
        todas = todas.filter(status=f_status)
    if q:
        todas = todas.filter(
            Q(item__nome__icontains=q)
            | Q(item__numero_serie__icontains=q)
            | Q(chamado__icontains=q)
        )

    todas = todas.order_by("-created_at")
    paginator = Paginator(todas, 20)
    page_obj = paginator.get_page(request.GET.get("page", 1))

    get_copy = request.GET.copy()
    get_copy.pop("page", None)
    qs_keep = get_copy.urlencode()

    context = {
        "pendentes": pendentes,
        "qtd_pendentes": pendentes.count(),
        "qtd_aprovacao": pendentes.filter(status=S.AGUARDANDO_APROVACAO).count(),
        "qtd_receber": pendentes.filter(status__in=_CONCLUIVEIS_TI).count(),
        "qtd_descartar": pendentes.filter(status=S.DEVOLVIDO_DESCARTE).count(),
        "qtd_descarte_local": pendentes.filter(status=S.DESCARTE_LOCAL_SOLICITADO).count(),
        "qtd_troca_antecipada": pendentes.filter(
            status__in=[S.TROCA_ANT_SUBSTITUTO_ENVIADO, S.TROCA_ANT_SUBSTITUTO_RECEBIDO,
                        S.TROCA_ANT_AGUARDANDO_APROVACAO]
        ).count(),
        "page_obj": page_obj,
        "total": paginator.count,
        "fornecedores": Fornecedor.objects.order_by("nome"),
        "status_choices": StatusOrdemManutencaoChoices.choices,
        "status_retorno_opcoes": _STATUS_RETORNO_OPCOES,
        "f_fornecedor": f_fornecedor,
        "f_status": f_status,
        "f_q": q,
        "tem_filtro": any([f_fornecedor, f_status, q]),
        "qs_keep": qs_keep,
    }
    return render(request, "front/manutencao/recebimentos.html", context)


@login_required
def manutencao_recebimentos_ajuda(request):
    """Central de ajuda do módulo de recebimentos — guia de uso para o TI."""
    return render(request, "front/manutencao/recebimentos_ajuda.html", {})


@login_required
def manutencao_recebimento_detail(request, pk: int):
    """Detalhe da OS — acompanha o processo de manutenção feito pelo fornecedor."""
    ordem = get_object_or_404(
        OrdemManutencao.objects.select_related(
            "item", "item__subtipo", "item__localidade", "item_substituto", "fornecedor",
            "movimentacao_origem", "aprovado_por", "devolucao_localidade",
        ),
        pk=pk,
    )
    eventos = ordem.eventos.select_related("criado_por").all()
    anexos = ordem.anexos.select_related("criado_por").all()
    context = {
        "ordem": ordem,
        "eventos": eventos,
        "anexos": anexos,
        "pode_aprovar": ordem.status == S.AGUARDANDO_APROVACAO,
        "pode_concluir": ordem.status in _CONCLUIVEIS_TI,
        "pode_descartar": ordem.status == S.DEVOLVIDO_DESCARTE,
        "pode_aprovar_descarte_local": ordem.status == S.DESCARTE_LOCAL_SOLICITADO,
        "pode_receber_substituto_ant": ordem.status == S.TROCA_ANT_SUBSTITUTO_ENVIADO,
        "pode_enviar_defeituoso_ant": ordem.status == S.TROCA_ANT_SUBSTITUTO_RECEBIDO,
        "pode_aprovar_troca_antecipada": ordem.status == S.TROCA_ANT_AGUARDANDO_APROVACAO,
        "pode_aprovar_troca": ordem.status == S.TROCA_AGUARDANDO_APROVACAO,
        "pode_decidir_dano_substituicao": ordem.status == S.SUBSTITUTO_ENVIADO,
        "pode_aprovar_descarte": ordem.status == S.SEM_CONDICOES,
        "status_retorno_opcoes": _STATUS_RETORNO_OPCOES,
    }
    return render(request, "front/manutencao/recebimento_detail.html", context)


# Ações que o TI pode disparar. DESCARTE_LOCAL_APROVADO = aprovar o pedido de
# descarte local; DESCARTE_AVALIACAO_APROVADA a partir de descarte_local_solicitado
# = recusar o LOCAL mas aceitar o valor (exige devolução para descarte interno).
_ACOES_TI_VALIDAS = {
    S.APROVADO, S.REPROVADO, S.CONCLUIDO, S.DESCARTADO,
    S.DESCARTE_LOCAL_APROVADO,
    S.TROCA_APROVADA, S.TROCA_REPROVADA,
    S.TROCA_DANO_REPROVADA,
    S.DESCARTE_AVALIACAO_APROVADA, S.DESCARTE_AVALIACAO_REPROVADA,
    S.TROCA_ANT_SUBSTITUTO_RECEBIDO, S.TROCA_ANT_DEFEITUOSO_ENVIADO,
    S.TROCA_ANT_REPROVADO,
}


@login_required
def manutencao_recebimento_acao(request, pk: int):
    ordem = get_object_or_404(OrdemManutencao, pk=pk)
    if request.method != "POST":
        return redirect("manutencao_recebimentos")

    destino = request.POST.get("next") or "manutencao_recebimentos"
    acao = request.POST.get("acao", "")

    def _voltar():
        if destino == "detalhe":
            return redirect("manutencao_recebimento_detail", pk=pk)
        return redirect("manutencao_recebimentos")

    # ── Upload de Nota Fiscal pelo TI (pode anexar quantas quiser) ──────────
    if acao == "anexar_nf":
        arquivos = request.FILES.getlist("nf")
        if not arquivos:
            messages.error(request, "Selecione ao menos um arquivo de NF.")
        else:
            descricao = request.POST.get("descricao", "").strip()
            for arq in arquivos:
                OrdemManutencaoAnexo.objects.create(
                    ordem=ordem,
                    arquivo=arq,
                    origem=OrdemManutencaoAnexo.OrigemAnexo.TI,
                    descricao=descricao,
                    criado_por=request.user,
                    atualizado_por=request.user,
                )
            messages.success(request, f"{len(arquivos)} nota(s) fiscal(is) anexada(s).")
        return _voltar()

    # ── Exclusão de Nota Fiscal (correção de erro) ──────────────────────────
    # O TI pode excluir qualquer NF da OS (do fornecedor ou dele mesmo).
    if acao == "excluir_nf":
        anexo = ordem.anexos.filter(pk=request.POST.get("anexo_id")).first()
        if not anexo:
            messages.error(request, "Nota fiscal não encontrada.")
        else:
            if anexo.arquivo:
                anexo.arquivo.delete(save=False)  # remove o arquivo físico também
            anexo.delete()
            messages.success(request, "Nota fiscal excluída.")
        return _voltar()

    # import tardio evita ciclo (service importa models do app)
    from services.ordem_manutencao_service import OrdemManutencaoService

    # ── Troca antecipada: aprovar a proposta de reparo já conclui a OS num só
    # clique (2 hops internos, 2 eventos na timeline) — ver aprovar_e_concluir_troca_antecipada.
    if acao == "aprovar_troca_ant":
        try:
            OrdemManutencaoService.aprovar_e_concluir_troca_antecipada(
                ordem=ordem, user=request.user, observacao=request.POST.get("observacao", ""),
            )
            messages.success(request, f"Proposta de reparo da OS #{ordem.pk} aprovada — troca antecipada concluída.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        return _voltar()

    # ── Troca (sem reparo): aprovar a cobrança pelo equipamento danificado já
    # conclui a OS num só clique (2 hops internos, 2 eventos na timeline) — ver
    # aprovar_e_concluir_troca_danificado.
    if acao == "aprovar_dano_substituicao":
        try:
            OrdemManutencaoService.aprovar_e_concluir_troca_danificado(
                ordem=ordem, user=request.user, observacao=request.POST.get("observacao", ""),
            )
            messages.success(request, f"Cobrança pelo equipamento danificado da OS #{ordem.pk} aprovada — ordem concluída.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        return _voltar()

    # ── Transição conduzida pelo TI (aprovar / reprovar / concluir) ─────────
    novo_status = acao if acao in {str(s) for s in _ACOES_TI_VALIDAS} else S.CONCLUIDO

    extra = {}
    status_retorno = request.POST.get("status_retorno")
    if status_retorno:
        extra["status_retorno"] = status_retorno

    try:
        OrdemManutencaoService.transicionar(
            ordem=ordem,
            novo_status=novo_status,
            user=request.user,
            observacao=request.POST.get("observacao", ""),
            ator="ti",
            **extra,
        )
        _msg = {
            str(S.APROVADO): f"Orçamento da OS #{ordem.pk} aprovado.",
            str(S.REPROVADO): f"Orçamento da OS #{ordem.pk} reprovado.",
            str(S.CONCLUIDO): f"OS #{ordem.pk} concluída com sucesso.",
            str(S.DESCARTADO): f"Equipamento da OS #{ordem.pk} descartado.",
            str(S.DESCARTE_LOCAL_APROVADO): f"Descarte local da OS #{ordem.pk} aprovado. Aguardando o fornecedor confirmar.",
            str(S.TROCA_APROVADA): f"Contrato do equipamento substituto da OS #{ordem.pk} aprovado. Aguardando o fornecedor enviar o substituto.",
            str(S.TROCA_REPROVADA): f"Contrato do equipamento substituto da OS #{ordem.pk} reprovado. O fornecedor pode revisar e reenviar.",
            str(S.TROCA_DANO_REPROVADA): f"Cobrança pelo equipamento danificado da OS #{ordem.pk} reprovada. O fornecedor pode revisar e reenviar o valor.",
            str(S.DESCARTE_AVALIACAO_APROVADA): f"Avaliação de descarte da OS #{ordem.pk} aprovada. O equipamento deve ser devolvido para descarte interno.",
            str(S.DESCARTE_AVALIACAO_REPROVADA): f"Avaliação de descarte da OS #{ordem.pk} reprovada. O fornecedor pode revisar e reenviar o valor.",
            str(S.TROCA_ANT_SUBSTITUTO_RECEBIDO): f"Substituto da OS #{ordem.pk} recebido em estoque. Envie o equipamento defeituoso (prioritário).",
            str(S.TROCA_ANT_DEFEITUOSO_ENVIADO): f"Equipamento defeituoso da OS #{ordem.pk} enviado ao fornecedor.",
            str(S.TROCA_ANT_REPROVADO): f"Proposta de reparo da OS #{ordem.pk} reprovada. O fornecedor pode reenviar.",
        }.get(str(novo_status), "Status atualizado com sucesso.")
        messages.success(request, _msg)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))

    return _voltar()


# ─── Lotes de Envio do Fornecedor (visão do TI — todos os fornecedores) ───────
# Carrinho que o PRÓPRIO fornecedor monta no Portal (troca antecipada / cadastro
# de equipamento novo) e envia ao TI. Ver services/lote_envio_fornecedor_service.py.
# Direção oposta a Lotes de Manutenção (fatura pós-fato) e Remessa (staging do TI).

@login_required
def envio_fornecedor_list(request):
    lotes = (
        LoteEnvioFornecedor.objects
        .exclude(status=StatusLoteEnvioFornecedorChoices.ABERTO)
        .select_related("fornecedor", "criado_por")
        .prefetch_related("itens")
        .order_by("-enviado_em", "-created_at")
    )

    f_fornecedor = (request.GET.get("fornecedor") or "").strip()
    if f_fornecedor.isdigit():
        lotes = lotes.filter(fornecedor_id=int(f_fornecedor))

    context = {
        "lotes": lotes,
        "fornecedores": Fornecedor.objects.order_by("nome"),
        "f_fornecedor": f_fornecedor,
        "tem_filtro": bool(f_fornecedor),
    }
    return render(request, "front/manutencao/envio_fornecedor_list.html", context)


@login_required
def envio_fornecedor_detail(request, pk: int):
    from services.lote_envio_fornecedor_service import LoteEnvioFornecedorService

    lote = get_object_or_404(
        LoteEnvioFornecedor.objects.select_related("fornecedor", "criado_por"), pk=pk,
    )
    itens = (
        lote.itens
        .select_related("item_defeituoso", "item_resultado", "ordem", "ordem__item", "localidade_devolucao")
        .order_by("created_at")
    )
    anexos = lote.anexos.select_related("criado_por").all()
    context = {
        "lote": lote,
        "itens": itens,
        "anexos": anexos,
        "historico": LoteEnvioFornecedorService.montar_historico_lote(lote),
        "tem_pendente_confirmacao": itens.filter(status="enviado").exists(),
        "status_retorno_opcoes": _STATUS_RETORNO_OPCOES,
    }
    return render(request, "front/manutencao/envio_fornecedor_detail.html", context)


@login_required
def envio_fornecedor_confirmar_lote(request, pk: int):
    """Confirma, de uma vez, a etapa 'TI recebeu' de todos os itens do lote que
    estão exatamente nela (equipamento novo enviado / substituto a caminho) —
    itens mais adiante no processo (ex. aguardando aprovação de orçamento)
    continuam exigindo ação individual em Recebimentos."""
    from services.lote_envio_fornecedor_service import LoteEnvioFornecedorService

    lote = get_object_or_404(LoteEnvioFornecedor, pk=pk)
    if request.method == "POST":
        try:
            resumo = LoteEnvioFornecedorService.confirmar_recebimento_lote(
                lote=lote, user=request.user,
                status_retorno=request.POST.get("status_retorno"),
            )
            msg = f"{resumo['confirmados']} ite{'m' if resumo['confirmados'] == 1 else 'ns'} confirmado(s)."
            if resumo["ignorados"]:
                msg += f" {resumo['ignorados']} item(ns) precisam de ação individual (ex. aprovação de orçamento)."
            messages.success(request, msg)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
    return redirect("envio_fornecedor_detail", pk=lote.pk)


@login_required
def envio_fornecedor_item_confirmar(request, pk: int):
    """Confirma o recebimento de um item isolado: cadastro de EQUIPAMENTO NOVO
    ou retorno de REPARO_CONCLUIDO (a troca antecipada já usa o fluxo de
    Recebimentos existente, via a OS gerada)."""
    from services.lote_envio_fornecedor_service import LoteEnvioFornecedorService

    item_lote = get_object_or_404(LoteEnvioFornecedorItem, pk=pk)

    if request.method == "POST":
        try:
            if item_lote.tipo == TipoItemLoteEnvioChoices.REPARO_CONCLUIDO:
                LoteEnvioFornecedorService.confirmar_reparo_concluido(
                    item_lote=item_lote, user=request.user,
                    status_retorno=request.POST.get("status_retorno"),
                )
                messages.success(request, f"OS #{item_lote.ordem_id} concluída — equipamento recebido de volta.")
            else:
                LoteEnvioFornecedorService.confirmar_equipamento_novo(
                    item_lote=item_lote, user=request.user,
                    status_retorno=request.POST.get("status_retorno"),
                )
                messages.success(request, "Equipamento novo recebido e ativado em estoque.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))

    return redirect("envio_fornecedor_detail", pk=item_lote.lote_id)


# ─── Painel de Manutenção (visão central) ─────────────────────────────────────
# Reúne, num único lugar, o que hoje fica disperso em telas sem cruzamento:
# KPIs gerais (reaproveita _dados_manutencao do dashboard de apresentação), a
# fila de orçamentos aguardando decisão do TI, e os lotes (Envio do Fornecedor
# / Remessa) ainda em aberto — cada um com link direto pra ação.

@login_required
def manutencao_painel(request):
    from services.manutencao_painel_service import ManutencaoPainelService
    from .dashboards import _dados_manutencao

    f_fornecedor = (request.GET.get("fornecedor") or "").strip()
    f_centro_custo = (request.GET.get("centro_custo") or "").strip()

    dados = _dados_manutencao(request)

    orcamentos_pendentes = ManutencaoPainelService.fila_orcamentos_pendentes(
        fornecedor_id=int(f_fornecedor) if f_fornecedor.isdigit() else None,
        centro_custo_id=int(f_centro_custo) if f_centro_custo.isdigit() else None,
    )
    lotes_envio_fornecedor = ManutencaoPainelService.lotes_envio_fornecedor_abertos()
    lotes_separacao = ManutencaoPainelService.lotes_separacao_abertos()

    context = {
        "kpis": dados["kpis"],
        "orcamentos_pendentes": orcamentos_pendentes,
        "lotes_envio_fornecedor": lotes_envio_fornecedor,
        "lotes_separacao": lotes_separacao,
        "fornecedores": Fornecedor.objects.order_by("nome"),
        "centros_custo": CentroCusto.objects.order_by("numero"),
        "f_fornecedor": f_fornecedor,
        "f_centro_custo": f_centro_custo,
    }
    return render(request, "front/manutencao/painel.html", context)
