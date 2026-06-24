"""
quiosque_service.py — Lógica do módulo Quiosque (integração com o app Android).

Centraliza enrollment, autenticação por token, registro de check-in (telemetria) e
montagem da configuração enviada ao device. As views (API e dashboard) apenas
chamam este serviço — nenhuma regra de negócio fica nas views ou templates.

Segurança:
  - O token do device é gerado aleatório (secrets) e só o SHA-256 é persistido.
  - O PIN do TI é guardado como hash PBKDF2 (Django make_password) — o app valida
    o PIN offline comparando o hash recebido na config.
  - O código de matrícula é de uso único e protege o enroll.
"""
import hashlib
import secrets
import string
from datetime import datetime, timedelta

from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.contrib.auth.hashers import make_password
from django.core.exceptions import ValidationError
from django.db.models.functions import Coalesce


# Retenção da telemetria (check-ins) por aparelho — janela móvel em dias.
# Após este prazo os dados antigos são sobrepostos pelos novos. A limpeza só roda
# QUANDO o aparelho faz check-in; logo, um aparelho que parou de enviar conserva
# todo o seu histórico (fica guardado como histórico do dispositivo).
RETENCAO_DIAS = 5


class EnrollConflict(Exception):
    """Código de matrícula já vinculado a OUTRO aparelho (resposta HTTP 409)."""


# ──────────────────────────────────────────────────────────────────────────────
# Helpers de token / código
# ──────────────────────────────────────────────────────────────────────────────

def hash_token(token: str) -> str:
    """SHA-256 hex de um token (para guardar/comparar sem expor o token puro)."""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def gerar_token() -> str:
    """Token opaco do device (enviado uma única vez, no enroll)."""
    return "tok_" + secrets.token_urlsafe(36)


def gerar_codigo_matricula(n: int = 8) -> str:
    """Código curto, legível, sem caracteres ambíguos (0/O, 1/I)."""
    alfabeto = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alfabeto) for _ in range(n))


def criar_matricula(*, descricao: str = "", validade_horas: int = 72, user=None):
    """Cria uma KioskMatricula de uso único (código exclusivo)."""
    from ProjetoEstoque.models import KioskMatricula

    codigo = gerar_codigo_matricula()
    while KioskMatricula.objects.filter(codigo=codigo).exists():
        codigo = gerar_codigo_matricula()

    expira = timezone.now() + timezone.timedelta(hours=validade_horas) if validade_horas else None
    return KioskMatricula.objects.create(
        codigo=codigo, descricao=descricao or "", expira_em=expira, criado_por=user,
    )


def definir_pin(device, pin: str) -> None:
    """Define o PIN do TI no device (guardado como hash PBKDF2). Bumpa a config."""
    device.admin_pin_hash = make_password(str(pin)) if pin else ""
    device.config_versao = (device.config_versao or 1) + 1
    device.save(update_fields=["admin_pin_hash", "config_versao", "atualizado_em"])


# ──────────────────────────────────────────────────────────────────────────────
# Configuração enviada ao device
# ──────────────────────────────────────────────────────────────────────────────

def config_dict(device) -> dict:
    """Monta o objeto `config` que o app aplica (apps liberados, Wi-Fi, PIN, etc.)."""
    return {
        "intervalo_checkin_seg": device.intervalo_checkin_seg,
        "wifi_only": device.wifi_only,
        "apps_permitidos": device.apps_permitidos or [],
        "admin_pin_hash": device.admin_pin_hash or "",
        "mensagem_quiosque": device.mensagem_quiosque or "",
        "config_versao": device.config_versao,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Enrollment
# ──────────────────────────────────────────────────────────────────────────────

def enroll(*, codigo_matricula: str, dados: dict) -> dict:
    """
    Matricula um aparelho a partir de um código de matrícula. A CHAVE do vínculo é o
    `android_id` (estável por aparelho). Retorna {device, token (puro, 1x), config}.

    Regras (contrato do app):
      1. Código livre → vincula ao android_id; reaproveita/cria o registro do aparelho.
      2. Código já usado pelo MESMO android_id → reuso, devolve o MESMO device_uuid.
      3. Código usado por android_id DIFERENTE → EnrollConflict (HTTP 409).

    Lança ValueError em erro de regra simples (400) e EnrollConflict em vínculo (409).
    """
    from ProjetoEstoque.models import KioskMatricula, KioskDevice

    codigo = (codigo_matricula or "").strip().upper()
    if not codigo:
        raise ValueError("Código de matrícula é obrigatório.")

    matricula = KioskMatricula.objects.select_related("device").filter(codigo=codigo).first()
    if matricula is None:
        raise ValueError("Código de matrícula inválido.")

    serial = (dados.get("serial") or "").strip()
    android_id = (dados.get("android_id") or "").strip()

    if matricula.usado:
        vinc = matricula.device
        if vinc is None:
            raise ValueError("Código de matrícula já utilizado.")
        # Mesmo aparelho rematriculando → reuso (preserva device_uuid e histórico)
        if android_id and vinc.android_id and vinc.android_id == android_id:
            device = vinc
        elif not android_id and serial and vinc.serial and vinc.serial == serial:
            device = vinc
        else:
            raise EnrollConflict("Código já vinculado a outro dispositivo.")
    else:
        if not matricula.esta_valida():
            raise ValueError("Código de matrícula expirado.")
        # Código livre: reaproveita o registro do MESMO aparelho (preserva histórico)
        device = None
        if android_id:
            device = KioskDevice.objects.filter(android_id=android_id).first()
        if device is None and serial:
            device = KioskDevice.objects.filter(serial=serial).first()
        if device is None:
            device = KioskDevice()

    # (Re)emite o token e atualiza a identificação do aparelho
    token = gerar_token()
    device.token_hash = hash_token(token)
    if serial:
        device.serial = serial
    if android_id:
        device.android_id = android_id
    device.fabricante = (dados.get("fabricante") or device.fabricante or "")[:80]
    device.modelo = (dados.get("modelo") or device.modelo or "")[:120]
    device.android_versao = str(dados.get("android_versao") or device.android_versao or "")[:20]
    device.app_versao = str(dados.get("app_versao") or device.app_versao or "")[:20]
    ram = _i(dados.get("ram_mb"))
    if ram is not None:
        device.ram_mb = ram
    device.ativo = True
    if not device.apelido:
        device.apelido = device.modelo or "Quiosque"
    device.save()

    if not matricula.usado or matricula.device_id != device.pk:
        matricula.usado = True
        matricula.usado_em = timezone.now()
        matricula.device = device
        matricula.save(update_fields=["usado", "usado_em", "device"])

    return {"device": device, "token": token, "config": config_dict(device)}


# ──────────────────────────────────────────────────────────────────────────────
# Autenticação por token
# ──────────────────────────────────────────────────────────────────────────────

def autenticar(token: str, device_uuid: str):
    """Resolve o KioskDevice ativo a partir do token (e do uuid). None se inválido."""
    from ProjetoEstoque.models import KioskDevice

    if not token or not device_uuid:
        return None
    try:
        device = KioskDevice.objects.get(device_uuid=device_uuid, ativo=True)
    except (KioskDevice.DoesNotExist, ValueError, ValidationError):
        return None
    if not secrets.compare_digest(device.token_hash, hash_token(token)):
        return None
    return device


# ──────────────────────────────────────────────────────────────────────────────
# Check-in (telemetria) + comandos pendentes
# ──────────────────────────────────────────────────────────────────────────────

def _f(v):
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _i(v):
    try:
        return int(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _parse_dt(v):
    """Converte 'coletado_em' (ISO 8601 com fuso) para datetime aware. None se inválido."""
    if not v:
        return None
    dt = v if isinstance(v, datetime) else parse_datetime(str(v))
    if dt is None:
        return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def prune_checkins(device) -> int:
    """
    Mantém apenas a janela móvel de RETENCAO_DIAS de telemetria do aparelho.

    Roda no check-in: aparelhos ativos giram uma janela de 5 dias; um aparelho que
    PAROU de enviar nunca é podado, então conserva todo o histórico já recebido.
    """
    from ProjetoEstoque.models import KioskCheckin

    cutoff = timezone.now() - timedelta(days=RETENCAO_DIAS)
    apagados, _ = (
        KioskCheckin.objects
        .filter(device=device)
        .annotate(ts=Coalesce("coletado_em", "registrado_em"))
        .filter(ts__lt=cutoff)
        .delete()
    )
    return apagados


def registrar_checkin(device, dados: dict) -> dict:
    """
    Grava um KioskCheckin, atualiza o estado mais recente do device e devolve a
    resposta para o app: config (se a versão mudou) e comandos pendentes.

    Suporta fila offline: o app pode enviar leituras com `coletado_em` no passado.
    Cada leitura vira uma linha de histórico; o "estado atual" só é atualizado
    quando a leitura é a mais recente já vista (não regride com dados antigos).
    """
    from ProjetoEstoque.models import KioskCheckin, KioskComando

    lat = _f(dados.get("latitude"))
    lon = _f(dados.get("longitude"))
    prec = _f(dados.get("precisao_m"))
    bat = _i(dados.get("bateria"))
    rede = (dados.get("rede") or "")[:20]
    online = bool(dados.get("online", True))
    carregando = bool(dados.get("carregando", False))
    coletado = _parse_dt(dados.get("coletado_em")) or timezone.now()
    serial = (dados.get("serial") or "").strip()

    KioskCheckin.objects.create(
        device=device, latitude=lat, longitude=lon, precisao_m=prec,
        bateria=bat, carregando=carregando, rede=rede, online=online,
        coletado_em=coletado,
    )

    eh_mais_recente = device.ultimo_checkin is None or coletado >= device.ultimo_checkin
    if eh_mais_recente:
        device.ultima_latitude = lat if lat is not None else device.ultima_latitude
        device.ultima_longitude = lon if lon is not None else device.ultima_longitude
        device.ultima_precisao_m = prec if prec is not None else device.ultima_precisao_m
        device.ultima_bateria = bat if bat is not None else device.ultima_bateria
        device.ultima_rede = rede or device.ultima_rede
        device.ultimo_checkin = coletado
        if serial and not device.serial:
            device.serial = serial
    if dados.get("app_versao"):
        device.app_versao = str(dados.get("app_versao"))[:20]
    device.save()

    # Retenção: janela móvel de 5 dias (só poda aparelhos que continuam enviando)
    prune_checkins(device)

    # Comandos pendentes → marca como entregues
    pendentes = list(device.comandos.filter(status=KioskComando.Status.PENDENTE).order_by("criado_em"))
    comandos = [{"id": c.id, "tipo": c.tipo, "payload": c.payload or {}} for c in pendentes]
    if pendentes:
        agora = timezone.now()
        for c in pendentes:
            c.status = KioskComando.Status.ENTREGUE
            c.entregue_em = agora
        KioskComando.objects.bulk_update(pendentes, ["status", "entregue_em"])

    # Config só vai de volta se o device estiver desatualizado
    try:
        cfg_device = int(dados.get("config_versao")) if dados.get("config_versao") is not None else None
    except (TypeError, ValueError):
        cfg_device = None
    config = config_dict(device) if (cfg_device is None or cfg_device < device.config_versao) else None

    return {"ok": True, "config_versao": device.config_versao, "config": config, "comandos": comandos}


def registrar_ack_comando(device, comando_id, status: str, detalhe: str = "") -> bool:
    """O device confirma a execução (ou falha) de um comando."""
    from ProjetoEstoque.models import KioskComando

    c = device.comandos.filter(id=comando_id).first()
    if c is None:
        return False
    if status not in (KioskComando.Status.EXECUTADO, KioskComando.Status.FALHOU):
        status = KioskComando.Status.EXECUTADO
    c.status = status
    c.detalhe = (detalhe or "")[:255]
    c.finalizado_em = timezone.now()
    c.save(update_fields=["status", "detalhe", "finalizado_em"])
    return True
