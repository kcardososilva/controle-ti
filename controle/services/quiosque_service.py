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
import random
import secrets
import string
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.db import transaction
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

# Probabilidade de rodar a poda em cada check-in. A poda é uma janela de 5 dias e
# NÃO precisa rodar a cada heartbeat — com o app em ~5s isso seria um DELETE-scan
# contínuo. Rodando de forma amostrada (~1 a cada 50 check-ins) a tabela continua
# limitada à janela e a resposta do check-in fica leve em alta frequência.
_PRUNE_PROB = 0.02


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
# Instalador do app (.apk) — pasta protegida + link de download com token
# ──────────────────────────────────────────────────────────────────────────────
# O TI copia o .apk diretamente para settings.KIOSK_APK_DIR (fora do /media/,
# que é servido sem autenticação). A tela de matrículas detecta o arquivo e
# permite gerar um link de download com token de validade curta — o mesmo
# princípio de segurança do código de matrícula, aplicado ao instalador.

_APK_LINK_MIN_MINUTOS = 5
_APK_LINK_MAX_MINUTOS = 240  # 4h — teto de segurança mesmo que o cliente peça mais


def apk_dir() -> Path:
    """Pasta protegida do instalador. Cria se ainda não existir."""
    destino = Path(getattr(settings, "KIOSK_APK_DIR", None) or (Path(settings.BASE_DIR) / "kiosk_apk"))
    destino.mkdir(parents=True, exist_ok=True)
    return destino


def apk_atual() -> dict | None:
    """Resolve o instalador atual: o .apk mais recente (por data de modificação)
    na pasta protegida. None se nenhum .apk foi copiado ainda."""
    candidatos = sorted(
        (p for p in apk_dir().iterdir() if p.is_file() and p.suffix.lower() == ".apk"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidatos:
        return None
    p = candidatos[0]
    st = p.stat()
    return {
        "nome": p.name,
        "tamanho": st.st_size,
        "modificado_em": timezone.make_aware(datetime.fromtimestamp(st.st_mtime)),
    }


def caminho_instalador(nome_arquivo: str) -> Path | None:
    """Resolve o caminho físico de um instalador dentro da pasta protegida.

    Valida que o nome não tem separador de caminho e que o arquivo resolvido
    continua DENTRO da pasta protegida — defesa contra path traversal mesmo que
    `nome_arquivo` venha corrompido de algum jeito."""
    if not nome_arquivo or nome_arquivo != Path(nome_arquivo).name:
        return None
    base = apk_dir().resolve()
    caminho = (base / nome_arquivo).resolve()
    if caminho.parent != base or not caminho.is_file():
        return None
    return caminho


def gerar_qrcode_data_uri(conteudo: str, tamanho_px: int = 260) -> str:
    """PNG do QR Code do conteúdo informado, como data URI (embutível direto em
    <img src="...">). Usa o gerador de QR já embutido no reportlab (dependência
    já existente no projeto para os PDFs) — evita adicionar uma lib nova só
    para isto."""
    import base64
    import io

    from reportlab.graphics import renderPM
    from reportlab.graphics.barcode.qr import QrCodeWidget
    from reportlab.graphics.shapes import Drawing

    qr = QrCodeWidget(conteudo)
    x0, y0, x1, y1 = qr.getBounds()
    largura, altura = (x1 - x0), (y1 - y0)
    desenho = Drawing(tamanho_px, tamanho_px, transform=[tamanho_px / largura, 0, 0, tamanho_px / altura, 0, 0])
    desenho.add(qr)
    buf = io.BytesIO()
    renderPM.drawToFile(desenho, buf, fmt="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def gerar_link_instalador(*, validade_minutos: int, user, request) -> dict:
    """Gera um link de instalação de uso temporário para o .apk atual.

    O token puro só existe nesta resposta (o banco guarda apenas o hash) — por
    isso o QR Code e a URL devem ser exibidos uma única vez, no momento da
    geração. Lança ValueError se não houver nenhum .apk na pasta protegida.
    """
    from django.urls import reverse

    from ProjetoEstoque.models import KioskInstaladorLink

    atual = apk_atual()
    if atual is None:
        raise ValueError("Nenhum instalador (.apk) encontrado na pasta do servidor.")

    validade_minutos = min(max(int(validade_minutos or 30), _APK_LINK_MIN_MINUTOS), _APK_LINK_MAX_MINUTOS)
    token = secrets.token_urlsafe(32)
    link = KioskInstaladorLink.objects.create(
        token_hash=hash_token(token),
        nome_arquivo=atual["nome"],
        expira_em=timezone.now() + timedelta(minutes=validade_minutos),
        criado_por=user,
    )
    url_absoluta = request.build_absolute_uri(reverse("kiosk_instalador_download", args=[token]))
    return {
        "link": link,
        "url": url_absoluta,
        "qr_base64": gerar_qrcode_data_uri(url_absoluta),
        "validade_minutos": validade_minutos,
    }


def resolver_instalador(token: str):
    """Resolve um KioskInstaladorLink válido (não revogado, não expirado) a
    partir do token puro da URL. Varre só os links atualmente válidos (poucos,
    validade curta) e compara em tempo constante — mesmo padrão do token de
    device. None se inválido/expirado/revogado."""
    from ProjetoEstoque.models import KioskInstaladorLink

    if not token:
        return None
    alvo = hash_token(token)
    for link in KioskInstaladorLink.objects.filter(revogado=False, expira_em__gt=timezone.now()):
        if secrets.compare_digest(link.token_hash, alvo):
            return link
    return None


def registrar_download_instalador(link, ip: str | None) -> None:
    """Contabiliza um download do instalador (auditoria — quem/quando/de onde)."""
    link.downloads = (link.downloads or 0) + 1
    link.ultimo_download_em = timezone.now()
    link.ultimo_download_ip = ip or None
    link.save(update_fields=["downloads", "ultimo_download_em", "ultimo_download_ip"])


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


# Inventário de apps: limites defensivos (o payload vem do device — não confiável).
_APPS_MAX      = 500   # teto de itens aceitos por inventário (folga sobre ~40–120)
_APPS_PKG_MAX  = 255
_APPS_NOME_MAX = 255


def _persistir_inventario(device, apps, apps_hash) -> bool:
    """
    Substitui o inventário de apps do device pela lista recebida no check-in.

    Contrato (ver docs/INFORME): o inventário vem no `/checkin/` **só quando muda**.

      • Ausência de `apps_instalados` ≠ "zero apps" → é "sem novidade": NÃO mexe no
        inventário guardado (a maioria dos check-ins não traz a lista).
      • Lista presente é sempre real e não-vazia; uma lista vazia é ignorada (o app
        nunca envia vazio — proteção extra contra apagar o inventário por engano).
      • Deduplica pelo `apps_hash`: se igual ao guardado, é reenvio (at-least-once)
        da fila offline → ignora.
      • Dados não-confiáveis: valida tipos, limita tamanhos e descarta itens
        malformados. A chave é o `pkg` (o `nome` é só exibição).

    Devolve True se o inventário foi de fato substituído (o chamador salva o device).
    """
    from ProjetoEstoque.models import KioskDeviceApp

    if not isinstance(apps, list) or not apps:
        return False

    novo_hash = str(apps_hash)[:64] if apps_hash else ''
    # Reenvio do mesmo inventário (garantia at-least-once da fila) → nada mudou.
    if novo_hash and device.apps_hash and novo_hash == device.apps_hash:
        return False

    registros, vistos = [], set()
    for it in apps:
        if not isinstance(it, dict):
            continue
        pkg = str(it.get('pkg') or '').strip()[:_APPS_PKG_MAX]
        if not pkg or pkg in vistos:
            continue
        vistos.add(pkg)
        registros.append(KioskDeviceApp(
            device=device,
            pkg=pkg,
            nome=str(it.get('nome') or '')[:_APPS_NOME_MAX],
            sistema=bool(it.get('sistema', False)),
        ))
        if len(registros) >= _APPS_MAX:
            break

    # Lista veio, mas toda malformada → não apaga o inventário válido já guardado.
    if not registros:
        return False

    # A lista completa vem inteira: substituição total é a mais simples e correta.
    device.apps.all().delete()
    KioskDeviceApp.objects.bulk_create(registros)
    device.apps_hash = novo_hash
    device.apps_atualizado_em = timezone.now()
    return True


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
    # ssid = estado do momento (vai na linha do check-in); mac = identidade estável (vai no device).
    # Ambos opcionais/anuláveis: o app pode mandar null (emulador/sem Wi-Fi). Nunca exigir.
    ssid = (dados.get("ssid") or None)
    if ssid:
        ssid = str(ssid)[:64]
    mac = (dados.get("mac") or "").strip()[:17] or None

    # Tudo num único bloco atômico: a 5s de intervalo isso reduz commits/locks no
    # SQLite (1 transação por check-in em vez de várias autocommit em série).
    with transaction.atomic():
        KioskCheckin.objects.create(
            device=device, latitude=lat, longitude=lon, precisao_m=prec,
            bateria=bat, carregando=carregando, rede=rede, online=online,
            ssid=ssid, coletado_em=coletado,
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
        # MAC: identidade estável do aparelho → atualiza só quando chega valor não-nulo
        # (não sobrescreve um MAC bom com null vindo de um check-in sem Device Owner).
        if mac and device.mac != mac:
            device.mac = mac
        # Inventário de apps: presente só nos ciclos em que a lista mudou. Ausência
        # não altera o inventário guardado (ver _persistir_inventario).
        _persistir_inventario(device, dados.get("apps_instalados"), dados.get("apps_hash"))
        if dados.get("app_versao"):
            device.app_versao = str(dados.get("app_versao"))[:20]
        device.save()

        # Retenção: janela móvel de 5 dias, podada de forma amostrada (ver _PRUNE_PROB)
        # — não roda a cada heartbeat para manter a resposta leve em alta frequência.
        if random.random() < _PRUNE_PROB:
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


# ──────────────────────────────────────────────────────────────────────────────
# Trilha de localização (traço de rota no mapa do detalhe)
# ──────────────────────────────────────────────────────────────────────────────

# Precisão (m) acima da qual um fix é considerado ruim e fica FORA do traço de
# rota. Fixes por Wi-Fi/torre chegam com precisao_m alta e esticam a linha para
# longe; descartá-los deixa o traço fiel ao caminho real percorrido.
TRILHA_PRECISAO_MAX_M = 80.0
# Velocidade (km/h) impossível entre dois pontos → descarta o ponto como glitch
# de GPS ("teletransporte"). Só vale para saltos com distância relevante.
TRILHA_VEL_MAX_KMH = 160.0
TRILHA_SALTO_MIN_M = 100.0
# Máximo de pontos recentes considerados no traço (janela de rota mais recente).
TRILHA_MAX_PONTOS = 150


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    """Distância em metros entre duas coordenadas (fórmula de haversine)."""
    from math import radians, sin, cos, asin, sqrt
    raio = 6371000.0
    dphi = radians(lat2 - lat1)
    dlmb = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlmb / 2) ** 2
    return 2 * raio * asin(sqrt(a))


def montar_trilha(device, max_pontos: int = TRILHA_MAX_PONTOS) -> list:
    """
    Monta o traço de deslocamento do device para o mapa do detalhe, priorizando a
    PRECISÃO do caminho:

      1. Ordena pelo horário REAL de coleta (coletado_em), não pela chegada ao
         servidor — corrige a forma da rota quando o app entrega uma fila offline
         em rajada (registrado_em fora de ordem).
      2. Descarta fixes ruins (precisao_m acima do limite) que jogam o traço longe.
      3. Remove saltos impossíveis (velocidade acima do limite) — glitches de GPS.

    Devolve a lista em ordem CRONOLÓGICA (antigo → recente):
    [{id, lat, lon, precisao, quando, bateria, online}].
    """
    from ProjetoEstoque.models import KioskCheckin

    base = list(
        KioskCheckin.objects
        .filter(device=device, latitude__isnull=False, longitude__isnull=False)
        .annotate(ts=Coalesce("coletado_em", "registrado_em"))
        .order_by("-ts")[:max_pontos]
    )
    base.reverse()  # cronológico ascendente (antigo → recente)

    def _construir(filtrar_precisao: bool) -> list:
        pontos, prev = [], None
        for c in base:
            if filtrar_precisao and c.precisao_m is not None and c.precisao_m > TRILHA_PRECISAO_MAX_M:
                continue
            ts = c.coletado_em or c.registrado_em
            if prev is not None:
                dist = _haversine_m(prev["lat"], prev["lon"], c.latitude, c.longitude)
                dt = (ts - prev["_ts"]).total_seconds()
                if dt > 0 and dist > TRILHA_SALTO_MIN_M and (dist / dt) * 3.6 > TRILHA_VEL_MAX_KMH:
                    continue  # salto impossível → descarta como glitch de GPS
            ponto = {
                "id": c.pk,
                "lat": c.latitude,
                "lon": c.longitude,
                "precisao": c.precisao_m,
                "quando": timezone.localtime(ts).strftime("%d/%m/%Y %H:%M"),
                "bateria": c.bateria,
                "online": c.online,
                "_ts": ts,
            }
            pontos.append(ponto)
            prev = ponto
        for p in pontos:
            p.pop("_ts", None)
        return pontos

    trilha = _construir(filtrar_precisao=True)
    # Se o filtro de precisão zerou o traço (device cujo GPS é sempre ruim), refaz
    # sem ele para ainda assim mostrar algum caminho.
    if len(trilha) < 2:
        trilha = _construir(filtrar_precisao=False)
    return trilha
