# Categoria - CRUD

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from decimal import Decimal
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.db.models import DateField, DateTimeField, IntegerField, BigIntegerField, CharField, Sum, Count, Case, When, Value as V, ExpressionWrapper, Window, DecimalField
from django.db.models import Q, Count, Prefetch, F, Sum, DecimalField, Value, CharField,Exists
from django.core.paginator import Paginator
from django.core.exceptions import FieldError
from django.db.models.functions import TruncMonth, Coalesce, Concat, Cast
from collections import defaultdict
from django.db.models import OuterRef, Subquery
from datetime import date, timedelta, datetime
from django.db import transaction
from django.utils import timezone
from django.conf import settings
from django.views.decorators.http import require_POST
from django.http import HttpResponse
from io import BytesIO
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from openpyxl.utils import get_column_letter
from django.db import models
import unicodedata
from django.db.models.functions import Coalesce, NullIf
from .models import Categoria, Subtipo, Usuario,PreventivaExecucao,Fornecedor, Localidade, Funcao, CentroCusto, Item, Locacao, Comentario, MovimentacaoItem, CicloManutencao, StatusItemChoices, LocalidadeChoices, SimNaoChoices, StatusUsuarioChoices, CheckListModelo, CheckListPergunta, Preventiva, PreventivaResposta, TipoRespostaChoices, SimNaoChoices, TipoMovimentacaoChoices, Licenca, MovimentacaoLicenca, TipoMovLicencaChoices, PeriodicidadeChoices, LicencaLote
from django.utils.dateparse import parse_date
from .forms import CategoriaForm, SubtipoForm, UsuarioForm, FornecedorForm, LocalidadeForm, FuncaoForm, CentroCustoForm, ItemForm, LocacaoForm, LicencaForm, ComentarioForm, MovimentacaoItemForm, CicloManutencaoForm, PreventivaStartForm, ChecklistModeloForm, ChecklistPerguntaForm, PreventivaStartForm, LicencaForm, MovimentacaoLicencaForm, LicencaLoteForm
from openpyxl.worksheet.table import Table, TableStyleInfo
from django.utils.timezone import now
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
import io
from django.http import JsonResponse
from django.template.loader import render_to_string

# --- Helpers de compatibilidade com nomes de campos diferentes no LicencaLote ---
def _lote_total_get(lote):
    for n in ("qtd_total", "quantidade_total", "quantidade", "total", "qtd"):
        if hasattr(lote, n):
            return getattr(lote, n) or 0
    return 0

def _lote_total_set(lote, val):
    for n in ("qtd_total", "quantidade_total", "quantidade", "total", "qtd"):
        if hasattr(lote, n):
            setattr(lote, n, val); return

def _lote_total_fieldname(lote_or_cls):
    # usado para apontar erro no form
    for n in ("qtd_total", "quantidade_total", "quantidade", "total", "qtd"):
        if hasattr(lote_or_cls, n):
            return n
    return None

def _lote_disp_get(lote):
    for n in ("disponivel", "qtd_disponivel", "quantidade_disponivel", "disponiveis", "saldo", "em_estoque"):
        if hasattr(lote, n):
            return getattr(lote, n) or 0
    return 0

def _lote_disp_set(lote, val):
    for n in ("disponivel", "qtd_disponivel", "quantidade_disponivel", "disponiveis", "saldo", "em_estoque"):
        if hasattr(lote, n):
            setattr(lote, n, val); return
############### CATEGORIA ##############################

def categorias_list(request):
    categorias = Categoria.objects.all()
    return render(request, 'categoria/list.html', {'categorias': categorias})

def categoria_create(request):
    form = CategoriaForm(request.POST or None)
    if form.is_valid():
        categoria = form.save(commit=False)
        categoria.criado_por = request.user
        categoria.atualizado_por = request.user
        categoria.save()
        return redirect('categorias_list')
    return render(request, 'categoria/form.html', {'form': form})

def categoria_update(request, pk):
    categoria = get_object_or_404(Categoria, pk=pk)
    form = CategoriaForm(request.POST or None, instance=categoria)
    if form.is_valid():
        categoria = form.save(commit=False)
        categoria.atualizado_por = request.user
        categoria.save()
        return redirect('categorias_list')
    return render(request, 'categoria/form.html', {'form': form})

def categoria_delete(request, pk):
    categoria = get_object_or_404(Categoria, pk=pk)
    if request.method == 'POST':
        categoria.delete()
        return redirect('categorias_list')
    return render(request, 'categoria/delete.html', {'obj': categoria})


############### SUBTIPO ##############################

@login_required
def subtipo_list(request):
    q = request.GET.get("q", "").strip()
    cat = request.GET.get("categoria", "").strip()
    alocado = request.GET.get("alocado", "").strip()

    qs = Subtipo.objects.select_related("categoria").order_by("categoria__nome", "nome")

    if q:
        qs = qs.filter(Q(nome__icontains=q) | Q(categoria__nome__icontains=q))
    if cat:
        qs = qs.filter(categoria__id=cat)
    if alocado:
        qs = qs.filter(alocado=alocado)

    context = {
        "subtipos": qs,
        "categorias": Categoria.objects.order_by("nome"),
        "alocado_choices": (("sim","Sim"),("nao","Não")),
        "request": request,  # para manter valores nos filtros
    }
    return render(request, "front/subtipo_list.html", context)

@login_required
def subtipo_create(request):
    if request.method == "POST":
        form = SubtipoForm(request.POST)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.criado_por = request.user
            obj.atualizado_por = request.user
            obj.save()
            messages.success(request, "Subtipo cadastrado com sucesso!")
            return redirect("subtipo_list")
        messages.error(request, "Verifique os campos destacados.")
    else:
        form = SubtipoForm()

    return render(request, "front/subtipo_form.html", {"form": form, "editar": False})

@login_required
def subtipo_update(request, pk):
    obj = get_object_or_404(Subtipo, pk=pk)
    if request.method == "POST":
        form = SubtipoForm(request.POST, instance=obj)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.atualizado_por = request.user
            obj.save()
            messages.success(request, "Subtipo atualizado com sucesso!")
            return redirect("subtipo_list")
        messages.error(request, "Verifique os campos destacados.")
    else:
        form = SubtipoForm(instance=obj)

    return render(request, "front/subtipo_form.html", {"form": form, "editar": True, "obj": obj})

@login_required
def subtipo_delete(request, pk):
    obj = get_object_or_404(Subtipo, pk=pk)
    if request.method == "POST":
        obj.delete()
        messages.success(request, "Subtipo excluído com sucesso!")
        return redirect("subtipo_list")
    return render(request, "front/subtipo_confirm_delete.html", {"obj": obj})

@login_required
def subtipo_detail(request, pk):
    obj = get_object_or_404(Subtipo.objects.select_related("categoria"), pk=pk)
    return render(request, "front/subtipo_detail.html", {"obj": obj})


############### USUÁRIO ##############################

@login_required
def usuario_list(request):
    """
    Lista de usuários com:
      - Filtros: q, status, pmb, cc, loc, func
      - KPIs: total, ativos, desligados, PMB(sim/nao)
      - Paginação: pp (10/20/50/100) + page
    """
    q     = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()      # 'ativo' | 'desligado' | ''
    pmb    = (request.GET.get("pmb") or "").strip()         # 'sim' | 'nao' | ''
    cc     = (request.GET.get("cc") or "").strip()          # id centro de custo
    loc    = (request.GET.get("loc") or "").strip()         # id localidade
    func   = (request.GET.get("func") or "").strip()        # id função

    qs = (
        Usuario.objects
        .select_related("centro_custo", "localidade", "funcao")
        .order_by("-created_at")
    )

    # --- Filtros ---
    if q:
        qs = qs.filter(Q(nome__icontains=q) | Q(email__icontains=q))
    if status in dict(StatusUsuarioChoices.choices):
        qs = qs.filter(status=status)
    if pmb in dict(SimNaoChoices.choices):
        qs = qs.filter(pmb=pmb)
    if cc.isdigit():
        qs = qs.filter(centro_custo_id=int(cc))
    if loc.isdigit():
        qs = qs.filter(localidade_id=int(loc))
    if func.isdigit():
        qs = qs.filter(funcao_id=int(func))

    qs = qs.distinct()
    total_filtrado = qs.count()

    # --- KPIs globais (sem filtro de status/PMB para visão executiva) ---
    total_geral     = Usuario.objects.count()
    total_ativos    = Usuario.objects.filter(status="ativo").count()
    total_desligado = Usuario.objects.filter(status="desligado").count()
    total_pmb_sim   = Usuario.objects.filter(pmb="sim").count()
    total_pmb_nao   = Usuario.objects.filter(pmb="nao").count()

    # --- Paginação ---
    try:
        per_page = int(request.GET.get("pp") or 20)
        if per_page not in (10, 20, 50, 100):
            per_page = 20
    except (TypeError, ValueError):
        per_page = 20

    paginator = Paginator(qs, per_page)
    page_number = request.GET.get("page") or 1
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    # Querystring preservada (sem o page)
    params = request.GET.copy()
    params.pop("page", None)
    qs_keep = params.urlencode()

    ctx = {
        "usuarios": page_obj.object_list,
        "page_obj": page_obj,
        "paginator": paginator,
        "per_page": per_page,
        "qs_keep": qs_keep,

        "total": total_filtrado,  # total da lista filtrada
        "kpi_total_geral": total_geral,
        "kpi_total_ativos": total_ativos,
        "kpi_total_desligado": total_desligado,
        "kpi_total_pmb_sim": total_pmb_sim,
        "kpi_total_pmb_nao": total_pmb_nao,

        "q": q, "status": status, "pmb": pmb, "cc": cc, "loc": loc, "func": func,
        "status_choices": StatusUsuarioChoices.choices,
        "pmb_choices": SimNaoChoices.choices,
        "cc_list": CentroCusto.objects.order_by("numero", "departamento"),
        "loc_list": Localidade.objects.order_by("local"),
        "func_list": Funcao.objects.order_by("nome"),
    }
    return render(request, "front/usuarios/usuario_list.html", ctx)

# CREATE
@login_required
def usuario_create(request):
    if request.method == "POST":
        form = UsuarioForm(request.POST)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.criado_por = request.user
            obj.atualizado_por = request.user
            obj.save()
            messages.success(request, "Usuário criado com sucesso!")
            return redirect("usuario_list")
        messages.error(request, "Corrija os erros do formulário.")
    else:
        form = UsuarioForm()
    return render(request, "front/usuarios/usuario_form.html", {"form": form, "editar": False})

@login_required
def usuario_update(request, pk: int):
    obj = get_object_or_404(Usuario, pk=pk)
    if request.method == "POST":
        form = UsuarioForm(request.POST, instance=obj)
        if form.is_valid():
            sobj = form.save(commit=False)
            sobj.atualizado_por = request.user
            sobj.save()
            messages.success(request, "Usuário atualizado com sucesso!")
            return redirect("usuario_list")
        messages.error(request, "Corrija os erros do formulário.")
    else:
        form = UsuarioForm(instance=obj)
    return render(request, "front/usuarios/usuario_form.html", {"form": form, "editar": True})


# DETAIL
@login_required
def usuario_detail(request, pk):
    obj = get_object_or_404(
        Usuario.objects.select_related("centro_custo", "localidade", "funcao"),
        pk=pk
    )

    # ===== ITENS (último movimento válido aponta posse) =====
    movs_itens = (
        MovimentacaoItem.objects
        .exclude(tipo_movimentacao__in=["entrada", "baixa"])
        .select_related("item", "item__subtipo", "item__localidade", "item__centro_custo")
        .order_by("item_id", "-created_at", "-id")
    )

    last_mov_by_item = {}
    for m in movs_itens:
        if m.item_id not in last_mov_by_item:
            last_mov_by_item[m.item_id] = m

    item_ids_com_usuario = []
    for item_id, m in last_mov_by_item.items():
        if m.usuario_id != obj.pk:
            continue
        if m.tipo_movimentacao == "transferencia" and (m.tipo_transferencia or "").lower() != "entrega":
            continue
        if m.tipo_movimentacao in ("envio_manutencao", "retorno_manutencao", "retorno"):
            continue
        item_ids_com_usuario.append(item_id)

    items_do_usuario = list(
        Item.objects
        .filter(pk__in=item_ids_com_usuario)
        .select_related("subtipo", "localidade", "centro_custo")
        .order_by("nome")
    )

    total_itens_loc_mensal = Decimal("0.00")
    total_itens_aquis = Decimal("0.00")

    for it in items_do_usuario:
        try:
            loc = it.locacao
        except Locacao.DoesNotExist:
            loc = None

        if getattr(it, "locado", None) == SimNaoChoices.SIM and loc and loc.valor_mensal:
            it.custo_tipo = "locacao"
            it.custo_valor = loc.valor_mensal
            total_itens_loc_mensal += loc.valor_mensal
        else:
            it.custo_tipo = "aquisicao"
            it.custo_valor = it.valor
            if it.valor:
                total_itens_aquis += it.valor

    # ===== LICENÇAS (última atribuição ativa para o usuário) =====
    movs_lic_usuario = (
        MovimentacaoLicenca.objects
        .filter(usuario=obj)
        .select_related("licenca", "licenca__fornecedor", "licenca__centro_custo", "lote")
        .order_by("licenca_id", "-created_at", "-id")
    )

    last_mov_by_lic = {}
    for m in movs_lic_usuario:
        if m.licenca_id not in last_mov_by_lic:
            last_mov_by_lic[m.licenca_id] = m

    def _mensal_anual_from_ciclo(licenca, custo_ciclo):
        if not custo_ciclo:
            return (None, None)
        per = (licenca.periodicidade or "").lower()
        ciclo = Decimal(custo_ciclo)
        if per == "mensal":
            return (ciclo, ciclo * Decimal("12"))
        elif per == "anual":
            return (ciclo / Decimal("12"), ciclo)
        elif per == "trimestral":
            return (ciclo / Decimal("3"), ciclo * (Decimal("12")/Decimal("3")))
        elif per == "semestral":
            return (ciclo / Decimal("6"), ciclo * (Decimal("12")/Decimal("6")))
        return (ciclo, ciclo * Decimal("12"))

    licencas_do_usuario = []
    total_lic_mensal = Decimal("0.00")
    total_lic_anual  = Decimal("0.00")

    for lic_id, mov in last_mov_by_lic.items():
        if mov.tipo != TipoMovLicencaChoices.ATRIBUICAO:
            continue

        lic = mov.licenca
        custo_mensal = None
        custo_anual  = None

        if mov.lote_id and getattr(mov.lote, "custo_ciclo", None) is not None:
            cm, ca = _mensal_anual_from_ciclo(lic, mov.lote.custo_ciclo)
            custo_mensal, custo_anual = cm, ca
        else:
            try:
                custo_mensal = lic.custo_mensal()
            except Exception:
                custo_mensal = None
            try:
                custo_anual = lic.custo_anual_estimado()
            except Exception:
                custo_anual = None

        if custo_mensal:
            total_lic_mensal += Decimal(custo_mensal)
        if custo_anual:
            total_lic_anual  += Decimal(custo_anual)

        licencas_do_usuario.append({
            "licenca": lic,
            "desde": mov.created_at,
            "custo_mensal": custo_mensal,
            "custo_anual":  custo_anual,
        })

    # ===== KPIs =====
    hoje = timezone.now().date()
    tenure_human = None
    if obj.data_inicio:
        anos = hoje.year - obj.data_inicio.year - ((hoje.month, hoje.day) < (obj.data_inicio.month, obj.data_inicio.day))
        meses_total = (hoje.year - obj.data_inicio.year) * 12 + (hoje.month - obj.data_inicio.month) - (1 if hoje.day < obj.data_inicio.day else 0)
        if anos >= 1:
            resto_meses = max(0, meses_total - anos*12)
            tenure_human = f"{anos} ano(s)" + (f" e {resto_meses} mês(es)" if resto_meses else "")
        else:
            tenure_human = f"{max(meses_total,0)} mês(es)"

    custo_total_mensal = (total_itens_loc_mensal or Decimal("0.00")) + (total_lic_mensal or Decimal("0.00"))
    custo_total_anual = (total_lic_anual or Decimal("0.00"))

    context = {
        "obj": obj,
        "items_do_usuario": items_do_usuario,
        "licencas_do_usuario": licencas_do_usuario,
        "totais_itens": {"loc_mensal": total_itens_loc_mensal, "aquisicao": total_itens_aquis},
        "totais_licencas": {"mensal": total_lic_mensal, "anual": total_lic_anual},
        "tenure_human": tenure_human,
        "itens_count": len(items_do_usuario),
        "custo_total_mensal": custo_total_mensal,
        "custo_total_anual": custo_total_anual,
    }
    return render(request, "front/usuarios/usuario_detail.html", context)




# DELETE (POST via modal)
@login_required
def usuario_delete(request, pk: int):
    obj = get_object_or_404(Usuario, pk=pk)
    if request.method == "POST":
        obj.delete()
        messages.success(request, "Usuário removido com sucesso.")
    else:
        messages.error(request, "Ação inválida.")
    return redirect("usuario_list")


############### FORNECEDOR ##############################

@login_required
def fornecedor_list(request):
    """
    Lista de Fornecedores com KPIs (custo mensal de locação, média, líder),
    cards por fornecedor (qtd itens, qtd locados, custo mensal) e tabela executiva.
    Filtros: q = nome/CNPJ, tem_contrato = sim|nao|.
    """
    q = (request.GET.get("q") or "").strip()
    tem_contrato = (request.GET.get("tem_contrato") or "").strip()  # "sim" | "nao" | ""

    qs = Fornecedor.objects.all().order_by("-created_at")
    if q:
        qs = qs.filter(Q(nome__icontains=q) | Q(cnpj__icontains=q))

    if tem_contrato == "sim":
        qs = qs.exclude(contrato__isnull=True).exclude(contrato__exact="")
    elif tem_contrato == "nao":
        qs = qs.filter(Q(contrato__isnull=True) | Q(contrato__exact=""))

    total_fornecedores = qs.count()
    fornecedores_ids = list(qs.values_list("id", flat=True))

    # ---------- Agregações por Item (evita depender de related_name) ----------
    itens_qs = Item.objects.filter(fornecedor_id__in=fornecedores_ids)
    # Totais globais (somente locados contam custo mensal)
    globais = itens_qs.filter(locado="sim").aggregate(
        custo_total=Sum("locacao__valor_mensal"),
        locados=Count("id"),
    )
    kpi_custo_total = globais.get("custo_total") or Decimal("0.00")
    kpi_total_locados = globais.get("locados") or 0
    kpi_media_fornecedor = (kpi_custo_total / total_fornecedores) if total_fornecedores else Decimal("0.00")

    # Por fornecedor: quantidade de itens totais, locados e soma mensal
    por_forn_total = (
        itens_qs
        .values("fornecedor_id")
        .annotate(qtd=Count("id"))
    )
    por_forn_locados = (
        itens_qs.filter(locado="sim")
        .values("fornecedor_id")
        .annotate(qtd=Count("id"), total=Sum("locacao__valor_mensal"))
    )

    m_total = {r["fornecedor_id"]: r["qtd"] for r in por_forn_total}
    m_loc_qtd = {r["fornecedor_id"]: r["qtd"] for r in por_forn_locados}
    m_loc_val = {r["fornecedor_id"]: (r["total"] or Decimal("0.00")) for r in por_forn_locados}

    # Fornecedor líder por custo (no conjunto filtrado)
    lider_id = max(m_loc_val, key=lambda k: m_loc_val[k]) if m_loc_val else None
    kpi_lider = None
    kpi_lider_val = Decimal("0.00")
    if lider_id:
        try:
            kpi_lider = Fornecedor.objects.get(id=lider_id)
            kpi_lider_val = m_loc_val.get(lider_id, Decimal("0.00"))
        except Fornecedor.DoesNotExist:
            kpi_lider = None
            kpi_lider_val = Decimal("0.00")

    # ---------- Paginação ----------
    try:
        per_page = int(request.GET.get("pp") or 20)
        if per_page not in (10, 20, 50, 100):
            per_page = 20
    except (TypeError, ValueError):
        per_page = 20

    paginator = Paginator(qs, per_page)
    page_number = request.GET.get("page") or 1
    try:
        page_obj = paginator.page(page_number)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    # Injeta métricas em cada fornecedor exibido na página
    for f in page_obj.object_list:
        f.qtd_itens = m_total.get(f.id, 0)
        f.qtd_locados = m_loc_qtd.get(f.id, 0)
        f.custo_locacao_total = m_loc_val.get(f.id, Decimal("0.00"))

    # Preserva filtros na paginação / pp
    params = request.GET.copy()
    params.pop("page", None)
    qs_keep = params.urlencode()

    ctx = {
        "fornecedores": page_obj.object_list,
        "page_obj": page_obj,
        "paginator": paginator,
        "per_page": per_page,
        "qs_keep": qs_keep,

        "total": total_fornecedores,
        "q": q,
        "tem_contrato": tem_contrato,

        # KPIs
        "kpi_custo_total": kpi_custo_total,
        "kpi_total_locados": kpi_total_locados,
        "kpi_media_fornecedor": kpi_media_fornecedor,
        "kpi_lider": kpi_lider,
        "kpi_lider_val": kpi_lider_val,
    }
    return render(request, "front/fornecedores/fornecedor_list.html", ctx)


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
    obj = get_object_or_404(Fornecedor, pk=pk)
    return render(request, "front/fornecedores/fornecedor_detail.html", {"obj": obj})


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

@login_required
def localidade_list(request):
    qs = Localidade.objects.all().order_by("local")

    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(local__icontains=q) | Q(codigo__icontains=q))

    context = {
        "localidades": qs,
        "LocalidadeChoices": LocalidadeChoices,
        "q": q,
    }
    return render(request, "front/localidade_list.html", context)

@login_required
def localidade_create(request):
    if request.method == "POST":
        form = LocalidadeForm(request.POST)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.criado_por = request.user
            obj.atualizado_por = request.user
            obj.save()
            messages.success(request, "Localidade criada com sucesso.")
            return redirect("localidade_list")
        messages.error(request, "Corrija os erros do formulário.")
    else:
        form = LocalidadeForm()

    return render(request, "front/localidade_form.html", {"form": form, "editar": False})

@login_required
def localidade_update(request, pk):
    obj = get_object_or_404(Localidade, pk=pk)
    if request.method == "POST":
        form = LocalidadeForm(request.POST, instance=obj)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.atualizado_por = request.user
            obj.save()
            messages.success(request, "Localidade atualizada com sucesso.")
            return redirect("localidade_list")
        messages.error(request, "Corrija os erros do formulário.")
    else:
        form = LocalidadeForm(instance=obj)

    return render(request, "front/localidade_form.html", {"form": form, "editar": True, "obj": obj})

@login_required
def localidade_delete(request, pk):
    obj = get_object_or_404(Localidade, pk=pk)
    if request.method == "POST":
        nome = obj.local
        obj.delete()
        messages.success(request, f"Localidade '{nome}' excluída.")
        return redirect("localidade_list")
    # Não renderizamos página separada; exclusão via modal na listagem
    return redirect("localidade_list")

# DETAIL
@login_required
def localidade_detail(request, pk):
    obj = get_object_or_404(Localidade, pk=pk)
    # Contagens para enriquecer o detalhe
    itens_count = Item.objects.filter(localidade=obj).count()
    usuarios_count = Usuario.objects.filter(localidade=obj).count()

    itens = Item.objects.filter(localidade=obj).order_by("nome")[:12]
    usuarios = Usuario.objects.filter(localidade=obj).order_by("nome")[:12]

    context = {
        "obj": obj,
        "itens_count": itens_count,
        "usuarios_count": usuarios_count,
        "itens": itens,
        "usuarios": usuarios,
    }
    return render(request, "front/localidade_detail.html", context)



#################### CENTRO DE CUSTO ########################

@login_required
def centrocusto_list(request):
    """
    Lista de Centros de Custo com:
      - Filtros (q: número/departamento; pmb: sim/não)
      - KPIs: custo total mensal (locação), média por centro, total de itens locados, centro líder
      - Cards por centro: custo mensal e qtd de itens locados
      - Tabela com colunas financeiras
      - Paginação preservando filtros
    """
    q = (request.GET.get("q") or "").strip()
    pmb = (request.GET.get("pmb") or "").strip()

    qs = CentroCusto.objects.all().order_by("-created_at")
    if q:
        qs = qs.filter(Q(numero__icontains=q) | Q(departamento__icontains=q))
    if pmb in dict(SimNaoChoices.choices):
        qs = qs.filter(pmb=pmb)

    # Centros filtrados (ids)
    centros_ids = list(qs.values_list("id", flat=True))
    total_centros = qs.count()

    # -------- Agregações: SOMENTE itens locados, somando locacao__valor_mensal --------
    # protegendo nulos com or Decimal("0.00")
    locados_qs = Item.objects.filter(
        centro_custo_id__in=centros_ids,
        locado="sim",
    )

    globais = locados_qs.aggregate(
        custo_total=Sum("locacao__valor_mensal"),
        qtd_itens=Count("id"),
    )
    kpi_custo_total = globais.get("custo_total") or Decimal("0.00")
    kpi_itens_locados = globais.get("qtd_itens") or 0
    kpi_media_por_centro = (kpi_custo_total / total_centros) if total_centros else Decimal("0.00")

    # Por centro
    # [{'centro_custo': <id>, 'total': Decimal, 'count': int}, ...]
    por_centro = (
        locados_qs
        .values("centro_custo")
        .annotate(total=Sum("locacao__valor_mensal"), count=Count("id"))
    )
    centro_totais = {row["centro_custo"]: (row["total"] or Decimal("0.00")) for row in por_centro}
    centro_counts = {row["centro_custo"]: row["count"] for row in por_centro}

    # Centro com maior custo (no conjunto filtrado)
    kpi_top_cc_id = None
    kpi_top_cc_val = Decimal("0.00")
    if centro_totais:
        kpi_top_cc_id = max(centro_totais, key=lambda k: centro_totais[k])
        kpi_top_cc_val = centro_totais[kpi_top_cc_id] or Decimal("0.00")

    # -------- Paginação --------
    try:
        per_page = int(request.GET.get("pp") or 20)
        if per_page not in (10, 20, 50, 100):
            per_page = 20
    except (TypeError, ValueError):
        per_page = 20

    paginator = Paginator(qs, per_page)
    page_number = request.GET.get("page") or 1
    try:
        page_obj = paginator.page(page_number)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    # Anexa atributos prontos pra template (sem filtros/templatetags custom)
    for obj in page_obj.object_list:
        obj.custo_locacao_total = centro_totais.get(obj.id, Decimal("0.00"))
        obj.qtd_locados = centro_counts.get(obj.id, 0)

    # Centro líder (instância completa) — independente da página atual
    kpi_top_cc = None
    if kpi_top_cc_id:
        try:
            kpi_top_cc = CentroCusto.objects.get(id=kpi_top_cc_id)
        except CentroCusto.DoesNotExist:
            kpi_top_cc = None

    # Preserva querystring (sem 'page')
    params = request.GET.copy()
    params.pop("page", None)
    qs_keep = params.urlencode()

    ctx = {
        # lista
        "centros": page_obj.object_list,
        "page_obj": page_obj,
        "paginator": paginator,
        "per_page": per_page,
        "qs_keep": qs_keep,

        # filtros
        "total": total_centros,
        "q": q,
        "pmb": pmb,
        "pmb_choices": SimNaoChoices.choices,

        # KPIs
        "kpi_custo_total": kpi_custo_total,
        "kpi_media_por_centro": kpi_media_por_centro,
        "kpi_itens_locados": kpi_itens_locados,
        "kpi_top_cc": kpi_top_cc,
        "kpi_top_cc_val": kpi_top_cc_val,
    }
    return render(request, "front/centrocusto/centrocusto_list.html", ctx)


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


# DETAIL
@login_required
def centrocusto_detail(request, pk):
    obj = get_object_or_404(CentroCusto, pk=pk)
    itens_cc = (Item.objects
                .filter(centro_custo=obj)
                .select_related('subtipo','localidade')
                .order_by('nome'))
    return render(request, 'front/centrocusto/centrocusto_detail.html', {
        'obj': obj,
        'itens_cc': itens_cc,
    })


# DELETE (POST via modal)
@login_required
def centrocusto_delete(request, pk: int):
    obj = get_object_or_404(CentroCusto, pk=pk)
    if request.method == "POST":
        obj.delete()
        messages.success(request, "Centro de custo removido com sucesso.")
    else:
        messages.error(request, "Ação inválida.")
    return redirect("centrocusto_list")

######################### FUNÇÃO ################################


################ ITEM #######################################

@login_required
def item_create(request):
    if request.method == "POST":
        form = ItemForm(request.POST)
        locacao_form = LocacaoForm(request.POST if request.POST.get("locado") == "sim" else None)

        if form.is_valid():
            item = form.save(commit=False)
            item.criado_por = request.user
            item.save()

            # 🔹 Centro de custo origem já é salvo pelo form (nada extra aqui)

            # Se o item for locado, exige preenchimento de tempo e valor
            if item.locado == "sim":
                if locacao_form.is_valid():
                    locacao = locacao_form.save(commit=False)
                    locacao.equipamento = item

                    if not locacao.tempo_locado or not locacao.valor_mensal:
                        form.add_error(None, "Se o equipamento é locado, informe o tempo em meses e o valor mensal.")
                        return render(request, "front/equipamentos/cadastrar_equipamento.html", {
                            "form": form,
                            "locacao_form": locacao_form
                        })

                    locacao.save()
                else:
                    return render(request, "front/equipamentos/cadastrar_equipamento.html", {
                        "form": form,
                        "locacao_form": locacao_form
                    })

            return redirect("equipamentos_list")

    else:
        form = ItemForm()
        locacao_form = LocacaoForm()

    return render(
        request,
        "front/equipamentos/cadastrar_equipamento.html",
        {"form": form, "locacao_form": locacao_form}
    )


# ITEM LIST

def _norm(s: str) -> str:
    return unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower()





# importe seus modelos/choices
# from .models import Item, Subtipo, MovimentacaoItem, StatusItemChoices

def _build_queryset_and_context(request):
    """
    Monta queryset + contexto base (padrão único para página e partial).
    """
    qs = (
        Item.objects
        .select_related("subtipo", "localidade", "centro_custo", "fornecedor")
        .order_by("nome", "id")
    )

    # Prefetch de movimentações (se for necessário em outras views)
    mov_qs = MovimentacaoItem.objects.select_related("usuario").order_by("-created_at")
    qs = qs.prefetch_related(Prefetch("movimentacoes", queryset=mov_qs, to_attr="pref_movs"))

    p = request.GET

    # -------- Filtros ----------
    nome         = (p.get("nome") or "").strip()
    subtipo_id   = (p.get("subtipo") or "").strip()
    status_code  = (p.get("status") or "").strip()
    numero_serie = (p.get("numero_serie") or "").strip()
    usuario      = (p.get("usuario") or "").strip()  # caso use em algum lugar
    localidade   = (p.get("localidade") or "").strip()
    centro_custo = (p.get("centro_custo") or "").strip()
    fornecedor   = (p.get("fornecedor") or "").strip()

    if nome:
        qs = qs.filter(nome__icontains=nome)
    if subtipo_id:
        qs = qs.filter(subtipo_id=subtipo_id)
    if status_code:
        qs = qs.filter(status=status_code)
    if numero_serie:
        qs = qs.filter(numero_serie__icontains=numero_serie)
    if usuario:
        qs = qs.filter(movimentacoes__usuario__nome__icontains=usuario)
    if localidade:
        qs = qs.filter(localidade__local__icontains=localidade)
    if centro_custo:
        qs = qs.filter(
            Q(centro_custo__departamento__icontains=centro_custo) |
            Q(centro_custo__numero__icontains=centro_custo)
        )
    if fornecedor:
        qs = qs.filter(fornecedor__nome__icontains=fornecedor)

    qs = qs.distinct()
    filtered_total = qs.count()

    # -------- Paginação --------
    try:
        per_page = int(p.get("pp") or 20)
        if per_page not in (10, 20, 50, 100):
            per_page = 20
    except (TypeError, ValueError):
        per_page = 20

    paginator = Paginator(qs, per_page)
    page_number = p.get("page") or 1
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    # querystring preservada (sem page)
    params = p.copy()
    params.pop("page", None)
    qs_keep = params.urlencode()

    # -------- KPIs (status) --------
    total_ativos     = Item.objects.filter(status=StatusItemChoices.ATIVO).count()
    total_backup     = Item.objects.filter(status=StatusItemChoices.BACKUP).count()
    total_manutencao = Item.objects.filter(status=StatusItemChoices.MANUTENCAO).count()
    total_queimados  = Item.objects.filter(status=StatusItemChoices.DEFEITO).count()
    total_geral      = Item.objects.count()

    context = {
        "equipamentos": page_obj.object_list,
        "page_obj": page_obj,
        "paginator": paginator,
        "per_page": per_page,
        "qs_keep": qs_keep,
        "filtered_total": filtered_total,

        "row_start": page_obj.start_index(),  # índice base da página (para #)
        "subtipos": Subtipo.objects.all().order_by("nome"),
        "status_choices": StatusItemChoices.choices,

        "total_ativos": total_ativos,
        "total_backup": total_backup,
        "total_manutencao": total_manutencao,
        "total_queimados": total_queimados,
        "total_geral": total_geral,
    }
    return context


@login_required
def equipamentos_list(request):
    """
    Página completa e também endpoint de atualização parcial (Ajax).
    Se 'partial=1' vier na query ou X-Requested-With=XMLHttpRequest, devolve JSON com fragmentos.
    """
    context = _build_queryset_and_context(request)

    # Modo parcial (Ajax): devolve só os blocos que o front precisa atualizar
    is_partial = request.GET.get("partial") == "1" or request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if is_partial:
        tbody_html = render_to_string(
            "front/equipamentos/_tbody.html",
            context,
            request=request
        )
        pagination_html = render_to_string(
            "front/equipamentos/_pagination.html",
            context,
            request=request
        )
        kpis_html = render_to_string(
            "front/equipamentos/_kpis.html",
            context,
            request=request
        )
        return JsonResponse({
            "tbody": tbody_html,
            "pagination": pagination_html,
            "kpis": kpis_html,
            "count": context["filtered_total"],
        })

    # Página inteira
    return render(request, "front/equipamentos/equipamentos_list.html", context)

### ITEM / Equipamento detalhe 

@login_required
def equipamento_detalhe(request, pk: int):
    """
    Detalhe do equipamento:
      - Item + relações
      - Preventivas (com cálculo de próxima execução quando faltante) e flag de prazo
      - Histórico de movimentações
      - Comentários (se existir o model)
    """
    item = get_object_or_404(
        Item.objects.select_related(
            "subtipo", "localidade", "centro_custo", "fornecedor"
        ),
        pk=pk,
    )

    movimentacoes = (
        MovimentacaoItem.objects
        .filter(item=item)
        .select_related(
            "usuario",
            "localidade_destino", "centro_custo_destino",
            "localidade_origem",  "centro_custo_origem",
        )
        .order_by("-created_at")
    )

    preventivas = (
        Preventiva.objects
        .filter(equipamento=item)
        .select_related("checklist_modelo")
        .order_by("-data_proxima", "-data_ultima", "-created_at")
    )

    # Calcula próxima execução quando não houver e adiciona flag legível no template
    today = timezone.localdate()
    for p in preventivas:
        if not getattr(p, "data_proxima", None):
            dias = 0
            if getattr(p, "checklist_modelo", None) and getattr(p.checklist_modelo, "intervalo_dias", None):
                try:
                    dias = int(p.checklist_modelo.intervalo_dias)
                except Exception:
                    dias = 0
            elif getattr(item, "data_limite_preventiva", None):
                try:
                    dias = int(item.data_limite_preventiva)
                except Exception:
                    dias = 0
            if dias > 0:
                base = p.data_ultima or today
                p.data_proxima = base + timedelta(days=dias)

        # >>> não usar underscore para permitir no template
        p.flag_em_dia = (p.data_proxima is None) or (today <= p.data_proxima)

    # Comentários (se existir o model)
    comentarios = []
    try:
        from .models import ComentarioEquipamento  # ajuste se seu app difere
        comentarios = ComentarioEquipamento.objects.filter(
            equipamento=item
        ).select_related("criado_por").order_by("-created_at")
    except Exception:
        comentarios = []

    # Locação segura
    locacao = None
    try:
        locacao = item.locacao
    except Exception:
        locacao = None

    context = {
        "item": item,
        "movimentacoes": movimentacoes,
        "preventivas": preventivas,
        "comentarios": comentarios,
        "locacao": locacao,
        "today": today,
    }
    return render(request, "front/equipamentos/equipamento_detalhe.html", context)


@login_required
def editar_equipamento(request, pk):
    equipamento = get_object_or_404(Item, pk=pk)
    # Tenta pegar a locação já existente (OneToOne)
    try:
        locacao_instance = equipamento.locacao
    except Locacao.DoesNotExist:
        locacao_instance = None

    if request.method == "POST":
        form = ItemForm(request.POST, instance=equipamento)
        locacao_form = LocacaoForm(request.POST, instance=locacao_instance)

        if form.is_valid() and (not form.cleaned_data.get("locado") or locacao_form.is_valid()):
            item = form.save(commit=False)
            item.atualizado_por = request.user
            item.save()

            if item.locado == "sim":
                # Se já existe, atualiza; senão cria
                locacao = locacao_form.save(commit=False)
                locacao.equipamento = item
                locacao.save()
            else:
                # Se não é mais locado, exclui a locação antiga
                if locacao_instance:
                    locacao_instance.delete()

            return redirect("equipamentos_list")

    else:
        form = ItemForm(instance=equipamento)
        locacao_form = LocacaoForm(instance=locacao_instance)

    return render(
        request,
        "front/equipamentos/cadastrar_equipamento.html",
        {"form": form, "locacao_form": locacao_form, "editar": True}
    )

@require_POST
@login_required
def equipamento_excluir(request, pk: int):
    item = get_object_or_404(Item, pk=pk)
    item.delete()
    messages.success(request, "Item excluído com sucesso.")
    return redirect("equipamentos_list")


#########################################################################################################################

# Cadastro de licença
@login_required
def cadastrar_licenca(request):
    if request.method == "POST":
        item_form = ItemForm(request.POST)
        licenca_form = LicencaForm(request.POST)

        if item_form.is_valid() and licenca_form.is_valid():
            item = item_form.save(commit=False)
            item.criado_por = request.user
            item.atualizado_por = request.user
            item.save()

            licenca = licenca_form.save(commit=False)
            licenca.criado_por = request.user
            licenca.atualizado_por = request.user
            licenca.save()
            licenca_form.save_m2m()

            return redirect("lista_licencas")
    else:
        item_form = ItemForm()
        licenca_form = LicencaForm()

    return render(request, "cadastro_licenca.html", {
        "item_form": item_form,
        "licenca_form": licenca_form
    })

################ EQUIPAMENTO ##################



#def equipamento_delete(request, pk):
    #equipamento = get_object_or_404(Equipamento, pk=pk)
    #f request.method == 'POST':
        #equipamento.delete()
        #return redirect('equipamentos_list')
    #return render(request, 'equipamento/delete.html', {'obj': equipamento})

########### LOCADO ##################

def locacoes_list(request):
    locacoes = Locacao.objects.all()
    return render(request, 'locacao/list.html', {'locacoes': locacoes})

def locacao_create(request):
    form = LocacaoForm(request.POST or None)
    if form.is_valid():
        locacao = form.save(commit=False)
        locacao.save()
        return redirect('locacoes_list')
    return render(request, 'locacao/form.html', {'form': form})

def locacao_update(request, pk):
    locacao = get_object_or_404(Locacao, pk=pk)
    form = LocacaoForm(request.POST or None, instance=locacao)
    if form.is_valid():
        locacao = form.save(commit=False)
        locacao.save()
        return redirect('locacoes_list')
    return render(request, 'locacao/form.html', {'form': form})

def locacao_delete(request, pk):
    locacao = get_object_or_404(Locacao, pk=pk)
    if request.method == 'POST':
        locacao.delete()
        return redirect('locacoes_list')
    return render(request, 'locacao/delete.html', {'obj': locacao})

@login_required
def funcao_list(request):
    q = (request.GET.get("q") or "").strip()
    funcoes = Funcao.objects.all().order_by("nome")
    if q:
        funcoes = funcoes.filter(nome__icontains=q)
    ctx = {
        "funcoes": funcoes,
        "q": q,
        "total": funcoes.count(),
    }
    return render(request, "front/funcoes/funcao_list.html", ctx)


@login_required
def funcao_form(request, pk=None):
    instance = get_object_or_404(Funcao, pk=pk) if pk else None

    if request.method == "POST":
        form = FuncaoForm(request.POST, instance=instance)
        if form.is_valid():
            obj = form.save(commit=False)
            if instance is None:
                # se seu AuditModel usa esse campo
                obj.criado_por = request.user
            # idem para atualizado_por
            obj.atualizado_por = request.user
            obj.save()

            messages.success(request, "Função salva com sucesso.")
            # ✅ redireciona para a LISTA após salvar
            return redirect("funcoes_list")
    else:
        form = FuncaoForm(instance=instance)

    return render(
        request,
        "front/funcoes/funcao_form.html",
        {"form": form, "instance": instance},
    )


@login_required
def funcao_delete(request, pk):
    if request.method != "POST":
        messages.error(request, "Requisição inválida.")
        return redirect("funcoes_list")

    obj = get_object_or_404(Funcao, pk=pk)
    obj.delete()
    messages.success(request, "Função removida.")
    return redirect("funcoes_list")

### COMENTARIO ####

def comentarios_list(request):
    comentarios = Comentario.objects.all()
    return render(request, 'comentario/list.html', {'comentarios': comentarios})

def comentario_create(request):
    form = ComentarioForm(request.POST or None)
    if form.is_valid():
        comentario = form.save(commit=False)
        comentario.criado_por = request.user
        comentario.save()
        return redirect('comentarios_list')
    return render(request, 'comentario/form.html', {'form': form})

def comentario_update(request, pk):
    comentario = get_object_or_404(Comentario, pk=pk)
    form = ComentarioForm(request.POST or None, instance=comentario)
    if form.is_valid():
        form.save()
        return redirect('comentarios_list')
    return render(request, 'comentario/form.html', {'form': form})

def comentario_delete(request, pk):
    comentario = get_object_or_404(Comentario, pk=pk)
    if request.method == 'POST':
        comentario.delete()
        return redirect('comentarios_list')
    return render(request, 'comentario/delete.html', {'obj': comentario})

####################### MOVIMENTA ITEM ################
# views.py
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator, EmptyPage
from django.db.models import Count, Q
from django.shortcuts import render

# Ajuste conforme seu projeto
# from .models import MovimentacaoItem, TipoMovimentacaoChoices

@login_required
def movimentacao_list(request):
    """
    Lista de movimentações com:
      - Filtros: q (item), tipo, usuario, numero_serie, centro_custo
      - KPIs por tipo SEM lookup inseguro no template
      - Paginação 10/20/50/100 preservando filtros
      - Layout responsivo com cabeçalho sticky e chips por tipo
    """
    # --------- Entrada / Filtros ---------
    q             = (request.GET.get("q") or "").strip()                    # nome do item
    tipo          = (request.GET.get("tipo") or "").strip()                 # choice do tipo
    usuario_q     = (request.GET.get("usuario") or "").strip()              # nome do usuário
    numero_serie  = (request.GET.get("numero_serie") or "").strip()         # nº série do item
    centro_custo  = (request.GET.get("centro_custo") or "").strip()         # número ou departamento

    qs = (
        MovimentacaoItem.objects
        .select_related(
            "item", "usuario",
            "localidade_origem", "localidade_destino",
            "centro_custo_origem", "centro_custo_destino",
            "fornecedor_manutencao",
        )
        .order_by("-created_at")
    )

    if q:
        qs = qs.filter(item__nome__icontains=q)

    if tipo:
        qs = qs.filter(tipo_movimentacao=tipo)

    if usuario_q:
        qs = qs.filter(usuario__nome__icontains=usuario_q)

    if numero_serie:
        qs = qs.filter(item__numero_serie__icontains=numero_serie)

    if centro_custo:
        qs = qs.filter(
            Q(centro_custo_origem__numero__icontains=centro_custo) |
            Q(centro_custo_origem__departamento__icontains=centro_custo) |
            Q(centro_custo_destino__numero__icontains=centro_custo) |
            Q(centro_custo_destino__departamento__icontains=centro_custo)
        )

    # --------- KPIs (sobre o conjunto filtrado) ---------
    grouped = dict(qs.values_list("tipo_movimentacao").annotate(c=Count("id")).order_by())

    def c(key: str) -> int:
        return int(grouped.get(key, 0))

    kpi_entrada                 = c("entrada")
    kpi_transferencia           = c("transferencia")
    kpi_transferencia_equip     = c("transferencia_equipamento")
    kpi_envio_manutencao        = c("envio_manutencao")
    kpi_retorno_manutencao      = c("retorno_manutencao")
    kpi_baixa                   = c("baixa")
    kpi_outros                  = c("outros")

    total_filtrado = (
        kpi_entrada + kpi_transferencia + kpi_transferencia_equip +
        kpi_envio_manutencao + kpi_retorno_manutencao + kpi_baixa + kpi_outros
    )

    tipos_choices = list(TipoMovimentacaoChoices.choices)

    # --------- Paginação ---------
    try:
        per_page = int(request.GET.get("pp") or 20)
        if per_page not in (10, 20, 50, 100):
            per_page = 20
    except (TypeError, ValueError):
        per_page = 20

    paginator = Paginator(qs, per_page)
    page_num = request.GET.get("page") or 1
    try:
        page_obj = paginator.page(page_num)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    # Preserva querystring (sem 'page')
    params = request.GET.copy()
    params.pop("page", None)
    qs_keep = params.urlencode()

    context = {
        # Dados
        "movimentacoes": page_obj.object_list,
        "page_obj": page_obj,
        "paginator": paginator,
        "per_page": per_page,
        "qs_keep": qs_keep,

        # Filtros de entrada
        "q": q,
        "tipo": tipo,
        "usuario_q": usuario_q,
        "numero_serie": numero_serie,
        "centro_custo": centro_custo,

        # Choices
        "tipos": tipos_choices,

        # KPIs
        "kpi_entrada": kpi_entrada,
        "kpi_transferencia": kpi_transferencia,
        "kpi_transferencia_equip": kpi_transferencia_equip,
        "kpi_envio_manutencao": kpi_envio_manutencao,
        "kpi_retorno_manutencao": kpi_retorno_manutencao,
        "kpi_baixa": kpi_baixa,
        "kpi_outros": kpi_outros,
        "total_filtrado": total_filtrado,
    }
    return render(request, "front/movimentacao_list.html", context)





@login_required
def movimentacao_create(request):
    if request.method == "POST":
        form = MovimentacaoItemForm(request.POST, request.FILES)
        if form.is_valid():
            mov = form.save(commit=False)
            # Remover usuário em envio/retorno de manutenção
            if mov.tipo_movimentacao in ("envio_manutencao", "retorno_manutencao", "retorno"):
                mov.usuario = None
            mov.criado_por = request.user
            mov.save()  # regras de estoque/status são aplicadas no model.save()
            messages.success(request, "Movimentação registrada com sucesso!")
            return redirect("movimentacao_list")
        else:
            print("⚠ Erros no formulário:", form.errors.as_ul())
            messages.error(request, "Erro ao registrar movimentação. Verifique os campos destacados.")
    else:
        form = MovimentacaoItemForm()

    return render(request, "front\\movimentacao_form.html", {"form": form})






@login_required
def movimentacao_detail(request, pk):
    mov = get_object_or_404(MovimentacaoItem, pk=pk)

    # Origem (sempre a fotografada no momento da criação)
    origem_localidade = mov.localidade_origem.local if mov.localidade_origem else None
    origem_cc_num = mov.centro_custo_origem.numero if mov.centro_custo_origem else None
    origem_cc_dep = mov.centro_custo_origem.departamento if mov.centro_custo_origem else None

    # Destino (campos de destino da movimentação)
    dest_localidade = mov.localidade_destino.local if mov.localidade_destino else None
    dest_cc_num = mov.centro_custo_destino.numero if mov.centro_custo_destino else None
    dest_cc_dep = mov.centro_custo_destino.departamento if mov.centro_custo_destino else None

    # Status exibido após a movimentação
    if mov.tipo_movimentacao in ("retorno", "retorno_manutencao"):
        status_pos = "Backup"
    elif mov.tipo_movimentacao == "transferencia_equipamento" and mov.status_transferencia:
        # label amigável do enum StatusItemChoices
        status_pos = dict(StatusItemChoices.choices).get(mov.status_transferencia, mov.status_transferencia)
    else:
        status_pos = mov.item.get_status_display() if mov.item else "—"

    # Efeito no estoque (somente texto)
    efeito_map = {
        "entrada": f"Entrada (+{mov.quantidade or 1})",
        "baixa": f"Saída (−{mov.quantidade or 1})",
        "envio_manutencao": f"Saída (−{mov.quantidade or 1})",
        "transferencia": "Sem alteração de quantidade (transferência)",
        "transferencia_equipamento": "Sem alteração de quantidade (transferência)",
        "retorno": "Sem alteração de quantidade (retorno)",
        "retorno_manutencao": "Sem alteração de quantidade (retorno)",
    }
    efeito_estoque = efeito_map.get(mov.tipo_movimentacao, "—")

    ctx = {
        "movimentacao": mov,
        "origem_localidade": origem_localidade,
        "origem_cc_num": origem_cc_num,
        "origem_cc_dep": origem_cc_dep,
        "dest_localidade": dest_localidade,
        "dest_cc_num": dest_cc_num,
        "dest_cc_dep": dest_cc_dep,
        "status_pos": status_pos,
        "efeito_estoque": efeito_estoque,
    }
    return render(request, "front/movimentacao_detail.html", ctx)


def movimentacao_update(request, pk):
    mov = get_object_or_404(MovimentacaoItem, pk=pk)
    form = MovimentacaoItemForm(request.POST or None, request.FILES or None, instance=mov)
    if form.is_valid():
        mov = form.save(commit=False)
        mov.atualizado_por = request.user
        mov.save()
        return redirect('movimentacoes_list')
    return render(request, 'movimentacaoitem/form.html', {'form': form})

def movimentacao_delete(request, pk):
    mov = get_object_or_404(MovimentacaoItem, pk=pk)
    if request.method == 'POST':
        mov.delete()
        return redirect('movimentacoes_list')
    return render(request, 'movimentacaoitem/delete.html', {'obj': mov})

###################### CICLO MANUTENÇÃO ##########################

# LISTA DE CICLOS
def ciclos_list(request):
    ciclos = CicloManutencao.objects.all().order_by('-created_at')
    return render(request, 'ciclomanutencao/list.html', {'ciclos': ciclos})

# CRIAR CICLO (INTELIGENTE)
def ciclo_create(request):
    form = CicloManutencaoForm(request.POST or None)

    if form.is_valid():
        ciclo = form.save(commit=False)
        item = ciclo.item

        # Verifica se já existe um ciclo aberto
        ciclo_aberto = CicloManutencao.objects.filter(item=item, data_fim__isnull=True).exists()
        if ciclo_aberto:
            messages.error(request, f"O item '{item.nome}' já possui um ciclo de manutenção em andamento.")
            return render(request, 'ciclomanutencao/form.html', {'form': form})

        ciclo.criado_por = request.user
        ciclo.atualizado_por = request.user
        item.status = 'manutencao'
        item.atualizado_por = request.user

        item.save()
        ciclo.save()

        messages.success(request, f"Ciclo de manutenção iniciado para '{item.nome}'.")
        return redirect('ciclos_list')

    return render(request, 'ciclomanutencao/form.html', {'form': form})

# ATUALIZAR CICLO (ENCERRAR)
def ciclo_update(request, pk):
    ciclo = get_object_or_404(CicloManutencao, pk=pk)
    form = CicloManutencaoForm(request.POST or None, instance=ciclo)

    if form.is_valid():
        ciclo = form.save(commit=False)
        ciclo.atualizado_por = request.user
        item = ciclo.item

        if ciclo.data_fim:
            item.status = 'ativo'  # ou 'backup', conforme regra de negócio
            item.atualizado_por = request.user
            item.save()

            messages.success(request, f"Ciclo encerrado. '{item.nome}' voltou para operação.")

        ciclo.save()
        return redirect('ciclos_list')

    return render(request, 'ciclomanutencao/form.html', {'form': form})

# EXCLUIR CICLO
def ciclo_delete(request, pk):
    ciclo = get_object_or_404(CicloManutencao, pk=pk)
    if request.method == 'POST':
        ciclo.delete()
        return redirect('ciclos_list')
    return render(request, 'ciclomanutencao/delete.html', {'obj': ciclo})

@login_required
def sobre_plataforma(request):
    # Versão/identidade opcionais via settings (defina se quiser)
    app_name = getattr(settings, "PROJECT_NAME", "Controle de Ativos")
    version = getattr(settings, "APP_VERSION", "1.0.0")
    build_date = getattr(settings, "APP_BUILD_DATE", None)

    # Métricas rápidas
    total_itens = Item.objects.count()
    total_ativos = Item.objects.filter(status=StatusItemChoices.ATIVO).count()
    total_backup = Item.objects.filter(status=StatusItemChoices.BACKUP).count()
    total_manut = Item.objects.filter(status=StatusItemChoices.MANUTENCAO).count()
    total_defeito = Item.objects.filter(status=StatusItemChoices.DEFEITO).count()

    ctx = {
        "app_name": app_name,
        "version": version,
        "build_date": build_date,
        "now": timezone.now(),
        "totais": {
            "itens": total_itens,
            "ativos": total_ativos,
            "backup": total_backup,
            "manutencao": total_manut,
            "defeitos": total_defeito,
            "usuarios": Usuario.objects.count(),
            "localidades": Localidade.objects.count(),
            "centros": CentroCusto.objects.count(),
            "fornecedores": Fornecedor.objects.count(),
            "subtipos": Subtipo.objects.count(),
            "categorias": Categoria.objects.count(),
            "funcoes": Funcao.objects.count(),
            "licencas": Licenca.objects.count(),
            "movimentacoes": MovimentacaoItem.objects.count(),
        },
    }
    return render(request, "front/sobre_plataforma.html", ctx)

# ----------------- CHECKLIST: LIST/CRUD -----------------
def _calc_proxima_para_item(item: Item, base: date | None = None) -> date | None:
    """
    Calcula a próxima data de preventiva usando 'data_limite_preventiva' (dias) do Item.
    Se o item não exige preventiva, retorna None.
    """
    if not item or item.precisa_preventiva != SimNaoChoices.SIM:
        return None
    dias = item.data_limite_preventiva or 0
    if dias <= 0:
        return None
    base = base or timezone.localdate()
    return base + timedelta(days=int(dias))

# =========================
#   CHECKLIST - LIST
# =========================
@login_required
def checklist_list(request):
    q = (request.GET.get("q") or "").strip()

    checklists = (
        CheckListModelo.objects
        .all()
        .annotate(perguntas_count=Count("perguntas"))  # related_name= 'perguntas'
        .order_by("nome")
    )
    if q:
        checklists = checklists.filter(Q(nome__icontains=q) | Q(descricao__icontains=q))

    context = {
        "checklists": checklists,
        "q": q,
        "total": checklists.count(),
    }
    return render(request, "front/preventivas/checklist_list.html", context)

# =========================
#   CHECKLIST - CREATE/EDIT
# =========================
@login_required
def checklist_form(request, pk=None):
    instance = get_object_or_404(CheckListModelo, pk=pk) if pk else None
    if request.method == "POST":
        form = ChecklistModeloForm(request.POST, instance=instance)
        if form.is_valid():
            obj = form.save(commit=False)
            if instance is None:
                obj.criado_por = request.user
            obj.atualizado_por = request.user
            obj.save()
            messages.success(request, "Checklist salvo com sucesso.")
            return redirect("checklist_form", pk=obj.pk)
    else:
        form = ChecklistModeloForm(instance=instance)

    perguntas = CheckListPergunta.objects.filter(checklist_modelo=instance).order_by("id") if instance else []
    return render(
        request,
        "front/preventivas/checklist_form.html",
        {"form": form, "perguntas": perguntas},
    )

@login_required
def checklist_delete(request, pk):
    if request.method != "POST":
        messages.error(request, "Requisição inválida.")
        return redirect("checklist_list")
    obj = get_object_or_404(CheckListModelo, pk=pk)
    obj.delete()
    messages.success(request, "Checklist removido.")
    return redirect("checklist_list")

# =========================
#   PERGUNTA - CREATE/EDIT
# =========================
@login_required
def pergunta_form(request, checklist_pk, pk=None):
    checklist = get_object_or_404(CheckListModelo, pk=checklist_pk)
    instance = get_object_or_404(CheckListPergunta, pk=pk, checklist_modelo=checklist) if pk else None

    if request.method == "POST":
        form = ChecklistPerguntaForm(request.POST, instance=instance)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.checklist_modelo = checklist
            if instance is None:
                obj.criado_por = request.user
            obj.atualizado_por = request.user
            obj.save()
            messages.success(request, "Pergunta salva com sucesso.")
            return redirect("checklist_form", pk=checklist.pk)
    else:
        form = ChecklistPerguntaForm(instance=instance)

    return render(
        request,
        "front/preventivas/pergunta_form.html",
        {"form": form, "checklist": checklist},
    )

@login_required
def pergunta_delete(request, checklist_pk, pk):
    if request.method != "POST":
        messages.error(request, "Requisição inválida.")
        return redirect("checklist_form", pk=checklist_pk)
    checklist = get_object_or_404(CheckListModelo, pk=checklist_pk)
    pergunta = get_object_or_404(CheckListPergunta, pk=pk, checklist_modelo=checklist)
    pergunta.delete()
    messages.success(request, "Pergunta removida.")
    return redirect("checklist_form", pk=checklist.pk)

# =========================
#   PREVENTIVAS - LIST
# =========================
@login_required
def preventiva_list(request):
    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()

    qs = (
        Preventiva.objects.select_related("equipamento", "checklist_modelo")
        .order_by("-data_proxima", "-data_ultima")
    )
    if q:
        qs = qs.filter(equipamento__nome__icontains=q)

    # status visual (ok/vencida)
    today = timezone.localdate()
    if status == "ok":
        qs = qs.filter(data_proxima__gte=today)
    elif status == "vencida":
        qs = qs.filter(data_proxima__lt=today)

    ctx = {
        "preventivas": qs,
        "total": qs.count(),
    }
    return render(request, "front/preventivas/preventiva_list.html", ctx)

# =========================
#   PREVENTIVA - START
# =========================
@login_required
def preventiva_start(request, item_id=None):
    item_instance = None
    if item_id:
        item_instance = get_object_or_404(Item.objects.select_related("subtipo"), pk=item_id)

    if request.method == "POST":
        form = PreventivaStartForm(request.POST, item_instance=item_instance)
        if form.is_valid():
            item = form.cleaned_data["item"]
            modelo = form.cleaned_data["checklist_modelo"]
            # cria (ou obtém) a preventiva “ativa” para o item + modelo
            prev, created = Preventiva.objects.get_or_create(
                equipamento=item,
                checklist_modelo=modelo,
                defaults={
                    "criado_por": request.user,
                    "atualizado_por": request.user,
                    "data_ultima": None,
                    "data_proxima": _calc_proxima_para_item(item, timezone.localdate()),
                },
            )
            if not created:
                prev.atualizado_por = request.user
                if not prev.data_proxima:
                    prev.data_proxima = _calc_proxima_para_item(item, timezone.localdate())
                prev.save(update_fields=["atualizado_por", "data_proxima", "updated_at"])
            messages.success(request, "Preventiva inicializada.")
            return redirect("preventiva_exec", pk=prev.pk)
    else:
        form = PreventivaStartForm(item_instance=item_instance)

    return render(request, "front/preventivas/preventiva_start.html", {"form": form})

# alias para a URL com item_id
@login_required
def preventiva_start_item(request, item_id):
    return preventiva_start(request, item_id=item_id)

# =========================
#   PREVENTIVA - DETAIL
# =========================
@login_required
def preventiva_detail(request, pk):
    """
    Exibe o histórico completo de execuções com filtros.
    Fotos/respostas são por execução (não sobrescreve nada).
    """
    preventiva = get_object_or_404(
        Preventiva.objects.select_related('equipamento', 'checklist_modelo'),
        pk=pk
    )

    # Filtros simples por período (opcional)
    ini = request.GET.get("inicio") or ""
    fim = request.GET.get("fim") or ""
    try:
        dt_ini = datetime.strptime(ini, "%Y-%m-%d").date() if ini else None
    except Exception:
        dt_ini = None
    try:
        dt_fim = datetime.strptime(fim, "%Y-%m-%d").date() if fim else None
    except Exception:
        dt_fim = None

    exec_qs = preventiva.execucoes.all().select_related("preventiva").order_by("-data_execucao", "-id")
    if dt_ini:
        exec_qs = exec_qs.filter(data_execucao__gte=dt_ini)
    if dt_fim:
        exec_qs = exec_qs.filter(data_execucao__lte=dt_fim)

    # Mantemos a ordem definida no checklist
    perguntas = CheckListPergunta.objects.filter(
        checklist_modelo=preventiva.checklist_modelo
    ).order_by("ordem", "id")

    # Monta a estrutura para o template: cada execução com suas respostas ordenadas
    execucoes = []
    for ex in exec_qs:
        resp_map = {r.pergunta_id: r for r in ex.respostas.select_related("pergunta")}
        linhas = []
        for p in perguntas:
            r = resp_map.get(p.id)
            linhas.append({
                "pergunta": p,
                "resposta": (r.resposta if r else None),
                "respondido_em": getattr(r, "created_at", None),
            })
        execucoes.append({"obj": ex, "linhas": linhas})

    context = {
        "preventiva": preventiva,
        "perguntas": perguntas,
        "execucoes": execucoes,
        "today": timezone.localdate(),
        "dt_ini": dt_ini,
        "dt_fim": dt_fim,
    }
    return render(request, "front/preventivas/preventiva_detail.html", context)


# =========================
#   PREVENTIVA - EXEC
# =========================
@login_required
def preventiva_exec(request, pk):
    preventiva = get_object_or_404(Preventiva, pk=pk)

    # Perguntas na ordem correta
    perguntas = (
        CheckListPergunta.objects
        .filter(checklist_modelo=preventiva.checklist_modelo)
        .order_by('ordem', 'id')
    )

    # Lista de opções (se tipo "escolha")
    for p in perguntas:
        p.opcoes_list = []
        if getattr(p, "opcoes", None):
            p.opcoes_list = [o.strip() for o in str(p.opcoes).split(",") if o.strip()]

    def _norm_tipo(tipo):
        return str(tipo or "").strip().lower().replace("-", "_").replace("/", "_")

    def _validar_e_coletar_respostas():
        """
        Lê POST, valida e devolve (erros, respostas_bulk_instancias_NAO_salvas).
        Cada instância já vem com (preventiva, pergunta, resposta); o vínculo
        à execução será adicionado após criarmos a execucao.
        """
        erros = []
        respostas_bulk = []

        for p in perguntas:
            name = f"r_{p.id}"          # <-- nome único por pergunta
            raw = (request.POST.get(name) or "").strip()
            tipo = _norm_tipo(p.tipo_resposta)
            obrig = (p.obrigatorio == "sim")

            # SIM/NÃO (choices 'sim'/'nao')
            if tipo in ("sim_nao", "booleano", "bool", "sn"):
                if raw not in ("sim", "nao"):
                    if obrig:
                        erros.append(f'Pergunta "{p.texto_pergunta}": selecione "Sim" ou "Não".')
                valor = raw if raw in ("sim", "nao") else ("" if not obrig else None)
                if obrig and valor is None:
                    continue  # não cria a resposta, volta com erro

            # NÚMERO
            elif tipo in ("numero", "inteiro", "decimal"):
                if raw == "":
                    if obrig:
                        erros.append(f'Pergunta "{p.texto_pergunta}": preencha um número.')
                    valor = ""
                else:
                    try:
                        _ = Decimal(raw)  # apenas valida
                        valor = raw
                    except Exception:
                        erros.append(f'Pergunta "{p.texto_pergunta}": valor numérico inválido.')
                        valor = None

            # ESCOLHA (opções)
            elif tipo in ("escolha", "opcao", "choice"):
                if raw == "":
                    if obrig:
                        erros.append(f'Pergunta "{p.texto_pergunta}": selecione uma opção.')
                    valor = ""
                else:
                    if p.opcoes_list and raw not in p.opcoes_list:
                        erros.append(f'Pergunta "{p.texto_pergunta}": opção inválida.')
                        valor = None
                    else:
                        valor = raw

            # TEXTO (default)
            else:
                if obrig and raw == "":
                    erros.append(f'Pergunta "{p.texto_pergunta}": resposta obrigatória.')
                valor = raw

            # Se houve erro nesta pergunta, não empilhamos
            if any(msg for msg in erros if p.texto_pergunta in msg):
                continue

            respostas_bulk.append(PreventivaResposta(
                preventiva=preventiva,
                pergunta=p,
                resposta=valor,
                criado_por=request.user,
                atualizado_por=request.user,
            ))

        return erros, respostas_bulk

    if request.method == "POST":
        erros, respostas_bulk = _validar_e_coletar_respostas()
        if erros:
            for e in erros:
                messages.error(request, e)
            # retorna o form SEM gravar nada (evita transação quebrada)
            return render(request, "front/preventivas/preventiva_exec.html", {
                "preventiva": preventiva,
                "perguntas": perguntas,
            })

        # Coleta evidências e observação desta execução
        foto_antes  = request.FILES.get("foto_antes")
        foto_depois = request.FILES.get("foto_depois")
        observacao  = (request.POST.get("observacao") or "").strip()

        with transaction.atomic():
            # 1) cria a execução (histórico) — fotos e observação ficam AQUI
            hoje = timezone.now().date()
            execucao = PreventivaExecucao.objects.create(
                preventiva=preventiva,
                data_execucao=hoje,
                observacao=observacao,
                foto_antes=foto_antes,
                foto_depois=foto_depois,
                criado_por=request.user,
                atualizado_por=request.user,
            )

            # 2) vincula a execução às respostas e salva em lote
            for r in respostas_bulk:
                r.execucao = execucao
            if respostas_bulk:
                PreventivaResposta.objects.bulk_create(respostas_bulk)

            # 3) atualiza agenda da Preventiva (mantém histórico pela execucao)
            dias = 0
            if preventiva.checklist_modelo and preventiva.checklist_modelo.intervalo_dias:
                dias = int(preventiva.checklist_modelo.intervalo_dias)
            elif preventiva.equipamento and getattr(preventiva.equipamento, "data_limite_preventiva", None):
                try:
                    dias = int(preventiva.equipamento.data_limite_preventiva or 0)
                except Exception:
                    dias = 0

            preventiva.data_ultima = hoje
            preventiva.data_proxima = (hoje + timedelta(days=dias)) if dias > 0 else None
            preventiva.dentro_do_prazo = (
                (preventiva.data_proxima is None) or (timezone.now().date() <= preventiva.data_proxima)
            )

            # Mantemos “última evidência” no objeto Preventiva (retrocompatibilidade)
            update_fields = ["data_ultima", "data_proxima", "dentro_do_prazo", "updated_at"]
            if observacao:
                preventiva.observacao = observacao
                update_fields.append("observacao")
            if foto_antes:
                preventiva.foto_antes = foto_antes
                update_fields.append("foto_antes")
            if foto_depois:
                preventiva.foto_depois = foto_depois
                update_fields.append("foto_depois")
            preventiva.save(update_fields=update_fields)

        messages.success(request, "Preventiva registrada com sucesso.")
        return redirect("preventiva_detail", pk=preventiva.pk)

    # GET
    return render(request, "front/preventivas/preventiva_exec.html", {
        "preventiva": preventiva,
        "perguntas": perguntas,
    })


# =========================
# Helpers de datas/séries
# =========================
def _month_key(dt):
    """YYYY-MM para indexação."""
    return f"{dt.year:04d}-{dt.month:02d}"

def _last_n_month_stamps(n=12):
    """Lista de (ano, mês) dos últimos n meses, do mais antigo ao mais recente."""
    now = timezone.localtime()
    y, m = now.year, now.month
    out = []
    for _ in range(n):
        out.append((y, m))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return list(reversed(out))

def _labels_pt_br(stamps):
    """Gera labels 'Mes/AnoCurto' ex.: Jan/25 a partir de (ano, mês)."""
    nomes = ["Jan","Fev","Mar","Abr","Mai","Jun","Jul","Ago","Set","Out","Nov","Dez"]
    return [f"{nomes[m-1]}/{str(y)[-2:]}" for (y, m) in stamps]

def _align_series(stamps, qs_month_count, field_name="c"):
    """
    Alinha uma série mensal (dict {'YYYY-MM': count}) aos stamps fornecidos.
    qs_month_count: queryset com values('m').annotate(c=Count(...))
                    onde 'm' = TruncMonth(), retornado como datetime.
    """
    m2v = {}
    for row in qs_month_count:
        mdt = row["m"]
        if not isinstance(mdt, datetime):
            # TruncMonth retorna datetime/tz-aware
            continue
        m2v[_month_key(timezone.localtime(mdt))] = row[field_name]
    out = []
    for (y, m) in stamps:
        out.append(int(m2v.get(f"{y:04d}-{m:02d}", 0)))
    return out


# =========================
# Helpers de custo de licença
# =========================
def _custo_mensal_lic(lic: Licenca) -> Decimal:
    cm = lic.custo_mensal()  # helper implementado na sua model
    return cm if cm is not None else Decimal("0.00")


@login_required
def dashboard(request):
    now = timezone.localtime()
    stamps12 = _last_n_month_stamps(12)
    stamps6 = _last_n_month_stamps(6)
    labels12 = _labels_pt_br(stamps12)
    labels6 = _labels_pt_br(stamps6)
    start12 = timezone.make_aware(datetime(stamps12[0][0], stamps12[0][1], 1))
    start6  = timezone.make_aware(datetime(stamps6[0][0],  stamps6[0][1],  1))

    # =============================
    # KPIs (status dos itens)
    # =============================
    total_geral = Item.objects.count()
    total_ativos = Item.objects.filter(status="ativo").count()
    total_backup = Item.objects.filter(status="backup").count()
    total_manutencao = Item.objects.filter(status="manutencao").count()
    # Mantém compatibilidade com possíveis dados legados "queimado"
    total_defeito = Item.objects.filter(status__in=["defeito", "queimado"]).count()

    # ======================================
    # Movimentações por mês (últimos 12)
    # ======================================
    mov_base = MovimentacaoItem.objects.filter(created_at__gte=start12)

    def _serie(tipo: str):
        qs = (
            mov_base.filter(tipo_movimentacao=tipo)
            .annotate(m=TruncMonth("created_at"))
            .values("m")
            .annotate(c=Count("id"))
            .order_by("m")
        )
        return _align_series(stamps12, qs)

    mov_entrada       = _serie("entrada")
    mov_baixa         = _serie("baixa")
    mov_transferencia = _serie("transferencia")
    mov_envio         = _serie("envio_manutencao")
    mov_retorno       = _serie("retorno_manutencao")

    # ======================================
    # Preventivas: OK x Vencida + Próximas
    # ======================================
    prev_total = Item.objects.filter(precisa_preventiva="sim").count() if hasattr(Item, "precisa_preventiva") else 0
    prev_vencida = 0

    if hasattr(Item, "precisa_preventiva") and hasattr(Item, "data_limite_preventiva"):
        fld = Item._meta.get_field("data_limite_preventiva")
        today = timezone.localdate()

        if isinstance(fld, (DateField, DateTimeField)):
            filtro_venc = {"precisa_preventiva": "sim", "data_limite_preventiva__lt": today}
        elif isinstance(fld, (IntegerField, BigIntegerField)):
            # Caso o campo seja usado como data (YYYYMMDD). Se na sua base ele é "dias", ajuste esta parte.
            today_int = int(today.strftime("%Y%m%d"))
            filtro_venc = {"precisa_preventiva": "sim", "data_limite_preventiva__lt": today_int}
        elif isinstance(fld, CharField):
            today_str = today.strftime("%Y-%m-%d")
            filtro_venc = {"precisa_preventiva": "sim", "data_limite_preventiva__lt": today_str}
        else:
            filtro_venc = {"precisa_preventiva": "sim"}

        prev_vencida = Item.objects.filter(**filtro_venc).count()

    prev_ok = max(0, prev_total - prev_vencida)

    proximas = []
    if Preventiva is not None and hasattr(Preventiva, "data_proxima"):
        proximas = (
            Preventiva.objects
            .filter(data_proxima__gte=timezone.localdate())
            .select_related("equipamento", "checklist_modelo")
            .order_by("data_proxima")[:10]
        )

    # ======================================
    # Itens por Subtipo / Localidade / CC
    # ======================================
    # Subtipo (Top 10)
    sub_qs = (
        Item.objects.values("subtipo__nome")
        .annotate(q=Count("id"))
        .order_by("-q")[:10]
    )
    chart_subtipo_labels = [r["subtipo__nome"] or "—" for r in sub_qs]
    chart_subtipo_data   = [int(r["q"]) for r in sub_qs]

    # Localidade (Top 8)
    loc_qs = (
        Item.objects.values("localidade__local")
        .annotate(q=Count("id"))
        .order_by("-q")[:8]
    )
    chart_loc_labels = [r["localidade__local"] or "—" for r in loc_qs]
    chart_loc_data   = [int(r["q"]) for r in loc_qs]

    # Centro de Custo (Top 8) - quantidade de itens
    cc_qs = (
        Item.objects.values("centro_custo__numero", "centro_custo__departamento")
        .annotate(q=Count("id"))
        .order_by("-q")[:8]
    )
    chart_cc_labels = [
        f'{r["centro_custo__numero"]} - {r["centro_custo__departamento"]}'
        if r["centro_custo__numero"] else "—" for r in cc_qs
    ]
    chart_cc_data   = [int(r["q"]) for r in cc_qs]

    # ======================================
    # Top Itens mais movimentados (12m)
    # ======================================
    top_mov_qs = (
        mov_base.values("item__nome")
        .annotate(q=Count("id"))
        .order_by("-q")[:10]
    )
    top_mov_labels = [r["item__nome"] or "—" for r in top_mov_qs]
    top_mov_data   = [int(r["q"]) for r in top_mov_qs]

    # ======================================
    # Custo de manutenção (6 meses)
    # ======================================
    custo_labels = labels6
    custo_data = [0] * len(labels6)
    if CicloManutencao is not None and hasattr(CicloManutencao, "custo"):
        cm_qs = (
            CicloManutencao.objects.filter(created_at__gte=start6)
            .annotate(m=TruncMonth("created_at"))
            .values("m")
            .annotate(total=Sum("custo"))
            .order_by("m")
        )
        m2v = {}
        for r in cm_qs:
            mdt = r["m"]
            if isinstance(mdt, datetime):
                k = _month_key(timezone.localtime(mdt))
                m2v[k] = float(r["total"] or 0)
        custo_data = []
        for (y, m) in stamps6:
            custo_data.append(m2v.get(f"{y:04d}-{m:02d}", 0.0))

    # ============================================================
    # *** NOVO/ATUALIZADO *** CUSTOS MENSAIS
    #   - Itens: locação (valor_mensal)
    #   - Licenças: custo do lote da ATRIBUIÇÃO (m.custo_mensal_usado)
    # ============================================================
    # Itens atualmente com usuário (ignora entrada/baixa e devoluções)
    last_posse_qs = (
        MovimentacaoItem.objects
        .filter(item=OuterRef("pk"))
        .exclude(tipo_movimentacao__in=["entrada", "baixa"])
        .order_by("-created_at", "-id")
    )
    itens_associados = (
        Item.objects
        .annotate(
            _last_user=Subquery(last_posse_qs.values("usuario_id")[:1]),
            _last_tipo=Subquery(last_posse_qs.values("tipo_movimentacao")[:1]),
            _last_tp=Subquery(last_posse_qs.values("tipo_transferencia")[:1]),
        )
        .filter(~Q(_last_user=None))
        .exclude(_last_tipo__in=("envio_manutencao", "retorno_manutencao", "retorno"))
        .exclude(Q(_last_tipo="transferencia") & ~Q(_last_tp="entrega"))
        .values("id", "_last_user", "centro_custo_id", "locado")
    )
    item_ids = [r["id"] for r in itens_associados]
    loc_map = {
        row["equipamento_id"]: (row["valor_mensal"] or Decimal("0.00"))
        for row in (
            Locacao.objects
            .filter(equipamento_id__in=item_ids)
            .values("equipamento_id", "valor_mensal")
        )
    }

    user_cost_items = defaultdict(Decimal)  # uid -> R$/mês
    cc_cost_items   = defaultdict(Decimal)  # ccid -> R$/mês
    for r in itens_associados:
        if r["locado"] == SimNaoChoices.SIM:
            vm = loc_map.get(r["id"], Decimal("0.00")) or Decimal("0.00")
            if vm > 0:
                user_cost_items[r["_last_user"]] += vm
                cc_cost_items[r["centro_custo_id"]] += vm

    # Licenças: considera o ÚLTIMO movimento por par (licenca, usuario) e usa custo_mensal_usado
    mov_l_qs = (
        MovimentacaoLicenca.objects
        .select_related("licenca", "usuario__centro_custo", "centro_custo_destino", "lote")
        .order_by("licenca_id", "usuario_id", "created_at", "id")
    )
    last_by_pair = {}  # (licenca_id, usuario_id) -> mov
    for m in mov_l_qs:
        if m.usuario_id is None:
            continue
        last_by_pair[(m.licenca_id, m.usuario_id)] = m

    user_cost_lics = defaultdict(Decimal)
    cc_cost_lics   = defaultdict(Decimal)
    lic_cost_map   = defaultdict(Decimal)  # p/ ranking de licenças

    for (lic_id, uid), m in last_by_pair.items():
        if m.tipo != TipoMovLicencaChoices.ATRIBUICAO:
            continue

        cm = m.custo_mensal_usado or Decimal("0.00")
        if cm <= 0:
            continue

        # Por usuário
        user_cost_lics[uid] += cm

        # Por CC (prioridade: CC do usuário, depois mov, depois licença)
        ccid = getattr(getattr(m.usuario, "centro_custo", None), "id", None) \
               or m.centro_custo_destino_id \
               or getattr(m.licenca.centro_custo, "id", None)
        cc_cost_lics[ccid] += cm

        # Ranking por licença
        lic_cost_map[lic_id] += cm

    # Top custos por usuários (itens + licenças)
    user_ids = set(user_cost_items.keys()) | set(user_cost_lics.keys())
    usuarios_map = {u.id: u.nome for u in Usuario.objects.filter(id__in=user_ids)}
    agg_user = []
    for uid in user_ids:
        itens = user_cost_items.get(uid, Decimal("0.00"))
        lics  = user_cost_lics.get(uid, Decimal("0.00"))
        total = itens + lics
        if total > 0:
            agg_user.append((usuarios_map.get(uid, f"Usuário {uid}"), itens, lics, total))
    agg_user.sort(key=lambda x: x[3], reverse=True)
    TOPU = 10
    top_users_labels = [n for (n, *_ ) in agg_user[:TOPU]]
    top_users_items  = [float(i) for (_, i, _, _) in agg_user[:TOPU]]
    top_users_lics   = [float(l) for (_, _, l, _) in agg_user[:TOPU]]

    # Custo por setores (CC)
    cc_ids = set(cc_cost_items.keys()) | set(cc_cost_lics.keys())
    cc_map = {None: "—"}
    if cc_ids:
        for c in CentroCusto.objects.filter(id__in=cc_ids).values("id", "numero", "departamento"):
            cc_map[c["id"]] = f'{c["numero"]} - {c["departamento"]}'
    agg_cc = []
    for ccid in cc_ids:
        itens = cc_cost_items.get(ccid, Decimal("0.00"))
        lics  = cc_cost_lics.get(ccid, Decimal("0.00"))
        total = itens + lics
        if total > 0:
            agg_cc.append((cc_map.get(ccid, "—"), itens, lics, total))
    agg_cc.sort(key=lambda x: x[3], reverse=True)
    TOPCC = 8
    cc_cost_labels = [n for (n, *_ ) in agg_cc[:TOPCC]]
    cc_cost_items_s = [float(i) for (_, i, _, _) in agg_cc[:TOPCC]]
    cc_cost_lics_s  = [float(l) for (_, _, l, _) in agg_cc[:TOPCC]]

    # Top licenças por custo (mês) — já somado com o custo do lote da atribuição
    lic_cost_pairs = []
    if lic_cost_map:
        # Busca nomes em um único hit
        lic_ids = list(lic_cost_map.keys())
        lic_name_map = {l.id: l.nome for l in Licenca.objects.filter(id__in=lic_ids).only("id", "nome")}
        for lid, total_mensal in lic_cost_map.items():
            if total_mensal > 0:
                lic_cost_pairs.append((lic_name_map.get(lid, f"Licença {lid}"), total_mensal))
        lic_cost_pairs.sort(key=lambda x: x[1], reverse=True)
    TOPLIC = 10
    lic_cost_labels = [n for (n, _) in lic_cost_pairs[:TOPLIC]]
    lic_cost_data   = [float(v) for (_, v) in lic_cost_pairs[:TOPLIC]]

    # =============================
    # Contexto para o template
    # =============================
    ctx = dict(
        # KPIs
        total_geral=total_geral,
        total_ativos=total_ativos,
        total_backup=total_backup,
        total_manutencao=total_manutencao,
        total_defeito=total_defeito,

        # Movimentações
        mov_labels=labels12,
        mov_entrada=mov_entrada,
        mov_baixa=mov_baixa,
        mov_transferencia=mov_transferencia,
        mov_envio=mov_envio,
        mov_retorno=mov_retorno,

        # Preventivas
        prev_ok=prev_ok,
        prev_vencida=prev_vencida,
        prev_total=prev_total,
        proximas=proximas,

        # Subtipo / Localidade / CC (quantidades)
        chart_subtipo_labels=chart_subtipo_labels,
        chart_subtipo_data=chart_subtipo_data,
        chart_loc_labels=chart_loc_labels,
        chart_loc_data=chart_loc_data,
        chart_cc_labels=chart_cc_labels,
        chart_cc_data=chart_cc_data,

        # Top itens mais movimentados
        top_mov_labels=top_mov_labels,
        top_mov_data=top_mov_data,

        # Custo manutenção (6m)
        custo_labels=labels6,
        custo_data=custo_data,

        # NOVOS gráficos de custo (com lote)
        top_users_labels=top_users_labels,
        top_users_items=top_users_items,
        top_users_lics=top_users_lics,

        cc_cost_labels=cc_cost_labels,
        cc_cost_items=cc_cost_items_s,
        cc_cost_lics=cc_cost_lics_s,

        lic_cost_labels=lic_cost_labels,
        lic_cost_data=lic_cost_data,
    )

    return render(request, "front/dashboards/dashboard.html", ctx)


# ==== helpers que você já usa em outros dashboards ====
def _month_key(dt):
    return f"{dt.year:04d}-{dt.month:02d}"

def _last_n_month_stamps(n=12):
    now = timezone.localtime()
    y, m = now.year, now.month
    out = []
    for _ in range(n):
        out.append((y, m))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return list(reversed(out))

def _labels_pt_br(stamps):
    nomes = ["Jan","Fev","Mar","Abr","Mai","Jun","Jul","Ago","Set","Out","Nov","Dez"]
    return [f"{nomes[m-1]}/{str(y)[-2:]}" for (y, m) in stamps]

def _align_series(stamps, qs_month_count, field_name="c"):
    """Alinha uma série mensal (values('m').annotate(c=...)) em relação aos stamps fornecidos."""
    m2v = {}
    for row in qs_month_count:
        mdt = row["m"]
        if not isinstance(mdt, datetime):
            continue
        m2v[_month_key(timezone.localtime(mdt))] = int(row[field_name] or 0)
    out = []
    for (y, m) in stamps:
        out.append(int(m2v.get(f"{y:04d}-{m:02d}", 0)))
    return out


@login_required
def preventiva_dashboard(request):
    """
    Dashboard de Preventivas:
      - KPIs: totais, no prazo, vencidas, sem agenda, executadas no mês
      - Séries (12 meses): executadas x programadas
      - Tabelas: por checklist, localidade e subtipo
      - Listas: vencidas e próximas 30 dias (+ histórico)
    Filtros: q (item/obs), status (ok|vencida|sem_agenda|""), checklist (id),
             local (icontains), subtipo (icontains), inicio/fim (opcional p/ séries)
    """
    today = timezone.localdate()
    now = timezone.localtime()

    # -------- filtros básicos --------
    q       = (request.GET.get("q") or "").strip()
    status  = (request.GET.get("status") or "").strip()       # ok | vencida | sem_agenda | ""
    chk_id  = (request.GET.get("checklist") or "").strip()
    loc     = (request.GET.get("local") or "").strip()
    subtipo = (request.GET.get("subtipo") or "").strip()

    base = (Preventiva.objects
            .select_related(
                "equipamento",
                "equipamento__localidade",
                "equipamento__subtipo",
                "checklist_modelo",
            ))

    if q:
        base = base.filter(Q(equipamento__nome__icontains=q) | Q(observacao__icontains=q))
    if chk_id.isdigit():
        base = base.filter(checklist_modelo_id=int(chk_id))
    if loc:
        base = base.filter(equipamento__localidade__local__icontains=loc)
    if subtipo:
        base = base.filter(equipamento__subtipo__nome__icontains=subtipo)

    # Guardamos uma cópia SEM o filtro de status para KPIs/listas.
    # (Se preferir que o status também afete KPIs/listas, troque 'base_kpi' por 'base' nos cálculos.)
    base_kpi = base

    # Filtro de status (apenas para a visualização geral; KPIs usam base_kpi para não “zerar”)
    if status == "ok":
        base = base.filter(data_proxima__isnull=False, data_proxima__gte=today)
    elif status == "vencida":
        base = base.filter(data_proxima__lt=today)
    elif status == "sem_agenda":
        base = base.filter(data_proxima__isnull=True)

    # -------- KPIs (usando base_kpi para refletir a situação real do parque) --------
    total             = base_kpi.count()
    vencidas_count    = base_kpi.filter(data_proxima__lt=today).count()
    sem_agenda_count  = base_kpi.filter(data_proxima__isnull=True).count()
    ok_count          = base_kpi.filter(data_proxima__isnull=False, data_proxima__gte=today).count()
    executadas_mes    = base_kpi.filter(data_ultima__year=now.year, data_ultima__month=now.month).count()

    # -------- Séries 12 meses: executadas x programadas (também a partir de base_kpi) --------
    stamps12 = _last_n_month_stamps(12)
    labels12 = _labels_pt_br(stamps12)
    start12  = timezone.make_aware(datetime(stamps12[0][0], stamps12[0][1], 1))

    exec_qs = (base_kpi.filter(data_ultima__isnull=False, data_ultima__gte=start12)
                        .annotate(m=TruncMonth("data_ultima"))
                        .values("m")
                        .annotate(c=Count("id"))
                        .order_by("m"))
    prog_qs = (base_kpi.filter(data_proxima__isnull=False, data_proxima__gte=start12)
                        .annotate(m=TruncMonth("data_proxima"))
                        .values("m")
                        .annotate(c=Count("id"))
                        .order_by("m"))

    serie_exec = _align_series(stamps12, exec_qs)
    serie_prog = _align_series(stamps12, prog_qs)

    # -------- AGG por Checklist / Localidade / Subtipo (usando base_kpi) --------
    agg_chk = (base_kpi.values("checklist_modelo_id", "checklist_modelo__nome")
                      .annotate(
                          total=Count("id"),
                          vencidas=Count("id", filter=Q(data_proxima__lt=today)),
                          ok=Count("id", filter=Q(data_proxima__isnull=False, data_proxima__gte=today)),
                          sem_agenda=Count("id", filter=Q(data_proxima__isnull=True)),
                          prox_30=Count("id", filter=Q(data_proxima__gte=today,
                                                       data_proxima__lte=today + timedelta(days=30))),
                      )
                      .order_by("-total", "checklist_modelo__nome"))

    chk_labels, chk_rates = [], []
    for r in agg_chk[:8]:
        den = (r["ok"] or 0) + (r["vencidas"] or 0)
        taxa = (100.0 * (r["ok"] or 0) / den) if den > 0 else 0.0
        chk_labels.append(r["checklist_modelo__nome"] or "Sem checklist")
        chk_rates.append(round(taxa, 2))

    agg_loc = (base_kpi.values("equipamento__localidade__local")
                      .annotate(
                          total=Count("id"),
                          vencidas=Count("id", filter=Q(data_proxima__lt=today)),
                          ok=Count("id", filter=Q(data_proxima__isnull=False, data_proxima__gte=today)),
                          sem_agenda=Count("id", filter=Q(data_proxima__isnull=True)),
                      )
                      .order_by("-vencidas", "equipamento__localidade__local"))

    agg_sub = (base_kpi.values("equipamento__subtipo__nome")
                      .annotate(
                          total=Count("id"),
                          vencidas=Count("id", filter=Q(data_proxima__lt=today)),
                          ok=Count("id", filter=Q(data_proxima__isnull=False, data_proxima__gte=today)),
                          sem_agenda=Count("id", filter=Q(data_proxima__isnull=True)),
                      )
                      .order_by("-vencidas", "equipamento__subtipo__nome"))

    # -------- Listas operacionais (calculadas corretamente) --------
    vencidas = list(
        base_kpi.filter(data_proxima__lt=today)
                .order_by("data_proxima")
                .select_related("equipamento", "equipamento__localidade", "equipamento__subtipo", "checklist_modelo")[:50]
    )
    for p in vencidas:
        p.dias_atraso = (today - p.data_proxima).days if p.data_proxima else None

    proximas = list(
        base_kpi.filter(data_proxima__isnull=False,
                        data_proxima__gte=today,
                        data_proxima__lte=today + timedelta(days=30))
                .order_by("data_proxima")
                .select_related("equipamento", "equipamento__localidade", "equipamento__subtipo", "checklist_modelo")
    )
    for p in proximas:
        p.dias_faltam = (p.data_proxima - today).days if p.data_proxima else None

    historico = (base_kpi.filter(data_ultima__isnull=False)
                        .order_by("-data_ultima", "-updated_at")
                        .select_related("equipamento", "checklist_modelo")[:20])

    checklist_opts = CheckListModelo.objects.all().order_by("nome").values("id", "nome")

    ctx = dict(
        # filtros
        q=q, status=status, checklist=chk_id, local=loc, subtipo=subtipo,
        checklist_opts=checklist_opts,

        # KPIs
        total=total,
        ok_count=ok_count,
        vencidas_count=vencidas_count,
        sem_agenda_count=sem_agenda_count,
        executadas_mes=executadas_mes,

        # Séries
        serie_labels=labels12,
        serie_exec=serie_exec,
        serie_prog=serie_prog,

        # Tabelas
        agg_chk=list(agg_chk),
        agg_loc=list(agg_loc),
        agg_sub=list(agg_sub),

        # Listas
        vencidas=vencidas,
        proximas=proximas,
        historico=historico,

        # Gráfico de adesão por checklist
        chk_labels=chk_labels,
        chk_rates=chk_rates,

        today=today,
    )
    return render(request, "front/dashboards/preventiva_dashboard.html", ctx)

# ---- helpers de data ----
def _parse_date(s, default):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return default

def _parse_date(s, fb):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date() if s else fb
    except Exception:
        return fb

def _parse_date(value, default):
    """
    Converte 'YYYY-MM-DD' -> date. Se vazio ou inválido, retorna default.
    """
    if not value:
        return default
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except Exception:
        return default


@login_required
def cc_custos_dashboard(request):
    """
    Dashboard de custos por Centro de Custo:
    - Itens locados (valor mensal)
    - Licenças (assentos ativos e custo mensal)
    - Baixas no período (R$)
    - Gráficos: barras empilhadas (Itens x Licenças), pizza (proporção)
    """
    # ---- Período para BAIXAS ----
    hoje = datetime.today().date()
    dt_ini = _parse_date(request.GET.get("inicio"), hoje - timedelta(days=30))
    dt_fim = _parse_date(request.GET.get("fim"), hoje)

    # Dicionário acumulador por CC
    totals = {}  # cc_id -> dict

    def acc(cc_id):
        if not cc_id:
            return None
        if cc_id not in totals:
            totals[cc_id] = {
                "cc": None,
                "usuarios": 0,
                "itens": 0,
                "licencas_set": set(),
                "assentos": 0,
                "custo_itens": Decimal("0.00"),
                "custo_licencas": Decimal("0.00"),
                "baixas": Decimal("0.00"),
            }
        return totals[cc_id]

    # ---- Custos mensais de ITENS locados por CC ----
    loc_qs = (
        Locacao.objects
        .select_related("equipamento", "equipamento__centro_custo")
        .exclude(valor_mensal__isnull=True)
    )
    for loc in loc_qs:
        item = loc.equipamento
        cc_id = getattr(item.centro_custo, "id", None)
        if not cc_id:
            continue
        valor = loc.valor_mensal or Decimal("0.00")
        if valor <= 0:
            continue
        a = acc(cc_id)
        a["custo_itens"] += valor

    # ---- Assentos de LICENÇAS (último evento por par licença/usuário) ----
    mov_l_qs = (
        MovimentacaoLicenca.objects
        .select_related("licenca", "usuario__centro_custo", "centro_custo_destino", "lote")
        .order_by("licenca_id", "usuario_id", "created_at", "id")
    )
    # Guarda o ÚLTIMO por (licenca, usuario)
    last_by_pair = {}  # (licenca_id, usuario_id) -> MovimentacaoLicenca
    for m in mov_l_qs:
        if m.usuario_id is None:
            continue
        last_by_pair[(m.licenca_id, m.usuario_id)] = m

    for (lic_id, user_id), m in last_by_pair.items():
        if m.tipo != TipoMovLicencaChoices.ATRIBUICAO:
            continue

        # Resolve CC (prioridade: usuário > mov > licença)
        cc_id = (
            getattr(getattr(m.usuario, "centro_custo", None), "id", None)
            or getattr(m.centro_custo_destino, "id", None)
            or getattr(m.licenca.centro_custo, "id", None)
        )
        if not cc_id:
            continue

        # Custo mensal POR ASSENTO usando o lote da ATRIBUIÇÃO quando houver
        cm = m.custo_mensal_usado or Decimal("0.00")

        a = acc(cc_id)
        a["assentos"] += 1
        a["custo_licencas"] += cm
        a["licencas_set"].add(lic_id)

    # ---- BAIXAS no período (R$) por CC ----
    baixas_qs = (
        MovimentacaoItem.objects
        .filter(
            tipo_movimentacao=TipoMovimentacaoChoices.BAIXA,
            created_at__date__gte=dt_ini, created_at__date__lte=dt_fim
        )
        .select_related("item__centro_custo", "centro_custo_origem")
    )
    for mv in baixas_qs:
        cc_id = (
            getattr(mv.centro_custo_origem, "id", None)
            or getattr(getattr(mv.item, "centro_custo", None), "id", None)
        )
        if not cc_id:
            continue
        valor_baixa = (
            mv.custo
            if mv.custo is not None
            else (mv.item.valor or Decimal("0.00")) * (mv.quantidade or 1)
        )
        a = acc(cc_id)
        a["baixas"] += (valor_baixa or Decimal("0.00"))

    # ---- Completa metadados por CC ----
    cc_ids = list(totals.keys())
    ccs = {cc.id: cc for cc in CentroCusto.objects.filter(id__in=cc_ids)}

    users_count = (
        Usuario.objects
        .filter(centro_custo_id__in=cc_ids, status="ativo")
        .values("centro_custo_id")
        .annotate(n=Count("id"))
    )
    itens_count = (
        Item.objects
        .filter(centro_custo_id__in=cc_ids)
        .values("centro_custo_id")
        .annotate(n=Count("id"))
    )
    map_users = {r["centro_custo_id"]: r["n"] for r in users_count}
    map_itens = {r["centro_custo_id"]: r["n"] for r in itens_count}

    linhas = []
    for cc_id, d in totals.items():
        cc = ccs.get(cc_id)
        if not cc:
            continue
        d["cc"] = cc
        d["usuarios"] = map_users.get(cc_id, 0)
        d["itens"] = map_itens.get(cc_id, 0)
        d["licencas"] = len(d["licencas_set"])
        d["total_mensal"] = (d["custo_itens"] + d["custo_licencas"])
        d["total_geral"] = (d["total_mensal"] + d["baixas"])

        linhas.append({
            "cc": cc,
            "usuarios": d["usuarios"],
            "itens": d["itens"],
            "licencas": d["licencas"],     # tipos distintos
            "assentos": d["assentos"],     # assentos ativos
            "custo_itens": d["custo_itens"],
            "custo_licencas": d["custo_licencas"],
            "baixas": d["baixas"],
            "total_mensal": d["total_mensal"],
            "total_geral": d["total_geral"],
        })

    # Ordena por total_geral desc
    linhas.sort(key=lambda x: x["total_geral"], reverse=True)

    # Vetores para os gráficos
    labels = [f"{l['cc'].numero} - {l['cc'].departamento}" for l in linhas]
    arr_itens   = [float(l["custo_itens"]) for l in linhas]
    arr_lics    = [float(l["custo_licencas"]) for l in linhas]
    arr_mensal  = [float(l["total_mensal"]) for l in linhas]
    arr_baixas  = [float(l["baixas"]) for l in linhas]

    # Totais (para pizza/proporção)
    total_itens_mensal = float(sum(l["custo_itens"] for l in linhas)) if linhas else 0.0
    total_lics_mensal  = float(sum(l["custo_licencas"] for l in linhas)) if linhas else 0.0

    context = {
        "dt_ini": dt_ini,
        "dt_fim": dt_fim,
        "linhas": linhas,
        # dados dos gráficos
        "cc_labels": labels,
        "cc_itens": arr_itens,
        "cc_licencas": arr_lics,
        "cc_mensal": arr_mensal,
        "cc_baixas": arr_baixas,
        "total_itens_mensal": total_itens_mensal,
        "total_lics_mensal": total_lics_mensal,
    }
    return render(request, "front/dashboards/cc_custos_dashboard.html", context)

    
# ===== LISTA DE LICENÇAS (com cartões) =====
# ============ LICENÇAS ============

@login_required
def licenca_list(request):
    # -------- Filtros (mantidos) --------
    q          = (request.GET.get("q") or "").strip()
    fornecedor = (request.GET.get("fornecedor") or "").strip()
    centro     = (request.GET.get("centro") or "").strip()
    pmb        = (request.GET.get("pmb") or "").strip()
    per        = (request.GET.get("periodicidade") or "").strip()

    qs = (
        Licenca.objects
        .select_related("fornecedor", "centro_custo")
        .annotate(
            total_lotes=Count("lotes", distinct=True),
            disp_lotes=Coalesce(Sum("lotes__quantidade_disponivel"), 0),
        )
        .order_by("nome")
    )

    if q:
        qs = qs.filter(Q(nome__icontains=q) | Q(observacao__icontains=q))
    if fornecedor:
        qs = qs.filter(fornecedor__nome__icontains=fornecedor)
    if centro:
        qs = qs.filter(
            Q(centro_custo__numero__icontains=centro) |
            Q(centro_custo__departamento__icontains=centro)
        )
    if pmb:
        qs = qs.filter(pmb=pmb)
    if per:
        qs = qs.filter(periodicidade=per)

    # total filtrado (mantém compatibilidade com seu template que usa "total")
    total = qs.count()

    # -------- Paginação --------
    try:
        per_page = int(request.GET.get("pp") or 20)
    except ValueError:
        per_page = 20
    per_page = max(1, min(per_page, 100))  # segurança

    paginator = Paginator(qs, per_page)
    page_num = request.GET.get("page") or 1
    page_obj = paginator.get_page(page_num)

    # Mantém os parâmetros (exceto 'page') para os links da paginação
    keep = request.GET.copy()
    keep.pop("page", None)
    qs_keep = keep.urlencode()

    ctx = {
        "licencas": page_obj.object_list,  # o iterable da página
        "page_obj": page_obj,
        "paginator": paginator,
        "per_page": per_page,
        "qs_keep": qs_keep,

        # filtros (eco no form)
        "q": q, "fornecedor": fornecedor, "centro": centro, "pmb": pmb, "per": per,

        # métrica mostrada no topo
        "total": total,
    }

    # choices diretamente do Model (mantendo compatibilidade com seu template)
    ctx["pmb_choices"] = Licenca._meta.get_field("pmb").choices
    ctx["periodicidade_choices"] = Licenca._meta.get_field("periodicidade").choices

    return render(request, "front/licencas/licenca_list.html", ctx)

@login_required
def licenca_form(request, pk=None):
    obj = get_object_or_404(Licenca, pk=pk) if pk else None
    if request.method == "POST":
        form = LicencaForm(request.POST, instance=obj)
        if form.is_valid():
            lic = form.save(commit=False)
            if obj is None:
                lic.criado_por = request.user
            lic.atualizado_por = request.user
            lic.save()
            messages.success(request, "Licença salva com sucesso.")
            return redirect("licenca_detail", pk=lic.pk)
        messages.error(request, "Corrija os campos destacados.")
    else:
        form = LicencaForm(instance=obj)

    return render(request, "front/licencas/licenca_form.html", {"form": form, "obj": obj})

@login_required
def licenca_detail(request, pk):
    lic = get_object_or_404(
        Licenca.objects.select_related("fornecedor", "centro_custo"),
        pk=pk
    )

    # últimas movimentações (já existia)
    movs = (
        MovimentacaoLicenca.objects
        .select_related("usuario", "centro_custo_destino")
        .filter(licenca=lic)
        .order_by("-created_at")[:20]
    )

    # ===== Lotes desta licença (robusto a nomes de campos) =====
    try:
        # Se o modelo existir no projeto
        from .models import LicencaLote  # não explode se não houver
        lotes_qs = LicencaLote.objects.filter(licenca=lic).order_by("-created_at")
    except Exception:
        lotes_qs = []

    def g(obj, names, default=None):
        """Pega o primeiro atributo existente em 'names'."""
        for n in names:
            if hasattr(obj, n):
                v = getattr(obj, n)
                try:
                    return v() if callable(v) else v
                except Exception:
                    continue
        return default

    lotes = []
    tot_total = 0
    tot_disp  = 0
    for lt in lotes_qs:
        nome = g(lt, ["nome", "descricao", "titulo", "label"], f"Lote #{getattr(lt, 'id', '')}")
        total = g(lt, ["qtd_total", "total", "quantidade_total", "qtd", "qte_total"], 0) or 0
        disp  = g(lt, ["qtd_disponivel", "disponivel", "saldo", "qtd_disp", "estoque", "disponibilidade"], 0) or 0
        usados = max(0, (total or 0) - (disp or 0))

        custo_unit   = g(lt, ["custo_unit", "valor_unitario", "preco_unitario"], None)
        custo_ciclo  = g(lt, ["custo_ciclo", "custo", "valor", "preco"], None)
        periodicidade = g(lt, ["periodicidade", "periodo"], None)
        pedido = g(lt, ["numero_pedido", "pedido", "nota", "contrato"], None)
        obs    = g(lt, ["observacao", "obs", "descricao_lote"], "")

        lotes.append({
            "pk": getattr(lt, "id", None),
            "nome": nome,
            "total": total,
            "disp": disp,
            "usados": usados,
            "custo_unit": custo_unit,
            "custo_ciclo": custo_ciclo,
            "periodicidade": periodicidade,
            "pedido": pedido,
            "obs": obs,
            "created_at": g(lt, ["created_at", "data", "data_compra", "created"], None),
        })
        tot_total += total
        tot_disp  += disp

    tot_usados = max(0, tot_total - tot_disp)
    qtd = Decimal(lic.quantidade or 0)
    custo_ciclo = lic.custo                          # já passava
    custo_mensal_unit = lic.custo_mensal() or Decimal("0.00")
    custo_anual_unit  = lic.custo_anual_estimado() or (custo_mensal_unit * Decimal(12))

    custo_mensal_total = (custo_mensal_unit * qtd).quantize(Decimal("0.01"))
    custo_anual_total  = (custo_anual_unit  * qtd).quantize(Decimal("0.01"))

    ctx = {
        "obj": lic,
        "movs": movs,
        "custo_ciclo": custo_ciclo,
        "custo_mensal": custo_mensal_unit,
        "custo_anual": custo_anual_unit,

        # >>> novos campos exibidos no layout <<<
        "qtd_disponivel": int(lic.quantidade or 0),
        "custo_mensal_total": custo_mensal_total,
        "custo_anual_total": custo_anual_total,

        "lotes": lotes,
        "lotes_totais": {"total": tot_total, "disp": tot_disp, "usados": tot_usados},
    }
    return render(request, "front/licencas/licenca_detail.html", ctx)
    ctx = {
        "obj": lic,
        "movs": movs,
        "custo_ciclo": lic.custo,                   # valor cadastrado na licença
        "custo_mensal": lic.custo_mensal(),         # normalizado
        "custo_anual": lic.custo_anual_estimado(),  # estimado

        # >>> novos dados de lotes <<<
        "lotes": lotes,
        "lotes_totais": {"total": tot_total, "disp": tot_disp, "usados": tot_usados},
    }
    return render(request, "front/licencas/licenca_detail.html", ctx)


# ============ MOVIMENTAÇÕES ============

@login_required
def mov_licenca_list(request):
    q = (request.GET.get("q") or "").strip()
    tipo = (request.GET.get("tipo") or "").strip()

    qs = (MovimentacaoLicenca.objects
          .select_related("licenca", "usuario", "centro_custo_destino")
          .order_by("-created_at"))
    if q:
        qs = qs.filter(Q(licenca__nome__icontains=q) | Q(usuario__nome__icontains=q))
    if tipo in (TipoMovLicencaChoices.ATRIBUICAO, TipoMovLicencaChoices.REMOCAO):
        qs = qs.filter(tipo=tipo)

    return render(request, "front/licencas/mov_licenca_list.html", {
        "movs": qs,
        "q": q,
        "tipo": tipo,
        "tipos": MovimentacaoLicenca._meta.get_field("tipo").choices,
    })

@login_required
def mov_licenca_form(request):
    initial = {}
    if request.method == "GET":
        if "licenca" in request.GET:
            initial["licenca"] = request.GET.get("licenca")
        if "usuario" in request.GET:
            initial["usuario"] = request.GET.get("usuario")

    if request.method == "POST":
        form = MovimentacaoLicencaForm(request.POST)
        if form.is_valid():
            mov = form.save(user=request.user)  # o form lê request.POST.get("lote")
            messages.success(request, f"Movimentação registrada: {mov.get_tipo_display()} - {mov.licenca.nome}.")
            next_url = request.GET.get("next")
            if next_url:
                return redirect(next_url)
            if mov.usuario_id:
                return redirect("usuario_detail", pk=mov.usuario_id)
            return redirect("licenca_detail", pk=mov.licenca_id)
    else:
        form = MovimentacaoLicencaForm(initial=initial)

    # ===== contexto dos lotes (mantendo sua lógica) =====
    lic_id = form.data.get("licenca") or form.initial.get("licenca")
    lotes = []
    show_lote = False
    lic_id = form.data.get("licenca") or form.initial.get("licenca")
    if lic_id:
        from .models import LicencaLote
        lotes = (
            LicencaLote.objects
            .filter(licenca_id=lic_id)  # ou .filter(licenca_id=lic_id, quantidade_disponivel__gt=0)
            .order_by("-created_at")
        )
        show_lote = lotes.exists()

    selected_lote = request.POST.get("lote") or request.GET.get("lote") or ""

    ctx = {
        "form": form,
        "lotes": lotes,
        "show_lote": show_lote,
        "selected_lote": selected_lote,  # <- usado no template
    }
    return render(request, "front/licencas/mov_licenca_form.html", ctx)


# --- LISTA DE LOTES ---
@login_required
def licenca_lote_list(request):
    q = request.GET.get("q", "").strip()
    qs = (
        LicencaLote.objects
        .select_related("licenca", "fornecedor", "centro_custo")
        .order_by("-created_at")
    )
    if q:
        qs = qs.filter(licenca__nome__icontains=q)
    return render(request, "front/licencas/licenca_lote_list.html", {"lotes": qs, "q": q})

@login_required
@transaction.atomic
def licenca_lote_form(request, pk=None):
    obj = get_object_or_404(LicencaLote.objects.select_related("licenca"), pk=pk) if pk else None

    if request.method == "POST":
        form = LicencaLoteForm(request.POST, instance=obj)
        if form.is_valid():
            is_new = form.instance.pk is None

            # valores antigos (se edição)
            old_total = _lote_total_get(obj) if obj else 0
            old_disp  = _lote_disp_get(obj)  if obj else 0

            lote = form.save(commit=False)

            if is_new:
                # novo lote entra totalmente disponível
                _lote_disp_set(lote, _lote_total_get(lote))
            else:
                # não pode reduzir abaixo do que já foi usado
                usados = max(0, old_total - old_disp)
                new_total = _lote_total_get(lote)
                if new_total < usados:
                    fld = _lote_total_fieldname(lote) or "qtd_total"
                    form.add_error(fld, "Quantidade menor que a já atribuída neste lote.")
                    return render(request, "front/licencas/licenca_lote_form.html", {"form": form})
                new_disp = max(0, new_total - usados)
                _lote_disp_set(lote, new_disp)

            lote.save()

            # Atualiza a disponibilidade global da Licença com lock
            lic = Licenca.objects.select_for_update().get(pk=lote.licenca_id)
            if is_new:
                lic.quantidade = (lic.quantidade or 0) + _lote_disp_get(lote)
            else:
                lic.quantidade = (lic.quantidade or 0) + (_lote_disp_get(lote) - old_disp)
            lic.save(update_fields=["quantidade", "updated_at"])

            messages.success(request, "Lote salvo com sucesso.")
            return redirect("licenca_lote_list")
    else:
        form = LicencaLoteForm(instance=obj)

    return render(request, "front/licencas/licenca_lote_form.html", {"form": form})




### exportações 

def _aplicar_filtros_equipamentos(request, base_qs=None):
    """
    Aplica os mesmos filtros da tela:
      nome, subtipo, status, numero_serie, fornecedor, localidade, centro_custo
    """
    qs = base_qs or Item.objects.all()
    qs = qs.select_related("subtipo", "localidade", "fornecedor", "centro_custo")

    nome = (request.GET.get("nome") or "").strip()
    if nome:
        qs = qs.filter(nome__icontains=nome)

    subtipo = (request.GET.get("subtipo") or "").strip()
    if subtipo:
        qs = qs.filter(subtipo_id=subtipo)

    status = (request.GET.get("status") or "").strip()
    if status:
        qs = qs.filter(status=status)

    numero_serie = (request.GET.get("numero_serie") or "").strip()
    if numero_serie:
        qs = qs.filter(numero_serie__icontains=numero_serie)

    fornecedor = (request.GET.get("fornecedor") or "").strip()
    if fornecedor:
        qs = qs.filter(fornecedor__nome__icontains=fornecedor)

    localidade = (request.GET.get("localidade") or "").strip()
    if localidade:
        qs = qs.filter(localidade__local__icontains=localidade)

    centro = (request.GET.get("centro_custo") or "").strip()
    if centro:
        qs = qs.filter(
            Q(centro_custo__numero__icontains=centro) |
            Q(centro_custo__departamento__icontains=centro)
        )

    return qs.order_by("nome", "id")


@login_required
def toner_cc_export_excel(request):
    """
    Exporta para Excel o custo de TONER por Centro de Custo, no período filtrado.
    Regra: considerar MovimentacaoItem do tipo 'baixa' cujo item.subtipo contém 'toner',
    agrupando por centro_custo_destino.
    Custo = quantidade * item.valor (se None, trata como 0).
    """
    # --- período (fallback: mês atual até hoje) ---
    hoje = timezone.localdate()
    dt_ini = parse_date(request.GET.get("inicio") or "") or hoje.replace(day=1)
    dt_fim = parse_date(request.GET.get("fim") or "") or hoje

    # --- queryset base: BAIXAS de TONER com CC destino válido ---
    qs = (
        MovimentacaoItem.objects
        .filter(
            tipo_movimentacao=TipoMovimentacaoChoices.BAIXA,
            item__subtipo__nome__icontains="toner",
            centro_custo_destino__isnull=False,
            created_at__date__gte=dt_ini,
            created_at__date__lte=dt_fim,
        )
        .select_related("item", "centro_custo_destino", "localidade_destino")
    )

    # --- anotações seguras (evita mixed types) ---
    qty_dec = Cast(F("quantidade"), DecimalField(max_digits=12, decimal_places=2))
    item_val = Coalesce(
        Cast(F("item__valor"), DecimalField(max_digits=12, decimal_places=2)),
        V(Decimal("0.00")),
        output_field=DecimalField(max_digits=12, decimal_places=2),
    )
    linha_total = qty_dec * item_val  # Decimal * Decimal
    # Para agrupar por Centro de Custo:
    grp = (
        qs.values(
            "centro_custo_destino",
            "centro_custo_destino__numero",
            "centro_custo_destino__departamento",
        )
        .annotate(
            total_qtd=Coalesce(Sum("quantidade"), V(0)),
            total_valor=Coalesce(
                Sum(Cast(linha_total, DecimalField(max_digits=18, decimal_places=2))),
                V(Decimal("0.00")),
                output_field=DecimalField(max_digits=18, decimal_places=2),
            ),
            itens_distintos=Count("item", distinct=True),
            movs=Count("id"),
        )
        .order_by("centro_custo_destino__numero", "centro_custo_destino__departamento")
    )

    # --- planilha ---
    wb = Workbook()
    ws1 = wb.active
    ws1.title = "Resumo por CC"

    # estilos
    header_fill = PatternFill(start_color="E8F0FE", end_color="E8F0FE", fill_type="solid")
    header_font = Font(bold=True, color="001e3a")
    thin = Side(style="thin", color="DDDDDD")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    # cabeçalho
    ws1.append(["Período", f"{dt_ini.strftime('%d/%m/%Y')} a {dt_fim.strftime('%d/%m/%Y')}"])
    ws1.merge_cells(start_row=1, start_column=2, end_row=1, end_column=6)

    ws1.append(["Centro de Custo", "Departamento", "Movimentações", "Itens Distintos",
                "Quantidade Baixada", "Valor Total (R$)"])
    for c in range(1, 7):
        cell = ws1.cell(row=2, column=c)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")
        cell.border = border

    # linhas
    total_qtd_geral = 0
    total_valor_geral = Decimal("0.00")

    r = 3
    for row in grp:
        cc_num = row["centro_custo_destino__numero"] or "-"
        cc_dep = row["centro_custo_destino__departamento"] or "-"
        movs = row["movs"] or 0
        itens_d = row["itens_distintos"] or 0
        qtd = int(row["total_qtd"] or 0)
        val = Decimal(row["total_valor"] or 0).quantize(Decimal("0.01"))

        ws1.cell(row=r, column=1, value=str(cc_num))
        ws1.cell(row=r, column=2, value=str(cc_dep))
        ws1.cell(row=r, column=3, value=movs)
        ws1.cell(row=r, column=4, value=itens_d)
        ws1.cell(row=r, column=5, value=qtd)
        c6 = ws1.cell(row=r, column=6, value=float(val))
        c6.number_format = 'R$ #,##0.00'

        for c in range(1, 7):
            ws1.cell(row=r, column=c).border = border

        total_qtd_geral += qtd
        total_valor_geral += val
        r += 1

    # totalizador
    ws1.append(["", "", "", "Totais:", total_qtd_geral, float(total_valor_geral)])
    ws1.cell(row=r, column=4).font = Font(bold=True)
    ws1.cell(row=r, column=5).font = Font(bold=True)
    ws1.cell(row=r, column=6).font = Font(bold=True)
    ws1.cell(row=r, column=6).number_format = 'R$ #,##0.00'
    for c in range(1, 7):
        ws1.cell(row=r, column=c).border = border

    # larguras
    widths = [20, 36, 18, 18, 22, 22]
    for i, w in enumerate(widths, start=1):
        ws1.column_dimensions[get_column_letter(i)].width = w

    # --- aba de detalhes (opcional, mas útil) ---
    ws2 = wb.create_sheet("Detalhes")
    ws2.append(["Data", "Centro de Custo", "Departamento", "Item", "Subtipo",
                "Quantidade", "Valor Unitário (R$)", "Valor Total (R$)"])

    for c in range(1, 9):
        cell = ws2.cell(row=1, column=c)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")
        cell.border = border

    r = 2
    for m in qs.order_by("centro_custo_destino__numero", "created_at", "id"):
        valor_unit = Decimal(m.item.valor or 0).quantize(Decimal("0.01"))
        qtd = Decimal(m.quantidade or 0)
        val_total = (valor_unit * qtd).quantize(Decimal("0.01"))

        ws2.cell(row=r, column=1, value=m.created_at.strftime("%d/%m/%Y %H:%M"))
        ws2.cell(row=r, column=2, value=getattr(m.centro_custo_destino, "numero", "") or "-")
        ws2.cell(row=r, column=3, value=getattr(m.centro_custo_destino, "departamento", "") or "-")
        ws2.cell(row=r, column=4, value=m.item.nome if m.item_id else "-")
        ws2.cell(row=r, column=5, value=getattr(m.item.subtipo, "nome", "") if m.item_id and m.item.subtipo_id else "-")
        ws2.cell(row=r, column=6, value=int(m.quantidade or 0))
        c7 = ws2.cell(row=r, column=7, value=float(valor_unit))
        c7.number_format = 'R$ #,##0.00'
        c8 = ws2.cell(row=r, column=8, value=float(val_total))
        c8.number_format = 'R$ #,##0.00'

        for c in range(1, 9):
            ws2.cell(row=r, column=c).border = border
        r += 1

    for i, w in enumerate([18, 18, 28, 36, 20, 14, 22, 22], start=1):
        ws2.column_dimensions[get_column_letter(i)].width = w

    # --- resposta HTTP (arquivo xlsx) ---
    filename = f"custo_toner_por_cc_{dt_ini.strftime('%Y%m%d')}_{dt_fim.strftime('%Y%m%d')}.xlsx"
    resp = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    wb.save(resp)
    return resp


@login_required
def equipamentos_exportar(request):
    # 1) Query filtrada igual à lista
    qs = _aplicar_filtros_equipamentos(request)

    # 2) Mapa de locação mensal por equipamento (sua FK é 'equipamento')
    ids = list(qs.values_list("id", flat=True))
    loc_map = dict(
        Locacao.objects
        .filter(equipamento_id__in=ids)
        .values_list("equipamento_id", "valor_mensal")
    )

    # 3) Monta planilha
    wb = Workbook()
    ws = wb.active
    ws.title = "Itens"

    header = [
        "#", "Nome", "Subtipo", "Status", "Localidade",
        "Nº Série", "Fornecedor", "Centro de Custo",
        "Locado", "Locação (R$/mês)", "Valor aquisição (R$)"
    ]
    ws.append(header)

    # Estilo do header
    hfill = PatternFill("solid", fgColor="1D4ED8")
    hfont = Font(color="FFFFFF", bold=True)
    align_center = Alignment(horizontal="center", vertical="center")
    thin = Side(style="thin", color="DDE3EE")
    hborder = Border(left=thin, right=thin, top=thin, bottom=thin)

    for col in range(1, len(header) + 1):
        cell = ws.cell(row=1, column=col)
        cell.fill = hfill
        cell.font = hfont
        cell.alignment = align_center
        cell.border = hborder

    # Linhas
    def _fmt_cc(obj):
        if not obj: return "-"
        num = getattr(obj, "numero", None) or ""
        dep = getattr(obj, "departamento", None) or ""
        return f"{num} - {dep}".strip(" -")

    numero_format = 'R$ #,##0.00'

    for i, it in enumerate(qs, start=1):
        subtipo = getattr(it.subtipo, "nome", "-") if getattr(it, "subtipo", None) else "-"
        status_disp = getattr(it, "get_status_display", None)
        status_txt = status_disp() if callable(status_disp) else (it.status or "-")
        local = getattr(it.localidade, "local", "-") if getattr(it, "localidade", None) else "-"
        fornecedor = getattr(it.fornecedor, "nome", "-") if getattr(it, "fornecedor", None) else "-"
        cc_txt = _fmt_cc(getattr(it, "centro_custo", None))

        locado_flag = getattr(it, "locado", None)
        # seu choices parecem "sim"/"nao" → ajuste se for booleano
        locado_txt = "Sim" if str(locado_flag).lower() in ("sim", "true", "1") else "Não"
        loc_mensal = loc_map.get(it.id) or Decimal("0.00")

        valor_aquis = getattr(it, "valor", None) or Decimal("0.00")

        row = [
            i,
            it.nome or "",
            subtipo,
            status_txt,
            local,
            it.numero_serie or "",
            fornecedor,
            cc_txt,
            locado_txt,
            float(loc_mensal),
            float(valor_aquis),
        ]
        ws.append(row)

    # Formatação de números nos 2 últimos campos
    for r in range(2, ws.max_row + 1):
        ws.cell(row=r, column=10).number_format = numero_format
        ws.cell(row=r, column=11).number_format = numero_format

    # Largura das colunas (auto-ajuste simples)
    widths = {}
    for row in ws.iter_rows(values_only=True):
        for idx, val in enumerate(row, start=1):
            txt = str(val) if val is not None else ""
            widths[idx] = max(widths.get(idx, 0), len(txt))
    for idx, w in widths.items():
        # um pouco de folga
        ws.column_dimensions[get_column_letter(idx)].width = min(max(w + 2, 10), 48)

    # 4) Resposta HTTP
    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    now = timezone.localtime().strftime("%Y%m%d-%H%M%S")
    filename = f"itens_filtrados_{now}.xlsx"
    resp = HttpResponse(
        buffer.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


def _parse_dt(s, default_dt):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return default_dt


@login_required
def toner_cc_dashboard(request):
    hoje = date.today()
    dt_ini = _parse_dt(request.GET.get("inicio") or "", date(hoje.year, 1, 1))
    dt_fim = _parse_dt(request.GET.get("fim") or "", hoje)

    # Base: BAIXAS de itens "toner"
    base = (
        MovimentacaoItem.objects
        .filter(
            tipo_movimentacao="baixa",
            created_at__date__gte=dt_ini,
            created_at__date__lte=dt_fim,
        )
        .filter(
            Q(item__subtipo__nome__icontains="toner") |
            Q(item__categoria__nome__icontains="toner") |
            Q(item__nome__icontains="toner")
        )
        .select_related("item", "centro_custo_origem", "centro_custo_destino")
    )

    # -------- Tipagem DECIMAL robusta
    dec14_2 = DecimalField(max_digits=14, decimal_places=2)
    qtd_dec = Cast(F("quantidade"), output_field=dec14_2)
    preco_item = Coalesce(
        F("item__valor"),
        V(Decimal("0.00"), output_field=dec14_2),
        output_field=dec14_2,
    )

    # Total da baixa:
    # - se custo > 0 na movimentação, usa o custo TOTAL informado (congelado)
    # - senão, usa quantidade * item.valor (fallback)
    custo_total_expr = Case(
        When(custo__gt=Decimal("0.00"), then=F("custo")),
        default=ExpressionWrapper(qtd_dec * preco_item, output_field=dec14_2),
        output_field=dec14_2,
    )

    # -------- Dimensão CC: origem > destino > CC do item
    base_cc = base.annotate(
        cc_id=Coalesce(
            F("centro_custo_origem_id"),
            F("centro_custo_destino_id"),
            F("item__centro_custo_id"),
        ),
        cc_numero=Case(
            When(centro_custo_origem__isnull=False, then=F("centro_custo_origem__numero")),
            When(centro_custo_destino__isnull=False, then=F("centro_custo_destino__numero")),
            default=F("item__centro_custo__numero"),
            output_field=CharField(),
        ),
        cc_departamento=Case(
            When(centro_custo_origem__isnull=False, then=F("centro_custo_origem__departamento")),
            When(centro_custo_destino__isnull=False, then=F("centro_custo_destino__departamento")),
            default=F("item__centro_custo__departamento"),
            output_field=CharField(),
        ),
    ).filter(cc_id__isnull=False)

    # -------- Agregado por CC
    por_cc_qs = (
        base_cc.values("cc_id", "cc_numero", "cc_departamento")
        .annotate(
            qtd=Coalesce(Sum("quantidade"), V(0)),
            gasto=Coalesce(Sum(custo_total_expr, output_field=dec14_2),
                           V(Decimal("0.00"), output_field=dec14_2)),
        )
        .order_by("cc_numero", "cc_departamento")
    )

    linhas, cc_labels, cc_gasto = [], [], []
    total_geral = Decimal("0.00")
    for r in por_cc_qs:
        cc_nome = f'{r["cc_numero"]} - {r["cc_departamento"]}'
        gasto = Decimal(r["gasto"] or 0)
        qtd = int(r["qtd"] or 0)
        linhas.append({"cc": cc_nome, "qtd": qtd, "gasto": gasto})
        cc_labels.append(cc_nome)
        cc_gasto.append(float(gasto))
        total_geral += gasto

    # -------- Top consumidores (por usuário) — mesma regra de custo
    por_user_qs = (
        base.values("usuario__id", "usuario__nome")
        .annotate(gasto=Coalesce(Sum(custo_total_expr, output_field=dec14_2),
                                 V(Decimal("0.00"), output_field=dec14_2)))
        .order_by("-gasto")[:10]
    )
    user_labels = [r["usuario__nome"] or "—" for r in por_user_qs]
    user_gasto  = [float(r["gasto"] or 0) for r in por_user_qs]

    # -------- Exportação CSV (mesma rota com ?export=1)
    if request.GET.get("export") == "1":
        resp = HttpResponse(content_type="text/csv; charset=utf-8")
        fname = f"gasto_toner_{dt_ini:%Y%m%d}_{dt_fim:%Y%m%d}.csv"
        resp["Content-Disposition"] = f'attachment; filename="{fname}"'
        resp.write("Centro de Custo;Quantidade;Gasto (R$)\n")
        for l in linhas:
            gasto_str = f"{l['gasto']:.2f}".replace(".", ",")
            resp.write(f"{l['cc']};{l['qtd']};{gasto_str}\n")
        total_str = f"{total_geral:.2f}".replace(".", ",")
        resp.write(f"TOTAL;;{total_str}\n")
        return resp

    ctx = {
        "dt_ini": dt_ini,
        "dt_fim": dt_fim,
        "linhas": linhas,
        "cc_labels": cc_labels,
        "cc_gasto": cc_gasto,
        "user_labels": user_labels,
        "user_gasto": user_gasto,
    }
    return render(request, "front/dashboards/dashboard_toner.html", ctx)


    ##### EXPORTAR EXCEL #################

@login_required
def custo_cc_export_excel(request):
    """Exporta para Excel a mesma tabela do cc_custos_dashboard, respeitando os filtros."""
    # ── filtros (iguais ao dashboard) ─────────────────────────────────────────────
    hoje = datetime.today().date()
    dt_ini = _parse_date(request.GET.get("inicio"), hoje - timedelta(days=30))
    dt_fim = _parse_date(request.GET.get("fim"), hoje)

    # ── imports locais (mesmos modelos usados no dashboard) ──────────────────────
    from .models import (
        Locacao, MovimentacaoLicenca, MovimentacaoItem, CentroCusto,
        Usuario, Item, TipoMovLicencaChoices, TipoMovimentacaoChoices
    )

    # ── acumulador por CC (mesma estrutura) ──────────────────────────────────────
    totals = {}  # cc_id -> dict

    def acc(cc_id):
        if not cc_id:
            return None
        if cc_id not in totals:
            totals[cc_id] = {
                "cc": None,
                "usuarios": 0,
                "itens": 0,
                "licencas_set": set(),
                "assentos": 0,
                "custo_itens": Decimal("0.00"),
                "custo_licencas": Decimal("0.00"),
                "baixas": Decimal("0.00"),
            }
        return totals[cc_id]

    # ── custo mensal de ITENS (locações) por CC ──────────────────────────────────
    loc_qs = (
        Locacao.objects
        .select_related("equipamento", "equipamento__centro_custo")
        .exclude(valor_mensal__isnull=True)
    )
    for loc in loc_qs:
        item = loc.equipamento
        cc_id = getattr(item.centro_custo, "id", None)
        if not cc_id:
            continue
        valor = loc.valor_mensal or Decimal("0.00")
        if valor > 0:
            a = acc(cc_id)
            a["custo_itens"] += valor

    # ── assentos/licenças (último evento por par licença/usuário) ───────────────
    mov_l_qs = (
        MovimentacaoLicenca.objects
        .select_related("licenca", "usuario__centro_custo", "centro_custo_destino", "lote")
        .order_by("licenca_id", "usuario_id", "created_at", "id")
    )

    last_by_pair = {}
    for m in mov_l_qs:
        if m.usuario_id is None:
            continue
        last_by_pair[(m.licenca_id, m.usuario_id)] = m

    for (lic_id, user_id), m in last_by_pair.items():
        if m.tipo != TipoMovLicencaChoices.ATRIBUICAO:
            continue
        cc_id = (
            getattr(getattr(m.usuario, "centro_custo", None), "id", None)
            or getattr(m.centro_custo_destino, "id", None)
            or getattr(m.licenca.centro_custo, "id", None)
        )
        if not cc_id:
            continue

        cm = m.custo_mensal_usado or Decimal("0.00")
        a = acc(cc_id)
        a["assentos"] += 1
        a["custo_licencas"] += cm
        a["licencas_set"].add(lic_id)

    # ── baixas no período ────────────────────────────────────────────────────────
    baixas_qs = (
        MovimentacaoItem.objects
        .filter(
            tipo_movimentacao=TipoMovimentacaoChoices.BAIXA,
            created_at__date__gte=dt_ini,
            created_at__date__lte=dt_fim,
        )
        .select_related("item__centro_custo", "centro_custo_origem")
    )
    for mv in baixas_qs:
        cc_id = (
            getattr(mv.centro_custo_origem, "id", None)
            or getattr(getattr(mv.item, "centro_custo", None), "id", None)
        )
        if not cc_id:
            continue
        valor_baixa = mv.custo if mv.custo is not None else (mv.item.valor or Decimal("0.00")) * (mv.quantidade or 1)
        a = acc(cc_id)
        a["baixas"] += (valor_baixa or Decimal("0.00"))

    # ── metadados: usuários e itens por CC ───────────────────────────────────────
    cc_ids = list(totals.keys())
    ccs = {cc.id: cc for cc in CentroCusto.objects.filter(id__in=cc_ids)}

    users_count = (
        Usuario.objects
        .filter(centro_custo_id__in=cc_ids, status="ativo")
        .values("centro_custo_id")
        .annotate(n=Count("id"))
    )
    itens_count = (
        Item.objects
        .filter(centro_custo_id__in=cc_ids)
        .values("centro_custo_id")
        .annotate(n=Count("id"))
    )
    map_users = {r["centro_custo_id"]: r["n"] for r in users_count}
    map_itens = {r["centro_custo_id"]: r["n"] for r in itens_count}

    # ── LINHAS (igual ao dashboard) ──────────────────────────────────────────────
    linhas = []
    for cc_id, d in totals.items():
        cc = ccs.get(cc_id)
        if not cc:
            continue
        d["cc"] = cc
        d["usuarios"] = map_users.get(cc_id, 0)
        d["itens"] = map_itens.get(cc_id, 0)
        lic_tipos = len(d["licencas_set"])
        d["licencas"] = lic_tipos
        d["total_mensal"] = (d["custo_itens"] + d["custo_licencas"])
        d["total_geral"] = (d["total_mensal"] + d["baixas"])

        linhas.append({
            "cc": cc,
            "usuarios": d["usuarios"],
            "itens": d["itens"],
            "licencas": d["licencas"],
            "assentos": d["assentos"],
            "custo_itens": d["custo_itens"],
            "custo_licencas": d["custo_licencas"],
            "baixas": d["baixas"],
            "total_mensal": d["total_mensal"],
            "total_geral": d["total_geral"],
        })

    # mesma ordenação da tela
    linhas.sort(key=lambda x: x["total_geral"], reverse=True)

    # ── EXCEL (apenas tabela detalhamento + resumo) ──────────────────────────────
    wb = Workbook()

    # Aba 1: Detalhamento (títulos iguais aos da tabela do template)
    ws = wb.active
    ws.title = "Detalhamento"

    headers = [
        "Centro de Custo",
        "Usuários",
        "Itens",
        "Licenças",
        "Assentos ativos",
        "Custo Itens (R$/mês)",
        "Custo Licenças (R$/mês)",
        "Baixas no período (R$)",
        "Total Mensal (R$)",
        "Total Geral (R$)",
    ]
    ws.append(headers)

    # estilos
    header_fill = PatternFill("solid", fgColor="FF1D4ED8")
    header_font = Font(color="FFFFFFFF", bold=True)
    thin = Side(style="thin", color="FFCBD5E1")
    border_all = Border(left=thin, right=thin, top=thin, bottom=thin)
    int_fmt = "#,##0"
    money_fmt = "[$R$-pt-BR] #,##0.00"

    for c in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=c)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border_all

    def _cc_nome(ccobj):
        try:
            return f"{ccobj.numero} - {ccobj.departamento}"
        except Exception:
            return str(ccobj) if ccobj else "—"

    for l in linhas:
        ws.append([
            _cc_nome(l["cc"]),
            int(l["usuarios"] or 0),
            int(l["itens"] or 0),
            int(l["licencas"] or 0),
            int(l["assentos"] or 0),
            Decimal(l["custo_itens"] or 0),
            Decimal(l["custo_licencas"] or 0),
            Decimal(l["baixas"] or 0),
            Decimal(l["total_mensal"] or 0),
            Decimal(l["total_geral"] or 0),
        ])

    last_row = ws.max_row
    for r in range(2, last_row + 1):
        for col in (2, 3, 4, 5):
            ws.cell(row=r, column=col).number_format = int_fmt
            ws.cell(row=r, column=col).border = border_all
        for col in (6, 7, 8, 9, 10):
            ws.cell(row=r, column=col).number_format = money_fmt
            ws.cell(row=r, column=col).border = border_all
        ws.cell(row=r, column=1).border = border_all

    # transforma em "tabela" do Excel (zebra)
    ref = f"A1:J{last_row}"
    table = Table(displayName="tb_detalhamento", ref=ref)
    table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium9", showRowStripes=True, showColumnStripes=False)
    ws.add_table(table)

    # larguras e freeze
    widths = [28, 12, 10, 12, 16, 22, 22, 20, 18, 16]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[chr(64+i)].width = w
    ws.freeze_panes = "A2"

    # Aba 2: Resumo rápido
    ws2 = wb.create_sheet("Resumo")
    ws2["A1"] = "Período"
    ws2["B1"] = f"{dt_ini:%d/%m/%Y} — {dt_fim:%d/%m/%Y}"
    ws2["A1"].font = Font(bold=True)
    ws2["A3"] = "Centros de Custo (linhas)"
    ws2["B3"] = len(linhas)
    ws2["A4"] = "Total Geral (soma)"
    if last_row >= 2:
        ws2["B4"] = f"=SUM(Detalhamento!J2:J{last_row})"
        ws2["B4"].number_format = money_fmt
    ws2.column_dimensions["A"].width = 30
    ws2.column_dimensions["B"].width = 36

    # resposta HTTP
    resp = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="custo_cc_{dt_ini:%Y%m%d}_{dt_fim:%Y%m%d}.xlsx"'
    wb.save(resp)
    return resp

try:
    from .models import LicencaLote
except Exception:
    LicencaLote = None

def _parse_date_opt(s: str):
    """Converte 'YYYY-MM-DD' em date, ou None."""
    try:
        return datetime.strptime(s, "%Y-%m-%d").date() if s else None
    except Exception:
        return None


def _meses_do_ciclo(lic: Licenca) -> int | None:
    try:
        return lic._meses_do_ciclo()
    except Exception:
        mapping = {"mensal": 1, "MENSAL": 1,
                   "semestral": 6, "SEMESTRAL": 6,
                   "anual": 12, "ANUAL": 12,
                   "tri": 36, "TRI": 36}
        m = getattr(lic, "periodicidade", None)
        return mapping.get(m, None)


@login_required
def licencas_dashboard(request):
    hoje = date.today()

    # ---- filtros ----
    q = (request.GET.get("q") or "").strip()
    fornecedor = (request.GET.get("fornecedor") or "").strip()
    centro_custo = (request.GET.get("centro_custo") or "").strip()
    periodicidade = (request.GET.get("periodicidade") or "").strip()
    pmb = (request.GET.get("pmb") or "").strip().lower()
    dt_ini = _parse_date_opt(request.GET.get("inicio"))
    dt_fim = _parse_date_opt(request.GET.get("fim"))

    lic_qs = Licenca.objects.select_related("fornecedor", "centro_custo").all().order_by("nome")
    if q:
        lic_qs = lic_qs.filter(
            Q(nome__icontains=q) |
            Q(observacao__icontains=q) |
            Q(fornecedor__nome__icontains=q)
        )
    if fornecedor:
        lic_qs = lic_qs.filter(fornecedor_id=fornecedor)
    if centro_custo:
        lic_qs = lic_qs.filter(centro_custo_id=centro_custo)
    if periodicidade:
        lic_qs = lic_qs.filter(periodicidade=periodicidade)
    if pmb in ("sim", "nao"):
        lic_qs = lic_qs.filter(pmb=pmb)
    if dt_ini:
        lic_qs = lic_qs.filter(data_inicio__gte=dt_ini)
    if dt_fim:
        lic_qs = lic_qs.filter(data_inicio__lte=dt_fim)

    licencas = list(lic_qs)
    lic_ids = [l.id for l in licencas]

    # ---- últimos movimentos por (licença, usuário) para saber quem está ATIVO e em qual CC (do usuário) ----
    mov_qs = (
        MovimentacaoLicenca.objects
        .select_related("usuario__centro_custo", "centro_custo_destino")
        .filter(licenca_id__in=lic_ids, usuario__isnull=False)
        .order_by("licenca_id", "usuario_id", "created_at", "id")
    )
    # Guardar o ÚLTIMO objeto (não só o tipo)
    last_by_pair = {}
    for m in mov_qs:
        last_by_pair[(m.licenca_id, m.usuario_id)] = m

    # Quantos ativos por licença (para KPIs/linhas)
    ativos_por_lic = {}
    for (lic_id, _uid), mv in last_by_pair.items():
        if mv.tipo == "atribuicao":
            ativos_por_lic[lic_id] = ativos_por_lic.get(lic_id, 0) + 1

    # ---- Lotes (custos detalhados) ----
    lotes_rows = []
    lotes_totais_por_lic = {}
    if LicencaLote and lic_ids:
        raw_lotes = (
            LicencaLote.objects
            .filter(licenca_id__in=lic_ids)
            .select_related("licenca")
            .order_by("-created_at", "-id")
        )
        for lt in raw_lotes:
            lic = lt.licenca
            total = int(getattr(lt, "quantidade_total", 0) or 0)
            disp = int(getattr(lt, "quantidade_disponivel", 0) or 0)
            usados = max(0, total - disp)

            meses = _meses_do_ciclo(lic) or 1
            custo_ciclo_lote = getattr(lt, "custo_ciclo", None)

            if custo_ciclo_lote:
                custo_mensal_lote = (Decimal(custo_ciclo_lote) / Decimal(meses)).quantize(Decimal("0.01"))
            else:
                cm_unit_lic = Decimal("0.00")
                if getattr(lic, "custo", None):
                    cm_unit_lic = (Decimal(lic.custo) / Decimal(meses)).quantize(Decimal("0.01"))
                custo_mensal_lote = (cm_unit_lic * Decimal(total)).quantize(Decimal("0.01"))

            prop_uso = Decimal(usados) / Decimal(total) if total > 0 else Decimal("0")
            custo_mensal_usado = (custo_mensal_lote * prop_uso).quantize(Decimal("0.01"))
            custo_anual_lote = (custo_mensal_lote * Decimal(12)).quantize(Decimal("0.01"))
            custo_anual_usado = (custo_mensal_usado * Decimal(12)).quantize(Decimal("0.01"))

            agg = lotes_totais_por_lic.setdefault(
                lic.id,
                {"total": 0, "disp": 0, "usados": 0,
                 "cm_lote": Decimal("0.00"), "cm_usado": Decimal("0.00"),
                 "ca_lote": Decimal("0.00"), "ca_usado": Decimal("0.00")}
            )
            agg["total"] += total
            agg["disp"] += disp
            agg["usados"] += usados
            agg["cm_lote"] += custo_mensal_lote
            agg["cm_usado"] += custo_mensal_usado
            agg["ca_lote"] += custo_anual_lote
            agg["ca_usado"] += custo_anual_usado

            lotes_rows.append({
                "licenca_id": lic.id,
                "licenca_nome": lic.nome,
                "lote_nome": getattr(lt, "nome", f"Lote #{lt.id}"),
                "total": total,
                "disp": disp,
                "usados": usados,
                "custo_ciclo": custo_ciclo_lote,
                "custo_mensal_lote": custo_mensal_lote,
                "custo_mensal_usado": custo_mensal_usado,
                "custo_anual_lote": custo_anual_lote,
                "custo_anual_usado": custo_anual_usado,
                "periodicidade": getattr(lt, "periodicidade", None),
                "pedido": getattr(lt, "pedido", None),
                "obs": getattr(lt, "observacao", None),
            })

    # ---- helpers ----
    def cm_unit(lic: Licenca) -> Decimal:
        meses = _meses_do_ciclo(lic)
        if not meses or not getattr(lic, "custo", None):
            return Decimal("0.00")
        return (Decimal(lic.custo) / Decimal(meses)).quantize(Decimal("0.01"))

    def cc_label_from_cc(cc) -> str:
        if not cc:
            return "—"
        return f"{getattr(cc, 'numero', '—')} - {getattr(cc, 'departamento', '—')}"

    # ---- linhas + KPIs ----
    linhas = []
    kpi_total = len(licencas)
    kpi_assentos_ativos = 0
    kpi_custo_mensal = Decimal("0.00")
    kpi_custo_anual = Decimal("0.00")
    kpi_fim_proximo = 0

    # Para o gráfico por CC: distribuir custo por CC do usuário
    cc_costs = {}  # label -> Decimal acumulado

    forn_labels_map, forn_val_map = {}, {}
    per_labels_map, per_count_map = {}, {}

    for l in licencas:
        assentos = int(ativos_por_lic.get(l.id, 0))
        kpi_assentos_ativos += assentos

        mensal_unit = cm_unit(l)
        qtd = int(getattr(l, "quantidade", 0) or 0)
        mensal_total = (mensal_unit * Decimal(qtd)).quantize(Decimal("0.01"))
        anual_total = (mensal_total * Decimal(12)).quantize(Decimal("0.01"))

        kpi_custo_mensal += mensal_total
        kpi_custo_anual += anual_total

        if l.data_fim and (l.data_fim <= (hoje + timedelta(days=30))):
            kpi_fim_proximo += 1

        # —— distribuição por CC do usuário para o gráfico —— #
        # pega os últimos movimentos ATIVOS desta licença
        ativos_movs = [mv for (lic_id, _uid), mv in last_by_pair.items()
                       if lic_id == l.id and mv.tipo == "atribuicao"]
        # para cada assento ativo, soma 1 * mensal_unit no CC do usuário
        for mv in ativos_movs:
            user_cc = getattr(getattr(mv, "usuario", None), "centro_custo", None)
            cc_label = cc_label_from_cc(user_cc) or "—"
            cc_costs[cc_label] = cc_costs.get(cc_label, Decimal("0.00")) + mensal_unit

        # se sobrar quantidade sem usuário (estoque), vai pro CC da licença
        restante = max(0, qtd - len(ativos_movs))
        if restante > 0:
            cc_label_lic = cc_label_from_cc(getattr(l, "centro_custo", None))
            cc_costs[cc_label_lic] = cc_costs.get(cc_label_lic, Decimal("0.00")) + (mensal_unit * Decimal(restante))

        # —— agregações por fornecedor/periodicidade (para gráficos) —— #
        forn_name = getattr(l.fornecedor, "nome", "—") if l.fornecedor_id else "—"
        forn_labels_map.setdefault(forn_name, None)
        forn_val_map[forn_name] = forn_val_map.get(forn_name, 0.0) + float(mensal_total)

        per = l.get_periodicidade_display() if hasattr(l, "get_periodicidade_display") else (l.periodicidade or "—")
        per_labels_map.setdefault(per, None)
        per_count_map[per] = per_count_map.get(per, 0) + 1

        # ——— linha p/ tabela principal ———
        lt = lotes_totais_por_lic.get(l.id, None)
        linhas.append({
            "id": l.id,
            "nome": l.nome,
            "fornecedor": getattr(l.fornecedor, "nome", None),
            "cc": cc_label_from_cc(getattr(l, "centro_custo", None)),
            "periodicidade": per,
            "pmb": l.pmb,
            "qtd": qtd,
            "assentos": assentos,
            "custo_ciclo": getattr(l, "custo", Decimal("0.00")),
            "custo_mensal": mensal_total,
            "custo_anual": anual_total,
            "data_inicio": l.data_inicio,
            "data_fim": l.data_fim,
            "lotes_total": lt["total"] if lt else 0,
            "lotes_disp": lt["disp"] if lt else 0,
            "lotes_usados": lt["usados"] if lt else 0,
            "lotes_cm_lote": lt["cm_lote"] if lt else Decimal("0.00"),
            "lotes_cm_usado": lt["cm_usado"] if lt else Decimal("0.00"),
            "lotes_ca_lote": lt["ca_lote"] if lt else Decimal("0.00"),
            "lotes_ca_usado": lt["ca_usado"] if lt else Decimal("0.00"),
        })

    # —— gráfico por CC (ordenado por valor decrescente) ——
    cc_sorted = sorted(cc_costs.items(), key=lambda kv: kv[1], reverse=True)
    cc_labels = [k for k, _ in cc_sorted]
    cc_mensal = [float(v) for _, v in cc_sorted]

    forn_labels = list(forn_labels_map.keys())
    forn_mensal = [float(forn_val_map.get(n, 0.0)) for n in forn_labels]
    per_labels = list(per_labels_map.keys())
    per_counts = [int(per_count_map.get(n, 0)) for n in per_labels]

    # choices de periodicidade com ordem “bonita”
    base_choices = list(getattr(Licenca._meta.get_field("periodicidade"), "choices", []))
    order_map = {"MENSAL": 0, "mensal": 0, "SEMESTRAL": 1, "semestral": 1, "ANUAL": 2, "anual": 2, "TRI": 3, "tri": 3}
    periodicidade_choices = sorted(base_choices, key=lambda kv: order_map.get(str(kv[0]), 99))

    context = {
        "dt_ini": dt_ini or "",
        "dt_fim": dt_fim or "",
        "q": q, "fornecedor": fornecedor, "centro_custo": centro_custo,
        "periodicidade": periodicidade, "pmb": pmb,

        "fornecedores": Fornecedor.objects.all().order_by("nome"),
        "centros_custo": CentroCusto.objects.all().order_by("numero", "departamento"),
        "periodicidade_choices": periodicidade_choices,

        "kpi_total": kpi_total,
        "kpi_assentos_ativos": kpi_assentos_ativos,
        "kpi_custo_mensal": kpi_custo_mensal,
        "kpi_custo_anual": kpi_custo_anual,
        "kpi_fim_proximo": kpi_fim_proximo,

        "linhas": linhas,

        # gráficos
        "cc_labels": cc_labels,
        "cc_mensal": cc_mensal,
        "forn_labels": forn_labels,
        "forn_mensal": forn_mensal,
        "per_labels": per_labels,
        "per_counts": per_counts,

        # lotes (flatten)
        "tem_lotes": bool(LicencaLote),
        "lotes_rows": lotes_rows,
    }
    return render(request, "front/dashboards/licencas_dashboard.html", context)



