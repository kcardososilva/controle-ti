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
from datetime import date, datetime, timedelta, timezone as dt_timezone
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.contrib.auth.hashers import make_password
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.db.models.functions import Coalesce


# Retenção da telemetria (check-ins) por aparelho — janela móvel em dias.
# Após este prazo os dados antigos são sobrepostos pelos novos. A limpeza só roda
# QUANDO o aparelho faz check-in; logo, um aparelho que parou de enviar conserva
# todo o seu histórico (fica guardado como histórico do dispositivo).
#
# Volume: com o intervalo de 300 s configurado na frota são 288 leituras por dia
# por aparelho — 15 dias × 40 aparelhos ≈ 173 mil linhas, folgado para o SQLite.
RETENCAO_DIAS = 15

# Probabilidade de rodar a poda em cada check-in. A poda NÃO precisa rodar a cada
# heartbeat — seria um DELETE-scan contínuo. Rodando de forma amostrada (~1 a cada
# 50 check-ins) a tabela continua limitada à janela e a resposta do check-in fica
# leve. O atraso que a amostragem introduz só faz a janela durar um pouco MAIS,
# nunca menos — erra para o lado de preservar dado.
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

# Quantas arquivagens (uploads substituídos) ficam retidas em versoes_anteriores/.
# Além disso, a mais antiga é apagada a cada novo upload — evita crescimento sem
# limite da pasta protegida (mesmo princípio de RETENCAO_DIAS para check-ins).
_VERSOES_ANTERIORES_MAX = 10


def apk_dir() -> Path:
    """Pasta protegida do instalador. Cria se ainda não existir."""
    destino = Path(getattr(settings, "KIOSK_APK_DIR", None) or (Path(settings.BASE_DIR) / "kiosk_apk"))
    destino.mkdir(parents=True, exist_ok=True)
    return destino


def apk_versoes_dir() -> Path:
    """Subpasta onde as versões substituídas do instalador ficam arquivadas (em
    vez de apagadas) — permite recuperar um .apk anterior se a build nova
    apresentar problema. Fica dentro da mesma pasta protegida (`KIOSK_APK_DIR`),
    fora do alcance do `apk_atual()` (que só olha o nível raiz)."""
    destino = apk_dir() / "versoes_anteriores"
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
    servidor. A versão nova SOBREPÕE a(s) anterior(es) na raiz da pasta, mas a(s)
    antiga(s) não é(são) apagada(s): vai(ão) para `versoes_anteriores/` (ver
    `_arquivar_apk_atual`), então `apk_atual()` sempre resolve para o arquivo
    recém-enviado e a build anterior continua disponível para download.

    Se `version_code` for informado, já registra a versão nova no mesmo passo —
    dispensando rodar o comando `assinar_apk_quiosque` à parte. Sem
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
    _arquivar_apk_atual(destino_dir)

    with open(destino_dir / nome, "wb") as destino:
        for pedaco in arquivo.chunks():
            destino.write(pedaco)

    if version_code:
        registrar_versao_apk_atual(version_code=version_code, version_name=version_name)

    return apk_atual()


def _arquivar_apk_atual(destino_dir: Path) -> None:
    """Move o(s) .apk hoje publicado(s) — e o sidecar de versão correspondente,
    se houver — para `versoes_anteriores/`, prefixando o nome com a data/hora do
    arquivamento (evita colisão de nomes e preserva a ordem cronológica). Poda a
    arquivagem mais antiga além de `_VERSOES_ANTERIORES_MAX`."""
    antigos = [p for p in destino_dir.iterdir() if p.is_file() and p.suffix.lower() == ".apk"]
    if not antigos:
        return

    sidecar = destino_dir / _ATUALIZACAO_SIDECAR
    info_sidecar = None
    if sidecar.is_file():
        try:
            info_sidecar = json.loads(sidecar.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            info_sidecar = None

    versoes_dir = apk_versoes_dir()
    prefixo = timezone.now().strftime("%Y%m%d-%H%M%S")
    for antigo in antigos:
        # Duas arquivagens no mesmo segundo (dois uploads em sequência rápida)
        # teriam o mesmo prefixo — sem o desempate abaixo, a segunda SOBRESCREVERIA
        # o arquivo da primeira (Path.replace troca silenciosamente o destino).
        arquivado = versoes_dir / f"{prefixo}__{antigo.name}"
        sufixo = 1
        while arquivado.exists():
            sufixo += 1
            arquivado = versoes_dir / f"{prefixo}-{sufixo}__{antigo.name}"
        antigo.replace(arquivado)
        if info_sidecar and info_sidecar.get("apk_nome") == antigo.name:
            (versoes_dir / f"{arquivado.name}.json").write_text(json.dumps(info_sidecar), encoding="utf-8")
    if sidecar.is_file():
        sidecar.unlink()

    _podar_versoes_anteriores()


def _podar_versoes_anteriores() -> None:
    """Mantém só as `_VERSOES_ANTERIORES_MAX` arquivagens mais recentes — apaga
    o excedente mais antigo (.apk + sidecar de versão, se houver)."""
    versoes_dir = apk_versoes_dir()
    apks = sorted(
        (p for p in versoes_dir.iterdir() if p.is_file() and p.suffix.lower() == ".apk"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for excedente in apks[_VERSOES_ANTERIORES_MAX:]:
        excedente.unlink(missing_ok=True)
        sidecar_excedente = versoes_dir / f"{excedente.name}.json"
        if sidecar_excedente.is_file():
            sidecar_excedente.unlink()


def versoes_anteriores() -> list[dict]:
    """Lista os instaladores (.apk) arquivados em `versoes_anteriores/` — versões
    substituídas por um upload mais recente na tela de Matrículas — da mais
    recente para a mais antiga. Cada item traz nome/tamanho/data e, quando o
    sidecar foi preservado, a versão (`version_code`/`version_name`) que estava
    registrada quando aquele .apk foi substituído."""
    versoes_dir = apk_versoes_dir()
    candidatos = sorted(
        (p for p in versoes_dir.iterdir() if p.is_file() and p.suffix.lower() == ".apk"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    resultado = []
    for p in candidatos:
        st = p.stat()
        versao = None
        sidecar = versoes_dir / f"{p.name}.json"
        if sidecar.is_file():
            try:
                info = json.loads(sidecar.read_text(encoding="utf-8"))
                versao = {"version_code": info.get("version_code"), "version_name": info.get("version_name")}
            except (ValueError, OSError):
                versao = None
        resultado.append({
            "nome": p.name,
            "tamanho": st.st_size,
            "modificado_em": timezone.make_aware(datetime.fromtimestamp(st.st_mtime)),
            "versao": versao,
        })
    return resultado


def caminho_versao_anterior(nome_arquivo: str) -> Path | None:
    """Resolve o caminho físico de um instalador arquivado (versão anterior)
    dentro de `versoes_anteriores/`. Mesma defesa contra path traversal de
    `caminho_instalador`: nome sem separador de caminho e resultado confirmado
    DENTRO da subpasta de arquivamento."""
    if not nome_arquivo or nome_arquivo != Path(nome_arquivo).name:
        return None
    base = apk_versoes_dir().resolve()
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
        "telemetria_wifi": device.telemetria_wifi,
        # Telemetria de rede móvel (v1.10.0+) — geração/operadora/força do sinal
        # do chip. App antigo ignora a chave e simplesmente não manda os campos:
        # o servidor trata ausência como "aparelho não reporta" (ver
        # sinal_do_checkin), nunca como sinal zero.
        "telemetria_movel": device.telemetria_movel,
        # None (não string vazia) quando não configurado — ver INFORME §4.1:
        # wifi_ssid vazio = "sem rede provisionada"; o app trata ausência/null
        # da mesma forma (não tenta provisionar nada).
        "wifi_ssid": device.wifi_ssid or None,
        "wifi_senha": device.wifi_senha or None,
        # Gates locais da tela "Gerência do TI" (v1.7.1+, ver
        # INFORME_SERVIDOR_CACHE_E_ENERGIA.md) — padrão False já garantido pelo
        # model; aparelho com app antigo que ignora estes campos simplesmente não
        # libera nada de novo (nenhuma regressão de segurança).
        "permite_reiniciar": device.permite_reiniciar,
        "permite_desligar": device.permite_desligar,
        "limpeza_cache_automatica": device.limpeza_cache_automatica,
        "permite_limpar_apps_terceiros": device.permite_limpar_apps_terceiros,
        # Gate da barra de status + bandeja de notificações (v1.9.0+, ver
        # INFORME_SERVIDOR_NOTIFICACOES.md) — só abre a "porta"; o app de
        # origem (WhatsApp Business, Outlook, Gmail…) também precisa estar em
        # apps_permitidos, senão fica suspenso e nunca notifica nada.
        "permite_notificacoes": device.permite_notificacoes,
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


# ──────────────────────────────────────────────────────────────────────────────
# Rede e sinal — normalização do que o aparelho reporta
# ──────────────────────────────────────────────────────────────────────────────
# O app reporta o transporte em `rede` como texto livre ("wifi" / "cellular" /
# "none", e nada impede uma build antiga mandar outra grafia). A UI (mapa de
# rota, selo de sinal, filtros) NÃO pode depender de comparar essas strings:
# normalizamos uma vez na gravação e guardamos em `rede_tipo`.

REDE_WIFI     = "wifi"
REDE_MOVEL    = "movel"
REDE_ETHERNET = "ethernet"
REDE_NENHUMA  = "nenhuma"

_REDE_TIPOS = {
    "wifi": REDE_WIFI, "wi-fi": REDE_WIFI, "wlan": REDE_WIFI,
    "cellular": REDE_MOVEL, "celular": REDE_MOVEL, "movel": REDE_MOVEL,
    "móvel": REDE_MOVEL, "mobile": REDE_MOVEL, "dados": REDE_MOVEL,
    "ethernet": REDE_ETHERNET, "eth": REDE_ETHERNET, "lan": REDE_ETHERNET,
    "none": REDE_NENHUMA, "nenhuma": REDE_NENHUMA, "offline": REDE_NENHUMA, "sem": REDE_NENHUMA,
}

# Rótulo curto para o selo em cima do ponto no mapa (sem a geração móvel, que
# é concatenada quando existe: "4G", "5G"…).
REDE_ROTULOS = {
    REDE_WIFI: "Wi-Fi", REDE_MOVEL: "Móvel",
    REDE_ETHERNET: "Cabo", REDE_NENHUMA: "Sem rede",
}

# Geração derivada da tecnologia crua do Android (TelephonyManager.getDataNetworkType).
# Fonte única: a UI exibe só a geração; `movel_tecnologia` fica guardada para auditoria.
_GERACAO_POR_TECNOLOGIA = {
    "nr": "5g", "nr_nsa": "5g", "nr_sa": "5g", "5g": "5g",
    "lte": "4g", "lte_ca": "4g", "lte_a": "4g", "lte+": "4g", "iwlan": "4g", "4g": "4g",
    "umts": "3g", "hspa": "3g", "hspa+": "3g", "hspap": "3g", "hsdpa": "3g",
    "hsupa": "3g", "evdo": "3g", "evdo_a": "3g", "evdo_b": "3g", "ehrpd": "3g",
    "td_scdma": "3g", "tdscdma": "3g", "3g": "3g",
    "gsm": "2g", "gprs": "2g", "edge": "2g", "cdma": "2g", "1xrtt": "2g", "iden": "2g", "2g": "2g",
}
_GERACOES_VALIDAS = ("2g", "3g", "4g", "5g")


def normalizar_rede_tipo(rede: str) -> str:
    """Texto livre de `rede` → um de REDE_WIFI/REDE_MOVEL/REDE_ETHERNET/REDE_NENHUMA.
    Devolve '' quando não reconhece (nunca inventa um transporte)."""
    chave = (rede or "").strip().lower()
    return _REDE_TIPOS.get(chave, "")


def normalizar_geracao(geracao: str = "", tecnologia: str = "") -> str:
    """Geração móvel normalizada ('2g'|'3g'|'4g'|'5g') a partir do que o app
    mandar: `movel_geracao` explícita tem prioridade; senão deriva da tecnologia
    crua do Android. '' quando não há como afirmar."""
    direta = (geracao or "").strip().lower().replace(" ", "")
    if direta in _GERACOES_VALIDAS:
        return direta
    bruta = (tecnologia or "").strip().lower().replace(" ", "").replace("-", "_")
    return _GERACAO_POR_TECNOLOGIA.get(bruta, "")


# Faixas de dBm → nível 0-4, usadas SÓ como fallback quando o app manda a força
# do sinal mas não o nível (o Android já calcula o nível por operadora/rádio, que
# é sempre mais fiel que uma régua fixa — por isso `nivel` do app tem prioridade).
_FAIXAS_DBM_MOVEL = ((-85, 4), (-95, 3), (-105, 2), (-115, 1))
_FAIXAS_DBM_WIFI  = ((-55, 4), (-66, 3), (-75, 2), (-85, 1))


def _nivel_por_dbm(dbm, faixas) -> int | None:
    if dbm is None:
        return None
    for limite, nivel in faixas:
        if dbm >= limite:
            return nivel
    return 0


def sinal_do_checkin(c) -> dict:
    """Sinal UNIFICADO de uma linha de check-in, já resolvido para o transporte
    em uso — é o que o selo em cima do ponto no mapa consome, sem precisar saber
    se o aparelho estava em Wi-Fi ou no chip.

    Devolve sempre o mesmo formato:
      {tipo, rotulo, geracao, nivel (0-4|None), dbm, operadora}

    `rotulo` é o que aparece no selo: "4G", "5G", "Wi-Fi", "Sem rede".
    `nivel` None = o aparelho não reporta força de sinal (telemetria desligada
    ou app antigo) — a UI mostra o transporte sem as barrinhas, nunca um nível
    inventado.
    """
    tipo = c.rede_tipo or normalizar_rede_tipo(c.rede)
    geracao = normalizar_geracao(c.movel_geracao, c.movel_tecnologia)

    if tipo == REDE_MOVEL:
        nivel = c.movel_nivel if c.movel_nivel is not None else _nivel_por_dbm(c.movel_rssi_dbm, _FAIXAS_DBM_MOVEL)
        rotulo = geracao.upper() if geracao else REDE_ROTULOS[REDE_MOVEL]
        return {
            "tipo": tipo, "rotulo": rotulo, "geracao": geracao,
            "nivel": nivel, "dbm": c.movel_rssi_dbm,
            "operadora": c.movel_operadora or "",
        }

    if tipo == REDE_WIFI:
        nivel = c.wifi_nivel if c.wifi_nivel is not None else _nivel_por_dbm(c.wifi_rssi_dbm, _FAIXAS_DBM_WIFI)
        return {
            "tipo": tipo, "rotulo": REDE_ROTULOS[REDE_WIFI], "geracao": "",
            "nivel": nivel, "dbm": c.wifi_rssi_dbm,
            "operadora": c.ssid or "",
        }

    return {
        "tipo": tipo or REDE_NENHUMA,
        "rotulo": REDE_ROTULOS.get(tipo, REDE_ROTULOS[REDE_NENHUMA]),
        "geracao": "", "nivel": None, "dbm": None, "operadora": "",
    }


# Piso absoluto (s) do limiar de "entregue de fila". O limiar real é relativo ao
# intervalo de check-in do aparelho (ver `limiar_fila`), mas nunca menor que
# isto: abaixo de 10 min o atraso não distingue falta de rede de um simples
# envio em lote, de uma retentativa com backoff ou do Doze do Android segurando
# a transmissão por alguns minutos. Um limiar curto demais pintaria de vermelho
# a rota inteira de um aparelho perfeitamente conectado — foi exatamente o que
# aconteceu ao validar com dados sintéticos antes deste ajuste.
_ATRASO_FILA_PISO_S = 600
# Múltiplo do intervalo de check-in a partir do qual o atraso deixa de ser
# explicável por lote/backoff. 4 ciclos perdidos = o aparelho realmente não
# estava conseguindo falar com o servidor.
_ATRASO_FILA_CICLOS = 4


def limiar_fila(device) -> int:
    """Atraso (s) a partir do qual uma leitura conta como ENTREGUE DE FILA para
    ESTE aparelho — isto é, ficou guardada na memória por falta de rede.

    Relativo ao `intervalo_checkin_seg` configurado: um aparelho em 5s e outro
    em 300s têm noções muito diferentes de "atrasado", e um limiar fixo
    classificaria errado um dos dois. Calcule UMA vez por device e repasse a
    `conexao_do_checkin`; ler `c.device` dentro do laço seria uma query por linha.
    """
    intervalo = getattr(device, "intervalo_checkin_seg", 0) or 0
    return max(_ATRASO_FILA_PISO_S, _ATRASO_FILA_CICLOS * intervalo)


def conexao_do_checkin(c, atraso_fila_s: int = _ATRASO_FILA_PISO_S) -> dict:
    """Classifica a CONECTIVIDADE de uma linha de check-in cruzando as três
    evidências disponíveis — é o que pinta o trecho da rota de verde ou vermelho:

      1. `online` — auto-declarado pelo app no instante da coleta.
      2. `rede_tipo == nenhuma` — o Android não tinha transporte nenhum.
      3. atraso de entrega acima de `atraso_fila_s` — a leitura chegou muito
         depois de ter sido coletada, logo ficou guardada na memória.

    Qualquer uma das três basta para marcar o ponto como SEM conexão: são
    evidências independentes e, na prática, um aparelho em área de sombra pode
    reportar `online=true` (o rádio ainda vê a torre) e ainda assim não
    conseguir transmitir — só o atraso revela esse caso. Por isso o limiar
    precisa ser folgado (ver `limiar_fila`): a terceira evidência é a única
    inferida, e um limiar apertado transforma envio em lote em falso "offline".

    Devolve {online: bool, fila: bool, atraso_s: int}. `fila` distingue "estava
    sem conexão E a leitura foi guardada na memória" (o caso que o usuário quer
    ver no mapa) de uma simples queda auto-declarada.
    """
    atraso_s = 0
    if c.coletado_em and c.registrado_em:
        # Negativo = relógio do aparelho adiantado em relação ao servidor; não é
        # fila. Clampa em 0 para não virar "entrega antecipada".
        atraso_s = max(0, int((c.registrado_em - c.coletado_em).total_seconds()))

    fila = atraso_s >= atraso_fila_s
    tipo = c.rede_tipo or normalizar_rede_tipo(c.rede)
    online = bool(c.online) and tipo != REDE_NENHUMA and not fila
    return {"online": online, "fila": fila, "atraso_s": atraso_s}


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
    Mantém a janela móvel de RETENCAO_DIAS de telemetria do aparelho.

    Roda no check-in: aparelhos ativos giram a janela; um aparelho que PAROU de
    enviar nunca é podado, então conserva todo o histórico já recebido.

    Uma linha só é descartada quando está fora da janela pelos DOIS carimbos —
    coleta E chegada ao servidor. Podar só por `coletado_em` (como era antes)
    destrói dado que o aparelho guardou na memória e entregou com atraso: uma
    leitura coletada há 20 dias e recebida agora nasceria já vencida e sumiria na
    poda seguinte, embora tivesse acabado de chegar. No histórico real deste
    sistema isso não é hipótese — há 1.097 leituras entregues mais de 24 h depois
    de coletadas, e uma com mais de 120 h de atraso.

    Como a chegada (`registrado_em`) é sempre crescente, ela é que limita o
    tamanho da tabela: todo dado recebido vive RETENCAO_DIAS a partir da entrega,
    e nada fica preso para sempre.

    Os dois filtros usam colunas indexadas diretamente (nada de `Coalesce`, que
    impediria o uso do índice e forçaria varredura completa da tabela).
    """
    from ProjetoEstoque.models import KioskCheckin

    cutoff = timezone.now() - timedelta(days=RETENCAO_DIAS)
    apagados, _ = (
        KioskCheckin.objects
        .filter(device=device, registrado_em__lt=cutoff)
        .filter(Q(coletado_em__lt=cutoff) | Q(coletado_em__isnull=True))
        .delete()
    )
    return apagados


# Campos que uma reentrega pode LEGITIMAMENTE trazer preenchidos quando a linha
# já gravada os tem vazios. O caso real é o GPS: o app manda o check-in assim que
# o ciclo vence e, se o fix ainda não chegou, manda de novo segundos depois com a
# posição — mesmo `coletado_em`. Sem consolidar, isso vira duas linhas no mesmo
# instante, uma sem posição; e um "primeira ganha" ingênuo guardaria justamente a
# linha SEM o GPS, descartando a coordenada.
#
# Booleanos (`online`, `carregando`) ficam de fora de propósito: em um booleano
# não há como distinguir "não informado" de "informado como falso", então
# completar seria adivinhar.
_CAMPOS_CONSOLIDAVEIS = (
    "latitude", "longitude", "precisao_m", "bateria",
    "rede", "ssid", "mac_em_uso",
    "wifi_rssi_dbm", "wifi_nivel", "wifi_velocidade_mbps",
    "wifi_frequencia_mhz", "wifi_banda_ghz",
    "rede_tipo", "movel_geracao", "movel_tecnologia",
    "movel_operadora", "movel_rssi_dbm", "movel_nivel",
)


def _consolidar_reenvio(device, coletado, valores: dict) -> bool:
    """
    Trata a reentrega de uma leitura JÁ gravada (mesmo aparelho, mesmo
    `coletado_em`). Devolve True quando a leitura foi absorvida — o chamador
    então NÃO cria uma linha nova.

    O app reenvia um lote da fila sempre que a resposta HTTP se perde; sem este
    tratamento cada reenvio virava uma linha extra, duplicando pontos na rota e
    inflando as contagens do dia (no histórico atual: 383 instantes repetidos,
    420 linhas excedentes).

    Reenvio idêntico é ignorado. Reenvio mais RICO completa os campos vazios da
    linha original — nunca sobrescreve um valor já gravado, porque reescrever
    telemetria recebida seria alterar histórico, não corrigi-lo.
    """
    from ProjetoEstoque.models import KioskCheckin

    existente = (
        KioskCheckin.objects
        .filter(device=device, coletado_em=coletado)
        .order_by("registrado_em")
        .first()
    )
    if existente is None:
        return False

    preenchidos = []
    for campo in _CAMPOS_CONSOLIDAVEIS:
        novo = valores.get(campo)
        if novo is None or novo == "":
            continue
        if getattr(existente, campo) in (None, ""):
            setattr(existente, campo, novo)
            preenchidos.append(campo)
    if preenchidos:
        existente.save(update_fields=preenchidos)
    return True


def registrar_checkin(device, dados: dict, request=None) -> dict:
    """
    Grava um KioskCheckin, atualiza o estado mais recente do device e devolve a
    resposta para o app: config (se a versão mudou) e comandos pendentes.

    Suporta fila offline: o app pode enviar leituras com `coletado_em` no passado.
    Cada leitura vira uma linha de histórico; o "estado atual" só é atualizado
    quando a leitura é a mais recente já vista (não regride com dados antigos).
    """
    from ProjetoEstoque.models import KioskCheckin, KioskDevice

    lat = _f(dados.get("latitude"))
    lon = _f(dados.get("longitude"))
    prec = _f(dados.get("precisao_m"))
    bat = _i(dados.get("bateria"))
    rede = (dados.get("rede") or "")[:20]
    online = bool(dados.get("online", True))
    carregando = bool(dados.get("carregando", False))
    # Guardamos separado se o instante veio do APARELHO ou se é o "agora" do
    # servidor: só o primeiro identifica uma leitura, e portanto só ele permite
    # reconhecer uma reentrega (ver _consolidar_reenvio).
    coletado_informado = _parse_dt(dados.get("coletado_em"))
    coletado = coletado_informado or timezone.now()
    serial = (dados.get("serial") or "").strip()
    # ssid = estado do momento (vai na linha do check-in); mac = identidade estável (vai no device).
    # Ambos opcionais/anuláveis: o app pode mandar null (emulador/sem Wi-Fi). Nunca exigir.
    ssid = (dados.get("ssid") or None)
    if ssid:
        ssid = str(ssid)[:64]
    mac = (dados.get("mac") or "").strip()[:17] or None
    # mac_em_uso: MAC visto pelo roteador AGORA (v1.7.0, sempre presente). Ausente
    # do payload (build antiga) e null (fora de Wi-Fi/OEM mascara) são o mesmo
    # caso aqui: não há leitura para comparar contra `mac`.
    mac_em_uso = (dados.get("mac_em_uso") or "").strip()[:17] or None
    apps_abertos = _lista_pacotes(dados.get("apps_abertos"))

    # Telemetria de sinal Wi-Fi: opt-in (só chega quando device.telemetria_wifi
    # está ligada) — ver INFORME_SERVIDOR_WIFI_TELEMETRIA §2.1. AUSENTE do
    # payload ≠ presente com null: ausente = _i()/etc devolvem None do mesmo
    # jeito que um valor null explícito, então não dá para distinguir os dois
    # casos aqui — e não precisa: em ambos não há leitura de sinal para gravar.
    wifi_rssi = _i(dados.get("wifi_rssi_dbm"))
    wifi_nivel = _i(dados.get("wifi_nivel"))
    wifi_velocidade = _i(dados.get("wifi_velocidade_mbps"))
    wifi_frequencia = _i(dados.get("wifi_frequencia_mhz"))
    wifi_banda = (dados.get("wifi_banda_ghz") or None)
    if wifi_banda:
        wifi_banda = str(wifi_banda)[:4]

    # Telemetria de rede móvel: opt-in (só chega quando device.telemetria_movel
    # está ligada) — ver INFORME_SERVIDOR_TELEMETRIA_REDE_MOVEL.md. `rede_tipo`
    # é a exceção: sempre derivado, inclusive de aparelho com app antigo que só
    # manda `rede`, para TODA linha do histórico ser classificável na leitura.
    rede_tipo = normalizar_rede_tipo(dados.get("rede_tipo") or rede)
    movel_tecnologia = str(dados.get("movel_tecnologia") or "").strip()[:24]
    movel_geracao = normalizar_geracao(dados.get("movel_geracao"), movel_tecnologia)
    movel_operadora = str(dados.get("movel_operadora") or "").strip()[:40]
    movel_rssi = _i(dados.get("movel_rssi_dbm"))
    # Nível vem do Android (SignalStrength.getLevel) já ponderado por rádio e
    # operadora; clampado na faixa 0-4 porque o dado vem do device e não é confiável.
    movel_nivel = _i(dados.get("movel_nivel"))
    if movel_nivel is not None:
        movel_nivel = min(4, max(0, movel_nivel))

    # Tudo num único bloco atômico: a 5s de intervalo isso reduz commits/locks no
    # SQLite (1 transação por check-in em vez de várias autocommit em série).
    valores = dict(
        latitude=lat, longitude=lon, precisao_m=prec,
        bateria=bat, carregando=carregando, rede=rede, online=online,
        ssid=ssid, mac_em_uso=mac_em_uso,
        wifi_rssi_dbm=wifi_rssi, wifi_nivel=wifi_nivel,
        wifi_velocidade_mbps=wifi_velocidade, wifi_frequencia_mhz=wifi_frequencia,
        wifi_banda_ghz=wifi_banda,
        rede_tipo=rede_tipo, movel_geracao=movel_geracao,
        movel_tecnologia=movel_tecnologia, movel_operadora=movel_operadora,
        movel_rssi_dbm=movel_rssi, movel_nivel=movel_nivel,
    )

    with transaction.atomic():
        # Reentrega da fila (resposta HTTP perdida) não vira linha nova: é
        # absorvida pela leitura já gravada no mesmo instante. A resposta segue
        # sendo "ok" — o dado ESTÁ no servidor, e é justamente esse "ok" que faz
        # o app parar de reenviar.
        duplicada = (
            coletado_informado is not None
            and _consolidar_reenvio(device, coletado_informado, valores)
        )
        if not duplicada:
            KioskCheckin.objects.create(device=device, coletado_em=coletado, **valores)

        eh_mais_recente = device.ultimo_checkin is None or coletado >= device.ultimo_checkin
        if eh_mais_recente:
            device.ultima_latitude = lat if lat is not None else device.ultima_latitude
            device.ultima_longitude = lon if lon is not None else device.ultima_longitude
            device.ultima_precisao_m = prec if prec is not None else device.ultima_precisao_m
            device.ultima_bateria = bat if bat is not None else device.ultima_bateria
            device.ultima_rede = rede or device.ultima_rede
            device.ultimo_checkin = coletado
            # Conectividade do último check-in (snapshot p/ o selo de sinal no
            # mapa da frota). `rede_tipo` sempre existe quando `rede` veio; os
            # campos móveis são sobrescritos SEM guarda de "só se não-vazio" de
            # propósito: ao sair do 4G para o Wi-Fi, a geração/operadora antigas
            # têm de ser limpas, senão o selo mostraria "4G" num aparelho que
            # está no Wi-Fi. Mesmo motivo para o nível/dBm serem recalculados
            # sempre a partir do transporte em uso agora.
            if rede_tipo:
                device.ultima_rede_tipo = rede_tipo
            device.ultima_movel_geracao = movel_geracao
            device.ultima_movel_operadora = movel_operadora
            if rede_tipo == REDE_MOVEL:
                device.ultimo_sinal_dbm = movel_rssi
                device.ultimo_sinal_nivel = movel_nivel if movel_nivel is not None else _nivel_por_dbm(movel_rssi, _FAIXAS_DBM_MOVEL)
            elif rede_tipo == REDE_WIFI:
                device.ultimo_sinal_dbm = wifi_rssi
                device.ultimo_sinal_nivel = wifi_nivel if wifi_nivel is not None else _nivel_por_dbm(wifi_rssi, _FAIXAS_DBM_WIFI)
            else:
                device.ultimo_sinal_dbm = None
                device.ultimo_sinal_nivel = None
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
            # Cache/dados do PRÓPRIO app Quiosque — mesmo padrão de snapshot acima
            # (sempre presentes no payload, ver INFORME_SERVIDOR_CACHE_E_ENERGIA §2).
            cache_app = _i(dados.get("cache_app_mb"))
            if cache_app is not None:
                device.cache_app_mb = cache_app
            dados_app = _i(dados.get("dados_app_mb"))
            if dados_app is not None:
                device.dados_app_mb = dados_app
            # Apps em uso (v1.8.0+, vem sempre — `[]` quando nenhum). Ausente = build
            # antiga: não apaga o último snapshot conhecido.
            if apps_abertos is not None:
                device.apps_abertos = apps_abertos
        # MAC: identidade estável do aparelho → atualiza só quando chega valor não-nulo
        # (não sobrescreve um MAC bom com null vindo de um check-in sem Device Owner).
        if mac and device.mac != mac:
            device.mac = mac
        # Verificação de MAC (v1.7.0) — snapshot do estado mais recente, mesmo padrão
        # de atualizacao_status. `ultima_mac_em_uso` só é sobrescrito com leitura real
        # (null = fora de Wi-Fi/OEM mascarando, não "MAC ausente"); os textos de
        # política ficam de fora quando o app não os manda (build antiga).
        if mac_em_uso:
            device.ultima_mac_em_uso = mac_em_uso
        wifi_mac_politica = dados.get("wifi_mac_politica")
        if wifi_mac_politica is not None:
            device.wifi_mac_politica = str(wifi_mac_politica)[:255]
        wifi_rede_provisionada = dados.get("wifi_rede_provisionada")
        if wifi_rede_provisionada is not None:
            device.wifi_rede_provisionada = str(wifi_rede_provisionada)[:255]
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

        # Retenção: janela móvel de RETENCAO_DIAS, podada de forma amostrada (ver
        # _PRUNE_PROB) — não roda a cada heartbeat para manter a resposta leve.
        if random.random() < _PRUNE_PROB:
            prune_checkins(device)

        comandos = _comandos_para_entrega(device)

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


# ──────────────────────────────────────────────────────────────────────────────
# Comandos remotos (app v1.8.0+) — ver INFORME_SERVIDOR_COMANDOS_REMOTOS.md
# ──────────────────────────────────────────────────────────────────────────────

# Tipos oferecidos no painel, na ordem do <select>. Todo comando nasce com
# `expira_em`: sem isso, um aparelho que passa dias desligado executaria um
# "reiniciar" velho assim que voltasse. O que interrompe o colaborador vence
# rápido; o que é idempotente pode esperar um dia.
COMANDOS = {
    "sincronizar_config": {
        "validade_min": 1440, "confirmar": False,
        "descricao": "Puxa a configuração e reaplica todas as políticas, mesmo sem mudança. Use quando o aparelho parecer fora do padrão.",
    },
    "reenviar_inventario": {
        "validade_min": 1440, "confirmar": False,
        "descricao": "Força o reenvio da lista de apps instalados no próximo check-in.",
    },
    "exibir_mensagem": {
        "validade_min": 480, "confirmar": False,
        "descricao": "Traz o Zelo para a frente e mostra o aviso com um botão OK.",
    },
    "fechar_apps": {
        "validade_min": 60, "confirmar": True,
        "descricao": "Fecha todos os apps liberados abertos. Para fechar só um, use “Apps em uso” acima.",
    },
    "limpar_cache": {
        "validade_min": 1440, "confirmar": False,
        "descricao": "Limpa o cache do próprio Zelo. Não mexe nos outros apps.",
    },
    "bloquear_tela": {
        "validade_min": 30, "confirmar": True,
        "descricao": "Desliga a tela. O colaborador religa pelo botão de energia.",
    },
    "desbloquear": {
        "validade_min": 30, "confirmar": False,
        "descricao": "Acorda a tela remotamente (contrapartida de “Bloquear tela”) e traz o Zelo de volta à frente. Sem PIN configurado, acender a tela já revela o quiosque direto.",
    },
    "reiniciar": {
        "validade_min": 30, "confirmar": True,
        "descricao": "Reinicia o aparelho (reinício silencioso do Device Owner). Não depende do “Permitir reiniciar” da configuração.",
    },
    "reiniciar_app": {
        "validade_min": 30, "confirmar": True,
        "descricao": "Reinicia só o processo do Zelo (mais rápido que “Reiniciar aparelho”) — útil quando o app está travado/lento sem precisar derrubar o aparelho inteiro.",
    },
    "reativar_quiosque": {
        "validade_min": 1440, "confirmar": False,
        "descricao": "Volta a travar o quiosque se ele foi pausado para manutenção e ficou destravado.",
    },
    "sair_quiosque": {
        "validade_min": 15, "confirmar": True,
        "descricao": "Sai do quiosque à distância e devolve o aparelho ao Android normal, sem PIN, para quem estiver na frente dele. Só quem tem a permissão “Pode tirar aparelhos do quiosque remotamente” vê esta opção. Para travar de novo, use “Reativar quiosque”.",
    },
}
COMANDO_VALIDADES_MIN = (15, 30, 60, 240, 480, 1440, 4320)
PERM_REINICIAR_REMOTO = "ProjetoEstoque.reiniciar_quiosque_remoto"
PERM_SAIR_QUIOSQUE_REMOTO = "ProjetoEstoque.sair_quiosque_remoto"
# versionCode da primeira versão do app que executa comandos (v1.8.0).
APP_VERSAO_CODIGO_COMANDOS = 13

# versionCode da primeira versão do app que MEDE sinal 2G/3G/4G/5G (v1.10.0),
# confirmado em INFORME_SERVIDOR_ROTA_E_SINAL_MOVEL.md §6.
#
# Por que isto existe: `telemetria_movel` é só o gate do SERVIDOR. Ligá-lo num
# aparelho cujo app não sabe medir sinal móvel não produz erro nenhum — o app
# simplesmente ignora a chave e nunca manda os campos. O painel então mostraria
# "Ativa" e uma coluna de "—" para sempre, e o TI procuraria o problema no SIM
# ou no chip (é o primeiro suspeito que o próprio informe do app sugere), quando
# a causa é a versão instalada. Medido na frota em 2026-10-01: 1.7.2 entrega
# `wifi_*` em 5.969 linhas e `movel_*` em ZERO — o gate de Wi-Fi funciona na
# frota atual, o de rede móvel não pode funcionar em nenhum aparelho dela.
APP_VERSAO_CODIGO_TELEMETRIA_MOVEL = 18


def reporta_sinal_movel(device) -> bool:
    """O app instalado NESTE aparelho sabe medir 2G/3G/4G/5G?

    `app_versao_codigo` 0/ausente = build anterior ao campo, logo anterior à
    v1.10.0: incapaz. Nunca otimista — é melhor o painel dizer "aguardando
    atualização" e estar errado por excesso de cautela do que afirmar que mede
    e devolver uma coluna vazia sem explicação.
    """
    return (getattr(device, "app_versao_codigo", 0) or 0) >= APP_VERSAO_CODIGO_TELEMETRIA_MOVEL


def estado_telemetria_movel(device) -> dict:
    """Estado REAL da telemetria de rede móvel deste aparelho, para a UI.

    Separa as duas perguntas que o painel confundia numa só:
      `ligada`     — o gate do servidor está ligado (decisão do TI);
      `reportando` — o app deste aparelho sabe cumprir o gate (capacidade);
      `aguardando` — ligada mas o app é antigo: o estado que precisa de aviso.

    `mostrar_sinal` é o que as colunas/selos de sinal móvel devem consultar:
    só há o que mostrar quando as duas condições valem.
    """
    ligada = bool(getattr(device, "telemetria_movel", False))
    reportando = reporta_sinal_movel(device)
    return {
        "ligada": ligada,
        "reportando": reportando,
        "aguardando": ligada and not reportando,
        "mostrar_sinal": ligada and reportando,
        "versao_minima": "1.10.0",
        "app_versao": getattr(device, "app_versao", "") or "",
    }

# O app tolera 1 min de diferença de relógio antes de recusar por `expira_em`;
# o servidor espera um pouco mais antes de tirar o comando da fila.
_COMANDO_FOLGA_EXPIRACAO = timedelta(minutes=2)
_COMANDO_DETALHE_MAX = 2000
_MENSAGEM_MAX = 500
_TITULO_MAX = 80
_PACOTES_MAX = 50
_ACK_STATUS = ("executado", "falhou", "nao_suportado", "expirado")


def _lista_pacotes(valor, limite: int = _PACOTES_MAX):
    """Normaliza uma lista de package names (dado não confiável: vem do aparelho
    ou do POST). None quando não é lista — campo ausente ≠ lista vazia."""
    if not isinstance(valor, list):
        return None
    pacotes = []
    for p in valor:
        p = p.strip()[:_APPS_PKG_MAX] if isinstance(p, str) else ""
        if p and p not in pacotes:
            pacotes.append(p)
            if len(pacotes) >= limite:
                break
    return pacotes


def _validade_label(minutos: int) -> str:
    if minutos < 60:
        return f"{minutos} min"
    horas = minutos // 60
    if horas < 24 or horas % 24:
        return f"{horas} h"
    dias = horas // 24
    return f"{dias} dia{'s' if dias > 1 else ''}"


def opcoes_comando(pode_reiniciar: bool, pode_sair_quiosque: bool = False) -> list:
    """Tipos para o <select> do painel. "Reiniciar" e "Sair do quiosque" só
    aparecem para quem tem a permissão correspondente (a view revalida no
    POST)."""
    from ProjetoEstoque.models import KioskComando

    rotulos = dict(KioskComando.Tipo.choices)
    return [
        {
            "valor": tipo,
            "rotulo": rotulos[tipo],
            "descricao": cfg["descricao"],
            "confirmar": cfg["confirmar"],
            "validade_label": _validade_label(cfg["validade_min"]),
        }
        for tipo, cfg in COMANDOS.items()
        if (tipo != KioskComando.Tipo.REINICIAR or pode_reiniciar)
        and (tipo != KioskComando.Tipo.SAIR_QUIOSQUE or pode_sair_quiosque)
    ]


def opcoes_validade() -> list:
    return [{"min": m, "label": _validade_label(m)} for m in COMANDO_VALIDADES_MIN]


def criar_comando(device, tipo: str, *, user=None, mensagem: str = "", titulo: str = "",
                  pacotes=None, validade_min=None):
    """Enfileira um comando para o aparelho (sai no próximo check-in).

    Levanta ValueError com a mensagem pronta para o usuário. A autorização
    (ex.: permissão para reiniciar) é checada na view.
    """
    from ProjetoEstoque.models import KioskComando

    tipo = (tipo or "").strip()
    cfg = COMANDOS.get(tipo)
    if cfg is None:
        raise ValueError("Tipo de comando inválido.")
    if not device.ativo:
        raise ValueError("Dispositivo revogado: ele não faz mais check-in e nunca receberia o comando.")

    payload = {}
    if tipo == KioskComando.Tipo.EXIBIR_MENSAGEM:
        mensagem = (mensagem or "").strip()[:_MENSAGEM_MAX]
        if not mensagem:
            raise ValueError("Informe o texto da mensagem.")
        payload["mensagem"] = mensagem
        titulo = (titulo or "").strip()[:_TITULO_MAX]
        if titulo:
            payload["titulo"] = titulo
    elif tipo == KioskComando.Tipo.FECHAR_APPS:
        # Sem lista = o app fecha todos os liberados.
        lista = _lista_pacotes(pacotes)
        if lista:
            payload["pacotes"] = lista

    try:
        minutos = int(validade_min)
    except (TypeError, ValueError):
        minutos = None
    if minutos not in COMANDO_VALIDADES_MIN:
        minutos = cfg["validade_min"]
    expira_em = (timezone.now() + timedelta(minutes=minutos)).replace(microsecond=0)

    return KioskComando.objects.create(
        device=device, tipo=tipo, payload=payload, expira_em=expira_em, criado_por=user,
    )


def _comandos_para_entrega(device) -> list:
    """Monta o `comandos` da resposta do check-in: todos os abertos (pendentes +
    entregues ainda sem ACK), na ordem em que foram pedidos.

    Reentrega até o ACK: o app deduplica por `id`, então repetir é seguro e
    cobre a perda de uma resposta de rede (inclusive na rajada da fila offline).
    Os vencidos saem da fila aqui como `expirado`; um ACK que chegue depois (o
    app insiste por até 24 h) ainda sobrescreve com o desfecho real. As escritas
    filtram pelo status lido para não atropelar um ACK que chegue no meio.
    """
    from ProjetoEstoque.models import KioskComando

    St = KioskComando.Status
    agora = timezone.now()
    entregar, novos, vencidos_pendentes, vencidos_entregues = [], [], [], []
    for c in device.comandos.filter(status__in=KioskComando.ABERTOS).order_by("criado_em"):
        if c.expira_em and c.expira_em + _COMANDO_FOLGA_EXPIRACAO < agora:
            (vencidos_pendentes if c.status == St.PENDENTE else vencidos_entregues).append(c.pk)
            continue
        if c.status == St.PENDENTE:
            novos.append(c.pk)
        entregar.append({
            "id": c.pk,
            "tipo": c.tipo,
            "payload": c.payload or {},
            "expira_em": timezone.localtime(c.expira_em).isoformat(timespec="seconds") if c.expira_em else None,
        })

    if novos:
        KioskComando.objects.filter(pk__in=novos, status=St.PENDENTE).update(status=St.ENTREGUE, entregue_em=agora)
    if vencidos_pendentes:
        KioskComando.objects.filter(pk__in=vencidos_pendentes, status=St.PENDENTE).update(
            status=St.EXPIRADO, finalizado_em=agora,
            detalhe="Expirou antes de ser entregue: o aparelho não fez check-in dentro do prazo.",
        )
    if vencidos_entregues:
        KioskComando.objects.filter(pk__in=vencidos_entregues, status=St.ENTREGUE).update(
            status=St.EXPIRADO, finalizado_em=agora,
            detalhe="Expirou sem confirmação do aparelho (sem rede, ou app anterior à v1.8.0).",
        )
    return entregar


def registrar_ack_comando(device, comando_id, dados: dict) -> bool:
    """O aparelho confirma o desfecho de um comando (POST /comando/<id>/ack/).

    False se o `id` não é deste aparelho (a view responde 404 e o app para de
    tentar); ValueError para `status` fora do contrato (400). Idempotente: numa
    reentrega o app manda o ACK de novo com o resultado original, e um ACK
    tardio sobrescreve o `expirado` que o servidor tenha marcado por conta própria.
    """
    c = device.comandos.filter(id=comando_id).first()
    if c is None:
        return False
    status = str(dados.get("status") or "").strip().lower()
    if status not in _ACK_STATUS:
        raise ValueError(f"Status inválido. Use: {', '.join(_ACK_STATUS)}.")
    try:
        executado_em = _parse_dt(dados.get("executado_em"))
    except ValueError:
        executado_em = None

    c.status = status
    c.detalhe = str(dados.get("detalhe") or "")[:_COMANDO_DETALHE_MAX]
    c.executado_em = executado_em
    c.app_versao = str(dados.get("app_versao") or "")[:20]
    c.finalizado_em = timezone.now()
    c.save(update_fields=["status", "detalhe", "executado_em", "app_versao", "finalizado_em"])
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
# Máximo de pontos recentes considerados no traço padrão (janela de rota mais
# recente, sem filtro de dia — mantém o mapa do detalhe leve).
TRILHA_MAX_PONTOS = 150
# Ao filtrar UM dia específico o traço deve cobrir o dia inteiro (não só uma
# janela recente) — teto maior, e decimado (ver _decimar_trilha) se precisar.
TRILHA_DIA_MAX_PONTOS = 600
# Teto de segurança na leitura bruta de um único dia (defesa contra um device
# mal configurado com intervalo de check-in no mínimo de 5s: 86400/5 = 17280).
TRILHA_DIA_FETCH_MAX = 20000
# Limiares iniciais de decimação (distância/tempo mínimos entre pontos mantidos
# do traço de um dia) — crescem geometricamente até caber em TRILHA_DIA_MAX_PONTOS.
TRILHA_DECIM_MIN_M = 12.0
TRILHA_DECIM_MIN_S = 20.0

# Janelas de tempo oferecidas no filtro do mapa de rota. Substituem o antigo
# "últimos 150 pontos" como modo padrão: com o app em 5s, 150 pontos cobriam
# ~12 minutos de trajeto — inútil para quem percorre a fazenda por horas. A
# janela é por TEMPO e o volume é resolvido pela decimação, que preserva a forma.
TRILHA_JANELAS_H = (1, 6, 12, 24)
TRILHA_JANELA_PADRAO_H = 6


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    """Distância em metros entre duas coordenadas (fórmula de haversine)."""
    from math import radians, sin, cos, asin, sqrt
    raio = 6371000.0
    dphi = radians(lat2 - lat1)
    dlmb = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlmb / 2) ** 2
    return 2 * raio * asin(sqrt(a))


def intervalo_dia_local(dia: date) -> tuple:
    """[início, fim) de um dia de calendário no fuso local, como datetimes aware
    (para filtrar campos DateTimeField sem depender de TruncDate em cada query)."""
    tz = timezone.get_current_timezone()
    inicio = timezone.make_aware(datetime.combine(dia, datetime.min.time()), tz)
    return inicio, inicio + timedelta(days=1)


_DIAS_SEMANA_PT = ["Segunda-feira", "Terça-feira", "Quarta-feira", "Quinta-feira", "Sexta-feira", "Sábado", "Domingo"]


def _rotulo_dia(dia: date, hoje: date | None = None) -> str:
    """Rótulo do chip de filtro por dia. Sempre inclui a data (dd/mm) — inclusive em
    "Hoje"/"Ontem" — para o dia exato da coleta ficar explícito em qualquer botão,
    sem depender só do rótulo relativo."""
    hoje = hoje or timezone.localdate()
    delta = (hoje - dia).days
    data_fmt = dia.strftime('%d/%m')
    if delta == 0:
        return f"Hoje, {data_fmt}"
    if delta == 1:
        return f"Ontem, {data_fmt}"
    return f"{_DIAS_SEMANA_PT[dia.weekday()]}, {data_fmt}"


# Teto de chips no seletor de dias. A quantidade REAL é limitada pela retenção;
# este número só impede que um aparelho que entregou um atraso enorme encha a
# tela de chips.
DIAS_SELETOR_MAX = 31


def dias_disponiveis_checkin(device) -> list:
    """Dias com pelo menos um check-in guardado — alimenta os filtros de
    "histórico por dia" na tela de detalhe. Mais recente primeiro.
    [{data, label, total}].

    Lista o que EXISTE na tabela, sem recortar por "últimos N dias". O recorte
    por data de coleta escondia exatamente o dado que `prune_checkins` faz
    questão de preservar: a leitura coletada há muito tempo e entregue agora
    (fila offline longa) fica guardada, e precisa aparecer para ser consultável —
    dado retido mas inalcançável pela tela é o mesmo que dado perdido.
    """
    from django.db.models import Count
    from django.db.models.functions import TruncDate
    from ProjetoEstoque.models import KioskCheckin

    hoje = timezone.localdate()
    linhas = (
        KioskCheckin.objects
        .filter(device=device)
        .annotate(ts=Coalesce("coletado_em", "registrado_em"))
        .annotate(d=TruncDate("ts"))
        .values("d")
        .annotate(total=Count("id"))
        .order_by("-d")
    )
    return [
        {"data": row["d"], "label": _rotulo_dia(row["d"], hoje), "total": row["total"]}
        for row in linhas
        if row["d"] is not None
    ][:DIAS_SELETOR_MAX]


def estatisticas_retencao(device) -> dict:
    """Cobertura REAL do histórico guardado deste aparelho — alimenta o aviso de
    retenção na tela de detalhe.

    Mostra o que existe, não o que a política promete: um aparelho matriculado
    ontem tem 1 dia de histórico, não 15, e dizer "15 dias" ali seria mentir
    sobre a base de uma análise. `dias_cobertos` conta dias-calendário distintos
    com leitura, e não a diferença entre extremos, porque um aparelho que passou
    a semana desligado tem buracos que não são cobertura.
    """
    from django.db.models import Count, Max, Min
    from django.db.models.functions import TruncDate
    from ProjetoEstoque.models import KioskCheckin

    base = KioskCheckin.objects.filter(device=device).annotate(
        ts=Coalesce("coletado_em", "registrado_em")
    )
    ag = base.aggregate(total=Count("id"), inicio=Min("ts"), fim=Max("ts"))
    total = ag["total"] or 0
    if not total:
        return {
            "total": 0, "inicio": None, "fim": None, "dias_cobertos": 0,
            "retencao_dias": RETENCAO_DIAS, "completo": False,
        }

    dias = base.annotate(d=TruncDate("ts")).values("d").distinct().count()
    return {
        "total": total,
        "inicio": timezone.localtime(ag["inicio"]) if ag["inicio"] else None,
        "fim": timezone.localtime(ag["fim"]) if ag["fim"] else None,
        "dias_cobertos": dias,
        "retencao_dias": RETENCAO_DIAS,
        # Só é "janela cheia" quando há dado cobrindo o período todo — o que
        # separa "guardamos 15 dias" de "este aparelho ainda não tem 15 dias".
        "completo": dias >= RETENCAO_DIAS,
    }


def _mudou_conectividade(a: dict, b: dict) -> bool:
    """True quando dois pontos consecutivos diferem no que a rota precisa MOSTRAR:
    estado de conexão, origem da leitura (fila offline) ou rótulo/força de sinal.

    Usado como trava da decimação: um ponto que marca transição nunca é
    descartado por estar "perto do anterior". Sem isso, decimar um dia inteiro
    apagaria justamente o ponto onde a conexão caiu — a informação que a tela
    existe para dar.
    """
    return (
        a["online"] != b["online"]
        or a["fila"] != b["fila"]
        or a["sinal_rotulo"] != b["sinal_rotulo"]
        or a["sinal_nivel"] != b["sinal_nivel"]
    )


def _decimar_trilha(pontos: list, alvo: int) -> list:
    """Reduz um traço muito denso (ex.: device configurado com check-in a cada
    5s) a no máximo `alvo` pontos, preservando a FORMA do trajeto: descarta um
    ponto só quando está muito perto (distância E tempo) do último ponto
    mantido — o dispositivo parado gera muitos pontos redundantes; em
    movimento, os pontos ficam naturalmente mais espaçados e são preservados.
    Primeiro e último ponto do dia são sempre mantidos (partida/chegada exatas
    para o resumo do dia), assim como toda transição de conectividade/sinal
    (ver `_mudou_conectividade`) — que é conteúdo, não redundância."""
    if len(pontos) <= alvo:
        return pontos
    min_m, min_s = TRILHA_DECIM_MIN_M, TRILHA_DECIM_MIN_S
    mantidos = pontos
    for _ in range(8):  # crescimento geométrico converge em poucas iterações
        mantidos = [pontos[0]]
        for p in pontos[1:-1]:
            ult = mantidos[-1]
            dist = _haversine_m(ult["lat"], ult["lon"], p["lat"], p["lon"])
            dt = (p["_ts"] - ult["_ts"]).total_seconds()
            if dist >= min_m or dt >= min_s or _mudou_conectividade(ult, p):
                mantidos.append(p)
        mantidos.append(pontos[-1])
        if len(mantidos) <= alvo:
            break
        min_m *= 1.7
        min_s *= 1.7
    return mantidos


# Raio (m) dentro do qual leituras consecutivas são consideradas "o mesmo
# lugar" — ruído normal de GPS/Wi-Fi mesmo com o aparelho parado (ex.: tablet
# de ponto fixado na parede). Sem isso, um device que NUNCA se move ainda gera
# dezenas de leituras espalhadas num raio de poucos metros, e a polilinha do
# traço — que conecta cada leitura na ordem cronológica — vira um emaranhado
# de linhas cruzadas sem direção nenhuma (visualmente parece "andou muito" sem
# ter andado nada). Também inflava a distância percorrida no resumo do dia,
# somando ruído como se fosse deslocamento real.
STAY_RADIUS_M = 35.0
# Só colapsa em "parado" quando há pelo menos N leituras seguidas na mesma
# área — 1-2 pontos próximos é normal (não formam a "teia") e ficam como estão.
STAY_MIN_PONTOS = 3


def _colapsar_paradas(pontos: list) -> list:
    """Reduz sequências de leituras que ficam dentro de STAY_RADIUS_M de onde
    a sequência começou a UM ponto representativo (centróide), marcado com
    `parado=True`. A ANCORAGEM no primeiro ponto da sequência (não no ponto
    anterior) evita que uma deriva lenta e cumulativa — vários passos pequenos
    de ruído, cada um dentro do raio do anterior — escape do raio real de
    "parado" e seja tratada como uma sequência só; qualquer leitura que se
    afaste mais de STAY_RADIUS_M de onde a sequência começou fecha o cluster
    atual e abre um novo (evidência real de deslocamento).

    Preserva a ordem cronológica. Não mexe em sequências menores que
    STAY_MIN_PONTOS (ficam como pontos individuais — não há "teia" a resolver)."""
    if len(pontos) < STAY_MIN_PONTOS + 1:
        return pontos

    def _centro(cluster: list) -> dict:
        n = len(cluster)
        precisoes = [p["precisao"] for p in cluster if p["precisao"] is not None]
        # Conectividade do cluster: o aparelho ficou parado no mesmo lugar, mas a
        # conexão pode ter oscilado ali. Contamos as duas metades — é o dado que
        # responde "nesse ponto eu tinha sinal?" com honestidade ("12 leituras:
        # 9 com conexão, 3 sem") — e a COR do ponto segue a maioria, para o mapa
        # não pintar de vermelho um lugar onde a conexão só piscou.
        offline_pontos = sum(1 for p in cluster if not p["online"])
        online_pontos = n - offline_pontos
        # Sinal representativo: o pior nível observado na parada. Numa área de
        # sombra o que importa é o teto de degradação, não a média otimista.
        niveis = [p["sinal_nivel"] for p in cluster if p["sinal_nivel"] is not None]
        pior = min(niveis) if niveis else None
        base = next((p for p in cluster if p["sinal_nivel"] == pior), cluster[-1]) if pior is not None else cluster[-1]
        return {
            "id": cluster[-1]["id"],
            "lat": sum(p["lat"] for p in cluster) / n,
            "lon": sum(p["lon"] for p in cluster) / n,
            "precisao": max(precisoes) if precisoes else None,
            "quando": cluster[-1]["quando"],
            "quando_inicio": cluster[0]["quando"],
            # Uma parada COBRE um intervalo: guardamos os dois extremos em ISO
            # para quem precisa medir duração (ex.: quanto tempo o aparelho ficou
            # sem conexão parado ali) não ter que reinterpretar texto.
            "ts": cluster[-1]["ts"],
            "ts_inicio": cluster[0]["ts"],
            "bateria": cluster[-1]["bateria"],
            "online": online_pontos >= offline_pontos,
            "fila": any(p["fila"] for p in cluster),
            "atraso_s": max(p["atraso_s"] for p in cluster),
            "rede": base["rede"],
            "sinal_tipo": base["sinal_tipo"],
            "sinal_rotulo": base["sinal_rotulo"],
            "sinal_nivel": pior,
            "sinal_dbm": base["sinal_dbm"],
            "sinal_operadora": base["sinal_operadora"],
            "_ts": cluster[-1]["_ts"],
            "parado": True,
            "pontos_originais": n,
            "online_pontos": online_pontos,
            "offline_pontos": offline_pontos,
        }

    resultado, cluster, ancora = [], [pontos[0]], pontos[0]
    for p in pontos[1:]:
        if _haversine_m(ancora["lat"], ancora["lon"], p["lat"], p["lon"]) <= STAY_RADIUS_M:
            cluster.append(p)
            continue
        resultado.append(_centro(cluster) if len(cluster) >= STAY_MIN_PONTOS else cluster)
        cluster, ancora = [p], p
    resultado.append(_centro(cluster) if len(cluster) >= STAY_MIN_PONTOS else cluster)

    # `resultado` mistura clusters colapsados (dict único) com trechos que
    # ficaram como lista de pontos individuais — achata tudo numa lista plana.
    achatado = []
    for item in resultado:
        achatado.extend(item) if isinstance(item, list) else achatado.append(item)
    return achatado


def _marcar_marcos(trilha: list) -> None:
    """Marca (in-place) os pontos que merecem um SELO visível no mapa.

    Pôr o rótulo de sinal em cima de todos os pontos de um trajeto de 600
    leituras deixa o mapa ilegível. O selo aparece onde a informação muda de
    fato: início, fim, paradas e toda transição de conexão/sinal
    (`_mudou_conectividade`). O resto continua clicável — o dado está no ponto,
    só o rótulo é que não é desenhado por padrão.
    """
    anterior = None
    for i, p in enumerate(trilha):
        primeiro_ou_ultimo = i == 0 or i == len(trilha) - 1
        p["marco"] = bool(
            primeiro_ou_ultimo
            or p.get("parado")
            or (anterior is not None and _mudou_conectividade(anterior, p))
        )
        anterior = p


def montar_trilha(device, *, dia: date | None = None, horas: int | None = None,
                  max_pontos: int = TRILHA_MAX_PONTOS) -> list:
    """
    Monta o traço de deslocamento do device para o mapa do detalhe, priorizando a
    PRECISÃO do caminho:

      1. Ordena pelo horário REAL de coleta (coletado_em), não pela chegada ao
         servidor — corrige a forma da rota quando o app entrega uma fila offline
         em rajada (registrado_em fora de ordem). É o que faz a rota ficar certa
         mesmo para os trechos que o aparelho guardou na memória por estar
         offline: cada leitura entra no lugar onde foi COLETADA, não onde chegou.
      2. Descarta fixes ruins (precisao_m acima do limite) que jogam o traço longe.
      3. Remove saltos impossíveis (velocidade acima do limite) — glitches de GPS.

    Janela (nesta ordem de precedência):
      - `dia`   → o dia inteiro (00:00–23:59 no fuso local).
      - `horas` → as últimas N horas.
      - nenhum  → últimos `max_pontos` pontos (modo legado, mais leve).
    Em qualquer janela por tempo o volume é resolvido pela decimação
    (`_decimar_trilha`), que preserva a forma e as transições de conexão.

    Devolve a lista em ordem CRONOLÓGICA (antigo → recente); cada ponto traz
    posição, telemetria e a classificação de conectividade daquele instante
    (ver `conexao_do_checkin` e `sinal_do_checkin`).
    """
    from ProjetoEstoque.models import KioskCheckin

    query = (
        KioskCheckin.objects
        .filter(device=device, latitude__isnull=False, longitude__isnull=False)
        .annotate(ts=Coalesce("coletado_em", "registrado_em"))
    )
    decimar_para = None
    if dia is not None:
        inicio, fim = intervalo_dia_local(dia)
        base = list(query.filter(ts__gte=inicio, ts__lt=fim).order_by("ts")[:TRILHA_DIA_FETCH_MAX])
        decimar_para = TRILHA_DIA_MAX_PONTOS
    elif horas:
        base = list(query.filter(ts__gte=timezone.now() - timedelta(hours=horas)).order_by("ts")[:TRILHA_DIA_FETCH_MAX])
        decimar_para = TRILHA_DIA_MAX_PONTOS
    else:
        base = list(query.order_by("-ts")[:max_pontos])
        base.reverse()  # cronológico ascendente (antigo → recente)

    # Limiar de "guardado na memória" calculado UMA vez para este aparelho
    # (ver limiar_fila) — dentro do laço, ler c.device seria 1 query por linha.
    atraso_fila = limiar_fila(device)

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
            conexao = conexao_do_checkin(c, atraso_fila)
            sinal = sinal_do_checkin(c)
            ponto = {
                "id": c.pk,
                "lat": c.latitude,
                "lon": c.longitude,
                "precisao": c.precisao_m,
                "quando": timezone.localtime(ts).strftime("%d/%m/%Y %H:%M"),
                # Mesmo instante em ISO 8601. `quando` é para exibir; `ts` é para
                # CALCULAR (duração de um trecho sem conexão, por exemplo) sem
                # ninguém precisar reinterpretar o texto formatado acima.
                "ts": timezone.localtime(ts).isoformat(),
                "bateria": c.bateria,
                "online": conexao["online"],
                "fila": conexao["fila"],
                "atraso_s": conexao["atraso_s"],
                "rede": c.rede or "",
                "sinal_tipo": sinal["tipo"],
                "sinal_rotulo": sinal["rotulo"],
                "sinal_nivel": sinal["nivel"],
                "sinal_dbm": sinal["dbm"],
                "sinal_operadora": sinal["operadora"],
                "_ts": ts,
            }
            pontos.append(ponto)
            prev = ponto
        return pontos

    trilha = _construir(filtrar_precisao=True)
    # Se o filtro de precisão zerou o traço (device cujo GPS é sempre ruim), refaz
    # sem ele para ainda assim mostrar algum caminho.
    if len(trilha) < 2:
        trilha = _construir(filtrar_precisao=False)

    # Colapsa "paradas" (ruído de GPS/Wi-Fi com o aparelho fisicamente parado)
    # ANTES da decimação: um device parado gera muitos pontos no mesmo lugar, e
    # é exatamente esse volume que faria a decimação (pensada para trajetos
    # longos de verdade) cortar pontos do jeito errado.
    trilha = _colapsar_paradas(trilha)

    if decimar_para and len(trilha) > decimar_para:
        trilha = _decimar_trilha(trilha, decimar_para)

    _marcar_marcos(trilha)
    for p in trilha:
        p.pop("_ts", None)
    return trilha


def resumir_cobertura(trilha: list) -> dict:
    """Estatísticas de conectividade e deslocamento do traço exibido — alimenta a
    barra de informação sob o mapa e a legenda dos filtros.

    Vale para QUALQUER janela (dia, últimas N horas ou modo legado), ao contrário
    de `montar_resumo_dia`, que só existe com um dia filtrado. As leituras de uma
    parada colapsada contam pelo seu volume ORIGINAL (`pontos_originais`), senão
    um aparelho parado 3h num ponto sem sinal apareceria como "1 leitura offline".
    """
    leituras = online = offline = fila = 0
    distancia_m = 0.0
    niveis, geracoes, operadoras = [], {}, {}

    for p in trilha:
        n = p.get("pontos_originais", 1)
        leituras += n
        if p["online"]:
            online += n
        else:
            offline += n
        if p["fila"]:
            fila += p.get("offline_pontos", n) or n
        if p["sinal_nivel"] is not None:
            niveis.append(p["sinal_nivel"])
        if p.get("sinal_tipo") == REDE_MOVEL and p["sinal_rotulo"]:
            geracoes[p["sinal_rotulo"]] = geracoes.get(p["sinal_rotulo"], 0) + n
        if p.get("sinal_operadora"):
            operadoras[p["sinal_operadora"]] = operadoras.get(p["sinal_operadora"], 0) + n

    for a, b in zip(trilha, trilha[1:]):
        distancia_m += _haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])

    return {
        "pontos": len(trilha),
        "leituras": leituras,
        "online": online,
        "offline": offline,
        "fila": fila,
        "pct_online": round(online / leituras * 100) if leituras else 0,
        "distancia_km": round(distancia_m / 1000, 2),
        # Trechos SEM conexão desenhados no mapa — corridas consecutivas de
        # pontos offline. Conta exatamente o que se vê (um trecho vermelho = 1),
        # e por isso NÃO é "quedas de sinal do dia": essa é `montar_resumo_dia`,
        # que olha os check-ins crus, sem paradas colapsadas nem decimação. Dois
        # números diferentes com o mesmo nome se contradiriam na tela.
        "segmentos_offline": sum(
            1 for i, p in enumerate(trilha)
            if not p["online"] and (i == 0 or trilha[i - 1]["online"])
        ),
        "sinal_medio": round(sum(niveis) / len(niveis), 1) if niveis else None,
        "sinal_minimo": min(niveis) if niveis else None,
        "tem_sinal": bool(niveis),
        "geracao_predominante": max(geracoes, key=geracoes.get) if geracoes else "",
        "operadora_predominante": max(operadoras, key=operadoras.get) if operadoras else "",
    }


def montar_mapa_dict(device, trilha: list, dia: date | None = None) -> dict | None:
    """Monta o dict de mapa consumido pelo template/AJAX do detalhe.

    - SEM filtro de dia: o pino de posição usa os campos brutos mais recentes
      do device (`ultima_latitude`/`ultima_longitude`/`ultima_precisao_m`) —
      sempre o ÚLTIMO fix relatado, mesmo que sua precisão seja pior que o
      filtro de qualidade do traço (TRILHA_PRECISAO_MAX_M). Antes o pino usava
      o último ponto do traço já filtrado e podia "travar" numa posição antiga
      sempre que os fixes mais novos ficassem um pouco acima do limiar de
      precisão — dando a impressão de que "o mapa não atualiza". O traço
      (polilinha) continua vindo de `montar_trilha`, já limpo de glitches.
    - COM filtro de dia: é histórico, não existe "posição atual" — o pino
      mostra o último ponto do TRAÇO DAQUELE DIA. Sem fallback para os campos
      globais do device (que refletem o dia mais recente, não o dia filtrado —
      usar esse fallback mostraria uma posição de outro dia, confundindo quem
      está olhando um dia passado).
    """
    nome = device.apelido or device.modelo or "Quiosque"
    cobertura = resumir_cobertura(trilha)
    if dia is not None:
        if not trilha:
            return None
        ultimo = trilha[-1]
        return {
            "nome": nome, "lat": ultimo["lat"], "lon": ultimo["lon"], "precisao": ultimo["precisao"],
            "online": device.online, "trilha": trilha, "historico": True,
            "cobertura": cobertura, "sinal": sinal_atual_device(device),
        }

    if not device.tem_localizacao:
        return None
    return {
        "nome": nome, "lat": device.ultima_latitude, "lon": device.ultima_longitude,
        "precisao": device.ultima_precisao_m, "online": device.online,
        "trilha": trilha, "historico": False,
        "cobertura": cobertura, "sinal": sinal_atual_device(device),
    }


def sinal_atual_device(device) -> dict:
    """Sinal do ÚLTIMO check-in a partir do snapshot no device — mesmo formato de
    `sinal_do_checkin`, para o selo do mapa (frota e detalhe) consumir os dois
    sem ramificar. Usa o snapshot, e não uma consulta ao último KioskCheckin,
    para a tela da frota não virar um N+1 com 40 aparelhos."""
    tipo = device.ultima_rede_tipo or normalizar_rede_tipo(device.ultima_rede)
    geracao = (device.ultima_movel_geracao or "").lower()
    if tipo == REDE_MOVEL:
        rotulo = geracao.upper() if geracao else REDE_ROTULOS[REDE_MOVEL]
    else:
        rotulo = REDE_ROTULOS.get(tipo, REDE_ROTULOS[REDE_NENHUMA])
    return {
        "tipo": tipo or REDE_NENHUMA,
        "rotulo": rotulo,
        "geracao": geracao,
        "nivel": device.ultimo_sinal_nivel,
        "dbm": device.ultimo_sinal_dbm,
        "operadora": device.ultima_movel_operadora or "",
    }


def montar_resumo_dia(device, dia: date, trilha: list) -> dict:
    """Resumo em linguagem natural + estatísticas do dia filtrado no detalhe do
    dispositivo — traça o percurso e cruza telemetria (bateria/rede/online) dos
    check-ins REAIS daquele dia (não a versão decimada usada no traço do mapa),
    para números fiéis mesmo quando o traço precisou ser reduzido."""
    from collections import Counter
    from ProjetoEstoque.models import KioskCheckin

    label = _rotulo_dia(dia)
    inicio, fim = intervalo_dia_local(dia)
    checkins_dia = list(
        KioskCheckin.objects
        .filter(device=device)
        .annotate(ts=Coalesce("coletado_em", "registrado_em"))
        .filter(ts__gte=inicio, ts__lt=fim)
        .order_by("ts")
    )
    total = len(checkins_dia)
    if not total:
        return {
            "total_checkins": 0, "label": label,
            "resumo_texto": f"Nenhum check-in registrado em {label.lower()}.",
        }

    primeiro, ultimo = checkins_dia[0], checkins_dia[-1]
    baterias = [c.bateria for c in checkins_dia if c.bateria is not None]
    redes = Counter(c.rede for c in checkins_dia if c.rede)
    rede_top = redes.most_common(1)[0][0] if redes else None

    # Conectividade pela MESMA classificação usada no mapa (`conexao_do_checkin`),
    # não pelo campo `online` cru — assim o número no resumo e a cor do traço no
    # mapa nunca se contradizem, inclusive nas leituras entregues de fila.
    atraso_fila = limiar_fila(device)
    conexoes = [conexao_do_checkin(c, atraso_fila) for c in checkins_dia]
    online_count = sum(1 for k in conexoes if k["online"])
    fila_count = sum(1 for k in conexoes if k["fila"])
    pct_online = round(online_count / total * 100)
    # Quedas = transições com→sem conexão ao longo do dia (não o total de
    # leituras offline): é o que responde "quantas vezes perdi o sinal".
    quedas = sum(1 for a, b in zip(conexoes, conexoes[1:]) if a["online"] and not b["online"])

    # Tempo sem conexão: soma dos intervalos entre leituras consecutivas em que a
    # segunda estava offline. Melhor que "nº de leituras × intervalo" porque
    # respeita o intervalo real (que varia com a configuração e com o Doze).
    segundos_offline = 0
    for (ca, ka), (cb, kb) in zip(zip(checkins_dia, conexoes), zip(checkins_dia[1:], conexoes[1:])):
        if not kb["online"]:
            delta = (cb.quando - ca.quando).total_seconds()
            if 0 < delta <= 3600:  # ignora buracos > 1h (aparelho desligado, não "offline")
                segundos_offline += int(delta)
    offline_min = round(segundos_offline / 60)

    sinais = [sinal_do_checkin(c) for c in checkins_dia]
    niveis = [s["nivel"] for s in sinais if s["nivel"] is not None]
    geracoes = Counter(s["rotulo"] for s in sinais if s["tipo"] == REDE_MOVEL and s["rotulo"])
    operadoras = Counter(s["operadora"] for s in sinais if s["tipo"] == REDE_MOVEL and s["operadora"])

    distancia_km = 0.0
    for a, b in zip(trilha, trilha[1:]):
        distancia_km += _haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
    distancia_km = round(distancia_km / 1000, 2)

    p_local, u_local = timezone.localtime(primeiro.quando), timezone.localtime(ultimo.quando)
    duracao_min = max(0, round((ultimo.quando - primeiro.quando).total_seconds() / 60))
    horas, minutos = divmod(duracao_min, 60)
    duracao_label = f"{horas}h{minutos:02d}min" if horas else f"{minutos}min"

    partes = [f"{total} check-in(s) em {label.lower()}, entre {p_local:%H:%M} e {u_local:%H:%M} ({duracao_label})."]
    if distancia_km >= 0.05:
        partes.append(f"Percorreu aproximadamente {distancia_km:.2f} km no traço registrado.")
    if baterias:
        if len(baterias) >= 2 and baterias[0] != baterias[-1]:
            partes.append(f"Bateria variou de {baterias[0]}% para {baterias[-1]}%.")
        else:
            partes.append(f"Bateria em torno de {baterias[-1]}%.")

    conectividade = f"Manteve conexão em {pct_online}% das leituras"
    if geracoes:
        conectividade += f", predominantemente em {geracoes.most_common(1)[0][0]}"
        if operadoras:
            conectividade += f" ({operadoras.most_common(1)[0][0]})"
    elif rede_top:
        conectividade += f", majoritariamente via {rede_top}"
    partes.append(conectividade + ".")

    if quedas:
        texto_queda = f"Perdeu o sinal {quedas} vez(es)"
        if offline_min:
            texto_queda += f", somando cerca de {offline_min} min sem conexão"
        if fila_count:
            texto_queda += f"; {fila_count} leitura(s) ficaram guardadas na memória do aparelho até a rede voltar"
        partes.append(texto_queda + ".")
    elif fila_count:
        partes.append(f"{fila_count} leitura(s) chegaram com atraso, guardadas na memória do aparelho.")

    return {
        "total_checkins": total,
        "label": label,
        "primeiro_checkin": p_local,
        "ultimo_checkin": u_local,
        "duracao_label": duracao_label,
        "bateria_inicial": baterias[0] if baterias else None,
        "bateria_final": baterias[-1] if baterias else None,
        "rede_predominante": rede_top,
        "pct_online": pct_online,
        "quedas": quedas,
        "offline_min": offline_min,
        "fila_count": fila_count,
        "sinal_medio": round(sum(niveis) / len(niveis), 1) if niveis else None,
        "sinal_minimo": min(niveis) if niveis else None,
        "geracao_predominante": geracoes.most_common(1)[0][0] if geracoes else "",
        "operadora_predominante": operadoras.most_common(1)[0][0] if operadoras else "",
        "pontos_no_traco": len(trilha),
        "distancia_km": distancia_km,
        "resumo_texto": " ".join(partes),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Mapa de sinal (uma tela por aparelho/dia — cobertura no espaço e no tempo)
# ──────────────────────────────────────────────────────────────────────────────
# Diferente do traço de rota de `montar_trilha`: lá o objetivo é o CAMINHO, e
# por isso fixes ruins e saltos impossíveis são descartados e as paradas são
# colapsadas num ponto só. Aqui o objetivo é o SINAL em cada leitura, então
# nenhuma leitura com coordenada é descartada:
#
#   • não há filtro de precisão — um fix de 1.700 m ainda diz "neste pedaço da
#     fazenda o 4G estava em nível 3", que é exatamente a pergunta da tela; o
#     raio de precisão é exibido como círculo para o usuário julgar;
#   • paradas não são colapsadas — num ponto fixo o sinal oscila ao longo das
#     horas, e colapsar esconderia a oscilação, que é o dado;
#   • a ordem é cronológica por `coletado_em` (ascendente), para a linha do
#     tempo e o traço seguirem o evento real, não a chegada ao servidor.
MAPA_SINAL_MAX_PONTOS = 2000


def _hhmmss(dt) -> str:
    return timezone.localtime(dt).strftime("%H:%M:%S") if dt else ""


def dados_mapa_sinal(device, dia: date | None = None) -> dict:
    """Série de leituras geolocalizadas de um dia, já resolvidas para o que a
    tela de Mapa de Sinal consome (um registro por check-in).

    Cada registro traz o sinal JÁ RESOLVIDO para o transporte em uso
    (`sinal_do_checkin`) e a conectividade pelas três evidências
    (`conexao_do_checkin`) — a tela não reinterpreta nada, para nunca
    contradizer o mapa de rota nem a planilha exportada.

    Sem dia informado, usa o dia mais recente que tem leitura (e não "hoje",
    que num aparelho desligado viria vazio sem explicar por quê).
    """
    from ProjetoEstoque.models import KioskCheckin
    from django.db.models.functions import Coalesce

    base = KioskCheckin.objects.filter(device=device).annotate(
        _quando=Coalesce("coletado_em", "registrado_em")
    )

    if dia is None:
        ultimo = base.order_by("-_quando").values_list("_quando", flat=True).first()
        dia = timezone.localtime(ultimo).date() if ultimo else timezone.localdate()

    inicio, fim = intervalo_dia_local(dia)
    do_dia = base.filter(_quando__gte=inicio, _quando__lt=fim).order_by("_quando")

    total_dia = do_dia.count()
    # Só leituras com coordenada entram no mapa — não há onde desenhar as
    # outras. O total que ficou de fora é informado na tela em vez de somir:
    # desde a v1.10.0 o app manda posição nula de propósito quando o fix está
    # velho (ver INFORME_SERVIDOR_ROTA_E_SINAL_MOVEL §3), então "sem GPS" passou
    # a ser um estado legítimo e frequente, não um defeito.
    com_gps = do_dia.exclude(latitude=None).exclude(longitude=None)
    n_com_gps = com_gps.count()
    truncado = n_com_gps > MAPA_SINAL_MAX_PONTOS
    linhas = list(com_gps[:MAPA_SINAL_MAX_PONTOS]) if truncado else list(com_gps)

    atraso_fila = limiar_fila(device)
    pontos = []
    # `n` numera do mais recente para o mais antigo (o registro #1 é a última
    # leitura do dia), espelhando a ordem da tabela de histórico do detalhe.
    total = len(linhas)
    for i, c in enumerate(linhas):
        sinal = sinal_do_checkin(c)
        conx = conexao_do_checkin(c, atraso_fila)
        pontos.append({
            "id": c.pk,
            "n": total - i,
            "t": _hhmmss(c.coletado_em or c.registrado_em),
            "rt": timezone.localtime(c.registrado_em).strftime("%d/%m %H:%M:%S") if c.registrado_em else "",
            "bat": c.bateria,
            "chg": bool(c.carregando),
            "rede": c.rede or "",
            "ssid": c.ssid or None,
            "wr": c.wifi_rssi_dbm,
            "wl": c.wifi_nivel,
            "vel": c.wifi_velocidade_mbps,
            "band": c.wifi_banda_ghz or None,
            "con": sinal["rotulo"],
            "op": c.movel_operadora or None,
            "mr": c.movel_rssi_dbm,
            "ml": c.movel_nivel,
            "tec": c.movel_tecnologia or None,
            "ok": conx["online"],
            "mem": conx["fila"],
            "delay": conx["atraso_s"],
            "lat": c.latitude,
            "lon": c.longitude,
            "acc": c.precisao_m,
            "dbm": sinal["dbm"],
            "lvl": sinal["nivel"],
        })

    com_conexao = sum(1 for p in pontos if p["ok"])
    n_wifi = sum(1 for p in pontos if normalizar_rede_tipo(p["rede"]) == REDE_WIFI)
    n_movel = sum(1 for p in pontos if normalizar_rede_tipo(p["rede"]) == REDE_MOVEL)
    com_medicao = sum(1 for p in pontos if p["lvl"] is not None)

    return {
        "dia": dia,
        "pontos": pontos,
        "kpis": {
            "total": len(pontos),
            "com_conexao": com_conexao,
            "sem_conexao": len(pontos) - com_conexao,
            "pct_online": round(com_conexao / len(pontos) * 100, 1) if pontos else 0.0,
            "wifi": n_wifi,
            "movel": n_movel,
            "com_medicao": com_medicao,
        },
        "t_inicio": pontos[0]["t"] if pontos else "",
        "t_fim": pontos[-1]["t"] if pontos else "",
        "total_dia": total_dia,
        "sem_gps": total_dia - n_com_gps,
        "truncado": truncado,
        "limite": MAPA_SINAL_MAX_PONTOS,
        "atraso_fila_s": atraso_fila,
    }


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
    comandos_resumo = {
        "aguardando": comandos_status.get("pendente", 0) + comandos_status.get("entregue", 0),
        "executado": comandos_status.get("executado", 0),
        "falhou": comandos_status.get("falhou", 0) + comandos_status.get("nao_suportado", 0),
        "expirado": comandos_status.get("expirado", 0),
    }

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

        "comandos_total": comandos_total, "comandos_resumo": comandos_resumo,

        "labels_meses": labels_meses, "devices_serie": devices_serie, "matriculas_serie": matriculas_serie,
        "dias_labels": dias_labels, "checkins_por_dia": checkins_por_dia,
        "rede_labels": rede_labels, "rede_dados": rede_dados,
        "retencao_dias": RETENCAO_DIAS,

        "resumo": resumo,
        "gerado_em": agora,
    }
