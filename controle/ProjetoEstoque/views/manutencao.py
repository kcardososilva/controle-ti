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
    StatusItemChoices,
    StatusOrdemManutencaoChoices,
)

S = StatusOrdemManutencaoChoices

# TI age quando o fornecedor já devolveu o reparado (DEVOLVIDO) ou enviou o substituto.
_PENDENTES_TI = [S.DEVOLVIDO, S.SUBSTITUTO_ENVIADO]

_STATUS_RETORNO_OPCOES = [
    (StatusItemChoices.BACKUP.value, StatusItemChoices.BACKUP.label),
    (StatusItemChoices.ATIVO.value, StatusItemChoices.ATIVO.label),
]


@login_required
def manutencao_recebimentos(request):
    base = OrdemManutencao.objects.select_related("item", "item_substituto", "fornecedor")

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
def manutencao_recebimento_detail(request, pk: int):
    """Detalhe da OS — acompanha o processo de manutenção feito pelo fornecedor."""
    ordem = get_object_or_404(
        OrdemManutencao.objects.select_related(
            "item", "item__subtipo", "item__localidade", "item_substituto", "fornecedor",
            "movimentacao_origem",
        ),
        pk=pk,
    )
    eventos = ordem.eventos.select_related("criado_por").all()
    context = {
        "ordem": ordem,
        "eventos": eventos,
        "pode_concluir": ordem.status in _PENDENTES_TI,
        "status_retorno_opcoes": _STATUS_RETORNO_OPCOES,
    }
    return render(request, "front/manutencao/recebimento_detail.html", context)


@login_required
def manutencao_recebimento_acao(request, pk: int):
    ordem = get_object_or_404(OrdemManutencao, pk=pk)
    if request.method != "POST":
        return redirect("manutencao_recebimentos")

    # import tardio evita ciclo (service importa models do app)
    from services.ordem_manutencao_service import OrdemManutencaoService

    extra = {}
    status_retorno = request.POST.get("status_retorno")
    if status_retorno:
        extra["status_retorno"] = status_retorno

    destino = request.POST.get("next") or "manutencao_recebimentos"

    try:
        OrdemManutencaoService.transicionar(
            ordem=ordem,
            novo_status=S.CONCLUIDO,
            user=request.user,
            observacao=request.POST.get("observacao", ""),
            ator="ti",
            **extra,
        )
        messages.success(request, f"OS #{ordem.pk} concluída com sucesso.")
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))

    if destino == "detalhe":
        return redirect("manutencao_recebimento_detail", pk=pk)
    return redirect("manutencao_recebimentos")
