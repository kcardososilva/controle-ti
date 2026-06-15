"""
ninja_service.py — Proxy seguro para a API NinjaOne RMM.

Autenticacao: Authorization Code flow (nao client_credentials).
O usuario realiza login UMA vez pelo botao 'Conectar NinjaOne' no dashboard.
O token e armazenado no banco (NinjaOAuthToken pk=1).
Refresh automatico via refresh_token quando disponivel.

Fluxo:
  1. Usuario acessa /ninja/autorizar/
  2. Redireciona para https://app.ninjarmm.com/ws/oauth/authorize
  3. Usuario faz login no NinjaOne
  4. NinjaOne redireciona para /ninja/oauth/callback/?code=XXXX
  5. Django troca code por access_token + refresh_token
  6. Tokens salvos em NinjaOAuthToken (pk=1)
  7. sync_ninja usa o token armazenado automaticamente

Redirect URI registrada no NinjaOne:
  http://santa-colomba-karitel-qqprmnjdwc.dynamic-m.com:65300/ninja/oauth/callback/
"""

import logging
from datetime import datetime, timedelta, timezone as tz

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

_TOKEN_CACHE_KEY  = "ninja_access_token_v3"
_OAUTH_STATE_KEY  = "ninja_oauth_state"
_SCOPE            = "monitoring"   # offline_access nao suportado neste app


def get_redirect_uri() -> str:
    """Lê NINJA_REDIRECT_URI do .env. Fallback para localhost em dev."""
    return getattr(settings, "NINJA_REDIRECT_URI",
                   "http://localhost:8000/ninja/oauth/callback/")

_INVALID_SERIALS = frozenset({
    "", "to be filled by o.e.m.", "not specified", "default string",
    "none", "n/a", "na", "0", "00000000", "system serial number",
    "to be filled by oem", "chassis serial number",
})


# ─────────────────────────────────────────────────────────────
# Configuracao
# ─────────────────────────────────────────────────────────────

def _cfg() -> tuple[str, str, str]:
    """Retorna (base_api_url, client_id, client_secret)."""
    base = getattr(settings, "NINJA_BASE_URL", "").rstrip("/")
    cid  = getattr(settings, "NINJA_CLIENT_ID", "")
    sec  = getattr(settings, "NINJA_CLIENT_SECRET", "")
    return base, cid, sec


def is_configured() -> bool:
    base, cid, sec = _cfg()
    return bool(base and cid and sec)


def is_authenticated() -> bool:
    """Retorna True se ha um token valido armazenado."""
    try:
        from ProjetoEstoque.models import NinjaOAuthToken
        return NinjaOAuthToken.get().is_valid
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────
# Authorization URL (passo 1 do fluxo)
# ─────────────────────────────────────────────────────────────

def get_authorization_url() -> tuple[str, str]:
    """
    Gera (url_de_autorizacao, state).
    O state deve ser salvo na sessao do usuario e validado no callback
    para prevenir CSRF via OAuth (RFC 6749 §10.12).
    """
    import secrets
    import urllib.parse

    base, cid, _ = _cfg()
    state = secrets.token_urlsafe(32)

    params = urllib.parse.urlencode({
        "client_id":     cid,
        "response_type": "code",
        "redirect_uri":  get_redirect_uri(),
        "scope":         _SCOPE,
        "state":         state,
    })
    return f"{base}/ws/oauth/authorize?{params}", state


# ─────────────────────────────────────────────────────────────
# Troca code por token (passo 5 do fluxo)
# ─────────────────────────────────────────────────────────────

def exchange_code_for_token(code: str, user=None) -> dict:
    """
    Troca o authorization code por access_token + refresh_token.
    Salva os tokens no banco (NinjaOAuthToken pk=1).
    Retorna dict com sucesso/erro.
    """
    import requests as req
    from ProjetoEstoque.models import NinjaOAuthToken
    from django.utils import timezone

    base, cid, sec = _cfg()

    try:
        resp = req.post(
            f"{base}/ws/oauth/token",
            data={
                "grant_type":   "authorization_code",
                "client_id":    cid,
                "client_secret": sec,
                "code":         code,
                "redirect_uri": get_redirect_uri(),
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.error("ninja_service.exchange_code: falha — %s", exc)
        return {"ok": False, "error": str(exc)}

    access  = data.get("access_token", "")
    refresh = data.get("refresh_token", "")
    expires = data.get("expires_in", 3600)

    if not access:
        # Loga detalhes internamente, não expõe ao usuário
        logger.error("ninja_service.exchange_code: resposta sem access_token — campos: %s", list(data.keys()))
        return {"ok": False, "error": "Falha ao obter token de acesso. Verifique as credenciais no .env."}

    token_obj = NinjaOAuthToken.get()
    token_obj.access_token  = access
    token_obj.refresh_token = refresh
    token_obj.expires_at    = timezone.now() + timedelta(seconds=int(expires) - 60)
    token_obj.scope         = data.get("scope", _SCOPE)
    token_obj.updated_by    = user
    token_obj.save()

    # Atualiza cache imediatamente
    cache.set(_TOKEN_CACHE_KEY, access, timeout=max(int(expires) - 60, 30))

    logger.info(
        "ninja_service: token autorizado. Expira em %ds. Refresh: %s",
        expires, "sim" if refresh else "nao",
    )
    return {"ok": True, "has_refresh": bool(refresh), "expires_in": expires}


# ─────────────────────────────────────────────────────────────
# Obtencao do token (uso interno)
# ─────────────────────────────────────────────────────────────

def _get_token() -> str | None:
    """
    Retorna access_token valido.
    Ordem: cache -> banco (valido) -> refresh_token -> None
    """
    # 1. Cache em memoria (mais rapido)
    cached = cache.get(_TOKEN_CACHE_KEY)
    if cached:
        return cached

    try:
        from ProjetoEstoque.models import NinjaOAuthToken
        from django.utils import timezone

        token_obj = NinjaOAuthToken.get()

        # 2. Token no banco ainda valido
        if token_obj.is_valid:
            ttl = max(
                int((token_obj.expires_at - timezone.now()).total_seconds()) - 30,
                60,
            ) if token_obj.expires_at else 3540
            cache.set(_TOKEN_CACHE_KEY, token_obj.access_token, timeout=ttl)
            return token_obj.access_token

        # 3. Tenta refresh_token se disponivel
        if token_obj.refresh_token:
            return _refresh_access_token(token_obj)

    except Exception as exc:
        logger.error("ninja_service._get_token: erro — %s", exc)

    logger.warning("ninja_service: nao autenticado — acesse /ninja/autorizar/ para conectar.")
    return None


def _refresh_access_token(token_obj) -> str | None:
    """Usa o refresh_token para obter um novo access_token."""
    import requests as req
    from django.utils import timezone

    base, cid, sec = _cfg()

    try:
        resp = req.post(
            f"{base}/ws/oauth/token",
            data={
                "grant_type":    "refresh_token",
                "client_id":     cid,
                "client_secret": sec,
                "refresh_token": token_obj.refresh_token,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.error("ninja_service._refresh: falha — %s", exc)
        return None

    access  = data.get("access_token", "")
    refresh = data.get("refresh_token", token_obj.refresh_token)
    expires = int(data.get("expires_in", 3600))

    if not access:
        return None

    token_obj.access_token  = access
    token_obj.refresh_token = refresh
    token_obj.expires_at    = timezone.now() + timedelta(seconds=expires - 60)
    token_obj.save(update_fields=["access_token", "refresh_token", "expires_at", "updated_at"])

    cache.set(_TOKEN_CACHE_KEY, access, timeout=max(expires - 60, 30))
    logger.info("ninja_service: token renovado via refresh_token.")
    return access


def invalidate_token():
    cache.delete(_TOKEN_CACHE_KEY)


def revoke_token(user=None):
    """Remove os tokens do banco e limpa o cache."""
    try:
        from ProjetoEstoque.models import NinjaOAuthToken
        token_obj = NinjaOAuthToken.get()
        token_obj.access_token  = ""
        token_obj.refresh_token = ""
        token_obj.expires_at    = None
        token_obj.updated_by    = user
        token_obj.save()
    except Exception as exc:
        logger.error("ninja_service.revoke_token: %s", exc)
    finally:
        cache.delete(_TOKEN_CACHE_KEY)


# ─────────────────────────────────────────────────────────────
# HTTP helper
# ─────────────────────────────────────────────────────────────

def _api_get(path: str, params: dict | None = None) -> list | dict | None:
    import requests

    token = _get_token()
    if not token:
        return None

    _, api_base, _ = ("", getattr(settings, "NINJA_BASE_URL", "").rstrip("/"), "")
    url = f"{api_base}/api/v2{path}"
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params=params or {},
            timeout=15,
        )
        if resp.status_code == 401:
            # Token expirou — tenta refresh uma vez
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
        logger.error("ninja_service._api_get %s: %s", path, exc)
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
            items = data.get("devices") or data.get("data") or data.get("results") or []
            results.extend(items)
            cursor = data.get("cursor") or data.get("nextCursor")
            if not cursor or not items:
                break
            after = cursor
        else:
            break
    return results


# ─────────────────────────────────────────────────────────────
# Endpoints de dados
# ─────────────────────────────────────────────────────────────

def get_devices() -> list[dict]:
    cached = cache.get("ninja_devices_v3")
    if cached is not None:
        return cached
    devices = _paginate("/devices")
    if devices:
        cache.set("ninja_devices_v3", devices, 120)
    return devices


def get_device_system_info(device_id: int) -> dict:
    for path in [
        f"/device/{device_id}/windows/system-info",
        f"/device/{device_id}/system-info",
    ]:
        data = _api_get(path)
        if isinstance(data, dict) and data:
            return data
    data = _api_get(f"/device/{device_id}")
    if isinstance(data, dict):
        return data.get("system") or data.get("systemInfo") or {}
    return {}


def get_device_last_user(device_id: int) -> str:
    data = _api_get(f"/device/{device_id}/last-logged-on-user")
    if isinstance(data, dict):
        user = (
            data.get("lastLoggedOnUser") or
            data.get("username") or
            data.get("user") or ""
        )
        return _clean_user(str(user))
    if isinstance(data, str):
        return _clean_user(data)
    return ""


def invalidate_devices_cache():
    cache.delete("ninja_devices_v3")


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
    refs     = d.get("references") or {}
    org_ref  = refs.get("organization") or {}
    org_name = org_ref.get("name") or ""
    is_online = not d.get("offline", True)
    hostname  = d.get("dnsName") or d.get("netbiosName") or d.get("systemName") or ""
    display_name = d.get("displayName") or d.get("systemName") or f"Device #{d.get('id','?')}"
    return {
        "ninja_id":        d.get("id"),
        "display_name":    display_name,
        "hostname":        hostname,
        "serial_number":   "",
        "os_name":         "",
        "manufacturer":    "",
        "model_name":      "",
        "processor":       "",
        "total_memory_mb": None,
        "ip_address":      "",
        "last_contact":    _parse_timestamp(d.get("lastContact")),
        "is_online":       is_online,
        "last_user":       "",
        "node_class":      d.get("nodeClass") or "",
        "organization_name": org_name,
        "org_id":          d.get("organizationId"),
    }


def _parse_system_info(raw: dict) -> dict:
    data   = raw.get("system") or raw
    serial = _clean_serial(
        data.get("biosSerialNumber") or data.get("serialNumber") or
        raw.get("serialNumber") or ""
    )
    mem_raw = data.get("totalPhysicalMemory") or data.get("physicalMemory") or 0
    mem_mb  = int(mem_raw / (1024 * 1024)) if mem_raw > 1_000_000 else (int(mem_raw) if mem_raw > 0 else None)
    ips    = data.get("ipAddresses") or raw.get("ipAddresses") or []
    ip     = ips[0] if isinstance(ips, list) and ips else (data.get("ipAddress") or raw.get("ipAddress") or "")
    os_name = (data.get("os") or {}).get("name") or data.get("osName") or raw.get("osName") or ""
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
    Sincroniza dispositivos NinjaOne -> NinjaDevice.
    Requer autenticacao previa via /ninja/autorizar/.
    """
    from ProjetoEstoque.models import NinjaDevice, Item
    from django.utils import timezone

    if not is_authenticated():
        logger.error("ninja_service.sync_devices: nao autenticado.")
        return {"synced": 0, "matched": 0, "online": 0, "error": True,
                "error_msg": "Nao autenticado. Acesse /ninja/autorizar/ primeiro."}

    raw_devices = get_devices()
    if not raw_devices:
        return {"synced": 0, "matched": 0, "online": 0, "error": True,
                "error_msg": "API nao retornou dispositivos."}

    synced = matched = online = 0
    now = timezone.now()

    for raw in raw_devices:
        parsed   = _parse_device_basic(raw)
        ninja_id = parsed.get("ninja_id")
        if not ninja_id:
            continue

        sys_info_raw = get_device_system_info(ninja_id)
        hw           = _parse_system_info(sys_info_raw) if sys_info_raw else {}
        last_user    = get_device_last_user(ninja_id)

        parsed.update({k: v for k, v in hw.items() if v})
        if last_user:
            parsed["last_user"] = last_user

        serial = parsed.get("serial_number", "")
        parsed.pop("org_id", None)

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
    return len(snapshots)


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
