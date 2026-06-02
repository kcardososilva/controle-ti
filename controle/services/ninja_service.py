"""
ninja_service.py — Proxy seguro para a API NinjaOne RMM.

Credenciais lidas EXCLUSIVAMENTE de settings.NINJA_BASE_URL / CLIENT_ID / CLIENT_SECRET.
Nunca expoe credenciais ao browser.

Fluxo OAuth2 (Client Credentials Grant):
    POST {BASE_URL}/ws/oauth/token  ->  access_token (~1h)
    GET  {BASE_URL}/api/v2/...      ->  dados com Authorization: Bearer <token>

Schema real do endpoint GET /api/v2/devices (confirmado via documentacao):
  - offline: bool          -> online = not offline
  - displayName / systemName / dnsName / netbiosName
  - lastContact: int       -> epoch Unix
  - organizationId: int
  - references.organization.name -> nome da organizacao (inline)
  - references.assignedOwner     -> tecnico responsavel (nao eh o usuario logado)

Hardware (serial, fabricante, modelo, CPU, RAM) e usuario logado NAO estao
no endpoint basico de devices. Sao obtidos via chamadas adicionais:
  - GET /api/v2/device/{id}/windows/system-info  -> hardware/sistema
  - GET /api/v2/device/{id}/last-logged-on-user  -> ultimo usuario logado
"""

import logging
from datetime import datetime, timezone as tz

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

_TOKEN_CACHE_KEY   = "ninja_token_v2"
_DEVICES_CACHE_KEY = "ninja_devices_v2"
_DEVICES_CACHE_TTL = 120   # 2 min
_ORG_CACHE_TTL     = 600   # 10 min

_INVALID_SERIALS = frozenset({
    "", "to be filled by o.e.m.", "not specified", "default string",
    "none", "n/a", "na", "0", "00000000", "system serial number",
    "to be filled by oem", "chassis serial number",
})


# ─────────────────────────────────────────────────────────────
# Configuracao
# ─────────────────────────────────────────────────────────────

def _cfg() -> tuple[str, str, str]:
    base = getattr(settings, "NINJA_BASE_URL", "").rstrip("/")
    cid  = getattr(settings, "NINJA_CLIENT_ID", "")
    sec  = getattr(settings, "NINJA_CLIENT_SECRET", "")
    return base, cid, sec


def is_configured() -> bool:
    base, cid, sec = _cfg()
    return bool(base and cid and sec)


# ─────────────────────────────────────────────────────────────
# OAuth2
# ─────────────────────────────────────────────────────────────

def _get_token() -> str | None:
    cached = cache.get(_TOKEN_CACHE_KEY)
    if cached:
        return cached

    base, cid, sec = _cfg()
    if not all([base, cid, sec]):
        logger.warning("ninja_service: credenciais nao configuradas.")
        return None

    import requests
    try:
        resp = requests.post(
            f"{base}/ws/oauth/token",
            data={
                "grant_type":    "client_credentials",
                "client_id":     cid,
                "client_secret": sec,
                "scope":         "monitoring management",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data  = resp.json()
        token = data.get("access_token")
        ttl   = max(int(data.get("expires_in", 3600)) - 60, 30)
        if token:
            cache.set(_TOKEN_CACHE_KEY, token, timeout=ttl)
            logger.info("ninja_service: token obtido (expira em %ds).", ttl)
        return token
    except Exception as exc:
        logger.error("ninja_service: falha ao obter token — %s", exc)
        return None


def invalidate_token():
    cache.delete(_TOKEN_CACHE_KEY)


# ─────────────────────────────────────────────────────────────
# HTTP helper
# ─────────────────────────────────────────────────────────────

def _api_get(path: str, params: dict | None = None) -> list | dict | None:
    import requests

    token = _get_token()
    if not token:
        return None

    base, _, _ = _cfg()
    url = f"{base}/api/v2{path}"
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params=params or {},
            timeout=15,
        )
        if resp.status_code == 401:
            invalidate_token()
            new_token = _get_token()
            if not new_token:
                return None
            resp = requests.get(
                url,
                headers={"Authorization": f"Bearer {new_token}"},
                params=params or {},
                timeout=15,
            )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.error("ninja_service: erro em GET %s — %s", path, exc)
        return None


# ─────────────────────────────────────────────────────────────
# Paginacao
# ─────────────────────────────────────────────────────────────

def _paginate(path: str, page_size: int = 200) -> list[dict]:
    results: list[dict] = []
    after = None

    while True:
        params: dict = {"pageSize": page_size}
        if after:
            params["after"] = after

        data = _api_get(path, params)
        if data is None:
            break

        if isinstance(data, list):
            results.extend(data)
            break

        if isinstance(data, dict):
            items = (
                data.get("devices") or
                data.get("data") or
                data.get("results") or
                []
            )
            results.extend(items)
            cursor = data.get("cursor") or data.get("nextCursor")
            if not cursor or not items:
                break
            after = cursor
        else:
            break

    return results


# ─────────────────────────────────────────────────────────────
# Endpoints publicos de leitura
# ─────────────────────────────────────────────────────────────

def get_devices() -> list[dict]:
    """
    Lista dispositivos do NinjaOne.
    Schema real: offline(bool), displayName, dnsName, lastContact(epoch),
                 organizationId, references.organization.name
    Hardware e usuario logado NAO estao aqui — requerem chamadas por device.
    """
    cached = cache.get(_DEVICES_CACHE_KEY)
    if cached is not None:
        return cached

    devices = _paginate("/devices")
    if devices:
        cache.set(_DEVICES_CACHE_KEY, devices, _DEVICES_CACHE_TTL)
    return devices


def get_device_system_info(device_id: int) -> dict:
    """
    Retorna informacoes de hardware/sistema de um device especifico.
    Tenta varias rotas conhecidas da API NinjaOne em ordem de preferencia.
    Retorna dict com: serialNumber, manufacturer, model, processorType,
                      totalPhysicalMemory, biosSerialNumber, ipAddress
    """
    # Rota 1: endpoint de system info do Windows
    data = _api_get(f"/device/{device_id}/windows/system-info")
    if isinstance(data, dict) and data:
        return data

    # Rota 2: endpoint generico de system info
    data = _api_get(f"/device/{device_id}/system-info")
    if isinstance(data, dict) and data:
        return data

    # Rota 3: detalhe completo do device (pode incluir system inline)
    data = _api_get(f"/device/{device_id}")
    if isinstance(data, dict):
        return data.get("system") or data.get("systemInfo") or {}

    return {}


def get_device_last_user(device_id: int) -> str:
    """
    Retorna o ultimo usuario logado no dispositivo.
    Endpoint: GET /api/v2/device/{id}/last-logged-on-user
    """
    data = _api_get(f"/device/{device_id}/last-logged-on-user")
    if isinstance(data, dict):
        user = (
            data.get("lastLoggedOnUser") or
            data.get("username") or
            data.get("user") or
            data.get("name") or
            ""
        )
        return _clean_user(str(user))
    if isinstance(data, str):
        return _clean_user(data)
    return ""


def invalidate_devices_cache():
    cache.delete(_DEVICES_CACHE_KEY)


# ─────────────────────────────────────────────────────────────
# Parsing
# ─────────────────────────────────────────────────────────────

def _parse_timestamp(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)) and float(value) > 0:
        try:
            return datetime.fromtimestamp(float(value), tz=tz.utc)
        except (ValueError, OSError):
            return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _clean_serial(serial: str) -> str:
    s = serial.strip()
    return "" if s.lower() in _INVALID_SERIALS else s


def _clean_user(raw: str) -> str:
    u = (raw or "").strip()
    if not u or u.lower() in ("null", "none", "undefined"):
        return ""
    if "\\" in u:
        u = u.split("\\")[-1]
    return u


def _parse_device_basic(d: dict) -> dict:
    """
    Extrai campos do schema real de GET /api/v2/devices.
    Hardware e usuario logado ficam em branco — preenchidos pelo sync completo.
    """
    refs      = d.get("references") or {}
    org_ref   = refs.get("organization") or {}
    org_name  = org_ref.get("name") or ""

    # Online: campo e 'offline' (bool) — True = offline, False = online
    is_online = not d.get("offline", True)

    # Hostname preferencia: dnsName > netbiosName > systemName
    hostname = (
        d.get("dnsName") or
        d.get("netbiosName") or
        d.get("systemName") or
        ""
    )

    # Display name
    display_name = (
        d.get("displayName") or
        d.get("systemName") or
        f"Device #{d.get('id', '?')}"
    )

    return {
        "ninja_id":        d.get("id"),
        "display_name":    display_name,
        "hostname":        hostname,
        "serial_number":   "",        # preenchido via get_device_system_info
        "os_name":         "",        # idem
        "manufacturer":    "",        # idem
        "model_name":      "",        # idem
        "processor":       "",        # idem
        "total_memory_mb": None,      # idem
        "ip_address":      "",        # idem
        "last_contact":    _parse_timestamp(d.get("lastContact")),
        "is_online":       is_online,
        "last_user":       "",        # preenchido via get_device_last_user
        "node_class":      d.get("nodeClass") or "",
        "organization_name": org_name,
        "org_id":          d.get("organizationId"),
    }


def _parse_system_info(raw: dict) -> dict:
    """
    Extrai campos de hardware do endpoint system-info.
    Suporta variacoes de schema (campo raiz ou aninhado em 'system').
    """
    # Alguns endpoints retornam os dados direto, outros dentro de 'system'
    data = raw.get("system") or raw

    serial = _clean_serial(
        data.get("biosSerialNumber") or
        data.get("serialNumber") or
        raw.get("serialNumber") or
        ""
    )

    mem_raw = data.get("totalPhysicalMemory") or data.get("physicalMemory") or 0
    if mem_raw > 1_000_000:        # bytes -> MB
        mem_mb = int(mem_raw / (1024 * 1024))
    elif mem_raw > 0:              # ja em MB
        mem_mb = int(mem_raw)
    else:
        mem_mb = None

    # IP: pode estar em varios campos dependendo da versao
    ips = data.get("ipAddresses") or raw.get("ipAddresses") or []
    ip  = ips[0] if isinstance(ips, list) and ips else (data.get("ipAddress") or raw.get("ipAddress") or "")

    os_name = (
        (data.get("os") or {}).get("name") or
        data.get("osName") or
        raw.get("osName") or
        ""
    )

    return {
        "serial_number":   serial,
        "os_name":         os_name,
        "manufacturer":    data.get("manufacturer") or "",
        "model_name":      data.get("model") or data.get("name") or "",
        "processor":       data.get("processorType") or data.get("processor") or "",
        "total_memory_mb": mem_mb,
        "ip_address":      ip,
    }


# ─────────────────────────────────────────────────────────────
# Sincronizacao
# ─────────────────────────────────────────────────────────────

def sync_devices() -> dict:
    """
    Sincroniza todos os dispositivos NinjaOne com a tabela NinjaDevice.

    Fluxo:
    1. Busca lista de devices (GET /devices) — status, nome, org
    2. Para cada device, busca system-info (serial, hardware) e last-logged-user
    3. Vincula ao Item pelo numero de serie
    4. Grava snapshot do estado atual

    Retorna: {"synced": N, "matched": N, "online": N, "error": bool}
    """
    from ProjetoEstoque.models import NinjaDevice, Item
    from django.utils import timezone

    raw_devices = get_devices()
    if not raw_devices:
        logger.error("ninja_service.sync_devices: API nao retornou dispositivos.")
        return {"synced": 0, "matched": 0, "online": 0, "error": True}

    synced = matched = online = 0
    now = timezone.now()

    for raw in raw_devices:
        parsed = _parse_device_basic(raw)
        ninja_id = parsed.get("ninja_id")
        if not ninja_id:
            continue

        # ── Hardware e serial (chamada adicional por device) ──────────────
        sys_info_raw = get_device_system_info(ninja_id)
        hw = _parse_system_info(sys_info_raw) if sys_info_raw else {}

        # ── Ultimo usuario logado ─────────────────────────────────────────
        last_user = get_device_last_user(ninja_id)

        # Mescla dados
        parsed.update({k: v for k, v in hw.items() if v})
        if last_user:
            parsed["last_user"] = last_user

        serial   = parsed.get("serial_number", "")
        org_id   = parsed.pop("org_id", None)

        # ── Vincula ao Item pelo numero de serie ──────────────────────────
        item = Item.objects.filter(numero_serie__iexact=serial).first() if serial else None

        defaults = {k: v for k, v in parsed.items() if k != "ninja_id"}
        defaults["item"] = item
        NinjaDevice.objects.update_or_create(ninja_id=ninja_id, defaults=defaults)

        synced += 1
        if item:
            matched += 1
        if parsed.get("is_online"):
            online += 1

    _take_snapshot(now)
    invalidate_devices_cache()

    logger.info(
        "ninja_service.sync_devices: %d devices | %d vinculados | %d online",
        synced, matched, online,
    )
    return {"synced": synced, "matched": matched, "online": online, "error": False}


def _take_snapshot(timestamp=None) -> int:
    from ProjetoEstoque.models import NinjaDevice, NinjaDeviceSnapshot
    from django.utils import timezone

    ts = timestamp or timezone.now()
    snapshots = [
        NinjaDeviceSnapshot(
            device_id=device.pk,
            timestamp=ts,
            is_online=device.is_online,
            current_user=device.last_user,
            ip_address=device.ip_address,
        )
        for device in NinjaDevice.objects.all()
    ]
    NinjaDeviceSnapshot.objects.bulk_create(snapshots)
    logger.info("ninja_service._take_snapshot: %d snapshots.", len(snapshots))
    return len(snapshots)


# ─────────────────────────────────────────────────────────────
# Helpers para views
# ─────────────────────────────────────────────────────────────

def get_live_status() -> dict:
    from ProjetoEstoque.models import NinjaDevice

    qs       = NinjaDevice.objects.select_related("item")
    total    = qs.count()
    online   = qs.filter(is_online=True).count()
    matched  = qs.filter(item__isnull=False).count()
    com_user = qs.filter(is_online=True).exclude(last_user="").count()

    return {
        "total":       total,
        "online":      online,
        "offline":     total - online,
        "matched":     matched,
        "com_user":    com_user,
        "pct_online":  round(online  / total * 100) if total else 0,
        "pct_matched": round(matched / total * 100) if total else 0,
    }
