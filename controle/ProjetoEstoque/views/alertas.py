from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Exists, OuterRef
from django.shortcuts import redirect, render
from django.utils import timezone


@login_required
def alertas_dashboard(request):
    from ProjetoEstoque.models import (
        ConfiguracaoSistema,
        MovimentacaoLicenca,
        StatusUsuarioChoices,
    )
    from services.email_alertas import itens_estoque_critico, preventivas_relevantes

    config = ConfiguracaoSistema.get()
    hoje = timezone.localdate()

    # 1. Preventivas — vencidas + próximas 7 dias.
    #    A data efetiva é CALCULADA (data_ultima + intervalo), a MESMA regra das telas
    #    de preventivas/equipamentos e dos e-mails — o campo data_proxima sozinho pode
    #    estar desatualizado. As vencidas (em atraso) também são exibidas, pois são as
    #    mais críticas e antes ficavam de fora do painel.
    prev_vencidas, prev_proximas = preventivas_relevantes(7)
    preventivas = prev_vencidas + prev_proximas

    # 2. Itens de consumo com estoque crítico — saldo EFETIVO por lote quando aplicável
    #    (soma de ItemLote.quantidade_disponivel); senão usa Item.quantidade.
    itens_criticos = itens_estoque_critico(2)

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
    n_licencas = licencas_pendentes.count()

    context = {
        "preventivas": preventivas,
        "preventivas_vencidas": prev_vencidas,
        "preventivas_proximas": prev_proximas,
        "itens_criticos": itens_criticos,
        "licencas_pendentes": licencas_pendentes,
        "hoje": hoje,
        "alertas_ativos": config.alertas_email_ativos,
        "config_updated_at": config.updated_at,
        "config_atualizado_por": config.atualizado_por,
        "kpi": {
            "preventivas": len(preventivas),
            "preventivas_vencidas": len(prev_vencidas),
            "preventivas_proximas": len(prev_proximas),
            "estoque": len(itens_criticos),
            "licencas": n_licencas,
            "total": len(preventivas) + len(itens_criticos) + n_licencas,
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
        relatorio_diario,
    )

    resultados = {}

    try:
        # Relatório consolidado completo (estoque + baixas + movimentações +
        # licenças + preventivas vencidas/próximas) em um único e-mail.
        if tipo in ("relatorio", "diario"):
            resultados["relatorio"] = relatorio_diario(horas=24)
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
