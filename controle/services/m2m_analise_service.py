"""
A análise dos chips M2M: utilização, consumo e inatividade — os três gráficos do dashboard de Chips e Conectividade.

Cada número sai de dado que o sistema TEM: o cadastro (vínculo chip↔equipamento), as sessões de dados lidas na
plataforma da TIM (`M2MSessaoDados`) e o histórico de consultas (`M2MConsulta`). Sem dado, o resultado diz "sem
informação" — nunca um zero. As regras que não são óbvias:

  • UTILIZAÇÃO É VÍNCULO, NÃO RESPOSTA. "Em uso" é o chip com vínculo ativo num equipamento em operação
    (`m2m_monitoramento_service.q_uso`). Ele ter respondido à API, ter sessão ou ter consumo não o torna "em uso": um SIM
    de reserva dentro de um aparelho sem cadastro de vínculo consome dado e continua "não utilizado" — e isso é um
    achado, não um erro do gráfico. Inativo, em manutenção, monitoramento desativado e operadora sem integração ficam
    SEPARADOS e fora do percentual.
  • O PERÍODO É O QUE FOI LIDO. Cada janela diz de quando a quando; o "período da TIM" é o início que a própria TIM
    informou na consulta de consumo (a maioria dos chips divide o mesmo; quem veio com outro não entra na comparação).
    Janela só é comparável entre chips que tenham o histórico lido por INTEIRO nela (`dados_desde`): um chip lido a
    partir do dia 1 não tem "30 dias", e comparar a soma dele com a de um chip lido por 30 dias é comparar períodos
    diferentes como se fossem iguais.
  • ZERO É UMA AFIRMAÇÃO. "Consumo zero" só com o histórico da janela completo, dados recentes e sem falha de consulta;
    do contrário é "sem informação", "falha de consulta" ou "dados desatualizados" — três coisas diferentes, e nenhuma
    delas é zero. Chip com consumo mas com histórico parcial entra no ranking de MAIORES (a soma é um piso, e a linha
    diz isso) e nunca no de MENORES (um piso baixo não prova consumo baixo).
  • INATIVIDADE É A ÚLTIMA ATIVIDADE CONHECIDA, NUNCA A HORA DA CONSULTA. A última atividade de uma sessão é o `fim`
    dela ou, se ainda aberta, `início + duração` — o último avanço que a PLATAFORMA informou. Quando o chip não tem
    sessões lidas, vale a última consulta VÁLIDA que a API interpretou como sessão ativa (era ativa naquele instante).
    Hora de consulta sem sessão ativa não é atividade. Chip que falhou na consulta, ou cujo dado está velho, não é
    classificado por faixa: a falha é de comunicação, e dizer "há 3 dias sem atividade" a partir de dado velho seria
    acusar o chip de um defeito da leitura.
  • NENHUMA CONSULTA POR LINHA. Cada bloco é uma consulta agregada; o gráfico e a lista que ele abre partem da MESMA
    classificação (`pks_do_filtro`), então o número do segmento é o número que a lista mostra.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, time, timedelta
from urllib.parse import urlencode

from django.db.models import Count, Max, Sum
from django.urls import reverse
from django.utils import timezone

from ProjetoEstoque.models import FrotaVinculo, M2MChip, M2MConsulta, M2MIntegracao, M2MSessaoDados
from services import icone_service
from services import m2m_interpretacao as interp
from services import m2m_monitoramento_service as m2m

# ── filtros ──────────────────────────────────────────────────────────────
PERIODOS = ("api", "7", "30", "90")
PERIODO_PADRAO = "api"
ESCOPOS = (("uso", "Somente em uso"), ("todos", "Todos os monitorados"))
ESCOPO_PADRAO = "uso"
LIMITE_DO_RANKING = 8

# ── inatividade ──────────────────────────────────────────────────────────
HORA = 3600
FAIXAS_DE_INATIVIDADE = (
    ("menos_1h", "Menos de 1 hora"),
    ("de_1_a_6h", "1 a 6 horas"),
    ("de_6_a_24h", "6 a 24 horas"),
    ("de_1_a_7d", "1 a 7 dias"),
    ("mais_7d", "Mais de 7 dias"),
    ("insuficiente", "Sem histórico suficiente"),
    ("comunicacao", "Falha ou dado desatualizado"),
)
ROTULO_DA_FAIXA = dict(FAIXAS_DE_INATIVIDADE)
DIAS_PARA_AFIRMAR_INATIVIDADE = 7       # histórico lido por tantos dias sem atividade = "mais de 7 dias"
_LIMITES_DAS_FAIXAS = ((1 * HORA, "menos_1h"), (6 * HORA, "de_1_a_6h"), (24 * HORA, "de_6_a_24h"), (7 * 24 * HORA, "de_1_a_7d"))

# ── consumo ──────────────────────────────────────────────────────────────
CATEGORIAS_DE_CONSUMO = (
    ("com_consumo", "Com consumo"),
    ("zero_confirmado", "Consumo zero confirmado"),
    ("sem_informacao", "Sem informação de consumo"),
    ("falha_consulta", "Falha de consulta"),
    ("desatualizado", "Dados desatualizados"),
    ("periodo_diferente", "Período diferente do da TIM"),
)
ROTULO_DA_CATEGORIA = dict(CATEGORIAS_DE_CONSUMO)


# ═════════════════════════════════════════════════════════════════════════
# Filtros
# ═════════════════════════════════════════════════════════════════════════

def periodo_pedido(valor) -> str:
    texto = str(valor or "").strip().lower()
    return texto if texto in PERIODOS else PERIODO_PADRAO


def escopo_pedido(valor) -> str:
    texto = str(valor or "").strip().lower()
    return texto if texto in dict(ESCOPOS) else ESCOPO_PADRAO


def unidade_pedida(valor) -> int | None:
    texto = str(valor or "").strip()
    return int(texto) if texto.isdigit() else None


def chips_do_escopo(escopo: str = ESCOPO_PADRAO, unidade: int | None = None):
    """Os chips que a API consulta (monitorados, de operadora com integração) — os únicos para os quais consumo e
    atividade existem —, restritos aos que estão em uso (`escopo="uso"`) e/ou a uma unidade."""
    qs = M2MChip.objects.filter(m2m.Q_CONSULTAVEL)
    if escopo == "uso":
        qs = qs.filter(m2m.q_uso("em_uso"))
    if unidade:
        qs = qs.filter(m2m.q_unidade(unidade))
    return qs


def _base(cfg) -> int:
    return m2m.base_de_consumo(cfg.mapeamento)


def _inicio_do_dia(dia) -> datetime:
    return timezone.make_aware(datetime.combine(dia, time.min))


def _texto_de_data(momento) -> str:
    return timezone.localtime(momento).strftime("%d/%m/%Y %H:%M") if momento else ""


def _rotulo_relativo(segundos: float) -> str:
    segundos = max(0, int(segundos))
    if segundos < 60:
        return "agora há pouco"
    if segundos < HORA:
        return f"há {segundos // 60} min"
    if segundos < 48 * HORA:
        return f"há {segundos // HORA} h"
    return f"há {segundos // (24 * HORA)} dias"


# ═════════════════════════════════════════════════════════════════════════
# Atividade (a base da inatividade e do "última atividade conhecida" do mapa)
# ═════════════════════════════════════════════════════════════════════════

def ultima_atividade_por_chip(chip_ids) -> dict[int, dict]:
    """`{chip_id: {"em": datetime, "fonte": "sessao" | "consulta"}}` — só para quem TEM evidência. Duas consultas
    agregadas (sessões fechadas e abertas) mais uma só para os chips sem sessão lida."""
    ids = list(chip_ids)
    if not ids:
        return {}
    ultimas: dict[int, datetime] = dict(
        M2MSessaoDados.objects.filter(chip_id__in=ids, fim__isnull=False).values_list("chip_id").annotate(m=Max("fim"))
    )
    # sessão aberta: o último avanço que a plataforma informou (início + duração), não o momento da leitura
    for chip_id, inicio, duracao in M2MSessaoDados.objects.filter(chip_id__in=ids, fim__isnull=True).values_list("chip_id", "inicio", "duracao_s"):
        avancou = inicio + timedelta(seconds=duracao or 0)
        if chip_id not in ultimas or avancou > ultimas[chip_id]:
            ultimas[chip_id] = avancou
    resultado = {cid: {"em": em, "fonte": "sessao"} for cid, em in ultimas.items()}
    sem_sessao = [i for i in ids if i not in resultado]
    if sem_sessao:
        consultas = (
            M2MConsulta.objects.filter(
                chip_id__in=sem_sessao, tipo=M2MConsulta.Tipo.SESSAO, resultado__in=M2MConsulta.RESULTADOS_VALIDOS,
                estado_interpretado=M2MChip.SessaoEstado.ATIVA,
            ).values_list("chip_id").annotate(m=Max("iniciada_em"))
        )
        for chip_id, em in consultas:
            resultado[chip_id] = {"em": em, "fonte": "consulta"}
    return resultado


def _dados_de_leitura(chip: dict, cfg: M2MIntegracao, agora) -> tuple[bool, bool]:
    """`(falha, defasado)` de um chip (dict de `.values()`): a última consulta falhou, ou os dados das sessões não são
    lidos há mais do que `horas_desatualizado`. Os dois dizem "não confie nesta leitura"."""
    falha = (chip["falhas_consecutivas"] or 0) > 0
    sincronizado = chip["dados_sincronizados_em"]
    limite = timedelta(hours=cfg.horas_desatualizado)
    defasado = sincronizado is None or (agora - sincronizado) > limite
    return falha, defasado


_CAMPOS_DO_CHIP = (
    "pk", "nome", "iccid", "falhas_consecutivas", "dados_desde", "dados_sincronizados_em", "sessao_estado",
    "ultima_valida_em", "consumo_consulta__periodo_inicio",
)


def _destinos(chip_ids) -> dict[int, dict]:
    """Onde cada chip está (vínculo ativo): rótulo, link e ícone do equipamento — numa consulta só."""
    vinculos = FrotaVinculo.objects.filter(chip_id__in=list(chip_ids), fim_em__isnull=True).select_related(
        "veiculo", "veiculo__tipo", "veiculo__classe_operacional", "item", "item__subtipo", "item__subtipo__categoria",
    )
    saida = {}
    for v in vinculos:
        if v.veiculo_id:
            saida[v.chip_id] = {
                "rotulo": v.destino_rotulo, "url": reverse("frota_veiculo_detalhe", args=[v.veiculo_id]),
                "icone": icone_service.icone_do_veiculo(v.veiculo.classe_operacional, v.veiculo.tipo),
            }
        elif v.item_id:
            saida[v.chip_id] = {
                "rotulo": v.destino_rotulo, "url": reverse("equipamento_detalhe", args=[v.item_id]),
                "icone": icone_service.icone_id(
                    v.item.subtipo.nome if v.item.subtipo_id else "", ambiente="ti",
                    categoria=lambda: getattr(v.item.subtipo.categoria, "nome", None) if v.item.subtipo_id else None,
                ),
            }
    return saida


# ═════════════════════════════════════════════════════════════════════════
# 1 · Utilização: em uso × não utilizado
# ═════════════════════════════════════════════════════════════════════════

def _url_da_lista(**parametros) -> str:
    limpos = {k: v for k, v in parametros.items() if v not in (None, "")}
    return reverse("m2m_chips") + ("?" + urlencode(limpos) if limpos else "")


# A nota de fonte do gráfico. O 2º período diz por que a API não conta como "uso" — texto da API: quem não vê API recebe só o 1º.
ORIGEM_DA_UTILIZACAO = "Vínculos entre chips e equipamentos (cadastro). Resposta da API, sessão ou consumo não tornam um chip \"em uso\"."
ORIGEM_DA_UTILIZACAO_SEM_API = "Vínculos entre chips e equipamentos (cadastro)."


def utilizacao(unidade: int | None = None) -> dict:
    """A distribuição dos chips CADASTRADOS por grupo de uso (`GRUPOS_DE_USO`), cada chip em um só. O percentual é de
    `em_uso` e `nao_utilizado` entre si (o "total considerado"); os demais estados aparecem separados, com o número
    e o link, fora dessa conta."""
    base = M2MChip.objects.all()
    if unidade:
        base = base.filter(m2m.q_unidade(unidade))
    contagens = base.aggregate(**{chave: Count("pk", filter=m2m.q_uso(chave)) for chave, _ in m2m.GRUPOS_DE_USO})
    total = base.count()
    considerados = contagens["em_uso"] + contagens["nao_utilizado"]

    def grupo(chave):
        n = contagens[chave]
        return {
            "chave": chave, "rotulo": m2m.ROTULO_DE_USO[chave], "n": n,
            "pct": round(n * 100 / considerados, 1) if considerados and chave in ("em_uso", "nao_utilizado") else None,
            "url": _url_da_lista(uso=chave, unidade=unidade),
        }

    principais = [grupo("em_uso"), grupo("nao_utilizado")]
    # o arco do donut: `pathLength=100` no SVG, então o tamanho do traço é o próprio percentual
    acumulado = 0.0
    for g in principais:
        g["traco"] = round(g["n"] * 100 / considerados, 2) if considerados else 0
        g["resto"] = round(100 - g["traco"], 2)
        g["inicio"] = round(acumulado, 2)
        acumulado += g["traco"]
    fora = [grupo(c) for c, _ in m2m.GRUPOS_DE_USO if c not in ("em_uso", "nao_utilizado") and contagens[c]]
    return {
        "total": total, "considerados": considerados, "fora_do_calculo": total - considerados,
        "principais": principais, "fora": fora, "vazio": considerados == 0,
        "origem": ORIGEM_DA_UTILIZACAO,
    }


# ═════════════════════════════════════════════════════════════════════════
# 2 · Consumo
# ═════════════════════════════════════════════════════════════════════════

def janela_de_consumo(periodo: str, chips, agora=None) -> dict:
    """De quando a quando o consumo é somado. `api`: o início que a TIM informou na consulta de consumo (o mais comum
    entre os chips; `inicio_dt` é `None` se nenhum chip trouxe período). `7`/`30`/`90`: os últimos N dias, a partir da
    meia-noite local do primeiro dia."""
    agora = agora or timezone.now()
    hoje = timezone.localdate(agora)
    if periodo == "api":
        inicios = Counter(chips.filter(consumo_consulta__periodo_inicio__isnull=False).values_list("consumo_consulta__periodo_inicio", flat=True))
        if not inicios:
            return {"periodo": "api", "inicio_dt": None, "inicio_dia": None, "dias": 0, "texto": "período da TIM não informado", "rotulo": "Período da TIM"}
        inicio_dt = inicios.most_common(1)[0][0]
        dia = timezone.localdate(inicio_dt)
        return {
            "periodo": "api", "inicio_dt": inicio_dt, "inicio_dia": dia, "dias": (hoje - dia).days + 1,
            "texto": f"{dia:%d/%m/%Y} a {hoje:%d/%m/%Y} (período informado pela TIM)", "rotulo": "Período da TIM",
        }
    dias = int(periodo)
    dia = hoje - timedelta(days=dias - 1)
    return {
        "periodo": periodo, "inicio_dt": _inicio_do_dia(dia), "inicio_dia": dia, "dias": dias,
        "texto": f"{dia:%d/%m/%Y} a {hoje:%d/%m/%Y} (últimos {dias} dias)", "rotulo": f"Últimos {dias} dias",
    }


def classificar_consumo(periodo: str, escopo: str = ESCOPO_PADRAO, unidade: int | None = None, agora=None) -> dict:
    """Classifica CADA chip do escopo na janela do período (ver o docstring do módulo) e devolve
    `{"janela", "linhas": [...], "base"}`. Cada linha: `pk`, `nome`, `iccid`, `categoria` (`CATEGORIAS_DE_CONSUMO`),
    `bytes` (soma das sessões que COMEÇARAM na janela; 0 sem consumo), `parcial` (histórico não cobre a janela inteira),
    `defasado` (falha de consulta ou dado velho)."""
    agora = agora or timezone.now()
    cfg = M2MIntegracao.get()
    chips = chips_do_escopo(escopo, unidade)
    janela = janela_de_consumo(periodo, chips, agora)
    linhas = []
    dados = list(chips.values(*_CAMPOS_DO_CHIP))
    somas: dict[int, dict] = {}
    if janela["inicio_dt"] is not None and dados:
        somas = {
            r["chip"]: r for r in M2MSessaoDados.objects.filter(chip_id__in=[c["pk"] for c in dados], inicio__gte=janela["inicio_dt"])
            .values("chip").annotate(total=Sum("total_bytes"), n=Count("pk"))
        }
    for c in dados:
        falha, defasado = _dados_de_leitura(c, cfg, agora)
        soma = somas.get(c["pk"])
        total = int(soma["total"] or 0) if soma else 0
        cobre = bool(janela["inicio_dia"] and c["dados_desde"] and c["dados_desde"] <= janela["inicio_dia"])
        diferente = (
            periodo == "api" and janela["inicio_dt"] is not None
            and c["consumo_consulta__periodo_inicio"] not in (None, janela["inicio_dt"])
        )
        if janela["inicio_dt"] is None:
            categoria = "sem_informacao"
        elif diferente:
            categoria = "periodo_diferente"
        elif total > 0:
            categoria = "com_consumo"
        elif falha:
            categoria = "falha_consulta"
        elif defasado:
            categoria = "desatualizado"
        elif cobre:
            categoria = "zero_confirmado"
        else:
            categoria = "sem_informacao"
        linhas.append({
            "pk": c["pk"], "nome": c["nome"], "iccid": c["iccid"], "categoria": categoria, "bytes": total,
            "parcial": not cobre, "defasado": falha or defasado, "sincronizado_em": c["dados_sincronizados_em"],
        })
    return {"janela": janela, "linhas": linhas, "base": _base(cfg)}


def consumo(periodo: str = PERIODO_PADRAO, escopo: str = ESCOPO_PADRAO, unidade: int | None = None, agora=None, limite: int = LIMITE_DO_RANKING) -> dict:
    """O gráfico de maiores e menores consumidores, mais o resumo de quem NÃO tem número confiável. `maiores`: os de
    maior soma (piso, se o histórico é parcial); `menores`: só chips com a janela COMPLETA, dados em dia e consumo
    acima de zero — os de zero confirmado são contados à parte (empate em zero não é ranking)."""
    agora = agora or timezone.now()
    r = classificar_consumo(periodo, escopo, unidade, agora)
    base, linhas, janela = r["base"], r["linhas"], r["janela"]
    com = [l for l in linhas if l["categoria"] == "com_consumo"]

    maiores = sorted(com, key=lambda l: (-l["bytes"], l["nome"]))[:limite]
    menores = sorted((l for l in com if not l["parcial"] and not l["defasado"]), key=lambda l: (l["bytes"], l["nome"]))[:limite]
    destinos = _destinos({l["pk"] for l in maiores} | {l["pk"] for l in menores})

    def linha(l, maior):
        return {
            "pk": l["pk"], "nome": l["nome"], "iccid": l["iccid"], "bytes": l["bytes"],
            "texto": interp.formatar_bytes(l["bytes"], base),
            "pct": round(l["bytes"] * 100 / maior, 1) if maior else 0,
            "parcial": l["parcial"], "defasado": l["defasado"], "destino": destinos.get(l["pk"]),
            "url": reverse("m2m_chip_detalhe", args=[l["pk"]]),
        }

    maior_dos_maiores = max((l["bytes"] for l in maiores), default=0)
    maior_dos_menores = max((l["bytes"] for l in menores), default=0)
    contagem = Counter(l["categoria"] for l in linhas)
    parametros = {"periodo": periodo, "escopo": escopo, "unidade": unidade}
    resumo = [
        {
            "chave": chave, "rotulo": ROTULO_DA_CATEGORIA[chave], "n": contagem[chave],
            "url": _url_da_lista(consumo=chave, **parametros),
        }
        for chave in ("zero_confirmado", "sem_informacao", "falha_consulta", "desatualizado", "periodo_diferente") if contagem[chave]
    ]
    sincronizados = [l["sincronizado_em"] for l in linhas if l["sincronizado_em"]]
    return {
        "janela": janela, "periodo": periodo, "unidade_de_medida": "base 1024 (KB, MB, GB), como o portal da TIM",
        "maiores": [linha(l, maior_dos_maiores) for l in maiores],
        "menores": [linha(l, maior_dos_menores) for l in menores],
        "resumo": resumo, "total": len(linhas), "com_consumo": len(com),
        "parciais": sum(1 for l in com if l["parcial"]),
        "vazio": not linhas,
        "sem_numero": not com,
        "atualizado_em": max(sincronizados) if sincronizados else None,
        "origem": "Sessões de dados lidas na plataforma da TIM. Zero só é afirmado com o histórico da janela completo, dados recentes e sem falha de consulta.",
    }


def periodos_disponiveis(escopo: str = ESCOPO_PADRAO, unidade: int | None = None, agora=None) -> list[dict]:
    """Para o seletor de período: cada opção diz se existe chip com o histórico COMPLETO naquela janela. 30 e 90 dias
    só valem "quando houver histórico suficiente" — a opção continua visível (o usuário vê por que não vale), mas
    desabilitada, com a data até onde o histórico foi lido."""
    agora = agora or timezone.now()
    chips = chips_do_escopo(escopo, unidade)
    hoje = timezone.localdate(agora)
    lido_desde = chips.filter(dados_desde__isnull=False).aggregate(m=Max("dados_desde"))["m"]
    saida = []
    for valor, rotulo in (("api", "Período da TIM"), ("7", "7 dias"), ("30", "30 dias"), ("90", "90 dias")):
        if valor == "api":
            disponivel, nota = True, "o período que a própria TIM informa na consulta de consumo"
        else:
            inicio = hoje - timedelta(days=int(valor) - 1)
            disponivel = chips.filter(dados_desde__lte=inicio).exists()
            nota = "" if disponivel else (
                "histórico ainda não cobre esta janela" + (f" (o mais antigo foi lido a partir de {lido_desde:%d/%m})" if lido_desde else "")
            )
        saida.append({"valor": valor, "rotulo": rotulo, "disponivel": disponivel, "nota": nota})
    return saida


# ═════════════════════════════════════════════════════════════════════════
# 3 · Inatividade
# ═════════════════════════════════════════════════════════════════════════

def _faixa_da_idade(segundos: float) -> str:
    for limite, chave in _LIMITES_DAS_FAIXAS:
        if segundos < limite:
            return chave
    return "mais_7d"


def classificar_inatividade(escopo: str = ESCOPO_PADRAO, unidade: int | None = None, agora=None) -> list[dict]:
    """Uma linha por chip do escopo: `pk`, `nome`, `iccid`, `faixa` (`FAIXAS_DE_INATIVIDADE`), `ultima_atividade`
    (datetime ou `None`), `fonte` (`sessao`/`consulta`/`""`) e `idade_s`. Ver o docstring do módulo para o critério."""
    agora = agora or timezone.now()
    cfg = M2MIntegracao.get()
    hoje = timezone.localdate(agora)
    dados = list(chips_do_escopo(escopo, unidade).values(*_CAMPOS_DO_CHIP))
    atividades = ultima_atividade_por_chip(c["pk"] for c in dados)
    linhas = []
    for c in dados:
        falha, defasado = _dados_de_leitura(c, cfg, agora)
        ev = atividades.get(c["pk"])
        em = min(ev["em"], agora) if ev else None            # um relógio adiantado da plataforma não vira "daqui a pouco"
        idade = (agora - em).total_seconds() if em else None
        if falha or defasado:
            faixa = "comunicacao"
        elif em is not None:
            faixa = _faixa_da_idade(idade)
        elif c["dados_desde"] and (hoje - c["dados_desde"]).days >= DIAS_PARA_AFIRMAR_INATIVIDADE:
            faixa = "mais_7d"           # o histórico lido cobre 7+ dias e não tem uma sessão: a afirmação é verdadeira
        else:
            faixa = "insuficiente"
        linhas.append({
            "pk": c["pk"], "nome": c["nome"], "iccid": c["iccid"], "faixa": faixa, "ultima_atividade": em,
            "fonte": ev["fonte"] if ev else "", "idade_s": idade, "desde": c["dados_desde"], "falha": falha,
        })
    return linhas


def _resumo_da_inatividade(linhas: list[dict], contagem: Counter) -> str:
    """Uma frase honesta sobre o que o gráfico sabe: quantos chips têm última atividade CONHECIDA (e portanto tempo de
    verdade), quantos estão em "mais de 7 dias" só por não ter nenhuma sessão no histórico lido, e quantos ficaram sem
    classificação (falha de comunicação ou histórico curto)."""
    total = len(linhas)
    if not total:
        return ""
    conhecidos = sum(1 for l in linhas if l["ultima_atividade"] is not None and l["faixa"] != "comunicacao")
    sem_sessao = sum(1 for l in linhas if l["faixa"] == "mais_7d" and l["ultima_atividade"] is None)
    partes = [f"{conhecidos} de {total} chip{'s' if total != 1 else ''} com última atividade conhecida"]
    if sem_sessao:
        partes.append(f"{sem_sessao} sem nenhuma sessão no histórico lido (em \"mais de 7 dias\" por isso, sem tempo exato)")
    nao_classificados = contagem["insuficiente"] + contagem["comunicacao"]
    if nao_classificados:
        partes.append(f"{nao_classificados} sem classificação (histórico curto, falha de comunicação ou dado desatualizado)")
    return "; ".join(partes) + "."


def inatividade(escopo: str = ESCOPO_PADRAO, unidade: int | None = None, agora=None, limite_da_lista: int = 8) -> dict:
    """O gráfico de faixas de inatividade e a lista dos que estão há mais tempo sem atividade."""
    agora = agora or timezone.now()
    linhas = classificar_inatividade(escopo, unidade, agora)
    contagem = Counter(l["faixa"] for l in linhas)
    classificados = sum(contagem[c] for c, _ in FAIXAS_DE_INATIVIDADE if c not in ("insuficiente", "comunicacao"))
    parametros = {"escopo": escopo, "unidade": unidade}
    maior = max((contagem[c] for c, _ in FAIXAS_DE_INATIVIDADE), default=0)
    total = len(linhas)
    faixas = [
        {
            "chave": chave, "rotulo": rotulo, "n": contagem[chave],
            "pct_total": round(contagem[chave] * 100 / total, 1) if total else 0,
            # `pct` é a LARGURA da barra (relativa à maior faixa), como em `agro_dash_service.barras`; o percentual do
            # total vai no texto (`nota`)
            "pct": round(contagem[chave] * 100 / maior, 1) if maior else 0,
            "nota": f"{contagem[chave] * 100 / total:.1f}".replace(".", ",") + " %" if total else "",
            "url": _url_da_lista(inatividade=chave, **parametros),
            "tom": {"insuficiente": "neutro", "comunicacao": "atencao"}.get(chave, "normal"),
        }
        for chave, rotulo in FAIXAS_DE_INATIVIDADE
    ]
    # quem está há mais tempo parado (só com tempo CONHECIDO; "mais de 7 dias" sem sessão no histórico vem por último)
    parados = [l for l in linhas if l["faixa"] in ("de_1_a_7d", "mais_7d")]
    parados.sort(key=lambda l: (l["ultima_atividade"] is None, -(l["idade_s"] or 0), l["nome"]))
    destinos = _destinos([l["pk"] for l in parados[:limite_da_lista]])
    lista = [
        {
            "pk": l["pk"], "nome": l["nome"], "url": reverse("m2m_chip_detalhe", args=[l["pk"]]), "destino": destinos.get(l["pk"]),
            "faixa_rotulo": ROTULO_DA_FAIXA[l["faixa"]],
            "texto": (
                f"{_rotulo_relativo(l['idade_s'])} · {_texto_de_data(l['ultima_atividade'])}"
                if l["ultima_atividade"] else
                (f"sem sessão no histórico lido desde {l['desde']:%d/%m}" if l["desde"] else "sem sessão registrada")
            ),
        }
        for l in parados[:limite_da_lista]
    ]
    return {
        "faixas": faixas, "total": total, "classificados": classificados, "vazio": total == 0, "lista": lista,
        "sem_classificar": total - classificados,
        "resumo": _resumo_da_inatividade(linhas, contagem),
        "origem": "Última atividade de sessão conhecida (fim da sessão, ou o último avanço informado pela plataforma); nunca a hora da consulta.",
    }


# ═════════════════════════════════════════════════════════════════════════
# O dashboard e a lista filtrada
# ═════════════════════════════════════════════════════════════════════════

def indicadores(unidade: int | None = None, com_api: bool = True) -> dict:
    """A faixa de indicadores gerais, no mesmo recorte do restante do painel. Cada contagem tem o link para a lista com o
    MESMO critério: `total`, `em_uso`, `disponiveis` (cadastro) e, sobre os chips consultados pela API, `sessao_ativa`,
    `sem_sessao` e `falha_consulta` (os segmentos de `resumo_conectividade`, cada chip em um só). `com_api=False` (quem não
    vê API) devolve só as de cadastro e vínculo — as outras são o que a operadora respondeu, e nem são calculadas."""
    from django.db.models import Q
    base = M2MChip.objects.all()
    if unidade:
        base = base.filter(m2m.q_unidade(unidade))
    consultaveis = base.filter(m2m.Q_CONSULTAVEL)
    leitura = consultaveis.aggregate(
        sessao_ativa=Count("pk", filter=Q(falhas_consecutivas=0, sessao_estado=M2MChip.SessaoEstado.ATIVA)),
        sem_sessao=Count("pk", filter=Q(falhas_consecutivas=0, sessao_estado=M2MChip.SessaoEstado.SEM_SESSAO)),
        falha_consulta=Count("pk", filter=Q(falhas_consecutivas__gt=0)),
    ) if com_api else {}
    uso = base.aggregate(
        em_uso=Count("pk", filter=m2m.q_uso("em_uso")), disponiveis=Count("pk", filter=m2m.q_uso("nao_utilizado")),
    )
    saida = {
        "total": base.count(), **uso, **leitura,
        "url_total": _url_da_lista(unidade=unidade),
        "url_em_uso": _url_da_lista(uso="em_uso", unidade=unidade),
        "url_disponiveis": _url_da_lista(uso="nao_utilizado", unidade=unidade),
    }
    if com_api:
        saida.update(
            url_sessao_ativa=_url_da_lista(conectividade="ativa", unidade=unidade),
            url_sem_sessao=_url_da_lista(conectividade="sem_sessao", unidade=unidade),
            url_falha=_url_da_lista(conectividade="falha_consulta", unidade=unidade),
        )
    return saida


def painel_de_analise(periodo=None, escopo=None, unidade=None, agora=None, com_api: bool = True) -> dict:
    """Tudo que o dashboard de análise lê: os três gráficos, os filtros normalizados e a data da última leitura.
    `com_api=False` (quem não vê API) deixa só a UTILIZAÇÃO — chips em uso e não utilizados, que vem do vínculo — e os
    indicadores de cadastro: `consumo` e `inatividade` (o que a operadora respondeu) voltam `None` e nem são calculados."""
    agora = agora or timezone.now()
    periodo, escopo, unidade = periodo_pedido(periodo), escopo_pedido(escopo), unidade_pedida(unidade)
    cons = consumo(periodo, escopo, unidade, agora) if com_api else None
    util = utilizacao(unidade)
    if not com_api:
        util["origem"] = ORIGEM_DA_UTILIZACAO_SEM_API
    inativ = inatividade(escopo, unidade, agora) if com_api else None
    atualizados = [m for m in ((cons or {}).get("atualizado_em"),) if m]
    return {
        "periodo": periodo, "escopo": escopo, "unidade": unidade,
        "periodos": periodos_disponiveis(escopo, unidade, agora) if com_api else [], "escopos": ESCOPOS,
        "utilizacao": util, "consumo": cons, "inatividade": inativ, "indicadores": indicadores(unidade, com_api),
        "atualizado_em": max(atualizados) if atualizados else None,
        "atualizado_texto": _texto_de_data(max(atualizados)) if atualizados else "",
        "atualizado_rotulo": _rotulo_relativo((agora - max(atualizados)).total_seconds()) if atualizados else "",
    }


def pks_do_filtro(filtros: dict, agora=None) -> list[int]:
    """Os chips de `?inatividade=` ou `?consumo=` da lista — a MESMA classificação do gráfico, para o número do segmento
    ser o número da lista. Valor inválido: nenhum chip."""
    escopo, unidade = escopo_pedido(filtros.get("escopo")), unidade_pedida(filtros.get("unidade"))
    if filtros.get("inatividade"):
        faixa = filtros["inatividade"]
        if faixa not in ROTULO_DA_FAIXA:
            return []
        return [l["pk"] for l in classificar_inatividade(escopo, unidade, agora) if l["faixa"] == faixa]
    categoria = filtros.get("consumo")
    if categoria not in ROTULO_DA_CATEGORIA:
        return []
    r = classificar_consumo(periodo_pedido(filtros.get("periodo")), escopo, unidade, agora)
    return [l["pk"] for l in r["linhas"] if l["categoria"] == categoria]
