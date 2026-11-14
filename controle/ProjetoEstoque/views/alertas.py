from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Exists, OuterRef
from django.shortcuts import redirect, render
from django.utils import timezone


@login_required
def alertas_dashboard(request):
    from ProjetoEstoque.models import (
        CanalNotificacao,
        ConfiguracaoSistema,
        MovimentacaoLicenca,
        StatusUsuarioChoices,
    )
    from services.email_alertas import itens_estoque_critico, preventivas_relevantes, sincronizar_catalogo_notificacoes

    config = ConfiguracaoSistema.get()
    hoje = timezone.localdate()

    # Card de acesso ao painel de notificações (corpo da página) — contagem ao
    # vivo, por isso sincroniza o catálogo aqui também (idempotente e barato).
    sincronizar_catalogo_notificacoes()
    canais_total = CanalNotificacao.objects.count()
    canais_ativos = CanalNotificacao.objects.filter(ativo=True).count()

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
        "canais_kpi": {
            "total": canais_total,
            "ativos": canais_ativos,
            "inativos": canais_total - canais_ativos,
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


# ── Painel de notificações ────────────────────────────────────────────────────────────────────────────────────────
# Cada ação do painel (ativar/desativar, trocar destinatários, tirar um e-mail, desvincular um login) pode ser feita de
# duas maneiras: pelo formulário comum (POST + redirect + mensagem — é o que vale sem JavaScript) e pela própria tela,
# que chama a mesma URL por fetch e recebe JSON com o cartão do canal JÁ redesenhado. A segunda existe porque a primeira
# recarregava a página inteira a cada e-mail salvo: a pessoa perdia a posição de rolagem numa lista de ~20 canais e
# voltava ao topo para conferir o que tinha acabado de mudar. A regra de cada ação é uma só (mesmo código nas duas).

def _quer_json(request) -> bool:
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _preparar_canal(canal):
    """Acrescenta ao canal o que o cartão mostra: quem recebe AGORA (resolvido em tempo real, não o texto gravado)."""
    from services.email_alertas import resolver_destinatarios_atuais

    pessoas, nota = resolver_destinatarios_atuais(canal)
    canal.pessoas = pessoas
    canal.pessoas_nota = nota
    canal.pessoas_total = len(pessoas)
    return canal


def _kpi_canais() -> dict:
    """Os quatro totais da faixa do topo. Uma consulta: a tabela tem ~20 linhas, mas a faixa é refeita a cada ação."""
    from django.db.models import Count, Q, Sum

    from ProjetoEstoque.models import CanalNotificacao

    agregado = CanalNotificacao.objects.aggregate(total=Count("pk"), ativos=Count("pk", filter=Q(ativo=True)), envios=Sum("total_envios"))
    total, ativos = agregado["total"] or 0, agregado["ativos"] or 0
    return {"total": total, "ativos": ativos, "inativos": total - ativos, "envios": agregado["envios"] or 0}


def _responder(request, canal, nivel: str, texto: str):
    """Encerra uma ação do painel. `nivel` é o do `django.contrib.messages` (success, warning, error, info).
    Pela tela (fetch): JSON com a mensagem, os totais e o cartão do canal redesenhado — e NENHUMA mensagem na sessão,
    senão ela apareceria de novo na próxima vez que a página fosse aberta. Pelo formulário comum: mensagem + redirect."""
    if _quer_json(request):
        from django.http import JsonResponse
        from django.template.loader import render_to_string

        html = None
        if canal is not None and nivel != "error":
            html = render_to_string("front/alertas/_canal_notificacao.html", {"canal": _preparar_canal(canal)}, request=request)
        status = 404 if canal is None else (400 if nivel == "error" else 200)
        return JsonResponse({"ok": nivel != "error", "nivel": nivel, "mensagem": texto, "html": html, "kpi": _kpi_canais()}, status=status)
    getattr(messages, nivel)(request, texto)
    return redirect("alertas_notificacoes")


@login_required
def alertas_notificacoes(request):
    """Painel de controle central de TODAS as notificações por e-mail do sistema:
    ativar/desativar cada uma individualmente e, para as de lista fixa,
    redirecionar destinatários — sem mexer em código. O catálogo é sincronizado
    a cada acesso (novas notificações adicionadas ao código aparecem sozinhas)."""
    from ProjetoEstoque.models import CanalNotificacao, ConfiguracaoSistema
    from services.email_alertas import sincronizar_catalogo_notificacoes

    sincronizar_catalogo_notificacoes()
    config = ConfiguracaoSistema.get()

    categorias = {}
    for c in CanalNotificacao.objects.all().order_by("categoria", "nome"):
        categorias.setdefault(c.categoria or "Outros", []).append(_preparar_canal(c))

    context = {
        "alertas_ativos": config.alertas_email_ativos,
        "categorias": sorted(categorias.items(), key=lambda kv: kv[0]),
        "kpi": _kpi_canais(),
    }
    return render(request, "front/alertas/alertas_notificacoes.html", context)


@login_required
def alertas_notificacao_toggle(request, pk):
    if request.method != "POST":
        return redirect("alertas_notificacoes")

    from ProjetoEstoque.models import CanalNotificacao

    canal = CanalNotificacao.objects.filter(pk=pk).first()
    if canal is None:
        return _responder(request, None, "error", "Notificação não encontrada.")

    canal.ativo = not canal.ativo
    canal.atualizado_por = request.user
    canal.save(update_fields=["ativo", "atualizado_por", "updated_at"])

    if canal.ativo:
        return _responder(request, canal, "success", f"Notificação '{canal.nome}' ativada.")
    return _responder(request, canal, "warning", f"Notificação '{canal.nome}' desativada. Nenhum e-mail será enviado por este canal.")


@login_required
def alertas_notificacao_destinatarios(request, pk):
    if request.method != "POST":
        return redirect("alertas_notificacoes")

    from ProjetoEstoque.models import CanalNotificacao

    canal = CanalNotificacao.objects.filter(pk=pk).first()
    if canal is None:
        return _responder(request, None, "error", "Notificação não encontrada.")

    if canal.codigo == "item_defeito":
        return _responder(request, canal, "error", "Esta notificação tem destinatários definidos por login — gerencie em Fornecedores → Acesso ao Portal.")

    destinatarios = request.POST.get("destinatarios", "").strip()
    canal.destinatarios_customizados = destinatarios
    canal.destinatarios_customizados_ativo = bool(destinatarios)
    canal.atualizado_por = request.user
    canal.save(update_fields=["destinatarios_customizados", "destinatarios_customizados_ativo", "atualizado_por", "updated_at"])

    if destinatarios:
        return _responder(request, canal, "success", f"Destinatários de '{canal.nome}' atualizados.")
    return _responder(request, canal, "success", f"'{canal.nome}' voltou a usar os destinatários padrão do sistema.")


@login_required
def alertas_notificacao_remover_email(request, pk):
    """Desvincula UM e-mail da lista-base de um canal com um clique — sem exigir
    reescrever a lista inteira. Funciona tanto para canais de lista fixa quanto
    para os "dinâmicos com base" (movimentacao_transacional, transferencia_
    equipamento — a parte dinâmica deles é só o e-mail do evento, sempre
    adicionado por cima, nunca afetado por esta remoção). Na primeira remoção,
    'congela' a lista efetiva atual (customizada ou padrão do .env) menos o
    e-mail removido, e marca `destinatarios_customizados_ativo=True` — mesmo
    que o resultado fique vazio, para não reintroduzir o padrão silenciosamente."""
    if request.method != "POST":
        return redirect("alertas_notificacoes")

    from ProjetoEstoque.models import CanalNotificacao
    from services.email_alertas import resolver_destinatarios_atuais

    canal = CanalNotificacao.objects.filter(pk=pk).first()
    if canal is None:
        return _responder(request, None, "error", "Notificação não encontrada.")

    if canal.codigo == "item_defeito":
        return _responder(request, canal, "error", "Esta notificação tem destinatários definidos por login — use o botão de desvincular do próprio card.")

    alvo = request.POST.get("email", "").strip().lower()
    if not alvo:
        return _responder(request, canal, "info", "Nenhum e-mail informado para remover.")

    pessoas, _ = resolver_destinatarios_atuais(canal)
    restantes = [p["email"] for p in pessoas if p["email"].strip().lower() != alvo]

    canal.destinatarios_customizados = ", ".join(restantes)
    canal.destinatarios_customizados_ativo = True
    canal.atualizado_por = request.user
    canal.save(update_fields=["destinatarios_customizados", "destinatarios_customizados_ativo", "atualizado_por", "updated_at"])

    if restantes:
        return _responder(request, canal, "success", f"'{alvo}' removido de '{canal.nome}'.")
    return _responder(
        request, canal, "warning",
        f"'{alvo}' removido de '{canal.nome}'. Nenhum destinatário restante — este canal não enviará até que alguém seja adicionado.",
    )


@login_required
def alertas_notificacao_desvincular_perfil(request, pk, perfil_id):
    """Desvincula um login do Portal do Fornecedor da notificação de 'Defeito'
    (canal item_defeito) com um clique — reaproveita o mesmo toggle já usado em
    Fornecedores → Acesso ao Portal, sem precisar navegar até lá."""
    if request.method != "POST":
        return redirect("alertas_notificacoes")

    from ProjetoEstoque.models import CanalNotificacao, PerfilFornecedor
    from services.fornecedor_acesso_service import FornecedorAcessoService

    canal = CanalNotificacao.objects.filter(pk=pk).first()
    if canal is None or canal.codigo != "item_defeito":
        return _responder(request, canal, "error", "Esta notificação não pode ser desvinculada por aqui.")

    perfil = PerfilFornecedor.objects.filter(pk=perfil_id).select_related("usuario", "fornecedor").first()
    if perfil is None:
        return _responder(request, canal, "error", "Login do fornecedor não encontrado.")

    FornecedorAcessoService.definir_notificacao_defeito(perfil, False, request.user)
    nome = perfil.usuario.get_full_name() or perfil.usuario.username
    return _responder(request, canal, "success", f"'{nome}' ({perfil.fornecedor.nome}) não receberá mais avisos de Defeito.")
