from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Exists, OuterRef
from django.shortcuts import redirect, render
from django.utils import timezone


@login_required
def alertas_dashboard(request):
    from ProjetoEstoque.models import (
        ConfiguracaoSistema,
        Item,
        MovimentacaoLicenca,
        Preventiva,
        SimNaoChoices,
        StatusUsuarioChoices,
    )
    config = ConfiguracaoSistema.get()

    hoje = timezone.localdate()

    # 1. Preventivas nos próximos 7 dias.
    #    IMPORTANTE: a data efetiva da próxima preventiva é CALCULADA da mesma forma
    #    que nas telas de preventivas/equipamentos (data_ultima + intervalo), e não lida
    #    diretamente do campo data_proxima — que pode estar desatualizado. Sem isso o
    #    alerta mostrava 0 enquanto o sistema apontava preventivas a vencer.
    #    Intervalo: prioridade para Item.data_limite_preventiva → CheckListModelo.intervalo_dias.
    _JANELA = 7
    preventivas = []
    for p in (
        Preventiva.objects
        .filter(pausada=False)
        .select_related("equipamento", "equipamento__localidade", "checklist_modelo")
    ):
        intervalo = 0
        try:
            intervalo = int(p.equipamento.data_limite_preventiva or 0)
        except (TypeError, ValueError):
            intervalo = 0
        if intervalo <= 0 and p.checklist_modelo:
            try:
                intervalo = int(p.checklist_modelo.intervalo_dias or 0)
            except (TypeError, ValueError):
                intervalo = 0

        if intervalo > 0 and p.data_ultima:
            proxima = p.data_ultima + timedelta(days=intervalo)
        else:
            proxima = p.data_proxima

        if not proxima:
            continue
        dias = (proxima - hoje).days
        if 0 <= dias <= _JANELA:
            p.data_proxima = proxima  # data efetiva (em memória) para exibição
            p.dias_rest = dias
            preventivas.append(p)

    preventivas.sort(key=lambda p: (p.data_proxima, p.equipamento.nome))

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
        "alertas_ativos": config.alertas_email_ativos,
        "config_updated_at": config.updated_at,
        "config_atualizado_por": config.atualizado_por,
        "kpi": {
            "preventivas": len(preventivas),
            "estoque": itens_criticos.count(),
            "licencas": licencas_pendentes.count(),
            "total": len(preventivas) + itens_criticos.count() + licencas_pendentes.count(),
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


@login_required
def alertas_toggle(request):
    if request.method != "POST":
        return redirect("alertas_dashboard")

    from ProjetoEstoque.models import ConfiguracaoSistema

    config = ConfiguracaoSistema.get()
    config.alertas_email_ativos = not config.alertas_email_ativos
    config.atualizado_por = request.user
    config.save()

    if config.alertas_email_ativos:
        messages.success(
            request,
            "Alertas de e-mail ativados. O sistema voltará a enviar notificações automáticas.",
        )
    else:
        messages.warning(
            request,
            "Alertas de e-mail desativados. Nenhuma notificação será enviada até que sejam reativados.",
        )

    return redirect("alertas_dashboard")
