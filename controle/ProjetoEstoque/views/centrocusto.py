from decimal import Decimal

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.http import HttpResponse
from django.db.models import Q, Count, Sum
from django.core.paginator import Paginator
from django.template.loader import get_template
from xhtml2pdf import pisa
from django.db.models import OuterRef, Subquery, Sum

from ..models import CentroCusto, Item, SimNaoChoices, Usuario
from ..forms import CentroCustoForm

from ..models import (
    CentroCusto,
    Item,
    MovimentacaoItem,
    SimNaoChoices,
    TipoMovimentacaoChoices,
    TipoTransferenciaChoices,
    Usuario,
)

def _get_centros_filtrados(request):
    q = request.GET.get("q", "").strip()
    pmb = request.GET.get("pmb", "").strip()

    qs = CentroCusto.objects.all().order_by("numero")

    if q:
        qs = qs.filter(Q(numero__icontains=q) | Q(departamento__icontains=q))
    if pmb:
        qs = qs.filter(pmb=pmb)
    
    return qs, q, pmb

@login_required
def centrocusto_list(request):
    """
    Dashboard de Centros de Custo (Card View).
    """
    qs, q, pmb = _get_centros_filtrados(request)
    
    centros_ids = list(qs.values_list("id", flat=True))
    total_filtrado = qs.count()

    # --- KPIs ---
    locados_qs = Item.objects.filter(
        centro_custo_id__in=centros_ids,
        locado="sim"
    )

    agregado = locados_qs.aggregate(
        total_custo=Sum("locacao__valor_mensal"),
        total_itens=Count("id")
    )
    kpi_custo_total = agregado["total_custo"] or Decimal(0)
    kpi_itens_locados = agregado["total_itens"] or 0
    kpi_media = (kpi_custo_total / total_filtrado) if total_filtrado > 0 else 0

    # Dados por Centro
    dados_por_centro = (
        locados_qs
        .values("centro_custo")
        .annotate(custo=Sum("locacao__valor_mensal"), qtd=Count("id"))
    )
    mapa_custo = {d["centro_custo"]: d["custo"] or 0 for d in dados_por_centro}
    mapa_qtd = {d["centro_custo"]: d["qtd"] or 0 for d in dados_por_centro}

    # Top Offensor
    kpi_top_cc = None
    kpi_top_val = 0
    if mapa_custo:
        top_id = max(mapa_custo, key=mapa_custo.get)
        kpi_top_val = mapa_custo[top_id]
        try:
            kpi_top_cc = CentroCusto.objects.get(id=top_id)
        except CentroCusto.DoesNotExist: pass

    # --- Paginação ---
    try:
        per_page = int(request.GET.get("pp", 12)) # Cards geralmente pedem menos itens por pg
    except ValueError:
        per_page = 12

    paginator = Paginator(qs, per_page)
    page_num = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_num)

    for cc in page_obj.object_list:
        cc.custo_calc = mapa_custo.get(cc.id, 0)
        cc.qtd_calc = mapa_qtd.get(cc.id, 0)

    get_copy = request.GET.copy()
    if "page" in get_copy: del get_copy["page"]
    qs_keep = get_copy.urlencode()

    context = {
        "centros": page_obj.object_list,
        "page_obj": page_obj,
        "total": total_filtrado,
        "qs_keep": qs_keep,
        "f_q": q, "f_pmb": pmb, "opt_pmb": SimNaoChoices.choices,
        "kpi_custo_total": kpi_custo_total,
        "kpi_itens": kpi_itens_locados,
        "kpi_media": kpi_media,
        "kpi_top_cc": kpi_top_cc,
        "kpi_top_val": kpi_top_val,
    }

    return render(request, "front/centrocusto/centrocusto_list.html", context)

@login_required
def centrocusto_export_pdf(request):
    qs, q, pmb = _get_centros_filtrados(request)
    centros = list(qs)
    centros_ids = [c.id for c in centros]
    
    locados_qs = Item.objects.filter(centro_custo_id__in=centros_ids, locado="sim")
    dados = locados_qs.values("centro_custo").annotate(custo=Sum("locacao__valor_mensal"), qtd=Count("id"))
    
    mapa_custo = {d["centro_custo"]: d["custo"] or 0 for d in dados}
    mapa_qtd = {d["centro_custo"]: d["qtd"] or 0 for d in dados}
    
    total_geral = 0
    for c in centros:
        c.custo_calc = mapa_custo.get(c.id, 0)
        c.qtd_calc = mapa_qtd.get(c.id, 0)
        total_geral += c.custo_calc

    context = {
        "centros": centros,
        "total_geral": total_geral,
        "filtros": {"Busca": q, "PMB": pmb},
        "usuario": request.user,
    }
    
    template_path = 'front/centrocusto/centrocusto_pdf.html'
    response = HttpResponse(content_type='application/pdf')
    response['Content-Disposition'] = 'attachment; filename="relatorio_centros.pdf"'

    template = get_template(template_path)
    html = template.render(context)

    pisa_status = pisa.CreatePDF(html, dest=response)
    if pisa_status.err: return HttpResponse('Erro PDF', status=500)
    return response


# CREATE
@login_required
def centrocusto_create(request):
    if request.method == "POST":
        form = CentroCustoForm(request.POST)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.criado_por = request.user
            obj.atualizado_por = request.user
            obj.save()
            messages.success(request, "Centro de custo criado com sucesso!")
            return redirect("centrocusto_list")
        messages.error(request, "Corrija os erros do formulário.")
    else:
        form = CentroCustoForm()

    return render(request, "front/centrocusto/centrocusto_form.html", {"form": form, "editar": False})


# UPDATE
@login_required
def centrocusto_update(request, pk: int):
    obj = get_object_or_404(CentroCusto, pk=pk)
    if request.method == "POST":
        form = CentroCustoForm(request.POST, instance=obj)
        if form.is_valid():
            sobj = form.save(commit=False)
            sobj.atualizado_por = request.user
            sobj.save()
            messages.success(request, "Centro de custo atualizado com sucesso!")
            return redirect("centrocusto_list")
        messages.error(request, "Corrija os erros do formulário.")
    else:
        form = CentroCustoForm(instance=obj)

    return render(request, "front/centrocusto/centrocusto_form.html", {"form": form, "editar": True})


@login_required
def centrocusto_detail(request, pk):
    """
    Dashboard detalhado de um Centro de Custo.

    Melhoria aplicada:
    - Mostra o usuário atualmente vinculado ao item sem criar campo novo no model Item.
    - O vínculo é calculado pela última movimentação relevante do item.
    - Se a última movimentação relevante for devolução, o item aparece como "Sem usuário".

    Observação técnica:
    O model MovimentacaoItem usa related_name="movimentacoes" no campo item.
    Portanto, não utilize item.movimentacaoitem_set neste projeto.
    """
    obj = get_object_or_404(CentroCusto, pk=pk)

    movimentos_vinculo_usuario = (
        MovimentacaoItem.objects
        .filter(
            item_id=OuterRef("pk"),
            usuario__isnull=False,
            tipo_movimentacao__in=[
                TipoMovimentacaoChoices.TRANSFERENCIA,
                TipoMovimentacaoChoices.TRANSFERENCIA_EQUIPAMENTO,
            ],
        )
        .order_by("-created_at", "-pk")
    )

    itens_qs = (
        Item.objects
        .filter(centro_custo=obj)
        .select_related("subtipo", "localidade", "categoria", "fornecedor")
        .annotate(
            ultimo_usuario_id=Subquery(movimentos_vinculo_usuario.values("usuario_id")[:1]),
            ultimo_tipo_transferencia=Subquery(movimentos_vinculo_usuario.values("tipo_transferencia")[:1]),
            ultimo_vinculo_data=Subquery(movimentos_vinculo_usuario.values("created_at")[:1]),
        )
        .order_by("nome")
    )

    itens_cc = list(itens_qs)

    usuarios_ids_vinculados = {
        item.ultimo_usuario_id
        for item in itens_cc
        if item.ultimo_usuario_id
        and item.ultimo_tipo_transferencia != TipoTransferenciaChoices.DEVOLUCAO
    }

    usuarios_map = (
        Usuario.objects
        .filter(pk__in=usuarios_ids_vinculados)
        .select_related("funcao", "localidade", "centro_custo")
        .in_bulk()
    )

    total_itens_com_usuario = 0
    for item in itens_cc:
        usuario_atual = None

        if (
            item.ultimo_usuario_id
            and item.ultimo_tipo_transferencia != TipoTransferenciaChoices.DEVOLUCAO
        ):
            usuario_atual = usuarios_map.get(item.ultimo_usuario_id)

        item.usuario_vinculado = usuario_atual

        if usuario_atual:
            total_itens_com_usuario += 1

    colaboradores_qs = (
        Usuario.objects
        .filter(centro_custo=obj)
        .select_related("funcao", "localidade")
        .order_by("nome")
    )

    total_itens = len(itens_cc)
    total_colaboradores = colaboradores_qs.count()
    total_colaboradores_ativos = colaboradores_qs.filter(status="ativo").count()

    custo_mensal = (
        Item.objects
        .filter(centro_custo=obj, locado=SimNaoChoices.SIM)
        .aggregate(total=Sum("locacao__valor_mensal"))
        .get("total")
        or Decimal("0.00")
    )

    valor_patrimonial = (
        Item.objects
        .filter(centro_custo=obj)
        .exclude(locado=SimNaoChoices.SIM)
        .aggregate(total=Sum("valor"))
        .get("total")
        or Decimal("0.00")
    )

    context = {
        "obj": obj,
        "itens_cc": itens_cc,
        "colaboradores": colaboradores_qs,
        "kpi": {
            "total_itens": total_itens,
            "total_itens_com_usuario": total_itens_com_usuario,
            "total_colaboradores": total_colaboradores,
            "total_colaboradores_ativos": total_colaboradores_ativos,
            "custo_mensal": custo_mensal,
            "valor_patrimonial": valor_patrimonial,
        },
    }

    return render(request, "front/centrocusto/centrocusto_detail.html", context)


# DELETE (POST via modal)
@login_required
@permission_required("ProjetoEstoque.delete_centrocusto", raise_exception=True)
def centrocusto_delete(request, pk: int):
    obj = get_object_or_404(CentroCusto, pk=pk)
    if request.method == "POST":
        obj.delete()
        messages.success(request, "Centro de custo removido com sucesso.")
    else:
        messages.error(request, "Ação inválida.")
    return redirect("centrocusto_list")

