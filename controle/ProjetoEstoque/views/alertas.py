from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Exists, OuterRef
from django.shortcuts import redirect, render
from django.utils import timezone


@login_required
def alertas_dashboard(request):
    from ProjetoEstoque.models import (
        Item,
        MovimentacaoLicenca,
        Preventiva,
        SimNaoChoices,
        StatusUsuarioChoices,
    )

    hoje = timezone.localdate()

    # 1. Preventivas nos próximos 7 dias
    preventivas = (
        Preventiva.objects
        .filter(data_proxima__gte=hoje, data_proxima__lte=hoje + timedelta(days=7), pausada=False)
        .select_related("equipamento", "equipamento__localidade", "checklist_modelo")
        .order_by("data_proxima", "equipamento__nome")
    )
    for p in preventivas:
        p.dias_rest = (p.data_proxima - hoje).days

    # 2. Itens de consumo com estoque crítico
    itens_criticos = (
        Item.objects
        .filter(item_consumo=SimNaoChoices.SIM, quantidade__lt=2)
        .select_related("localidade", "centro_custo", "subtipo")
        .order_by("quantidade", "nome")
    )

    # 3. Licenças de usuários desligados sem devolução
    mov_devolucao_posterior = MovimentacaoLicenca.objects.filter(
        usuario=OuterRef("usuario_id"),
        lote=OuterRef("lote_id"),
        tipo="devolucao",
        created_at__gt=OuterRef("created_at"),
    )
    licencas_pendentes = (
        MovimentacaoLicenca.objects
        .filter(tipo="atribuicao", usuario__status=StatusUsuarioChoices.DESLIGADO)
        .annotate(foi_devolvida=Exists(mov_devolucao_posterior))
        .filter(foi_devolvida=False)
        .select_related("usuario", "lote", "lote__licenca", "usuario__funcao", "usuario__centro_custo")
        .order_by("usuario__nome")
    )

    context = {
        "preventivas": preventivas,
        "itens_criticos": itens_criticos,
        "licencas_pendentes": licencas_pendentes,
        "hoje": hoje,
        "kpi": {
            "preventivas": preventivas.count(),
            "estoque": itens_criticos.count(),
            "licencas": licencas_pendentes.count(),
            "total": preventivas.count() + itens_criticos.count() + licencas_pendentes.count(),
        },
    }
    return render(request, "front/alertas/alertas_dashboard.html", context)


@login_required
def alertas_enviar(request):
    if request.method != "POST":
        return redirect("alertas_dashboard")

    tipo = request.POST.get("tipo", "todos")

    from services.email_alertas import (
        alerta_estoque_critico,
        alerta_licencas_desligados,
        alerta_preventivas_proximas,
    )

    resultados = {}

    try:
        if tipo in ("preventivas", "todos"):
            resultados["preventivas"] = alerta_preventivas_proximas(dias=7)
        if tipo in ("estoque", "todos"):
            resultados["estoque"] = alerta_estoque_critico(limite_qtd=2)
        if tipo in ("licencas", "todos"):
            resultados["licencas"] = alerta_licencas_desligados()

        enviados = sum(1 for v in resultados.values() if v)
        sem_dados = sum(1 for v in resultados.values() if not v)

        if enviados:
            messages.success(
                request,
                f"{enviados} alerta(s) enviado(s) com sucesso."
                + (f" {sem_dados} sem dados para enviar." if sem_dados else ""),
            )
        else:
            messages.info(request, "Nenhum dado encontrado para os alertas selecionados. Nenhum e-mail enviado.")

    except Exception as exc:
        messages.error(request, f"Erro ao enviar alertas: {exc}")

    return redirect("alertas_dashboard")
