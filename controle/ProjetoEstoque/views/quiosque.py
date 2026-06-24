"""
quiosque.py — Módulo Quiosque (app Android corporativo).

Dois grupos de views:

  • API do dispositivo (/api/quiosque/...): consumidas pelo APK Android.
    São @csrf_exempt e autenticadas por TOKEN do device (nunca @login_required).
    Contrato em docs/MODULO_QUIOSQUE_ANDROID.md (Seção 4).

  • Dashboard interno (/quiosque/...): telas web para o TI gerenciar os celulares.
    Essas sim são @login_required.

Toda a regra de negócio fica em services/quiosque_service.py.
"""
import json
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from services import quiosque_service as qs


# ──────────────────────────────────────────────────────────────────────────────
# Infra da API do dispositivo
# ──────────────────────────────────────────────────────────────────────────────

def _json_body(request) -> dict:
    """Lê o corpo JSON da requisição; cai para POST form se não for JSON."""
    if request.body:
        try:
            data = json.loads(request.body.decode("utf-8"))
            if isinstance(data, dict):
                return data
        except (ValueError, UnicodeDecodeError):
            pass
    return {k: v for k, v in request.POST.items()}


def kiosk_token_required(view):
    """Autentica o device por `Authorization: Bearer <token>` + `X-Device-UUID`."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        auth = request.headers.get("Authorization", "")
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        device_uuid = request.headers.get("X-Device-UUID", "").strip()
        device = qs.autenticar(token, device_uuid)
        if device is None:
            return JsonResponse({"ok": False, "erro": "Não autorizado."}, status=401)
        request.kiosk_device = device
        return view(request, *args, **kwargs)
    return csrf_exempt(wrapper)


# ──────────────────────────────────────────────────────────────────────────────
# API do dispositivo
# ──────────────────────────────────────────────────────────────────────────────

@csrf_exempt
def kiosk_enroll(request):
    """POST /api/quiosque/enroll/ — matrícula do device (código de uso único)."""
    if request.method != "POST":
        return JsonResponse({"ok": False, "erro": "Método não permitido."}, status=405)

    dados = _json_body(request)
    try:
        res = qs.enroll(codigo_matricula=dados.get("codigo_matricula", ""), dados=dados)
    except ValueError as exc:
        return JsonResponse({"ok": False, "erro": str(exc)}, status=400)
    except Exception:  # noqa: BLE001
        return JsonResponse({"ok": False, "erro": "Falha no enrollment."}, status=500)

    device = res["device"]
    return JsonResponse({
        "ok": True,
        "device_uuid": str(device.device_uuid),
        "token": res["token"],
        "config": res["config"],
    })


@kiosk_token_required
def kiosk_checkin(request):
    """POST /api/quiosque/checkin/ — telemetria periódica (heartbeat)."""
    if request.method != "POST":
        return JsonResponse({"ok": False, "erro": "Método não permitido."}, status=405)
    resp = qs.registrar_checkin(request.kiosk_device, _json_body(request))
    return JsonResponse(resp)


@kiosk_token_required
def kiosk_config(request):
    """GET /api/quiosque/config/ — configuração atual do device."""
    return JsonResponse({"ok": True, "config": qs.config_dict(request.kiosk_device)})


@kiosk_token_required
def kiosk_comando_ack(request, pk: int):
    """POST /api/quiosque/comando/<id>/ack/ — confirmação de execução de comando."""
    if request.method != "POST":
        return JsonResponse({"ok": False, "erro": "Método não permitido."}, status=405)
    dados = _json_body(request)
    ok = qs.registrar_ack_comando(
        request.kiosk_device, pk, dados.get("status", "executado"), dados.get("detalhe", "")
    )
    return JsonResponse({"ok": ok}, status=200 if ok else 404)


# ──────────────────────────────────────────────────────────────────────────────
# Dashboard interno (TI)
# ──────────────────────────────────────────────────────────────────────────────

@login_required
def quiosque_dashboard(request):
    from ProjetoEstoque.models import KioskDevice, KioskMatricula

    f_status = (request.GET.get("status") or "").strip()  # online | offline | sem_local
    q = (request.GET.get("q") or "").strip()

    devices = list(KioskDevice.objects.filter(ativo=True).select_related("item"))

    kpi = {
        "total": len(devices),
        "online": sum(1 for d in devices if d.online),
        "offline": sum(1 for d in devices if not d.online),
        "sem_local": sum(1 for d in devices if not d.tem_localizacao),
        "matriculas": KioskMatricula.objects.filter(usado=False).count(),
    }

    if f_status == "online":
        devices = [d for d in devices if d.online]
    elif f_status == "offline":
        devices = [d for d in devices if not d.online]
    elif f_status == "sem_local":
        devices = [d for d in devices if not d.tem_localizacao]
    if q:
        ql = q.lower()
        devices = [
            d for d in devices
            if ql in (d.apelido or "").lower() or ql in (d.modelo or "").lower()
            or ql in (d.serial or "").lower() or ql in (d.fabricante or "").lower()
        ]

    devices.sort(key=lambda d: (d.online is False, (d.apelido or d.modelo or "").lower()))

    return render(request, "front/quiosque/quiosque_dashboard.html", {
        "devices": devices,
        "kpi": kpi,
        "f_status": f_status,
        "f_q": q,
        "total_filtrado": len(devices),
    })


@login_required
def quiosque_detalhe(request, pk: int):
    from ProjetoEstoque.models import KioskDevice

    device = get_object_or_404(KioskDevice.objects.select_related("item"), pk=pk)
    checkins = device.checkins.all()
    paginator = Paginator(checkins, 30)
    page_obj = paginator.get_page(request.GET.get("page", 1))
    comandos = device.comandos.all()[:20]

    return render(request, "front/quiosque/quiosque_detalhe.html", {
        "device": device,
        "page_obj": page_obj,
        "checkins": page_obj.object_list,
        "total_checkins": paginator.count,
        "comandos": comandos,
    })


@login_required
def quiosque_matriculas(request):
    from ProjetoEstoque.models import KioskMatricula

    if request.method == "POST":
        descricao = (request.POST.get("descricao") or "").strip()
        try:
            validade = int(request.POST.get("validade_horas") or 72)
        except (TypeError, ValueError):
            validade = 72
        m = qs.criar_matricula(descricao=descricao, validade_horas=validade, user=request.user)
        messages.success(request, f"Código de matrícula gerado: {m.codigo}")
        return redirect("quiosque_matriculas")

    matriculas = KioskMatricula.objects.select_related("device", "criado_por").all()[:100]
    return render(request, "front/quiosque/quiosque_matriculas.html", {
        "matriculas": matriculas,
        "disponiveis": KioskMatricula.objects.filter(usado=False).count(),
    })


@login_required
def quiosque_config_editar(request, pk: int):
    from ProjetoEstoque.models import KioskDevice

    device = get_object_or_404(KioskDevice, pk=pk)

    if request.method == "POST":
        device.apelido = (request.POST.get("apelido") or "").strip()[:120]
        device.wifi_only = request.POST.get("wifi_only") == "on"
        device.mensagem_quiosque = (request.POST.get("mensagem_quiosque") or "").strip()[:200]
        try:
            device.intervalo_checkin_seg = max(60, int(request.POST.get("intervalo_checkin_seg") or 300))
        except (TypeError, ValueError):
            device.intervalo_checkin_seg = 300
        # Apps permitidos: uma linha/pacote ou separados por vírgula
        raw = (request.POST.get("apps_permitidos") or "")
        apps = [a.strip() for a in raw.replace(",", "\n").splitlines() if a.strip()]
        device.apps_permitidos = apps
        device.config_versao = (device.config_versao or 1) + 1
        device.save()

        novo_pin = (request.POST.get("admin_pin") or "").strip()
        if novo_pin:
            qs.definir_pin(device, novo_pin)

        messages.success(request, "Configuração atualizada. Será aplicada no próximo check-in do dispositivo.")
        return redirect("quiosque_detalhe", pk=device.pk)

    return render(request, "front/quiosque/quiosque_config.html", {
        "device": device,
        "apps_texto": "\n".join(device.apps_permitidos or []),
    })


@login_required
def quiosque_comando_novo(request, pk: int):
    from ProjetoEstoque.models import KioskDevice, KioskComando

    device = get_object_or_404(KioskDevice, pk=pk)
    if request.method == "POST":
        tipo = (request.POST.get("tipo") or "").strip()
        if tipo in KioskComando.Tipo.values:
            payload = {}
            if tipo == KioskComando.Tipo.MENSAGEM:
                payload = {"texto": (request.POST.get("mensagem") or "").strip()[:200]}
            KioskComando.objects.create(device=device, tipo=tipo, payload=payload, criado_por=request.user)
            messages.success(request, "Comando enfileirado. Será entregue no próximo check-in.")
        else:
            messages.error(request, "Tipo de comando inválido.")
    return redirect("quiosque_detalhe", pk=device.pk)


@login_required
def quiosque_revogar(request, pk: int):
    from ProjetoEstoque.models import KioskDevice

    device = get_object_or_404(KioskDevice, pk=pk)
    if request.method == "POST":
        device.ativo = False
        device.save(update_fields=["ativo", "atualizado_em"])
        messages.success(request, f"Acesso do dispositivo “{device}” revogado.")
        return redirect("quiosque_dashboard")
    return redirect("quiosque_detalhe", pk=device.pk)
