"""
Sessões de dados do chip — o que a aba "Dados" do portal da Saitro mostra.

POR QUE EXISTE. O resumo do SIM Card (`GET /simcards/{id}/`: `conexao`, `ultimo_acesso`,
`servicos.dados.consumo`) NÃO acompanha o uso: em 13/11/2026, 23 minutos depois de o primeiro chip
conectar e consumir 15 MB, ele seguia `nunca_conectou` / `0,00 KB`, enquanto o portal mostrava
"Online". O que o portal mostra vem de outro endpoint — as sessões de dados
(`GET /simcards/{id}/consumo/dados/{AAAA-MM-DD}/`) —, que bate com ele evento a evento. Este módulo lê
esse endpoint, guarda cada sessão em `M2MSessaoDados` e entrega à interpretação:

  • `_eventos`        — as sessões abertas, a última sessão e o momento da leitura. Sessão aberta
                        (`dt_fim` vazio) = "online", como no portal;
  • `_dados_periodo`  — a soma EXATA, em bytes, das sessões do mês (do dia 1 até a leitura).

Os dois entram na resposta que vai para `M2MConsulta` (o payload continua sendo "o que a plataforma
disse naquele momento"), e quem os lê é `m2m_interpretacao` — por isso reinterpretar o histórico,
a prévia da Configuração e o resto do módulo seguem funcionando sem saber que a fonte mudou.

COMO LÊ SEM ESTOURAR O LIMITE DE REQUISIÇÕES. O endpoint é POR DIA, então:
  • em regime, uma leitura por chip: o dia de hoje (+ ontem na virada do dia, e o dia de qualquer sessão
    ainda aberta que começou antes, para a plataforma poder fechá-la);
  • o histórico do mês é baixado aos poucos (`DIAS_DE_HISTORICO_POR_LEITURA` dias por leitura) até
    cobrir o dia 1. `M2MChip.dados_desde` diz até onde já cobriu; enquanto não chega ao dia 1 o
    consumo do mês sai MENOR que o do portal, e `_dados_periodo["completo"]` avisa.

REGRAS QUE NÃO SÃO ÓBVIAS.
  • A leitura de HOJE falhar é falha da consulta inteira (o estado "sem sessão" sairia do resumo
    atrasado, que é justamente o que mente); falhar um dia ANTIGO só deixa o histórico para a
    próxima leitura.
  • `dados_desde` só recua por dias CONSECUTIVOS lidos com sucesso — um buraco no meio não pode
    virar "histórico completo".
  • Uma sessão aberta cuja duração a plataforma não avança há mais de 2 h não conta como online
    (`m2m_interpretacao.LIMITE_SESSAO_PARADA_S`).
  • Dia sem sessão é dia lido (`{"msg": "sem_historico_no_periodo"}`), não falha.
  • A HTTP nunca roda dentro de transação: as sessões lidas são gravadas de uma vez, no fim.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time as hora_do_dia, timedelta
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Count, Sum
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from ProjetoEstoque.models import M2MChip, M2MSessaoDados

logger = logging.getLogger(__name__)

DIAS_DE_HISTORICO_POR_LEITURA = 3      # dias do mês baixados, além dos de regime, a cada leitura
MAX_DIAS_LACUNA = 7                    # quantos dias para trás a leitura recupera se o sistema ficou parado
SESSOES_NA_TELA = 40                   # linhas da tabela do detalhe do chip
MAX_INT = 2 ** 62


# ─────────────────────────────────────────────────────────────
# Leitura do que a plataforma devolve (funções puras)
# ─────────────────────────────────────────────────────────────

def _momento(valor) -> datetime | None:
    """`dt_inicio` vem como `{"date": "2026-11-13 21:46:47.000000", "timezone": "America/Sao_Paulo"}` e
    `dt_fim` como `"2026-11-13 21:47:16-03"` (ou `null` com a sessão em andamento). Sem fuso no texto,
    vale o `timezone` informado e, na falta dele, o do sistema."""
    zona = None
    if isinstance(valor, dict):
        zona, valor = valor.get("timezone"), valor.get("date")
    if not isinstance(valor, str) or not valor.strip():
        return None
    try:
        momento = parse_datetime(valor.strip())
    except ValueError:
        return None
    if momento is None:
        return None
    if timezone.is_naive(momento):
        try:
            fuso = ZoneInfo(zona) if zona else timezone.get_current_timezone()
        except Exception:  # noqa: BLE001 — zona desconhecida: cai no fuso do sistema
            fuso = timezone.get_current_timezone()
        momento = momento.replace(tzinfo=fuso)
    return momento


def _inteiro(valor) -> int | None:
    if isinstance(valor, bool):
        return None
    try:
        n = int(valor)
    except (TypeError, ValueError):
        return None
    return n if 0 <= n <= MAX_INT else None


def _texto(valor, limite: int) -> str:
    return str(valor).strip()[:limite] if valor not in (None, "") else ""


def ler_evento(objeto) -> dict | None:
    """Uma sessão da plataforma como os campos de `M2MSessaoDados`; `None` se não dá para confiar nela
    (sem id, sem início). Os bytes saem de `upload`/`download`/`total`; sem `total`, vale a soma."""
    if not isinstance(objeto, dict):
        return None
    id_evento = _inteiro(objeto.get("id_evento"))
    inicio = _momento(objeto.get("dt_inicio"))
    if id_evento is None or inicio is None:
        return None
    upload, download = _inteiro(objeto.get("upload")) or 0, _inteiro(objeto.get("download")) or 0
    total = _inteiro(objeto.get("total"))
    return {
        "id_evento": id_evento,
        "inicio": inicio,
        "fim": _momento(objeto.get("dt_fim")),
        "duracao_s": min(_inteiro(objeto.get("duracao_segundos")) or 0, 2 ** 31 - 1),
        "ip": _texto(objeto.get("ip"), 45),
        "apn": _texto(objeto.get("apn"), 100),
        "tecnologia": _texto(objeto.get("tecnologia"), 30),
        "location_info": _texto(objeto.get("location_info"), 100),
        "upload_bytes": upload,
        "download_bytes": download,
        "total_bytes": total if total is not None else upload + download,
    }


# ─────────────────────────────────────────────────────────────
# Quais dias ler
# ─────────────────────────────────────────────────────────────

def _inicio_do_dia(dia: date) -> datetime:
    return timezone.make_aware(datetime.combine(dia, hora_do_dia.min))


def dias_a_ler(chip: M2MChip, hoje: date) -> list[date]:
    """Os dias desta leitura, do mais novo para o mais antigo: hoje; os que ficaram para trás desde a
    última leitura (a virada do dia, ou o sistema parado — até `MAX_DIAS_LACUNA`); o dia de qualquer
    sessão que ficou aberta antes de hoje (só relendo é que a plataforma a mostra fechada); e, por
    fim, até `DIAS_DE_HISTORICO_POR_LEITURA` dias do mês ainda não baixados."""
    dias = [hoje]
    if chip.dados_sincronizados_em is not None:
        ultima = timezone.localdate(chip.dados_sincronizados_em)
        for k in range(1, min((hoje - ultima).days, MAX_DIAS_LACUNA) + 1):
            dias.append(hoje - timedelta(days=k))
    abertas = chip.sessoes_dados.filter(fim__isnull=True, inicio__lt=_inicio_do_dia(hoje)).values_list("inicio", flat=True)
    for inicio in abertas:
        dia = timezone.localdate(inicio)
        if dia not in dias and (hoje - dia).days <= MAX_DIAS_LACUNA:
            dias.append(dia)
    desde = chip.dados_desde
    if desde is not None and chip.dados_sincronizados_em is not None:
        if (hoje - timezone.localdate(chip.dados_sincronizados_em)).days > MAX_DIAS_LACUNA:
            desde = None          # buraco que a leitura não alcança: a cobertura recomeça de hoje
    ponta = min([d for d in dias] + ([desde] if desde else []))
    mes = hoje.replace(day=1)
    anterior = ponta - timedelta(days=1)
    historico = []
    while anterior >= mes and len(historico) < DIAS_DE_HISTORICO_POR_LEITURA:
        historico.append(anterior)
        anterior -= timedelta(days=1)
    return sorted(set(dias), reverse=True) + [d for d in historico if d not in dias]


# ─────────────────────────────────────────────────────────────
# Sincronização
# ─────────────────────────────────────────────────────────────

@dataclass
class ResultadoDados:
    """O que uma leitura de sessões de dados fez. `ok=False`: a leitura de HOJE falhou e `falha` é a
    resposta classificada (a atualização inteira falha com ela). `sessao` e `periodo` são os
    complementos do payload (`_eventos` / `_dados_periodo`)."""
    ok: bool = True
    falha: object = None
    sessao: dict = field(default_factory=dict)
    periodo: dict = field(default_factory=dict)
    dias_lidos: list = field(default_factory=list)
    novas: int = 0
    atualizadas: int = 0
    avisos: list = field(default_factory=list)


def usar_eventos(cfg, cliente) -> bool:
    """Lê as sessões de dados? Só com a opção ligada e um cliente que saiba lê-las."""
    return bool(getattr(cfg, "dados_por_eventos", False)) and callable(getattr(cliente, "consultar_dia", None))


def sincronizar(chip: M2MChip, cliente, origem: str, *, agora=None) -> ResultadoDados:
    """Lê os dias de `dias_a_ler`, grava as sessões (`id_evento` já existente só é atualizada) e devolve os
    complementos do payload. As chamadas HTTP vêm antes e a gravação é uma transação só no fim."""
    agora = agora or timezone.now()
    hoje = timezone.localdate(agora)
    resultado = ResultadoDados()
    lidos: dict[date, list[dict]] = {}
    for dia in dias_a_ler(chip, hoje):
        resposta = cliente.consultar_dia(chip, dia, origem)
        if not resposta.valida:
            if dia == hoje or resposta.interrompe_ciclo:
                resultado.ok, resultado.falha = False, resposta
                return resultado
            resultado.avisos.append(f"{dia:%d/%m}: {resposta.mensagem_erro or resposta.resultado}")
            continue
        payload = resposta.payload if isinstance(resposta.payload, dict) else {}
        eventos = [e for e in (ler_evento(o) for o in payload.get("objetos") or []) if e]
        descartadas = len(payload.get("objetos") or []) - len(eventos)
        if descartadas:
            resultado.avisos.append(f"{dia:%d/%m}: {descartadas} sessão(ões) ignorada(s) por falta de id ou de início")
        if payload.get("completa") is False:
            resultado.avisos.append(f"{dia:%d/%m}: a lista de sessões veio incompleta")
        lidos[dia] = eventos
    resultado.dias_lidos = sorted(lidos, reverse=True)

    with transaction.atomic():
        novas, atualizadas = _gravar(chip, [e for dia in lidos for e in lidos[dia]], agora)
        resultado.novas, resultado.atualizadas = novas, atualizadas
        desde = _cobertura(chip, hoje, set(lidos))
        M2MChip.objects.filter(pk=chip.pk).update(dados_desde=desde, dados_sincronizados_em=agora)
        chip.dados_desde, chip.dados_sincronizados_em = desde, agora
        resultado.sessao = _resumo_da_sessao(chip, agora, resultado.dias_lidos)
        resultado.periodo = _resumo_do_periodo(chip, agora, hoje)
    logger.info(
        "TIM M2M: chip %s, sessões de dados: %s nova(s), %s atualizada(s), dias %s",
        chip.pk, novas, atualizadas, ", ".join(f"{d:%d/%m}" for d in resultado.dias_lidos),
    )
    return resultado


def _gravar(chip: M2MChip, eventos: list[dict], agora) -> tuple[int, int]:
    if not eventos:
        return 0, 0
    existentes = {s.id_evento: s for s in M2MSessaoDados.objects.filter(id_evento__in=[e["id_evento"] for e in eventos])}
    novas, atualizadas, vistos = [], 0, set()
    for e in eventos:
        if e["id_evento"] in vistos:
            continue
        vistos.add(e["id_evento"])
        atual = existentes.get(e["id_evento"])
        if atual is None:
            novas.append(M2MSessaoDados(chip=chip, lido_em=agora, **e))
        elif atual.chip_id == chip.pk:
            mudou = [campo for campo, valor in e.items() if getattr(atual, campo) != valor]
            if mudou:
                for campo in mudou:
                    setattr(atual, campo, e[campo])
                atual.lido_em = agora
                atual.save(update_fields=[*mudou, "lido_em"])
                atualizadas += 1
        else:
            logger.warning("TIM M2M: o evento %s já pertence a outro chip (%s); ignorado.", e["id_evento"], atual.chip_id)
    M2MSessaoDados.objects.bulk_create(novas, ignore_conflicts=True)
    return len(novas), atualizadas


def _cobertura(chip: M2MChip, hoje: date, lidos: set) -> date:
    """O dia mais antigo a partir do qual TODOS os dias até hoje já foram lidos. Junta o que esta leitura
    trouxe com o que já estava coberto e recua de hoje enquanto não houver buraco: um dia que falhou no
    meio (antigo ou recente) para a contagem nele, em vez de virar "histórico completo"."""
    cobertos = set(lidos)
    if chip.dados_desde and chip.dados_sincronizados_em:
        ultima = timezone.localdate(chip.dados_sincronizados_em)
        if (hoje - ultima).days <= MAX_DIAS_LACUNA:
            dia = chip.dados_desde
            while dia <= ultima:
                cobertos.add(dia)
                dia += timedelta(days=1)
    desde = hoje
    while desde - timedelta(days=1) in cobertos:
        desde -= timedelta(days=1)
    return desde


# ─────────────────────────────────────────────────────────────
# Complementos do payload (o que a interpretação lê)
# ─────────────────────────────────────────────────────────────

def _iso(momento) -> str | None:
    return momento.isoformat() if momento else None


def _resumo_da_sessao(chip: M2MChip, agora, dias: list) -> dict:
    abertas = list(chip.sessoes_dados.filter(fim__isnull=True).order_by("-inicio")[:5])
    ultima = chip.sessoes_dados.order_by("-inicio", "-id_evento").first()

    def linha(s):
        return {
            "id": s.id_evento, "inicio": _iso(s.inicio), "fim": _iso(s.fim), "duracao_s": s.duracao_s,
            "total_bytes": s.total_bytes, "ip": s.ip, "apn": s.apn,
        }

    return {
        "fonte": "sessoes_de_dados", "consultado_em": _iso(agora), "dias_lidos": [f"{d:%Y-%m-%d}" for d in dias],
        "abertas": [linha(s) for s in abertas], "ultima": linha(ultima) if ultima else None,
    }


def _resumo_do_periodo(chip: M2MChip, agora, hoje: date) -> dict:
    inicio = _inicio_do_dia(hoje.replace(day=1))
    soma = chip.sessoes_dados.filter(inicio__gte=inicio).aggregate(
        total=Sum("total_bytes"), upload=Sum("upload_bytes"), download=Sum("download_bytes"), sessoes=Count("pk"),
    )
    return {
        "fonte": "sessoes_de_dados", "inicio": _iso(inicio), "fim": _iso(agora),
        "total_bytes": int(soma["total"] or 0), "upload_bytes": int(soma["upload"] or 0),
        "download_bytes": int(soma["download"] or 0), "sessoes": soma["sessoes"],
        "completo": bool(chip.dados_desde and chip.dados_desde <= hoje.replace(day=1)),
        "dados_desde": chip.dados_desde.isoformat() if chip.dados_desde else None,
    }


def enriquecer(resposta, tipo: str, dados: ResultadoDados):
    """A resposta do resumo do SIM Card com o que as sessões de dados trouxeram. Se a leitura delas falhou,
    a consulta INTEIRA falha com o motivo dela: aceitar só o resumo faria o chip online aparecer como
    "sem sessão" — e o último dado válido do chip não é tocado por falha (regra de `atualizar`)."""
    if not dados.ok:
        falha = dados.falha
        return replace(
            resposta, resultado=falha.resultado, http_status=falha.http_status, payload=None,
            tentativas=falha.tentativas, retry_after_s=falha.retry_after_s,
            mensagem_erro=f"Não foi possível ler as sessões de dados do chip na plataforma. {falha.mensagem_erro}".strip(),
        )
    payload = dict(resposta.payload) if isinstance(resposta.payload, dict) else {}
    if tipo == "sessao":
        payload["_eventos"] = dados.sessao
    else:
        payload["_dados_periodo"] = dados.periodo
    return replace(resposta, payload=payload)


# ─────────────────────────────────────────────────────────────
# Leitura para a tela
# ─────────────────────────────────────────────────────────────

def painel_do_chip(chip: M2MChip, agora=None, limite: int = SESSOES_NA_TELA) -> dict:
    """O que o detalhe do chip mostra, no molde do portal: as últimas sessões, os totais do mês e até onde o
    histórico já foi baixado. `sessoes` traz `ao_vivo` (sessão aberta e ainda viva) já resolvido."""
    from services import m2m_interpretacao as interp

    agora = agora or timezone.now()
    hoje = timezone.localdate(agora)
    mes = _inicio_do_dia(hoje.replace(day=1))
    sessoes = list(chip.sessoes_dados.order_by("-inicio", "-id_evento")[:limite])
    for s in sessoes:
        s.ao_vivo = s.fim is None and interp._sessao_viva({"inicio": _iso(s.inicio), "duracao_s": s.duracao_s}, agora)
    soma = chip.sessoes_dados.filter(inicio__gte=mes).aggregate(
        total=Sum("total_bytes"), upload=Sum("upload_bytes"), download=Sum("download_bytes"),
        duracao=Sum("duracao_s"), n=Count("pk"),
    )
    return {
        "sessoes": sessoes,
        "tem_sessoes": bool(sessoes),
        "mes_inicio": mes,
        "mes": {k: int(soma[k] or 0) for k in ("total", "upload", "download", "duracao", "n")},
        "dados_desde": chip.dados_desde,
        "sincronizado_em": chip.dados_sincronizados_em,
        "completo": bool(chip.dados_desde and chip.dados_desde <= hoje.replace(day=1)),
    }
