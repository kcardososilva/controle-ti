"""
Interpretação da resposta da API TIM M2M — só o que o MAPEAMENTO configurado manda.

Por que existe: o domínio da API (`tim-m2m-platform.com`) não resolve no DNS e não
há documentação pública dos endpoints, então os nomes dos campos da resposta NÃO
são conhecidos. Em vez de inventá-los, a resposta é guardada crua e este módulo só
a interpreta quando a tela de Integração disser, depois da primeira resposta real,
qual campo é o estado da sessão, qual é o volume e qual é a unidade. Sem
mapeamento nada é interpretado: o resultado é `nao_interpretado` — nunca um
palpite ("sessão ativa", "volume X") que o dado não sustenta.

Funções PURAS: recebem o payload (JSON já decodificado) e o mapeamento, devolvem
valores. Nada aqui lê o banco, a rede ou o relógio.

Formato do mapeamento (`M2MIntegracao.mapeamento`) — caminhos no estilo
`a.b[0].c`:

    {
      "sessao":    {"campo_estado": "...", "valores_ativa": ["..."],
                    "valores_sem_sessao": ["..."], "campos_exibir": ["..."]},
      "consumo":   {"campo_volume": "...", "campo_unidade": "..." | "unidade_fixa": "MB",
                    "base": 1000 | 1024,
                    "campo_periodo_inicio": "...", "campo_periodo_fim": "..."},
      "identidade": {"campo_iccid": "...", "campo_imsi": "..."}
    }

Mudar o mapeamento NÃO reescreve as consultas antigas por si: quem recalcula os
campos normalizados do histórico é `m2m_monitoramento_service.reinterpretar_historico`
(e nunca toca no payload).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, time as hora_do_dia, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

# Estados de sessão que a interpretação pode devolver.
ATIVA = "ativa"
SEM_SESSAO = "sem_sessao"
NAO_INTERPRETADO = "nao_interpretado"

BASES_VALIDAS = (1000, 1024)

# Uma sessão aberta cuja duração a plataforma não atualiza há mais que isto deixou de estar viva: a
# plataforma avança a sessão em andamento de ~15 em ~15 min, então 2 h sem avanço é sessão que
# acabou sem ninguém fechá-la. Sem este teto, uma linha esquecida valeria "online" para sempre.
LIMITE_SESSAO_PARADA_S = 2 * 3600
MAX_BIGINT = 2 ** 63 - 1

_CAMINHO_VALIDO = re.compile(r"(?:\.?[^.\[\]]+|\[\d+\])+")
_PASSO = re.compile(r"\.?([^.\[\]]+)|\[(\d+)\]")
_NUMERO = re.compile(r"-?\d+(?:\.\d+)?")

# Mapeamento da API GSIM (tim.saitro.com), tirado da documentação oficial e da
# resposta real da conta (13/11/2026) — NÃO é palpite:
#   • `conexao` vale `online`, `offline` ou `nunca_conectou` (a doc escreve
#     `nunca_conecto`, mas a resposta real traz `nunca_conectou`; qualquer outro
#     texto continua "não interpretado" e aparece cru na tela);
#   • `servicos.dados.consumo` é texto com a unidade colada ("0,00 KB"); a doc não
#     diz o período a que ele se refere, por isso nenhum período é mapeado;
#   • a base do KB/MB é 1024: o portal da Saitro mostra 703,70 KB / 14,50 MB / 15,19 MB para
#     720.593 / 15.207.610 / 15.928.203 bytes (conferido em 13/11/2026). O valor e a unidade
#     originais são guardados;
#   • `_eventos` e `_dados_periodo` NÃO vêm da API: são acrescentados à resposta pelo serviço de
#     dados (`m2m_dados_service`) a partir das sessões de dados da plataforma, que refletem o uso
#     muito antes do resumo do SIM Card. Quando existem, mandam no estado da sessão e no volume.
MAPEAMENTO_GSIM = {
    "sessao": {
        "campo_estado": "conexao",
        "valores_ativa": ["online"],
        "valores_sem_sessao": ["offline", "nunca_conectou"],
        "campos_exibir": ["ultimo_acesso", "rede", "ultima_apn", "tecnologia", "cobertura", "status"],
    },
    "consumo": {
        "campo_volume": "servicos.dados.consumo",
        "unidade_no_valor": True,
        "base": 1024,
    },
    "identidade": {"campo_iccid": "iccid"},
}


# ─────────────────────────────────────────────────────────────
# Caminhos
# ─────────────────────────────────────────────────────────────

def caminho_valido(caminho) -> bool:
    """`a.b[0].c` — chaves separadas por ponto e índices entre colchetes."""
    return isinstance(caminho, str) and bool(_CAMINHO_VALIDO.fullmatch(caminho.strip()))


def valor_em(payload, caminho):
    """Resolve `a.b[0].c` dentro do payload. Tolerante: caminho inexistente,
    índice fora da lista, tipo diferente do esperado ou caminho malformado
    devolvem `None` — nunca levantam. (Um `null` do JSON também vira `None`.)"""
    if payload is None or not isinstance(caminho, str):
        return None
    caminho = caminho.strip()
    if not caminho or not _CAMINHO_VALIDO.fullmatch(caminho):
        return None
    atual = payload
    for passo in _PASSO.finditer(caminho):
        chave, indice = passo.group(1), passo.group(2)
        if chave is not None:
            if not isinstance(atual, dict) or chave not in atual:
                return None
            atual = atual[chave]
        else:
            i = int(indice)
            if not isinstance(atual, list) or i >= len(atual):
                return None
            atual = atual[i]
    return atual


def _tipo_do_valor(valor) -> str:
    if valor is None:
        return "nulo"
    if isinstance(valor, bool):
        return "booleano"
    if isinstance(valor, (int, float)):
        return "numero"
    return "texto"


def caminhos_com_exemplo(payload, *, max_caminhos: int = 300, max_profundidade: int = 8,
                         max_itens_lista: int = 5) -> list[dict]:
    """Caminhos achatados das FOLHAS do payload, para a tela de mapeamento:
    `[{"caminho": "a.b[0].c", "tipo": "texto|numero|booleano|nulo", "exemplo": "..."}]`,
    na ordem em que aparecem. Listas contribuem só com os primeiros itens e a
    quantidade total é limitada (uma resposta enorme não pode inflar a tela).
    Chaves com `.`, `[` ou `]` no nome não têm caminho endereçável e são puladas."""
    saida: list[dict] = []

    def visita(valor, caminho: str, nivel: int):
        if len(saida) >= max_caminhos or nivel > max_profundidade:
            return
        if isinstance(valor, dict):
            for chave, filho in valor.items():
                if not isinstance(chave, str) or not chave or re.search(r"[.\[\]]", chave):
                    continue
                visita(filho, f"{caminho}.{chave}" if caminho else chave, nivel + 1)
        elif isinstance(valor, list):
            for i, filho in enumerate(valor[:max_itens_lista]):
                visita(filho, f"{caminho}[{i}]", nivel + 1)
        elif caminho:
            exemplo = "" if valor is None else str(valor)
            saida.append({"caminho": caminho, "tipo": _tipo_do_valor(valor), "exemplo": exemplo[:80]})

    visita(payload, "", 0)
    return saida


def caminhos(payload, **kwargs) -> list[str]:
    """Só os caminhos de `caminhos_com_exemplo` (o `datalist` da tela de mapeamento)."""
    return [c["caminho"] for c in caminhos_com_exemplo(payload, **kwargs)]


# ─────────────────────────────────────────────────────────────
# Unidades e formatação
# ─────────────────────────────────────────────────────────────

_EXPOENTE_DECIMAL = {"b": 0, "byte": 0, "bytes": 0, "kb": 1, "mb": 2, "gb": 3, "tb": 4}
_EXPOENTE_BINARIO = {"kib": 1, "mib": 2, "gib": 3, "tib": 4}


def fator_unidade(unidade, base: int = 1000) -> int | None:
    """Quantos bytes vale 1 `unidade`, ou `None` se a unidade não é reconhecida.

    KB/MB/GB/TB seguem `base` (1000 ou 1024, escolha de quem configura);
    KiB/MiB/GiB/TiB são sempre 1024. Unidades que podem ser BITS (`Mb`, `Gb`,
    `Kb`, `Tb` com "b" minúsculo e inicial maiúscula, e `b` sozinho) não são
    aceitas: ler megabits como megabytes erraria o volume por 8x sem avisar."""
    u = (unidade or "").strip() if isinstance(unidade, str) else ""
    if not u or u == "b" or re.fullmatch(r"[KMGT]b", u):
        return None
    chave = u.lower()
    if chave in _EXPOENTE_BINARIO:
        return 1024 ** _EXPOENTE_BINARIO[chave]
    if chave in _EXPOENTE_DECIMAL:
        return (base if base in BASES_VALIDAS else 1000) ** _EXPOENTE_DECIMAL[chave]
    return None


def unidade_conhecida(unidade) -> bool:
    return fator_unidade(unidade) is not None


def formatar_bytes(valor, base: int = 1000, *, casas_fixas: bool = False) -> str:
    """Volume legível em B/KB/MB/GB/TB com vírgula decimal (`1,5 GB`); `—` quando
    não há valor. `casas_fixas` mantém os dois decimais (`14,50 MB`), como o portal da Saitro. É só EXIBIÇÃO — o valor e a unidade originais continuam
    guardados na consulta."""
    if valor is None:
        return "—"
    try:
        n = int(valor)
    except (TypeError, ValueError):
        return "—"
    base = base if base in BASES_VALIDAS else 1000
    if n < base:
        return f"{n} B"
    numero, i = Decimal(n), 0
    while numero >= base and i < 4:
        numero /= base
        i += 1
    texto = f"{numero.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):f}"
    if "." in texto and not casas_fixas:
        texto = texto.rstrip("0").rstrip(".")
    return f"{texto.replace('.', ',')} {('B', 'KB', 'MB', 'GB', 'TB')[i]}"


def _decimal(valor) -> Decimal | None:
    """Número não negativo a partir de int/float/texto em formato simples
    (`123`, `12.5`). Texto com vírgula, milhar ou unidade colada NÃO é adivinhado."""
    if isinstance(valor, bool) or valor is None:
        return None
    try:
        if isinstance(valor, (int, float)):
            numero = Decimal(str(valor))
        elif isinstance(valor, str) and _NUMERO.fullmatch(valor.strip()):
            numero = Decimal(valor.strip())
        else:
            return None
    except InvalidOperation:
        return None
    return numero if numero.is_finite() and numero >= 0 else None


_VOLUME_COM_UNIDADE = re.compile(r"\s*(\d[\d.,]*)\s*([A-Za-z]+)\s*")


def _numero_formatado(texto: str) -> tuple[Decimal | None, str]:
    """Número com `.` ou `,` como separador ("0,00", "2.47", "1.234,56") → (Decimal, "").
    Quando não dá para saber o que o separador significa, NÃO adivinha: devolve
    (None, motivo). "1.234" é ambíguo (1,234 ou 1234) e é recusado; "0,000" não é,
    porque vale zero nas duas leituras."""
    t = (texto or "").strip()
    if not re.fullmatch(r"\d[\d.,]*", t):
        return None, f"O valor '{texto}' não é um número."
    pontos, virgulas = t.count("."), t.count(",")
    if pontos and virgulas:
        decimal = "." if t.rfind(".") > t.rfind(",") else ","
        milhar = "," if decimal == "." else "."
        if t.count(decimal) != 1 or t.rfind(milhar) > t.rfind(decimal):
            return None, f"O número '{texto}' tem separadores em ordem inválida."
        t = t.replace(milhar, "").replace(decimal, ".")
    elif pontos or virgulas:
        sep = "." if pontos else ","
        if t.count(sep) > 1:
            grupos = t.split(sep)
            if not (1 <= len(grupos[0]) <= 3 and all(len(g) == 3 for g in grupos[1:])):
                return None, f"O número '{texto}' tem separadores de milhar inválidos."
            t = "".join(grupos)
        else:
            inteiro, fracao = t.split(sep)
            if len(fracao) == 3 and inteiro.strip("0") != "":
                return None, (
                    f"O número '{texto}' é ambíguo: o separador pode ser decimal ou de milhar. "
                    "Não foi interpretado."
                )
            t = f"{inteiro}.{fracao}"
    try:
        numero = Decimal(t)
    except InvalidOperation:
        return None, f"O valor '{texto}' não é um número."
    return (numero, "") if numero.is_finite() else (None, f"O valor '{texto}' não é um número.")


def _texto(valor, limite: int = 100) -> str:
    if valor is None:
        return ""
    if isinstance(valor, bool):
        texto = "true" if valor else "false"
    elif isinstance(valor, (dict, list)):
        texto = json.dumps(valor, ensure_ascii=False)
    else:
        texto = str(valor)
    return texto.strip()[:limite]


def _como_data(valor) -> datetime | None:
    """ISO 8601 (`2026-11-01T00:00:00Z`) ou só a data (`2026-11-01`, meia-noite local)."""
    if not isinstance(valor, str) or not valor.strip():
        return None
    texto = valor.strip()
    try:
        momento = parse_datetime(texto)
        if momento is None:
            dia = parse_date(texto)
            if dia is None:
                return None
            momento = datetime.combine(dia, hora_do_dia.min)
    except ValueError:
        return None
    return timezone.make_aware(momento) if timezone.is_naive(momento) else momento


# ─────────────────────────────────────────────────────────────
# Validação do mapeamento
# ─────────────────────────────────────────────────────────────

def _lista_de_textos(valor, rotulo: str, erros: list[str], maximo: int = 50) -> list[str]:
    if valor in (None, ""):
        return []
    if isinstance(valor, str):
        valor = [parte for parte in re.split(r"[\n,;]", valor)]
    if not isinstance(valor, (list, tuple)):
        erros.append(f"{rotulo}: informe uma lista de valores.")
        return []
    itens: list[str] = []
    for item in valor:
        texto = _texto(item, 100)
        if texto and texto not in itens:
            itens.append(texto)
    if len(itens) > maximo:
        erros.append(f"{rotulo}: no máximo {maximo} valores.")
        return itens[:maximo]
    return itens


def _caminho_opcional(valor, rotulo: str, erros: list[str]) -> str:
    if valor in (None, ""):
        return ""
    if not isinstance(valor, str):
        erros.append(f"{rotulo}: informe o caminho do campo (ex.: dados.estado).")
        return ""
    texto = valor.strip()
    if not texto:
        return ""
    if len(texto) > 200 or not caminho_valido(texto):
        erros.append(f"{rotulo}: caminho inválido (use o formato a.b[0].c).")
        return ""
    return texto


def _lista_de_caminhos(valor, rotulo: str, erros: list[str], maximo: int = 20) -> list[str]:
    itens = _lista_de_textos(valor, rotulo, erros, maximo=maximo)
    caminhos_ok = []
    for item in itens:
        if len(item) > 200 or not caminho_valido(item):
            erros.append(f"{rotulo}: caminho inválido {item!r} (use o formato a.b[0].c).")
        else:
            caminhos_ok.append(item)
    return caminhos_ok


def validar_mapeamento(bruto) -> dict:
    """Valida e NORMALIZA o mapeamento (só chaves conhecidas, só o que foi
    preenchido). Levanta `ValidationError` com todas as mensagens em pt-BR de
    uma vez. `None`/`{}` = sem mapeamento (nada será interpretado)."""
    if bruto in (None, "", {}):
        return {}
    if not isinstance(bruto, dict):
        raise ValidationError("O mapeamento precisa ser um objeto com as seções sessao, consumo e identidade.")
    erros: list[str] = []
    saida: dict = {}

    s = bruto.get("sessao") or {}
    if not isinstance(s, dict):
        erros.append("Sessão: formato inválido.")
        s = {}
    sessao = {
        "campo_estado": _caminho_opcional(s.get("campo_estado"), "Sessão, campo do estado", erros),
        "valores_ativa": _lista_de_textos(s.get("valores_ativa"), "Sessão, valores de 'sessão ativa'", erros),
        "valores_sem_sessao": _lista_de_textos(s.get("valores_sem_sessao"), "Sessão, valores de 'sem sessão'", erros),
        "campos_exibir": _lista_de_caminhos(s.get("campos_exibir"), "Sessão, campos a exibir", erros),
    }
    comuns = {v.casefold() for v in sessao["valores_ativa"]} & {v.casefold() for v in sessao["valores_sem_sessao"]}
    if comuns:
        erros.append(f"Sessão: o valor {sorted(comuns)[0]!r} aparece nas duas listas (ativa e sem sessão).")
    if (sessao["valores_ativa"] or sessao["valores_sem_sessao"]) and not sessao["campo_estado"]:
        erros.append("Sessão: informe o campo do estado para usar os valores de 'ativa' e 'sem sessão'.")
    sessao = {k: v for k, v in sessao.items() if v}
    if sessao:
        saida["sessao"] = sessao

    c = bruto.get("consumo") or {}
    if not isinstance(c, dict):
        erros.append("Consumo: formato inválido.")
        c = {}
    consumo = {
        "campo_volume": _caminho_opcional(c.get("campo_volume"), "Consumo, campo do volume", erros),
        "campo_unidade": _caminho_opcional(c.get("campo_unidade"), "Consumo, campo da unidade", erros),
        "unidade_fixa": _texto(c.get("unidade_fixa"), 40),
        # A API entrega o volume como TEXTO com a unidade colada ("0,00 KB", "2.47 MB").
        "unidade_no_valor": str(c.get("unidade_no_valor") or "").strip().lower() in ("1", "true", "on", "sim"),
        "campo_periodo_inicio": _caminho_opcional(c.get("campo_periodo_inicio"), "Consumo, início do período", erros),
        "campo_periodo_fim": _caminho_opcional(c.get("campo_periodo_fim"), "Consumo, fim do período", erros),
    }
    if consumo["unidade_no_valor"] and (consumo["campo_unidade"] or consumo["unidade_fixa"]):
        erros.append("Consumo: com a unidade dentro do valor, não informe campo da unidade nem unidade fixa.")
    base_informada = c.get("base")
    try:
        base = int(base_informada) if base_informada not in (None, "") else 1000
    except (TypeError, ValueError):
        base = 0
    if base not in BASES_VALIDAS:
        erros.append("Consumo: a base de conversão deve ser 1000 ou 1024.")
        base = 1000
    if consumo["campo_unidade"] and consumo["unidade_fixa"]:
        erros.append("Consumo: informe o campo da unidade OU uma unidade fixa, não os dois.")
    if consumo["unidade_fixa"] and not unidade_conhecida(consumo["unidade_fixa"]):
        erros.append(
            "Consumo: unidade fixa não reconhecida. Use B, KB, MB, GB, TB (ou KiB, MiB, GiB, TiB); "
            "unidades em bits não são aceitas."
        )
    if (consumo["campo_unidade"] or consumo["unidade_fixa"] or consumo["unidade_no_valor"]) and not consumo["campo_volume"]:
        erros.append("Consumo: informe o campo do volume.")
    consumo = {k: v for k, v in consumo.items() if v}
    if consumo:
        consumo["base"] = base
        saida["consumo"] = consumo

    i = bruto.get("identidade") or {}
    if not isinstance(i, dict):
        erros.append("Identidade: formato inválido.")
        i = {}
    identidade = {
        "campo_iccid": _caminho_opcional(i.get("campo_iccid"), "Identidade, campo do ICCID", erros),
        "campo_imsi": _caminho_opcional(i.get("campo_imsi"), "Identidade, campo do IMSI", erros),
    }
    identidade = {k: v for k, v in identidade.items() if v}
    if identidade:
        saida["identidade"] = identidade

    if erros:
        raise ValidationError(erros)
    return saida


# ─────────────────────────────────────────────────────────────
# Interpretação
# ─────────────────────────────────────────────────────────────

@dataclass
class ResultadoSessao:
    estado: str = NAO_INTERPRETADO          # ativa | sem_sessao | nao_interpretado
    estado_bruto: str = ""                  # o valor, como a API mandou
    motivo: str = ""                        # por que não foi interpretado ("" quando foi)
    campos_exibir: list = field(default_factory=list)   # [{"caminho", "valor"}]


@dataclass
class ResultadoConsumo:
    valor_original: str = ""
    unidade_original: str = ""
    volume_bytes: int | None = None
    periodo_inicio: datetime | None = None
    periodo_fim: datetime | None = None
    motivo: str = ""


def _local(momento) -> str:
    """`2026-11-13T21:47:22-03:00` -> `13/11/2026 21:47` (no fuso do sistema); vazio se não for data."""
    achado = _como_data(momento)
    return timezone.localtime(achado).strftime("%d/%m/%Y %H:%M") if achado else ""


def _sessao_viva(sessao: dict, consultado_em) -> bool:
    """A sessão aberta (`fim` vazio) ainda anda? Vale o último avanço que a plataforma informou
    (`inicio` + `duracao_s`) contra o momento da leitura — ver `LIMITE_SESSAO_PARADA_S`."""
    inicio = _como_data(sessao.get("inicio"))
    if inicio is None:
        return False
    if consultado_em is None:
        return True
    try:
        avancou_ate = inicio + timedelta(seconds=int(sessao.get("duracao_s") or 0))
    except (TypeError, ValueError):
        return False
    return (consultado_em - avancou_ate).total_seconds() <= LIMITE_SESSAO_PARADA_S


def _aplicar_eventos(r: ResultadoSessao, eventos: dict) -> None:
    """Corrige o estado do RESUMO com o que as sessões de dados dizem (o resumo do SIM Card
    seguiu em `nunca_conectou` por mais de 20 min com o chip online e 15 MB consumidos):

      • há sessão aberta e viva  -> ATIVA ("online"), venha o que vier do resumo;
      • senão, o resumo já dizia ATIVA -> continua (a plataforma também chama de online);
      • senão, o chip já teve sessão -> SEM_SESSAO, "offline" (o resumo ainda podia dizer
        `nunca_conectou`, que aí seria mentira);
      • nenhuma sessão lida -> fica como o resumo disse."""
    consultado = _como_data(eventos.get("consultado_em"))
    vivas = [a for a in eventos.get("abertas") or [] if isinstance(a, dict) and _sessao_viva(a, consultado)]
    if vivas:
        atual = max(vivas, key=lambda a: str(a.get("inicio") or ""))
        r.estado, r.estado_bruto, r.motivo = ATIVA, "online", ""
        extras = [{"caminho": "sessao_aberta_desde", "valor": _local(atual.get("inicio"))}]
        for chave, rotulo in (("ip", "ip_da_sessao"), ("apn", "apn_da_sessao")):
            if atual.get(chave):
                extras.append({"caminho": rotulo, "valor": _texto(atual[chave], 200)})
        r.campos_exibir = extras + r.campos_exibir
        return
    if r.estado == ATIVA:
        return
    ultima = eventos.get("ultima")
    if isinstance(ultima, dict):
        r.estado, r.estado_bruto, r.motivo = SEM_SESSAO, "offline", ""
        quando = _local(ultima.get("fim")) or _local(ultima.get("inicio"))
        if quando:
            r.campos_exibir = [{"caminho": "ultima_sessao_ate", "valor": quando}] + r.campos_exibir


def interpretar_sessao(payload, mapeamento) -> ResultadoSessao:
    """Estado da sessão: o do resumo (`mapeamento["sessao"]`) corrigido pelas sessões de dados da
    plataforma quando a resposta as traz (`_eventos`, ver `_aplicar_eventos`)."""
    r = _sessao_do_resumo(payload, mapeamento)
    eventos = payload.get("_eventos") if isinstance(payload, dict) else None
    if isinstance(eventos, dict):
        _aplicar_eventos(r, eventos)
    return r


def _sessao_do_resumo(payload, mapeamento) -> ResultadoSessao:
    """Estado da sessão segundo `mapeamento["sessao"]`. Valor fora das duas listas
    (ou campo ausente, ou sem mapeamento) = `nao_interpretado`; o valor bruto é
    devolvido para a tela mostrá-lo."""
    cfg = (mapeamento or {}).get("sessao") or {}
    r = ResultadoSessao()
    if payload is None:
        r.motivo = "A resposta não trouxe conteúdo para interpretar."
        return r
    for caminho in cfg.get("campos_exibir") or []:
        valor = valor_em(payload, caminho)
        if valor is not None:
            r.campos_exibir.append({"caminho": caminho, "valor": _texto(valor, 200)})
    campo = cfg.get("campo_estado")
    if not campo:
        r.motivo = "Sem mapeamento do estado da sessão (Configuração da integração)."
        return r
    bruto = valor_em(payload, campo)
    if bruto is None or isinstance(bruto, (dict, list)):
        r.motivo = f"O campo '{campo}' não existe na resposta (ou não é um valor simples)."
        return r
    r.estado_bruto = _texto(bruto)
    chave = r.estado_bruto.casefold()
    ativa = {v.casefold() for v in cfg.get("valores_ativa") or []}
    sem = {v.casefold() for v in cfg.get("valores_sem_sessao") or []}
    if chave in ativa and chave not in sem:
        r.estado = ATIVA
    elif chave in sem and chave not in ativa:
        r.estado = SEM_SESSAO
    else:
        r.motivo = f"O valor '{r.estado_bruto}' não está em nenhuma das listas do mapeamento."
    return r


def interpretar_consumo(payload, mapeamento) -> ResultadoConsumo:
    """Volume segundo `mapeamento["consumo"]`. O valor e a unidade ORIGINAIS são
    guardados sempre que o campo existe; `volume_bytes` só quando o valor é um
    número simples e a unidade é conhecida."""
    cfg = (mapeamento or {}).get("consumo") or {}
    r = ResultadoConsumo()
    if payload is None:
        r.motivo = "A resposta não trouxe conteúdo para interpretar."
        return r
    periodo = payload.get("_dados_periodo") if isinstance(payload, dict) else None
    if isinstance(periodo, dict) and isinstance(periodo.get("total_bytes"), int) and not isinstance(periodo["total_bytes"], bool):
        # Soma exata, em bytes, das sessões de dados do período (do dia 1 até a leitura): não há
        # texto nem unidade a interpretar, e por isso o volume não depende de mapeamento nenhum.
        if 0 <= periodo["total_bytes"] <= MAX_BIGINT:
            r.valor_original, r.unidade_original = str(periodo["total_bytes"]), "B"
            r.volume_bytes = periodo["total_bytes"]
            r.periodo_inicio = _como_data(periodo.get("inicio"))
            r.periodo_fim = _como_data(periodo.get("fim"))
            return r
    campo = cfg.get("campo_volume")
    if not campo:
        r.motivo = "Sem mapeamento do volume de consumo (Configuração da integração)."
        return r
    bruto = valor_em(payload, campo)
    if bruto is None or isinstance(bruto, (dict, list)):
        r.motivo = f"O campo '{campo}' não existe na resposta (ou não é um valor simples)."
        return r
    r.valor_original = _texto(bruto)
    r.periodo_inicio = _como_data(valor_em(payload, cfg.get("campo_periodo_inicio") or ""))
    r.periodo_fim = _como_data(valor_em(payload, cfg.get("campo_periodo_fim") or ""))

    if cfg.get("unidade_no_valor"):
        # Volume em texto com a unidade colada ("0,00 KB", "2.47 MB"): o número e a
        # unidade saem do MESMO campo. O valor original continua guardado inteiro.
        achado = _VOLUME_COM_UNIDADE.fullmatch(bruto) if isinstance(bruto, str) else None
        if not achado:
            r.motivo = f"O valor '{r.valor_original}' não está no formato 'número unidade' (por exemplo, 2,47 MB)."
            return r
        r.unidade_original = _texto(achado.group(2), 40)
        numero, motivo = _numero_formatado(achado.group(1))
        if numero is None:
            r.motivo = motivo
            return r
    else:
        if cfg.get("campo_unidade"):
            r.unidade_original = _texto(valor_em(payload, cfg["campo_unidade"]), 40)
        else:
            r.unidade_original = _texto(cfg.get("unidade_fixa"), 40)
        numero = _decimal(bruto)
        if numero is None:
            r.motivo = f"O valor '{r.valor_original}' não é um número simples."
            return r
    fator = fator_unidade(r.unidade_original, cfg.get("base") or 1000)
    if fator is None:
        r.motivo = (
            f"Unidade '{r.unidade_original}' não reconhecida." if r.unidade_original
            else "Sem unidade mapeada (campo da unidade ou unidade fixa)."
        )
        return r
    bytes_ = int((numero * fator).to_integral_value(rounding=ROUND_HALF_UP))
    if bytes_ > MAX_BIGINT:
        r.motivo = "O volume informado é grande demais para ser normalizado."
        return r
    r.volume_bytes = bytes_
    return r


def identidade_do_payload(payload, mapeamento) -> dict:
    """`{"iccid": "...", "imsi": "..."}` (só dígitos; vazio se o campo não está
    mapeado ou não veio) — alimenta o alerta de identificador divergente."""
    cfg = (mapeamento or {}).get("identidade") or {}
    saida = {"iccid": "", "imsi": ""}
    for chave, campo in (("iccid", "campo_iccid"), ("imsi", "campo_imsi")):
        if cfg.get(campo):
            saida[chave] = re.sub(r"\D", "", _texto(valor_em(payload, cfg[campo]), 40))
    return saida


def campos_normalizados(tipo: str, payload, mapeamento, *, valida: bool = True) -> dict:
    """Os campos normalizados de uma `M2MConsulta` (todos vazios quando a consulta
    não é válida). É a ÚNICA porta de cálculo: a atualização e o
    "reinterpretar histórico" usam esta função, então não divergem."""
    campos = dict(
        valor_original="", unidade_original="", volume_bytes=None,
        periodo_inicio=None, periodo_fim=None, estado_bruto="", estado_interpretado="",
    )
    if not valida:
        return campos
    if tipo == "sessao":
        r = interpretar_sessao(payload, mapeamento)
        campos.update(estado_bruto=r.estado_bruto[:100], estado_interpretado=r.estado)
    elif tipo == "consumo":
        r = interpretar_consumo(payload, mapeamento)
        campos.update(
            valor_original=r.valor_original[:100], unidade_original=r.unidade_original[:40],
            volume_bytes=r.volume_bytes, periodo_inicio=r.periodo_inicio, periodo_fim=r.periodo_fim,
        )
    return campos


def previa(payload, mapeamento) -> dict:
    """Prévia ao vivo, serializável em JSON, do que o mapeamento (ainda não salvo)
    interpretaria neste payload — para a tela de configuração:
    `{"erros": [...]}` se o mapeamento é inválido; senão
    `{"sessao": {...}, "consumo": {...}, "identidade": {...}}`."""
    try:
        mapa = validar_mapeamento(mapeamento)
    except ValidationError as exc:
        return {"erros": list(exc.messages)}
    sessao = interpretar_sessao(payload, mapa)
    consumo = interpretar_consumo(payload, mapa)
    base = (mapa.get("consumo") or {}).get("base") or 1000
    return {
        "erros": [],
        "sessao": {
            "estado": sessao.estado, "estado_bruto": sessao.estado_bruto,
            "motivo": sessao.motivo, "campos_exibir": sessao.campos_exibir,
        },
        "consumo": {
            "valor_original": consumo.valor_original, "unidade_original": consumo.unidade_original,
            "volume_bytes": consumo.volume_bytes,
            "volume_formatado": formatar_bytes(consumo.volume_bytes, base),
            "periodo_inicio": consumo.periodo_inicio.isoformat() if consumo.periodo_inicio else None,
            "periodo_fim": consumo.periodo_fim.isoformat() if consumo.periodo_fim else None,
            "motivo": consumo.motivo,
        },
        "identidade": identidade_do_payload(payload, mapa),
    }
