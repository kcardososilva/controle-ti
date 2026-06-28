from decimal import Decimal

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.http import HttpResponse
from django.db.models import Q, Count, Sum
from django.core.paginator import Paginator
from django.template.loader import get_template
from django.utils import timezone
from xhtml2pdf import pisa
from django.db.models import OuterRef, Subquery, Sum

from ..models import CentroCusto, Item, SimNaoChoices, Usuario
from ..forms import CentroCustoForm

from ..models import (
    CentroCusto,
    Item,
    MovimentacaoItem,
    MovimentacaoLicenca,
    SimNaoChoices,
    TipoMovLicencaChoices,
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


def _custos_licenca_baixa_por_centro(cc_ids, dt_ini, dt_fim):
    """
    Custo de LICENÇAS (software, recorrente mensal) e de BAIXAS (consumo pontual no
    período) por centro de custo — reutiliza EXATAMENTE a regra do dashboard de
    Custos por Setor (``dashboards._get_cc_custos_data``):

      · Licenças: estado ATUAL de cada par (licença, colaborador) — última
        movimentação; conta apenas as que estão como ATRIBUIÇÃO (ativas). Custo =
        custo mensal unitário do lote. Atribuído ao CC do colaborador (fallback:
        centro_custo_destino → CC dono da licença).
      · Baixas: ``MovimentacaoItem`` do tipo BAIXA no período ``[dt_ini, dt_fim]``,
        atribuídas ao ``centro_custo_destino`` — o setor que CONSUMIU o item (ex.:
        toner dado baixa do estoque do TI mas consumido por outro setor). Fallback:
        ``centro_custo_origem`` → CC do item. Custo = ``mov.custo`` (fallback
        ``item.valor * quantidade``).

    Retorna ``{cc_id: {"custo_licencas", "qtd_licencas", "custo_baixas",
    "qtd_baixas", "custo_baixas_acum", "qtd_baixas_acum"}}``. O custo de baixas é
    devolvido em duas dimensões: ``custo_baixas`` (apenas o período ``[dt_ini,
    dt_fim]``, normalmente o mês corrente) e ``custo_baixas_acum`` (todas as
    baixas já realizadas para o centro, sem recorte de data).
    """
    from .dashboards import _calcular_custo_mensal_unitario_lote

    cc_set = {cc for cc in cc_ids if cc}
    out = {
        cc: {
            "custo_licencas": Decimal("0.00"),
            "qtd_licencas": 0,
            "custo_baixas": Decimal("0.00"),
            "qtd_baixas": 0,
            "custo_baixas_acum": Decimal("0.00"),
            "qtd_baixas_acum": 0,
        }
        for cc in cc_set
    }
    if not cc_set:
        return out

    # 1. Licenças — estado atual por (licença, colaborador)
    movs_lic = (
        MovimentacaoLicenca.objects
        .select_related("licenca", "usuario__centro_custo", "centro_custo_destino", "lote")
        .filter(usuario__isnull=False)
        .order_by("licenca_id", "usuario_id", "created_at")
    )
    estado_atual = {}
    for mov in movs_lic:
        estado_atual[(mov.licenca_id, mov.usuario_id)] = mov

    for mov in estado_atual.values():
        if mov.tipo != TipoMovLicencaChoices.ATRIBUICAO:
            continue
        cc_id = None
        if mov.usuario and mov.usuario.centro_custo_id:
            cc_id = mov.usuario.centro_custo_id
        elif mov.centro_custo_destino_id:
            cc_id = mov.centro_custo_destino_id
        elif mov.licenca and getattr(mov.licenca, "centro_custo_id", None):
            cc_id = mov.licenca.centro_custo_id
        if cc_id not in cc_set:
            continue

        if mov.lote:
            custo = _calcular_custo_mensal_unitario_lote(mov.lote)
        else:
            custo = Decimal(getattr(mov.licenca, "custo", 0) or 0)

        out[cc_id]["custo_licencas"] += custo
        out[cc_id]["qtd_licencas"] += 1

    # 2. Baixas — consumo pontual. Calcula o ACUMULADO (todas as baixas) e, em
    #    paralelo, o recorte do período (mês corrente). Uma única varredura.
    baixas = (
        MovimentacaoItem.objects
        .filter(tipo_movimentacao=TipoMovimentacaoChoices.BAIXA)
        .select_related("item__centro_custo", "centro_custo_origem", "centro_custo_destino")
    )
    for b in baixas:
        # O custo da baixa recai sobre o setor que consumiu o item (destino).
        # Fallback: origem (estoque) → CC cadastrado do item.
        cc_id = (
            b.centro_custo_destino_id
            or b.centro_custo_origem_id
            or (b.item.centro_custo_id if b.item else None)
        )
        if cc_id not in cc_set:
            continue
        custo_baixa = b.custo
        if custo_baixa is None:
            custo_baixa = Decimal(b.item.valor or 0) * Decimal(b.quantidade or 1)
        custo_baixa = Decimal(custo_baixa or 0)

        out[cc_id]["custo_baixas_acum"] += custo_baixa
        out[cc_id]["qtd_baixas_acum"] += 1

        data_baixa = b.created_at.date() if b.created_at else None
        if data_baixa and dt_ini <= data_baixa <= dt_fim:
            out[cc_id]["custo_baixas"] += custo_baixa
            out[cc_id]["qtd_baixas"] += 1

    return out


def _custo_mensal_colaboradores(usuario_ids):
    """
    Custo mensal recorrente atribuível a cada colaborador — mesmo conceito de
    "burn rate" usado no dashboard de usuários:

      custo_mensal = licenças ativas (custo mensal unitário do lote)
                   + equipamentos LOCADOS sob posse atual do colaborador
                     (``Locacao.valor_mensal``).

    A posse do equipamento é resolvida pela última movimentação de
    transferência do item (ignorada quando a última for devolução), avaliada
    globalmente — assim um item que saiu do colaborador para outra pessoa não é
    contabilizado indevidamente.

    Retorna ``{usuario_id: {"lic", "loc", "qtd_lic", "qtd_itens"}}``.
    """
    from .dashboards import _calcular_custo_mensal_unitario_lote

    ids = {u for u in usuario_ids if u}
    out = {
        u: {"lic": Decimal("0.00"), "loc": Decimal("0.00"), "qtd_lic": 0, "qtd_itens": 0}
        for u in ids
    }
    if not ids:
        return out

    # 1. Licenças — estado atual por (licença, colaborador), só ATRIBUIÇÃO.
    movs_lic = (
        MovimentacaoLicenca.objects
        .filter(usuario_id__in=ids)
        .select_related("licenca", "lote")
        .order_by("licenca_id", "usuario_id", "created_at")
    )
    estado_lic = {}
    for mov in movs_lic:
        estado_lic[(mov.licenca_id, mov.usuario_id)] = mov

    for mov in estado_lic.values():
        if mov.tipo != TipoMovLicencaChoices.ATRIBUICAO:
            continue
        if mov.usuario_id not in ids:
            continue
        if mov.lote:
            custo = _calcular_custo_mensal_unitario_lote(mov.lote)
        else:
            custo = Decimal(getattr(mov.licenca, "custo", 0) or 0)
        out[mov.usuario_id]["lic"] += custo
        out[mov.usuario_id]["qtd_lic"] += 1

    # 2. Equipamentos locados — detentor atual (última transferência relevante).
    itens_loc_ids = list(
        Item.objects.filter(locado=SimNaoChoices.SIM).values_list("id", flat=True)
    )
    if itens_loc_ids:
        movs_item = (
            MovimentacaoItem.objects
            .filter(
                item_id__in=itens_loc_ids,
                usuario__isnull=False,
                tipo_movimentacao__in=[
                    TipoMovimentacaoChoices.TRANSFERENCIA,
                    TipoMovimentacaoChoices.TRANSFERENCIA_EQUIPAMENTO,
                ],
            )
            .select_related("item__locacao")
            .order_by("item_id", "-created_at", "-id")
        )
        vistos = set()
        for mov in movs_item:
            if mov.item_id in vistos:
                continue
            vistos.add(mov.item_id)
            if mov.tipo_transferencia == TipoTransferenciaChoices.DEVOLUCAO:
                continue
            if mov.usuario_id not in ids:
                continue
            locacao = getattr(mov.item, "locacao", None)
            valor_mensal = Decimal(getattr(locacao, "valor_mensal", 0) or 0) if locacao else Decimal("0.00")
            out[mov.usuario_id]["loc"] += valor_mensal
            out[mov.usuario_id]["qtd_itens"] += 1

    return out


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

    # Métricas operacionais por centro: total de itens e de colaboradores vinculados.
    mapa_itens = dict(
        Item.objects.filter(centro_custo_id__in=centros_ids)
        .values_list("centro_custo")
        .annotate(qtd=Count("id"))
    )
    mapa_colabs = dict(
        Usuario.objects.filter(centro_custo_id__in=centros_ids)
        .values_list("centro_custo")
        .annotate(qtd=Count("id"))
    )
    kpi_total_itens = sum(mapa_itens.values())
    kpi_total_colabs = sum(mapa_colabs.values())

    # Custo de licenças (mensal) e baixas (mês corrente) por centro — mesma regra
    # do dashboard de Custos por Setor.
    hoje = timezone.localdate()
    dt_ini = hoje.replace(day=1)
    dt_fim = hoje
    extras = _custos_licenca_baixa_por_centro(centros_ids, dt_ini, dt_fim)
    mapa_licencas = {cc: v["custo_licencas"] for cc, v in extras.items()}
    mapa_qtd_licencas = {cc: v["qtd_licencas"] for cc, v in extras.items()}
    mapa_baixas = {cc: v["custo_baixas"] for cc, v in extras.items()}
    mapa_baixas_acum = {cc: v["custo_baixas_acum"] for cc, v in extras.items()}

    kpi_licencas_total = sum(mapa_licencas.values()) or Decimal("0.00")
    kpi_baixas_total = sum(mapa_baixas.values()) or Decimal("0.00")
    kpi_baixas_acum_total = sum(mapa_baixas_acum.values()) or Decimal("0.00")
    kpi_total_mes = kpi_custo_total + kpi_licencas_total + kpi_baixas_total

    # Top Offensor (por impacto total no mês: locação + licenças + baixas)
    mapa_impacto = {
        cc_id: (mapa_custo.get(cc_id, 0) or 0) + mapa_licencas.get(cc_id, 0) + mapa_baixas.get(cc_id, 0)
        for cc_id in centros_ids
    }
    kpi_top_cc = None
    kpi_top_val = 0
    if mapa_impacto:
        top_id = max(mapa_impacto, key=mapa_impacto.get)
        kpi_top_val = mapa_impacto[top_id]
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
        cc.total_itens = mapa_itens.get(cc.id, 0)
        cc.total_colabs = mapa_colabs.get(cc.id, 0)
        cc.custo_licencas = mapa_licencas.get(cc.id, Decimal("0.00"))
        cc.qtd_licencas = mapa_qtd_licencas.get(cc.id, 0)
        cc.custo_baixas = mapa_baixas.get(cc.id, Decimal("0.00"))
        cc.custo_baixas_acum = mapa_baixas_acum.get(cc.id, Decimal("0.00"))
        cc.custo_total_mes = (cc.custo_calc or 0) + cc.custo_licencas + cc.custo_baixas

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
        "kpi_total_itens": kpi_total_itens,
        "kpi_total_colabs": kpi_total_colabs,
        "kpi_licencas_total": kpi_licencas_total,
        "kpi_baixas_total": kpi_baixas_total,
        "kpi_baixas_acum_total": kpi_baixas_acum_total,
        "kpi_total_mes": kpi_total_mes,
        "periodo_ini": dt_ini,
        "periodo_fim": dt_fim,
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

        # Tipo operacional do item — mesma classificação usada nas telas de equipamentos.
        if getattr(item, "item_consumo", "nao") == SimNaoChoices.SIM:
            item.tipo_op_label, item.tipo_op_class = "Consumo", "tp-consumo"
        elif getattr(item, "locado", "nao") == SimNaoChoices.SIM:
            item.tipo_op_label, item.tipo_op_class = "Locado", "tp-locado"
        elif getattr(item, "tem_lote", False):
            item.tipo_op_label, item.tipo_op_class = "Com lote", "tp-lote"
        else:
            item.tipo_op_label, item.tipo_op_class = "Patrimônio", "tp-patrimonio"

    # Colaboradores que pertencem a ESTE centro de custo (ativos primeiro).
    colaboradores_list = list(
        Usuario.objects
        .filter(centro_custo=obj)
        .select_related("funcao", "localidade")
        .order_by("status", "nome")   # 'ativo' < 'desligado' → ativos no topo
    )

    # Custo mensal recorrente por colaborador (licenças + locação de equipamentos).
    custo_colabs = _custo_mensal_colaboradores([c.pk for c in colaboradores_list])
    total_custo_colabs = Decimal("0.00")
    for colab in colaboradores_list:
        info = custo_colabs.get(colab.pk, {})
        colab.custo_lic = info.get("lic") or Decimal("0.00")
        colab.custo_loc = info.get("loc") or Decimal("0.00")
        colab.qtd_lic = info.get("qtd_lic", 0)
        colab.qtd_itens = info.get("qtd_itens", 0)
        colab.custo_mensal = colab.custo_lic + colab.custo_loc
        if colab.status == "ativo":
            total_custo_colabs += colab.custo_mensal

    total_itens = len(itens_cc)
    total_colaboradores = len(colaboradores_list)
    total_colaboradores_ativos = sum(1 for c in colaboradores_list if c.status == "ativo")

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

    # Custo de licenças (mensal) e baixas (mês corrente) — mesma regra do dashboard
    # de Custos por Setor.
    hoje = timezone.localdate()
    dt_ini = hoje.replace(day=1)
    dt_fim = hoje
    extras = _custos_licenca_baixa_por_centro([obj.pk], dt_ini, dt_fim).get(
        obj.pk, {"custo_licencas": Decimal("0.00"), "qtd_licencas": 0, "custo_baixas": Decimal("0.00")}
    )
    custo_licencas = extras["custo_licencas"]
    custo_baixas = extras["custo_baixas"]
    custo_baixas_acum = extras["custo_baixas_acum"]
    custo_total_mes = custo_mensal + custo_licencas + custo_baixas

    context = {
        "obj": obj,
        "itens_cc": itens_cc,
        "colaboradores": colaboradores_list,
        "periodo_ini": dt_ini,
        "periodo_fim": dt_fim,
        "kpi": {
            "total_itens": total_itens,
            "total_itens_com_usuario": total_itens_com_usuario,
            "total_colaboradores": total_colaboradores,
            "total_colaboradores_ativos": total_colaboradores_ativos,
            "custo_mensal": custo_mensal,
            "custo_licencas": custo_licencas,
            "qtd_licencas": extras["qtd_licencas"],
            "custo_baixas": custo_baixas,
            "qtd_baixas": extras["qtd_baixas"],
            "custo_baixas_acum": custo_baixas_acum,
            "qtd_baixas_acum": extras["qtd_baixas_acum"],
            "custo_colaboradores": total_custo_colabs,
            "custo_total_mes": custo_total_mes,
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

