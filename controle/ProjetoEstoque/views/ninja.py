"""
views/ninja.py — Módulo NinjaOne RMM

Views:
    ninja_dashboard        GET  /ninja/
    ninja_dispositivos     GET  /ninja/dispositivos/
    ninja_relatorio        GET  /ninja/relatorio/
    ninja_sync             POST /ninja/sync/
    ninja_api_live         GET  /ninja/api/live/   (AJAX — atualização em tempo real)
    ninja_api_relatorio    GET  /ninja/api/relatorio/ (AJAX — dados do relatório)
"""

import datetime
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.db.models import Count, Q
from services.ninja_service import is_configured, is_authenticated


# ─────────────────────────────────────────────────────────────
# Dashboard principal
# ─────────────────────────────────────────────────────────────

@login_required
def ninja_dashboard(request):
    from ProjetoEstoque.models import NinjaDevice, NinjaDeviceSnapshot

    configurado = is_configured()
    autenticado = is_authenticated()
    hoje = timezone.localdate()

    # Info do token para exibir no dashboard
    ninja_token_info = None
    try:
        from ProjetoEstoque.models import NinjaOAuthToken
        t = NinjaOAuthToken.get()
        if t.access_token:
            ninja_token_info = {
                "expires_at":   t.expires_at,
                "has_refresh":  bool(t.refresh_token),
                "updated_at":   t.updated_at,
                "is_valid":     t.is_valid,
            }
    except Exception:
        pass

    qs = NinjaDevice.objects.select_related("item", "item__centro_custo", "item__localidade")

    total     = qs.count()
    online    = qs.filter(is_online=True).count()
    matched   = qs.filter(item__isnull=False).count()
    unmatched = qs.filter(item__isnull=True, serial_number__gt="").count()
    sem_serie = qs.filter(serial_number="").count()

    # Devices online com usuário logado
    online_com_user = (
        qs.filter(is_online=True)
        .exclude(last_user="")
        .order_by("display_name")
    )

    # Devices online sem usuário registrado
    online_sem_user = (
        qs.filter(is_online=True, last_user="")
        .order_by("display_name")
    )

    # Snapshots de hoje — para indicar se o agendamento está rodando
    snapshots_hoje = NinjaDeviceSnapshot.objects.filter(timestamp__date=hoje).count()

    # Máquinas ativas hoje (ao menos 1 snapshot online)
    ativas_hoje = (
        NinjaDeviceSnapshot.objects
        .filter(timestamp__date=hoje, is_online=True)
        .values("device")
        .distinct()
        .count()
    )

    last_sync = (
        NinjaDevice.objects
        .order_by("-last_sync")
        .values_list("last_sync", flat=True)
        .first()
    )

    # Divergência: ativa no NinjaOne mas SEM match no estoque
    sem_match_online = qs.filter(is_online=True, item__isnull=True).order_by("display_name")[:10]

    context = {
        "configurado":    configurado,
        "autenticado":    autenticado,
        "ninja_token_info": ninja_token_info,
        "hoje": hoje,
        "last_sync": last_sync,
        "snapshots_hoje": snapshots_hoje,
        "kpi": {
            "total":        total,
            "online":       online,
            "offline":      total - online,
            "matched":      matched,
            "unmatched":    unmatched,
            "sem_serie":    sem_serie,
            "ativas_hoje":  ativas_hoje,
            "pct_online":   round(online  / total * 100) if total else 0,
            "pct_matched":  round(matched / total * 100) if total else 0,
        },
        "online_com_user":  online_com_user,
        "online_sem_user":  online_sem_user[:10],
        "sem_match_online": sem_match_online,
    }
    return render(request, "front/ninja/ninja_dashboard.html", context)


# ─────────────────────────────────────────────────────────────
# Lista de dispositivos
# ─────────────────────────────────────────────────────────────

@login_required
def ninja_dispositivos(request):
    from ProjetoEstoque.models import NinjaDevice

    q           = (request.GET.get("q") or "").strip()
    filtro_status = request.GET.get("status", "")   # online | offline | ""
    filtro_match  = request.GET.get("match", "")    # matched | unmatched | sem_serie

    qs = NinjaDevice.objects.select_related("item", "item__centro_custo", "item__localidade")

    if q:
        qs = qs.filter(
            Q(display_name__icontains=q) |
            Q(hostname__icontains=q)     |
            Q(serial_number__icontains=q) |
            Q(last_user__icontains=q)    |
            Q(ip_address__icontains=q)   |
            Q(item__nome__icontains=q)
        )

    if filtro_status == "online":
        qs = qs.filter(is_online=True)
    elif filtro_status == "offline":
        qs = qs.filter(is_online=False)

    if filtro_match == "matched":
        qs = qs.filter(item__isnull=False)
    elif filtro_match == "unmatched":
        qs = qs.filter(item__isnull=True, serial_number__gt="")
    elif filtro_match == "sem_serie":
        qs = qs.filter(serial_number="")

    qs = qs.order_by("-is_online", "display_name")

    total     = NinjaDevice.objects.count()
    online    = NinjaDevice.objects.filter(is_online=True).count()
    matched   = NinjaDevice.objects.filter(item__isnull=False).count()
    unmatched = NinjaDevice.objects.filter(item__isnull=True, serial_number__gt="").count()

    context = {
        "dispositivos": qs,
        "total_filtrado": qs.count(),
        "f_q":      q,
        "f_status": filtro_status,
        "f_match":  filtro_match,
        "resumo": {
            "total":     total,
            "online":    online,
            "matched":   matched,
            "unmatched": unmatched,
        },
    }
    return render(request, "front/ninja/ninja_dispositivos.html", context)


# ─────────────────────────────────────────────────────────────
# Relatório de uso
# ─────────────────────────────────────────────────────────────

@login_required
def ninja_relatorio(request):
    from ProjetoEstoque.models import NinjaDevice, NinjaDeviceSnapshot

    hoje = timezone.localdate()

    # Período padrão: últimos 7 dias
    try:
        data_ini = datetime.date.fromisoformat(request.GET.get("data_ini") or "")
    except ValueError:
        data_ini = hoje - datetime.timedelta(days=6)

    try:
        data_fim = datetime.date.fromisoformat(request.GET.get("data_fim") or "")
    except ValueError:
        data_fim = hoje

    if data_fim < data_ini:
        data_fim = data_ini

    # Snapshots no período
    snaps_qs = NinjaDeviceSnapshot.objects.filter(
        timestamp__date__gte=data_ini,
        timestamp__date__lte=data_fim,
    ).select_related("device", "device__item")

    # Calcula intervalo médio entre snapshots (para estimativa de tempo)
    total_snaps = snaps_qs.count()
    delta_dias  = max((data_fim - data_ini).days + 1, 1)

    # Agrupa por device
    from collections import defaultdict
    device_data: dict[int, dict] = defaultdict(lambda: {
        "device": None,
        "total_online":  0,
        "total_offline": 0,
        "usuarios":      set(),
        "primeira_vez":  None,
        "ultima_vez":    None,
        "dias_ativos":   set(),
    })

    for snap in snaps_qs.order_by("device_id", "timestamp"):
        d = device_data[snap.device_id]
        d["device"] = snap.device

        if snap.is_online:
            d["total_online"] += 1
            d["dias_ativos"].add(snap.timestamp.date())
            if snap.current_user:
                d["usuarios"].add(snap.current_user)
            ts_local = timezone.localtime(snap.timestamp)
            if d["primeira_vez"] is None or ts_local < d["primeira_vez"]:
                d["primeira_vez"] = ts_local
            if d["ultima_vez"] is None or ts_local > d["ultima_vez"]:
                d["ultima_vez"] = ts_local
        else:
            d["total_offline"] += 1

    # Monta lista do relatório
    relatorio = []
    for dev_id, info in sorted(device_data.items(),
                                key=lambda x: (x[1]["device"].display_name if x[1]["device"] else "")):
        device   = info["device"]
        if not device:
            continue
        tot      = info["total_online"] + info["total_offline"]
        pct      = round(info["total_online"] / tot * 100) if tot else 0
        # Estimativa: cada snapshot = ~15 min se não soubermos o intervalo real
        minutos_estimados = info["total_online"] * 15

        relatorio.append({
            "device":        device,
            "total_online":  info["total_online"],
            "total_offline": info["total_offline"],
            "pct_online":    pct,
            "minutos_est":   minutos_estimados,
            "horas_est":     round(minutos_estimados / 60, 1),
            "usuarios":      sorted(info["usuarios"]),
            "primeira_vez":  info["primeira_vez"],
            "ultima_vez":    info["ultima_vez"],
            "dias_ativos":   len(info["dias_ativos"]),
        })

    # Ordenar por tempo online (mais ativo primeiro)
    relatorio.sort(key=lambda x: x["total_online"], reverse=True)

    # KPIs do relatório
    total_devices_com_dado = len(relatorio)
    total_ativas = sum(1 for r in relatorio if r["total_online"] > 0)
    max_online = max((r["total_online"] for r in relatorio), default=0)

    context = {
        "relatorio":   relatorio,
        "data_ini":    data_ini,
        "data_fim":    data_fim,
        "hoje":        hoje,
        "total_snaps": total_snaps,
        "delta_dias":  delta_dias,
        "kpi": {
            "total_devices":   total_devices_com_dado,
            "total_ativas":    total_ativas,
            "total_inativas":  total_devices_com_dado - total_ativas,
            "max_online_snaps": max_online,
        },
    }
    return render(request, "front/ninja/ninja_relatorio.html", context)


# ─────────────────────────────────────────────────────────────
# Sync manual (POST)
# ─────────────────────────────────────────────────────────────

@login_required
def ninja_sync(request):
    if request.method != "POST":
        return redirect("ninja_dashboard")

    from services.ninja_service import sync_devices, is_configured

    if not is_configured():
        messages.error(request, "NinjaOne não configurado. Defina as variáveis NINJA_* no .env.")
        return redirect("ninja_dashboard")

    result = sync_devices()

    if result.get("error"):
        messages.error(
            request,
            "Falha ao sincronizar com NinjaOne. Verifique as credenciais e o endereço da API.",
        )
    else:
        messages.success(
            request,
            f"Sincronização concluída: {result['synced']} dispositivos | "
            f"{result['matched']} vinculados ao estoque | "
            f"{result['online']} online agora.",
        )

    return redirect("ninja_dashboard")


# ─────────────────────────────────────────────────────────────
# AJAX — status em tempo real
# ─────────────────────────────────────────────────────────────

@login_required
def ninja_oauth_start(request):
    """
    Redireciona o usuario para a pagina de login do NinjaOne.
    Gera um state aleatorio (RFC 6749 §10.12) e salva na sessao
    para validacao no callback e prevencao de CSRF via OAuth.
    """
    from services.ninja_service import get_authorization_url

    if not is_configured():
        messages.error(request, "NinjaOne nao configurado. Verifique NINJA_BASE_URL, NINJA_CLIENT_ID e NINJA_CLIENT_SECRET no .env.")
        return redirect("ninja_dashboard")

    auth_url, state = get_authorization_url()
    request.session["ninja_oauth_state"] = state
    request.session.modified = True
    return redirect(auth_url)


@login_required
def ninja_oauth_callback(request):
    """
    Recebe o authorization code do NinjaOne e troca pelo access token.
    Valida o state para prevenir CSRF (RFC 6749 §10.12).
    URL registrada no NinjaOne: /ninja/oauth/callback/
    """
    from services.ninja_service import exchange_code_for_token
    import logging
    logger = logging.getLogger(__name__)

    # ── Valida state (anti-CSRF) ──────────────────────────────────
    state_recebido = request.GET.get("state", "")
    state_esperado = request.session.pop("ninja_oauth_state", None)

    if not state_esperado or state_recebido != state_esperado:
        logger.warning(
            "ninja_oauth_callback: state invalido para user %s (ip %s)",
            request.user, request.META.get("REMOTE_ADDR"),
        )
        messages.error(request, "Falha de segurança na autenticação. Tente novamente.")
        return redirect("ninja_dashboard")

    # ── Verifica erro do NinjaOne ─────────────────────────────────
    error = request.GET.get("error")
    if error:
        # Loga detalhes, exibe mensagem genérica ao usuário
        logger.error("ninja_oauth_callback: NinjaOne retornou error=%s desc=%s",
                     error, request.GET.get("error_description", ""))
        messages.error(request, "O NinjaOne recusou a autorização. Verifique as permissões do aplicativo OAuth.")
        return redirect("ninja_dashboard")

    # ── Troca code por token ──────────────────────────────────────
    code = request.GET.get("code")
    if not code:
        messages.error(request, "Codigo de autorizacao nao recebido.")
        return redirect("ninja_dashboard")

    result = exchange_code_for_token(code, user=request.user)

    if result.get("ok"):
        msg = "NinjaOne conectado com sucesso!"
        if not result.get("has_refresh"):
            expires_h = round(result.get("expires_in", 3600) / 3600, 1)
            msg += f" Token expira em ~{expires_h}h — reconecte quando necessario."
        messages.success(request, msg)
    else:
        # Exibe mensagem genérica; detalhe já está no log
        messages.error(request, "Falha ao conectar NinjaOne. Tente novamente ou verifique as credenciais.")

    return redirect("ninja_dashboard")


@login_required
def ninja_oauth_revogar(request):
    """Remove os tokens armazenados (desconecta do NinjaOne)."""
    if request.method != "POST":
        return redirect("ninja_dashboard")

    from services.ninja_service import revoke_token
    revoke_token(user=request.user)
    messages.warning(request, "NinjaOne desconectado. Clique em 'Conectar' para autenticar novamente.")
    return redirect("ninja_dashboard")


@login_required
def ninja_api_live(request):
    """
    Retorna JSON com status atual dos dispositivos (sem chamar a API NinjaOne —
    usa dados do banco para ser rápido). Usado pelo JS da página para refresh.
    """
    from services.ninja_service import get_live_status
    from ProjetoEstoque.models import NinjaDevice
    from django.utils.timesince import timesince

    status = get_live_status()

    last_sync = (
        NinjaDevice.objects
        .order_by("-last_sync")
        .values_list("last_sync", flat=True)
        .first()
    )
    status["last_sync_str"] = (
        f"há {timesince(last_sync)}" if last_sync else "nunca"
    )

    # Últimos dispositivos que ficaram online
    recentes = list(
        NinjaDevice.objects
        .filter(is_online=True)
        .order_by("-last_contact")
        .values("display_name", "last_user", "ip_address", "serial_number")[:8]
    )
    status["recentes"] = recentes

    return JsonResponse(status)
