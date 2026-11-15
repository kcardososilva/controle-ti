"""
preventiva_meta_service.py — metas de manutenção preventiva por área e ciclo.

Fonte ÚNICA da apuração. O painel gerencial, o Plano de Agendamento, a tela de
configuração e os testes leem daqui; nenhuma view recalcula previsto/realizado
por conta própria (foi assim que o módulo de licenças acabou com quatro
implementações da mesma regra — CLAUDE.md, regra 44).

Três conceitos, e eles não se misturam:

  • CLASSIFICAÇÃO  — `Item.area_operacional`: a qual área o ativo pertence.
  • RESPONSABILIDADE — `PreventivaAreaResponsavel`: de quem é aquele parque.
  • CICLO          — `PreventivaCicloMeta`: a janela de 90 dias em que o parque
                     inteiro do subtipo tem de ser executado uma vez.

A tela "Responsáveis por Área" também lê daqui: `parque_por_responsavel` monta,
para cada técnico, a lista do que ele responde (mesma regra de
`responsaveis_map`, mesma apuração de `apurar_ciclo`; só leitura).

A meta NÃO é um placar gravado. Previstos, realizados e pendentes saem de
`PreventivaExecucao` a cada leitura: gravar o resultado criaria uma segunda
verdade que fica errada no instante em que alguém corrige uma execução, e o
gestor passaria a ter dois números para a mesma pergunta.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import date, timedelta

from django.db.models import Q
from django.utils import timezone


# Tolerância, em pontos percentuais, entre o % executado e o % do prazo já
# decorrido antes de a área ser apontada como atrasada. Sem folga nenhuma,
# qualquer dia sem execução acenderia alerta — e preventiva é trabalho que
# acontece em blocos, não linearmente.
_TOLERANCIA_RITMO = 10

# Faixa final do ciclo em que pendência deixa de ser "atraso" e passa a ser
# "risco de não cumprimento": com menos de 1/4 do prazo restante não há mais
# tempo de recuperar ritmo sem mutirão.
_FRACAO_RETA_FINAL = 0.25

# Janela, em dias, para casar a SAÍDA de um equipamento de operação com a
# ENTRADA de outro no mesmo lugar e considerar que houve TROCA. Sete dias
# cobrem o caso real (o switch queima, o reserva sobe no mesmo dia ou na
# semana) sem casar equipamentos que mudaram por motivos independentes.
_JANELA_TROCA_DIAS = 7


# =========================================================
# ELEGIBILIDADE — quem entra no universo da meta
# =========================================================

def universo_qs(subtipo_id: int, area: str | None = None):
    """
    Equipamentos elegíveis à meta: do subtipo do ciclo, marcados como
    "precisa preventiva", ATIVOS e já classificados por área.

    As quatro condições têm motivo:

      • `precisa_preventiva='sim'` — é o cadastro que diz que o ativo entra no
        programa; cobrar preventiva de quem está fora dele inflaria o
        denominador com equipamento que ninguém deveria visitar.
      • `status='ativo'` — item em backup, defeito, manutenção ou pausado tem
        a contagem CONGELADA pelo próprio modelo (`Preventiva.pausar`). Contar
        como previsto cobraria do técnico uma visita a equipamento que está na
        prateleira. Eles aparecem no painel em bloco próprio ("fora do ciclo").
      • `area_operacional` preenchida — sem área não há responsável nem
        comparativo; o ativo aparece como pendência de CADASTRO, não de
        execução.
      • `excluido=False` — garantido pelo `ItemManager` padrão.
    """
    from ProjetoEstoque.models import Item, SimNaoChoices, StatusItemChoices

    qs = (
        Item.objects
        .filter(
            subtipo_id=subtipo_id,
            precisa_preventiva=SimNaoChoices.SIM,
            status=StatusItemChoices.ATIVO,
            area_operacional__isnull=False,
        )
        .exclude(area_operacional="")
    )
    if area:
        qs = qs.filter(area_operacional=area)
    return qs


def fora_do_ciclo_qs(subtipo_id: int):
    """
    Equipamentos do subtipo que precisam de preventiva mas NÃO entram no ciclo
    porque não estão ativos. Existe para o painel poder dizer por que o
    denominador é menor do que o parque total — número que cai sem explicação
    é a primeira coisa que o gestor desconfia.
    """
    from ProjetoEstoque.models import Item, SimNaoChoices, StatusItemChoices

    return (
        Item.objects
        .filter(subtipo_id=subtipo_id, precisa_preventiva=SimNaoChoices.SIM)
        .exclude(status=StatusItemChoices.ATIVO)
    )


def nao_classificados_qs(subtipo_id: int):
    """Ativos do subtipo que entrariam na meta, mas estão sem área definida."""
    from ProjetoEstoque.models import Item, SimNaoChoices, StatusItemChoices

    return (
        Item.objects
        .filter(
            subtipo_id=subtipo_id,
            precisa_preventiva=SimNaoChoices.SIM,
            status=StatusItemChoices.ATIVO,
        )
        .filter(Q(area_operacional__isnull=True) | Q(area_operacional=""))
    )


# =========================================================
# MOVIMENTAÇÃO DO PARQUE DENTRO DO CICLO
# =========================================================
#
# Equipamento parado não acumula uso, e o sistema já congela a contagem dele
# (`Preventiva.pausar`). Mas o parque se MEXE durante os 90 dias: o switch
# queima e o reserva sobe no lugar. Sem olhar para isso, o ativo que entrou em
# operação no dia 70 é cobrado como se estivesse ali desde o dia 1 — e o
# técnico aparece atrasado por trabalho que não existia quando o ciclo abriu.
#
# A fonte é o `ItemStatusHistorico`, gravado por signal a cada mudança de
# status do Item (991 linhas na base). `Preventiva.data_reativacao` existe e
# diz a mesma coisa, mas só é preenchido quando a preventiva já existe — tem 2
# linhas na base contra 991 do histórico, e usar o campo escasso deixaria a
# maior parte das entradas invisível.


def entradas_em_operacao(subtipo_id: int, ciclo) -> dict:
    """
    {item_id: data} para cada equipamento do subtipo que ENTROU em operação
    dentro da janela do ciclo. Guarda a entrada MAIS RECENTE de cada um: se o
    ativo oscilou (ativo → defeito → ativo), o que vale para a cobrança é a
    última vez que ele ficou disponível.
    """
    from ProjetoEstoque.models import ItemStatusHistorico, StatusItemChoices

    linhas = (
        ItemStatusHistorico.objects
        .filter(
            item__subtipo_id=subtipo_id,
            status_novo=StatusItemChoices.ATIVO,
            alterado_em__date__gte=ciclo.data_inicio,
            alterado_em__date__lte=ciclo.data_fim,
        )
        .exclude(status_anterior="")          # criação do cadastro não é "entrada"
        .values_list("item_id", "alterado_em")
        .order_by("item_id", "-alterado_em")
    )
    entradas = {}
    for item_id, quando in linhas:
        if item_id not in entradas:
            entradas[item_id] = timezone.localtime(quando).date()
    return entradas


def saidas_de_operacao(subtipo_id: int, ciclo) -> list:
    """
    Equipamentos que SAÍRAM de operação durante o ciclo (de `ativo` para
    qualquer outro status). Eles deixam o universo da meta — é o que a regra
    pede —, mas o painel precisa dizer que saíram, senão o denominador encolhe
    sem explicação no meio do ciclo.
    """
    from ProjetoEstoque.models import ItemStatusHistorico, StatusItemChoices

    linhas = (
        ItemStatusHistorico.objects
        .filter(
            item__subtipo_id=subtipo_id,
            status_anterior=StatusItemChoices.ATIVO,
            alterado_em__date__gte=ciclo.data_inicio,
            alterado_em__date__lte=ciclo.data_fim,
        )
        .exclude(status_novo=StatusItemChoices.ATIVO)
        .select_related("item", "item__localidade")
        .order_by("-alterado_em")
    )
    vistos, saidas = set(), []
    for linha in linhas:
        if linha.item_id in vistos:
            continue
        vistos.add(linha.item_id)
        saidas.append({
            "item": linha.item,
            "data": timezone.localtime(linha.alterado_em).date(),
            "status": linha.status_novo,
            "status_label": dict(StatusItemChoices.choices).get(
                linha.status_novo, linha.status_novo
            ),
            "localidade_id": linha.item.localidade_id,
        })
    return saidas


def pares_de_troca(entradas_itens: list, saidas: list) -> list:
    """
    Casa cada ENTRADA com a SAÍDA que ela provavelmente substituiu.

    O critério é físico: mesma LOCALIDADE e no máximo `_JANELA_TROCA_DIAS` de
    distância. É a assinatura da troca real — o reserva vai para o lugar onde
    o equipamento falhou. Sem a localidade, duas mudanças independentes na
    mesma semana virariam uma "troca" inventada, e o painel passaria a afirmar
    algo que não aconteceu.

    `entradas_itens`: [(item, data_entrada)] dos itens que entraram.
    Devolve os pares e marca a saída como consumida (cada saída casa uma vez).
    """
    disponiveis = list(saidas)
    pares = []
    for item, entrada in sorted(entradas_itens, key=lambda t: t[1]):
        melhor, melhor_dist = None, None
        for saida in disponiveis:
            if saida.get("_usada"):
                continue
            # Ninguém substitui a si mesmo. Um ativo que OSCILA dentro da
            # janela (ativo → backup → ativo, ou manutenção e volta) produz uma
            # saída E uma entrada, na mesma localidade e a poucos dias de
            # distância: sem este teste ele casaria consigo e o painel
            # afirmaria "SW-X substituiu SW-X".
            if saida["item"].pk == item.pk:
                continue
            if saida["localidade_id"] != item.localidade_id:
                continue
            dist = abs((entrada - saida["data"]).days)
            if dist > _JANELA_TROCA_DIAS:
                continue
            if melhor_dist is None or dist < melhor_dist:
                melhor, melhor_dist = saida, dist
        if melhor is not None:
            melhor["_usada"] = True
            pares.append({
                "entrou": item,
                "entrou_em": entrada,
                "saiu": melhor["item"],
                "saiu_em": melhor["data"],
                "saiu_para": melhor["status_label"],
                "dias": melhor_dist,
                "localidade": getattr(item.localidade, "local", None),
            })
    return pares


def _dias_em_operacao(entrada, ciclo, hoje) -> int:
    """
    Dias que o equipamento esteve DISPONÍVEL dentro do ciclo, até hoje.

    É o que substitui "dias corridos do ciclo" na hora de julgar se o técnico
    está atrasado: cobrar 52% de execução de um switch que subiu ontem é
    cobrar trabalho que não existia.
    """
    fim = min(hoje, ciclo.data_fim)
    if fim < ciclo.data_inicio:
        return 0
    inicio = max(entrada, ciclo.data_inicio) if entrada else ciclo.data_inicio
    if inicio > fim:
        return 0
    return (fim - inicio).days + 1


# =========================================================
# RESPONSABILIDADE
# =========================================================

def responsaveis_map(subtipo_id: int | None = None) -> dict:
    """
    {area: User} para o subtipo informado, já resolvendo o padrão da área.

    A linha específica do subtipo vence a linha padrão (`subtipo` nulo). Uma
    consulta só: a tela de metas precisa disso para as duas áreas e chamar por
    área daria duas idas ao banco por página.
    """
    from ProjetoEstoque.models import PreventivaAreaResponsavel

    qs = (
        PreventivaAreaResponsavel.objects
        .filter(ativo=True)
        .filter(Q(subtipo__isnull=True) | Q(subtipo_id=subtipo_id))
        .select_related("tecnico")
    )
    padrao, especifico = {}, {}
    for linha in qs:
        if linha.subtipo_id is None:
            padrao[linha.area] = linha.tecnico
        else:
            especifico[linha.area] = linha.tecnico
    return {**padrao, **especifico}


def responsavel_do_item(item) -> "object | None":
    """
    Técnico responsável por um equipamento, pela área + subtipo dele.
    Usado pelo Plano de Agendamento para pré-selecionar o técnico.
    """
    if not getattr(item, "area_operacional", None):
        return None
    return responsaveis_map(item.subtipo_id).get(item.area_operacional)


# =========================================================
# CICLOS
# =========================================================

def ciclo_vigente(subtipo_id: int, hoje: date | None = None):
    """Ciclo que cobre a data de hoje para o subtipo, ou None."""
    from ProjetoEstoque.models import PreventivaCicloMeta

    hoje = hoje or timezone.localdate()
    for ciclo in PreventivaCicloMeta.objects.filter(
        subtipo_id=subtipo_id, data_inicio__lte=hoje
    ).order_by("-data_inicio"):
        if ciclo.contem(hoje):
            return ciclo
    return None


def ciclos_do_subtipo(subtipo_id: int, limite: int | None = None):
    """Ciclos do subtipo, do mais recente para o mais antigo."""
    from ProjetoEstoque.models import PreventivaCicloMeta

    qs = (
        PreventivaCicloMeta.objects
        .filter(subtipo_id=subtipo_id)
        .select_related("subtipo")
        .order_by("-data_inicio")
    )
    return list(qs[:limite] if limite else qs)


def proximo_inicio(subtipo_id: int, hoje: date | None = None) -> date:
    """
    Data em que o próximo ciclo do subtipo deve começar.

    Encadeia no dia seguinte ao fim do último ciclo, para que não exista lacuna
    entre ciclos — dia descoberto é execução que não conta para meta nenhuma.
    Se o último ciclo terminou há tempos (ou não há nenhum), começa hoje.
    """
    from ProjetoEstoque.models import PreventivaCicloMeta

    hoje = hoje or timezone.localdate()
    ultimo = (
        PreventivaCicloMeta.objects
        .filter(subtipo_id=subtipo_id)
        .order_by("-data_inicio")
        .first()
    )
    if not ultimo:
        return hoje
    seguinte = ultimo.data_fim + timedelta(days=1)
    return seguinte if seguinte > hoje else hoje


def abrir_ciclo(subtipo_id: int, data_inicio: date | None = None,
                duracao_dias: int | None = None, usuario=None):
    """
    Cria o ciclo de referência do subtipo. Valida sobreposição via
    `full_clean()` (a regra mora no model, regra 2 do CLAUDE.md).
    """
    from ProjetoEstoque.models import PreventivaCicloMeta

    ciclo = PreventivaCicloMeta(
        subtipo_id=subtipo_id,
        data_inicio=data_inicio or proximo_inicio(subtipo_id),
        duracao_dias=duracao_dias or PreventivaCicloMeta.DURACAO_PADRAO,
        criado_por=usuario,
        atualizado_por=usuario,
    )
    ciclo.full_clean()
    ciclo.save()
    return ciclo


def impacto_de_editar_ciclo(ciclo, nova_inicio: date, nova_duracao: int) -> dict:
    """
    O que a edição de um ciclo faria com as execuções JÁ contadas.

    Mexer na janela não é mexer num cadastro: é mexer no placar. Encurtar um
    ciclo pode jogar execuções reais para fora da apuração — a preventiva
    continua no histórico, mas deixa de pagar a meta, e o percentual do técnico
    cai sem que ninguém tenha desfeito trabalho nenhum. Esta função calcula
    isso ANTES de salvar, para a tela poder avisar em vez de surpreender.

    Devolve `entram` e `saem` (contagem de equipamentos que passam a contar ou
    deixam de contar) e a janela resultante.
    """
    from datetime import timedelta as _td

    from ProjetoEstoque.models import PreventivaExecucao

    nova_fim = nova_inicio + _td(days=int(nova_duracao) - 1)
    item_ids = list(universo_qs(ciclo.subtipo_id).values_list("pk", flat=True))
    if not item_ids:
        return {"entram": 0, "saem": 0, "data_fim": nova_fim, "muda_placar": False}

    def _cobertos(inicio, fim):
        return set(
            PreventivaExecucao.objects
            .filter(
                preventiva__equipamento_id__in=item_ids,
                data_execucao__gte=inicio,
                data_execucao__lte=fim,
            )
            .values_list("preventiva__equipamento_id", flat=True)
        )

    antes = _cobertos(ciclo.data_inicio, ciclo.data_fim)
    depois = _cobertos(nova_inicio, nova_fim)
    saem = antes - depois
    entram = depois - antes
    return {
        "entram": len(entram),
        "saem": len(saem),
        "data_fim": nova_fim,
        "muda_placar": bool(saem or entram),
    }


def editar_ciclo(ciclo, data_inicio: date, duracao_dias: int,
                 observacao: str | None = None, usuario=None):
    """
    Altera a janela do ciclo. O `full_clean()` do model continua sendo quem
    recusa sobreposição — aqui não existe atalho que escape dessa validação.
    """
    ciclo.data_inicio = data_inicio
    ciclo.duracao_dias = int(duracao_dias)
    if observacao is not None:
        ciclo.observacao = observacao
    ciclo.atualizado_por = usuario
    ciclo.full_clean()
    ciclo.save(update_fields=[
        "data_inicio", "duracao_dias", "observacao", "atualizado_por", "updated_at",
    ])
    return ciclo


# =========================================================
# APURAÇÃO
# =========================================================

def _pct(parte: int, total: int) -> int:
    return round(parte / total * 100) if total else 0


def _nome(user) -> str:
    if not user:
        return "Sem responsável"
    return user.get_full_name() or user.username


def _risco(pct_exec: int, pct_tempo: int, pendentes: int, dias_restantes: int,
           duracao: int, situacao: str, previstos: int | None = None) -> tuple[str, str]:
    """
    Classifica a área em (chave, rótulo). A leitura é sempre execução CONTRA
    prazo decorrido: 40% executado é excelente no dia 20 e desastre no dia 85.
    """
    # Parque vazio NÃO é meta cumprida. Sem este teste, uma área sem nenhum
    # equipamento classificado exibia "Meta concluída" com 0 de 0 — e o cartão
    # dizia, duas linhas abaixo, que não havia equipamento nenhum ali.
    if previstos == 0:
        return "sem_parque", "Sem equipamentos"
    if pendentes == 0:
        return "concluido", "Meta concluída"
    if situacao == "futuro":
        return "aguardando", "Ciclo não iniciado"
    if situacao == "encerrado":
        return "nao_cumprida", "Encerrado sem concluir"
    if dias_restantes <= max(1, round(duracao * _FRACAO_RETA_FINAL)):
        return "risco", "Risco de não cumprir"
    if pct_exec + _TOLERANCIA_RITMO < pct_tempo:
        return "atrasado", "Abaixo do ritmo"
    return "em_dia", "Dentro do ritmo"


def _buckets_evolucao(ciclo, hoje: date) -> list[dict]:
    """
    Semanas do ciclo, para a curva de evolução. Semana — e não dia — porque 90
    pontos num gráfico de 400px viram uma mancha, e preventiva é medida em
    ritmo semanal pelo gestor.
    """
    buckets = []
    cursor = ciclo.data_inicio
    n = 1
    while cursor <= ciclo.data_fim:
        fim = min(cursor + timedelta(days=6), ciclo.data_fim)
        buckets.append({
            "n": n,
            "inicio": cursor,
            "fim": fim,
            "label": f"S{n}",
            "titulo": f"Semana {n} · {cursor:%d/%m} a {fim:%d/%m}",
            "decorrida": cursor <= hoje,
        })
        cursor = fim + timedelta(days=1)
        n += 1
    return buckets


def apurar_ciclo(ciclo, hoje: date | None = None) -> dict:
    """
    Apura a meta de um ciclo e devolve tudo o que o painel precisa.

    Uma passada no banco por tipo de dado (itens, execuções, preventivas,
    usuários) e o resto em memória: o painel abre com 5 consultas,
    independentemente do tamanho do parque.

    Crédito ao técnico:
      • REALIZADO  → quem executou (`PreventivaExecucao.tecnico`, ou o autor do
        registro quando o snapshot está vazio). É o mesmo critério da tela de
        Desempenho dos Técnicos — divergir ali faria as duas telas brigarem.
      • PENDENTE   → o técnico atribuído na preventiva; sem atribuição, o
        responsável da área. É assim que a pendência tem dono antes de o
        agendamento existir.
    """
    from django.contrib.auth import get_user_model

    from ProjetoEstoque.models import (
        AreaOperacionalChoices,
        Preventiva,
        PreventivaExecucao,
    )

    hoje = hoje or timezone.localdate()
    AuthUser = get_user_model()

    situacao = ciclo.situacao(hoje)
    duracao = int(ciclo.duracao_dias)
    dias_decorridos = ciclo.dias_decorridos(hoje)
    dias_restantes = ciclo.dias_restantes(hoje)
    pct_tempo = ciclo.pct_tempo(hoje)

    responsaveis = responsaveis_map(ciclo.subtipo_id)

    # Movimentação do parque DENTRO do ciclo (regra 2/3 do pedido): quem entrou
    # em operação no meio da janela não pode ser cobrado como se estivesse ali
    # desde o primeiro dia.
    entradas = entradas_em_operacao(ciclo.subtipo_id, ciclo)
    saidas = saidas_de_operacao(ciclo.subtipo_id, ciclo)

    itens = list(
        universo_qs(ciclo.subtipo_id)
        .select_related("localidade", "centro_custo", "subtipo")
        .order_by("area_operacional", "nome")
    )
    item_ids = [i.pk for i in itens]

    # ── Execuções do ciclo (mais recente por equipamento) ──────────────────
    exec_por_item: dict[int, dict] = {}
    execucoes_cronologicas: list[dict] = []
    if item_ids:
        linhas = (
            PreventivaExecucao.objects
            .filter(
                preventiva__equipamento_id__in=item_ids,
                data_execucao__gte=ciclo.data_inicio,
                data_execucao__lte=ciclo.data_fim,
            )
            .values(
                "id", "data_execucao", "tecnico_id", "criado_por_id",
                "preventiva__equipamento_id",
            )
            .order_by("preventiva__equipamento_id", "-data_execucao", "-id")
        )
        for linha in linhas:
            iid = linha["preventiva__equipamento_id"]
            if iid not in exec_por_item:
                exec_por_item[iid] = linha
            execucoes_cronologicas.append(linha)

    # ── Preventiva mais urgente por equipamento (dono da pendência) ────────
    prev_por_item: dict[int, dict] = {}
    if item_ids:
        for linha in (
            Preventiva.objects
            .filter(equipamento_id__in=item_ids)
            .values("equipamento_id", "tecnico_id", "data_agendamento",
                    "data_proxima", "pausada", "id")
            .order_by("equipamento_id", "data_proxima", "id")
        ):
            prev_por_item.setdefault(linha["equipamento_id"], linha)

    # ── Usuários citados, numa consulta ────────────────────────────────────
    uids = set()
    for linha in exec_por_item.values():
        uids.add(linha["tecnico_id"] or linha["criado_por_id"])
    for linha in prev_por_item.values():
        uids.add(linha["tecnico_id"])
    uids.update(u.pk for u in responsaveis.values() if u)
    uids.discard(None)
    users = {u.pk: u for u in AuthUser.objects.filter(pk__in=uids)} if uids else {}

    # ── Distribuição por área e por técnico ───────────────────────────────
    areas: "OrderedDict[str, dict]" = OrderedDict()
    for chave, label in AreaOperacionalChoices.choices:
        resp = responsaveis.get(chave)
        areas[chave] = {
            "area": chave,
            "label": label,
            "label_curto": "Produtiva" if chave == AreaOperacionalChoices.PRODUTIVA else "Não Produtiva",
            "tecnico": resp,
            "tecnico_nome": _nome(resp),
            "tecnico_id": resp.pk if resp else None,
            "previstos": 0,
            "realizados": 0,
            "pendentes": 0,
            "entraram": 0,
            "_dias_operacao": 0,
            "itens_pendentes": [],
            "itens_realizados": [],
        }

    por_tecnico: dict[int | None, dict] = {}

    def _tec_bucket(uid, user):
        if uid not in por_tecnico:
            por_tecnico[uid] = {
                "id": uid,
                "nome": _nome(user),
                "previstos": 0,
                "realizados": 0,
                "pendentes": 0,
                "areas": set(),
            }
        return por_tecnico[uid]

    entradas_itens = []        # [(item, data)] — alimenta o casamento de trocas

    for item in itens:
        area = areas.get(item.area_operacional)
        if area is None:          # área fora do enum (dado antigo) — não inventa bucket
            continue
        area["previstos"] += 1

        # Disponibilidade do ativo dentro do ciclo. Para quem esteve ativo o
        # tempo todo isto é igual aos dias decorridos do ciclo; para quem subiu
        # no meio, é só o tempo em que de fato deu para visitá-lo.
        item.meta_entrou_em = entradas.get(item.pk)
        item.meta_novo_no_ciclo = item.meta_entrou_em is not None
        item.meta_dias_operacao = _dias_em_operacao(item.meta_entrou_em, ciclo, hoje)
        area["_dias_operacao"] += item.meta_dias_operacao
        if item.meta_novo_no_ciclo:
            area["entraram"] += 1
            entradas_itens.append((item, item.meta_entrou_em))

        execucao = exec_por_item.get(item.pk)
        if execucao:
            uid = execucao["tecnico_id"] or execucao["criado_por_id"]
            item.meta_data_execucao = execucao["data_execucao"]
            item.meta_tecnico_nome = _nome(users.get(uid))
            area["realizados"] += 1
            area["itens_realizados"].append(item)
        else:
            prev = prev_por_item.get(item.pk) or {}
            uid = prev.get("tecnico_id") or (area["tecnico"].pk if area["tecnico"] else None)
            item.meta_tecnico_nome = _nome(users.get(uid))
            item.meta_data_agendamento = prev.get("data_agendamento")
            item.meta_data_proxima = prev.get("data_proxima")
            item.meta_sem_preventiva = not prev
            item.meta_agendamento_vencido = bool(
                prev.get("data_agendamento") and prev["data_agendamento"] < hoje
            )
            area["pendentes"] += 1
            area["itens_pendentes"].append(item)

        bucket = _tec_bucket(uid, users.get(uid))
        bucket["previstos"] += 1
        bucket["areas"].add(area["label_curto"])
        bucket["realizados" if execucao else "pendentes"] += 1

    # ── Percentuais, ritmo e risco por área ───────────────────────────────
    for area in areas.values():
        prev_total = area["previstos"]
        area["pct"] = _pct(area["realizados"], prev_total)
        area["pct_pendente"] = (100 - area["pct"]) if prev_total else 0

        # EXPECTATIVA: quanto já deveria estar executado, considerando a
        # disponibilidade real de cada ativo — não o calendário puro. Área sem
        # nenhuma entrada no meio do ciclo cai exatamente em `pct_tempo`, então
        # o caso comum não muda; a diferença aparece só quando houve troca.
        area["expectativa"] = (
            min(100, round(area["_dias_operacao"] / (prev_total * duracao) * 100))
            if prev_total and duracao else 0
        )
        area["expectativa_ajustada"] = area["expectativa"] != pct_tempo
        area.pop("_dias_operacao", None)
        area["ritmo_necessario"] = (
            round(area["pendentes"] / dias_restantes, 2) if dias_restantes and area["pendentes"] else 0
        )
        area["ritmo_semanal"] = (
            round(area["pendentes"] / dias_restantes * 7, 1) if dias_restantes and area["pendentes"] else 0
        )
        area["risco"], area["risco_label"] = _risco(
            area["pct"], area["expectativa"], area["pendentes"], dias_restantes,
            duracao, situacao, prev_total,
        )
        # Equipamentos que faltam: a lista completa alimenta o painel, mas a
        # tela mostra as primeiras e oferece o restante — 24 linhas de tabela
        # dentro de um cartão de comparativo afogam o número que importa.
        area["itens_pendentes_topo"] = area["itens_pendentes"][:8]
        area["pendentes_restantes"] = max(0, area["pendentes"] - 8)

    lista_areas = list(areas.values())

    # ── Totais do ciclo ───────────────────────────────────────────────────
    previstos = sum(a["previstos"] for a in lista_areas)
    realizados = sum(a["realizados"] for a in lista_areas)
    pendentes = previstos - realizados
    pct_total = _pct(realizados, previstos)
    entraram_total = sum(a["entraram"] for a in lista_areas)
    # Expectativa do ciclo: média das áreas ponderada pelo tamanho do parque,
    # para a leitura do topo não contradizer a dos dois cartões.
    expectativa_total = (
        round(sum(a["expectativa"] * a["previstos"] for a in lista_areas) / previstos)
        if previstos else 0
    )
    risco_total, risco_total_label = _risco(
        pct_total, expectativa_total, pendentes, dias_restantes, duracao,
        situacao, previstos,
    )

    # ── Desempenho por técnico ────────────────────────────────────────────
    tecnicos = []
    for bucket in por_tecnico.values():
        bucket["pct"] = _pct(bucket["realizados"], bucket["previstos"])
        bucket["pct_pendente"] = 100 - bucket["pct"] if bucket["previstos"] else 0
        bucket["areas_label"] = " · ".join(sorted(bucket["areas"])) or "—"
        bucket.pop("areas", None)
        tecnicos.append(bucket)
    tecnicos.sort(key=lambda t: (-t["pct"], -t["realizados"], t["nome"]))

    # ── Evolução semanal (acumulada) vs. meta linear ──────────────────────
    semanas = _buckets_evolucao(ciclo, hoje)
    por_area_semana = {a["area"]: [0] * len(semanas) for a in lista_areas}
    area_do_item = {i.pk: i.area_operacional for i in itens}
    vistos = set()
    for linha in sorted(execucoes_cronologicas, key=lambda r: (r["data_execucao"], r["id"])):
        iid = linha["preventiva__equipamento_id"]
        if iid in vistos:
            continue            # conta o ativo UMA vez: a meta é cobertura, não volume
        vistos.add(iid)
        area = area_do_item.get(iid)
        if area not in por_area_semana:
            continue
        for idx, semana in enumerate(semanas):
            if semana["inicio"] <= linha["data_execucao"] <= semana["fim"]:
                por_area_semana[area][idx] += 1
                break

    evolucao = {
        "labels": [s["label"] for s in semanas],
        "titulos": [s["titulo"] for s in semanas],
        "decorridas": [s["decorrida"] for s in semanas],
        "series": [],
        "ritmo_ideal": [],
    }
    for area in lista_areas:
        prev_total = area["previstos"]
        acumulado, soma = [], 0
        for valor in por_area_semana[area["area"]]:
            soma += valor
            acumulado.append(soma)
        evolucao["series"].append({
            "area": area["area"],
            "label": area["label_curto"],
            "previstos": area["previstos"],
            # Contagem acumulada — lida pelo tooltip ("7 de 42"). O gráfico
            # desenha o percentual, mas o número exato sai daqui em vez de ser
            # reconstruído a partir do % arredondado.
            "acumulado": acumulado,
            # A curva é em PORCENTAGEM da meta da própria área, não em
            # contagem: as duas áreas têm parques de tamanhos diferentes (42
            # contra 36 switches), e comparar contagens puniria a área maior
            # por ser maior. Em % as duas dividem o eixo 0–100 e a linha de
            # ritmo ideal passa a valer para as duas.
            #
            # Semana futura vai como `null`: linha cheia até a semana 13 faria
            # o ciclo em andamento parecer estagnado.
            "acumulado_pct": [
                (_pct(v, prev_total) if semanas[i]["decorrida"] else None)
                for i, v in enumerate(acumulado)
            ],
        })
    if semanas:
        # Referência (não é série de dados): onde a execução deveria estar em
        # cada semana para fechar o ciclo no prazo. Vai tracejada e recessiva.
        passo_pct = 100 / len(semanas)
        evolucao["ritmo_ideal"] = [
            round(passo_pct * (i + 1), 1) for i in range(len(semanas))
        ]

    # ── Pendências de cadastro e parque fora do ciclo ──────────────────────
    nao_classificados = list(
        nao_classificados_qs(ciclo.subtipo_id)
        .select_related("localidade")
        .order_by("nome")[:50]
    )
    nao_classificados_total = nao_classificados_qs(ciclo.subtipo_id).count()
    fora_total = fora_do_ciclo_qs(ciclo.subtipo_id).count()

    # Trocas: entrada casada com saída no mesmo lugar, dentro da janela.
    trocas = pares_de_troca(entradas_itens, saidas)
    ids_trocados = {p["saiu"].pk for p in trocas}
    saidas_sem_par = [s for s in saidas if s["item"].pk not in ids_trocados]

    return {
        "ciclo": ciclo,
        "subtipo": ciclo.subtipo,
        "hoje": hoje,
        "situacao": situacao,
        "situacao_label": {
            "vigente": "Ciclo em andamento",
            "futuro": "Ciclo não iniciado",
            "encerrado": "Ciclo encerrado",
        }[situacao],
        "duracao": duracao,
        "dias_decorridos": dias_decorridos,
        "dias_restantes": dias_restantes,
        "pct_tempo": pct_tempo,
        "expectativa": expectativa_total,
        "expectativa_ajustada": expectativa_total != pct_tempo,
        "entraram": entraram_total,
        "trocas": trocas,
        "saidas": saidas_sem_par,
        "saidas_total": len(saidas),
        "previstos": previstos,
        "realizados": realizados,
        "pendentes": pendentes,
        "pct": pct_total,
        "pct_pendente": (100 - pct_total) if previstos else 0,
        "risco": risco_total,
        "risco_label": risco_total_label,
        "ritmo_necessario": round(pendentes / dias_restantes, 2) if dias_restantes and pendentes else 0,
        "ritmo_semanal": round(pendentes / dias_restantes * 7, 1) if dias_restantes and pendentes else 0,
        "areas": lista_areas,
        "tecnicos": tecnicos,
        "evolucao": evolucao,
        "nao_classificados": nao_classificados,
        "nao_classificados_total": nao_classificados_total,
        "fora_do_ciclo_total": fora_total,
    }


def resumo_ciclo(ciclo, hoje: date | None = None) -> dict:
    """
    Versão enxuta da apuração, para listar ciclos anteriores sem montar as
    listas de equipamentos e a curva de evolução de cada um.
    """
    from ProjetoEstoque.models import PreventivaExecucao

    hoje = hoje or timezone.localdate()
    item_ids = list(universo_qs(ciclo.subtipo_id).values_list("pk", flat=True))
    previstos = len(item_ids)

    # A expectativa é calculada do MESMO jeito que em `apurar_ciclo`. Usar
    # `pct_tempo` puro aqui faria a lista de ciclos classificar como "abaixo do
    # ritmo" um ciclo que o painel mostra "dentro do ritmo" — a divergência
    # exata que `test_resumo_bate_com_a_apuracao_completa` existe para pegar.
    entradas = entradas_em_operacao(ciclo.subtipo_id, ciclo) if item_ids else {}
    dias_operacao = sum(
        _dias_em_operacao(entradas.get(iid), ciclo, hoje) for iid in item_ids
    )
    duracao = int(ciclo.duracao_dias)
    expectativa = (
        min(100, round(dias_operacao / (previstos * duracao) * 100))
        if previstos and duracao else 0
    )

    realizados = 0
    if item_ids:
        realizados = (
            PreventivaExecucao.objects
            .filter(
                preventiva__equipamento_id__in=item_ids,
                data_execucao__gte=ciclo.data_inicio,
                data_execucao__lte=ciclo.data_fim,
            )
            .values("preventiva__equipamento_id")
            .distinct()
            .count()
        )
    pendentes = max(0, previstos - realizados)
    pct = _pct(realizados, previstos)
    situacao = ciclo.situacao(hoje)
    risco, risco_label = _risco(
        pct, expectativa, pendentes, ciclo.dias_restantes(hoje),
        duracao, situacao, previstos,
    )
    return {
        "ciclo": ciclo,
        "expectativa": expectativa,
        "previstos": previstos,
        "realizados": realizados,
        "pendentes": pendentes,
        "pct": pct,
        "pct_pendente": (100 - pct) if previstos else 0,
        "situacao": situacao,
        "risco": risco,
        "risco_label": risco_label,
        "dias_restantes": ciclo.dias_restantes(hoje),
    }


# =========================================================
# PARQUE DE CADA RESPONSÁVEL
# =========================================================
#
# "Responsáveis por Área" diz QUEM responde por cada parque; esta leitura diz
# O QUE cada um responde — a lista de equipamentos, já com o que o técnico
# precisa saber para decidir a próxima visita. É só leitura: não grava nada e
# não tem regra própria de previsto/realizado. Com ciclo vigente, quem diz se o
# ativo está feito ou pendente é `apurar_ciclo` (regras 44 e 47: uma apuração
# só); sem ciclo, vale a cadência individual do ativo (`Preventiva.data_proxima`).

# Faixa, em dias, em que "próxima preventiva" deixa de ser "em dia" e passa a ser
# "vence em breve". Só vale quando o subtipo NÃO tem ciclo vigente: com ciclo, o
# prazo é o do ciclo, não o de cada ativo. Escolha minha (a confirmar): é o
# tempo de agendar e fazer a visita sem pegar o ativo já vencido.
_JANELA_VENCE_EM_BREVE_DIAS = 15

# Ordem de urgência dos tons: quem está atrasado vem antes de quem só está sem
# agenda, e quem já tem data marcada vem depois dos dois.
_RANK_DO_TOM = {"perigo": 0, "aviso": 1, "neutro": 2, "info": 3, "ok": 4}


def _dias_texto(n: int) -> str:
    return f"{n} dia" + ("" if n == 1 else "s")


def _estado_do_equipamento(item, prev, ciclo, tecnico_nome, hoje, rotulo_status):
    """
    O estado de UM equipamento na visão do técnico: em que grupo cai, com que
    tom e com que texto curto. `ciclo` é o dict montado a partir de
    `apurar_ciclo` (ou None quando o subtipo não tem ciclo vigente).
    """
    from ProjetoEstoque.models import StatusItemChoices

    base = {"novo": False, "data_ref": None}

    # Fora de operação: continua sendo do técnico, mas NÃO é visita — o modelo
    # congela a contagem de preventiva (`Preventiva.pausar`) e o ativo não está
    # no denominador da meta. Mostra o status, sem cobrança.
    if item.status != StatusItemChoices.ATIVO:
        return {**base, "grupo": "fora", "tom": "neutro",
                "etiqueta": rotulo_status.get(item.status, item.status)}

    if ciclo:
        base["novo"] = bool(ciclo.get("novo"))
        if ciclo["estado"] == "feita":
            quem = ciclo.get("quem") or ""
            etiqueta = f"Feita em {ciclo['data']:%d/%m}"
            if quem and quem != tecnico_nome:
                etiqueta += f" · {quem}"
            return {**base, "grupo": "em_dia", "tom": "ok", "etiqueta": etiqueta,
                    "data_ref": ciclo["data"]}
        # Pendente no ciclo. Mesmos textos do cartão "Equipamentos que faltam"
        # do painel de metas: a mesma pendência não pode ter duas linguagens.
        agendada = ciclo.get("agendada")
        quem = ciclo.get("quem") or ""
        de_outro = f" · {quem}" if quem and quem != tecnico_nome else ""
        if ciclo.get("sem_preventiva"):
            return {**base, "grupo": "pendente", "tom": "aviso", "etiqueta": "Sem preventiva"}
        if ciclo.get("vencida"):
            return {**base, "grupo": "pendente", "tom": "perigo",
                    "etiqueta": f"Venceu {agendada:%d/%m}{de_outro}", "data_ref": agendada}
        if agendada:
            return {**base, "grupo": "pendente", "tom": "info",
                    "etiqueta": f"Agendada {agendada:%d/%m}{de_outro}", "data_ref": agendada}
        return {**base, "grupo": "pendente", "tom": "neutro", "etiqueta": f"Sem agenda{de_outro}"}

    # Sem ciclo vigente: cadência individual do ativo.
    if prev is None:
        return {**base, "grupo": "pendente", "tom": "aviso", "etiqueta": "Sem preventiva"}
    proxima = prev["data_proxima"]
    if proxima is None:
        return {**base, "grupo": "pendente", "tom": "aviso", "etiqueta": "Sem data prevista"}
    atraso = (hoje - proxima).days
    if atraso > 0:
        return {**base, "grupo": "pendente", "tom": "perigo",
                "etiqueta": f"Vencida há {_dias_texto(atraso)}", "data_ref": proxima}
    faltam = (proxima - hoje).days
    if faltam <= _JANELA_VENCE_EM_BREVE_DIAS:
        return {**base, "grupo": "pendente", "tom": "aviso",
                "etiqueta": "Vence hoje" if faltam == 0 else f"Vence em {_dias_texto(faltam)}",
                "data_ref": proxima}
    return {**base, "grupo": "em_dia", "tom": "ok",
            "etiqueta": f"Em dia · próxima {proxima:%d/%m}", "data_ref": proxima}


def parque_por_responsavel(hoje: date | None = None, primeiro_id: int | None = None) -> dict:
    """
    Equipamentos que cada técnico responde, prontos para a tela "Responsáveis
    por Área".

    O ativo vai para o técnico pela MESMA regra de `responsaveis_map` — a linha
    específica do subtipo vence a padrão da área, e linha inativa não conta —,
    resolvida em memória para não fazer uma consulta por subtipo. Há teste
    fixando o paralelo com `responsavel_do_item`.

    Entram os ativos com `precisa_preventiva='sim'` e área definida. Quem está
    fora de operação entra também, num grupo à parte: continua sendo do técnico.

    Devolve `{"tecnicos": [...], "sem_responsavel": [...], ...}`. Cada técnico
    traz os totais e três listas: `locais` (PENDENTES agrupados por localidade,
    na ordem em que vale a pena visitar: mais pendência primeiro, e dentro do
    local o mais urgente), `em_dia` e `fora_de_operacao`. `primeiro_id` (o
    usuário que está olhando) vem à frente dos demais.

    Custo fixo em relação ao tamanho do parque: itens, preventivas e linhas de
    responsável são três consultas; o resto são as de `apurar_ciclo` para cada
    subtipo que tenha ciclo vigente.
    """
    from ProjetoEstoque.models import (
        AreaOperacionalChoices,
        Item,
        Preventiva,
        PreventivaAreaResponsavel,
        SimNaoChoices,
        StatusItemChoices,
    )

    hoje = hoje or timezone.localdate()
    rotulo_status = dict(StatusItemChoices.choices)
    rotulo_area = {
        AreaOperacionalChoices.PRODUTIVA: "Produtiva",
        AreaOperacionalChoices.NAO_PRODUTIVA: "Não Produtiva",
    }

    # ── Quem responde por quê ──────────────────────────────────────────────
    linhas = list(
        PreventivaAreaResponsavel.objects
        .filter(ativo=True)
        .select_related("tecnico", "subtipo")
        .order_by("area", "subtipo__nome")
    )
    padrao: dict = {}
    especifico: dict = {}
    buckets: "OrderedDict[int, dict]" = OrderedDict()
    for linha in linhas:
        if linha.subtipo_id is None:
            padrao[linha.area] = linha.tecnico
        else:
            especifico[(linha.area, linha.subtipo_id)] = linha.tecnico
        uid = linha.tecnico_id
        if uid not in buckets:
            buckets[uid] = {
                "tecnico": linha.tecnico,
                "id": uid,
                "nome": _nome(linha.tecnico),
                "voce": uid == primeiro_id,
                "escopos": [],
                "total": 0,
                "pendentes": 0,
                "atrasados": 0,
                "feitos": 0,
                "fora": 0,
                "subtipos": set(),
                "_pendentes": [],
                "em_dia": [],
                "fora_de_operacao": [],
            }
        buckets[uid]["escopos"].append({
            "linha_id": linha.pk,
            "area": linha.area,
            "area_label": rotulo_area.get(linha.area, linha.get_area_display()),
            "subtipo": linha.subtipo.nome if linha.subtipo_id else None,
        })

    # ── Equipamentos do programa, já classificados ────────────────────────
    itens = list(
        Item.objects
        .filter(
            precisa_preventiva=SimNaoChoices.SIM,
            area_operacional__in=[AreaOperacionalChoices.PRODUTIVA, AreaOperacionalChoices.NAO_PRODUTIVA],
        )
        .select_related("localidade", "subtipo")
        .order_by("nome")
    )
    item_ids = [i.pk for i in itens]

    # Preventiva mais urgente de cada um — o MESMO critério de `apurar_ciclo`.
    prev_por_item: dict[int, dict] = {}
    if item_ids:
        for linha in (
            Preventiva.objects
            .filter(equipamento_id__in=item_ids)
            .values("equipamento_id", "data_proxima")
            .order_by("equipamento_id", "data_proxima", "id")
        ):
            prev_por_item.setdefault(linha["equipamento_id"], linha)

    # ── Ciclo vigente: quem diz "feita" ou "pendente" é a apuração ─────────
    ciclo_por_item: dict[int, dict] = {}
    for subtipo_id in sorted({i.subtipo_id for i in itens if i.subtipo_id}):
        ciclo = ciclo_vigente(subtipo_id, hoje)
        if ciclo is None:
            continue
        apuracao = apurar_ciclo(ciclo, hoje)
        for area in apuracao["areas"]:
            for it in area["itens_realizados"]:
                ciclo_por_item[it.pk] = {
                    "estado": "feita",
                    "data": it.meta_data_execucao,
                    "quem": it.meta_tecnico_nome,
                    "novo": it.meta_novo_no_ciclo,
                }
            for it in area["itens_pendentes"]:
                ciclo_por_item[it.pk] = {
                    "estado": "pendente",
                    "agendada": it.meta_data_agendamento,
                    "vencida": it.meta_agendamento_vencido,
                    "sem_preventiva": it.meta_sem_preventiva,
                    "quem": it.meta_tecnico_nome,
                    "novo": it.meta_novo_no_ciclo,
                }

    # ── Distribuição ───────────────────────────────────────────────────────
    sem_resp: dict[tuple, dict] = {}
    for item in itens:
        tecnico = especifico.get((item.area_operacional, item.subtipo_id)) or padrao.get(item.area_operacional)
        if tecnico is None:
            chave = (item.area_operacional, item.subtipo_id)
            achado = sem_resp.setdefault(chave, {
                "area": item.area_operacional,
                "area_label": rotulo_area.get(item.area_operacional, item.area_operacional),
                "subtipo": item.subtipo.nome if item.subtipo_id else "Sem subtipo",
                "total": 0,
            })
            achado["total"] += 1
            continue

        bucket = buckets[tecnico.pk]
        estado = _estado_do_equipamento(
            item, prev_por_item.get(item.pk), ciclo_por_item.get(item.pk),
            bucket["nome"], hoje, rotulo_status,
        )
        linha = {
            "item": item,
            "pk": item.pk,
            "nome": item.nome,
            "numero_serie": item.numero_serie,
            "subtipo": item.subtipo.nome if item.subtipo_id else "",
            "area": item.area_operacional,
            "area_label": rotulo_area.get(item.area_operacional, ""),
            "localidade": item.localidade.local if item.localidade_id else "Sem localidade",
            **estado,
        }
        bucket["subtipos"].add(item.subtipo_id)
        bucket["total"] += 1
        if estado["grupo"] == "pendente":
            bucket["pendentes"] += 1
            if estado["tom"] == "perigo":
                bucket["atrasados"] += 1
            bucket["_pendentes"].append(linha)
        elif estado["grupo"] == "em_dia":
            bucket["feitos"] += 1
            bucket["em_dia"].append(linha)
        else:
            bucket["fora"] += 1
            bucket["fora_de_operacao"].append(linha)

    # ── Ordem de visita dentro de cada técnico ─────────────────────────────
    def _urgencia(linha):
        # Sem data de referência (nunca teve preventiva, ou não tem agenda) vem
        # ANTES de quem tem data no mesmo tom: o buraco maior é o que nunca foi
        # feito, e "vence em 5 dias" ao menos já está no radar.
        return (
            _RANK_DO_TOM.get(linha["tom"], 9),
            linha["data_ref"] or date.min,
            linha["nome"].lower(),
        )

    for bucket in buckets.values():
        por_local: dict[str, list] = {}
        for linha in bucket.pop("_pendentes"):
            por_local.setdefault(linha["localidade"], []).append(linha)
        bucket["locais"] = sorted(
            (
                {"local": local, "total": len(lista), "itens": sorted(lista, key=_urgencia)}
                for local, lista in por_local.items()
            ),
            key=lambda g: (-g["total"], g["local"].lower()),
        )
        bucket["em_dia"].sort(key=lambda l: l["nome"].lower())
        bucket["fora_de_operacao"].sort(key=lambda l: l["nome"].lower())
        # Mais de um subtipo (ou das duas áreas) no parque: a linha passa a
        # dizer de qual é cada um. Com um só, seria repetição em toda linha.
        bucket["varios_subtipos"] = len(bucket["subtipos"]) > 1
        bucket["varias_areas"] = len({e["area"] for e in bucket["escopos"]}) > 1
        bucket.pop("subtipos")

    tecnicos = sorted(
        buckets.values(),
        key=lambda b: (0 if b["voce"] else 1, -b["pendentes"], b["nome"].lower()),
    )
    vagos = sorted(sem_resp.values(), key=lambda v: (v["area_label"], v["subtipo"]))
    return {
        "hoje": hoje,
        "tecnicos": tecnicos,
        "sem_responsavel": vagos,
        "sem_responsavel_total": sum(v["total"] for v in vagos),
    }
