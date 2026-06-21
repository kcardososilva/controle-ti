"""
ninja_service.py — Importação de dispositivos NinjaOne via planilha CSV.

A integração via API/OAuth foi removida. Agora os dados vêm de uma planilha
CSV exportada do NinjaOne e importada pelo usuário nas telas do módulo Ninja.

Função principal:
    importar_csv(arquivo, user=None) -> dict   (estatísticas da importação)

Relações inteligentes resolvidas na importação:
    • Dispositivo ↔ Item do estoque  → por número de série (BIOS) e, em fallback,
      pelo nome do dispositivo. Nunca DESvincula um item já vinculado se não houver
      novo match (preserva ligações manuais).
    • Dispositivo ↔ Local / Site      → campo `local` (Karitel, Pinheiros, ...).
    • Dispositivo ↔ Usuário logado    → extrai o login (sem o domínio DOMÍNIO\\user).
    • A cada importação grava um Snapshot (alimenta o Relatório de Uso).
"""

import csv
import io
import logging
import re
import unicodedata
from datetime import datetime

logger = logging.getLogger(__name__)

_INVALID_SERIALS = frozenset({
    "", "to be filled by o.e.m.", "not specified", "default string",
    "none", "n/a", "na", "0", "00000000", "system serial number",
    "to be filled by oem", "chassis serial number",
})


# ─────────────────────────────────────────────────────────────
# Helpers de parsing
# ─────────────────────────────────────────────────────────────

def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )


def _norm_header(h: str) -> str:
    """Normaliza um cabeçalho: minúsculo, sem acento, espaços colapsados."""
    return " ".join(_strip_accents((h or "").strip().lower()).split())


def _clean_serial(serial: str) -> str:
    s = (serial or "").strip()
    return "" if s.lower() in _INVALID_SERIALS else s


def _clean_user(raw: str) -> str:
    """Extrai o login do formato 'DOMINIO\\usuario' ou 'usuario (ttyS0)'."""
    u = (raw or "").strip()
    if not u or u.lower() in ("null", "none", "undefined"):
        return ""
    if "\\" in u:
        u = u.split("\\")[-1]
    # remove sufixos do tipo " (ttyS0)" / " (pts/0 / ...)"
    if " (" in u:
        u = u.split(" (")[0]
    return u.strip()


def _parse_bool_online(value: str) -> bool:
    return (value or "").strip().lower() in ("true", "1", "online", "sim", "ativo")


def _parse_timestamp(value):
    if not value:
        return None
    v = str(value).strip()
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                    "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S"):
            try:
                return datetime.strptime(v, fmt)
            except ValueError:
                continue
    return None


# ─────────────────────────────────────────────────────────────
# Mapeamento de colunas (robusto a acento / encoding)
# ─────────────────────────────────────────────────────────────

def _mapear_colunas(fieldnames) -> dict:
    """Retorna {campo_logico: nome_original_da_coluna} a partir do cabeçalho."""
    mapa = {}
    for col in fieldnames or []:
        n = _norm_header(col)
        if "modelo" in n:
            mapa.setdefault("model", col)
        elif "serie" in n and "bios" in n:
            mapa.setdefault("serial", col)
        elif n == "dispositivo":
            mapa.setdefault("display", col)
        elif "organiza" in n:
            mapa.setdefault("org", col)
        elif n == "local":
            mapa.setdefault("local", col)
        elif "ultimo tempo de atividade" in n and "format" not in n:
            mapa.setdefault("last_contact", col)
        elif "ultimo login" in n:
            mapa.setdefault("last_user", col)
        elif "status" in n and "atividade" in n:
            mapa.setdefault("status", col)
    return mapa


def _decode(arquivo) -> str:
    """Lê o conteúdo do upload tentando utf-8-sig e, em fallback, cp1252."""
    raw = arquivo.read()
    if isinstance(raw, str):
        return raw
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


# ─────────────────────────────────────────────────────────────
# Importação
# ─────────────────────────────────────────────────────────────

def importar_csv(arquivo, user=None) -> dict:
    """
    Importa a planilha CSV do NinjaOne para NinjaDevice.
    Retorna estatísticas: {ok, total, criados, atualizados, vinculados,
                           sem_serie, nao_vinculados, locais, erro}.
    """
    from django.utils import timezone
    from ProjetoEstoque.models import NinjaDevice, Item

    texto = _decode(arquivo)
    # detecta delimitador (vírgula ou ponto-e-vírgula)
    amostra = texto[:4096]
    delim = ";" if amostra.count(";") > amostra.count(",") else ","
    reader = csv.DictReader(io.StringIO(texto), delimiter=delim)

    cols = _mapear_colunas(reader.fieldnames)
    if "display" not in cols:
        return {"ok": False, "erro": "Coluna 'Dispositivo' não encontrada no CSV. "
                                     "Confirme que é a exportação de dispositivos do NinjaOne."}

    def val(row, key):
        c = cols.get(key)
        return (row.get(c) or "").strip() if c else ""

    criados = atualizados = vinculados = sem_serie = total = 0
    locais = {}
    agora = timezone.now()

    # 1) Lê e parseia todas as linhas da planilha.
    parsed = []
    for row in reader:
        display = val(row, "display")
        if not display:
            continue
        parsed.append({
            "display": display,
            "serial": _clean_serial(val(row, "serial")),
            "last_user": _clean_user(val(row, "last_user")),
            "is_online": _parse_bool_online(val(row, "status")),
            "last_contact": _parse_timestamp(val(row, "last_contact")),
            "local": val(row, "local"),
            "model": val(row, "model"),
            "org": val(row, "org"),
        })

    # 2) Ordena para que o dispositivo MAIS ATIVO reivindique o Item primeiro
    #    (online e contato mais recente vêm antes). Como Item↔Dispositivo é 1-para-1,
    #    quando a planilha tem a mesma série em 2 dispositivos, o ativo fica vinculado
    #    e o duplicado fica sem vínculo — sem quebrar a importação.
    from datetime import datetime as _dt, timezone as _tzmod
    _MIN_TS = _dt.min.replace(tzinfo=_tzmod.utc)
    parsed.sort(key=lambda r: (r["is_online"], r["last_contact"] or _MIN_TS), reverse=True)

    itens_usados = set()  # item_id já reivindicado nesta importação

    for r in parsed:
        display = r["display"]
        serial = r["serial"]
        total += 1
        if not serial:
            sem_serie += 1
        if r["local"]:
            locais[r["local"]] = locais.get(r["local"], 0) + 1

        obj = NinjaDevice.objects.filter(display_name=display).first()

        # ── Relação inteligente Dispositivo ↔ Item (à prova de conflito 1-para-1) ──
        candidate = None
        if serial:
            candidate = Item.objects.filter(numero_serie__iexact=serial).first()
        if candidate is None:
            candidate = Item.objects.filter(nome__iexact=display).first()
        if candidate is not None:
            ja_usado = candidate.pk in itens_usados
            if not ja_usado:
                conflito = NinjaDevice.objects.filter(item=candidate)
                if obj is not None:
                    conflito = conflito.exclude(pk=obj.pk)
                ja_usado = conflito.exists()
            if ja_usado:
                candidate = None  # série/nome já pertence a outro dispositivo

        defaults = {
            "serial_number": serial,
            "hostname": display,
            "last_user": r["last_user"],
            "is_online": r["is_online"],
            "last_contact": r["last_contact"],
            "local": r["local"],
            "model_name": r["model"],
            "organization_name": r["org"],
        }

        try:
            if obj:
                for k, v in defaults.items():
                    setattr(obj, k, v)
                if candidate is not None:
                    obj.item = candidate
                if hasattr(obj, "atualizado_por"):
                    obj.atualizado_por = user
                obj.save()
                atualizados += 1
                if obj.item_id:
                    vinculados += 1
                    itens_usados.add(obj.item_id)
            else:
                novo = NinjaDevice(display_name=display, item=candidate, **defaults)
                if hasattr(novo, "criado_por"):
                    novo.criado_por = user
                    novo.atualizado_por = user
                novo.save()
                criados += 1
                if novo.item_id:
                    vinculados += 1
                    itens_usados.add(novo.item_id)
        except Exception as exc:  # noqa: BLE001 — uma linha problemática não derruba a importação
            logger.error("importar_csv: falha ao gravar '%s': %s", display, exc)
            total -= 1
            continue

    _take_snapshot(agora)

    # Valida os logins (último usuário do device × colaborador atribuído) e
    # registra no histórico quando há mudança. Nunca derruba a importação.
    validacao = {}
    try:
        validacao = registrar_validacao(user=user)
    except Exception as exc:  # noqa: BLE001
        logger.error("importar_csv: falha ao validar logins: %s", exc)

    return {
        "ok": True,
        "erro": None,
        "total": total,
        "criados": criados,
        "atualizados": atualizados,
        "vinculados": vinculados,
        "nao_vinculados": total - vinculados,
        "sem_serie": sem_serie,
        "locais": locais,
        "validacao": validacao,
    }


# ─────────────────────────────────────────────────────────────
# Snapshot + status (alimentam dashboard e relatório)
# ─────────────────────────────────────────────────────────────

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
    if snapshots:
        NinjaDeviceSnapshot.objects.bulk_create(snapshots)
    return len(snapshots)


def get_live_status() -> dict:
    from ProjetoEstoque.models import NinjaDevice
    qs = NinjaDevice.objects.select_related("item")
    total = qs.count()
    online = qs.filter(is_online=True).count()
    matched = qs.filter(item__isnull=False).count()
    com_user = qs.filter(is_online=True).exclude(last_user="").count()
    return {
        "total": total,
        "online": online,
        "offline": total - online,
        "matched": matched,
        "com_user": com_user,
        "pct_online": round(online / total * 100) if total else 0,
        "pct_matched": round(matched / total * 100) if total else 0,
    }


# ═════════════════════════════════════════════════════════════
# VALIDAÇÃO DE LOGIN
# Compara o último usuário ativo do dispositivo (last_user) com o colaborador
# atribuído ao item no sistema (última transferência de 'entrega' não devolvida).
# ═════════════════════════════════════════════════════════════

def _login_base(login: str) -> str:
    """Normaliza o login: minúsculo, sem domínio e sem sufixos."""
    u = (login or "").strip().lower()
    if "\\" in u:
        u = u.split("\\")[-1]
    if " (" in u:
        u = u.split(" (")[0]
    return u.strip()


def _login_tokens(login: str) -> set:
    base = _strip_accents(_login_base(login))
    return {p for p in re.split(r"[._\-\s]+", base) if len(p) >= 2}


def _nome_tokens(nome: str) -> set:
    return {t for t in _strip_accents((nome or "").lower()).split() if len(t) >= 2}


def _resolver_usuarios_sistema(item_ids):
    """
    Para cada item, retorna o colaborador atualmente atribuído — a partir da
    última transferência ('entrega' = atribuído; 'devolucao' = liberado).
    {item_id: Usuario | None}
    """
    from ProjetoEstoque.models import MovimentacaoItem

    estado = {}  # item_id -> (tipo_transferencia, usuario)
    movs = (
        MovimentacaoItem.objects
        .filter(item_id__in=list(item_ids),
                tipo_movimentacao="transferencia",
                tipo_transferencia__in=["entrega", "devolucao"])
        .select_related("usuario")
        .order_by("item_id", "created_at", "id")
    )
    for m in movs:
        estado[m.item_id] = (m.tipo_transferencia, m.usuario)
    return {iid: (u if tipo == "entrega" else None) for iid, (tipo, u) in estado.items()}


def _indexar_usuarios():
    """Lista [(usuario, nome_tokens, email_local)] para casar logins."""
    from ProjetoEstoque.models import Usuario
    index = []
    for u in Usuario.objects.all().only("id", "nome", "email"):
        email_local = ""
        if u.email and "@" in u.email:
            email_local = u.email.split("@")[0].strip().lower()
        index.append((u, _nome_tokens(u.nome), email_local))
    return index


def _login_casa_usuario(login: str, usuario) -> bool:
    """True se o login do dispositivo corresponde ao colaborador informado."""
    if usuario is None:
        return False
    base = _login_base(login)
    if not base:
        return False
    if usuario.email and "@" in usuario.email:
        local = usuario.email.split("@")[0].strip().lower()
        if local and local.replace(".", "") == base.replace(".", ""):
            return True
    lt = _login_tokens(login)
    return bool(lt) and lt <= _nome_tokens(usuario.nome)


def _detectar_usuario(login: str, index):
    """Tenta descobrir, entre os colaboradores, quem é o login do dispositivo."""
    base = _login_base(login)
    if not base:
        return None
    base_nodot = base.replace(".", "")
    lt = _login_tokens(login)
    melhor, melhor_score = None, 0
    for usuario, tokens, email_local in index:
        score = 0
        if email_local and email_local.replace(".", "") == base_nodot:
            score = 100
        elif lt and lt <= tokens:
            score = 50 + len(lt)
        if score > melhor_score:
            melhor, melhor_score = usuario, score
    return melhor if melhor_score >= 50 else None


def _classificar_device(device, usuario_sistema, index):
    """
    Retorna (status, detalhe, usuario_detectado) comparando o login do device
    com o colaborador atribuído ao item.
    """
    from ProjetoEstoque.models import NinjaLoginRegistro as R

    login = (device.last_user or "").strip()
    detectado = _detectar_usuario(login, index) if login else None

    if not login:
        return R.STATUS_SEM_LOGIN, "Dispositivo sem usuário logado capturado.", None

    if usuario_sistema is None:
        if detectado:
            det = f"Login '{login}' (≈ {detectado.nome}); item não atribuído a colaborador no sistema."
        else:
            det = f"Login '{login}'; item não atribuído a nenhum colaborador no sistema."
        return R.STATUS_SEM_ATRIBUICAO, det, detectado

    if _login_casa_usuario(login, usuario_sistema):
        return R.STATUS_CONFERE, f"Login '{login}' confere com {usuario_sistema.nome}.", usuario_sistema

    if detectado:
        det = f"Em uso por '{login}' (≈ {detectado.nome}), mas o item está atribuído a {usuario_sistema.nome}."
    else:
        det = f"Login '{login}' diverge do colaborador atribuído ({usuario_sistema.nome})."
    return R.STATUS_DIVERGENTE, det, detectado


def avaliar_logins():
    """
    Avalia (sem gravar) o login de todos os dispositivos vinculados a um item.
    Retorna lista de dicts ordenada por gravidade (divergente primeiro).
    """
    from ProjetoEstoque.models import NinjaDevice

    devices = list(
        NinjaDevice.objects.filter(item__isnull=False)
        .select_related("item")
        .order_by("display_name")
    )
    atribuidos = _resolver_usuarios_sistema([d.item_id for d in devices])
    index = _indexar_usuarios()

    ordem = {"divergente": 0, "sem_atribuicao": 1, "sem_login": 2, "confere": 3}
    resultados = []
    for d in devices:
        usuario = atribuidos.get(d.item_id)
        status, detalhe, detectado = _classificar_device(d, usuario, index)
        resultados.append({
            "device": d,
            "login": (d.last_user or "").strip(),
            "usuario": usuario,
            "usuario_nome": usuario.nome if usuario else "",
            "detectado": detectado,
            "detectado_nome": detectado.nome if detectado else "",
            "status": status,
            "detalhe": detalhe,
        })
    resultados.sort(key=lambda r: (ordem.get(r["status"], 9), r["device"].display_name.lower()))
    return resultados


def avaliar_login_device(device):
    """Avalia um único dispositivo (para a tela de detalhe)."""
    index = _indexar_usuarios()
    usuario = None
    if device.item_id:
        usuario = _resolver_usuarios_sistema([device.item_id]).get(device.item_id)
    status, detalhe, detectado = _classificar_device(device, usuario, index)
    return {
        "device": device,
        "login": (device.last_user or "").strip(),
        "usuario": usuario,
        "usuario_nome": usuario.nome if usuario else "",
        "detectado": detectado,
        "detectado_nome": detectado.nome if detectado else "",
        "status": status,
        "detalhe": detalhe,
    }


def registrar_validacao(user=None, resultados=None) -> dict:
    """
    Grava um NinjaLoginRegistro por dispositivo SEMPRE QUE o status/login/colaborador
    muda em relação ao último registro (evita duplicar linhas idênticas no histórico).
    Retorna estatísticas dos status atuais + nº de novos registros.
    """
    from ProjetoEstoque.models import NinjaLoginRegistro

    if resultados is None:
        resultados = avaliar_logins()

    stats = {"total": 0, "confere": 0, "divergente": 0,
             "sem_atribuicao": 0, "sem_login": 0, "novos_registros": 0}

    for r in resultados:
        stats["total"] += 1
        stats[r["status"]] = stats.get(r["status"], 0) + 1
        device = r["device"]
        usuario_id = r["usuario"].id if r["usuario"] else None

        ultimo = (
            NinjaLoginRegistro.objects
            .filter(device=device).order_by("-verificado_em", "-id").first()
        )
        mudou = (
            ultimo is None
            or ultimo.status != r["status"]
            or (ultimo.device_user or "") != (r["login"] or "")
            or (ultimo.usuario_sistema_id or None) != usuario_id
        )
        if mudou:
            NinjaLoginRegistro.objects.create(
                device=device,
                device_user=r["login"],
                usuario_sistema=r["usuario"],
                usuario_sistema_nome=r["usuario_nome"],
                usuario_detectado=r["detectado_nome"],
                item_nome=(device.item.nome if device.item_id else ""),
                status=r["status"],
                detalhe=r["detalhe"][:400],
            )
            stats["novos_registros"] += 1

    return stats
