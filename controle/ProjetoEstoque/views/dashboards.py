from decimal import Decimal, ROUND_HALF_UP
from datetime import date, timedelta, datetime

from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.template.loader import get_template
from xhtml2pdf import pisa
from django.db.models import Q, Count, Sum, F, Case, When, Value as V
from django.db.models import DecimalField
from django.db.models.functions import TruncMonth, Coalesce
from django.utils import timezone
from django.utils.dateparse import parse_date
from dateutil.relativedelta import relativedelta

from ..models import (
    Item, Licenca, LicencaLote, MovimentacaoLicenca,
    MovimentacaoItem, Preventiva, PreventivaExecucao,
    CentroCusto, Fornecedor, Localidade, Usuario, StatusItemChoices,
    PeriodicidadeChoices, TipoMovLicencaChoices, TipoMovimentacaoChoices,
    Locacao, CheckListModelo,
)

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

# ==============================================================================
# 1. MOTOR DE DADOS (Funções Auxiliares)
# ==============================================================================

def _generate_month_keys(months=12):
    """
    Gera as chaves (ano, mes) e os labels para os últimos N meses.
    Retorna: (lista_chaves, lista_labels)
    Ex: ([(2023, 1), ...], ['Jan/23', ...])
    """
    today = timezone.localdate()
    keys = []
    labels = []
    
    # Começa do primeiro dia do mês atual
    curr = today.replace(day=1)
    
    names = ["", "Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]

    for _ in range(months):
        k = (curr.year, curr.month)
        lbl = f"{names[curr.month]}/{str(curr.year)[2:]}"
        
        keys.append(k)
        labels.append(lbl)
        
        # Volta 1 mês
        curr = (curr - timedelta(days=1)).replace(day=1)
    
    # Inverte para ficar cronológico (Antigo -> Novo)
    return list(reversed(keys)), list(reversed(labels))

def _process_chart_data(keys, queryset):
    """
    Recebe um QuerySet agrupado por mês e alinha com as chaves de data.
    IMPORTANTE: O QuerySet DEVE ter o campo anotado como 'valor'.
    """
    # 1. Transforma o QuerySet em um Dicionário de busca rápida: {(ano, mes): valor}
    data_map = {}
    for item in queryset:
        dt = item.get('m') # 'm' é o TruncMonth
        val = item.get('valor') # 'valor' é o dado padronizado
        
        if dt:
            # Garante que None vire 0 (caso de Somas nulas)
            final_val = val if val is not None else 0
            data_map[(dt.year, dt.month)] = final_val

    # 2. Monta a lista final alinhada com as keys (preenche buracos com 0)
    result = []
    for k in keys:
        result.append(data_map.get(k, 0))
        
    return result

# ==============================================================================
# 2. VIEW PRINCIPAL
# ==============================================================================

@login_required
def dashboard(request):
    """
    Dashboard Enterprise - Refatorado para estabilidade total.
    """
    # --- Configuração de Tempo ---
    month_keys, labels = _generate_month_keys(12)
    
    # Data de corte (início do período)
    first_key = month_keys[0] # (Ano, Mes) mais antigo
    start_date = timezone.datetime(year=first_key[0], month=first_key[1], day=1)
    start_date = timezone.make_aware(start_date) # Adiciona timezone se necessário

    # --- A. KPIs (Topo) ---
    kpi = {
        "total": Item.objects.count(),
        "ativos": Item.objects.filter(status='ativo').count(),
        "estoque": Item.objects.filter(status='backup').count(),
        "manutencao": Item.objects.filter(status='manutencao').count(),
        "problema": Item.objects.filter(status__in=['defeito', 'sucata', 'queimado']).count(),
    }

    # --- B. Gráfico de Movimentações (Linha do Tempo) ---
    # Base: últimos 12 meses
    mov_base = MovimentacaoItem.objects.filter(created_at__gte=start_date)

    def get_mov_series(tipo_mov):
        """Helper interno para buscar movimentações padronizadas"""
        qs = (
            mov_base.filter(tipo_movimentacao=tipo_mov)
            .annotate(m=TruncMonth('created_at'))
            .values('m')
            .annotate(valor=Count('id')) # <--- PADRONIZAÇÃO: Nome sempre será 'valor'
            .order_by('m')
        )
        return _process_chart_data(month_keys, qs)

    series_mov = {
        "entrada": get_mov_series('entrada'),
        "baixa": get_mov_series('baixa'),
        "transf": get_mov_series('transferencia'),
        "manut": get_mov_series('envio_manutencao'), # Envios para manutenção
    }

    # --- C. Gráfico de Custos (Manutenção) ---
    # Soma dos custos de manutenção nos últimos 12 meses
    qs_custo = (
        mov_base.filter(tipo_movimentacao__in=['envio_manutencao', 'retorno_manutencao'])
        .annotate(m=TruncMonth('created_at'))
        .values('m')
        .annotate(valor=Sum('custo')) # <--- PADRONIZAÇÃO: Nome sempre será 'valor'
        .order_by('m')
    )
    data_custo = _process_chart_data(month_keys, qs_custo)

    # --- D. Preventivas (Status) ---
    today = timezone.localdate()
    prev_atrasadas = Preventiva.objects.filter(data_proxima__lt=today).count()
    prev_em_dia = Preventiva.objects.filter(data_proxima__gte=today).count()
    
    # Próximas a vencer (Tabela)
    prev_proximas = (
        Preventiva.objects
        .filter(data_proxima__gte=today)
        .select_related('equipamento', 'checklist_modelo')
        .order_by('data_proxima')[:5]
    )

    # --- E. Categorias (Barras) ---
    # Função genérica para Top N
    def get_top_category(field_name, limit=5):
        qs = (
            Item.objects.values(field_name)
            .annotate(valor=Count('id')) # Padronizado
            .order_by('-valor')[:limit]
        )
        # Trata valores nulos no nome
        labels_cat = [item[field_name] or 'Não Definido' for item in qs]
        data_cat = [item['valor'] for item in qs]
        return labels_cat, data_cat

    sub_labels, sub_data = get_top_category('subtipo__nome', 5)
    loc_labels, loc_data = get_top_category('localidade__local', 8)

    # --- Contexto Final ---
    context = {
        "kpi": kpi,
        "labels": labels, # Eixo X (Jan/24, Fev/24...)
        "series": series_mov,
        "custo_data": data_custo,
        "prev_status": [prev_em_dia, prev_atrasadas],
        "prev_proximas": prev_proximas,
        "cat_subtipo": {"labels": sub_labels, "data": sub_data},
        "cat_local": {"labels": loc_labels, "data": loc_data},
    }

    return render(request, "front/dashboards/dashboard.html", context)


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
def _parse_date(date_str, default):
    if not date_str: return default
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except:
        return default

def _get_meses_ciclo(periodicidade_str):
    """Auxiliar para converter periodicidade em meses"""
    if not periodicidade_str: return 1
    p = str(periodicidade_str).upper()
    if 'MEN' in p: return 1
    if 'BI' in p: return 2
    if 'TRI' in p: return 3
    if 'SEM' in p: return 6
    if 'ANU' in p: return 12
    return 1

def _get_cc_custos_data(request):
    """
    Função Helper: Processa todos os cálculos de custo por Centro de Custo.
    Retorna o dicionário de contexto para ser usado na View Web ou no PDF.
    """
    hoje = timezone.localdate()
    dt_ini = _parse_date(request.GET.get("inicio"), hoje.replace(day=1))
    dt_fim = _parse_date(request.GET.get("fim"), hoje)

    totals = {}

    def get_acc(cc_id):
        if not cc_id: return None
        if cc_id not in totals:
            totals[cc_id] = {
                "cc_obj": None,
                "qtd_usuarios": 0,
                "qtd_itens": 0,
                "qtd_licencas": 0,
                "custo_locacao": Decimal("0.00"),
                "custo_licencas": Decimal("0.00"),
                "custo_baixas": Decimal("0.00"),
            }
        return totals[cc_id]

    # 1. Locação (Hardware)
    locacoes = Locacao.objects.select_related("equipamento__centro_custo").filter(
        equipamento__status='ativo', valor_mensal__gt=0, equipamento__centro_custo__isnull=False
    )
    for loc in locacoes:
        acc = get_acc(loc.equipamento.centro_custo.id)
        if acc: acc["custo_locacao"] += (loc.valor_mensal or Decimal(0))

    # 2. Licenças (Software - Unitário)
    movs_lic = MovimentacaoLicenca.objects.select_related(
        "licenca", "usuario__centro_custo", "centro_custo_destino", "lote"
    ).filter(usuario__isnull=False).order_by("licenca_id", "usuario_id", "created_at")

    estado_atual_lic = { (m.licenca_id, m.usuario_id): m for m in movs_lic }

    for (lid, uid), mov in estado_atual_lic.items():
        if mov.tipo == TipoMovLicencaChoices.ATRIBUICAO:
            cc_id = None
            if mov.usuario and mov.usuario.centro_custo:
                cc_id = mov.usuario.centro_custo.id
            elif mov.centro_custo_destino:
                cc_id = mov.centro_custo_destino.id
            elif mov.licenca.centro_custo:
                cc_id = mov.licenca.centro_custo.id
            
            acc = get_acc(cc_id)
            if acc:
                lote = mov.lote
                custo_mensal_unit = Decimal("0.00")
                if lote:
                    c_ciclo = lote.custo_ciclo or Decimal(0)
                    meses = _get_meses_ciclo(lote.periodicidade)
                    if meses > 0: custo_mensal_unit = (c_ciclo / Decimal(meses))
                else:
                    custo_mensal_unit = mov.licenca.custo or Decimal(0) # Fallback

                acc["custo_licencas"] += custo_mensal_unit
                acc["qtd_licencas"] += 1

    # 3. Baixas (Pontual)
    baixas = MovimentacaoItem.objects.filter(
        tipo_movimentacao=TipoMovimentacaoChoices.BAIXA,
        created_at__date__gte=dt_ini, created_at__date__lte=dt_fim
    ).select_related("item__centro_custo", "centro_custo_origem")

    for b in baixas:
        cc_id = b.centro_custo_origem.id if b.centro_custo_origem else (b.item.centro_custo.id if b.item.centro_custo else None)
        acc = get_acc(cc_id)
        if acc:
            val = b.custo if b.custo is not None else (b.item.valor or Decimal(0)) * (b.quantidade or 1)
            acc["custo_baixas"] += val

    # 4. Metadados (Nomes, Contagens)
    cc_ids = list(totals.keys())
    ccs_objs = CentroCusto.objects.filter(id__in=cc_ids)
    for cc in ccs_objs:
        if cc.id in totals: totals[cc.id]["cc_obj"] = cc

    users_agg = Usuario.objects.filter(centro_custo_id__in=cc_ids, status='ativo').values('centro_custo_id').annotate(n=Count('id'))
    for u in users_agg: 
        if u['centro_custo_id'] in totals: totals[u['centro_custo_id']]['qtd_usuarios'] = u['n']

    itens_agg = Item.objects.filter(centro_custo_id__in=cc_ids, status='ativo').values('centro_custo_id').annotate(n=Count('id'))
    for i in itens_agg:
        if i['centro_custo_id'] in totals: totals[i['centro_custo_id']]['qtd_itens'] = i['n']

    # 5. Consolidação
    linhas = []
    total_geral_itens = Decimal(0)
    total_geral_lics = Decimal(0)
    total_geral_baixas = Decimal(0)

    for cc_id, dados in totals.items():
        if not dados["cc_obj"]: continue

        c_itens = dados["custo_locacao"]
        c_lics = dados["custo_licencas"]
        c_baixas = dados["custo_baixas"]
        total_mensal = c_itens + c_lics
        total_impacto = total_mensal + c_baixas

        total_geral_itens += c_itens
        total_geral_lics += c_lics
        total_geral_baixas += c_baixas

        linhas.append({
            "cc": dados["cc_obj"],
            "usuarios": dados["qtd_usuarios"],
            "itens": dados["qtd_itens"],
            "licencas": dados["qtd_licencas"],
            "custo_itens": c_itens,
            "custo_licencas": c_lics,
            "baixas": c_baixas,
            "total_mensal": total_mensal,
            "total_impacto": total_impacto
        })

    linhas.sort(key=lambda x: x["total_impacto"], reverse=True)

    # Dados de Gráfico
    chart_labels = [f"{l['cc'].numero}" for l in linhas[:10]]
    chart_itens = [float(l['custo_itens']) for l in linhas[:10]]
    chart_lics = [float(l['custo_licencas']) for l in linhas[:10]]

    return {
        "dt_ini": dt_ini,
        "dt_fim": dt_fim,
        "linhas": linhas,
        
        # KPIs
        "kpi_cc_count": len(linhas),
        "kpi_total_mensal": total_geral_itens + total_geral_lics,
        "kpi_total_baixas": total_geral_baixas,
        "kpi_top_cc": linhas[0]['cc'].departamento if linhas else "-",
        
        # Charts (apenas para Web)
        "js_labels": chart_labels,
        "js_itens": chart_itens,
        "js_lics": chart_lics,
        "js_mix_values": [float(total_geral_itens), float(total_geral_lics)]
    }

@login_required
def cc_custos_dashboard(request):
    """
    Dashboard de Custos por Centro de Custo (Enterprise Version)
    - Locação de Hardware (Valor Mensal)
    - Licenças de Software (Valor Unitário Mensal x Qtd Usuários)
    - Baixas/Perdas (Valor Pontual no Período)
    """
    hoje = timezone.localdate()
    dt_ini = _parse_date(request.GET.get("inicio"), hoje.replace(day=1))
    dt_fim = _parse_date(request.GET.get("fim"), hoje)

    totals = {}

    def get_acc(cc_id):
        if not cc_id:
            return None
        if cc_id not in totals:
            totals[cc_id] = {
                "cc_obj": None,
                "qtd_usuarios": 0,
                "qtd_itens": 0,
                "qtd_licencas": 0,
                "custo_locacao": Decimal("0.00"),
                "custo_licencas": Decimal("0.00"),
                "custo_baixas": Decimal("0.00"),
            }
        return totals[cc_id]

    # ==========================================================
    # 1. CUSTO DE LOCAÇÃO (Hardware Recorrente)
    # ==========================================================
    locacoes = (
        Locacao.objects
        .select_related("equipamento__centro_custo")
        .filter(
            equipamento__status='ativo',
            valor_mensal__gt=0,
            equipamento__centro_custo__isnull=False
        )
    )

    for loc in locacoes:
        cc_id = loc.equipamento.centro_custo.id
        acc = get_acc(cc_id)
        if acc:
            acc["custo_locacao"] += Decimal(loc.valor_mensal or 0)

    # ==========================================================
    # 2. CUSTO DE LICENÇAS (Software Recorrente - Lógica Corrigida)
    # ==========================================================
    movs_lic = (
        MovimentacaoLicenca.objects
        .select_related("licenca", "usuario__centro_custo", "centro_custo_destino", "lote")
        .filter(usuario__isnull=False)
        .order_by("licenca_id", "usuario_id", "created_at")
    )

    estado_atual_lic = {}
    for m in movs_lic:
        estado_atual_lic[(m.licenca_id, m.usuario_id)] = m

    for (lid, uid), mov in estado_atual_lic.items():
        if mov.tipo == TipoMovLicencaChoices.ATRIBUICAO:
            cc_id = None
            if mov.usuario and mov.usuario.centro_custo:
                cc_id = mov.usuario.centro_custo.id
            elif mov.centro_custo_destino:
                cc_id = mov.centro_custo_destino.id
            elif mov.licenca.centro_custo:
                cc_id = mov.licenca.centro_custo.id

            acc = get_acc(cc_id)
            if not acc:
                continue

            lote = mov.lote
            custo_mensal_unitario = Decimal("0.00")

            if lote and (lote.quantidade_total or 0) > 0:
                qtd_lote = Decimal(lote.quantidade_total or 0)
                custo_ciclo_lote = Decimal(lote.custo_ciclo or 0)
                periodicidade = str(lote.periodicidade or "").lower()

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

                custo_mensal_unitario = (custo_mensal_lote_base / qtd_lote).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )

            else:
                custo_base = Decimal(getattr(mov.licenca, "custo", 0) or 0)
                custo_mensal_unitario = custo_base.quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )

            acc["custo_licencas"] += custo_mensal_unitario
            acc["qtd_licencas"] += 1

    # ==========================================================
    # 3. CUSTO DE BAIXAS (Perda/Descarte - Pontual no Período)
    # ==========================================================
    baixas = (
        MovimentacaoItem.objects
        .filter(
            tipo_movimentacao=TipoMovimentacaoChoices.BAIXA,
            created_at__date__gte=dt_ini,
            created_at__date__lte=dt_fim
        )
        .select_related("item__centro_custo", "centro_custo_origem")
    )

    for b in baixas:
        cc_id = None
        if b.centro_custo_origem:
            cc_id = b.centro_custo_origem.id
        elif b.item.centro_custo:
            cc_id = b.item.centro_custo.id

        acc = get_acc(cc_id)
        if acc:
            custo_baixa = b.custo if b.custo is not None else (Decimal(b.item.valor or 0) * Decimal(b.quantidade or 1))
            acc["custo_baixas"] += Decimal(custo_baixa or 0)

    # ==========================================================
    # 4. Dados Cadastrais para Contexto
    # ==========================================================
    cc_ids = list(totals.keys())

    ccs_objs = CentroCusto.objects.filter(id__in=cc_ids)
    for cc in ccs_objs:
        if cc.id in totals:
            totals[cc.id]["cc_obj"] = cc

    users_agg = (
        Usuario.objects
        .filter(centro_custo_id__in=cc_ids, status='ativo')
        .values('centro_custo_id')
        .annotate(n=Count('id'))
    )
    for u in users_agg:
        if u['centro_custo_id'] in totals:
            totals[u['centro_custo_id']]['qtd_usuarios'] = u['n']

    itens_agg = (
        Item.objects
        .filter(centro_custo_id__in=cc_ids, status='ativo')
        .values('centro_custo_id')
        .annotate(n=Count('id'))
    )
    for i in itens_agg:
        if i['centro_custo_id'] in totals:
            totals[i['centro_custo_id']]['qtd_itens'] = i['n']

    # ==========================================================
    # 5. Montagem Final
    # ==========================================================
    linhas = []

    total_geral_itens = Decimal("0.00")
    total_geral_lics = Decimal("0.00")
    total_geral_baixas = Decimal("0.00")

    for cc_id, dados in totals.items():
        if not dados["cc_obj"]:
            continue

        c_itens = dados["custo_locacao"]
        c_lics = dados["custo_licencas"]
        c_baixas = dados["custo_baixas"]

        total_mensal = c_itens + c_lics
        total_impacto = total_mensal + c_baixas

        total_geral_itens += c_itens
        total_geral_lics += c_lics
        total_geral_baixas += c_baixas

        linhas.append({
            "cc": dados["cc_obj"],
            "usuarios": dados["qtd_usuarios"],
            "itens": dados["qtd_itens"],
            "licencas": dados["qtd_licencas"],
            "custo_itens": c_itens,
            "custo_licencas": c_lics,
            "baixas": c_baixas,
            "total_mensal": total_mensal,
            "total_impacto": total_impacto,
        })

    linhas.sort(key=lambda x: x["total_impacto"], reverse=True)

    chart_labels = [f"{l['cc'].numero}" for l in linhas[:10]]
    chart_itens = [float(l['custo_itens']) for l in linhas[:10]]
    chart_lics = [float(l['custo_licencas']) for l in linhas[:10]]

    context = {
        "dt_ini": dt_ini,
        "dt_fim": dt_fim,
        "linhas": linhas,
        "kpi_cc_count": len(linhas),
        "kpi_total_mensal": total_geral_itens + total_geral_lics,
        "kpi_total_baixas": total_geral_baixas,
        "kpi_top_cc": linhas[0]['cc'].departamento if linhas else "-",
        "js_labels": chart_labels,
        "js_itens": chart_itens,
        "js_lics": chart_lics,
        "js_mix_values": [float(total_geral_itens), float(total_geral_lics)],
    }

    return render(request, "front/dashboards/cc_custos_dashboard.html", context)

@login_required
def cc_custos_export_pdf(request):
    """Exportação PDF - Custos por Centro de Custo"""
    data = _get_cc_custos_data(request)

    data["usuario"] = request.user
    data["data_geracao"] = timezone.now()

    template_path = "front/dashboards/cc_custos_pdf.html"
    template = get_template(template_path)
    html = template.render(data)

    response = HttpResponse(content_type="application/pdf")
    filename = f'relatorio_custos_cc_{timezone.now().strftime("%Y%m%d_%H%M")}.pdf'
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    pisa_status = pisa.CreatePDF(html, dest=response)
    if pisa_status.err:
        return HttpResponse("Erro ao gerar PDF")

    return response
def _calcular_custo_mensal_unitario_lote(lote):
    """
    Regra consolidada:
    - mensal: custo do lote já é mensal
    - trimestral: divide por 3
    - semestral: divide por 6
    - anual: divide por 12

    Depois:
    - mensal unitário = mensal do lote / quantidade
    - anual unitário = mensal unitário * 12
    """
    if not lote:
        return Decimal("0.00")

    qtd = Decimal(lote.quantidade_total or 0)
    if qtd <= 0:
        return Decimal("0.00")

    custo_ciclo = Decimal(lote.custo_ciclo or 0)
    periodicidade = str(lote.periodicidade or "").lower()

    if periodicidade == "mensal":
        custo_mensal_lote = custo_ciclo
    elif periodicidade == "trimestral":
        custo_mensal_lote = (custo_ciclo / Decimal("3")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    elif periodicidade == "semestral":
        custo_mensal_lote = (custo_ciclo / Decimal("6")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    elif periodicidade == "anual":
        custo_mensal_lote = (custo_ciclo / Decimal("12")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    else:
        custo_mensal_lote = custo_ciclo.quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

    custo_mensal_unitario = (custo_mensal_lote / qtd).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return custo_mensal_unitario


def _get_cc_custos_data(request):
    hoje = timezone.localdate()
    dt_ini = _parse_date(request.GET.get("inicio"), hoje.replace(day=1))
    dt_fim = _parse_date(request.GET.get("fim"), hoje)

    totals = {}

    def get_acc(cc_id):
        if not cc_id:
            return None

        if cc_id not in totals:
            totals[cc_id] = {
                "cc_obj": None,
                "qtd_usuarios": 0,
                "qtd_itens": 0,
                "qtd_licencas": 0,
                "custo_locacao": Decimal("0.00"),
                "custo_licencas": Decimal("0.00"),
                "custo_baixas": Decimal("0.00"),
            }
        return totals[cc_id]

    # ==========================================================
    # 1. CUSTO DE LOCAÇÃO (HARDWARE - RECORRENTE MENSAL)
    # ==========================================================
    locacoes = (
        Locacao.objects
        .select_related("equipamento__centro_custo")
        .filter(
            equipamento__status="ativo",
            valor_mensal__gt=0,
            equipamento__centro_custo__isnull=False
        )
    )

    for loc in locacoes:
        cc_id = loc.equipamento.centro_custo.id
        acc = get_acc(cc_id)
        if acc:
            acc["custo_locacao"] += Decimal(loc.valor_mensal or 0)

    # ==========================================================
    # 2. CUSTO DE LICENÇAS (SOFTWARE - RECORRENTE MENSAL)
    # ==========================================================
    movs_lic = (
        MovimentacaoLicenca.objects
        .select_related("licenca", "usuario__centro_custo", "centro_custo_destino", "lote")
        .filter(usuario__isnull=False)
        .order_by("licenca_id", "usuario_id", "created_at")
    )

    estado_atual_lic = {}
    for mov in movs_lic:
        estado_atual_lic[(mov.licenca_id, mov.usuario_id)] = mov

    for (_, _), mov in estado_atual_lic.items():
        if mov.tipo != TipoMovLicencaChoices.ATRIBUICAO:
            continue

        cc_id = None
        if mov.usuario and mov.usuario.centro_custo:
            cc_id = mov.usuario.centro_custo.id
        elif mov.centro_custo_destino:
            cc_id = mov.centro_custo_destino.id
        elif mov.licenca and mov.licenca.centro_custo:
            cc_id = mov.licenca.centro_custo.id

        acc = get_acc(cc_id)
        if not acc:
            continue

        if mov.lote:
            custo_mensal_unitario = _calcular_custo_mensal_unitario_lote(mov.lote)
        else:
            # fallback legado
            custo_mensal_unitario = Decimal(getattr(mov.licenca, "custo", 0) or 0).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )

        acc["custo_licencas"] += custo_mensal_unitario
        acc["qtd_licencas"] += 1

    # ==========================================================
    # 3. CUSTO DE BAIXAS (PONTUAL NO PERÍODO)
    # ==========================================================
    baixas = (
        MovimentacaoItem.objects
        .filter(
            tipo_movimentacao=TipoMovimentacaoChoices.BAIXA,
            created_at__date__gte=dt_ini,
            created_at__date__lte=dt_fim,
        )
        .select_related("item__centro_custo", "centro_custo_origem")
    )

    for baixa in baixas:
        cc_id = None
        if baixa.centro_custo_origem:
            cc_id = baixa.centro_custo_origem.id
        elif baixa.item and baixa.item.centro_custo:
            cc_id = baixa.item.centro_custo.id

        acc = get_acc(cc_id)
        if not acc:
            continue

        custo_baixa = baixa.custo
        if custo_baixa is None:
            custo_baixa = Decimal(baixa.item.valor or 0) * Decimal(baixa.quantidade or 1)

        acc["custo_baixas"] += Decimal(custo_baixa or 0)

    # ==========================================================
    # 4. DADOS DE CONTEXTO
    # ==========================================================
    cc_ids = list(totals.keys())

    ccs_objs = CentroCusto.objects.filter(id__in=cc_ids)
    for cc in ccs_objs:
        if cc.id in totals:
            totals[cc.id]["cc_obj"] = cc

    users_agg = (
        Usuario.objects
        .filter(centro_custo_id__in=cc_ids, status="ativo")
        .values("centro_custo_id")
        .annotate(n=Count("id"))
    )
    for row in users_agg:
        cc_id = row["centro_custo_id"]
        if cc_id in totals:
            totals[cc_id]["qtd_usuarios"] = row["n"]

    itens_agg = (
        Item.objects
        .filter(centro_custo_id__in=cc_ids, status="ativo")
        .values("centro_custo_id")
        .annotate(n=Count("id"))
    )
    for row in itens_agg:
        cc_id = row["centro_custo_id"]
        if cc_id in totals:
            totals[cc_id]["qtd_itens"] = row["n"]

    # ==========================================================
    # 5. MONTAGEM FINAL
    # ==========================================================
    linhas = []

    total_geral_itens = Decimal("0.00")
    total_geral_lics = Decimal("0.00")
    total_geral_baixas = Decimal("0.00")

    for cc_id, dados in totals.items():
        if not dados["cc_obj"]:
            continue

        custo_itens = dados["custo_locacao"].quantize(Decimal("0.01"))
        custo_licencas = dados["custo_licencas"].quantize(Decimal("0.01"))
        custo_baixas = dados["custo_baixas"].quantize(Decimal("0.01"))

        total_mensal = (custo_itens + custo_licencas).quantize(Decimal("0.01"))
        total_impacto = (total_mensal + custo_baixas).quantize(Decimal("0.01"))

        total_geral_itens += custo_itens
        total_geral_lics += custo_licencas
        total_geral_baixas += custo_baixas

        linhas.append({
            "cc": dados["cc_obj"],
            "usuarios": dados["qtd_usuarios"],
            "itens": dados["qtd_itens"],
            "licencas": dados["qtd_licencas"],
            "custo_itens": custo_itens,
            "custo_licencas": custo_licencas,
            "baixas": custo_baixas,
            "total_mensal": total_mensal,
            "total_impacto": total_impacto,
        })

    linhas.sort(key=lambda x: x["total_impacto"], reverse=True)

    chart_labels = [f"{l['cc'].numero}" for l in linhas[:10]]
    chart_itens = [float(l["custo_itens"]) for l in linhas[:10]]
    chart_lics = [float(l["custo_licencas"]) for l in linhas[:10]]

    return {
        "dt_ini": dt_ini,
        "dt_fim": dt_fim,
        "linhas": linhas,
        "kpi_cc_count": len(linhas),
        "kpi_total_mensal": (total_geral_itens + total_geral_lics).quantize(Decimal("0.01")),
        "kpi_total_baixas": total_geral_baixas.quantize(Decimal("0.01")),
        "kpi_top_cc": linhas[0]["cc"].departamento if linhas else "-",
        "js_labels": chart_labels,
        "js_itens": chart_itens,
        "js_lics": chart_lics,
        "js_mix_values": [
            float(total_geral_itens.quantize(Decimal("0.01"))),
            float(total_geral_lics.quantize(Decimal("0.01"))),
        ],
    }

