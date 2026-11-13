"""
ninja_api_service.py — Conexão OAuth com a API do NinjaOne e sincronização de dispositivos.

O app do NinjaOne é do tipo "Web" (Authorization Code): alguém com acesso de
administrador autoriza UMA vez, no navegador, e o sistema passa a renovar o token
sozinho. Fluxo:

    iniciar_autorizacao(session)  -> URL do NinjaOne para onde o navegador vai
    validar_state(session, state) -> o retorno é do pedido que NÓS iniciamos?
    concluir_autorizacao(code, u) -> troca o código por tokens e guarda (cifrado)
    sincronizar(user)             -> busca organizações, locais e dispositivos e grava
                                     pelo MESMO núcleo da planilha CSV
                                     (`ninja_service.gravar_dispositivos`)

Decisões que não devem ser "simplificadas" (Architecture Rule 59):

  • TOKEN CIFRADO NO BANCO (Fernet, `NINJA_CHAVE_CIFRA` do .env) — mesmo padrão do
    módulo TIM. A chave é conferida ANTES de usar o código de autorização: o código
    é de uso único, e gastá-lo para depois não conseguir guardar o token obrigaria a
    refazer a autorização.
  • `redirect_uri` SEMPRE da configuração, nunca derivado da requisição: o NinjaOne só
    aceita a URI exatamente como cadastrada, e derivar do `Host` deixaria um cabeçalho
    forjado escolher para onde o código é devolvido.
  • `state` aleatório, de uso único e com validade, guardado na sessão (comparação em
    tempo constante). Sem ele, qualquer link com `?code=` abriria a conexão do sistema
    com a conta de OUTRA pessoa.
  • As chamadas que levam segredo (`/ws/oauth/token`) NÃO seguem redirecionamento: o
    corpo do POST (com o client_secret) seria reenviado ao destino.
  • NENHUMA mensagem de erro repete o corpo da resposta do NinjaOne: o erro de refresh
    token inválido devolve o próprio token enviado dentro do texto. Só o `error` curto
    é lido, e só para escolher a mensagem em português.
  • Sincronização é uma fonte PARCIAL: o que a API não informa não apaga o que a
    planilha trouxe (ver `preservar_vazios` em `gravar_dispositivos`).
"""

import hmac
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone as _tz
from urllib.parse import quote, urlencode, urlparse

import requests
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from services.ninja_service import (
    _clean_serial,
    _clean_user,
    _parse_timestamp,
    gravar_dispositivos,
)

logger = logging.getLogger(__name__)
logger_seg = logging.getLogger("seguranca")

SESSAO_STATE = "ninja_oauth_state"
STATE_VALIDADE_S = 600          # o usuário tem 10 min para autorizar no NinjaOne
MARGEM_RENOVACAO_S = 120        # renova o access token quando faltar menos que isto
TRAVA_SYNC_MIN = 15             # trava de sincronização esquecida (crash) expira sozinha
TIMEOUT_S = (5, 45)             # (conexão, leitura)
PAGINA = 500
MAX_PAGINAS = 100               # teto de laço: 100 x 500 = 50 mil registros

COMANDO_GERAR_CHAVE = (
    'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
)


class NinjaErro(Exception):
    """Mensagem JÁ PRONTA para o usuário (pt-BR) e sem segredo. `status` é o HTTP, se houve."""

    def __init__(self, mensagem, status=None):
        super().__init__(mensagem)
        self.status = status


class NinjaTokenInvalido(NinjaErro):
    """O NinjaOne recusou o código/refresh token (venceu, foi usado ou revogado)."""


# ─────────────────────────────────────────────────────────────
# Configuração
# ─────────────────────────────────────────────────────────────

def _cfg(nome: str) -> str:
    return (getattr(settings, nome, "") or "").strip()


def base_url() -> str:
    """Raiz do NinjaOne. Só https: é para lá que vão o client_secret e os tokens."""
    base = _cfg("NINJA_BASE_URL").rstrip("/")
    p = urlparse(base)
    if p.scheme != "https" or not p.netloc or p.path or p.query or p.fragment:
        raise NinjaErro(
            "NINJA_BASE_URL (no .env) precisa ser https://<servidor>, sem caminho — "
            "por exemplo https://app.ninjarmm.com."
        )
    return base


def host_de_retorno() -> str:
    """Host:porta da URI de retorno cadastrada (onde o navegador cai depois de autorizar)."""
    return urlparse(_cfg("NINJA_REDIRECT_URI")).netloc


def _fernet() -> Fernet:
    chave = _cfg("NINJA_CHAVE_CIFRA")
    if not chave:
        raise NinjaErro(
            "Não dá para guardar o token no banco: falta a chave de cifra NINJA_CHAVE_CIFRA no .env. "
            f"Gere uma com: {COMANDO_GERAR_CHAVE} — coloque no .env e reinicie o servidor."
        )
    try:
        return Fernet(chave.encode())
    except (ValueError, TypeError):
        raise NinjaErro(
            "A chave NINJA_CHAVE_CIFRA do .env não é uma chave Fernet válida. "
            f"Gere uma nova com: {COMANDO_GERAR_CHAVE}"
        ) from None


def motivo_nao_configurada() -> str:
    """"" se está tudo no .env; senão, o que falta (para a tela dizer, em vez de falhar calada)."""
    faltam = [n for n in ("NINJA_BASE_URL", "NINJA_CLIENT_ID", "NINJA_CLIENT_SECRET", "NINJA_REDIRECT_URI")
              if not _cfg(n)]
    if faltam:
        return "Falta no .env: " + ", ".join(faltam) + "."
    try:
        base_url()
        _fernet()
    except NinjaErro as exc:
        return str(exc)
    return ""


def _cifrar(valor: str) -> str:
    return _fernet().encrypt(valor.encode()).decode()


def _decifrar(cifrado: str) -> str:
    try:
        return _fernet().decrypt(cifrado.encode()).decode()
    except InvalidToken:
        raise NinjaErro(
            "O token salvo não pôde ser lido: a NINJA_CHAVE_CIFRA do .env mudou desde a conexão. "
            "Conecte ao NinjaOne de novo."
        ) from None


# ─────────────────────────────────────────────────────────────
# Autorização (o "conectar")
# ─────────────────────────────────────────────────────────────

def iniciar_autorizacao(session) -> str:
    """Gera o `state` (guardado na sessão) e devolve a URL de autorização do NinjaOne."""
    motivo = motivo_nao_configurada()
    if motivo:
        raise NinjaErro(motivo)
    state = secrets.token_urlsafe(32)
    session[SESSAO_STATE] = {"state": state, "em": time.time()}
    params = {
        "response_type": "code",
        "client_id": _cfg("NINJA_CLIENT_ID"),
        "redirect_uri": _cfg("NINJA_REDIRECT_URI"),
        "scope": _cfg("NINJA_SCOPES") or "monitoring",
        "state": state,
    }
    # quote (e não quote_plus): o escopo vai como "monitoring%20offline_access".
    return f"{base_url()}/ws/oauth/authorize?" + urlencode(params, quote_via=quote)


def validar_state(session, recebido) -> bool:
    """Uso ÚNICO: o state é consumido mesmo quando não confere, para não ser retentado."""
    guardado = session.pop(SESSAO_STATE, None)
    if not isinstance(guardado, dict) or not recebido:
        return False
    if time.time() - float(guardado.get("em") or 0) > STATE_VALIDADE_S:
        return False
    return hmac.compare_digest(str(guardado.get("state", "")), str(recebido))


def _json_ou_vazio(resposta) -> dict:
    try:
        dado = resposta.json()
    except ValueError:
        return {}
    return dado if isinstance(dado, dict) else {}


def _pedir_token(corpo: dict) -> dict:
    """POST em /ws/oauth/token. Levanta NinjaErro/NinjaTokenInvalido com mensagem segura."""
    dados = {**corpo, "client_id": _cfg("NINJA_CLIENT_ID"), "client_secret": _cfg("NINJA_CLIENT_SECRET")}
    try:
        r = requests.post(
            f"{base_url()}/ws/oauth/token", data=dados, timeout=TIMEOUT_S,
            headers={"Accept": "application/json"}, allow_redirects=False,
        )
    except requests.RequestException:
        # `from None`: a cadeia de exceção não vai parar num log com URL/corpo.
        raise NinjaErro("Não consegui falar com o NinjaOne (rede ou DNS). Tente de novo em instantes.") from None

    resp = _json_ou_vazio(r)
    if r.status_code != 200:
        codigo = str(resp.get("error") or "")[:40]
        texto = str(resp.get("errorMessage") or "").lower()
        if codigo in ("invalid_grant", "invalid_token") or "uuid" in texto or "refresh token" in texto:
            raise NinjaTokenInvalido("O NinjaOne recusou o código/token: venceu, já foi usado ou foi revogado.",
                                     r.status_code)
        if codigo == "invalid_client":
            raise NinjaErro(
                "O NinjaOne não reconhece o Client ID/secret do .env. Confira NINJA_CLIENT_ID e "
                "NINJA_CLIENT_SECRET (o secret pode ter sido regenerado).", r.status_code)
        if codigo == "unauthorized_client":
            raise NinjaErro(
                "O app do NinjaOne não permite este tipo de autorização. Em Administração > Apps > API, "
                "o app precisa ser do tipo Web com 'Authorization code' e 'Refresh token' marcados.",
                r.status_code)
        if codigo == "invalid_scope":
            raise NinjaErro(
                "O app do NinjaOne não tem o escopo pedido liberado. Libere o escopo no app ou "
                "defina NINJA_SCOPES=monitoring no .env.", r.status_code)
        raise NinjaErro(f"O NinjaOne recusou o pedido de token (HTTP {r.status_code}).", r.status_code)

    if not resp.get("access_token"):
        raise NinjaErro("O NinjaOne respondeu sem access token.", r.status_code)
    return resp


def _guardar_tokens(conexao, resp: dict) -> None:
    """Cifra e grava. `update_fields` explícito: um save() completo de uma instância velha
    zeraria a trava de sincronização que outro processo acabou de tomar."""
    conexao.access_cifrado = _cifrar(resp["access_token"])
    if resp.get("refresh_token"):
        conexao.refresh_cifrado = _cifrar(resp["refresh_token"])
    try:
        validade = int(resp.get("expires_in") or 3600)
    except (TypeError, ValueError):
        validade = 3600
    conexao.expira_em = timezone.now() + timedelta(seconds=validade)
    conexao.escopo = str(resp.get("scope") or conexao.escopo or "")[:255]
    conexao.ultimo_erro = ""
    conexao.ultimo_erro_em = None
    conexao.save(update_fields=[
        "access_cifrado", "refresh_cifrado", "expira_em", "escopo",
        "ultimo_erro", "ultimo_erro_em", "atualizado_em",
    ])


def concluir_autorizacao(code: str, user) -> dict:
    """Troca o código por tokens e guarda. Retorna {"refresh_token": bool, "escopo": str}."""
    from ProjetoEstoque.models import NinjaConexao

    code = (code or "").strip()
    if not code:
        raise NinjaErro("O NinjaOne não devolveu o código de autorização.")
    _fernet()  # antes de gastar o código (uso único): sem chave não há onde guardar o token
    try:
        resp = _pedir_token({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _cfg("NINJA_REDIRECT_URI"),
        })
    except NinjaTokenInvalido:
        raise NinjaErro("O código de autorização venceu ou já foi usado. Clique em Conectar de novo.") from None

    conexao = NinjaConexao.get()
    conexao.refresh_cifrado = ""          # conexão nova não herda refresh token da anterior
    conexao.conectado_por = user if getattr(user, "pk", None) else None
    conexao.conectado_em = timezone.now()
    conexao.save(update_fields=["refresh_cifrado", "conectado_por", "conectado_em", "atualizado_em"])
    _guardar_tokens(conexao, resp)
    logger_seg.info("NinjaOne: conexão autorizada por %s (refresh token: %s).",
                    getattr(user, "username", "?"), "sim" if resp.get("refresh_token") else "NÃO")
    return {"refresh_token": bool(resp.get("refresh_token")), "escopo": conexao.escopo}


def desconectar(user=None) -> None:
    """Esquece os tokens daqui. NÃO revoga no NinjaOne — para isso, apagar o app/token lá."""
    from ProjetoEstoque.models import NinjaConexao

    NinjaConexao.objects.filter(pk=1).update(
        access_cifrado="", refresh_cifrado="", expira_em=None, escopo="",
        conectado_por=None, conectado_em=None, sincronizando_desde=None,
    )
    logger_seg.info("NinjaOne: conexão removida por %s.", getattr(user, "username", "?"))


# ─────────────────────────────────────────────────────────────
# Token em uso (com renovação)
# ─────────────────────────────────────────────────────────────

def _ainda_vale(conexao) -> bool:
    return bool(conexao.access_cifrado and conexao.expira_em
                and conexao.expira_em - timezone.now() > timedelta(seconds=MARGEM_RENOVACAO_S))


def _renovar(conexao) -> str:
    from ProjetoEstoque.models import NinjaConexao

    refresh = _decifrar(conexao.refresh_cifrado) if conexao.refresh_cifrado else ""
    if not refresh:
        raise NinjaErro(
            "A conexão com o NinjaOne expirou e não há refresh token para renová-la. Conecte de novo — e, "
            "para a conexão durar, o app precisa do escopo offline_access (NINJA_SCOPES)."
        )
    try:
        resp = _pedir_token({"grant_type": "refresh_token", "refresh_token": refresh})
    except NinjaTokenInvalido:
        # O NinjaOne pode girar o refresh token: se outro processo (a tarefa agendada, outra aba)
        # renovou primeiro, o nosso já está queimado e o dele já está no banco. Reler antes de desistir.
        atual = NinjaConexao.objects.get(pk=conexao.pk)
        if atual.refresh_cifrado != conexao.refresh_cifrado and _ainda_vale(atual):
            return _decifrar(atual.access_cifrado)
        NinjaConexao.objects.filter(pk=conexao.pk).update(
            access_cifrado="", refresh_cifrado="", expira_em=None,
            ultimo_erro="O NinjaOne recusou a renovação da conexão (revogada ou expirada). Conecte de novo.",
            ultimo_erro_em=timezone.now(),
        )
        logger_seg.warning("NinjaOne: renovação recusada; a conexão foi encerrada e precisa ser refeita.")
        raise NinjaErro("O NinjaOne recusou a renovação da conexão (revogada ou expirada). "
                        "Conecte de novo.") from None
    _guardar_tokens(conexao, resp)
    return resp["access_token"]


def _access_token() -> str:
    from ProjetoEstoque.models import NinjaConexao

    conexao = NinjaConexao.get()
    if not conexao.conectada:
        raise NinjaErro("O sistema ainda não está conectado ao NinjaOne. Use 'Conectar ao NinjaOne'.")
    if _ainda_vale(conexao):
        return _decifrar(conexao.access_cifrado)
    return _renovar(conexao)


class _Cliente:
    """GET autenticado. Em 401 renova UMA vez e repete (o token pode ter sido revogado)."""

    def __init__(self):
        self._token = _access_token()

    def get(self, caminho: str, params=None):
        from ProjetoEstoque.models import NinjaConexao

        for tentativa in (1, 2):
            try:
                r = requests.get(
                    f"{base_url()}{caminho}", params=params, timeout=TIMEOUT_S, allow_redirects=False,
                    headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
                )
            except requests.RequestException:
                raise NinjaErro("Não consegui falar com o NinjaOne (rede ou DNS). Tente de novo em instantes.") from None
            if r.status_code == 401 and tentativa == 1:
                self._token = _renovar(NinjaConexao.get())
                continue
            break

        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                raise NinjaErro(f"Resposta inesperada do NinjaOne em {caminho} (não é JSON).", 200) from None
        if r.status_code in (401, 403):
            raise NinjaErro(
                f"O NinjaOne negou acesso a {caminho} (HTTP {r.status_code}). Confira os escopos do app "
                "(monitoring) e, se preciso, conecte de novo.", r.status_code)
        if r.status_code == 404:
            raise NinjaErro(f"O NinjaOne não tem o recurso {caminho} (HTTP 404).", 404)
        if r.status_code == 429:
            raise NinjaErro("O NinjaOne pediu para diminuir o ritmo (limite de requisições). "
                            "Tente de novo em alguns minutos.", 429)
        raise NinjaErro(f"O NinjaOne respondeu HTTP {r.status_code} em {caminho}.", r.status_code)


def _listar_por_id(cliente, caminho: str, params=None) -> list:
    """Lista paginada pelo id do último item (`after`). Para quando uma página não traz nada novo —
    se o NinjaOne ignorasse `after`, devolveria sempre a mesma página e o laço nunca acabaria."""
    vistos, itens, after = set(), [], None
    for _ in range(MAX_PAGINAS):
        p = {**(params or {}), "pageSize": PAGINA}
        if after is not None:
            p["after"] = after
        pagina = cliente.get(caminho, p)
        if isinstance(pagina, dict):
            pagina = pagina.get("results") or []
        novos = [x for x in pagina if isinstance(x, dict) and x.get("id") not in vistos]
        if not novos:
            break
        for x in novos:
            vistos.add(x.get("id"))
            itens.append(x)
        ids = [x["id"] for x in novos if isinstance(x.get("id"), int)]
        if len(pagina) < PAGINA or not ids:
            break
        after = max(ids)
    return itens


def _listar_por_cursor(cliente, caminho: str, params=None) -> list:
    """Consultas `/v2/queries/*`: devolvem {cursor:{name}, results:[...]}."""
    itens, cursor = [], None
    for _ in range(MAX_PAGINAS):
        p = {**(params or {}), "pageSize": PAGINA}
        if cursor:
            p["cursor"] = cursor
        resp = cliente.get(caminho, p)
        if not isinstance(resp, dict):
            break
        resultados = [x for x in (resp.get("results") or []) if isinstance(x, dict)]
        itens.extend(resultados)
        cursor = (resp.get("cursor") or {}).get("name") if isinstance(resp.get("cursor"), dict) else None
        if not cursor or not resultados:
            break
    return itens


# ─────────────────────────────────────────────────────────────
# Mapeamento API -> formato do núcleo de gravação
# ─────────────────────────────────────────────────────────────

def _t(valor, limite: int) -> str:
    return str(valor or "").strip()[:limite]


def _para_datetime(valor):
    """O NinjaOne manda instante como segundos desde 1970 (às vezes com fração); aceita ms e texto."""
    if valor in (None, ""):
        return None
    if isinstance(valor, (int, float)) and not isinstance(valor, bool):
        seg = valor / 1000 if valor > 1e11 else valor
        try:
            return datetime.fromtimestamp(seg, tz=_tz.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return _parse_timestamp(valor)


def _mapear_dispositivo(d: dict, orgs: dict, locais: dict, usuarios: dict):
    """Um dispositivo da API -> dict de `gravar_dispositivos`. None = não entra (sem id/nome,
    ou ainda pendente de aprovação no NinjaOne). `.get` em tudo: o formato não é presumido."""
    nid = d.get("id")
    if not isinstance(nid, int) or isinstance(nid, bool):
        return None
    if d.get("approvalStatus") not in (None, "APPROVED"):
        return None
    display = _t(d.get("displayName") or d.get("systemName") or d.get("dnsName"), 255)
    if not display:
        return None

    sistema = d.get("system") if isinstance(d.get("system"), dict) else {}
    so = d.get("os") if isinstance(d.get("os"), dict) else {}
    procs = d.get("processors") if isinstance(d.get("processors"), list) else []
    primeiro_proc = procs[0] if procs and isinstance(procs[0], dict) else {}
    ips = d.get("ipAddresses") if isinstance(d.get("ipAddresses"), list) else []
    capacidade = (d.get("memory") or {}).get("capacity") if isinstance(d.get("memory"), dict) else None
    mb = round(capacidade / 1048576) if isinstance(capacidade, (int, float)) and capacidade > 0 else None

    r = {
        "display": display,
        "serial": _t(_clean_serial(sistema.get("biosSerialNumber") or sistema.get("serialNumber") or ""), 100),
        # Sem o campo `offline` não se sabe: erra para o lado de NÃO afirmar que está online.
        "is_online": d.get("offline") is False,
        "last_contact": _para_datetime(d.get("lastContact")),
        "local": _t(locais.get(d.get("locationId")), 120),
        "model": _t(sistema.get("model"), 255),
        "org": _t(orgs.get(d.get("organizationId")), 255),
        "ninja_id": nid,
        "hostname": _t(d.get("dnsName") or d.get("systemName"), 255) or display,
        "os_name": _t(so.get("name"), 255),
        "manufacturer": _t(sistema.get("manufacturer"), 255),
        "processor": _t(primeiro_proc.get("name"), 255),
        "total_memory_mb": mb,
        "ip_address": _t(ips[0] if ips else "", 50),
        "node_class": _t(d.get("nodeClass"), 60),
        "preservar_vazios": True,
    }
    usuario = usuarios.get(nid)
    if usuario:
        r["last_user"] = _t(usuario, 255)
    return r


def _usuarios_logados(cliente):
    """{id_do_dispositivo: login}. MELHOR ESFORÇO: se a consulta falhar, a sincronização segue sem
    atualizar o usuário (o que já está gravado é preservado) e devolve o aviso."""
    try:
        linhas = _listar_por_cursor(cliente, "/v2/queries/logged-on-users")
    except NinjaErro as exc:
        return {}, f"Usuário logado não atualizado: {exc}"
    mapa = {}
    for linha in linhas:
        did, login = linha.get("deviceId"), _clean_user(linha.get("userName") or "")
        if isinstance(did, int) and login and did not in mapa:
            mapa[did] = login
    return mapa, ""


# ─────────────────────────────────────────────────────────────
# Sincronização
# ─────────────────────────────────────────────────────────────

def _tomar_trava() -> bool:
    """UPDATE condicional = atômico no banco, vale entre processos (botão x tarefa agendada)."""
    from ProjetoEstoque.models import NinjaConexao

    NinjaConexao.get()
    limite = timezone.now() - timedelta(minutes=TRAVA_SYNC_MIN)
    return NinjaConexao.objects.filter(pk=1).filter(
        Q(sincronizando_desde__isnull=True) | Q(sincronizando_desde__lt=limite)
    ).update(sincronizando_desde=timezone.now()) == 1


def _soltar_trava() -> None:
    from ProjetoEstoque.models import NinjaConexao

    NinjaConexao.objects.filter(pk=1).update(sincronizando_desde=None)


def _registrar_erro(mensagem: str) -> None:
    from ProjetoEstoque.models import NinjaConexao

    NinjaConexao.objects.filter(pk=1).update(ultimo_erro=mensagem[:1000], ultimo_erro_em=timezone.now())


def _exigir_conectada():
    from ProjetoEstoque.models import NinjaConexao

    motivo = motivo_nao_configurada()
    if motivo:
        raise NinjaErro(motivo)
    if not NinjaConexao.get().conectada:
        raise NinjaErro("O sistema ainda não está conectado ao NinjaOne. Use 'Conectar ao NinjaOne'.")


def sincronizar(user=None) -> dict:
    """Busca tudo na API e grava. Retorna as estatísticas de `gravar_dispositivos` + `avisos`.
    Levanta NinjaErro (mensagem pronta); nada é gravado se a API não devolver dispositivo algum."""
    from ProjetoEstoque.models import NinjaConexao

    _exigir_conectada()
    if not _tomar_trava():
        raise NinjaErro("Já existe uma sincronização com o NinjaOne em andamento. Aguarde terminar.")
    try:
        cliente = _Cliente()
        orgs = {o.get("id"): o.get("name") for o in _listar_por_id(cliente, "/v2/organizations")}
        locais = {x.get("id"): x.get("name") for x in _listar_por_id(cliente, "/v2/locations")}
        brutos = _listar_por_id(cliente, "/v2/devices-detailed")
        usuarios, aviso_usuarios = _usuarios_logados(cliente)

        parsed = [m for m in (_mapear_dispositivo(d, orgs, locais, usuarios) for d in brutos) if m]
        if not parsed:
            if brutos:
                raise NinjaErro(
                    f"O NinjaOne devolveu {len(brutos)} registro(s), mas nenhum pôde ser interpretado como "
                    "dispositivo (formato inesperado ou todos pendentes de aprovação). Nada foi alterado.")
            raise NinjaErro("O NinjaOne não devolveu nenhum dispositivo. Nada foi alterado.")

        resultado = gravar_dispositivos(parsed, user=user)
        resultado["avisos"] = [aviso_usuarios] if aviso_usuarios else []
        resultado["lidos_na_api"] = len(brutos)
        NinjaConexao.objects.filter(pk=1).update(
            ultima_sincronizacao_em=timezone.now(), ultimo_erro="", ultimo_erro_em=None)
        logger.info("NinjaOne: sincronização ok — %s dispositivo(s), %s novo(s), %s atualizado(s).",
                    resultado.get("total"), resultado.get("criados"), resultado.get("atualizados"))
        return resultado
    except NinjaErro as exc:
        _registrar_erro(str(exc))
        raise
    except Exception as exc:  # noqa: BLE001 — qualquer outra falha vira registro + mensagem segura
        logger.exception("NinjaOne: falha inesperada na sincronização")
        _registrar_erro("Falha inesperada na sincronização. Veja logs/erros.log.")
        raise NinjaErro("Falha inesperada na sincronização com o NinjaOne. O detalhe está em "
                        "logs/erros.log.") from exc
    finally:
        _soltar_trava()


def diagnosticar() -> dict:
    """Mostra o FORMATO do que a API devolve — só NOMES de campo e contagens, nunca valores
    (há login e IP de gente real ali). Não grava nada. Serve para validar o mapeamento."""
    _exigir_conectada()
    cliente = _Cliente()

    def chaves(obj):
        return sorted(obj.keys()) if isinstance(obj, dict) else []

    orgs = _listar_por_id(cliente, "/v2/organizations")
    locais = _listar_por_id(cliente, "/v2/locations")
    devs = _listar_por_id(cliente, "/v2/devices-detailed")
    amostra = devs[0] if devs else {}
    try:
        logados = _listar_por_cursor(cliente, "/v2/queries/logged-on-users")
        erro_logados = ""
    except NinjaErro as exc:
        logados, erro_logados = [], str(exc)
    return {
        "organizacoes": len(orgs), "campos_organizacao": chaves(orgs[0]) if orgs else [],
        "locais": len(locais), "campos_local": chaves(locais[0]) if locais else [],
        "dispositivos": len(devs), "campos_dispositivo": chaves(amostra),
        "campos_system": chaves(amostra.get("system")), "campos_os": chaves(amostra.get("os")),
        "campos_memory": chaves(amostra.get("memory")),
        "tem_processors": bool(amostra.get("processors")),
        "mapeados": sum(1 for d in devs if _mapear_dispositivo(d, {}, {}, {})),
        "logados": len(logados), "campos_logado": chaves(logados[0]) if logados else [],
        "erro_logados": erro_logados,
    }


# ─────────────────────────────────────────────────────────────
# Estado (para a tela)
# ─────────────────────────────────────────────────────────────

def estado() -> dict:
    """O que a tela precisa para dizer em que pé está a conexão. Só lê (GET não grava)."""
    from ProjetoEstoque.models import NinjaConexao

    motivo = motivo_nao_configurada()
    c = NinjaConexao.objects.select_related("conectado_por").filter(pk=1).first()
    agora = timezone.now()
    conectada = bool(c and c.conectada)
    tem_refresh = bool(c and c.refresh_cifrado)
    quem = ""
    if c and c.conectado_por_id:
        quem = c.conectado_por.get_full_name() or c.conectado_por.get_username()
    return {
        "configurada": not motivo,
        "motivo": motivo,
        "conectada": conectada,
        "tem_refresh": tem_refresh,
        # sem refresh e com o access vencido: a conexão já morreu, só não foi dito
        "expirada": bool(conectada and not tem_refresh and c.expira_em and c.expira_em <= agora),
        "conectado_em": c.conectado_em if c else None,
        "conectado_por": quem,
        "expira_em": c.expira_em if c else None,
        "escopo": c.escopo if c else "",
        "ultima_sincronizacao_em": c.ultima_sincronizacao_em if c else None,
        "ultimo_erro": c.ultimo_erro if c else "",
        "ultimo_erro_em": c.ultimo_erro_em if c else None,
        "sincronizando": bool(c and c.sincronizando_desde
                              and c.sincronizando_desde > agora - timedelta(minutes=TRAVA_SYNC_MIN)),
        "host_retorno": host_de_retorno(),
    }
