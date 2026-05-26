"""
prtg_service.py — Proxy seguro para a API REST do PRTG Network Monitor.

Regras de uso:
  - Credenciais lidas EXCLUSIVAMENTE de settings.PRTG_URL/USER/PASSHASH.
  - Nunca instanciar requests.get() fora deste módulo.
  - Em caso de falha, retorna estrutura vazia — o caller decide como degradar.
  - Cache de 30s via Django cache backend.

Arquitetura de status (dois níveis):
  - Nível de device: status agregado de todos os sensores do dispositivo.
  - Nível de sensor: status individual de cada sensor (ping, SNMP, etc.).
  O status efetivo exibido nas plantas é o PIOR entre os dois, garantindo que
  "ping down" apareça como offline mesmo quando o device-aggregate ainda é "up".
"""
import logging
import urllib3
import requests
from django.conf import settings
from django.core.cache import cache

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

_CACHE_KEY_DEVICES = "prtg_devices_v2"
_CACHE_KEY_PING    = "prtg_ping_sensors_v2"
_CACHE_TTL = 30  # segundos

# Mapeamento texto de status → código numérico (fallback quando status_raw ausente)
_TEXT_TO_INT: dict[str, int] = {
    "up": 3, "warning": 4, "down": 5, "no probe": 6,
    "paused": 7, "unusual": 10, "unknown": 1, "collecting": 2,
}

# Mapeamento numérico de status PRTG → slug textual
_STATUS_MAP: dict[int, str] = {
    1:  "unknown",
    2:  "collecting",
    3:  "up",
    4:  "warning",
    5:  "down",
    6:  "no_probe",
    7:  "paused_by_user",
    8:  "paused_by_dep",
    9:  "paused_by_sched",
    10: "unusual",
    11: "not_licensed",
    12: "paused_until",
}

# Severidade de cada status para comparação (maior = pior)
# down(5) > warning(4) > unusual(10) > unknown(1) > collecting/paused > up(3)
_SEVERITY: dict[int, int] = {
    3:  1,   # up
    2:  2,   # collecting
    1:  3,   # unknown
    6:  3,   # no probe
    7:  3,   # paused by user
    8:  3,   # paused by dependency
    9:  3,   # paused by schedule
    11: 3,   # not licensed
    12: 3,   # paused until
    10: 4,   # unusual
    4:  5,   # warning
    5:  6,   # down — pior de todos
}

# Cores CSS para cada status
STATUS_CSS: dict[str, str] = {
    "up":            "var(--success, #34c759)",
    "warning":       "var(--warning, #ff9500)",
    "down":          "var(--danger,  #ff3b30)",
    "unusual":       "var(--warning, #ff9500)",
    "unknown":       "var(--text-tertiary, #6e6e73)",
    "collecting":    "var(--info,    #5ac8fa)",
    # Pausado por dependência — azul neon para indicar que o upstream está down
    "paused_by_dep": "#00cfff",
    # Demais estados de pausa — cinza neutro
    "paused_by_user":  "var(--text-tertiary, #6e6e73)",
    "paused_by_sched": "var(--text-tertiary, #6e6e73)",
    "paused_until":    "var(--text-tertiary, #6e6e73)",
    "no_probe":        "var(--text-tertiary, #6e6e73)",
    "not_licensed":    "var(--text-tertiary, #6e6e73)",
}

# Palavras-chave que identificam sensores de ping/ICMP
_PING_KEYWORDS = frozenset(("ping", "icmp"))


def _get_credentials() -> tuple[str, str, str]:
    base = getattr(settings, "PRTG_URL", "").rstrip("/")
    user = getattr(settings, "PRTG_USER", "")
    ph   = getattr(settings, "PRTG_PASSHASH", "")
    return base, user, ph


def _prtg_get(params: dict) -> dict:
    """Executa chamada HTTP ao PRTG com timeout de 5s e SSL interno."""
    base, user, ph = _get_credentials()
    if not all([base, user, ph]):
        logger.warning("prtg_service: PRTG_URL/USER/PASSHASH não configurados.")
        return {}
    try:
        response = requests.get(
            f"{base}/api/table.json",
            params={**params, "username": user, "passhash": ph, "output": "json"},
            timeout=5,
            verify=False,  # certificado self-signed — rede interna corporativa
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.Timeout:
        logger.warning("prtg_service: timeout ao consultar PRTG (%s).", base)
        return {}
    except requests.exceptions.ConnectionError:
        logger.warning("prtg_service: PRTG inacessível em %s.", base)
        return {}
    except Exception as exc:
        logger.error("prtg_service: erro inesperado — %s", exc)
        return {}


def _status_int(d: dict) -> int:
    """
    Extrai status numérico de um objeto PRTG (device ou sensor).
    Prioridade: status_raw (explícito) → int(status) → mapeamento de texto.
    """
    raw = d.get("status_raw")
    if raw is not None:
        try:
            return int(float(raw))
        except (ValueError, TypeError):
            pass
    status = d.get("status", 0)
    try:
        return int(float(status))
    except (ValueError, TypeError):
        # PRTG pode retornar "Down (Ping)", "Warning", "Up", etc.
        normalized = str(status).lower().strip().split("(")[0].strip()
        return _TEXT_TO_INT.get(normalized, 0)


def _is_ping_sensor(s: dict) -> bool:
    """Identifica se um sensor PRTG é de ping ou ICMP."""
    stype = str(s.get("sensortype", "")).lower()
    name  = str(s.get("name", "")).lower().strip()
    return (
        any(kw in stype for kw in _PING_KEYWORDS)
        or name in ("ping", "icmp ping", "ping test", "ping monitor", "icmp", "ping sensor")
    )


def get_devices() -> list[dict]:
    """
    Retorna todos os devices do PRTG com campos de status.
    Cache de 30s para minimizar chamadas ao PRTG.
    """
    cached = cache.get(_CACHE_KEY_DEVICES)
    if cached is not None:
        return cached
    data = _prtg_get({
        "content": "devices",
        "columns": "objid,name,host,status,status_raw,statustext,group,active",
    })
    devices = data.get("devices", [])
    cache.set(_CACHE_KEY_DEVICES, devices, _CACHE_TTL)
    return devices


def get_ping_status_by_device() -> dict[int, int]:
    """
    Retorna {device_objid: ping_sensor_status_int} para todos os devices que
    possuem sensor de ping ou ICMP cadastrado no PRTG.

    Permite detectar "ping down" mesmo quando o status de device-level ainda
    aparece como "up" (situação comum quando o sensor de ping não é o sensor
    raiz de dependência do device no PRTG).

    Cache de 30s — mesma janela dos devices para consistência.
    """
    cached = cache.get(_CACHE_KEY_PING)
    if cached is not None:
        return cached

    data = _prtg_get({
        "content": "sensors",
        "columns": "objid,name,status,status_raw,parentid,parentid_raw,sensortype",
        "count":   "5000",
    })

    result: dict[int, int] = {}
    for s in data.get("sensors", []):
        if not _is_ping_sensor(s):
            continue
        try:
            pid = int(float(s.get("parentid_raw") or s.get("parentid") or 0))
        except (ValueError, TypeError):
            continue
        if pid <= 0:
            continue
        st = _status_int(s)
        # Guarda o pior status de ping encontrado para este device
        if _SEVERITY.get(st, 0) > _SEVERITY.get(result.get(pid, 0), 0):
            result[pid] = st

    cache.set(_CACHE_KEY_PING, result, _CACHE_TTL)
    return result


def get_devices_map() -> dict[int, dict]:
    """
    Retorna dict { objid(int): device_dict } para lookup O(1) no viewer.

    O campo 'status' reflete o STATUS EFETIVO: o pior entre o status de
    device-level e o status do sensor de ping. Isso garante que um device
    cujo ping sensor está "Down" seja exibido como offline nas plantas,
    mesmo que o PRTG ainda marque o device como "Up" em nível agregado.

    Campos adicionais:
      device_status — status de device-level bruto do PRTG
      ping_status   — status do sensor de ping (None se não encontrado)
    """
    ping_by_device = get_ping_status_by_device()

    result = {}
    for d in get_devices():
        try:
            oid = int(d.get("objid_raw") or d["objid"])
        except (KeyError, ValueError, TypeError):
            continue

        device_status = _status_int(d)
        ping_status   = ping_by_device.get(oid)  # None quando não há sensor de ping

        # Status efetivo = pior de (device, ping sensor)
        if ping_status is not None and _SEVERITY.get(ping_status, 0) > _SEVERITY.get(device_status, 0):
            effective = ping_status
        else:
            effective = device_status

        slug = _STATUS_MAP.get(effective, "unknown")
        result[oid] = {
            "objid":         oid,
            "name":          d.get("name", ""),
            "host":          d.get("host", ""),
            "group":         d.get("group", ""),
            "status":        effective,       # usado pelo canvas para cor/status
            "device_status": device_status,   # nível de device (agregado PRTG)
            "ping_status":   ping_status,     # sensor de ping (None = sem sensor)
            "status_slug":   slug,
            "statustext":    d.get("statustext", ""),
            "css_color":     STATUS_CSS.get(slug, STATUS_CSS["unknown"]),
        }
    return result


def search_devices(query: str) -> list[dict]:
    """
    Busca devices por nome ou IP — usado no autocomplete do editor de plantas.
    Reutiliza o cache de get_devices(); não faz nova chamada ao PRTG.
    Retorna até 20 resultados.
    """
    q = query.strip().lower()
    if len(q) < 2:
        return []
    matches = []
    for d in get_devices():
        name = d.get("name", "").lower()
        host = d.get("host", "").lower()
        if q in name or q in host:
            status_int = _status_int(d)
            slug = _STATUS_MAP.get(status_int, "unknown")
            matches.append({
                "objid":       d.get("objid"),
                "name":        d.get("name", ""),
                "host":        d.get("host", ""),
                "group":       d.get("group", ""),
                "status_slug": slug,
                "statustext":  d.get("statustext", ""),
            })
        if len(matches) >= 20:
            break
    return matches


def status_slug(status_int: int) -> str:
    """Converte código numérico de status PRTG em slug textual."""
    return _STATUS_MAP.get(status_int, "unknown")


def is_configured() -> bool:
    """Verifica se as credenciais PRTG estão definidas."""
    base, user, ph = _get_credentials()
    return bool(base and user and ph)
