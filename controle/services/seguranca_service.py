"""
Monitoramento de segurança de autenticação
(ISO 27001 A.8.15 Registro de eventos / A.8.16 Monitoramento).

Captura login, logout e falha de login via signals nativos do Django, grava em
`RegistroSeguranca` (com IP, agente e caminho) e detecta anomalias de baixo
ruído:

  • rajada de falhas: >= _LIMITE_FALHAS falhas do mesmo IP OU usuário dentro de
    _JANELA_MIN minutos  → força bruta;
  • login bem-sucedido logo após uma rajada de falhas → possível invasão.

Eventos suspeitos disparam alerta por e-mail (fire-and-forget, via
transaction.on_commit) reaproveitando services/email_alertas.py.

Conectado em ProjetoEstoque/apps.py (ready()); importar o módulo já conecta os
receivers.
"""
import logging
from datetime import timedelta

from django.contrib.auth.signals import (
    user_logged_in,
    user_logged_out,
    user_login_failed,
)
from django.db import transaction
from django.db.models import Q
from django.dispatch import receiver
from django.utils import timezone

logger = logging.getLogger("seguranca")

# Rajada: N falhas do mesmo IP/username em JANELA minutos => suspeito.
_LIMITE_FALHAS = 5
_JANELA_MIN = 15


# ── Extração de contexto da request ──────────────────────────────────────────
def _ip(request):
    if request is None:
        return None
    # Respeita proxy reverso (X-Forwarded-For); cai para REMOTE_ADDR.
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[0].strip()[:45] or None
    return request.META.get("REMOTE_ADDR")


def _agente(request):
    if request is None:
        return ""
    return (request.META.get("HTTP_USER_AGENT", "") or "")[:400]


def _caminho(request):
    if request is None:
        return ""
    return (getattr(request, "path", "") or "")[:200]


def _registrar(tipo, *, username="", usuario=None, request=None, suspeito=False, detalhe=""):
    """Cria um RegistroSeguranca. Nunca propaga exceção — monitoramento não pode
    quebrar o fluxo de autenticação."""
    from ProjetoEstoque.models import RegistroSeguranca
    try:
        return RegistroSeguranca.objects.create(
            tipo=tipo,
            username=(username or "")[:254],
            usuario=usuario,
            ip=_ip(request),
            user_agent=_agente(request),
            caminho=_caminho(request),
            suspeito=suspeito,
            detalhe=(detalhe or "")[:200],
        )
    except Exception:
        logger.warning("Falha ao registrar evento de segurança", exc_info=True)
        return None


def _falhas_recentes(username, ip):
    """Quantidade de falhas de login do mesmo usuário OU IP na janela."""
    from ProjetoEstoque.models import RegistroSeguranca
    desde = timezone.now() - timedelta(minutes=_JANELA_MIN)
    filtro = Q()
    if username:
        filtro |= Q(username=username)
    if ip:
        filtro |= Q(ip=ip)
    if not filtro:
        return 0
    return RegistroSeguranca.objects.filter(
        tipo="login_falha", criado_em__gte=desde,
    ).filter(filtro).count()


def _disparar_alerta(evento):
    """Envia alerta de acesso suspeito após o commit (fire-and-forget)."""
    def _envia():
        try:
            from services.email_alertas import alerta_acesso_suspeito
            alerta_acesso_suspeito(evento)
        except Exception:
            logger.warning("Falha ao enviar alerta de acesso suspeito", exc_info=True)
    try:
        transaction.on_commit(_envia)
    except Exception:
        _envia()


# ── Receivers ────────────────────────────────────────────────────────────────
@receiver(user_logged_in)
def _on_login(sender, request, user, **kwargs):
    username = getattr(user, "username", "") or ""
    ip = _ip(request)
    falhas = _falhas_recentes(username, ip)
    suspeito = falhas >= _LIMITE_FALHAS
    detalhe = f"Login após {falhas} falhas em {_JANELA_MIN} min" if suspeito else ""
    logger.info("login_ok user=%s ip=%s", username, ip)
    ev = _registrar(
        "login_ok", username=username, usuario=user,
        request=request, suspeito=suspeito, detalhe=detalhe,
    )
    if suspeito and ev:
        _disparar_alerta(ev)


@receiver(user_logged_out)
def _on_logout(sender, request, user, **kwargs):
    _registrar(
        "logout",
        username=getattr(user, "username", "") or "",
        usuario=user,
        request=request,
    )


@receiver(user_login_failed)
def _on_login_failed(sender, credentials, request=None, **kwargs):
    username = (credentials or {}).get("username", "") or ""
    ip = _ip(request)
    # +1 conta esta tentativa que ainda não foi gravada.
    total = _falhas_recentes(username, ip) + 1
    suspeito = total >= _LIMITE_FALHAS
    detalhe = f"{total} falhas em {_JANELA_MIN} min" if suspeito else ""
    logger.warning("login_falha user=%s ip=%s suspeito=%s", username, ip, suspeito)
    ev = _registrar(
        "login_falha", username=username,
        request=request, suspeito=suspeito, detalhe=detalhe,
    )
    if suspeito and ev:
        _disparar_alerta(ev)
