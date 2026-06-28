"""
Portal do Fornecedor — área isolada (sandbox) para fornecedores externos.

Camadas de isolamento (defesa em profundidade — ver CLAUDE.md):
  1. FornecedorAccessMiddleware  — restringe o grupo "Fornecedor" a /portal/
  2. @fornecedor_required        — resolve o Fornecedor do request ou 403
  3. itens_do_fornecedor(...)    — TODA query parte daqui (nunca Item.objects.all())

v1: somente visão de equipamentos + status.
Fluxo de manutenção e licenças entram em fases seguintes.
"""
from decimal import Decimal
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q, Count, Sum
from django.db.models.functions import Coalesce
from django.http import HttpResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.utils import timezone

from ..models import (
    Item,
    Categoria,
    Localidade,
    Licenca,
    OrdemManutencao,
    OrdemManutencaoAnexo,
    StatusItemChoices,
    StatusOrdemManutencaoChoices,
    TipoMovimentacaoChoices,
)


# ─── Helpers de segurança ─────────────────────────────────────────────────────

def fornecedor_do_request(request):
    """Retorna o Fornecedor vinculado ao usuário logado (perfil ativo), ou None."""
    perfil = getattr(request.user, "perfil_fornecedor", None)
    if perfil is not None and perfil.ativo:
        return perfil.fornecedor
    return None


def fornecedor_required(view_func):
    """
    Garante que o request tem um Fornecedor ativo vinculado e injeta
    `request.fornecedor`. Deve decorar TODA view do portal.

    Quando o usuário está no grupo "Fornecedor" mas ainda não tem um
    PerfilFornecedor ativo, mostra uma página orientativa (em vez de um 403
    cru) pedindo para o TI concluir a configuração do acesso.
    """
    @wraps(view_func)
    @login_required
    def _wrapped(request, *args, **kwargs):
        fornecedor = fornecedor_do_request(request)
        if fornecedor is None:
            return render(request, "front/portal/portal_sem_acesso.html", status=403)
        request.fornecedor = fornecedor
        return view_func(request, *args, **kwargs)
    return _wrapped


def itens_do_fornecedor(fornecedor):
    """
    Núcleo de segurança: o conjunto de itens visíveis a um fornecedor.
      • itens fornecidos por ele (Item.fornecedor)
      • itens enviados para manutenção sob sua responsabilidade
        (MovimentacaoItem.fornecedor_manutencao em um envio_manutencao)
    Toda view do portal DEVE partir desta função — nunca de Item.objects.all().
    """
    return (
        Item.objects
        .filter(
            Q(fornecedor=fornecedor)
            | Q(
                movimentacoes__tipo_movimentacao=TipoMovimentacaoChoices.ENVIO_MANUTENCAO,
                movimentacoes__fornecedor_manutencao=fornecedor,
            )
        )
        .distinct()
    )


# Ordem de exibição dos status nos cards do painel.
_STATUS_ORDEM = [
    StatusItemChoices.ATIVO,
    StatusItemChoices.MANUTENCAO,
    StatusItemChoices.DEFEITO,
    StatusItemChoices.BACKUP,
    StatusItemChoices.PAUSADO,
]


# ─── Views ────────────────────────────────────────────────────────────────────

@fornecedor_required
def portal_home(request):
    """Painel inicial do fornecedor: KPIs de equipamentos por status."""
    qs = itens_do_fornecedor(request.fornecedor)

    counts = {
        row["status"]: row["n"]
        for row in qs.values("status").annotate(n=Count("id"))
    }
    status_cards = [
        {
            "slug": s.value,
            "label": s.label,
            "count": counts.get(s.value, 0),
        }
        for s in _STATUS_ORDEM
    ]

    total = qs.count()

    recentes = (
        qs.select_related("subtipo", "localidade").order_by("-updated_at")[:6]
    )

    # Resumo de manutenção do fornecedor
    SOM = StatusOrdemManutencaoChoices
    os_qs = OrdemManutencao.objects.filter(fornecedor=request.fornecedor)
    qtd_os_abertas = os_qs.exclude(status__in=[SOM.CONCLUIDO, SOM.CANCELADO]).count()
    qtd_os_acao = os_qs.filter(status__in=[
        SOM.AGUARDANDO_RECEBIMENTO, SOM.RECEBIDO, SOM.EM_AVALIACAO,
        SOM.EM_REPARO, SOM.REPARADO, SOM.SEM_REPARO,
    ]).count()

    context = {
        "fornecedor": request.fornecedor,
        "total_itens": total,
        "status_cards": status_cards,
        "recentes": recentes,
        "qtd_os_abertas": qtd_os_abertas,
        "qtd_os_acao": qtd_os_acao,
        "active_nav": "home",
    }
    return render(request, "front/portal/portal_home.html", context)


def _portal_itens_filtrados(request):
    """Aplica busca + filtros (status, localidade, categoria) ao escopo do fornecedor.
    Usado pela listagem e pela exportação para garantir o mesmo resultado."""
    qs = (
        itens_do_fornecedor(request.fornecedor)
        .select_related("subtipo", "subtipo__categoria", "localidade")
        .order_by("nome")
    )

    q = request.GET.get("q", "").strip()
    marca = request.GET.get("marca", "").strip()
    modelo = request.GET.get("modelo", "").strip()
    serie = request.GET.get("serie", "").strip()
    status = request.GET.get("status", "").strip()
    localidade = request.GET.get("localidade", "").strip()
    categoria = request.GET.get("categoria", "").strip()

    if q:
        qs = qs.filter(nome__icontains=q)
    if marca:
        qs = qs.filter(marca__icontains=marca)
    if modelo:
        qs = qs.filter(modelo__icontains=modelo)
    if serie:
        qs = qs.filter(numero_serie__icontains=serie)
    if status:
        qs = qs.filter(status=status)
    if localidade.isdigit():
        qs = qs.filter(localidade_id=int(localidade))
    if categoria.isdigit():
        qs = qs.filter(subtipo__categoria_id=int(categoria))

    filtros = {
        "q": q, "marca": marca, "modelo": modelo, "serie": serie,
        "status": status, "localidade": localidade, "categoria": categoria,
    }
    return qs, filtros


@fornecedor_required
def portal_equipamentos_list(request):
    """Lista de equipamentos do fornecedor — busca + filtros separados + export."""
    qs, filtros = _portal_itens_filtrados(request)

    base = itens_do_fornecedor(request.fornecedor)
    localidades = Localidade.objects.filter(id__in=base.values("localidade")).order_by("local")
    categorias = Categoria.objects.filter(id__in=base.values("subtipo__categoria")).order_by("nome")

    paginator = Paginator(qs, 15)
    page_obj = paginator.get_page(request.GET.get("page", 1))

    get_copy = request.GET.copy()
    get_copy.pop("page", None)
    qs_keep = get_copy.urlencode()

    context = {
        "fornecedor": request.fornecedor,
        "page_obj": page_obj,
        "total": paginator.count,
        "f_q": filtros["q"],
        "f_marca": filtros["marca"],
        "f_modelo": filtros["modelo"],
        "f_serie": filtros["serie"],
        "f_status": filtros["status"],
        "f_localidade": filtros["localidade"],
        "f_categoria": filtros["categoria"],
        "tem_filtro": any(filtros.values()),
        "status_choices": StatusItemChoices.choices,
        "localidades": localidades,
        "categorias": categorias,
        "qs_keep": qs_keep,
        "active_nav": "equipamentos",
    }
    return render(request, "front/portal/portal_equipamentos_list.html", context)


@fornecedor_required
def portal_equipamentos_export(request):
    """Exporta para Excel os equipamentos do fornecedor conforme os filtros aplicados."""
    qs, _ = _portal_itens_filtrados(request)
    return _portal_itens_xlsx(list(qs), request.fornecedor)


def _portal_itens_xlsx(itens, fornecedor):
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
    from openpyxl.utils import get_column_letter

    BRAND_DARK, BRAND, SOFT, ZEBRA, INK = "0B3D6E", "0071E3", "E5F0FB", "F4F9FE", "1F2733"
    hair = Side(style="thin", color="CFE0F2")
    border = Border(left=hair, right=hair, top=hair, bottom=hair)
    f_title = Font(name="Calibri", size=18, bold=True, color="FFFFFF")
    f_sub = Font(name="Calibri", size=10, italic=True, color="5B6B7F")
    f_header = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
    f_cell = Font(name="Calibri", size=10, color=INK)
    fill_title = PatternFill("solid", fgColor=BRAND_DARK)
    fill_sub = PatternFill("solid", fgColor=SOFT)
    fill_header = PatternFill("solid", fgColor=BRAND)
    fill_zebra = PatternFill("solid", fgColor=ZEBRA)
    a_center = Alignment(horizontal="center", vertical="center")
    a_left = Alignment(horizontal="left", vertical="center", indent=1)

    STATUS_FILL = {
        "ativo": ("E6F4EA", "1E8E3E"), "manutencao": ("FEF1E0", "B35A00"),
        "defeito": ("FCE8E6", "D93025"), "backup": ("E5F0FB", "0B5BB5"),
        "pausado": ("F0F0F2", "5B6B7F"),
    }

    header = ["#", "Equipamento", "Nº Série", "Marca", "Modelo", "Categoria", "Tipo", "Localidade", "Status"]
    ncols = len(header)
    center_cols = {1, 9}

    wb = Workbook()
    ws = wb.active
    ws.title = "Equipamentos"
    ws.sheet_view.showGridLines = False

    last = get_column_letter(ncols)
    ws.merge_cells(f"A1:{last}1")
    c = ws["A1"]; c.value = "MEUS EQUIPAMENTOS"; c.font = f_title; c.fill = fill_title; c.alignment = a_left
    ws.row_dimensions[1].height = 34
    gerado = timezone.localtime().strftime("%d/%m/%Y às %H:%M")
    ws.merge_cells(f"A2:{last}2")
    c2 = ws["A2"]
    c2.value = f"{fornecedor.nome}  ·  {len(itens)} equipamento(s)  ·  gerado em {gerado}"
    c2.font = f_sub; c2.fill = fill_sub; c2.alignment = a_left
    ws.row_dimensions[2].height = 18

    HEADER_ROW = 3
    for ci, h in enumerate(header, 1):
        cc = ws.cell(row=HEADER_ROW, column=ci, value=h)
        cc.fill = fill_header; cc.font = f_header; cc.border = border
        cc.alignment = a_center if ci in center_cols else a_left
    ws.row_dimensions[HEADER_ROW].height = 26

    row = HEADER_ROW + 1
    for i, it in enumerate(itens, start=1):
        categoria = it.subtipo.categoria.nome if (it.subtipo and it.subtipo.categoria) else ""
        tipo = it.subtipo.nome if it.subtipo else ""
        local = it.localidade.local if it.localidade else ""
        valores = [i, it.nome, it.numero_serie or "", it.marca or "", it.modelo or "",
                   categoria, tipo, local, it.get_status_display()]
        zebra = (i % 2 == 0)
        for ci, val in enumerate(valores, 1):
            cell = ws.cell(row=row, column=ci, value=val)
            cell.border = border; cell.font = f_cell
            cell.alignment = a_center if ci in center_cols else a_left
            if ci == 9:
                bg, fg = STATUS_FILL.get(it.status, ("F0F0F2", "5B6B7F"))
                cell.fill = PatternFill("solid", fgColor=bg)
                cell.font = Font(name="Calibri", size=10, bold=True, color=fg)
                cell.alignment = a_center
            elif zebra:
                cell.fill = fill_zebra
        row += 1

    ws.freeze_panes = f"A{HEADER_ROW + 1}"
    ws.auto_filter.ref = f"A{HEADER_ROW}:{last}{max(row - 1, HEADER_ROW)}"

    widths = {}
    for r_ in ws.iter_rows(min_row=HEADER_ROW, values_only=True):
        for idx, val in enumerate(r_, start=1):
            widths[idx] = max(widths.get(idx, 0), len(str(val)) if val is not None else 0)
    for idx, w in widths.items():
        ws.column_dimensions[get_column_letter(idx)].width = min(max(w + 2, 11), 46)

    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    now = timezone.localtime().strftime("%Y%m%d-%H%M%S")
    resp = HttpResponse(
        buffer.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    resp["Content-Disposition"] = f'attachment; filename="meus_equipamentos_{now}.xlsx"'
    return resp


@fornecedor_required
def portal_equipamento_detail(request, pk: int):
    """Ficha read-only de um equipamento — 404 se não pertencer ao fornecedor."""
    item = get_object_or_404(
        itens_do_fornecedor(request.fornecedor).select_related(
            "subtipo", "subtipo__categoria", "localidade"
        ),
        pk=pk,
    )

    manutencoes = (
        item.movimentacoes
        .filter(
            tipo_movimentacao__in=[
                TipoMovimentacaoChoices.ENVIO_MANUTENCAO,
                TipoMovimentacaoChoices.RETORNO_MANUTENCAO,
            ]
        )
        .order_by("-created_at")[:20]
    )

    # OS de manutenção aberta para este item sob responsabilidade do fornecedor
    os_aberta = (
        OrdemManutencao.objects
        .filter(item=item, fornecedor=request.fornecedor)
        .exclude(status__in=[
            StatusOrdemManutencaoChoices.CONCLUIDO,
            StatusOrdemManutencaoChoices.CANCELADO,
        ])
        .order_by("-created_at")
        .first()
    )

    # Histórico de locação (congelável por status) — registro do fornecedor.
    loc_periodos = list(item.locacao_periodos.all()) if item.eh_locado else []
    loc_total = sum((p.valor_acumulado for p in loc_periodos), Decimal("0.00"))
    loc_atual = next((p for p in loc_periodos if p.em_andamento), None)

    context = {
        "fornecedor": request.fornecedor,
        "item": item,
        "manutencoes": manutencoes,
        "os_aberta": os_aberta,
        "loc_periodos": loc_periodos,
        "loc_total": loc_total,
        "loc_atual": loc_atual,
        "loc_congelado": item.eh_locado and loc_atual is None,
        "active_nav": "equipamentos",
    }
    return render(request, "front/portal/portal_equipamento_detail.html", context)


# ─── Manutenção (Ordens de Serviço conduzidas pelo fornecedor) ────────────────

_OS_ABERTAS_EXCLUI = [
    StatusOrdemManutencaoChoices.CONCLUIDO,
    StatusOrdemManutencaoChoices.CANCELADO,
]


@fornecedor_required
def portal_manutencao_list(request):
    """Fila de ordens de manutenção do fornecedor (abertas + histórico)."""
    base = (
        OrdemManutencao.objects
        .filter(fornecedor=request.fornecedor)
        .select_related("item", "item_substituto")
    )
    abertas = base.exclude(status__in=_OS_ABERTAS_EXCLUI)
    historico = base.filter(status__in=_OS_ABERTAS_EXCLUI)[:50]

    # Resumo por status (somente os que têm ordens) — melhora a visão geral.
    counts = {row["status"]: row["n"] for row in base.values("status").annotate(n=Count("id"))}
    resumo_status = [
        {"slug": s.value, "label": s.label, "count": counts.get(s.value, 0)}
        for s in StatusOrdemManutencaoChoices
        if counts.get(s.value, 0)
    ]

    context = {
        "fornecedor": request.fornecedor,
        "abertas": abertas,
        "historico": historico,
        "qtd_abertas": abertas.count(),
        "resumo_status": resumo_status,
        "total_os": base.count(),
        "active_nav": "manutencao",
    }
    return render(request, "front/portal/portal_manutencao_list.html", context)


@fornecedor_required
def portal_manutencao_detail(request, pk: int):
    """Detalhe da OS + ações de transição do fornecedor."""
    ordem = get_object_or_404(
        OrdemManutencao.objects.select_related(
            "item", "item__subtipo", "item_substituto", "fornecedor", "movimentacao_origem",
        ),
        pk=pk,
        fornecedor=request.fornecedor,
    )

    if request.method == "POST":
        acao = request.POST.get("acao", "")

        # ── Upload de Nota Fiscal (o fornecedor pode anexar quantas quiser) ──
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
                        origem=OrdemManutencaoAnexo.OrigemAnexo.FORNECEDOR,
                        descricao=descricao,
                        criado_por=request.user,
                        atualizado_por=request.user,
                    )
                messages.success(request, f"{len(arquivos)} nota(s) fiscal(is) anexada(s).")
            return redirect("portal_manutencao_detail", pk=pk)

        # ── Transição de status conduzida pelo fornecedor ────────────────────
        from services.ordem_manutencao_service import OrdemManutencaoService
        extra = {
            "diagnostico": request.POST.get("diagnostico", ""),
            "nome": request.POST.get("nome", ""),
            "numero_serie": request.POST.get("numero_serie", ""),
            "marca": request.POST.get("marca", ""),
            "modelo": request.POST.get("modelo", ""),
            "contrato": request.POST.get("contrato", ""),
            "valor": request.POST.get("valor", ""),
            "data": request.POST.get("data", ""),
            "locado": request.POST.get("locado", ""),
            "localidade_devolucao": request.POST.get("localidade_devolucao", ""),
            "localidade_substituto": request.POST.get("localidade_substituto", ""),
            "reparo_valor": request.POST.get("reparo_valor", ""),
            "valor_orcamento": request.POST.get("valor_orcamento", ""),
            "valor_conserto": request.POST.get("valor_conserto", ""),
            "valor_total": request.POST.get("valor_total", ""),
            "valor_avaliacao_tecnica": request.POST.get("valor_avaliacao_tecnica", ""),
        }
        try:
            OrdemManutencaoService.transicionar(
                ordem=ordem,
                novo_status=acao,
                user=request.user,
                observacao=request.POST.get("observacao", ""),
                ator="fornecedor",
                **extra,
            )
            messages.success(request, "Status atualizado com sucesso.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        return redirect("portal_manutencao_detail", pk=pk)

    eventos = ordem.eventos.select_related("criado_por").all()
    anexos = ordem.anexos.select_related("criado_por").all()

    context = {
        "fornecedor": request.fornecedor,
        "ordem": ordem,
        "eventos": eventos,
        "anexos": anexos,
        "localidades": Localidade.objects.order_by("local"),
        "active_nav": "manutencao",
    }
    return render(request, "front/portal/portal_manutencao_detail.html", context)


# ─── Licenças (somente leitura) ───────────────────────────────────────────────

@fornecedor_required
def portal_licencas_list(request):
    """Licenças vinculadas ao fornecedor — somente leitura, com agregados de lotes."""
    licencas = (
        Licenca.objects
        .filter(fornecedor=request.fornecedor)
        .annotate(
            total_assentos=Coalesce(Sum("lotes__quantidade_total"), 0),
            saldo_disponivel=Coalesce(Sum("lotes__quantidade_disponivel"), 0),
            qtd_lotes=Count("lotes", distinct=True),
        )
        .order_by("nome")
    )

    q = request.GET.get("q", "").strip()
    if q:
        licencas = licencas.filter(nome__icontains=q)

    paginator = Paginator(licencas, 15)
    page_obj = paginator.get_page(request.GET.get("page", 1))

    get_copy = request.GET.copy()
    get_copy.pop("page", None)
    qs_keep = get_copy.urlencode()

    context = {
        "fornecedor": request.fornecedor,
        "page_obj": page_obj,
        "total": paginator.count,
        "f_q": q,
        "qs_keep": qs_keep,
        "active_nav": "licencas",
    }
    return render(request, "front/portal/portal_licencas_list.html", context)
