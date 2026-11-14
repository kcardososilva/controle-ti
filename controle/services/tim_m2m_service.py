"""
Transporte HTTP da API do GSIM (Saitro, a plataforma de SIM Cards da TIM) — só o que
fala com a rede e com a credencial.

A API (documentação oficial, com login, em https://tim.saitro.com/api/docs/):

  • CREDENCIAL. OAuth2 `client_credentials`: `POST {base}/oauth/token` com
    `grant_type`, `client_id` e `client_secret` no CORPO devolve um `access_token`
    Bearer que vale 7200 s. (Quem já tem um Bearer pronto pode usar `TIM_M2M_TOKEN`.)
  • CONSULTA DO CHIP. `GET {base}/simcards/{MSISDN | ICCID | IMSI}/` devolve UMA lista
    com um registro (a doc mostra um objeto; a resposta real é lista). O MESMO
    registro traz a conexão (`conexao`, `ultimo_acesso`…) e o consumo de dados
    (`servicos.dados.consumo`, texto como "0,00 KB") — não existem `/session` nem
    `/usage`: "sessão" e "consumo" saem do mesmo GET.
  • LISTA. `GET {base}/simcards/` devolve TODOS os SIM Cards da conta de uma vez (205
    numa página só, ao contrário do que a doc diz sobre 10/20 por página). O ciclo
    agendado usa isto: UMA chamada atualiza todos os chips, em vez de uma por chip —
    a doc fala em limite de requisições por token.
  • A API só atende IPs liberados pela Saitro (403 `acesso_negado_ip_nao_autorizado`).

Este módulo faz isso e NADA além: não interpreta a resposta (`m2m_interpretacao`),
não decide o que é falha de chip (`m2m_monitoramento_service`).

O que ele garante:

  • HOST TRAVADO. O endereço vem só de `settings.TIM_M2M_BASE_URL` (.env); nenhuma
    tela aceita URL. Só https, e a cada chamada o esquema e o host da URL montada
    são conferidos de novo (inclusive o `proxima` da paginação).
    `allow_redirects=False`: um redirect levaria a credencial para outro host —
    resposta 3xx vira `resposta_invalida`, não é seguida.
  • A CREDENCIAL NUNCA SAI DAQUI. Senha, token de acesso e `Bearer ...` não vão para
    log, nem para mensagem de erro, nem para o payload guardado (`sanear`/
    `preparar_payload`). Os headers da requisição nunca são registrados.
  • TOKEN ESTÁTICO NO BANCO É CIFRADO (Fernet, chave `TIM_M2M_CHAVE_CIFRA` do .env).
    Usuário e senha da API ficam só no .env (como PRTG e e-mail).
  • CLASSIFICAÇÃO HONESTA. 404 é `nao_encontrado` (nunca "sem sessão"); falha de rede
    ou de API nunca é "chip inativo". Retentativa só para timeout, erro de rede e
    5xx (nunca para 401, 404 ou 429). Token de acesso recusado (401) é renovado UMA
    vez antes de se declarar a credencial recusada.

`ClienteTIM(sessao=None)` recebe a sessão HTTP por injeção: o teste passa uma falsa e
nenhuma chamada de rede acontece fora de produção (mesmo desenho de `caixa_graph_service`).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, replace
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urljoin, urlsplit

import requests
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

TIMEOUT = (5, 20)                       # (conexão, leitura) em segundos
ESPERAS_RETENTATIVA = (1.0, 3.0)        # espera antes da 1a e da 2a retentativa
RETENTATIVAS_AGENDADA = 2
RETENTATIVAS_MANUAL = 1
BLOQUEIO_PADRAO_S = 15 * 60             # 429 sem Retry-After: pausa de 15 min
BLOQUEIO_MAXIMO_S = 24 * 3600
PAYLOAD_MAX_BYTES = 64 * 1024

TIPOS = ("sessao", "consumo")
# Sessão e consumo saem do MESMO recurso: o registro do SIM Card.
RECURSO = "simcards"
CAMINHO_DO_TIPO = {"sessao": RECURSO, "consumo": RECURSO}
MAX_PAGINAS_LOTE = 60                   # trava contra laço de paginação
MEMO_SEGUNDOS = 10                      # sessão + consumo do mesmo chip = 1 chamada só
MAX_PAGINAS_DIA = 5                      # páginas de eventos de UM dia (trava contra laço de paginação)
MARGEM_TOKEN_S = 300                    # renova o token de acesso 5 min antes de vencer
VALIDADE_TOKEN_PADRAO_S = 7200          # `expires_in` da doc, se a resposta não trouxer
RESULTADOS_RETENTAVEIS = ("timeout", "erro_rede", "indisponivel")
RESULTADOS_QUE_INTERROMPEM = ("falha_autenticacao", "limite_excedido")

COMANDO_GERAR_CHAVE = (
    'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
)


class TimM2MErro(Exception):
    """Erro de configuração/uso do transporte (não confundir com uma resposta
    ruim da API, que vira `RespostaTIM` com `resultado` de falha)."""


class ConfiguracaoInvalida(TimM2MErro):
    """`TIM_M2M_BASE_URL` ausente ou fora do que é aceito (https, com host)."""


class SemToken(TimM2MErro):
    """Não há token configurado (nem no .env, nem salvo no banco)."""


# ─────────────────────────────────────────────────────────────
# Identificadores: ICCID e IMSI
# ─────────────────────────────────────────────────────────────

def _so_digitos(valor, rotulo: str) -> str:
    texto = re.sub(r"[\s.\-]", "", str(valor or ""))
    if not texto:
        raise ValidationError(f"Informe o {rotulo}.")
    if not texto.isdigit():
        raise ValidationError(f"O {rotulo} deve conter apenas dígitos.")
    return texto


def calcular_digito_luhn(corpo: str) -> str:
    """Dígito verificador Luhn que fecha `corpo` (usado para montar ICCIDs de teste)."""
    soma = 0
    for posicao, digito in enumerate(reversed(corpo)):
        n = int(digito)
        if posicao % 2 == 0:
            n *= 2
            if n > 9:
                n -= 9
        soma += n
    return str((10 - soma % 10) % 10)


def luhn_valido(digitos: str) -> bool:
    return bool(digitos) and digitos.isdigit() and calcular_digito_luhn(digitos[:-1]) == digitos[-1]


def normalizar_iccid(valor) -> str:
    """ICCID só com dígitos: 19 ou 20, começando por 89 (prefixo de telecom do
    padrão ITU-T E.118) e com o último dígito conferindo no Luhn. Aceita espaços,
    pontos e hífens na entrada. Levanta `ValidationError` em pt-BR."""
    iccid = _so_digitos(valor, "ICCID")
    if len(iccid) not in (19, 20):
        raise ValidationError(f"O ICCID deve ter 19 ou 20 dígitos (o informado tem {len(iccid)}).")
    if not iccid.startswith("89"):
        raise ValidationError("O ICCID deve começar com 89.")
    if not luhn_valido(iccid):
        raise ValidationError("O ICCID é inválido: o dígito verificador não confere. Confira se digitou todos os números.")
    return iccid


def normalizar_imsi(valor) -> str:
    """IMSI: exatamente 15 dígitos. Levanta `ValidationError` em pt-BR."""
    imsi = _so_digitos(valor, "IMSI")
    if len(imsi) != 15:
        raise ValidationError(f"O IMSI deve ter 15 dígitos (o informado tem {len(imsi)}).")
    return imsi


def normalizar_numero_linha(valor) -> str:
    """Número da linha do chip, só dígitos (com DDD): de 10 a 15. Aceita espaços,
    parênteses, hífen, ponto e `+` na entrada ("+55 (77) 99999-1234"); qualquer letra
    é recusada. Vazio devolve `""` (o número da linha é opcional e NÃO é único).
    Levanta `ValidationError` em pt-BR."""
    bruto = str(valor if valor is not None else "").strip()
    if not bruto:
        return ""
    if re.search(r"[^\d\s().+\-]", bruto):
        raise ValidationError("O número da linha deve ter só dígitos (pode usar espaços, parênteses e hífen).")
    digitos = re.sub(r"\D", "", bruto)
    if not 10 <= len(digitos) <= 15:
        raise ValidationError(
            f"O número da linha deve ter de 10 a 15 dígitos, com DDD (o informado tem {len(digitos)})."
        )
    return digitos


def identificador_do_chip(chip) -> str:
    """O identificador que vai na URL, já normalizado (ICCID ou IMSI, conforme o
    cadastro). `ValidationError` se o que está gravado não é válido."""
    if chip.identificador_consulta == "imsi":
        return normalizar_imsi(chip.imsi)
    return normalizar_iccid(chip.iccid)


# ─────────────────────────────────────────────────────────────
# Saneamento: o token não pode vazar
# ─────────────────────────────────────────────────────────────

_BEARER = re.compile(r"(?i)\bbearer\s+\S+")


def _segredos(token) -> list[str]:
    """`token` pode ser um texto ou vários (senha, token de acesso…): só os que são
    longos o bastante para serem segredo de verdade entram na lista."""
    bruto = [token] if isinstance(token, str) else list(token or [])
    return [s for s in bruto if isinstance(s, str) and len(s) >= 6]


def sanear(texto, token="") -> str:
    """Remove o(s) segredo(s) informado(s) e qualquer `Bearer <algo>` do texto. Toda
    mensagem de erro que vai para log, banco ou tela passa por aqui."""
    saida = str(texto or "")
    for segredo in _segredos(token):
        saida = saida.replace(segredo, "[token]")
    return _BEARER.sub("Bearer [oculto]", saida)


def preparar_payload(valor, token="", *, limitar: bool = True):
    """O JSON a guardar em `M2MConsulta.payload`: o próprio valor, com o(s)
    segredo(s) redigido(s) caso a API o(s) ecoe, e limitado a 64 KB. Acima disso
    devolve um envelope `{"_truncado": true, "_tamanho_original_bytes": N, "texto":
    "<início do JSON>"}`. `limitar=False` (só a lista completa de SIM Cards, que fica
    em memória e nunca é gravada inteira) devolve o valor inteiro, já redigido."""
    if valor is None:
        return None
    try:
        texto = json.dumps(valor, allow_nan=False)
    except (TypeError, ValueError):
        return {"_nao_serializavel": True}
    segredos = [s for s in _segredos(token) if s in texto]
    if segredos:
        for segredo in segredos:
            texto = texto.replace(segredo, "[token]")
        valor = json.loads(texto)
    if not limitar:
        return valor
    tamanho = len(texto.encode("utf-8"))
    if tamanho <= PAYLOAD_MAX_BYTES:
        return valor
    corte = PAYLOAD_MAX_BYTES // 2
    while True:
        envelope = {
            "_truncado": True, "_tamanho_original_bytes": tamanho,
            "_aviso": "Resposta maior que 64 KB: só o início foi guardado.", "texto": texto[:corte],
        }
        if corte <= 500 or len(json.dumps(envelope).encode("utf-8")) <= PAYLOAD_MAX_BYTES:
            return envelope
        corte = int(corte * 0.8)


# ─────────────────────────────────────────────────────────────
# Configuração: URL e token
# ─────────────────────────────────────────────────────────────

def base_url() -> str:
    """A raiz da API, validada: https, com host, sem usuário/senha/consulta.
    `ConfiguracaoInvalida` caso contrário."""
    bruto = (getattr(settings, "TIM_M2M_BASE_URL", "") or "").strip().rstrip("/")
    partes = urlsplit(bruto)
    if (
        partes.scheme != "https" or not partes.hostname
        or partes.username or partes.password or partes.query or partes.fragment
    ):
        raise ConfiguracaoInvalida(
            "TIM_M2M_BASE_URL (no .env) precisa ser uma URL https com o endereço do servidor, "
            "por exemplo https://servidor.exemplo."
        )
    return bruto


def _token_env() -> str:
    return (getattr(settings, "TIM_M2M_TOKEN", "") or "").strip()


def _client_id() -> str:
    return (getattr(settings, "TIM_M2M_CLIENT_ID", "") or "").strip()


def _client_secret() -> str:
    return (getattr(settings, "TIM_M2M_CLIENT_SECRET", "") or "").strip()


def modo_credencial() -> str:
    """`"oauth2"` (usuário + senha da API no .env, trocados por um token de acesso),
    `"token"` (um Bearer pronto: .env ou banco) ou `"nenhum"`. O usuário e a senha
    vencem o token estático — é o modo que a API documenta."""
    if _client_id() and _client_secret():
        return "oauth2"
    from ProjetoEstoque.models import M2MIntegracao

    if _token_env() or M2MIntegracao.get().token_cifrado:
        return "token"
    return "nenhum"


def _fernet() -> Fernet:
    chave = (getattr(settings, "TIM_M2M_CHAVE_CIFRA", "") or "").strip()
    if not chave:
        raise ValidationError(
            "Não dá para guardar o token no banco: falta a chave de cifra TIM_M2M_CHAVE_CIFRA no .env. "
            f"Gere uma com: {COMANDO_GERAR_CHAVE} — coloque o resultado no .env e reinicie o servidor."
        )
    try:
        return Fernet(chave.encode())
    except (ValueError, TypeError):
        raise ValidationError(
            "A chave TIM_M2M_CHAVE_CIFRA do .env não é uma chave Fernet válida. "
            f"Gere uma nova com: {COMANDO_GERAR_CHAVE}"
        ) from None


def chave_cifra_configurada() -> bool:
    try:
        _fernet()
    except ValidationError:
        return False
    return True


def _decifrar(cifrado: str) -> str:
    """O token em claro, ou "" se não dá para decifrar (chave ausente/trocada).
    Sem log: quem precisa explicar o motivo é `estado_token`."""
    try:
        return _fernet().decrypt(cifrado.encode()).decode()
    except (ValidationError, InvalidToken, ValueError, TypeError):
        return ""


def token() -> str:
    """O token em uso: o do .env, se houver; senão o salvo no banco (decifrado).
    "" quando não há nenhum utilizável. NUNCA registrar o retorno."""
    do_env = _token_env()
    if do_env:
        return do_env
    from ProjetoEstoque.models import M2MIntegracao

    cifrado = M2MIntegracao.get().token_cifrado
    return _decifrar(cifrado) if cifrado else ""


def _sufixo(valor: str) -> str:
    # Só mostra o final quando o token é longo o bastante para o sufixo não ser o token inteiro.
    return valor[-4:] if len(valor) >= 12 else ""


def estado_token() -> dict:
    """Diagnóstico do token, sem expô-lo:
    `{"origem": "env"|"banco"|"nenhum", "sufixo": "abcd", "legivel": bool,
      "token_env_definido": bool, "token_no_banco": bool, "chave_cifra_configurada": bool}`.
    `legivel` é False quando há token no banco mas ele não decifra (chave trocada ou ausente).
    Com usuário e senha da API no .env, `modo` é "oauth2" e `origem` é "env" (sem sufixo:
    não se mostra pedaço de senha)."""
    from ProjetoEstoque.models import M2MIntegracao

    cfg = M2MIntegracao.get()
    do_env = _token_env()
    no_banco = bool(cfg.token_cifrado)
    base = {
        "token_env_definido": bool(do_env), "token_no_banco": no_banco,
        "chave_cifra_configurada": chave_cifra_configurada(),
        "modo": modo_credencial(), "usuario_api_definido": bool(_client_id() and _client_secret()),
    }
    if base["modo"] == "oauth2":
        return {**base, "origem": "env", "sufixo": "", "legivel": True}
    if do_env:
        return {**base, "origem": "env", "sufixo": _sufixo(do_env), "legivel": True}
    if no_banco:
        return {**base, "origem": "banco", "sufixo": cfg.token_sufixo, "legivel": bool(_decifrar(cfg.token_cifrado))}
    return {**base, "origem": "nenhum", "sufixo": "", "legivel": False}


def configurada() -> bool:
    """Há token utilizável e a URL é aceita. Não prova que a API responde — só o
    primeiro acesso prova —, mas sem isso nem se tenta."""
    return not motivo_nao_configurada()


def motivo_nao_configurada() -> str:
    """"" se está configurada; senão o motivo, em pt-BR. Só caracteres do cp1252 (a
    mensagem vai para o console do Windows pelo comando `monitorar_m2m`)."""
    try:
        base_url()
    except ConfiguracaoInvalida as exc:
        return str(exc)
    estado = estado_token()
    if estado["origem"] == "nenhum":
        if _client_id() or _client_secret():
            return (
                "Falta metade da credencial da API: defina TIM_M2M_CLIENT_ID (usuario) e "
                "TIM_M2M_CLIENT_SECRET (senha) no .env e reinicie o servidor."
            )
        return (
            "Nenhuma credencial configurada. Defina TIM_M2M_CLIENT_ID e TIM_M2M_CLIENT_SECRET (usuario e "
            "senha da API) no .env, ou informe um token na tela de Configuração."
        )
    if not estado["legivel"]:
        return (
            "O token salvo no banco não pode ser lido: a chave TIM_M2M_CHAVE_CIFRA do .env está ausente ou "
            "mudou desde que o token foi salvo. Restaure a chave ou grave o token de novo."
        )
    return ""


def salvar_token(valor, usuario=None) -> None:
    """Cifra e guarda o token. Recusa (`ValidationError`) se falta a chave de
    cifra — com o comando para gerá-la —, se o token é vazio ou tem espaços.
    Zera `falha_autenticacao_em`: credencial nova merece tentativa nova. Grava um
    `M2MEvento` SEM o token."""
    from ProjetoEstoque.models import M2MEvento, M2MIntegracao

    texto = (valor or "").strip() if isinstance(valor, str) else ""
    if not texto:
        raise ValidationError("Informe o token.")
    if re.search(r"\s", texto) or len(texto) > 4096:
        raise ValidationError("Cole só o token, sem a palavra Bearer, sem espaços e sem quebras de linha.")
    cifrado = _fernet().encrypt(texto.encode()).decode()
    agora = timezone.now()
    M2MIntegracao.get()
    with transaction.atomic():
        M2MIntegracao.objects.filter(pk=1).update(
            token_cifrado=cifrado, token_sufixo=_sufixo(texto),
            token_atualizado_em=agora, token_atualizado_por=usuario if getattr(usuario, "pk", None) else None,
            falha_autenticacao_em=None,
        )
        M2MEvento.objects.create(
            tipo=M2MEvento.Tipo.TOKEN_ALTERADO, usuario=usuario if getattr(usuario, "pk", None) else None,
            dados={"acao": "salvo", "sufixo": _sufixo(texto), "token_do_env_vence": bool(_token_env())},
        )


def remover_token(usuario=None) -> None:
    """Apaga o token salvo no banco (o do .env, se houver, continua valendo)."""
    from ProjetoEstoque.models import M2MEvento, M2MIntegracao

    M2MIntegracao.get()
    with transaction.atomic():
        M2MIntegracao.objects.filter(pk=1).update(
            token_cifrado="", token_sufixo="", token_atualizado_em=timezone.now(),
            token_atualizado_por=usuario if getattr(usuario, "pk", None) else None,
        )
        M2MEvento.objects.create(
            tipo=M2MEvento.Tipo.TOKEN_ALTERADO, usuario=usuario if getattr(usuario, "pk", None) else None,
            dados={"acao": "removido"},
        )


# ─────────────────────────────────────────────────────────────
# Cliente HTTP
# ─────────────────────────────────────────────────────────────

@dataclass
class RespostaTIM:
    """O que uma consulta devolveu, já classificado.

    `resultado`: sucesso | sucesso_sem_corpo | falha_autenticacao | nao_encontrado |
    limite_excedido | timeout | erro_rede | indisponivel | resposta_invalida.
    `payload`: JSON da resposta, já sem o token e limitado a 64 KB (None se não houve corpo JSON).
    `retry_after_s`: só no 429 — quanto esperar (Retry-After, ou 15 min).
    `tentativas`: chamadas HTTP feitas (1 a 3)."""
    resultado: str
    http_status: int | None = None
    payload: object = None
    duracao_ms: int = 0
    tentativas: int = 1
    mensagem_erro: str = ""
    retry_after_s: int | None = None
    identificador_tipo: str = ""

    @property
    def valida(self) -> bool:
        return self.resultado in ("sucesso", "sucesso_sem_corpo")

    @property
    def interrompe_ciclo(self) -> bool:
        """401/403 e 429: continuar só martelaria a credencial recusada ou o limite."""
        return self.resultado in RESULTADOS_QUE_INTERROMPEM


_MARCAS_DNS = (
    "getaddrinfo failed", "name or service not known", "nameresolutionerror",
    "failed to resolve", "nodename nor servname", "temporary failure in name resolution",
)


def _retry_after_segundos(cabecalhos) -> int:
    valor = ""
    try:
        for chave, v in (cabecalhos or {}).items():
            if str(chave).lower() == "retry-after":
                valor = str(v).strip()
                break
    except AttributeError:
        valor = ""
    segundos = BLOQUEIO_PADRAO_S
    if valor.isdigit():
        segundos = int(valor)
    elif valor:
        try:
            alvo = parsedate_to_datetime(valor)
            segundos = int((alvo - timezone.now()).total_seconds())
        except (TypeError, ValueError):
            segundos = BLOQUEIO_PADRAO_S
    return max(1, min(segundos, BLOQUEIO_MAXIMO_S))


_ACESSO: dict = {"valor": "", "expira": 0.0, "usuario": ""}   # token de acesso OAuth2, só em memória
_TRAVA_ACESSO = threading.Lock()


def limpar_token_de_acesso() -> None:
    """Esquece o token de acesso em memória (o próximo uso pede um novo)."""
    with _TRAVA_ACESSO:
        _ACESSO.update(valor="", expira=0.0, usuario="")


class ClienteTIM:
    def __init__(self, sessao=None):
        # `sessao` existe para o teste injetar uma sessão falsa: nenhuma
        # chamada de rede acontece fora de produção.
        self._http = sessao or requests.Session()
        self._token_estatico: str | None = None
        self._memo: dict[str, tuple[float, RespostaTIM]] = {}
        # A lista completa de SIM Cards (ver `carregar_lote`).
        self._lote: dict | None = None
        self._lote_resposta: RespostaTIM | None = None     # a falha, se a lista não pôde ser lida
        self._lote_completo = False
        self._lote_total = 0
        # Ritmo mínimo entre duas leituras de eventos (s): quem chama define (`pausa_efetiva_s`); o cliente
        # espera o que faltar. A leitura da lista completa não conta aqui — ela é uma chamada só.
        self.ritmo_eventos_s = 0.0
        self._ultimo_evento = 0.0
        # Quantas requisições HTTP este cliente fez (o token também conta): o ciclo grava o total em
        # `M2MExecucao.chamadas`, e é com ele que se confere, de verdade, o limite de requisições por minuto.
        self.chamadas = 0

    # ── URL ──────────────────────────────────────────────────
    @staticmethod
    def _conferir_host(url: str, base: str) -> None:
        alvo, origem = urlsplit(url), urlsplit(base)
        if alvo.scheme != "https" or alvo.hostname != origem.hostname or alvo.port != origem.port:
            raise ConfiguracaoInvalida("A URL montada não aponta para o host configurado; a consulta foi bloqueada.")

    @staticmethod
    def url_da_consulta(identificador: str, tipo: str) -> str:
        """`{base}/simcards/{identificador}/` (o MSISDN, o ICCID ou o IMSI, codificado) e a
        conferência final de esquema e host. Sessão e consumo usam a MESMA URL.
        `ConfiguracaoInvalida` se algo não bate."""
        if tipo not in CAMINHO_DO_TIPO:
            raise TimM2MErro(f"Tipo de consulta desconhecido: {tipo!r}.")
        base = base_url()
        url = f"{base}/{CAMINHO_DO_TIPO[tipo]}/{quote(str(identificador), safe='')}/"
        ClienteTIM._conferir_host(url, base)
        return url

    @staticmethod
    def url_dos_eventos(identificador: str, dia) -> str:
        """`{base}/simcards/{id}/consumo/dados/{AAAA-MM-DD}/` — as sessões de dados que começaram naquele dia
        (a mesma lista da aba Dados do portal). Aceita ICCID, IMSI e o número da linha. Mesma conferência
        de esquema e host das outras URLs."""
        base = base_url()
        url = f"{base}/{RECURSO}/{quote(str(identificador), safe='')}/consumo/dados/{dia:%Y-%m-%d}/"
        ClienteTIM._conferir_host(url, base)
        return url

    @staticmethod
    def url_da_lista() -> str:
        base = base_url()
        url = f"{base}/{RECURSO}/"
        ClienteTIM._conferir_host(url, base)
        return url

    @staticmethod
    def _url_da_pagina(proxima) -> str | None:
        """O `meta.proxima` da paginação (`/api/simcards/?pagina=2`) como URL absoluta NO
        HOST CONFIGURADO. `None` quando não há próxima; `ConfiguracaoInvalida` se ela
        apontar para outro host (a credencial não vai junto)."""
        if not proxima or not isinstance(proxima, str):
            return None
        base = base_url()
        partes = urlsplit(base)
        url = urljoin(f"{partes.scheme}://{partes.netloc}/", proxima)
        ClienteTIM._conferir_host(url, base)
        return url

    # ── credencial ───────────────────────────────────────────
    def _segredos_em_uso(self) -> list[str]:
        return [s for s in (_client_secret(), _ACESSO["valor"], self._token_estatico or "") if s]

    def _exigir_credencial(self) -> None:
        if modo_credencial() == "nenhum":
            raise SemToken(motivo_nao_configurada() or "Nenhuma credencial configurada.")

    def _credencial(self, forcar: bool = False) -> tuple[str, RespostaTIM | None]:
        """O Bearer a enviar — ou, se o token de acesso não pôde ser obtido, a falha já
        classificada. Levanta `SemToken` quando não há credencial nenhuma."""
        if modo_credencial() == "oauth2":
            return self._token_de_acesso(forcar)
        if self._token_estatico is None:
            self._token_estatico = token()
        if not self._token_estatico:
            raise SemToken(motivo_nao_configurada() or "Nenhuma credencial configurada.")
        return self._token_estatico, None

    def _token_de_acesso(self, forcar: bool) -> tuple[str, RespostaTIM | None]:
        """OAuth2 `client_credentials`: `POST {base}/oauth/token` com usuário e senha no
        CORPO (nunca na URL). O token vale ~2 h e fica em memória, renovado 5 min antes."""
        usuario = _client_id()
        with _TRAVA_ACESSO:
            if (
                not forcar and _ACESSO["valor"] and _ACESSO["usuario"] == usuario
                and time.monotonic() < _ACESSO["expira"]
            ):
                return _ACESSO["valor"], None
            base = base_url()
            url = f"{base}/oauth/token"
            self._conferir_host(url, base)
            enviado = self._enviar(
                "post", url, headers={"Accept": "application/json"},
                dados={"grant_type": "client_credentials", "client_id": usuario, "client_secret": _client_secret()},
            )
            if isinstance(enviado, RespostaTIM):
                return "", enviado
            status = enviado.status_code
            if status in (400, 401, 403):
                return "", RespostaTIM(
                    "falha_autenticacao", status, mensagem_erro=self._mensagem_de_recusa(enviado, "o usuário e a senha da API"),
                )
            if status == 429:
                return "", RespostaTIM(
                    "limite_excedido", status, retry_after_s=_retry_after_segundos(getattr(enviado, "headers", None)),
                    mensagem_erro="A API limitou as requisições (HTTP 429) ao pedir o token de acesso.",
                )
            if status >= 500:
                return "", RespostaTIM(
                    "indisponivel", status, mensagem_erro=f"A API está indisponível ou com erro (HTTP {status}) ao pedir o token de acesso.",
                )
            if not 200 <= status < 300:
                return "", RespostaTIM(
                    "resposta_invalida", status, mensagem_erro=f"A API recusou o pedido de token de acesso (HTTP {status}).",
                )
            try:
                dados = enviado.json()
            except (ValueError, TypeError):
                dados = None
            acesso = dados.get("access_token") if isinstance(dados, dict) else None
            if not isinstance(acesso, str) or not acesso.strip():
                return "", RespostaTIM(
                    "resposta_invalida", status, mensagem_erro="A API não devolveu o token de acesso esperado.",
                )
            try:
                validade = int(dados.get("expires_in") or VALIDADE_TOKEN_PADRAO_S)
            except (TypeError, ValueError):
                validade = VALIDADE_TOKEN_PADRAO_S
            _ACESSO.update(
                valor=acesso.strip(), usuario=usuario,
                expira=time.monotonic() + max(60, validade - MARGEM_TOKEN_S),
            )
            return _ACESSO["valor"], None

    # ── consulta ─────────────────────────────────────────────
    def consultar(self, chip, tipo: str, origem: str = "agendada") -> RespostaTIM:
        """Uma consulta (`sessao` ou `consumo`) do chip. Nunca levanta por causa da
        API: o que deu errado vem em `RespostaTIM.resultado`. Só levanta `SemToken`
        e `ConfiguracaoInvalida` (problemas de configuração, não da resposta).

        De onde sai a resposta, nesta ordem:
          1. a lista completa já carregada (`carregar_lote`) — sem chamada nenhuma;
          2. a consulta de MENOS DE 10 s atrás ao mesmo chip (sessão e consumo são o
             mesmo registro: "Atualizar tudo" faz uma chamada só);
          3. `GET {base}/simcards/{id}/`, com retentativa só para timeout, erro de rede
             e 5xx (até 2 na agendada e 1 nas demais origens, com espera de 1 s e depois
             3 s). Nunca para 401, 404 ou 429."""
        self._exigir_credencial()
        try:
            identificador = identificador_do_chip(chip)
        except ValidationError as exc:
            return RespostaTIM(
                "resposta_invalida", tentativas=0, identificador_tipo=chip.identificador_consulta,
                mensagem_erro=f"Identificador do cadastro inválido ({' '.join(exc.messages)}). A consulta não foi enviada.",
            )
        inicio = time.monotonic()
        resposta = self._do_lote(chip)
        if resposta is None:
            url = self.url_da_consulta(identificador, tipo)
            resposta = self._do_memo(url)
            if resposta is None:
                resposta = self._com_retentativa(url, origem)
                resposta.duracao_ms = int((time.monotonic() - inicio) * 1000)
                if resposta.valida:
                    self._memo[url] = (time.monotonic(), replace(resposta))
                log = logger.warning if resposta.interrompe_ciclo else logger.info
                log(
                    "TIM M2M: chip %s, %s -> %s (HTTP %s, %s tentativa(s), %s ms)",
                    chip.pk, tipo, resposta.resultado, resposta.http_status, resposta.tentativas, resposta.duracao_ms,
                )
        resposta.identificador_tipo = chip.identificador_consulta
        resposta.mensagem_erro = sanear(resposta.mensagem_erro, self._segredos_em_uso())
        return resposta

    def consultar_dia(self, chip, dia, origem: str = "agendada") -> RespostaTIM:
        """As sessões de dados do chip no `dia` (uma `date`, no fuso do sistema), seguindo a paginação.

        Devolve `RespostaTIM` com `payload = {"objetos": [...], "paginas": n, "completa": bool}`; dia sem
        sessão (a API responde `{"msg": "sem_historico_no_periodo"}`) é SUCESSO com `objetos` vazio — não é
        falha e não diz nada sobre o chip estar ou não conectado. `completa=False` quando a paginação foi
        cortada (trava de páginas ou outro host). Mesmas regras de `consultar`: nunca levanta por causa
        da API, retentativa só para timeout, erro de rede e 5xx, e respeita `ritmo_eventos_s`."""
        self._exigir_credencial()
        try:
            identificador = identificador_do_chip(chip)
        except ValidationError as exc:
            return RespostaTIM(
                "resposta_invalida", tentativas=0, identificador_tipo=chip.identificador_consulta,
                mensagem_erro=f"Identificador do cadastro inválido ({' '.join(exc.messages)}). A consulta não foi enviada.",
            )
        url = self.url_dos_eventos(identificador, dia)
        inicio = time.monotonic()
        objetos: list = []
        paginas, tentativas, completa = 0, 0, True
        while url and paginas < MAX_PAGINAS_DIA:
            espera = self.ritmo_eventos_s - (time.monotonic() - self._ultimo_evento)
            if self._ultimo_evento and espera > 0:
                time.sleep(espera)
            resposta = self._com_retentativa(url, origem, limitar=False)
            self._ultimo_evento = time.monotonic()
            tentativas += resposta.tentativas
            if not resposta.valida:
                resposta.tentativas = tentativas
                resposta.duracao_ms = int((time.monotonic() - inicio) * 1000)
                resposta.identificador_tipo = chip.identificador_consulta
                resposta.mensagem_erro = sanear(resposta.mensagem_erro, self._segredos_em_uso())
                log = logger.warning if resposta.interrompe_ciclo else logger.info
                log("TIM M2M: chip %s, eventos de %s -> %s (HTTP %s)", chip.pk, dia, resposta.resultado, resposta.http_status)
                return resposta
            dados = resposta.payload
            pagina = dados.get("objetos") if isinstance(dados, dict) else None
            if isinstance(pagina, dict):
                pagina = list(pagina.values())
            if pagina is None and isinstance(dados, dict) and "msg" in dados:
                pagina = []          # `sem_historico_no_periodo`: o dia existe, só não teve sessão
            if not isinstance(pagina, list):
                return RespostaTIM(
                    "resposta_invalida", resposta.http_status, tentativas=tentativas, identificador_tipo=chip.identificador_consulta,
                    duracao_ms=int((time.monotonic() - inicio) * 1000),
                    mensagem_erro="A lista de sessões de dados veio num formato inesperado.",
                )
            objetos.extend(o for o in pagina if isinstance(o, dict))
            paginas += 1
            try:
                url = self._url_da_pagina((dados.get("meta") or {}).get("proxima")) if isinstance(dados, dict) else None
            except ConfiguracaoInvalida:
                url, completa = None, False
                logger.warning("TIM M2M: a paginação dos eventos apontou para outro host; a lista ficou incompleta.")
        if url:
            completa = False
        logger.info("TIM M2M: chip %s, eventos de %s -> %s sessão(ões), %s página(s)", chip.pk, dia, len(objetos), paginas)
        return RespostaTIM(
            "sucesso", 200, {"objetos": objetos, "paginas": paginas, "completa": completa}, tentativas=tentativas,
            duracao_ms=int((time.monotonic() - inicio) * 1000), identificador_tipo=chip.identificador_consulta,
        )

    def _do_memo(self, url: str) -> RespostaTIM | None:
        guardado = self._memo.get(url)
        if not guardado or time.monotonic() - guardado[0] > MEMO_SEGUNDOS:
            return None
        return replace(guardado[1], tentativas=0, duracao_ms=0)

    def _com_retentativa(self, url: str, origem: str, *, limitar: bool = True) -> RespostaTIM:
        maximo = RETENTATIVAS_AGENDADA if origem == "agendada" else RETENTATIVAS_MANUAL
        tentativa = 0
        while True:
            tentativa += 1
            resposta = self._chamar_autenticado(url, limitar=limitar)
            if resposta.resultado in RESULTADOS_RETENTAVEIS and tentativa <= maximo:
                time.sleep(ESPERAS_RETENTATIVA[tentativa - 1])
                continue
            break
        resposta.tentativas = tentativa
        return resposta

    def _chamar_autenticado(self, url: str, *, limitar: bool = True) -> RespostaTIM:
        bearer, falha = self._credencial()
        if falha is not None:
            return falha
        resposta = self._get(url, bearer, limitar)
        if resposta.http_status == 401 and modo_credencial() == "oauth2":
            # O token de acesso pode ter vencido ou sido trocado por outro pedido do mesmo
            # usuário (a API admite um token por usuário): renova UMA vez antes de desistir.
            bearer, falha = self._credencial(forcar=True)
            if falha is not None:
                return falha
            resposta = self._get(url, bearer, limitar)
        return resposta

    # ── a lista completa ─────────────────────────────────────
    def carregar_lote(self, origem: str = "agendada") -> RespostaTIM:
        """Lê TODOS os SIM Cards da conta (`GET {base}/simcards/`, seguindo `meta.proxima`)
        e guarda um índice por ICCID e por MSISDN; as consultas seguintes
        (`consultar`) saem dele, sem chamada nenhuma. É uma chamada para centenas de
        chips. Devolve a resposta da leitura (`payload` = só `{"registros", "paginas",
        "completa"}`); se ela falhar, TODO chip consultado depois herda essa falha, e
        é assim que cada um registra a sua `falha de consulta`.

        Chip sem ICCID nem número da linha (só IMSI) não é achado na lista — a resposta
        dela não traz IMSI — e cai na consulta individual."""
        self._exigir_credencial()
        self._lote, self._lote_resposta, self._lote_completo, self._lote_total = None, None, False, 0
        inicio = time.monotonic()
        url = self.url_da_lista()
        registros: list[dict] = []
        paginas, tentativas, incompleta = 0, 0, False
        while url and paginas < MAX_PAGINAS_LOTE:
            resposta = self._com_retentativa(url, origem, limitar=False)
            tentativas += resposta.tentativas
            falha = None
            if not resposta.valida:
                falha = resposta
            else:
                dados = resposta.payload
                objetos = dados.get("objetos") if isinstance(dados, dict) else None
                if isinstance(objetos, dict):
                    objetos = list(objetos.values())
                if not isinstance(objetos, list):
                    falha = RespostaTIM(
                        "resposta_invalida", resposta.http_status,
                        mensagem_erro="A lista de SIM Cards veio num formato inesperado.",
                    )
            if falha is not None:
                falha.tentativas = tentativas
                falha.duracao_ms = int((time.monotonic() - inicio) * 1000)
                falha.mensagem_erro = sanear(falha.mensagem_erro, self._segredos_em_uso())
                self._lote_resposta = falha
                logger.warning("TIM M2M: lista de SIM Cards -> %s (HTTP %s)", falha.resultado, falha.http_status)
                return falha
            registros.extend(r for r in objetos if isinstance(r, dict))
            paginas += 1
            try:
                url = self._url_da_pagina((dados.get("meta") or {}).get("proxima"))
            except ConfiguracaoInvalida:
                url, incompleta = None, True
                logger.warning("TIM M2M: a paginação apontou para outro host; a lista ficou incompleta.")
                break
        self._lote_completo = url is None and not incompleta

        por_iccid: dict[str, dict] = {}
        por_msisdn: dict[str, dict] = {}
        for reg in registros:
            iccid = re.sub(r"\D", "", str(reg.get("iccid") or ""))
            linha = re.sub(r"\D", "", str(reg.get("msisdn") or ""))
            if iccid:
                por_iccid[iccid] = reg
            if linha:
                por_msisdn[linha] = reg
                if linha.startswith("55"):
                    por_msisdn.setdefault(linha[2:], reg)
        self._lote = {"iccid": por_iccid, "msisdn": por_msisdn}
        self._lote_total = len(registros)
        duracao = int((time.monotonic() - inicio) * 1000)
        logger.info(
            "TIM M2M: lista de SIM Cards lida (%s registro(s), %s página(s), %s ms%s)",
            len(registros), paginas, duracao, "" if self._lote_completo else ", INCOMPLETA",
        )
        return RespostaTIM(
            "sucesso", 200, {"registros": len(registros), "paginas": paginas, "completa": self._lote_completo},
            duracao_ms=duracao, tentativas=tentativas,
        )

    @property
    def lote_carregado(self) -> bool:
        """A lista foi lida (inteira ou não)? Se a leitura falhou, é `False`."""
        return self._lote is not None

    @property
    def lote_resposta(self) -> RespostaTIM | None:
        """A falha da leitura da lista, se houve."""
        return self._lote_resposta

    def _do_lote(self, chip) -> RespostaTIM | None:
        if self._lote_resposta is not None:
            return replace(self._lote_resposta)
        if self._lote is None:
            return None
        iccid = re.sub(r"\D", "", chip.iccid or "")
        linha = re.sub(r"\D", "", getattr(chip, "numero_linha", "") or "")
        achado = self._lote["iccid"].get(iccid) if iccid else None
        if achado is None and linha:
            achado = self._lote["msisdn"].get(linha) or self._lote["msisdn"].get(
                linha[2:] if linha.startswith("55") else linha
            )
        if achado is not None:
            return RespostaTIM(
                "sucesso", 200, preparar_payload(achado, self._segredos_em_uso()), tentativas=0, duracao_ms=0,
            )
        if (iccid or linha) and self._lote_completo:
            return RespostaTIM(
                "nao_encontrado", 200, tentativas=0,
                mensagem_erro=(
                    f"Este chip não consta na lista de SIM Cards da conta ({self._lote_total} lidos). "
                    "Isso não significa que esteja sem sessão."
                ),
            )
        return None

    # ── HTTP ─────────────────────────────────────────────────
    def _enviar(self, metodo: str, url: str, *, headers: dict, dados=None):
        """A resposta HTTP, ou — se a rede falhou — um `RespostaTIM` já classificado."""
        host = urlsplit(url).hostname
        self.chamadas += 1
        try:
            if metodo == "post":
                return self._http.post(
                    url, data=dados, headers=headers, timeout=TIMEOUT, allow_redirects=False, verify=True,
                )
            return self._http.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=False, verify=True)
        except requests.Timeout:
            return RespostaTIM("timeout", mensagem_erro="Tempo esgotado: a API não respondeu a tempo.")
        except requests.exceptions.SSLError:
            return RespostaTIM("erro_rede", mensagem_erro="Erro de rede: falha na verificação do certificado TLS do servidor.")
        except requests.ConnectionError as exc:
            if any(m in str(exc).lower() for m in _MARCAS_DNS):
                return RespostaTIM("erro_rede", mensagem_erro=f"Erro de rede: host não encontrado ({host}).")
            return RespostaTIM("erro_rede", mensagem_erro="Erro de rede: não foi possível conectar ao servidor da API.")
        except requests.RequestException as exc:
            return RespostaTIM("erro_rede", mensagem_erro=f"Erro de rede ({exc.__class__.__name__}).")

    def _get(self, url: str, bearer: str, limitar: bool) -> RespostaTIM:
        enviado = self._enviar("get", url, headers={"Authorization": f"Bearer {bearer}", "Accept": "application/json"})
        if isinstance(enviado, RespostaTIM):
            return enviado
        return self._classificar(enviado, self._segredos_em_uso(), limitar)

    @staticmethod
    def _corpo_json(r, segredos, limitar: bool = True):
        try:
            return preparar_payload(r.json(), segredos, limitar=limitar)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _mensagem_de_recusa(r, o_que: str) -> str:
        """401/403/400 da API. A Saitro só atende IPs liberados e responde 403
        `acesso_negado_ip_nao_autorizado` — que NÃO é senha errada e não se resolve
        trocando a credencial."""
        status = r.status_code
        try:
            corpo = str(getattr(r, "text", "") or "").lower()
        except Exception:  # noqa: BLE001 — resposta falsa/estranha: cai na mensagem genérica
            corpo = ""
        if "ip_nao_autorizado" in corpo or "ip nao autorizado" in corpo:
            return (
                f"A API recusou o IP deste servidor (HTTP {status}: acesso negado por IP). A credencial não "
                "foi o problema: peça à Saitro/TIM para liberar o IP de saída do servidor."
            )
        return f"A API recusou a credencial (HTTP {status}). Confira {o_que}."

    def _classificar(self, r, segredos, limitar: bool = True) -> RespostaTIM:
        status = r.status_code
        if status in (401, 403):
            return RespostaTIM(
                "falha_autenticacao", status, self._corpo_json(r, segredos),
                mensagem_erro=self._mensagem_de_recusa(r, "o usuário e a senha da API (ou o token)"),
            )
        if status == 404:
            return RespostaTIM(
                "nao_encontrado", status, self._corpo_json(r, segredos),
                mensagem_erro="A API não encontrou este identificador (HTTP 404). Isso não significa que o chip esteja sem sessão.",
            )
        if status == 429:
            return RespostaTIM(
                "limite_excedido", status, retry_after_s=_retry_after_segundos(getattr(r, "headers", None)),
                mensagem_erro="A API limitou as requisições (HTTP 429). As consultas ficam pausadas.",
            )
        if status >= 500:
            return RespostaTIM(
                "indisponivel", status, self._corpo_json(r, segredos),
                mensagem_erro=f"A API está indisponível ou com erro (HTTP {status}).",
            )
        if 300 <= status < 400:
            return RespostaTIM(
                "resposta_invalida", status,
                mensagem_erro=(
                    f"A API respondeu com redirecionamento (HTTP {status}), que não é seguido "
                    "para não enviar a credencial a outro endereço."
                ),
            )
        if not 200 <= status < 300:
            return RespostaTIM(
                "resposta_invalida", status, self._corpo_json(r, segredos),
                mensagem_erro=f"A API recusou a consulta (HTTP {status}).",
            )
        corpo = getattr(r, "content", b"") or b""
        if status == 204 or not corpo.strip():
            return RespostaTIM("sucesso_sem_corpo", status)
        try:
            dados = r.json()
        except (ValueError, TypeError):
            return RespostaTIM(
                "resposta_invalida", status,
                mensagem_erro=f"A API respondeu HTTP {status}, mas o conteúdo não é JSON.",
            )
        if isinstance(dados, list):
            # A consulta de UM SIM Card devolve uma lista com o registro (a doc mostra um objeto).
            if not dados:
                return RespostaTIM(
                    "nao_encontrado", status,
                    mensagem_erro="A API devolveu uma lista vazia para este identificador. Isso não significa que o chip esteja sem sessão.",
                )
            if len(dados) > 1 or not isinstance(dados[0], dict):
                return RespostaTIM(
                    "resposta_invalida", status,
                    mensagem_erro=f"A API devolveu {len(dados)} registros para o mesmo identificador; não dá para saber qual vale.",
                )
            dados = dados[0]
        if isinstance(dados, dict) and "erro" in dados and not any(k in dados for k in ("iccid", "objetos")):
            return RespostaTIM(
                "resposta_invalida", status, preparar_payload(dados, segredos),
                mensagem_erro=f"A API respondeu: {str(dados.get('erro'))[:200]}",
            )
        return RespostaTIM("sucesso", status, preparar_payload(dados, segredos, limitar=limitar))
