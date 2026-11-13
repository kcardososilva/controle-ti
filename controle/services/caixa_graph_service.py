"""
Leitura da caixa de e-mail dos chamados pelo Microsoft Graph.

Por que Graph e não IMAP: o Exchange Online deste tenant responde ao IMAP com
`LOGINDISABLED` / "Basic authentication is disabled" — só aceita OAuth2. Graph
com credencial de APLICATIVO (client credentials) é o caminho suportado para um
serviço ler uma caixa sem usuário na frente, e é feito só com `requests`.

O que este módulo faz — e só isto:

  • `configurada()` — as quatro chaves do `.env` existem?
  • `CaixaGraph.listar(desde)` — os e-mails novos da Caixa de Entrada, do mais
    antigo para o mais novo: `{"id", "recebido_em", "conversation_id"}`;
  • `CaixaGraph.baixar(id)` — o e-mail em formato bruto (MIME / RFC 822), o MESMO
    formato que `chamado_email_entrada_service` já sabe interpretar. Pedir o MIME
    (`/$value`) em vez do JSON da mensagem mantém um interpretador só: o que se
    testa com um `.eml` é o que roda em produção.

Ele NÃO decide nada sobre o e-mail, NÃO marca como lido e NÃO apaga: a caixa é
de uma pessoa também (as respostas chegam à mesma caixa que envia os alertas), e
mexer nela só poderia esconder e-mail de quem o está lendo. A deduplicação é do
banco (`provider_id`/`message_id`).

Permissão necessária no Entra ID: `Mail.Read` (tipo APLICATIVO) — restrita à caixa
dos chamados por uma política de acesso de aplicativo do Exchange; sem a restrição
o aplicativo leria QUALQUER caixa do tenant. Passo a passo em
docs/TUTORIAL_CHAMADOS_CONVERSA.md.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone as dt_timezone
from urllib.parse import quote

import requests
from django.conf import settings
from django.utils.dateparse import parse_datetime

logger = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com"
TIMEOUT = (10, 60)        # (conexão, leitura) em segundos
POR_PAGINA = 50
MAX_PAGINAS = 40          # trava contra laço: 2000 e-mails por execução

_token_em_cache: dict = {"valor": "", "expira": 0.0}


class CaixaIndisponivel(Exception):
    """A caixa não pôde ser lida (credencial, rede, limite do provedor)."""


def _chaves() -> tuple[str, str, str, str]:
    return (
        settings.CHAMADOS_GRAPH_TENANT_ID, settings.CHAMADOS_GRAPH_CLIENT_ID,
        settings.CHAMADOS_GRAPH_CLIENT_SECRET, settings.CHAMADOS_CAIXA,
    )


def configurada() -> bool:
    """As quatro chaves existem. Não prova que funcionam — só o primeiro acesso
    prova —, mas sem elas a sincronização nem tenta, e a tela diz isso."""
    return all(c.strip() for c in _chaves())


def limpar_token() -> None:
    _token_em_cache.update(valor="", expira=0.0)


class CaixaGraph:
    def __init__(self, sessao=None):
        # `sessao` existe para o teste injetar uma sessão falsa: nenhuma chamada
        # de rede acontece fora de produção.
        self._http = sessao or requests.Session()

    # ── autenticação ─────────────────────────────────────────
    def _token(self) -> str:
        if _token_em_cache["valor"] and time.time() < _token_em_cache["expira"] - 60:
            return _token_em_cache["valor"]
        tenant, cliente, segredo, _ = _chaves()
        try:
            r = self._http.post(
                f"{LOGIN}/{quote(tenant)}/oauth2/v2.0/token",
                data={
                    "client_id": cliente, "client_secret": segredo,
                    "scope": "https://graph.microsoft.com/.default",
                    "grant_type": "client_credentials",
                },
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            raise CaixaIndisponivel(f"Sem conexão com o provedor de e-mail: {exc.__class__.__name__}") from exc
        if r.status_code != 200:
            raise CaixaIndisponivel(f"O provedor recusou a credencial do aplicativo (HTTP {r.status_code}).")
        dados = r.json()
        _token_em_cache.update(
            valor=dados["access_token"], expira=time.time() + int(dados.get("expires_in", 3000)),
        )
        return _token_em_cache["valor"]

    def _get(self, url: str, **kwargs):
        try:
            r = self._http.get(
                url, headers={"Authorization": f"Bearer {self._token()}"}, timeout=TIMEOUT, **kwargs,
            )
        except requests.RequestException as exc:
            raise CaixaIndisponivel(f"Sem conexão com o provedor de e-mail: {exc.__class__.__name__}") from exc
        if r.status_code == 401:
            limpar_token()  # token expirou/revogado: a próxima execução pede outro
            raise CaixaIndisponivel("O provedor recusou o acesso à caixa (HTTP 401).")
        if r.status_code == 403:
            raise CaixaIndisponivel(
                "O aplicativo não tem permissão para ler esta caixa (HTTP 403) — confira "
                "`Mail.Read` e a política de acesso do Exchange."
            )
        if r.status_code == 429:
            raise CaixaIndisponivel("O provedor limitou as consultas (HTTP 429). Tenta de novo na próxima execução.")
        if r.status_code >= 400:
            raise CaixaIndisponivel(f"O provedor respondeu HTTP {r.status_code}.")
        return r

    # ── leitura ──────────────────────────────────────────────
    def listar(self, desde: datetime):
        """E-mails da Caixa de Entrada recebidos a partir de `desde`, do mais
        antigo ao mais novo, paginando até o fim."""
        desde = desde.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _, _, _, caixa = _chaves()
        url = f"{GRAPH}/users/{quote(caixa)}/mailFolders/inbox/messages"
        params = {
            "$filter": f"receivedDateTime ge {desde}",
            "$orderby": "receivedDateTime asc",
            "$select": "id,receivedDateTime,conversationId",
            "$top": POR_PAGINA,
        }
        for _ in range(MAX_PAGINAS):
            dados = self._get(url, params=params).json()
            for item in dados.get("value", []):
                yield {
                    "id": item["id"],
                    "recebido_em": parse_datetime(item.get("receivedDateTime", "")),
                    "conversation_id": item.get("conversationId", "") or "",
                }
            url = dados.get("@odata.nextLink")
            if not url:
                return
            params = None  # o nextLink já carrega a consulta inteira

    def baixar(self, provider_id: str) -> bytes:
        """O e-mail em MIME (RFC 822), exatamente como chegou."""
        _, _, _, caixa = _chaves()
        r = self._get(f"{GRAPH}/users/{quote(caixa)}/messages/{quote(provider_id, safe='')}/$value")
        return r.content
