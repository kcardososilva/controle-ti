"""
Transporte HTTP da API TIM M2M — só o que fala com a rede e com a credencial.

Por que é assim (e por que é desconfiado): o domínio `tim-m2m-platform.com` não
resolve no DNS e não há documentação pública da API. Este módulo faz o que a
especificação recebida diz — `GET {base}/{imsi_ou_iccid}/session` e `/usage`, com
`Authorization: Bearer` — e NADA além disso: não interpreta a resposta
(`m2m_interpretacao`), não decide o que é falha de chip (`m2m_monitoramento_service`).

O que ele garante:

  • HOST TRAVADO. O endereço vem só de `settings.TIM_M2M_BASE_URL` (.env); nenhuma
    tela aceita URL. Só https, e a cada chamada o esquema e o host da URL montada
    são conferidos de novo. `allow_redirects=False`: um redirect levaria o token
    para outro host — resposta 3xx vira `resposta_invalida`, não é seguida.
  • O TOKEN NUNCA SAI DAQUI. Não vai para log, nem para mensagem de erro, nem para o
    payload guardado (`sanear`/`preparar_payload` removem o valor e qualquer
    `Bearer ...`). Os headers da requisição nunca são registrados.
  • TOKEN NO BANCO É CIFRADO (Fernet, chave `TIM_M2M_CHAVE_CIFRA` do .env). Se
    `TIM_M2M_TOKEN` existir no .env, ele VENCE o do banco. Sem a chave, salvar token
    é recusado — nunca se grava em claro.
  • CLASSIFICAÇÃO HONESTA. 404 é `nao_encontrado` (nunca "sem sessão"); falha de rede
    ou de API nunca é "chip inativo". Retentativa só para timeout, erro de rede e
    5xx (nunca para 401, 404 ou 429).

`ClienteTIM(sessao=None)` recebe a sessão HTTP por injeção: o teste passa uma falsa e
nenhuma chamada de rede acontece fora de produção (mesmo desenho de `caixa_graph_service`).
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlsplit

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
CAMINHO_DO_TIPO = {"sessao": "session", "consumo": "usage"}
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


def sanear(texto, token: str = "") -> str:
    """Remove o token (se informado) e qualquer `Bearer <algo>` do texto. Toda
    mensagem de erro que vai para log, banco ou tela passa por aqui."""
    saida = str(texto or "")
    if token and len(token) >= 6:
        saida = saida.replace(token, "[token]")
    return _BEARER.sub("Bearer [oculto]", saida)


def preparar_payload(valor, token: str = ""):
    """O JSON a guardar em `M2MConsulta.payload`: o próprio valor, com o token
    redigido caso a API o ecoe, e limitado a 64 KB. Acima disso devolve um envelope
    `{"_truncado": true, "_tamanho_original_bytes": N, "texto": "<início do JSON>"}`."""
    if valor is None:
        return None
    try:
        texto = json.dumps(valor, allow_nan=False)
    except (TypeError, ValueError):
        return {"_nao_serializavel": True}
    if token and len(token) >= 6 and token in texto:
        texto = texto.replace(token, "[token]")
        valor = json.loads(texto)
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
    `legivel` é False quando há token no banco mas ele não decifra (chave trocada ou ausente)."""
    from ProjetoEstoque.models import M2MIntegracao

    cfg = M2MIntegracao.get()
    do_env = _token_env()
    no_banco = bool(cfg.token_cifrado)
    base = {
        "token_env_definido": bool(do_env), "token_no_banco": no_banco,
        "chave_cifra_configurada": chave_cifra_configurada(),
    }
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
        return (
            "Nenhum token configurado. Informe o token em Monitoramento TIM M2M, na tela de "
            "Configuração, ou defina TIM_M2M_TOKEN no .env."
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


class ClienteTIM:
    def __init__(self, sessao=None):
        # `sessao` existe para o teste injetar uma sessão falsa: nenhuma chamada
        # de rede acontece fora de produção.
        self._http = sessao or requests.Session()
        self._token: str | None = None

    # ── URL ──────────────────────────────────────────────────
    @staticmethod
    def url_da_consulta(identificador: str, tipo: str) -> str:
        """`{base}/{identificador}/session|usage`, com o identificador codificado, e a
        conferência final de esquema e host. `ConfiguracaoInvalida` se algo não bate."""
        if tipo not in CAMINHO_DO_TIPO:
            raise TimM2MErro(f"Tipo de consulta desconhecido: {tipo!r}.")
        base = base_url()
        url = f"{base}/{quote(str(identificador), safe='')}/{CAMINHO_DO_TIPO[tipo]}"
        alvo, origem = urlsplit(url), urlsplit(base)
        if alvo.scheme != "https" or alvo.hostname != origem.hostname or alvo.port != origem.port:
            raise ConfiguracaoInvalida("A URL montada não aponta para o host configurado; a consulta foi bloqueada.")
        return url

    def _token_em_uso(self) -> str:
        if self._token is None:
            self._token = token()
        if not self._token:
            raise SemToken(motivo_nao_configurada() or "Nenhum token configurado.")
        return self._token

    # ── consulta ─────────────────────────────────────────────
    def consultar(self, chip, tipo: str, origem: str = "agendada") -> RespostaTIM:
        """Uma consulta (`sessao` ou `consumo`) do chip. Nunca levanta por causa da
        API: o que deu errado vem em `RespostaTIM.resultado`. Só levanta `SemToken`
        e `ConfiguracaoInvalida` (problemas de configuração, não da resposta).

        Retentativa só para timeout, erro de rede e 5xx: até 2 na agendada e 1 nas
        demais origens (manual, teste), com espera de 1 s e depois 3 s. Nunca para
        401, 404 ou 429."""
        token_em_uso = self._token_em_uso()
        try:
            identificador = identificador_do_chip(chip)
        except ValidationError as exc:
            return RespostaTIM(
                "resposta_invalida", tentativas=0, identificador_tipo=chip.identificador_consulta,
                mensagem_erro=f"Identificador do cadastro inválido ({' '.join(exc.messages)}). A consulta não foi enviada.",
            )
        url = self.url_da_consulta(identificador, tipo)
        maximo = RETENTATIVAS_AGENDADA if origem == "agendada" else RETENTATIVAS_MANUAL

        inicio = time.monotonic()
        tentativa = 0
        while True:
            tentativa += 1
            resposta = self._chamar(url, token_em_uso)
            if resposta.resultado in RESULTADOS_RETENTAVEIS and tentativa <= maximo:
                time.sleep(ESPERAS_RETENTATIVA[tentativa - 1])
                continue
            break
        resposta.tentativas = tentativa
        resposta.duracao_ms = int((time.monotonic() - inicio) * 1000)
        resposta.identificador_tipo = chip.identificador_consulta
        resposta.mensagem_erro = sanear(resposta.mensagem_erro, token_em_uso)

        log = logger.warning if resposta.interrompe_ciclo else logger.info
        log(
            "TIM M2M: chip %s, %s -> %s (HTTP %s, %s tentativa(s), %s ms)",
            chip.pk, tipo, resposta.resultado, resposta.http_status, tentativa, resposta.duracao_ms,
        )
        return resposta

    def _chamar(self, url: str, token_em_uso: str) -> RespostaTIM:
        host = urlsplit(url).hostname
        try:
            r = self._http.get(
                url,
                headers={"Authorization": f"Bearer {token_em_uso}", "Accept": "application/json"},
                timeout=TIMEOUT, allow_redirects=False, verify=True,
            )
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
        return self._classificar(r, token_em_uso)

    @staticmethod
    def _corpo_json(r, token_em_uso: str):
        try:
            return preparar_payload(r.json(), token_em_uso)
        except (ValueError, TypeError):
            return None

    def _classificar(self, r, token_em_uso: str) -> RespostaTIM:
        status = r.status_code
        if status in (401, 403):
            return RespostaTIM(
                "falha_autenticacao", status, self._corpo_json(r, token_em_uso),
                mensagem_erro=f"A API recusou a credencial (HTTP {status}). Confira o token.",
            )
        if status == 404:
            return RespostaTIM(
                "nao_encontrado", status, self._corpo_json(r, token_em_uso),
                mensagem_erro="A API não encontrou este identificador (HTTP 404). Isso não significa que o chip esteja sem sessão.",
            )
        if status == 429:
            return RespostaTIM(
                "limite_excedido", status, retry_after_s=_retry_after_segundos(getattr(r, "headers", None)),
                mensagem_erro="A API limitou as requisições (HTTP 429). As consultas ficam pausadas.",
            )
        if status >= 500:
            return RespostaTIM(
                "indisponivel", status, self._corpo_json(r, token_em_uso),
                mensagem_erro=f"A API está indisponível ou com erro (HTTP {status}).",
            )
        if 300 <= status < 400:
            return RespostaTIM(
                "resposta_invalida", status,
                mensagem_erro=(
                    f"A API respondeu com redirecionamento (HTTP {status}), que não é seguido "
                    "para não enviar o token a outro endereço."
                ),
            )
        if not 200 <= status < 300:
            return RespostaTIM(
                "resposta_invalida", status, self._corpo_json(r, token_em_uso),
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
        return RespostaTIM("sucesso", status, preparar_payload(dados, token_em_uso))
