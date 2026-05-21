from decimal import Decimal, ROUND_HALF_UP

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.db.models import Q, Count, Sum
from django.db.models.functions import Coalesce
from django.core.paginator import Paginator
from django.template.loader import get_template
from xhtml2pdf import pisa

from ..models import Fornecedor, Item, Licenca, LicencaLote, SimNaoChoices
from ..forms import FornecedorForm

def _get_fornecedores_filtrados(request):
    q = request.GET.get("q", "").strip()
    tem_contrato = request.GET.get("tem_contrato", "").strip()

    qs = Fornecedor.objects.all().order_by("nome")

    if q:
        qs = qs.filter(Q(nome__icontains=q) | Q(cnpj__icontains=q))
    
    if tem_contrato == "sim":
        qs = qs.exclude(contrato__isnull=True).exclude(contrato__exact="")
    elif tem_contrato == "nao":
        qs = qs.filter(Q(contrato__isnull=True) | Q(contrato__exact=""))
    
    return qs, q, tem_contrato

@login_required
def fornecedor_list(request):
    """
    Dashboard de Fornecedores com consolidação de custos:
    - Itens comprados
    - Itens locados
    - Licenças / lotes
    """
    qs, q, tem_contrato = _get_fornecedores_filtrados(request)

    fornecedores_ids = list(qs.values_list("id", flat=True))
    total_filtrado = qs.count()

    def calcular_custos_lote_agregado(lotes):
        mensal_total = Decimal("0.00")
        anual_total = Decimal("0.00")
        qtd_licencas_total = 0

        for lote in lotes:
            periodicidade = str(lote.periodicidade or "").lower()
            qtd_lote = int(lote.quantidade_total or 0)
            custo_ciclo_lote = Decimal(lote.custo_ciclo or 0)

            qtd_licencas_total += qtd_lote

            if qtd_lote <= 0:
                continue

            if periodicidade == "mensal":
                custo_mensal_lote = custo_ciclo_lote
            elif periodicidade == "trimestral":
                custo_mensal_lote = (custo_ciclo_lote / Decimal("3")).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            elif periodicidade == "semestral":
                custo_mensal_lote = (custo_ciclo_lote / Decimal("6")).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            elif periodicidade == "anual":
                custo_mensal_lote = (custo_ciclo_lote / Decimal("12")).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            else:
                custo_mensal_lote = custo_ciclo_lote.quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )

            custo_anual_lote = (custo_mensal_lote * Decimal("12")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )

            mensal_total += custo_mensal_lote
            anual_total += custo_anual_lote

        return {
            "mensal_total": mensal_total,
            "anual_total": anual_total,
            "qtd_licencas_total": qtd_licencas_total,
        }

    # =========================
    # MAPAS DE ITENS
    # =========================
    itens_qs = Item.objects.filter(fornecedor_id__in=fornecedores_ids)

    dados_itens = (
        itens_qs.values("fornecedor")
        .annotate(
            qtd_total=Count("id"),
            valor_total_itens=Coalesce(Sum("valor"), Decimal("0.00")),
            qtd_locados=Count("id", filter=Q(locado="sim")),
            custo_mensal_locacao=Coalesce(
                Sum("locacao__valor_mensal", filter=Q(locado="sim")),
                Decimal("0.00")
            ),
        )
    )

    mapa_itens = {}
    for d in dados_itens:
        mapa_itens[d["fornecedor"]] = {
            "qtd_total": d["qtd_total"] or 0,
            "valor_total_itens": d["valor_total_itens"] or Decimal("0.00"),
            "qtd_locados": d["qtd_locados"] or 0,
            "custo_mensal_locacao": d["custo_mensal_locacao"] or Decimal("0.00"),
        }

    # =========================
    # MAPAS DE LICENÇAS
    # =========================
    licencas_qs = (
        Licenca.objects.filter(fornecedor_id__in=fornecedores_ids)
        .select_related("fornecedor")
    )

    dados_licencas = (
        licencas_qs.values("fornecedor")
        .annotate(qtd_licencas=Count("id"))
    )
    mapa_qtd_licencas = {
        d["fornecedor"]: d["qtd_licencas"] or 0
        for d in dados_licencas
    }

    lotes_qs = (
        LicencaLote.objects.filter(licenca__fornecedor_id__in=fornecedores_ids)
        .select_related("licenca", "licenca__fornecedor")
    )

    mapa_licencas = {}
    mapa_lotes = {}

    for fornecedor_id in fornecedores_ids:
        lotes_fornecedor = [l for l in lotes_qs if l.licenca and l.licenca.fornecedor_id == fornecedor_id]
        custo_lic = calcular_custos_lote_agregado(lotes_fornecedor)

        mapa_licencas[fornecedor_id] = {
            "custo_mensal_licencas": custo_lic["mensal_total"],
            "custo_anual_licencas": custo_lic["anual_total"],
            "qtd_assentos_licencas": custo_lic["qtd_licencas_total"],
        }
        mapa_lotes[fornecedor_id] = len(lotes_fornecedor)

    # =========================
    # KPI GLOBAIS
    # =========================
    kpi_custo_mensal_total = Decimal("0.00")
    kpi_custo_anual_total = Decimal("0.00")
    kpi_total_itens = 0
    kpi_total_licencas = 0

    kpi_top_forn = None
    kpi_top_val = Decimal("0.00")

    # =========================
    # PAGINAÇÃO
    # =========================
    try:
        per_page = int(request.GET.get("pp", 12))
    except ValueError:
        per_page = 12

    paginator = Paginator(qs, per_page)
    page_num = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_num)

    for f in page_obj.object_list:
        item_data = mapa_itens.get(f.id, {})
        lic_data = mapa_licencas.get(f.id, {})

        f.qtd_total_itens_calc = item_data.get("qtd_total", 0)
        f.valor_total_itens_calc = item_data.get("valor_total_itens", Decimal("0.00"))
        f.qtd_locados_calc = item_data.get("qtd_locados", 0)
        f.custo_mensal_itens_calc = item_data.get("custo_mensal_locacao", Decimal("0.00"))

        f.qtd_licencas_calc = mapa_qtd_licencas.get(f.id, 0)
        f.qtd_lotes_calc = mapa_lotes.get(f.id, 0)
        f.qtd_assentos_licencas_calc = lic_data.get("qtd_assentos_licencas", 0)
        f.custo_mensal_licencas_calc = lic_data.get("custo_mensal_licencas", Decimal("0.00"))
        f.custo_anual_licencas_calc = lic_data.get("custo_anual_licencas", Decimal("0.00"))

        # Totais consolidados do fornecedor
        f.custo_total_mensal_calc = (
            f.custo_mensal_itens_calc + f.custo_mensal_licencas_calc
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        f.custo_total_anual_calc = (
            f.valor_total_itens_calc + f.custo_anual_licencas_calc
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    # KPI global com base em TODOS os fornecedores filtrados
    for f in qs:
        item_data = mapa_itens.get(f.id, {})
        lic_data = mapa_licencas.get(f.id, {})

        custo_mensal_itens = item_data.get("custo_mensal_locacao", Decimal("0.00"))
        valor_total_itens = item_data.get("valor_total_itens", Decimal("0.00"))
        custo_mensal_lic = lic_data.get("custo_mensal_licencas", Decimal("0.00"))
        custo_anual_lic = lic_data.get("custo_anual_licencas", Decimal("0.00"))
        qtd_itens = item_data.get("qtd_total", 0)
        qtd_lic = mapa_qtd_licencas.get(f.id, 0)

        total_mensal_forn = (custo_mensal_itens + custo_mensal_lic).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        total_anual_forn = (valor_total_itens + custo_anual_lic).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

        kpi_custo_mensal_total += total_mensal_forn
        kpi_custo_anual_total += total_anual_forn
        kpi_total_itens += qtd_itens
        kpi_total_licencas += qtd_lic

        if total_mensal_forn > kpi_top_val:
            kpi_top_val = total_mensal_forn
            kpi_top_forn = f

    kpi_media = (
        (kpi_custo_mensal_total / total_filtrado).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if total_filtrado > 0 else Decimal("0.00")
    )

    get_copy = request.GET.copy()
    if "page" in get_copy:
        del get_copy["page"]
    qs_keep = get_copy.urlencode()

    context = {
        "fornecedores": page_obj.object_list,
        "page_obj": page_obj,
        "paginator": paginator,
        "total": total_filtrado,
        "qs_keep": qs_keep,

        "f_q": q,
        "f_contrato": tem_contrato,

        "kpi_custo_mensal_total": kpi_custo_mensal_total,
        "kpi_custo_anual_total": kpi_custo_anual_total,
        "kpi_total_itens": kpi_total_itens,
        "kpi_total_licencas": kpi_total_licencas,
        "kpi_media": kpi_media,
        "kpi_top_forn": kpi_top_forn,
        "kpi_top_val": kpi_top_val,
    }

    return render(request, "front/fornecedores/fornecedor_list.html", context)

@login_required
def fornecedor_export_pdf(request):
    qs, q, tem_contrato = _get_fornecedores_filtrados(request)
    
    # Lista completa para o PDF
    fornecedores = list(qs)
    forn_ids = [f.id for f in fornecedores]
    
    # Recalcula dados
    itens_qs = Item.objects.filter(fornecedor_id__in=forn_ids)
    
    # Mapas
    mapa_total = {d["fornecedor"]: d["qtd"] for d in itens_qs.values("fornecedor").annotate(qtd=Count("id"))}
    
    dados_fin = itens_qs.filter(locado="sim").values("fornecedor").annotate(
        custo=Sum("locacao__valor_mensal"), 
        qtd_loc=Count("id")
    )
    mapa_custo = {d["fornecedor"]: d["custo"] or 0 for d in dados_fin}
    mapa_locados = {d["fornecedor"]: d["qtd_loc"] or 0 for d in dados_fin}

    total_geral_custo = 0
    for f in fornecedores:
        f.qtd_total_calc = mapa_total.get(f.id, 0)
        f.qtd_locados_calc = mapa_locados.get(f.id, 0)
        f.custo_calc = mapa_custo.get(f.id, 0)
        total_geral_custo += f.custo_calc

    context = {
        "fornecedores": fornecedores,
        "total_geral_custo": total_geral_custo,
        "filtros": {"Busca": q, "Contrato": tem_contrato},
        "usuario": request.user,
    }
    
    template_path = 'front/fornecedores/fornecedor_pdf.html'
    response = HttpResponse(content_type='application/pdf')
    response['Content-Disposition'] = 'attachment; filename="relatorio_fornecedores.pdf"'

    template = get_template(template_path)
    html = template.render(context)

    pisa_status = pisa.CreatePDF(html, dest=response)
    if pisa_status.err: return HttpResponse('Erro ao gerar PDF', status=500)
    return response

# CREATE
@login_required
def fornecedor_create(request):
    if request.method == "POST":
        form = FornecedorForm(request.POST)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.criado_por = request.user
            obj.atualizado_por = request.user
            obj.save()
            messages.success(request, "Fornecedor criado com sucesso!")
            return redirect("fornecedor_list")
        messages.error(request, "Corrija os erros do formulário.")
    else:
        form = FornecedorForm()

    return render(request, "front/fornecedores/fornecedor_form.html", {"form": form, "editar": False})


# UPDATE
@login_required
def fornecedor_update(request, pk: int):
    obj = get_object_or_404(Fornecedor, pk=pk)
    if request.method == "POST":
        form = FornecedorForm(request.POST, instance=obj)
        if form.is_valid():
            sobj = form.save(commit=False)
            sobj.atualizado_por = request.user
            sobj.save()
            messages.success(request, "Fornecedor atualizado com sucesso!")
            return redirect("fornecedor_list")
        messages.error(request, "Corrija os erros do formulário.")
    else:
        form = FornecedorForm(instance=obj)

    return render(request, "front/fornecedores/fornecedor_form.html", {"form": form, "editar": True})


# DETAIL
@login_required
def fornecedor_detail(request, pk: int):
    """
    Dashboard detalhado do fornecedor com consolidação de:
    - Itens
    - Itens locados
    - Licenças
    - Lotes de licença
    """
    obj = get_object_or_404(Fornecedor, pk=pk)

    def calcular_custos_lote(lote):
        periodicidade = str(lote.periodicidade or "").lower()
        qtd_lote = int(lote.quantidade_total or 0)
        custo_ciclo_lote = Decimal(lote.custo_ciclo or 0)

        if qtd_lote <= 0:
            return {
                "mensal_lote": Decimal("0.00"),
                "anual_lote": Decimal("0.00"),
                "mensal_unit": Decimal("0.00"),
                "anual_unit": Decimal("0.00"),
            }

        if periodicidade == "mensal":
            custo_mensal_lote = custo_ciclo_lote
        elif periodicidade == "trimestral":
            custo_mensal_lote = (custo_ciclo_lote / Decimal("3")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        elif periodicidade == "semestral":
            custo_mensal_lote = (custo_ciclo_lote / Decimal("6")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        elif periodicidade == "anual":
            custo_mensal_lote = (custo_ciclo_lote / Decimal("12")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        else:
            custo_mensal_lote = custo_ciclo_lote.quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )

        custo_anual_lote = (custo_mensal_lote * Decimal("12")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

        mensal_unit = (custo_mensal_lote / Decimal(qtd_lote)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        anual_unit = (custo_anual_lote / Decimal(qtd_lote)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

        return {
            "mensal_lote": custo_mensal_lote,
            "anual_lote": custo_anual_lote,
            "mensal_unit": mensal_unit,
            "anual_unit": anual_unit,
        }

    # =========================
    # ITENS
    # =========================
    itens_qs = (
        Item.objects
        .filter(fornecedor=obj)
        .select_related("subtipo", "localidade", "centro_custo", "locacao")
        .order_by("-created_at")
    )

    total_itens = itens_qs.count()
    locados_qs = itens_qs.filter(locado=SimNaoChoices.SIM)
    qtd_locados = locados_qs.count()

    custo_mensal_itens = locados_qs.aggregate(
        total=Sum("locacao__valor_mensal")
    )["total"] or Decimal("0.00")

    valor_patrimonial = itens_qs.exclude(
        locado=SimNaoChoices.SIM
    ).aggregate(
        total=Sum("valor")
    )["total"] or Decimal("0.00")

    # =========================
    # LICENÇAS
    # =========================
    licencas_qs = (
        Licenca.objects
        .filter(fornecedor=obj)
        .select_related("centro_custo")
        .order_by("nome")
    )

    total_licencas = licencas_qs.count()

    # =========================
    # LOTES DE LICENÇA
    # =========================
    lotes_qs = (
        LicencaLote.objects
        .filter(licenca__fornecedor=obj)
        .select_related("licenca", "centro_custo", "fornecedor")
        .order_by("-data_compra", "-id")
    )

    total_lotes = lotes_qs.count()
    total_assentos_licencas = 0
    custo_mensal_licencas = Decimal("0.00")
    custo_anual_licencas = Decimal("0.00")

    for lote in lotes_qs:
        custos = calcular_custos_lote(lote)

        lote.custo_mensal_calc = custos["mensal_lote"]
        lote.custo_anual_calc = custos["anual_lote"]
        lote.custo_mensal_unit_calc = custos["mensal_unit"]
        lote.custo_anual_unit_calc = custos["anual_unit"]

        total_assentos_licencas += int(lote.quantidade_total or 0)
        custo_mensal_licencas += custos["mensal_lote"]
        custo_anual_licencas += custos["anual_lote"]

    custo_total_mensal = (custo_mensal_itens + custo_mensal_licencas).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    custo_total_anual = (valor_patrimonial + custo_anual_licencas).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )

    context = {
        "obj": obj,
        "itens_fornecidos": itens_qs,
        "licencas_fornecidas": licencas_qs,
        "lotes_licencas": lotes_qs,
        "kpi": {
            "total_itens": total_itens,
            "qtd_locados": qtd_locados,
            "custo_mensal_itens": custo_mensal_itens,
            "valor_aquisicao": valor_patrimonial,
            "total_licencas": total_licencas,
            "total_lotes": total_lotes,
            "total_assentos_licencas": total_assentos_licencas,
            "custo_mensal_licencas": custo_mensal_licencas,
            "custo_anual_licencas": custo_anual_licencas,
            "custo_total_mensal": custo_total_mensal,
            "custo_total_anual": custo_total_anual,
        }
    }

    return render(request, "front/fornecedores/fornecedor_detail.html", context)


# DELETE (POST via modal)
@login_required
def fornecedor_delete(request, pk: int):
    obj = get_object_or_404(Fornecedor, pk=pk)
    if request.method == "POST":
        obj.delete()
        messages.success(request, "Fornecedor removido com sucesso.")
    else:
        messages.error(request, "Ação inválida.")
    return redirect("fornecedor_list")


############### LOCALIDADE ##############################
