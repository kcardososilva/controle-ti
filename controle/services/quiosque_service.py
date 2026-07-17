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
import base64
import hashlib
import json
import random
import secrets
import string
from datetime import datetime, timedelta, timezone as dt_timezone
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


# Teto de sanidade para o upload pela tela — bem acima do tamanho normal de um
# APK (dezenas de MB); existe só para não deixar um upload arbitrariamente
# grande encher o disco do servidor.
_APK_UPLOAD_MAX_MB = 400


def salvar_apk_upload(arquivo, *, version_code: int | None = None, version_name: str = "") -> dict:
    """Salva um novo instalador (.apk) enviado pela tela de Matrículas, na pasta
    protegida (`KIOSK_APK_DIR`) — dispensa copiar o arquivo manualmente no
    servidor. A versão nova SOBREPÕE a(s) anterior(es): remove todo `.apk` já
    existente na pasta antes de gravar o novo, então `apk_atual()` sempre resolve
    para o arquivo recém-enviado.

    Sempre remove o sidecar de versão (`atualizacao.json`, ver
    `registrar_versao_apk_atual`): ele foi calculado para o arquivo antigo e
    ficaria órfão. Se `version_code` for informado, já registra a versão nova no
    mesmo passo — dispensando rodar o comando `assinar_apk_quiosque` à parte. Sem
    `version_code`, o .apk fica publicado e a auto-atualização (Device Owner)
    simplesmente fica ausente até a versão ser registrada (aqui de novo, ou pelo
    comando) — não quebra o check-in (ver `atualizacao_disponivel`).

    Lança ValueError em nome/tamanho inválido (400 na view).
    """
    nome = Path(getattr(arquivo, "name", "") or "").name
    if not nome or not nome.lower().endswith(".apk"):
        raise ValueError("O arquivo precisa ter extensão .apk.")
    if arquivo.size > _APK_UPLOAD_MAX_MB * 1024 * 1024:
        raise ValueError(f"Arquivo maior que o limite de {_APK_UPLOAD_MAX_MB}MB.")

    destino_dir = apk_dir()
    for antigo in destino_dir.iterdir():
        if antigo.is_file() and antigo.suffix.lower() == ".apk":
            antigo.unlink()
    sidecar = destino_dir / _ATUALIZACAO_SIDECAR
    if sidecar.is_file():
        sidecar.unlink()

    with open(destino_dir / nome, "wb") as destino:
        for pedaco in arquivo.chunks():
            destino.write(pedaco)

    if version_code:
        registrar_versao_apk_atual(version_code=version_code, version_name=version_name)

    return apk_atual()


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
# Auto-atualização do .apk (Device Owner) — sha256 + campo `atualizacao`
# ──────────────────────────────────────────────────────────────────────────────
# O app já matriculado se auto-instala uma build nova sozinho (Device Owner).
# A API de produção roda em HTTP puro (sem TLS), mas isso NÃO exige assinatura
# própria do transporte: o .apk publicado já é assinado com a keystore de
# release do app, e o PRÓPRIO ANDROID recusa instalar uma "atualização" que não
# esteja assinada com a mesma chave do app já instalado — verificação feita pelo
# SO no `PackageInstaller.commit()`, que nenhuma interceptação em trânsito
# contorna. O `sha256` abaixo serve só para o app detectar download
# incompleto/corrompido antes de instalar — checagem de integridade de
# transporte, não uma camada de segurança adicional (ver INFORME do time Android
# sobre auto-atualização do APK do quiosque).

_ATUALIZACAO_SIDECAR = "atualizacao.json"


def registrar_versao_apk_atual(*, version_code: int, version_name: str) -> dict:
    """Calcula o sha256 do .apk hoje publicado em KIOSK_APK_DIR e grava o
    resultado num sidecar JSON ao lado do arquivo — é o que o /checkin/ lê para
    oferecer auto-atualização. Chamado automaticamente pelo upload da tela de
    Matrículas quando `version_code` é informado; para quem preferir copiar o
    .apk manualmente na pasta do servidor, rodar depois o management command
    `assinar_apk_quiosque`. Sem isso, os aparelhos em campo continuam vendo a
    versão anterior como a mais recente (não quebra nada — só não dispara a
    auto-atualização)."""
    atual = apk_atual()
    if atual is None:
        raise ValueError("Nenhum instalador (.apk) encontrado na pasta do servidor.")

    dados_apk = (apk_dir() / atual["nome"]).read_bytes()
    info = {
        "version_code": int(version_code),
        "version_name": str(version_name)[:20],
        "sha256": hashlib.sha256(dados_apk).hexdigest(),
        "apk_nome": atual["nome"],
    }
    (apk_dir() / _ATUALIZACAO_SIDECAR).write_text(json.dumps(info), encoding="utf-8")
    return info


def _ler_sidecar_atualizacao() -> dict | None:
    """Lê o sidecar de versão (`atualizacao.json`) e valida que ainda corresponde
    ao .apk atualmente publicado. None se ausente, corrompido, ou órfão (aponta
    para um arquivo que não é mais o atual — ex.: .apk substituído sem registrar
    a versão de novo)."""
    caminho = apk_dir() / _ATUALIZACAO_SIDECAR
    if not caminho.is_file():
        return None
    try:
        info = json.loads(caminho.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None

    atual = apk_atual()
    if atual is None or atual["nome"] != info.get("apk_nome"):
        return None
    return info


def versao_apk_registrada() -> dict | None:
    """Versão (version_code/version_name) atualmente registrada para
    auto-atualização — para exibição na tela de Matrículas (indica se a frota já
    matriculada vai receber esta build sozinha ou não)."""
    info = _ler_sidecar_atualizacao()
    if info is None:
        return None
    return {"version_code": info["version_code"], "version_name": info["version_name"]}


def atualizacao_disponivel(request) -> dict | None:
    """Objeto `atualizacao` devolvido em todo /checkin/: sempre a versão mais
    recente publicada — o app já compara sozinho contra a própria versão
    instalada, então o servidor nunca precisa rastrear em qual versão cada
    aparelho está (mesmo princípio já usado para config_versao/config).

    None se a versão ainda não foi registrada, ou se o .apk foi trocado sem
    registrar de novo (ver `_ler_sidecar_atualizacao`)."""
    info = _ler_sidecar_atualizacao()
    if info is None:
        return None

    from django.urls import reverse

    return {
        "version_code": info["version_code"],
        "version_name": info["version_name"],
        "url": request.build_absolute_uri(reverse("kiosk_atualizacao_apk")),
        "sha256": info["sha256"],
    }


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
_APPS_MAX        = 500   # teto de itens aceitos por inventário (folga sobre ~40–120)
_APPS_PKG_MAX    = 255
_APPS_NOME_MAX   = 255
_APPS_VERSAO_MAX = 100


def _parse_dt_ms(v):
    """Converte epoch ms (int) para datetime aware. None se inválido/ausente."""
    ms = _i(v)
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=dt_timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


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
            versao=str(it.get('versao') or '')[:_APPS_VERSAO_MAX],
            versao_codigo=_i(it.get('versao_codigo')) or 0,
            atualizado_em=_parse_dt_ms(it.get('atualizado_em_ms')),
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


def registrar_checkin(device, dados: dict, request=None) -> dict:
    """
    Grava um KioskCheckin, atualiza o estado mais recente do device e devolve a
    resposta para o app: config (se a versão mudou) e comandos pendentes.

    Suporta fila offline: o app pode enviar leituras com `coletado_em` no passado.
    Cada leitura vira uma linha de histórico; o "estado atual" só é atualizado
    quando a leitura é a mais recente já vista (não regride com dados antigos).
    """
    from ProjetoEstoque.models import KioskCheckin, KioskComando, KioskDevice

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
            # Memória/armazenamento: snapshot do check-in mais recente (não histórico —
            # ver INFORME_SERVIDOR_MEMORIA_DISCO §1.3). Vêm em TODO check-in do app.
            ram_total = _i(dados.get("ram_total_mb"))
            if ram_total is not None:
                device.ram_total_mb = ram_total
            ram_livre = _i(dados.get("ram_livre_mb"))
            if ram_livre is not None:
                device.ram_livre_mb = ram_livre
            ram_usada = _i(dados.get("ram_usada_mb"))
            if ram_usada is not None:
                device.ram_usada_mb = ram_usada
            if "ram_pouca" in dados:
                device.ram_pouca = bool(dados.get("ram_pouca"))
            arm_total = _i(dados.get("armazenamento_total_mb"))
            if arm_total is not None:
                device.armazenamento_total_mb = arm_total
            arm_livre = _i(dados.get("armazenamento_livre_mb"))
            if arm_livre is not None:
                device.armazenamento_livre_mb = arm_livre
            arm_usado = _i(dados.get("armazenamento_usado_mb"))
            if arm_usado is not None:
                device.armazenamento_usado_mb = arm_usado
        # MAC: identidade estável do aparelho → atualiza só quando chega valor não-nulo
        # (não sobrescreve um MAC bom com null vindo de um check-in sem Device Owner).
        if mac and device.mac != mac:
            device.mac = mac
        # Inventário de apps: presente só nos ciclos em que a lista mudou. Ausência
        # não altera o inventário guardado (ver _persistir_inventario).
        _persistir_inventario(device, dados.get("apps_instalados"), dados.get("apps_hash"))
        if dados.get("app_versao"):
            device.app_versao = str(dados.get("app_versao"))[:20]
        # versionCode do app rodando agora + status da auto-atualização — reportados em
        # TODO check-in (ver INFORME sobre auto-atualização §1.4). São só para exibição
        # no painel (selo de conformidade); NUNCA influenciam o que o servidor devolve em
        # `atualizacao` (ver atualizacao_disponivel — sempre a versão mais recente, o
        # cliente decide). Validado contra as choices do model: dado vindo do device não
        # é confiável, um valor desconhecido simplesmente não atualiza o status guardado.
        app_versao_codigo = _i(dados.get("app_versao_codigo"))
        if app_versao_codigo is not None:
            device.app_versao_codigo = app_versao_codigo
        atualizacao_status = (dados.get("atualizacao_status") or "").strip()
        if atualizacao_status in KioskDevice.AtualizacaoStatus.values:
            device.atualizacao_status = atualizacao_status
            device.atualizacao_motivo = (
                str(dados.get("atualizacao_motivo") or "")[:255]
                if atualizacao_status == KioskDevice.AtualizacaoStatus.BLOQUEADA else ""
            )
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

    atualizacao = atualizacao_disponivel(request) if request is not None else None

    return {
        "ok": True,
        "config_versao": device.config_versao,
        "config": config,
        "comandos": comandos,
        "atualizacao": atualizacao,
    }


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


# ──────────────────────────────────────────────────────────────────────────────
# Painel Gerencial (indicadores para apresentação — RH / gestão de TI)
# ──────────────────────────────────────────────────────────────────────────────
# Métricas construídas só a partir do que é persistido de forma confiável:
# KioskCheckin tem retenção móvel de RETENCAO_DIAS (ver prune_checkins) — por
# isso "atividade recente" cobre só essa janela. As demais séries usam
# `criado_em` de KioskDevice/KioskMatricula/KioskInstaladorLink/KioskComando,
# que nunca é podado, e por isso servem para tendências de 12 meses.

_ATENCAO_BATERIA_PCT = 20
_ATENCAO_ARMAZENAMENTO_MB = 1024


def _top_n(valores, n=6, rotulo_outros="Outros"):
    """Conta ocorrências e agrupa o rabo da distribuição em 'Outros' — usado
    nos gráficos de composição da frota (fabricante, versão do Android/app)."""
    from collections import Counter

    contagem = Counter(v for v in valores if v)
    top = contagem.most_common(n)
    restante = sum(contagem.values()) - sum(v for _, v in top)
    labels = [k for k, _ in top]
    dados = [v for _, v in top]
    if restante > 0:
        labels.append(rotulo_outros)
        dados.append(restante)
    return labels, dados


def _meses_stamps(n=12):
    now = timezone.localtime()
    y, m = now.year, now.month
    out = []
    for _ in range(n):
        out.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return list(reversed(out))


def _meses_labels_pt(stamps):
    nomes = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]
    return [f"{nomes[m - 1]}/{str(y)[-2:]}" for (y, m) in stamps]


def _alinhar_serie_mensal(stamps, queryset_mensal):
    """queryset_mensal: `.values('m').annotate(c=Count(...))`, 'm' vindo de TruncMonth."""
    m2v = {}
    for row in queryset_mensal:
        dt = row["m"]
        if dt is None:
            continue
        local = timezone.localtime(dt) if timezone.is_aware(dt) else dt
        m2v[(local.year, local.month)] = int(row["c"] or 0)
    return [m2v.get((y, m), 0) for (y, m) in stamps]


def montar_indicadores_gerenciais() -> dict:
    """
    Fonte única de dados do Painel Gerencial do Quiosque (`/quiosque/indicadores/`):
    saúde da frota, adoção do provisionamento e composição do parque, cruzando
    com Item/Localidade quando o número de série bate — para apresentação à
    gestão (RH e TI), sem o detalhe operacional de cada aparelho (esse fica em
    `quiosque_dashboard`).
    """
    from collections import Counter

    from django.db.models import Count, Q, Sum
    from django.db.models.functions import TruncDate, TruncMonth

    from ProjetoEstoque.models import (
        Item, KioskCheckin, KioskComando, KioskDevice, KioskInstaladorLink, KioskMatricula,
    )

    agora = timezone.now()
    devices = list(KioskDevice.objects.filter(ativo=True))
    total = len(devices)

    online = sum(1 for d in devices if d.online)
    ativos_24h = sum(
        1 for d in devices
        if d.ultimo_checkin and (agora - d.ultimo_checkin).total_seconds() <= 86400
    )
    sem_localizacao = sum(1 for d in devices if not d.tem_localizacao)
    bateria_critica = sum(1 for d in devices if d.ultima_bateria is not None and d.ultima_bateria <= _ATENCAO_BATERIA_PCT)
    armazenamento_critico = sum(
        1 for d in devices
        if d.armazenamento_livre_mb is not None and d.armazenamento_livre_mb < _ATENCAO_ARMAZENAMENTO_MB
    )
    ram_critica = sum(1 for d in devices if d.ram_pouca)
    # Telemetria de memória/armazenamento só existe em builds do app a partir da
    # v1.5.1 (ver INFORME_SERVIDOR_MEMORIA_DISCO_E_VERSAO_APPS.md) — sem isto, os
    # dois contadores acima ficam sempre em 0 mesmo com a frota inteira sem dado
    # nenhum, o que pareceria "tudo certo" num painel gerencial. Expor a cobertura
    # real evita essa falsa sensação de saúde.
    com_telemetria_memoria = sum(1 for d in devices if d.armazenamento_livre_mb is not None)

    pct_online = round(online / total * 100) if total else 0
    pct_24h = round(ativos_24h / total * 100) if total else 0

    # -------- Aparelhos que precisam de atenção (offline, bateria, armazenamento, RAM) --------
    atencao = []
    for d in devices:
        motivos = []
        if not d.online:
            dias = (agora - d.ultimo_checkin).days if d.ultimo_checkin else None
            motivos.append(f"Offline há {dias} dia(s)" if dias is not None else "Nunca conectou")
        if d.ultima_bateria is not None and d.ultima_bateria <= _ATENCAO_BATERIA_PCT:
            motivos.append(f"Bateria em {d.ultima_bateria}%")
        if d.armazenamento_livre_mb is not None and d.armazenamento_livre_mb < _ATENCAO_ARMAZENAMENTO_MB:
            motivos.append("Armazenamento crítico")
        if d.ram_pouca:
            motivos.append("Pouca RAM")
        if motivos:
            atencao.append({"device": d, "motivos": motivos})
    atencao.sort(key=lambda a: (a["device"].online, -len(a["motivos"])))
    atencao = atencao[:20]

    # -------- Composição do parque --------
    fab_labels, fab_dados = _top_n([d.fabricante for d in devices], 6)
    android_labels, android_dados = _top_n([d.android_versao for d in devices], 8)
    appver_labels, appver_dados = _top_n([d.app_versao for d in devices], 8)

    modelos_counter = Counter(
        f"{(d.fabricante or '—').strip()} {(d.modelo or '—').strip()}".strip()
        for d in devices
    )
    top_modelos = modelos_counter.most_common(10)

    # -------- Cobertura por localidade (cruza com Item pelo nº de série) --------
    seriais = [(d.serial or "").strip() for d in devices if (d.serial or "").strip()]
    itens_por_serial = {}
    if seriais:
        itens_por_serial = {
            it.numero_serie: it
            for it in Item.objects.filter(numero_serie__in=seriais).select_related("localidade")
        }
    cobertura = Counter()
    vinculados = 0
    for d in devices:
        it = itens_por_serial.get((d.serial or "").strip())
        if it:
            vinculados += 1
            cobertura[it.localidade.local if it.localidade else "Sem localidade"] += 1
        else:
            cobertura["Sem vínculo no estoque"] += 1
    cobertura_top = cobertura.most_common(8)
    pct_vinculados = round(vinculados / total * 100) if total else 0

    # -------- Matrículas (provisionamento) --------
    validas_q = Q(expira_em__isnull=True) | Q(expira_em__gt=agora)
    mat_total = KioskMatricula.objects.count()
    mat_usadas = KioskMatricula.objects.filter(usado=True).count()
    mat_disponiveis = KioskMatricula.objects.filter(usado=False).filter(validas_q).count()
    mat_expiradas = mat_total - mat_usadas - mat_disponiveis
    mat_taxa_conversao = round(mat_usadas / mat_total * 100, 1) if mat_total else 0.0

    # -------- Instaladores (auto-atendimento de provisionamento) --------
    inst_total = KioskInstaladorLink.objects.count()
    inst_downloads = KioskInstaladorLink.objects.aggregate(s=Sum("downloads"))["s"] or 0
    inst_validos = KioskInstaladorLink.objects.filter(revogado=False, expira_em__gt=agora).count()
    inst_revogados = KioskInstaladorLink.objects.filter(revogado=True).count()

    # -------- Comandos remotos (canal de controle) --------
    comandos_status = dict(KioskComando.objects.values("status").annotate(c=Count("id")).values_list("status", "c"))
    comandos_total = sum(comandos_status.values())

    # -------- Crescimento da frota (12 meses) — criado_em nunca é podado --------
    stamps = _meses_stamps(12)
    labels_meses = _meses_labels_pt(stamps)
    inicio_janela = timezone.make_aware(datetime(stamps[0][0], stamps[0][1], 1))
    devices_serie = _alinhar_serie_mensal(
        stamps,
        KioskDevice.objects.filter(criado_em__gte=inicio_janela)
        .annotate(m=TruncMonth("criado_em")).values("m").annotate(c=Count("id")),
    )
    matriculas_serie = _alinhar_serie_mensal(
        stamps,
        KioskMatricula.objects.filter(criado_em__gte=inicio_janela)
        .annotate(m=TruncMonth("criado_em")).values("m").annotate(c=Count("id")),
    )

    # -------- Atividade recente (janela real de retenção do check-in) --------
    inicio_atividade = agora - timedelta(days=RETENCAO_DIAS)
    checkins_recentes = KioskCheckin.objects.filter(registrado_em__gte=inicio_atividade)
    dias_stamps = [(agora - timedelta(days=i)).date() for i in range(RETENCAO_DIAS - 1, -1, -1)]
    dias_labels = [d.strftime("%d/%m") for d in dias_stamps]
    checkins_por_dia_map = {
        row["d"]: row["c"]
        for row in checkins_recentes.annotate(d=TruncDate("registrado_em")).values("d").annotate(c=Count("id"))
    }
    checkins_por_dia = [checkins_por_dia_map.get(d, 0) for d in dias_stamps]

    rede_rows = list(checkins_recentes.exclude(rede="").values("rede").annotate(c=Count("id")))
    rede_labels = [r["rede"] for r in rede_rows]
    rede_dados = [r["c"] for r in rede_rows]

    # -------- Resumo inteligente (linguagem natural, gerado a partir dos KPIs) --------
    partes = [f"A frota tem {total} aparelho(s) ativo(s), com {online} ({pct_online}%) online neste momento."]
    partes.append(f"{ativos_24h} aparelho(s) ({pct_24h}%) enviaram telemetria nas últimas 24 horas.")
    if atencao:
        partes.append(f"{len(atencao)} aparelho(s) precisam de atenção — offline, bateria ou armazenamento críticos.")
    else:
        partes.append("Nenhum aparelho está em estado crítico no momento.")
    if mat_total:
        partes.append(f"Das {mat_total} matrícula(s) geradas, {mat_usadas} ({mat_taxa_conversao}%) já provisionaram um aparelho.")
    if total:
        partes.append(f"{vinculados} aparelho(s) ({pct_vinculados}%) estão vinculados a um equipamento do estoque pelo número de série.")
    resumo = " ".join(partes)

    return {
        "total": total,
        "online": online,
        "pct_online": pct_online,
        "ativos_24h": ativos_24h,
        "pct_24h": pct_24h,
        "sem_localizacao": sem_localizacao,
        "bateria_critica": bateria_critica,
        "armazenamento_critico": armazenamento_critico,
        "ram_critica": ram_critica,
        "com_telemetria_memoria": com_telemetria_memoria,
        "vinculados": vinculados,
        "pct_vinculados": pct_vinculados,
        "atencao": atencao,

        "fab_labels": fab_labels, "fab_dados": fab_dados,
        "android_labels": android_labels, "android_dados": android_dados,
        "appver_labels": appver_labels, "appver_dados": appver_dados,
        "top_modelos": top_modelos,
        "cobertura_top": cobertura_top,

        "mat_total": mat_total, "mat_usadas": mat_usadas, "mat_disponiveis": mat_disponiveis,
        "mat_expiradas": mat_expiradas, "mat_taxa_conversao": mat_taxa_conversao,

        "inst_total": inst_total, "inst_downloads": inst_downloads,
        "inst_validos": inst_validos, "inst_revogados": inst_revogados,

        "comandos_total": comandos_total, "comandos_status": comandos_status,

        "labels_meses": labels_meses, "devices_serie": devices_serie, "matriculas_serie": matriculas_serie,
        "dias_labels": dias_labels, "checkins_por_dia": checkins_por_dia,
        "rede_labels": rede_labels, "rede_dados": rede_dados,
        "retencao_dias": RETENCAO_DIAS,

        "resumo": resumo,
        "gerado_em": agora,
    }
