from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.http import HttpResponse
from django.db.models import Q, Count, Sum, F
from django.db.models.functions import Coalesce
from django.core.paginator import Paginator
from django.utils import timezone
from django.db import transaction
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

from ..models import (
    SimNaoChoices,
    Licenca, LicencaLote, MovimentacaoLicenca,
    TipoMovLicencaChoices, PeriodicidadeChoices,
    Usuario, CentroCusto, Fornecedor,
)
from ..forms import LicencaForm, MovimentacaoLicencaForm, LicencaLoteForm


def _normalizar_custos_lote(lote):
    """
    Calcula custos unitários mensais e anuais de um LicencaLote.
    Centralizado aqui para evitar duplicação entre licenca_detail e licenca_export_excel.
    """
    periodicidade = str(lote.periodicidade or "").lower()
    qtd_lote = int(lote.quantidade_total or 0)
    custo_ciclo_lote = Decimal(lote.custo_ciclo or 0)

    if qtd_lote <= 0:
        return {
            "unitario_ciclo": Decimal("0.00"),
            "mensal_unit": Decimal("0.00"),
            "anual_unit": Decimal("0.00"),
            "mensal_total": Decimal("0.00"),
            "anual_total": Decimal("0.00"),
        }

    if periodicidade == "mensal":
        custo_mensal_lote_base = custo_ciclo_lote
    elif periodicidade == "trimestral":
        custo_mensal_lote_base = (custo_ciclo_lote / Decimal("3")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    elif periodicidade == "semestral":
        custo_mensal_lote_base = (custo_ciclo_lote / Decimal("6")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    elif periodicidade == "anual":
        custo_mensal_lote_base = (custo_ciclo_lote / Decimal("12")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    else:
        custo_mensal_lote_base = custo_ciclo_lote.quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

    unitario_ciclo = (custo_ciclo_lote / Decimal(qtd_lote)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    mensal_unit = (custo_mensal_lote_base / Decimal(qtd_lote)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    anual_unit = (mensal_unit * Decimal("12")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    mensal_total = (mensal_unit * Decimal(qtd_lote)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    anual_total = (mensal_total * Decimal("12")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )

    return {
        "unitario_ciclo": unitario_ciclo,
        "mensal_unit": mensal_unit,
        "anual_unit": anual_unit,
        "mensal_total": mensal_total,
        "anual_total": anual_total,
    }


@login_required
def licenca_list(request):
    """
    Dashboard de Licenças (Enterprise View).
    Exibe listagem com saldo consolidado em tempo real baseado nos lotes.
    """
    # --- 1. Construção do QuerySet (Eager Loading para Performance) ---
    qs = (
        Licenca.objects
        .select_related("fornecedor", "centro_custo")
        .annotate(
            # KPI por Linha: Quantos lotes existem para esta licença?
            qtd_lotes=Count("lotes", distinct=True),
            
            # KPI Crítico: Soma o saldo disponível de cada lote vinculado
            # Se não tiver lotes, retorna 0 (Coalesce)
            estoque_real=Coalesce(Sum("lotes__quantidade_disponivel"), 0)
        )
        .order_by("nome")
    )

    # --- 2. Filtros Inteligentes ---
    q = request.GET.get("q", "").strip()
    fornecedor_id = request.GET.get("fornecedor", "").strip()
    pmb_filter = request.GET.get("pmb", "").strip()
    status_estoque = request.GET.get("status", "").strip()

    if q:
        qs = qs.filter(Q(nome__icontains=q) | Q(observacao__icontains=q))
    
    if fornecedor_id and fornecedor_id.isdigit():
        qs = qs.filter(fornecedor_id=fornecedor_id)
        
    if pmb_filter:
        qs = qs.filter(pmb=pmb_filter)

    # Filtro de Status de Estoque (Baseado na anotação calculada)
    if status_estoque == "com_estoque":
        qs = qs.filter(estoque_real__gt=0)
    elif status_estoque == "sem_estoque":
        qs = qs.filter(estoque_real=0)

    # --- 3. KPIs Globais (Cards do Topo) ---
    # Calculamos totais rápidos para o gestor ter visão macro
    kpi_total_licencas = Licenca.objects.count()
    
    # Soma total de assentos disponíveis na empresa inteira
    kpi_total_assentos = LicencaLote.objects.aggregate(
        total=Coalesce(Sum('quantidade_disponivel'), 0)
    )['total']
    
    kpi_pmb = Licenca.objects.filter(pmb=SimNaoChoices.SIM).count()

    # --- 4. Paginação e Controle ---
    try:
        per_page = int(request.GET.get("pp", 15))
    except ValueError:
        per_page = 15
    
    paginator = Paginator(qs, per_page)
    page_num = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_num)

    # Preserva filtros na paginação (query string)
    get_copy = request.GET.copy()
    if "page" in get_copy: del get_copy["page"]
    qs_keep = get_copy.urlencode()

    # --- 5. Contexto para o Template ---
    context = {
        "page_obj": page_obj,
        "qs_keep": qs_keep,
        "total_registros": qs.count(),
        
        # KPIs
        "kpi_total": kpi_total_licencas,
        "kpi_assentos": kpi_total_assentos,
        "kpi_pmb": kpi_pmb,

        # Estado dos Filtros (para manter selecionado)
        "filter_q": q,
        "filter_fornecedor": int(fornecedor_id) if fornecedor_id.isdigit() else "",
        "filter_pmb": pmb_filter,
        "filter_status": status_estoque,
        "per_page": per_page,

        # Opções para Dropdowns
        "opt_fornecedores": Fornecedor.objects.values("id", "nome").order_by("nome"),
        "opt_pmb": SimNaoChoices.choices,
    }

    return render(request, "front/licencas/licenca_list.html", context)

@login_required
def licenca_form(request, pk=None):
    """
    View simplificada para Cadastro/Edição de Licença (4 campos).
    """
    # Se houver PK, é edição. Senão, criação.
    obj = get_object_or_404(Licenca, pk=pk) if pk else None

    if request.method == "POST":
        form = LicencaForm(request.POST, instance=obj)
        if form.is_valid():
            try:
                licenca = form.save(commit=False)
                
                # Preenche auditoria
                if not obj:
                    licenca.criado_por = request.user
                licenca.atualizado_por = request.user
                
                licenca.save()
                
                verb = "editada" if obj else "criada"
                messages.success(request, f"Licença '{licenca.nome}' {verb} com sucesso!")
                return redirect("licenca_list")
                
            except Exception as e:
                messages.error(request, f"Erro crítico ao salvar: {e}")
        else:
            messages.error(request, "Verifique os campos obrigatórios.")
    else:
        form = LicencaForm(instance=obj)

    return render(request, "front/licencas/licenca_form.html", {
        "form": form,
        "obj": obj
    })


# --- HELPER: Calcula Alocação por Centro de Custo ---
def _get_dados_cc(licenca):
    """
    Reconstitui o estado atual das licenças para agrupar por Centro de Custo.
    Lógica: Pega todas as movimentações ordenadas. 
    Se 'atribuicao' -> Adiciona usuário. Se 'devolucao' -> Remove usuário.
    """
    movs = MovimentacaoLicenca.objects.filter(licenca=licenca).select_related(
        'usuario', 'centro_custo_destino'
    ).order_by('created_at')

    # 1. Descobrir quem está ativo e qual seu custo atual
    ativos = {} # {usuario_id: MovimentacaoObj}
    
    for mov in movs:
        if mov.tipo == 'atribuicao':
            ativos[mov.usuario_id] = mov
        elif mov.tipo == 'devolucao':
            ativos.pop(mov.usuario_id, None)

    # 2. Agrupar por Centro de Custo
    cc_stats = defaultdict(lambda: {'nome': 'Não Definido', 'qtd': 0, 'total': Decimal(0)})

    for mov in ativos.values():
        cc = mov.centro_custo_destino
        cc_id = cc.id if cc else 'na'
        cc_name = cc.departamento if cc else 'Sem Centro de Custo'
        
        cc_stats[cc_id]['nome'] = cc_name
        cc_stats[cc_id]['qtd'] += 1
        cc_stats[cc_id]['total'] += (mov.valor_unitario or Decimal(0))

    # Retorna lista ordenada pelo maior valor total
    return sorted(cc_stats.values(), key=lambda x: x['total'], reverse=True)

@login_required
def licenca_detail(request, pk):
    licenca = get_object_or_404(
        Licenca.objects.select_related("fornecedor", "centro_custo"),
        pk=pk
    )

    lotes_qs = (
        LicencaLote.objects.filter(licenca=licenca)
        .select_related("fornecedor", "centro_custo")
        .order_by("-data_compra", "-id")
    )
    lotes = list(lotes_qs)

    qtd_total = 0
    qtd_disp = 0

    total_investido_historico = Decimal("0.00")
    burn_rate_mensal = Decimal("0.00")
    burn_rate_anual = Decimal("0.00")

    # Normalização dos lotes + KPIs
    for lote in lotes:
        qtd_lote = int(lote.quantidade_total or 0)
        qtd_disponivel = int(lote.quantidade_disponivel or 0)
        em_uso = max(0, qtd_lote - qtd_disponivel)

        qtd_total += qtd_lote
        qtd_disp += qtd_disponivel

        custos = _normalizar_custos_lote(lote)

        lote.unitario_real = custos["unitario_ciclo"]
        lote.custo_mensal_unit = custos["mensal_unit"]
        lote.custo_anual_unit = custos["anual_unit"]
        lote.total_mensal_calc = custos["mensal_total"]
        lote.total_investido_calc = custos["anual_total"]

        total_investido_historico += custos["anual_total"]

        if em_uso > 0:
            burn_rate_mensal += (custos["mensal_unit"] * Decimal(em_uso)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            burn_rate_anual += (custos["anual_unit"] * Decimal(em_uso)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )

    qtd_em_uso = max(0, qtd_total - qtd_disp)
    pct_uso = int((qtd_em_uso / qtd_total) * 100) if qtd_total > 0 else 0

    # Histórico completo + paginação
    movimentacoes_qs = (
        MovimentacaoLicenca.objects.filter(licenca=licenca)
        .select_related("usuario", "lote", "centro_custo_destino", "criado_por")
        .order_by("-created_at")
    )

    movimentacoes_lista = []
    for mov in movimentacoes_qs:
        mensal = Decimal("0.00")
        anual = Decimal("0.00")

        if mov.lote:
            custos_mov = _normalizar_custos_lote(mov.lote)
            mensal = custos_mov["mensal_unit"]
            anual = custos_mov["anual_unit"]

        mov.custo_mensal_exibicao = mensal
        mov.custo_anual_exibicao = anual
        movimentacoes_lista.append(mov)

    paginator = Paginator(movimentacoes_lista, 10)
    page_number = request.GET.get("page")
    movimentacoes_page = paginator.get_page(page_number)

    cc_list = _get_dados_cc(licenca)

    context = {
        "obj": licenca,
        "kpi": {
            "total": qtd_total,
            "disponivel": qtd_disp,
            "em_uso": qtd_em_uso,
            "pct_uso": pct_uso,
            "investimento_total": total_investido_historico,
            "gasto_mensal": burn_rate_mensal,
            "gasto_anual": burn_rate_anual,
        },
        "lotes": lotes,
        "movimentacoes": movimentacoes_page,
        "cc_list": cc_list,
    }

    return render(request, "front/licencas/licenca_detail.html", context)


@login_required
@permission_required("ProjetoEstoque.view_licenca", raise_exception=True)
def licenca_export_excel(request, pk):
    licenca = get_object_or_404(
        Licenca.objects.select_related("fornecedor", "centro_custo"),
        pk=pk
    )

    lotes_qs = (
        LicencaLote.objects.filter(licenca=licenca)
        .select_related("fornecedor", "centro_custo")
        .order_by("-data_compra", "-id")
    )

    movimentacoes_qs = (
        MovimentacaoLicenca.objects.filter(licenca=licenca)
        .select_related("usuario", "lote", "centro_custo_destino", "criado_por")
        .order_by("-created_at")
    )

    cc_list = _get_dados_cc(licenca)

    # =========================
    # CRIA WORKBOOK
    # =========================
    wb = Workbook()

    # Estilos
    header_fill = PatternFill("solid", fgColor="1F4E78")
    header_font = Font(color="FFFFFF", bold=True)
    title_font = Font(size=13, bold=True, color="1F1F1F")
    bold_font = Font(bold=True)
    thin_border = Border(
        left=Side(style="thin", color="D9D9D9"),
        right=Side(style="thin", color="D9D9D9"),
        top=Side(style="thin", color="D9D9D9"),
        bottom=Side(style="thin", color="D9D9D9"),
    )
    center_alignment = Alignment(horizontal="center", vertical="center")
    left_alignment = Alignment(horizontal="left", vertical="center")
    right_alignment = Alignment(horizontal="right", vertical="center")

    # =========================
    # ABA 1 - HISTÓRICO USUÁRIOS
    # =========================
    ws1 = wb.active
    ws1.title = "Usuarios e Alocacoes"

    ws1["A1"] = f"Exportação de Licença - {licenca.nome}"
    ws1["A1"].font = title_font

    ws1["A2"] = "Fornecedor:"
    ws1["A2"].font = bold_font
    ws1["B2"] = licenca.fornecedor.nome if licenca.fornecedor else "-"

    ws1["D2"] = "Centro de Custo Padrão:"
    ws1["D2"].font = bold_font
    ws1["E2"] = licenca.centro_custo.departamento if licenca.centro_custo else "-"

    headers_ws1 = [
        "Tipo de Ação",
        "Colaborador",
        "E-mail",
        "Matrícula",
        "Lote",
        "Periodicidade do Lote",
        "Centro de Custo Destino",
        "Número CC",
        "Custo Mensal",
        "Custo Anual",
        "Responsável Registro",
        "Data",
        "Hora",
    ]

    start_row_ws1 = 4
    for col_num, header in enumerate(headers_ws1, 1):
        cell = ws1.cell(row=start_row_ws1, column=col_num, value=header)
        cell.fill = header_fill
        cell.font = header_font
        cell.border = thin_border
        cell.alignment = center_alignment

    row = start_row_ws1 + 1

    for mov in movimentacoes_qs:
        mensal = Decimal("0.00")
        anual = Decimal("0.00")

        if mov.lote:
            custos = _normalizar_custos_lote(mov.lote)
            mensal = custos["mensal_unit"]
            anual = custos["anual_unit"]

        nome_usuario = mov.usuario.nome if mov.usuario else "-"
        email_usuario = (mov.usuario.email or "-") if mov.usuario else "-"
        matricula_usuario = (mov.usuario.matricula or "-") if mov.usuario else "-"

        tipo_acao = "Saída" if mov.tipo == "atribuicao" else "Devolução"

        ws1.cell(row=row, column=1, value=tipo_acao)
        ws1.cell(row=row, column=2, value=nome_usuario)
        ws1.cell(row=row, column=3, value=email_usuario)
        ws1.cell(row=row, column=4, value=matricula_usuario)
        ws1.cell(row=row, column=5, value=f"#{mov.lote.pk}" if mov.lote else "-")
        ws1.cell(
            row=row,
            column=6,
            value=mov.lote.get_periodicidade_display() if mov.lote else "-"
        )
        ws1.cell(
            row=row,
            column=7,
            value=mov.centro_custo_destino.departamento if mov.centro_custo_destino else "-"
        )
        ws1.cell(
            row=row,
            column=8,
            value=mov.centro_custo_destino.numero if mov.centro_custo_destino else "-"
        )
        ws1.cell(row=row, column=9, value=float(mensal))
        ws1.cell(row=row, column=10, value=float(anual))
        ws1.cell(
            row=row,
            column=11,
            value=mov.criado_por.username if mov.criado_por else "-"
        )
        ws1.cell(
            row=row,
            column=12,
            value=mov.created_at.strftime("%d/%m/%Y") if mov.created_at else "-"
        )
        ws1.cell(
            row=row,
            column=13,
            value=mov.created_at.strftime("%H:%M") if mov.created_at else "-"
        )

        for col in range(1, 14):
            cell = ws1.cell(row=row, column=col)
            cell.border = thin_border
            if col in [9, 10]:
                cell.number_format = 'R$ #,##0.00'
                cell.alignment = right_alignment
            else:
                cell.alignment = left_alignment

        row += 1

    # Auto width aba 1
    widths_ws1 = {
        1: 16, 2: 28, 3: 30, 4: 22, 5: 12, 6: 18, 7: 28,
        8: 14, 9: 16, 10: 16, 11: 20, 12: 14, 13: 10
    }
    for col_idx, width in widths_ws1.items():
        ws1.column_dimensions[get_column_letter(col_idx)].width = width

    ws1.freeze_panes = "A5"

    # =========================
    # ABA 2 - CENTROS DE CUSTO
    # =========================
    ws2 = wb.create_sheet(title="Centros de Custo")

    ws2["A1"] = f"Centros de Custo - {licenca.nome}"
    ws2["A1"].font = title_font

    headers_ws2 = [
        "Departamento / Centro de Custo",
        "Qtd. Assentos",
        "Gasto Consolidado",
    ]

    start_row_ws2 = 3
    for col_num, header in enumerate(headers_ws2, 1):
        cell = ws2.cell(row=start_row_ws2, column=col_num, value=header)
        cell.fill = header_fill
        cell.font = header_font
        cell.border = thin_border
        cell.alignment = center_alignment

    row = start_row_ws2 + 1
    total_assentos = 0
    total_gasto = Decimal("0.00")

    for cc in cc_list:
        qtd = cc.get("qtd", 0) or 0
        total = Decimal(cc.get("total", 0) or 0)

        ws2.cell(row=row, column=1, value=cc.get("nome", "-"))
        ws2.cell(row=row, column=2, value=qtd)
        ws2.cell(row=row, column=3, value=float(total))

        ws2.cell(row=row, column=1).alignment = left_alignment
        ws2.cell(row=row, column=2).alignment = center_alignment
        ws2.cell(row=row, column=3).alignment = right_alignment
        ws2.cell(row=row, column=3).number_format = 'R$ #,##0.00'

        for col in range(1, 4):
            ws2.cell(row=row, column=col).border = thin_border

        total_assentos += qtd
        total_gasto += total
        row += 1

    # Linha de total
    ws2.cell(row=row, column=1, value="TOTAL")
    ws2.cell(row=row, column=2, value=total_assentos)
    ws2.cell(row=row, column=3, value=float(total_gasto))

    for col in range(1, 4):
        cell = ws2.cell(row=row, column=col)
        cell.font = bold_font
        cell.border = thin_border
        if col == 1:
            cell.alignment = left_alignment
        elif col == 2:
            cell.alignment = center_alignment
        else:
            cell.alignment = right_alignment
            cell.number_format = 'R$ #,##0.00'

    ws2.column_dimensions["A"].width = 38
    ws2.column_dimensions["B"].width = 16
    ws2.column_dimensions["C"].width = 20
    ws2.freeze_panes = "A4"

    # =========================
    # ABA 3 - LOTES
    # =========================
    ws3 = wb.create_sheet(title="Lotes")

    ws3["A1"] = f"Lotes da Licença - {licenca.nome}"
    ws3["A1"].font = title_font

    headers_ws3 = [
        "Lote",
        "Data Compra",
        "Pedido / NF",
        "Periodicidade",
        "Qtd. Total",
        "Qtd. Disponível",
        "Custo Ciclo / Licença",
        "Custo Mensal / Licença",
        "Custo Anual / Licença",
        "Mensal do Lote",
        "Anual do Lote",
    ]

    start_row_ws3 = 3
    for col_num, header in enumerate(headers_ws3, 1):
        cell = ws3.cell(row=start_row_ws3, column=col_num, value=header)
        cell.fill = header_fill
        cell.font = header_font
        cell.border = thin_border
        cell.alignment = center_alignment

    row = start_row_ws3 + 1

    for lote in lotes_qs:
        custos = _normalizar_custos_lote(lote)

        ws3.cell(row=row, column=1, value=f"#{lote.pk}")
        ws3.cell(row=row, column=2, value=lote.data_compra.strftime("%d/%m/%Y") if lote.data_compra else "-")
        ws3.cell(row=row, column=3, value=lote.numero_pedido or "-")
        ws3.cell(row=row, column=4, value=lote.get_periodicidade_display() if lote.periodicidade else "-")
        ws3.cell(row=row, column=5, value=lote.quantidade_total or 0)
        ws3.cell(row=row, column=6, value=lote.quantidade_disponivel or 0)
        ws3.cell(row=row, column=7, value=float(custos["unitario_ciclo"]))
        ws3.cell(row=row, column=8, value=float(custos["mensal_unit"]))
        ws3.cell(row=row, column=9, value=float(custos["anual_unit"]))
        ws3.cell(row=row, column=10, value=float(custos["mensal_total"]))
        ws3.cell(row=row, column=11, value=float(custos["anual_total"]))

        for col in range(1, 12):
            cell = ws3.cell(row=row, column=col)
            cell.border = thin_border
            if col in [7, 8, 9, 10, 11]:
                cell.number_format = 'R$ #,##0.00'
                cell.alignment = right_alignment
            elif col in [5, 6]:
                cell.alignment = center_alignment
            else:
                cell.alignment = left_alignment

        row += 1

    widths_ws3 = {
        1: 12, 2: 14, 3: 18, 4: 16, 5: 12, 6: 14,
        7: 18, 8: 18, 9: 18, 10: 18, 11: 18
    }
    for col_idx, width in widths_ws3.items():
        ws3.column_dimensions[get_column_letter(col_idx)].width = width

    ws3.freeze_panes = "A4"

    # =========================
    # RESPOSTA
    # =========================
    output = BytesIO()
    wb.save(output)
    output.seek(0)

    nome_arquivo = f"licenca_{licenca.pk}_{licenca.nome.replace(' ', '_')}.xlsx"

    response = HttpResponse(
        output.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = f'attachment; filename="{nome_arquivo}"'

    return response

# ============ MOVIMENTAÇÕES ============

@login_required
def mov_licenca_list(request):
    """
    Listagem de Movimentações de Licenças com filtros e paginação.
    """
    # Filtros
    q = (request.GET.get("q") or "").strip()
    tipo = (request.GET.get("tipo") or "").strip()

    # QuerySet Otimizado
    qs = (
        MovimentacaoLicenca.objects
        .select_related("licenca", "usuario", "centro_custo_destino")
        .order_by("-created_at")
    )

    if q:
        qs = qs.filter(
            Q(licenca__nome__icontains=q) | 
            Q(usuario__nome__icontains=q)
        )
    
    # Validação do Tipo (segurança)
    valid_types = [choice[0] for choice in MovimentacaoLicenca._meta.get_field("tipo").choices]
    if tipo in valid_types:
        qs = qs.filter(tipo=tipo)

    # Paginação (Padrão 20 itens)
    try:
        per_page = int(request.GET.get("pp", 20))
        per_page = max(10, min(per_page, 100)) # Limites de segurança
    except ValueError:
        per_page = 20

    paginator = Paginator(qs, per_page)
    page_num = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_num)

    # Preserva filtros na paginação
    get_copy = request.GET.copy()
    get_copy.pop("page", None)
    qs_keep = get_copy.urlencode()

    context = {
        "movs": page_obj.object_list,
        "page_obj": page_obj,
        "paginator": paginator,
        "qs_keep": qs_keep,
        "q": q,
        "tipo": tipo,
        "tipos": MovimentacaoLicenca._meta.get_field("tipo").choices,
        "total": qs.count()
    }

    return render(request, "front/licencas/mov_licenca_list.html", context)

@login_required
def mov_licenca_form(request):
    initial = {}
    if "licenca" in request.GET: initial["licenca"] = request.GET.get("licenca")
    if "usuario" in request.GET: initial["usuario"] = request.GET.get("usuario")

    if request.method == "POST":
        form = MovimentacaoLicencaForm(request.POST)
        if form.is_valid():
            try:
                mov = form.save(user=request.user)
                
                # Feedback detalhado
                lote_txt = f"Lote #{mov.lote.pk}" if mov.lote else "N/A"
                cc_txt = mov.centro_custo_destino.departamento if mov.centro_custo_destino else "N/A"
                
                messages.success(request, f"{mov.get_tipo_display()} realizada. Estoque: {lote_txt} | Custo: {cc_txt}")
                return redirect("licenca_list")
            except Exception as e:
                messages.error(request, f"Erro: {e}")
    else:
        form = MovimentacaoLicencaForm(initial=initial)

    # JSON para Select2 (Apenas lotes com saldo)
    lotes_qs = LicencaLote.objects.filter(quantidade_disponivel__gt=0).values(
        'id', 'licenca_id', 'quantidade_disponivel', 'numero_pedido', 'data_compra'
    )
    
    lotes_dict = {}
    for l in lotes_qs:
        lid = str(l['licenca_id'])
        if lid not in lotes_dict: lotes_dict[lid] = []
        dt = l['data_compra'].strftime('%d/%m/%Y') if l['data_compra'] else "-"
        txt = f"Lote #{l['id']} - Disp: {l['quantidade_disponivel']} ({dt})"
        lotes_dict[lid].append({'id': l['id'], 'text': txt})

    context = {
        "form": form,
        "lotes_json": lotes_dict,
        "pre_selected_lote": request.POST.get("lote_id_select") or ""
    }
    return render(request, "front/licencas/mov_licenca_form.html", context)
# --- LISTA DE LOTES ---
@login_required
def licenca_lote_list(request):
    """
    Lista de Lotes com busca avançada e layout otimizado.
    """
    q = request.GET.get("q", "").strip()
    
    # QueryBase com select_related para evitar N+1 queries
    qs = (
        LicencaLote.objects
        .select_related("licenca", "fornecedor", "centro_custo")
        .order_by("-created_at")
    )

    # Filtro Textual
    if q:
        qs = qs.filter(
            Q(licenca__nome__icontains=q) | 
            Q(numero_pedido__icontains=q) | 
            Q(observacao__icontains=q)
        )

    return render(request, "front/licencas/licenca_lote_list.html", {
        "lotes": qs,
        "q": q
    })

@login_required
@transaction.atomic
def licenca_lote_form(request, pk=None):
    """
    View Inteligente para Gestão de Lotes.
    Calcula automaticamente a disponibilidade baseada na entrada.
    """
    obj = get_object_or_404(LicencaLote, pk=pk) if pk else None

    if request.method == "POST":
        form = LicencaLoteForm(request.POST, instance=obj)
        if form.is_valid():
            lote = form.save(commit=False)
            
            # --- LÓGICA DE SALDO ---
            if not obj:
                # Novo Lote: Disponível = Total
                lote.quantidade_disponivel = lote.quantidade_total
                lote.criado_por = request.user # Auditoria
            else:
                # Edição: Ajusta o disponível pela diferença do total
                # Ex: Tinha 10 (8 disp, 2 uso). Editou total para 15 (+5).
                # Novo Disp = 8 + 5 = 13. Usados continuam 2.
                diff = lote.quantidade_total - obj.quantidade_total
                lote.quantidade_disponivel = obj.quantidade_disponivel + diff
            
            lote.atualizado_por = request.user # Auditoria
            lote.save()
            
            msg = f"Lote #{lote.pk} atualizado com sucesso!" if obj else "Lote criado com sucesso!"
            messages.success(request, msg)
            return redirect("licenca_lote_list")
        else:
            messages.error(request, "Verifique os erros no formulário abaixo.")
    else:
        form = LicencaLoteForm(instance=obj)

    return render(request, "front/licencas/licenca_lote_form.html", {
        "form": form,
        "obj": obj
    })




### exportações 

