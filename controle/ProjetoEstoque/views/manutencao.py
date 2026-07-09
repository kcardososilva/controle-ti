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
    Fornecedor,
    OrdemManutencao,
    OrdemManutencaoAnexo,
    StatusItemChoices,
    StatusOrdemManutencaoChoices,
)

S = StatusOrdemManutencaoChoices

# TI precisa agir: aprovar/reprovar orçamento, receber o reparado/substituto,
# receber e armazenar p/ descarte um equipamento devolvido, aprovar/recusar um
# pedido de descarte local, ou, na troca antecipada, receber o substituto e enviar
# o defeituoso. (sem_condicoes = vez do fornecedor devolver.)
_PENDENTES_TI = [
    S.AGUARDANDO_APROVACAO, S.DEVOLVIDO, S.SUBSTITUTO_ENVIADO,
    S.DEVOLVIDO_DESCARTE, S.DESCARTE_LOCAL_SOLICITADO,
    S.TROCA_ANT_SUBSTITUTO_ENVIADO, S.TROCA_ANT_SUBSTITUTO_RECEBIDO,
]
# Estados em que a ação do TI é "concluir" a ordem (receber item de volta).
_CONCLUIVEIS_TI = [S.DEVOLVIDO, S.SUBSTITUTO_ENVIADO]

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
            status__in=[S.TROCA_ANT_SUBSTITUTO_ENVIADO, S.TROCA_ANT_SUBSTITUTO_RECEBIDO]
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
        "status_retorno_opcoes": _STATUS_RETORNO_OPCOES,
    }
    return render(request, "front/manutencao/recebimento_detail.html", context)


# Ações que o TI pode disparar. DESCARTE_LOCAL_APROVADO = aprovar o pedido de
# descarte local; SEM_CONDICOES = recusar o descarte local (exigir devolução).
_ACOES_TI_VALIDAS = {
    S.APROVADO, S.REPROVADO, S.CONCLUIDO, S.DESCARTADO,
    S.DESCARTE_LOCAL_APROVADO, S.SEM_CONDICOES,
    S.TROCA_ANT_SUBSTITUTO_RECEBIDO, S.TROCA_ANT_DEFEITUOSO_ENVIADO,
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

    # ── Transição conduzida pelo TI (aprovar / reprovar / concluir) ─────────
    # import tardio evita ciclo (service importa models do app)
    from services.ordem_manutencao_service import OrdemManutencaoService

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
            str(S.SEM_CONDICOES): f"Descarte local da OS #{ordem.pk} recusado. O equipamento deve ser devolvido para descarte interno.",
            str(S.TROCA_ANT_SUBSTITUTO_RECEBIDO): f"Substituto da OS #{ordem.pk} recebido em estoque. Envie o equipamento defeituoso (prioritário).",
            str(S.TROCA_ANT_DEFEITUOSO_ENVIADO): f"Equipamento defeituoso da OS #{ordem.pk} enviado ao fornecedor.",
        }.get(str(novo_status), "Status atualizado com sucesso.")
        messages.success(request, _msg)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))

    return _voltar()
