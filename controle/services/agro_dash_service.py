"""
Os números dos dashboards ("Visão geral") do Zelo Agro: distribuições, séries e listas de atenção.

Princípios — os que o pedido de 14/11 fez regra:

  • TODO NÚMERO VEM DO CADASTRO OU DO QUE A API RESPONDEU. Nada é estimado, preenchido ou arredondado para "ficar
    bonito"; sem dado, a tela mostra estado vazio (`vazio=True`), não um gráfico de zeros.
  • CADA GRÁFICO DIZ A UNIDADE, O PERÍODO E A ORIGEM. Os dicts levam `origem` (de onde sai o número) e `unidade`; o
    parcial de gráfico os imprime.
  • NÃO SE INVENTA FRANQUIA. Consumo é o que a plataforma registrou nas sessões de dados (`M2MSessaoDados`). Nenhum
    percentual de uso é calculado: o limite contratado de cada chip não é conhecido pelo sistema.
  • COBERTURA É DITA. O histórico de sessões é baixado aos poucos (3 dias por leitura): enquanto um chip não tem o
    período inteiro, a soma pode estar abaixo do consumo real, e `cobertura` diz quantos chips têm o período completo.
  • NENHUMA CONSULTA POR LINHA. Cada bloco é uma consulta agregada.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.urls import reverse
from django.utils import timezone

from ProjetoEstoque.models import (
    FrotaVeiculo,
    M2MChip,
    M2MSessaoDados,
    TecEnlace,
)
from services import icone_service
from services import m2m_interpretacao as interp
from services import m2m_monitoramento_service as m2m

PERIODOS = (7, 30, 90)
PERIODO_PADRAO = 30
STATUS_ATIVOS_DA_FROTA = (FrotaVeiculo.Status.ATIVO, FrotaVeiculo.Status.MANUTENCAO, FrotaVeiculo.Status.INATIVO)


def periodo_pedido(valor) -> int:
    """`7`, `30` ou `90` dias; qualquer outra coisa volta ao padrão."""
    texto = str(valor or "").strip()
    return int(texto) if texto.isdigit() and int(texto) in PERIODOS else PERIODO_PADRAO


def barras(pares, *, limite: int = 6, rotulo_outros: str = "Outros", vazio_rotulo: str = "Não informado") -> list[dict]:
    """De `[(rotulo, n, url_ou_None)]` para `[{"rotulo", "n", "pct", "url"}]`, do maior para o menor, com `pct`
    relativo ao MAIOR (a barra mais longa ocupa a largura toda — é comparação entre barras, não percentual do todo).
    Passando de `limite` itens, o resto some em uma barra "Outros" (sem link). Rótulo vazio vira `vazio_rotulo`."""
    # cada par é (rótulo, n, url) e pode trazer um 4º elemento: o id do ícone do tipo (biblioteca do Zelo)
    itens = [(p[0] or vazio_rotulo, int(p[1]), p[2], p[3] if len(p) > 3 else "") for p in pares if p[1]]
    itens.sort(key=lambda i: (-i[1], i[0].lower()))
    if len(itens) > limite:
        resto = sum(i[1] for i in itens[limite - 1:])
        itens = itens[: limite - 1] + [(rotulo_outros, resto, None, "")]
    maior = max((i[1] for i in itens), default=0)
    return [
        {"rotulo": r, "n": n, "pct": round(n * 100 / maior, 1) if maior else 0, "url": u, "icone": ic}
        for r, n, u, ic in itens
    ]


# ════════════════════════════════════════════════════════
# Frotas e Maquinários
# ════════════════════════════════════════════════════════

def painel_da_frota() -> dict:
    """Distribuição da frota (equipamentos NÃO desativados): por tipo, fabricante e fazenda, e o que falta no cadastro.

        {"total", "vazio", "por_tipo", "por_fabricante", "por_fazenda"          # listas de `barras`
         "incompletos": {"sem_fabricante", "sem_modelo", "sem_fazenda", "sem_localizacao", "total"},
         "com_localizacao"}
    `incompletos.total` conta equipamentos com PELO MENOS uma das três faltas cadastrais (fabricante, modelo, fazenda);
    a localização no mapa é outra pergunta e vem separada."""
    base = FrotaVeiculo.objects.filter(status__in=STATUS_ATIVOS_DA_FROTA)
    total = base.count()
    lista = reverse("frota_visao_geral")
    por_tipo = base.values("tipo", "tipo__nome").annotate(n=Count("pk"))
    por_fabricante = base.values("fabricante").annotate(n=Count("pk"))
    por_fazenda = base.values("localidade", "localidade__local").annotate(n=Count("pk"))
    faltas = base.aggregate(
        sem_fabricante=Count("pk", filter=Q(fabricante="")),
        sem_modelo=Count("pk", filter=Q(modelo="")),
        sem_fazenda=Count("pk", filter=Q(localidade__isnull=True)),
        sem_localizacao=Count("pk", filter=Q(latitude__isnull=True)),
        total=Count("pk", filter=Q(fabricante="") | Q(modelo="") | Q(localidade__isnull=True)),
    )
    return {
        "total": total, "vazio": total == 0,
        "por_tipo": barras([
            (r["tipo__nome"], r["n"], f"{lista}?tipo={r['tipo']}", icone_service.icone_id(r["tipo__nome"], ambiente="agro"))
            for r in por_tipo
        ]),
        "por_fabricante": barras([(r["fabricante"], r["n"], f"{lista}?q={r['fabricante']}" if r["fabricante"] else None) for r in por_fabricante]),
        "por_fazenda": barras([(r["localidade__local"], r["n"], f"{lista}?localidade={r['localidade']}" if r["localidade"] else None) for r in por_fazenda],
                              vazio_rotulo="Sem fazenda"),
        "incompletos": faltas, "com_localizacao": total - faltas["sem_localizacao"],
    }


def movimentacoes_recentes_da_frota(limite: int = 6) -> list:
    """As últimas movimentações de EQUIPAMENTO (mudança de fazenda, responsável, setor, centro de custo) — o que a
    operação registrou com motivo em "Registrar movimentação"."""
    from ProjetoEstoque.models import FrotaEvento

    return list(
        FrotaEvento.objects.filter(tipo=FrotaEvento.Tipo.MOVIMENTACAO).select_related("veiculo", "usuario")
        .order_by("-em", "-pk")[:limite]
    )


# ════════════════════════════════════════════════════════
# Chips e Conectividade
# ════════════════════════════════════════════════════════

def _base_do_consumo(cfg=None) -> int:
    return m2m.base_de_consumo((cfg or m2m.M2MIntegracao.get()).mapeamento)


def serie_de_consumo(dias: int, *, fazenda=None, agora=None) -> dict:
    """O consumo de dados por DIA, somando as sessões que começaram em cada dia (todos os chips consultáveis).

        {"dias", "inicio", "pontos": [{"rotulo": "12/11", "iso", "bytes", "texto", "altura"}], "vazio",
         "total_bytes", "total_texto", "unidade": "B/KB/MB/GB", "cobertura": {"completos", "com_historico", "total", "parcial"},
         "origem", "periodo_texto"}

    `altura` é a altura relativa da coluna (0–100, em relação ao maior dia). Dia sem sessão é 0 B — mas só é
    "zero de verdade" para chips com o histórico completo: `cobertura.parcial` avisa quando nem todos têm."""
    agora = agora or timezone.now()
    hoje = timezone.localdate(agora)
    inicio = hoje - timedelta(days=dias - 1)
    cfg = m2m.M2MIntegracao.get()
    base = _base_do_consumo(cfg)
    chips = M2MChip.objects.filter(m2m.Q_CONSULTAVEL)
    if str(fazenda or "").isdigit():
        chips = chips.filter(m2m.q_unidade(int(fazenda)))      # a unidade do chip vinculado é a do equipamento
    inicio_dt = timezone.make_aware(datetime.combine(inicio, time.min))
    por_dia = dict(
        M2MSessaoDados.objects.filter(chip__in=chips, inicio__gte=inicio_dt)
        .annotate(dia=TruncDate("inicio")).values_list("dia").annotate(total=Sum("total_bytes"))
    )
    sincronizados = chips.filter(dados_sincronizados_em__isnull=False)
    completos = sincronizados.filter(dados_desde__lte=inicio).count()
    cobertura = {
        "completos": completos, "com_historico": sincronizados.count(), "total": chips.count(),
        "parcial": completos < chips.count(),
    }
    pontos = []
    maior = max(por_dia.values(), default=0)
    for k in range(dias):
        dia = inicio + timedelta(days=k)
        valor = int(por_dia.get(dia, 0) or 0)
        pontos.append({
            "rotulo": f"{dia:%d/%m}", "iso": dia.isoformat(), "bytes": valor,
            "texto": interp.formatar_bytes(valor, base) if valor else "0",
            "altura": round(valor * 100 / maior, 1) if maior else 0,
        })
    total = sum(p["bytes"] for p in pontos)
    return {
        "dias": dias, "inicio": inicio, "pontos": pontos, "vazio": not any(p["bytes"] for p in pontos),
        "total_bytes": total, "total_texto": interp.formatar_bytes(total, base) if total else "0",
        "cobertura": cobertura,
        "origem": "Sessões de dados lidas na plataforma da TIM (base 1024)",
        "periodo_texto": f"{inicio:%d/%m/%Y} a {hoje:%d/%m/%Y}",
    }


def maiores_consumos(dias: int, *, fazenda=None, limite: int = 5, agora=None) -> list[dict]:
    """Os chips que mais consumiram no período (soma das sessões), `[{"chip", "bytes", "texto", "pct"}]`."""
    agora = agora or timezone.now()
    inicio = timezone.localdate(agora) - timedelta(days=dias - 1)
    inicio_dt = timezone.make_aware(datetime.combine(inicio, time.min))
    consultaveis = M2MChip.objects.filter(m2m.Q_CONSULTAVEL)
    if str(fazenda or "").isdigit():
        consultaveis = consultaveis.filter(m2m.q_unidade(int(fazenda)))
    filtro = Q(chip__in=consultaveis)
    linhas = list(
        M2MSessaoDados.objects.filter(filtro, inicio__gte=inicio_dt).values("chip").annotate(total=Sum("total_bytes"))
        .order_by("-total")[:limite]
    )
    chips = {c.pk: c for c in M2MChip.objects.filter(pk__in=[l["chip"] for l in linhas])}
    base = _base_do_consumo()
    maior = max((l["total"] for l in linhas), default=0)
    return [
        {"chip": chips[l["chip"]], "bytes": l["total"], "texto": interp.formatar_bytes(l["total"], base),
         "pct": round(l["total"] * 100 / maior, 1) if maior else 0}
        for l in linhas if l["chip"] in chips and l["total"]
    ]


def painel_de_conectividade(dias: int, fazenda=None, agora=None) -> dict:
    """Tudo que o dashboard de conectividade lê, em um dict (cada item já agregado):

        kpis (de `frota_service.indicadores_frota`), `chips_sem_sessao_vinculados`, `desatualizados`, `consumo` (série),
        `maiores`, `execucoes` (últimos ciclos), `falhas` (últimas consultas com falha), `sincronizacao`."""
    from services import frota_service

    agora = agora or timezone.now()
    ind = frota_service.indicadores_frota(agora)["conectividade"]
    cfg = m2m.M2MIntegracao.get()
    return {
        "kpis": ind,
        "desatualizados": m2m.indicadores(agora)["desatualizados"],
        "consumo": serie_de_consumo(dias, fazenda=fazenda, agora=agora),
        "maiores": maiores_consumos(dias, fazenda=fazenda, agora=agora),
        "execucoes": m2m.execucoes_recentes(8),
        "falhas": m2m.ultimas_consultas(8, so_falhas=True),
        "sincronizacao": m2m.estado_sincronizacao(agora),
        "analise": m2m.chips_com_falha(5, cfg, agora),
    }


# ════════════════════════════════════════════════════════
# A porta do Zelo Agro (`/agro/`): o painel que junta os três módulos
# ════════════════════════════════════════════════════════

def _item_de_atencao(modulo: str, rotulo: str, n: int, url: str, nota: str, tom: str = "atencao") -> dict:
    return {"modulo": modulo, "rotulo": rotulo, "n": int(n), "url": url, "nota": nota, "tom": tom}


def atencao_da_porta(acesso: dict, *, kpis_conectividade=None, desatualizados=None, infra=None, alertas=None) -> list[dict]:
    """O que pede ação agora, uma linha por assunto, SÓ com o que tem ocorrência (n > 0) e cada uma levando à lista já
    filtrada — o número do cartão é o número que a lista mostra. Não há nota de "tudo bem" aqui: lista vazia é a
    resposta (o template diz que não há pendência). Ordem: o que está quebrado antes do que está por cuidar.

    `acesso` diz o que a pessoa pode ver (`frota`, `chips`, `historico`, `infra`, `estoque_ti` — quem também opera o estoque de TI;
    `api` — quem vê o que a operadora respondeu: sem ele, nenhum assunto de consulta ou de alerta de conectividade): um assunto
    de um módulo que ela não enxerga nem é calculado."""
    itens: list[dict] = []
    ver_api = bool(acesso.get("api", True))
    if acesso.get("chips") and kpis_conectividade and ver_api:
        itens.append(_item_de_atencao(
            "Conectividade", "Consulta com falha", kpis_conectividade["com_falha"],
            reverse("m2m_chips") + "?situacao_uso=vinculado&situacao=erro_consulta",
            "chips vinculados; a falha é da consulta, não do chip", "erro",
        ))
        itens.append(_item_de_atencao(
            "Conectividade", "Dados desatualizados", desatualizados or 0,
            reverse("m2m_chips") + "?situacao=desatualizado", "sem resposta válida recente da API",
        ))
    if acesso.get("estoque_ti") and acesso.get("chips"):
        from services import chip_estoque_ti_service as estoque_ti
        itens.append(_item_de_atencao(
            "Estoque de TI", "Chips sem item no estoque", estoque_ti.contar_chips_sem_item(),
            reverse("m2m_chips") + "?estoque_ti=sem", "sem valor, nota fiscal nem termo no estoque de TI: concilie em “Conciliar chips”",
        ))
    if infra:
        lista = reverse("infra_enlaces")
        itens.append(_item_de_atencao(
            "Infraestrutura", "Enlaces indisponíveis", infra["indisponiveis"], lista + "?estado=indisponivel",
            "estado registrado por quem opera", "erro",
        ))
        itens.append(_item_de_atencao(
            "Infraestrutura", "Enlaces em manutenção", infra["manutencao"], lista + "?estado=manutencao", "estado registrado por quem opera",
        ))
        itens.append(_item_de_atencao(
            "Infraestrutura", "Enlaces sem estado", infra["nao_informados"], lista + "?estado=nao_informado",
            "ninguém registrou o estado ainda",
        ))
        itens.append(_item_de_atencao(
            "Infraestrutura", "Estruturas sem coordenada", infra["sem_coordenada"], reverse("infra_estruturas") + "?coordenada=sem",
            "não aparecem no mapa",
        ))
    if acesso.get("historico") and alertas and ver_api:
        itens.append(_item_de_atencao(
            "Conectividade", "Alertas em aberto", alertas, reverse("m2m_alertas"), "abertos ou em tratamento",
        ))
    if acesso.get("frota"):
        pendentes = FrotaVeiculo.objects.filter(validacao_pendente=True).count()
        itens.append(_item_de_atencao(
            "Frotas", "Aguardando validação", pendentes, reverse("frota_visao_geral") + "?validacao=pendente",
            "vieram de importação e ninguém confirmou a situação",
        ))
    ordem = {"erro": 0, "atencao": 1}
    return sorted((i for i in itens if i["n"]), key=lambda i: ordem.get(i["tom"], 2))


def painel_da_porta(acesso: dict, dias: int, agora=None) -> dict:
    """Tudo que o dashboard da porta do Agro lê, em um dict. `acesso` = `{"frota", "conectividade", "chips",
    "historico", "infra", "api"}` (booleanos, as permissões da pessoa): cada módulo só é consultado para quem o enxerga, e a
    chave volta `None` quando não é. `api` (padrão: verdadeiro, para quem chama sem ele) é o corte da operadora: sem ele, os
    indicadores de chip são só cadastro e vínculo, e consumo, sincronização, desatualizados e alertas voltam `None`. Os números vêm dos MESMOS services das telas de cada módulo — nada é recalculado
    aqui, então o cartão e a lista que ele abre não discordam.

        indicadores    `{"frota", "conectividade"}` de `frota_service.indicadores_frota` (ou None)
        frota          `painel_da_frota()` + `cadastro` (barras do que falta no cadastro) (ou None)
        movimentacoes  as últimas movimentações de equipamento
        consumo, maiores   a série diária e os chips que mais consumiram no período (só com `chips`)
        sincronizacao  `estado_sincronizacao` (só com `chips`)
        infra, infra_estados   `indicadores_infra` e os enlaces por estado registrado (barras)
        alertas_abertos
        atencao        `atencao_da_porta`
        vazio          True quando a pessoa não enxerga módulo nenhum"""
    from services import agro_infra_service as infra
    from services import frota_service

    agora = agora or timezone.now()
    ver_frota, ver_conect, ver_chips, ver_hist, ver_infra = (
        bool(acesso.get(k)) for k in ("frota", "conectividade", "chips", "historico", "infra")
    )
    ver_api = bool(acesso.get("api", True))
    indicadores = frota_service.indicadores_frota(agora, com_api=ver_api) if (ver_frota or ver_conect) else None
    ind_infra = infra.indicadores_infra() if ver_infra else None
    alertas = m2m.contar_alertas_abertos() if (ver_hist and ver_api) else None

    frota = None
    if ver_frota:
        frota = painel_da_frota()
        lista = reverse("frota_visao_geral")
        faltas = frota["incompletos"]
        frota["cadastro"] = barras([
            ("Sem localização no mapa", faltas["sem_localizacao"], f"{lista}?localizacao=sem"),
            ("Sem fazenda", faltas["sem_fazenda"], None),
            ("Sem modelo", faltas["sem_modelo"], None),
            ("Sem fabricante", faltas["sem_fabricante"], None),
        ])
        frota["resumo_mapa"] = (
            f"{frota['com_localizacao']} de {frota['total']} equipamentos têm coordenada cadastrada e aparecem no mapa."
            if frota["total"] else ""
        )

    estados = None
    if ind_infra:
        lista_enlaces = reverse("infra_enlaces")
        estados = barras([
            (rotulo, ind_infra["enlaces_por_estado"].get(valor, 0), f"{lista_enlaces}?estado={valor}")
            for valor, rotulo in TecEnlace.Estado.choices
        ])

    chips_da_api = ver_chips and ver_api
    desatualizados = m2m.indicadores(agora)["desatualizados"] if chips_da_api else None
    kpis_frota = indicadores["frota"] if (indicadores and ver_frota) else None
    kpis_conect = indicadores["conectividade"] if (indicadores and ver_conect) else None
    sincronizacao = m2m.estado_sincronizacao(agora) if chips_da_api else None
    return {
        "vazio": not (ver_frota or ver_conect or ver_chips or ver_hist or ver_infra),
        "indicadores": indicadores,
        "kpis_frota": kpis_frota,
        "kpis_conectividade": kpis_conect,
        "frota": frota,
        "movimentacoes": movimentacoes_recentes_da_frota() if ver_frota else None,
        "consumo": serie_de_consumo(dias, agora=agora) if chips_da_api else None,
        "maiores": maiores_consumos(dias, agora=agora) if chips_da_api else None,
        "sincronizacao": sincronizacao,
        "sincronizacao_pede_atencao": bool(sincronizacao and sincronizacao["tom"] in ("erro", "atencao")),
        "n_resumos": sum(1 for k in (kpis_frota, kpis_conect, ind_infra) if k),
        "infra": ind_infra,
        "infra_estados": estados,
        "alertas_abertos": alertas,
        "atencao": atencao_da_porta(
            acesso, kpis_conectividade=kpis_conect, desatualizados=desatualizados, infra=ind_infra, alertas=alertas,
        ),
    }
