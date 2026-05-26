from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, OuterRef, Q, Subquery
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..forms import (
    ChecklistModeloForm,
    ChecklistPerguntaForm,
    PreventivaStartForm,
)
from ..models import (
    CheckListModelo,
    CheckListPergunta,
    Item,
    Localidade,
    Preventiva,
    PreventivaExecucao,
    PreventivaResposta,
    SimNaoChoices,
    StatusItemChoices,
)


# =========================================================
# HELPERS - PROGRAMAÇÃO DE PREVENTIVA
# =========================================================

def _to_int(value, default=0) -> int:
    """Converte valores de intervalo com segurança."""
    try:
        if value in (None, ""):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _intervalo_preventiva(item=None, checklist=None) -> tuple[int, str]:
    """
    Define o intervalo oficial da programação.

    Regra adotada para não alterar radicalmente a estrutura atual:
    1. Primeiro usa Item.data_limite_preventiva, pois é específico do ativo.
    2. Se o item não tiver intervalo, usa CheckListModelo.intervalo_dias.
    3. Se nenhum existir, retorna 0 e deixa a preventiva sem programação calculada.
    """
    item_intervalo = _to_int(getattr(item, "data_limite_preventiva", None), 0)
    if item_intervalo > 0:
        return item_intervalo, "Cadastro do equipamento"

    checklist_intervalo = _to_int(getattr(checklist, "intervalo_dias", None), 0)
    if checklist_intervalo > 0:
        return checklist_intervalo, "Modelo de checklist"

    return 0, "Sem intervalo configurado"


def _calc_proxima(data_ultima=None, data_proxima=None, intervalo_dias: int = 0) -> date | None:
    """
    Calcula a próxima execução.

    - Se já houve execução e existe intervalo, calcula data_ultima + intervalo.
    - Se nunca houve execução, usa data_proxima já gravada.
    - Se nada estiver configurado, retorna None.
    """
    if data_ultima and intervalo_dias > 0:
        return data_ultima + timedelta(days=intervalo_dias)
    return data_proxima


def _classificar_programacao(proxima: date | None, hoje: date | None = None, janela_alerta: int = 7) -> dict:
    """Retorna status visual, label, classe CSS e dias restantes."""
    hoje = hoje or timezone.localdate()

    if not proxima:
        return {
            "status": "indefinido",
            "label": "Sem data",
            "css": "st-indef",
            "dias": None,
            "prioridade": 99,
        }

    dias = (proxima - hoje).days

    if dias < 0:
        return {
            "status": "vencida",
            "label": "Vencida",
            "css": "st-vencida",
            "dias": dias,
            "prioridade": 1,
        }

    if dias <= janela_alerta:
        return {
            "status": "atencao",
            "label": "Atenção",
            "css": "st-atencao",
            "dias": dias,
            "prioridade": 2,
        }

    return {
        "status": "ok",
        "label": "Em dia",
        "css": "st-ok",
        "dias": dias,
        "prioridade": 3,
    }


def _normalizar_decimal(valor: str) -> str:
    """Valida números aceitando vírgula decimal."""
    valor = (valor or "").strip().replace(".", "").replace(",", ".")
    try:
        return str(Decimal(valor))
    except (InvalidOperation, ValueError):
        raise ValueError("Número inválido")


def _preparar_opcoes_perguntas(perguntas):
    """Anexa lista de opções calculada para perguntas de escolha."""
    for pergunta in perguntas:
        pergunta.opcoes_list = []
        if getattr(pergunta, "opcoes", None):
            pergunta.opcoes_list = [
                opcao.strip()
                for opcao in str(pergunta.opcoes).split(",")
                if opcao.strip()
            ]
    return perguntas


def _aplicar_status_preventiva(preventiva: Preventiva, hoje: date | None = None) -> Preventiva:
    """Anexa propriedades calculadas em memória para uso nos templates."""
    hoje = hoje or timezone.localdate()
    intervalo, origem = _intervalo_preventiva(preventiva.equipamento, preventiva.checklist_modelo)
    proxima_auto = _calc_proxima(preventiva.data_ultima, preventiva.data_proxima, intervalo)
    # data_agendamento sobrepõe o cálculo automático; marca a diferença para o template
    agendamento = getattr(preventiva, "data_agendamento", None)
    proxima = agendamento if agendamento else proxima_auto

    preventiva.intervalo_dias_calc = intervalo
    preventiva.intervalo_origem_calc = origem
    preventiva.proxima_calc = proxima
    preventiva.tem_agendamento = bool(agendamento)

    if getattr(preventiva, "pausada", False):
        preventiva.status_visual = "pausada"
        preventiva.status_label = "Pausada"
        preventiva.status_css = "st-pausada"
        preventiva.dias_restantes = getattr(preventiva, "dias_restantes_pausa", None)
        preventiva.prioridade_visual = 4
    else:
        status = _classificar_programacao(proxima, hoje)
        preventiva.status_visual = status["status"]
        preventiva.status_label = status["label"]
        preventiva.status_css = status["css"]
        preventiva.dias_restantes = status["dias"]
        preventiva.prioridade_visual = status["prioridade"]

    return preventiva


# =========================================================
# CHECKLIST - LISTAGEM
# =========================================================
@login_required
def checklist_list(request):
    q = (request.GET.get("q") or "").strip()

    qs = (
        CheckListModelo.objects
        .select_related("subtipo", "subtipo__categoria")
        .annotate(perguntas_count=Count("perguntas"))
        .order_by("-created_at", "nome")
    )

    if q:
        qs = qs.filter(
            Q(nome__icontains=q)
            | Q(subtipo__nome__icontains=q)
            | Q(subtipo__categoria__nome__icontains=q)
        )

    paginator = Paginator(qs, 12)
    page_obj = paginator.get_page(request.GET.get("page"))

    context = {
        "page_obj": page_obj,
        "q": q,
        "total": qs.count(),
    }
    return render(request, "front/preventivas/checklist_list.html", context)


# =========================================================
# CHECKLIST - CRIAR / EDITAR
# =========================================================
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

    perguntas = (
        CheckListPergunta.objects
        .filter(checklist_modelo=instance)
        .order_by("ordem", "id")
        if instance else []
    )

    return render(
        request,
        "front/preventivas/checklist_form.html",
        {"form": form, "perguntas": perguntas},
    )


@login_required
@require_POST
def checklist_delete(request, pk):
    obj = get_object_or_404(CheckListModelo, pk=pk)
    obj.delete()
    messages.success(request, "Checklist removido com sucesso.")
    return redirect("checklist_list")


# =========================================================
# PERGUNTA - CRIAR / EDITAR
# =========================================================
@login_required
def pergunta_form(request, checklist_pk, pk=None):
    checklist = get_object_or_404(CheckListModelo, pk=checklist_pk)
    instance = (
        get_object_or_404(CheckListPergunta, pk=pk, checklist_modelo=checklist)
        if pk else None
    )

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
@require_POST
def pergunta_delete(request, checklist_pk, pk):
    checklist = get_object_or_404(CheckListModelo, pk=checklist_pk)
    pergunta = get_object_or_404(CheckListPergunta, pk=pk, checklist_modelo=checklist)
    pergunta.delete()
    messages.success(request, "Pergunta removida com sucesso.")
    return redirect("checklist_form", pk=checklist.pk)


# =========================================================
# PROGRAMAÇÃO AUTOMÁTICA DE PREVENTIVAS
# =========================================================
@login_required
@require_POST
def preventiva_sincronizar_programacao(request):
    """
    Cria programações pendentes para itens marcados com precisa_preventiva='sim'.

    Sem alteração radical de estrutura:
    - Não cria tabela nova.
    - Não muda models.
    - Usa Preventiva existente como programação ativa.
    - Tenta usar checklist do mesmo subtipo; se não houver, usa checklist genérico sem subtipo.
    """
    hoje = timezone.localdate()

    itens = (
        Item.objects
        .filter(precisa_preventiva=SimNaoChoices.SIM)
        .select_related("subtipo", "localidade", "centro_custo")
        .order_by("nome")
    )

    checklists_ativos = (
        CheckListModelo.objects
        .filter(ativo=SimNaoChoices.SIM)
        .select_related("subtipo")
        .order_by("subtipo__nome", "nome")
    )

    _STATUS_PAUSANTES = {
        StatusItemChoices.PAUSADO,
        StatusItemChoices.BACKUP,
        StatusItemChoices.MANUTENCAO,
        StatusItemChoices.DEFEITO,
    }

    criadas = 0
    existentes = 0
    sem_checklist = 0
    sem_intervalo = 0
    pausadas_sync = 0
    retomadas_sync = 0

    with transaction.atomic():
        for item in itens:
            checklist = None

            if item.subtipo_id:
                checklist = checklists_ativos.filter(subtipo=item.subtipo).first()

            if checklist is None:
                checklist = checklists_ativos.filter(subtipo__isnull=True).first()

            if checklist is None:
                sem_checklist += 1
                continue

            intervalo, _origem = _intervalo_preventiva(item, checklist)
            if intervalo <= 0:
                sem_intervalo += 1
                continue

            prev, created = Preventiva.objects.get_or_create(
                equipamento=item,
                checklist_modelo=checklist,
                defaults={
                    "data_ultima": None,
                    "data_proxima": hoje,
                    "dentro_do_prazo": True,
                    "criado_por": request.user,
                    "atualizado_por": request.user,
                },
            )

            if created:
                criadas += 1
            else:
                existentes += 1
                if not prev.data_proxima and not prev.data_ultima:
                    prev.data_proxima = hoje
                    prev.atualizado_por = request.user
                    prev.save(update_fields=["data_proxima", "atualizado_por", "updated_at"])

            # Sincroniza estado de pausa conforme status atual do equipamento.
            item_pausante = item.status in _STATUS_PAUSANTES
            if item_pausante and not prev.pausada:
                prev.pausar()
                pausadas_sync += 1
            elif not item_pausante and prev.pausada:
                prev.retomar()
                retomadas_sync += 1

    messages.success(
        request,
        (
            f"Programação sincronizada. Criadas: {criadas}. "
            f"Já existentes: {existentes}. Sem checklist: {sem_checklist}. "
            f"Sem intervalo: {sem_intervalo}. "
            f"Pausadas: {pausadas_sync}. Retomadas: {retomadas_sync}."
        ),
    )
    return redirect("preventiva_list")


# =========================================================
# PREVENTIVAS - LISTAGEM / PROGRAMAÇÃO
# =========================================================
@login_required
def preventiva_list(request):
    q = (request.GET.get("q") or "").strip()
    status_filter = (request.GET.get("status") or "").strip()
    localidade_filter = (request.GET.get("localidade") or "").strip()
    checklist_filter = (request.GET.get("checklist") or "").strip()
    janela_alerta = _to_int(request.GET.get("janela"), 7)
    janela_alerta = janela_alerta if janela_alerta in (7, 15, 30, 60) else 7

    ultima_execucao_qs = (
        PreventivaExecucao.objects
        .filter(preventiva=OuterRef("pk"))
        .order_by("-data_execucao", "-id")
    )

    qs = (
        Preventiva.objects
        .select_related(
            "equipamento",
            "equipamento__localidade",
            "equipamento__centro_custo",
            "equipamento__subtipo",
            "checklist_modelo",
            "atualizado_por",
        )
        .annotate(
            ultimo_executor_username=Subquery(ultima_execucao_qs.values("criado_por__username")[:1]),
            ultimo_executor_nome=Subquery(ultima_execucao_qs.values("criado_por__first_name")[:1]),
            ultima_execucao_data=Subquery(ultima_execucao_qs.values("data_execucao")[:1]),
        )
        .order_by("data_proxima", "equipamento__nome", "id")
    )

    if q:
        qs = qs.filter(
            Q(equipamento__nome__icontains=q)
            | Q(equipamento__numero_serie__icontains=q)
            | Q(equipamento__marca__icontains=q)
            | Q(equipamento__modelo__icontains=q)
            | Q(equipamento__localidade__local__icontains=q)
            | Q(equipamento__centro_custo__departamento__icontains=q)
            | Q(checklist_modelo__nome__icontains=q)
        )

    if localidade_filter:
        qs = qs.filter(equipamento__localidade_id=localidade_filter)

    if checklist_filter:
        qs = qs.filter(checklist_modelo_id=checklist_filter)

    hoje = timezone.localdate()
    processadas = []

    kpi = {
        "total": 0,
        "vencidas": 0,
        "proximas": 0,
        "proximas_30": 0,
        "em_dia": 0,
        "sem_data": 0,
    }

    for preventiva in qs:
        _aplicar_status_preventiva(preventiva, hoje)

        # Reclassifica atenção conforme a janela escolhida pelo usuário (ignora pausadas).
        if not getattr(preventiva, "pausada", False) and preventiva.proxima_calc:
            status = _classificar_programacao(preventiva.proxima_calc, hoje, janela_alerta)
            preventiva.status_visual = status["status"]
            preventiva.status_label = status["label"]
            preventiva.status_css = status["css"]
            preventiva.dias_restantes = status["dias"]
            preventiva.prioridade_visual = status["prioridade"]

        kpi["total"] += 1

        if preventiva.status_visual == "pausada":
            pass  # não conta em nenhuma categoria de prazo
        elif preventiva.status_visual == "vencida":
            kpi["vencidas"] += 1
        elif preventiva.status_visual == "atencao":
            kpi["proximas"] += 1
        elif preventiva.status_visual == "ok":
            kpi["em_dia"] += 1
        else:
            kpi["sem_data"] += 1

        if not getattr(preventiva, "pausada", False) and preventiva.proxima_calc and 0 <= (preventiva.proxima_calc - hoje).days <= 30:
            kpi["proximas_30"] += 1

        processadas.append(preventiva)

    if status_filter:
        if status_filter == "proxima":
            processadas = [p for p in processadas if p.status_visual == "atencao"]
        elif status_filter in ("vencida", "ok", "indefinido", "pausada"):
            processadas = [p for p in processadas if p.status_visual == status_filter]

    processadas.sort(key=lambda p: (p.prioridade_visual, p.proxima_calc or date.max, p.equipamento.nome))

    paginator = Paginator(processadas, 20)
    page_obj = paginator.get_page(request.GET.get("page"))

    context = {
        "preventivas": page_obj,
        "kpi": kpi,
        "today": hoje,
        "filter_q": q,
        "filter_status": status_filter,
        "filter_localidade": localidade_filter,
        "filter_checklist": checklist_filter,
        "janela_alerta": janela_alerta,
        "localidades": Localidade.objects.order_by("local"),
        "checklists": CheckListModelo.objects.order_by("nome"),
    }

    if request.GET.get("print") == "true":
        return render(request, "front/preventivas/preventiva_list_print.html", context)

    return render(request, "front/preventivas/preventiva_list.html", context)


# =========================================================
# PREVENTIVA - START
# =========================================================
@login_required
def preventiva_start(request, item_id=None):
    item_instance = None
    if item_id:
        item_instance = get_object_or_404(Item.objects.select_related("subtipo"), pk=item_id)

    if request.method == "POST":
        form = PreventivaStartForm(request.POST, item_instance=item_instance)
        if form.is_valid():
            item = form.cleaned_data["item"]
            checklist = form.cleaned_data["checklist_modelo"]
            hoje = timezone.localdate()

            intervalo, origem = _intervalo_preventiva(item, checklist)
            data_inicial = hoje

            if intervalo <= 0:
                messages.warning(
                    request,
                    "Preventiva criada sem intervalo definido. Configure a periodicidade no equipamento ou no checklist.",
                )

            with transaction.atomic():
                preventiva, created = Preventiva.objects.get_or_create(
                    equipamento=item,
                    checklist_modelo=checklist,
                    defaults={
                        "data_ultima": None,
                        "data_proxima": data_inicial,
                        "dentro_do_prazo": True,
                        "criado_por": request.user,
                        "atualizado_por": request.user,
                    },
                )

                if not created:
                    preventiva.atualizado_por = request.user
                    if not preventiva.data_proxima and not preventiva.data_ultima:
                        preventiva.data_proxima = data_inicial
                    preventiva.save(update_fields=["data_proxima", "atualizado_por", "updated_at"])

            messages.success(
                request,
                f"Preventiva programada para {item.nome}. Intervalo: {intervalo or 'não definido'} dias ({origem}).",
            )
            return redirect("preventiva_exec", pk=preventiva.pk)
    else:
        form = PreventivaStartForm(item_instance=item_instance)

    return render(request, "front/preventivas/preventiva_start.html", {"form": form})


@login_required
def preventiva_start_item(request, item_id):
    return preventiva_start(request, item_id=item_id)


# =========================================================
# PREVENTIVA - DETALHE
# =========================================================
@login_required
def preventiva_detail(request, pk):
    preventiva = get_object_or_404(
        Preventiva.objects.select_related(
            "equipamento",
            "equipamento__localidade",
            "equipamento__centro_custo",
            "equipamento__subtipo",
            "checklist_modelo",
        ),
        pk=pk,
    )

    dt_ini = None
    dt_fim = None

    ini = (request.GET.get("inicio") or "").strip()
    fim = (request.GET.get("fim") or "").strip()

    if ini:
        try:
            dt_ini = datetime.strptime(ini, "%Y-%m-%d").date()
        except ValueError:
            messages.warning(request, "Data inicial inválida. Filtro ignorado.")

    if fim:
        try:
            dt_fim = datetime.strptime(fim, "%Y-%m-%d").date()
        except ValueError:
            messages.warning(request, "Data final inválida. Filtro ignorado.")

    exec_qs = (
        preventiva.execucoes
        .select_related("criado_por")
        .prefetch_related("respostas", "respostas__pergunta")
        .order_by("-data_execucao", "-id")
    )

    if dt_ini:
        exec_qs = exec_qs.filter(data_execucao__gte=dt_ini)
    if dt_fim:
        exec_qs = exec_qs.filter(data_execucao__lte=dt_fim)

    perguntas = list(
        CheckListPergunta.objects
        .filter(checklist_modelo=preventiva.checklist_modelo)
        .order_by("ordem", "id")
    )

    execucoes_data = []
    total_nao_conforme = 0

    for execucao in exec_qs:
        resp_map = {resposta.pergunta_id: resposta for resposta in execucao.respostas.all()}
        linhas = []

        for pergunta in perguntas:
            resposta_obj = resp_map.get(pergunta.id)
            resposta = resposta_obj.resposta if resposta_obj else "-"

            if str(resposta).strip().lower() in {"nao", "não", "n"}:
                total_nao_conforme += 1

            linhas.append({
                "texto": pergunta.texto_pergunta,
                "tipo": pergunta.tipo_resposta,
                "resposta": resposta,
                "respondido_em": resposta_obj.created_at if resposta_obj else None,
            })

        execucoes_data.append({"obj": execucao, "linhas": linhas})

    hoje = timezone.localdate()
    _aplicar_status_preventiva(preventiva, hoje)

    context = {
        "preventiva": preventiva,
        "equipamento": preventiva.equipamento,
        "execucoes": execucoes_data,
        "today": hoje,
        "dt_ini": dt_ini,
        "dt_fim": dt_fim,
        "proxima_calc": preventiva.proxima_calc,
        "status_prazo": preventiva.status_visual,
        "status_css": preventiva.status_css,
        "dias_restantes": preventiva.dias_restantes,
        "intervalo_info": f"{preventiva.intervalo_dias_calc} dias ({preventiva.intervalo_origem_calc})",
        "total_execucoes": len(execucoes_data),
        "total_nao_conforme": total_nao_conforme,
    }

    if request.GET.get("print") == "true":
        return render(request, "front/preventivas/preventiva_print.html", context)

    return render(request, "front/preventivas/preventiva_detail.html", context)


# =========================================================
# PREVENTIVA - EXECUÇÃO
# =========================================================
@login_required
def preventiva_exec(request, pk):
    preventiva = get_object_or_404(
        Preventiva.objects.select_related(
            "equipamento",
            "equipamento__localidade",
            "equipamento__centro_custo",
            "checklist_modelo",
        ),
        pk=pk,
    )

    perguntas = list(
        CheckListPergunta.objects
        .filter(checklist_modelo=preventiva.checklist_modelo)
        .order_by("ordem", "id")
    )
    _preparar_opcoes_perguntas(perguntas)

    if request.method == "POST":
        erros = []
        respostas_bulk = []

        if not perguntas:
            erros.append("Este checklist não possui perguntas cadastradas.")

        for pergunta in perguntas:
            field_name = f"r_{pergunta.id}"
            raw_val = (request.POST.get(field_name) or "").strip()
            tipo = str(pergunta.tipo_resposta or "").lower()

            if pergunta.obrigatorio == SimNaoChoices.SIM and not raw_val:
                erros.append(f"A pergunta '{pergunta.texto_pergunta}' é obrigatória.")
                continue

            if not raw_val:
                continue

            if tipo in {"numero", "inteiro", "decimal"}:
                try:
                    raw_val = _normalizar_decimal(raw_val)
                except ValueError:
                    erros.append(f"Valor numérico inválido na pergunta '{pergunta.texto_pergunta}'.")
                    continue

            if tipo in {"booleano", "sim_nao", "sn"} and raw_val not in {"sim", "nao"}:
                erros.append(f"Resposta inválida na pergunta '{pergunta.texto_pergunta}'.")
                continue

            if tipo in {"escolha", "opcao", "choice"}:
                opcoes_validas = getattr(pergunta, "opcoes_list", [])
                if opcoes_validas and raw_val not in opcoes_validas:
                    erros.append(f"Opção inválida na pergunta '{pergunta.texto_pergunta}'.")
                    continue

            respostas_bulk.append(
                PreventivaResposta(
                    preventiva=preventiva,
                    pergunta=pergunta,
                    resposta=raw_val,
                    criado_por=request.user,
                    atualizado_por=request.user,
                )
            )

        if erros:
            for erro in erros:
                messages.error(request, erro)
            return render(
                request,
                "front/preventivas/preventiva_exec.html",
                {"preventiva": preventiva, "perguntas": perguntas},
            )

        with transaction.atomic():
            hoje = timezone.localdate()
            intervalo, _origem = _intervalo_preventiva(preventiva.equipamento, preventiva.checklist_modelo)
            proxima = hoje + timedelta(days=intervalo) if intervalo > 0 else None

            execucao = PreventivaExecucao.objects.create(
                preventiva=preventiva,
                data_execucao=hoje,
                observacao=(request.POST.get("observacao") or "").strip(),
                foto_antes=request.FILES.get("foto_antes"),
                foto_depois=request.FILES.get("foto_depois"),
                criado_por=request.user,
                atualizado_por=request.user,
            )

            for resposta in respostas_bulk:
                resposta.execucao = execucao

            if respostas_bulk:
                PreventivaResposta.objects.bulk_create(respostas_bulk)

            preventiva.data_ultima = hoje
            preventiva.data_proxima = proxima
            preventiva.data_agendamento = None  # agendamento consumido pela execução
            preventiva.dentro_do_prazo = True if proxima is None else hoje <= proxima
            preventiva.atualizado_por = request.user

            foto_antes = request.FILES.get("foto_antes")
            foto_depois = request.FILES.get("foto_depois")

            update_fields = [
                "data_ultima",
                "data_proxima",
                "data_agendamento",
                "dentro_do_prazo",
                "atualizado_por",
                "updated_at",
            ]

            if foto_antes:
                preventiva.foto_antes = foto_antes
                update_fields.append("foto_antes")

            if foto_depois:
                preventiva.foto_depois = foto_depois
                update_fields.append("foto_depois")

            preventiva.save(update_fields=update_fields)

        messages.success(request, "Execução registrada e próxima preventiva reagendada com sucesso.")
        return redirect("preventiva_detail", pk=preventiva.pk)

    hoje = timezone.localdate()
    _aplicar_status_preventiva(preventiva, hoje)

    return render(
        request,
        "front/preventivas/preventiva_exec.html",
        {"preventiva": preventiva, "perguntas": perguntas, "today": hoje},
    )


@login_required
def preventiva_agendadas(request):
    """
    Lista todas as preventivas com data_agendamento definida,
    agrupadas e ordenadas por proximidade da data agendada.
    """
    hoje = timezone.localdate()

    base_qs = Preventiva.objects.filter(data_agendamento__isnull=False)

    kpi_total    = base_qs.count()
    kpi_vencidas = base_qs.filter(data_agendamento__lt=hoje).count()
    kpi_hoje     = base_qs.filter(data_agendamento=hoje).count()
    kpi_semana   = base_qs.filter(
        data_agendamento__gte=hoje,
        data_agendamento__lte=hoje + timedelta(days=7),
    ).count()

    q                = (request.GET.get("q") or "").strip()
    localidade_filter = (request.GET.get("localidade") or "").strip()
    periodo_filter   = (request.GET.get("periodo") or "").strip()

    ultima_exec_qs = (
        PreventivaExecucao.objects
        .filter(preventiva=OuterRef("pk"))
        .order_by("-data_execucao", "-id")
    )

    qs = base_qs.select_related(
        "equipamento",
        "equipamento__localidade",
        "equipamento__centro_custo",
        "equipamento__subtipo",
        "checklist_modelo",
        "atualizado_por",
    ).annotate(
        ultimo_executor_username=Subquery(ultima_exec_qs.values("criado_por__username")[:1]),
        ultimo_executor_nome=Subquery(ultima_exec_qs.values("criado_por__first_name")[:1]),
        ultima_execucao_data=Subquery(ultima_exec_qs.values("data_execucao")[:1]),
    )

    if q:
        qs = qs.filter(
            Q(equipamento__nome__icontains=q)
            | Q(equipamento__numero_serie__icontains=q)
            | Q(equipamento__localidade__local__icontains=q)
            | Q(checklist_modelo__nome__icontains=q)
        )
    if localidade_filter:
        qs = qs.filter(equipamento__localidade_id=localidade_filter)
    if periodo_filter == "vencidas":
        qs = qs.filter(data_agendamento__lt=hoje)
    elif periodo_filter == "hoje":
        qs = qs.filter(data_agendamento=hoje)
    elif periodo_filter == "semana":
        qs = qs.filter(data_agendamento__lte=hoje + timedelta(days=7))

    qs = qs.order_by("data_agendamento", "equipamento__nome")

    preventivas = list(qs)
    for p in preventivas:
        _aplicar_status_preventiva(p, hoje)
        p.dias_agendamento = (p.data_agendamento - hoje).days

    paginator = Paginator(preventivas, 25)
    page_obj  = paginator.get_page(request.GET.get("page"))

    context = {
        "page_obj": page_obj,
        "kpi": {
            "total":    kpi_total,
            "vencidas": kpi_vencidas,
            "hoje":     kpi_hoje,
            "semana":   kpi_semana,
        },
        "today":            hoje,
        "filter_q":         q,
        "filter_localidade": localidade_filter,
        "filter_periodo":   periodo_filter,
        "localidades":      Localidade.objects.order_by("local"),
        "total":            len(preventivas),
    }
    return render(request, "front/preventivas/preventiva_agendadas.html", context)


@login_required
def preventiva_plano(request):
    """
    Plano de Preventivas — agendamento em lote por equipamento.

    GET : lista todos os equipamentos com precisa_preventiva='sim',
          exibindo o estado da preventiva associada (quando existir).
    POST acao='agendar' : cria Preventiva via get_or_create e define data_agendamento.
    POST acao='limpar'  : limpa data_agendamento das preventivas dos itens selecionados.
    """
    from django.utils.dateparse import parse_date
    from types import SimpleNamespace

    q = (request.GET.get("q") or "").strip()
    status_filter = (request.GET.get("status") or "").strip()
    localidade_filter = (request.GET.get("localidade") or "").strip()
    checklist_filter = (request.GET.get("checklist") or "").strip()

    if request.method == "POST":
        item_ids_raw = request.POST.getlist("item_ids")
        data_str = (request.POST.get("data_agendamento") or "").strip()
        acao = (request.POST.get("acao") or "").strip()

        if acao not in ("agendar", "limpar"):
            messages.error(request, "Ação inválida.")
            return redirect("preventiva_plano")

        item_ids = [int(i) for i in item_ids_raw if str(i).isdigit()]
        if not item_ids:
            messages.warning(request, "Nenhum equipamento selecionado.")
            return redirect("preventiva_plano")

        if acao == "limpar":
            count = Preventiva.objects.filter(
                equipamento_id__in=item_ids
            ).update(data_agendamento=None)
            messages.success(request, f"Agendamento removido de {count} preventiva(s).")
            return redirect("preventiva_plano")

        # acao == "agendar"
        if not data_str:
            messages.error(request, "Informe uma data para agendar os equipamentos selecionados.")
            return redirect("preventiva_plano")
        data = parse_date(data_str)
        if not data:
            messages.error(request, "Data inválida.")
            return redirect("preventiva_plano")

        itens = Item.objects.filter(pk__in=item_ids).select_related("subtipo")
        checklists_ativos = (
            CheckListModelo.objects
            .filter(ativo=SimNaoChoices.SIM)
            .select_related("subtipo")
            .order_by("subtipo__nome", "nome")
        )
        hoje = timezone.localdate()
        count = 0
        sem_checklist = 0

        with transaction.atomic():
            for item in itens:
                checklist = None
                if item.subtipo_id:
                    checklist = checklists_ativos.filter(subtipo=item.subtipo).first()
                if checklist is None:
                    checklist = checklists_ativos.filter(subtipo__isnull=True).first()
                if checklist is None:
                    sem_checklist += 1
                    continue

                preventiva, created = Preventiva.objects.get_or_create(
                    equipamento=item,
                    checklist_modelo=checklist,
                    defaults={
                        "data_ultima": None,
                        "dentro_do_prazo": True,
                        "criado_por": request.user,
                        "atualizado_por": request.user,
                    },
                )
                preventiva.data_agendamento = data
                preventiva.atualizado_por = request.user
                # Para preventivas recém-criadas ou sem data_proxima, calcula o prazo
                if not preventiva.data_proxima:
                    preventiva.recomputar_prazo()
                preventiva.save(update_fields=["data_agendamento", "data_proxima", "dentro_do_prazo", "atualizado_por", "updated_at"])
                count += 1

        if sem_checklist:
            messages.warning(
                request,
                f"{sem_checklist} equipamento(s) sem checklist configurado foram ignorados.",
            )
        if count:
            messages.success(
                request,
                f"{count} equipamento(s) agendado(s) para {data.strftime('%d/%m/%Y')}.",
            )
        return redirect("preventiva_plano")

    # ── GET — listar equipamentos com precisa_preventiva='sim' ────────────────
    items_qs = (
        Item.objects
        .filter(precisa_preventiva=SimNaoChoices.SIM)
        .select_related("localidade", "subtipo", "centro_custo")
        .order_by("nome")
    )

    if q:
        items_qs = items_qs.filter(
            Q(nome__icontains=q)
            | Q(numero_serie__icontains=q)
            | Q(marca__icontains=q)
            | Q(modelo__icontains=q)
            | Q(localidade__local__icontains=q)
            | Q(centro_custo__departamento__icontains=q)
        )

    if localidade_filter:
        items_qs = items_qs.filter(localidade_id=localidade_filter)

    if checklist_filter:
        ids_com_checklist = set(
            Preventiva.objects.filter(
                equipamento__in=items_qs,
                checklist_modelo_id=checklist_filter,
            ).values_list("equipamento_id", flat=True)
        )
        items_qs = items_qs.filter(pk__in=ids_com_checklist)

    # Pré-carrega a preventiva mais urgente por item (evita N+1)
    prev_map = {}
    for p in (
        Preventiva.objects
        .filter(equipamento__in=items_qs)
        .select_related("checklist_modelo", "equipamento")
        .order_by("equipamento_id", "data_proxima")
    ):
        if p.equipamento_id not in prev_map:
            prev_map[p.equipamento_id] = p

    hoje = timezone.localdate()
    rows = []

    for item in items_qs:
        p = prev_map.get(item.pk)

        row = SimpleNamespace(
            pk=item.pk,
            equipamento=item,
            checklist_modelo=p.checklist_modelo if p else None,
            data_ultima=p.data_ultima if p else None,
            data_proxima=p.data_proxima if p else None,
            data_agendamento=p.data_agendamento if p else None,
            pausada=getattr(p, "pausada", False) if p else False,
            status_visual="indefinido",
            status_label="Sem preventiva",
            status_css="st-indef",
            dias_restantes=None,
            prioridade_visual=98,
            intervalo_dias_calc=0,
            intervalo_origem_calc="Sem intervalo configurado",
            proxima_calc=None,
            tem_agendamento=False,
        )

        if p:
            _aplicar_status_preventiva(p, hoje)
            row.status_visual = p.status_visual
            row.status_label = p.status_label
            row.status_css = p.status_css
            row.dias_restantes = p.dias_restantes
            row.prioridade_visual = p.prioridade_visual
            row.intervalo_dias_calc = p.intervalo_dias_calc
            row.intervalo_origem_calc = p.intervalo_origem_calc
            row.proxima_calc = p.proxima_calc
            row.tem_agendamento = p.tem_agendamento
        else:
            intervalo, origem = _intervalo_preventiva(item, None)
            row.intervalo_dias_calc = intervalo
            row.intervalo_origem_calc = origem

        rows.append(row)

    if status_filter:
        if status_filter == "proxima":
            rows = [r for r in rows if r.status_visual == "atencao"]
        elif status_filter == "agendada":
            rows = [r for r in rows if r.tem_agendamento]
        elif status_filter in ("vencida", "ok", "indefinido", "pausada"):
            rows = [r for r in rows if r.status_visual == status_filter]

    rows.sort(
        key=lambda r: (r.prioridade_visual, r.proxima_calc or date.max, r.equipamento.nome)
    )

    paginator = Paginator(rows, 25)
    page_obj = paginator.get_page(request.GET.get("page"))

    context = {
        "page_obj": page_obj,
        "today": hoje,
        "filter_q": q,
        "filter_status": status_filter,
        "filter_localidade": localidade_filter,
        "filter_checklist": checklist_filter,
        "localidades": Localidade.objects.order_by("local"),
        "checklists": CheckListModelo.objects.order_by("nome"),
        "total": len(rows),
    }
    return render(request, "front/preventivas/preventiva_plano.html", context)


@login_required
def preventiva_agendar(request, pk):
    """AJAX — define ou limpa data_agendamento de uma preventiva."""
    from django.http import JsonResponse
    from django.utils.dateparse import parse_date

    if request.method != "POST":
        return JsonResponse({"ok": False, "erro": "Método não permitido."}, status=405)

    preventiva = get_object_or_404(Preventiva, pk=pk)
    data_str = (request.POST.get("data_agendamento") or "").strip()

    if data_str:
        data = parse_date(data_str)
        if not data:
            return JsonResponse({"ok": False, "erro": "Data inválida."}, status=400)
        preventiva.data_agendamento = data
    else:
        preventiva.data_agendamento = None

    preventiva.atualizado_por = request.user
    preventiva.save(update_fields=["data_agendamento", "atualizado_por", "updated_at"])

    return JsonResponse({
        "ok": True,
        "data_agendamento": preventiva.data_agendamento.strftime("%Y-%m-%d") if preventiva.data_agendamento else None,
        "data_agendamento_fmt": preventiva.data_agendamento.strftime("%d/%m/%Y") if preventiva.data_agendamento else None,
    })
