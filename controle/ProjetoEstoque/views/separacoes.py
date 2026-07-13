"""
Remessa de Itens — "Remessa para Envio" e "Remessa para Devolução".

Área de estágio anterior à movimentação real: um item entra aqui através de uma
movimentação `separacao_envio`/`separacao_devolucao` (ver MovimentacaoEstoqueService
+ SeparacaoService), pode ser agrupado num lote nomeado, e por fim é
despachado — o que dispara a movimentação de verdade (envio_manutencao ou
devolucao_locacao), respeitando todas as regras já existentes do sistema.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import render, redirect, get_object_or_404

from ..models import (
    DocumentoFiscalRemessa,
    Fornecedor,
    LoteSeparacao,
    SeparacaoItem,
    StatusSeparacaoChoices,
    TipoSeparacaoChoices,
)
from services.documento_fiscal_service import DocumentoFiscalService
from services.separacao_service import SeparacaoService

_TITULOS = {
    TipoSeparacaoChoices.ENVIO: "Remessa para Envio",
    TipoSeparacaoChoices.DEVOLUCAO: "Remessa para Devolução",
}
_ROTAS_LIST = {
    TipoSeparacaoChoices.ENVIO: "separacao_envio_list",
    TipoSeparacaoChoices.DEVOLUCAO: "separacao_devolucao_list",
}


def _separacao_list_context(request, tipo):
    q = (request.GET.get("q") or "").strip()

    soltos = (
        SeparacaoItem.objects
        .filter(tipo=tipo, status=StatusSeparacaoChoices.ABERTO, lote__isnull=True)
        .select_related(
            "item", "item__categoria", "item__subtipo", "item__localidade",
            "item__centro_custo", "item__locacao", "fornecedor", "criado_por",
        )
        .order_by("-created_at")
    )
    lotes = (
        LoteSeparacao.objects
        .filter(tipo=tipo)
        .select_related("fornecedor")
        .prefetch_related("itens")
        .order_by("-created_at")
    )

    if q:
        soltos = soltos.filter(item__nome__icontains=q)
        lotes = lotes.filter(nome__icontains=q)

    soltos = list(soltos)
    lotes = list(lotes)

    itens_info = {}
    for s in soltos:
        s.badge_contrato = SeparacaoService.badge_contrato(s.item)
        itens_info[str(s.id)] = SeparacaoService.info_equipamento(s)

    documentos = list(
        DocumentoFiscalRemessa.objects
        .filter(tipo=tipo)
        .prefetch_related("itens__item")
        .order_by("-created_at")[:15]
    )

    return {
        "tipo": tipo,
        "titulo": _TITULOS[tipo],
        "rota_list": _ROTAS_LIST[tipo],
        "soltos": soltos,
        "lotes": lotes,
        "itens_info": itens_info,
        "documentos": documentos,
        "fornecedores": Fornecedor.objects.order_by("nome"),
        "q": q,
        "kpi": {
            "soltos": len(soltos),
            "em_lote": sum(l.quantidade_abertos for l in lotes),
            "lotes_abertos": sum(1 for l in lotes if l.status == StatusSeparacaoChoices.ABERTO),
        },
    }


@login_required
def separacao_envio_list(request):
    context = _separacao_list_context(request, TipoSeparacaoChoices.ENVIO)
    return render(request, "front/equipamentos/separacao_list.html", context)


@login_required
def separacao_devolucao_list(request):
    context = _separacao_list_context(request, TipoSeparacaoChoices.DEVOLUCAO)
    return render(request, "front/equipamentos/separacao_list.html", context)


@login_required
def separacao_lote_create(request):
    if request.method != "POST":
        return redirect("separacao_envio_list")

    tipo = request.POST.get("tipo") or TipoSeparacaoChoices.ENVIO
    rota_volta = _ROTAS_LIST.get(tipo, "separacao_envio_list")

    nome = (request.POST.get("nome") or "").strip()
    fornecedor_id = request.POST.get("fornecedor")
    separacao_ids = request.POST.getlist("separacao_ids")

    if not nome:
        messages.error(request, "Informe um nome para o lote.")
        return redirect(rota_volta)

    fornecedor = Fornecedor.objects.filter(pk=fornecedor_id).first()
    if not fornecedor:
        messages.error(request, "Selecione o fornecedor do lote.")
        return redirect(rota_volta)

    separacoes = list(SeparacaoItem.objects.filter(pk__in=separacao_ids).select_related("item"))
    if not separacoes:
        messages.error(request, "Selecione ao menos um item para criar o lote.")
        return redirect(rota_volta)

    try:
        lote = SeparacaoService.criar_lote(
            nome=nome, tipo=tipo, fornecedor=fornecedor, separacoes=separacoes, user=request.user,
        )
        messages.success(request, f'Lote "{lote.nome}" criado com {len(separacoes)} item(ns).')
        return redirect("separacao_lote_detail", pk=lote.pk)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect(rota_volta)


@login_required
def separacao_lote_detail(request, pk):
    lote = get_object_or_404(
        LoteSeparacao.objects.select_related("fornecedor", "criado_por"), pk=pk,
    )
    itens = list(
        lote.itens
        .select_related(
            "item", "item__categoria", "item__subtipo", "item__localidade",
            "item__centro_custo", "item__locacao", "criado_por",
        )
        .order_by("-created_at")
    )

    itens_info = {}
    for i in itens:
        i.badge_contrato = SeparacaoService.badge_contrato(i.item)
        itens_info[str(i.id)] = SeparacaoService.info_equipamento(i)

    documentos = list(lote.documentos_fiscais.order_by("-created_at"))

    context = {
        "lote": lote,
        "itens": itens,
        "itens_info": itens_info,
        "documentos": documentos,
        "rota_list": _ROTAS_LIST.get(lote.tipo, "separacao_envio_list"),
        "titulo": _TITULOS.get(lote.tipo, "Remessa"),
    }
    return render(request, "front/equipamentos/separacao_lote_detail.html", context)


@login_required
def separacao_lote_excluir(request, pk):
    lote = get_object_or_404(LoteSeparacao, pk=pk)
    rota_volta = _ROTAS_LIST.get(lote.tipo, "separacao_envio_list")
    if request.method != "POST":
        return redirect(rota_volta)

    nome = lote.nome
    try:
        SeparacaoService.excluir_lote(lote=lote, user=request.user)
        messages.success(request, f'Lote "{nome}" desfeito — os itens voltaram a ficar soltos na remessa.')
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("separacao_lote_detail", pk=pk)
    return redirect(rota_volta)


@login_required
def separacao_lote_enviar(request, pk):
    lote = get_object_or_404(LoteSeparacao, pk=pk)
    rota_volta = _ROTAS_LIST.get(lote.tipo, "separacao_envio_list")
    if request.method != "POST":
        return redirect("separacao_lote_detail", pk=pk)

    try:
        SeparacaoService.despachar_lote(lote=lote, user=request.user)
        messages.success(request, f'Lote "{lote.nome}" despachado com sucesso.')
        return redirect(rota_volta)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("separacao_lote_detail", pk=pk)


@login_required
def separacao_item_remover(request, pk):
    separacao = get_object_or_404(SeparacaoItem.objects.select_related("item"), pk=pk)
    rota_volta = _ROTAS_LIST.get(separacao.tipo, "separacao_envio_list")
    lote_pk = separacao.lote_id
    if request.method != "POST":
        return redirect(rota_volta)

    try:
        SeparacaoService.remover_item(separacao=separacao, user=request.user)
        messages.success(request, f'"{separacao.item.nome}" removido da remessa.')
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))

    if lote_pk:
        return redirect("separacao_lote_detail", pk=lote_pk)
    return redirect(rota_volta)


@login_required
def separacao_item_enviar(request, pk):
    separacao = get_object_or_404(SeparacaoItem.objects.select_related("item"), pk=pk)
    rota_volta = _ROTAS_LIST.get(separacao.tipo, "separacao_envio_list")
    lote_pk = separacao.lote_id
    if request.method != "POST":
        return redirect(rota_volta)

    try:
        SeparacaoService.despachar_item(separacao=separacao, user=request.user)
        messages.success(request, f'"{separacao.item.nome}" despachado com sucesso.')
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))

    if lote_pk:
        return redirect("separacao_lote_detail", pk=lote_pk)
    return redirect(rota_volta)


@login_required
def documento_fiscal_gerar(request):
    """Gera o Documento Fiscal de Remessa (PDF + e-mail ao Fiscal) a partir dos
    itens soltos selecionados na lista, ou de um lote inteiro (`lote_id`)."""
    if request.method != "POST":
        return redirect("separacao_envio_list")

    tipo = request.POST.get("tipo") or TipoSeparacaoChoices.ENVIO
    rota_volta = _ROTAS_LIST.get(tipo, "separacao_envio_list")
    lote_pk = request.POST.get("lote_id")

    lote = None
    if lote_pk:
        lote = get_object_or_404(LoteSeparacao, pk=lote_pk)
        tipo = lote.tipo
        rota_volta = _ROTAS_LIST.get(tipo, "separacao_envio_list")
        separacoes = list(lote.itens.select_related("item", "fornecedor").all())
    else:
        separacao_ids = request.POST.getlist("separacao_ids")
        separacoes = list(
            SeparacaoItem.objects.filter(pk__in=separacao_ids).select_related("item", "fornecedor")
        )

    if not separacoes:
        messages.error(request, "Selecione ao menos um item para gerar o documento fiscal.")
        return redirect("separacao_lote_detail", pk=lote.pk) if lote else redirect(rota_volta)

    try:
        documento = DocumentoFiscalService.gerar_e_enviar(
            tipo=tipo, separacoes=separacoes, user=request.user, lote=lote,
        )
        if documento.email_enviado:
            messages.success(
                request,
                f"Documento fiscal {documento.numero} gerado e enviado para: "
                f"{documento.destinatarios_envio or '—'}.",
            )
        else:
            messages.warning(
                request,
                f"Documento fiscal {documento.numero} gerado, mas o e-mail não foi enviado "
                f"(canal desativado ou sem destinatários configurados em Central de Alertas → "
                f"Configurar Notificações). O PDF continua disponível para download.",
            )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))

    if lote:
        return redirect("separacao_lote_detail", pk=lote.pk)
    return redirect(rota_volta)


@login_required
def documento_fiscal_pdf_view(request, pk):
    """Reemite o PDF do Documento Fiscal sob demanda (não é persistido em disco)."""
    documento = get_object_or_404(DocumentoFiscalRemessa, pk=pk)
    pdf_bytes = DocumentoFiscalService.gerar_pdf_bytes(documento)
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="documento_fiscal_{documento.numero}.pdf"'
    return response
