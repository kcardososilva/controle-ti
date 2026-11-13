"""
Contratação de colaborador — regra de negócio do módulo de admissão.

Fonte única de três perguntas que a tela, o detalhe e qualquer export futuro
precisam responder do mesmo jeito:

  1. EM QUE ETAPA a vaga está, e o que falta para passar para a próxima
     (`avancar_etapa`, `pendencias`);
  2. O QUE JÁ FOI PROVISIONADO — acesso de sistema, assento de licença,
     equipamento entregue (`resumo_provisionamento`);
  3. COMO ESTÁ O QUADRO — quantas abertas, quantas atrasadas, quanto tempo
     cada etapa está levando (`painel`).

Por que isto não mora nas views: a etapa carimba data, grava evento e, no
caso do equipamento, grava o elo com o inventário. Repetir essa sequência em
cada view é como as duas primeiras divergem — uma grava o evento, a outra
esquece, e a linha do tempo passa a mentir sobre o que aconteceu.

O que este service NÃO faz, de propósito:

  • não movimenta estoque — registrar a entrega aqui diz QUAL equipamento
    saiu; a transferência continua em `movimentacao_service.py`, que é a
    fonte única do inventário;
  • não atribui assento de licença — isso debita lote e tem custo, e a regra
    mora em `licenca_service.py`. Aqui a licença é marcada como atribuída e
    opcionalmente ligada à `MovimentacaoLicenca` que o módulo de Licenças
    gerou.

Duplicar qualquer um dos dois criaria uma segunda verdade sobre o mesmo
ativo — o defeito que a regra 47 do CLAUDE.md descreve para as metas.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from ProjetoEstoque.models import (
    AtendimentoChamadoChoices,
    Contratacao,
    ContratacaoEtapaRegistro,
    ContratacaoEvento,
    ContratacaoLicenca,
    ContratacaoSistema,
    ContratacaoVagaHistorico,
    MovimentacaoItem,
    ReservaEquipamento,
    SistemaCorporativo,
    StatusContratacaoChoices,
    TipoChamadoContratacao,
    TipoComputadorChoices,
    Usuario,
)
from services import acesso_restrito_service as ar
from services import reserva_equipamento_service as reservas

# As quatro colunas do quadro, na ordem do fluxo. É a mesma ordem das colunas
# do Planner que o módulo substitui — manter o vocabulário é o que faz quem
# já usava o quadro não precisar reaprender nada.
ETAPAS = (
    {
        "chave": StatusContratacaoChoices.ABERTA,
        "label": "Em aberto",
        "icone": "fa-folder-open",
        "descricao": "Vaga solicitada, ainda sem pessoa definida.",
    },
    {
        "chave": StatusContratacaoChoices.VAGA_PREENCHIDA,
        "label": "Vaga preenchida",
        "icone": "fa-user-check",
        "descricao": "Já existe contratado; o TI prepara acessos e equipamento.",
    },
    {
        "chave": StatusContratacaoChoices.EQUIPAMENTO_ENTREGUE,
        "label": "Equipamento entregue",
        "icone": "fa-laptop",
        "descricao": "Máquina entregue — faltam os acessos que ainda estiverem pendentes.",
    },
    {
        "chave": StatusContratacaoChoices.CONCLUIDA,
        "label": "Concluída",
        "icone": "fa-circle-check",
        "descricao": "Tudo provisionado e conferido.",
    },
)

_ORDEM_ETAPA = {e["chave"]: i for i, e in enumerate(ETAPAS)}


# ─────────────────────────────────────────────────────────────
# Leitura
# ─────────────────────────────────────────────────────────────

# O que `etapas_do_processo` lê. Toda tela que avalia o processo (painel,
# detalhe) pré-carrega isto: sem o prefetch, cada cartão do painel dispararia
# quatro consultas só para decidir se a vaga tem equipamento e chamado.
PREFETCH_DO_PROCESSO = (
    "sistemas__sistema__licenca", "licencas_necessarias__licenca",
    "reservas__item__subtipo", "chamados", "etapas_registro__criado_por",
)


def queryset_base():
    """Consulta com os relacionamentos que TODA tela do módulo usa. Sem o
    `select_related`, o painel dispara uma consulta por card só para escrever
    o nome do centro de custo — N+1 numa tela que existe para mostrar lista."""
    return (
        Contratacao.objects
        .select_related(
            "funcao", "centro_custo", "localidade", "colaborador",
            "substituido", "item_entregue", "item_entregue__subtipo",
            "integracao_confirmada_por",
        )
        .annotate(
            sistemas_pedidos=Count("sistemas", filter=Q(sistemas__necessario=True), distinct=True),
            sistemas_prontos=Count(
                "sistemas",
                filter=Q(sistemas__necessario=True, sistemas__provisionado=True),
                distinct=True,
            ),
            licencas_pedidas=Count("licencas_necessarias", distinct=True),
            licencas_prontas=Count(
                "licencas_necessarias",
                filter=Q(licencas_necessarias__provisionada=True),
                distinct=True,
            ),
        )
    )


# ─────────────────────────────────────────────────────────────
# O processo de contratação: as etapas até "concluída"
# ─────────────────────────────────────────────────────────────
#
# A vaga estar "Preenchida" quer dizer que o RH encontrou a pessoa — não que
# ela esteja pronta para trabalhar. Concluir a contratação é afirmar que o
# colaborador foi cadastrado, recebeu a estrutura e foi integrado, e essa
# afirmação só vale se cada etapa aplicável a ESTA contratação foi cumprida.
#
# Três eixos diferentes, que não se confundem:
#   • a SITUAÇÃO DA VAGA (`Contratacao.status`) — onde ela está no quadro;
#   • a SITUAÇÃO DA INTEGRAÇÃO (`situacao_integracao`) — o RH confirmou?;
#   • a SITUAÇÃO DOS CHAMADOS (`ContratacaoChamado.atendimento`) — o que a TI fez.
#
# Cada etapa é DEDUZIDA do que o sistema já sabe (colaborador cadastrado,
# equipamento reservado, entrega por movimentação, acesso provisionado, chamado
# concluído) — nunca de uma caixa marcada à mão que repita o fato. Só duas
# coisas precisam de decisão humana e por isso têm registro próprio
# (`ContratacaoEtapaRegistro`): dispensar uma etapa que não se aplica (com
# justificativa) e registrar as orientações de uso dos sistemas.

CADASTRO = "cadastro_colaborador"
RESERVA = "equipamentos_reservados"
PREPARACAO = "preparacao_equipamento"
ENTREGA = "entrega_equipamento"
USUARIO_EMAIL = "usuario_email"
SISTEMAS = "liberacao_sistemas"
ORIENTACOES = "orientacoes"
INTEGRACAO = "integracao"
CONFIRMACAO = "confirmacao_rh"

# Quem não pode ser dispensado: a integração e a confirmação final SÃO a
# conclusão — dispensá-las seria concluir sem concluir.
_NAO_DISPENSAVEIS = frozenset({INTEGRACAO, CONFIRMACAO})
# Etapas em que o TI registra, à mão, que foi feita (não há fato que a prove).
_REGISTRAVEIS = frozenset({ORIENTACOES})

ROTULO_DA_ETAPA = {
    CADASTRO: "Cadastro do colaborador",
    RESERVA: "Equipamentos reservados",
    PREPARACAO: "Configuração e preparação do equipamento",
    ENTREGA: "Movimentação de entrega no nome do colaborador",
    USUARIO_EMAIL: "Criação de usuário e e-mail",
    SISTEMAS: "Liberação dos sistemas",
    ORIENTACOES: "Orientações de utilização dos sistemas",
    INTEGRACAO: "Integração do colaborador",
    CONFIRMACAO: "Confirmação final do RH",
}


@dataclass
class Etapa:
    """Uma etapa do processo, já avaliada para uma contratação."""
    chave: str
    rotulo: str
    dono: str                 # "RH" | "TI" — quem tem a bola
    estado: str               # "concluida" | "pendente" | "dispensada"
    detalhe: str = ""
    origem: str = "sistema"   # sistema | manual | configuracao | dispensa
    dispensavel: bool = False
    registravel: bool = False
    feita_em: object = None
    feita_por: str = ""

    @property
    def concluida(self) -> bool:
        return self.estado == "concluida"

    @property
    def pendente(self) -> bool:
        return self.estado == "pendente"

    @property
    def dispensada(self) -> bool:
        return self.estado == "dispensada"

    @property
    def tom(self) -> str:
        return {"concluida": "ok", "pendente": "aviso", "dispensada": "neutro"}[self.estado]


def entrega_efetiva(item, colaborador) -> bool:
    """O equipamento FOI entregue a este colaborador por uma movimentação
    patrimonial (de entrega, não revertida)? É a única prova de entrega aceita:
    reserva é planejamento e uma declaração digitada não move nada."""
    if item is None or colaborador is None:
        return False
    return MovimentacaoItem.objects.filter(
        item=item, usuario=colaborador, tipo_movimentacao="transferencia",
        tipo_transferencia="entrega", revertida=False,
    ).exists()


def _nome_do_chamado(chamado) -> str:
    return chamado.get_atendimento_display() if chamado else "não solicitado"


def etapas_do_processo(contratacao) -> list:
    """As nove etapas do processo, avaliadas para ESTA contratação.

    Lê só `.all()` das relações (nunca `.filter()`), de modo que um
    `prefetch_related` da tela vira zero consulta extra — o painel avalia um
    cartão por vaga. A única consulta que sobra é a de `entrega_efetiva`, e só
    para a vaga legada que registrou a entrega sem passar pela reserva."""
    c = contratacao
    reservas_ = list(c.reservas.all())
    ativas = [r for r in reservas_ if r.estado == ReservaEquipamento.Estado.ATIVA]
    entregues = [r for r in reservas_ if r.estado == ReservaEquipamento.Estado.ENTREGUE]
    chamados = {ch.tipo: ch for ch in c.chamados.all()}
    registros = {r.chave: r for r in c.etapas_registro.all() if r.desfeita_em is None}
    sistemas = [s for s in c.sistemas.all() if s.necessario]
    licencas = list(c.licencas_necessarias.all())

    def concluido(tipo) -> bool:
        ch = chamados.get(tipo)
        return bool(ch) and ch.atendimento == AtendimentoChamadoChoices.CONCLUIDO

    def montar(chave, dono, *, aplicavel=True, feita=False, pendente="", feita_txt="",
               nao_se_aplica="", feita_em=None):
        reg = registros.get(chave)
        etapa = Etapa(
            chave=chave, rotulo=ROTULO_DA_ETAPA[chave], dono=dono, estado="pendente",
            dispensavel=chave not in _NAO_DISPENSAVEIS, registravel=chave in _REGISTRAVEIS,
        )
        if reg is not None and reg.estado == ContratacaoEtapaRegistro.Estado.DISPENSADA and etapa.dispensavel:
            etapa.estado, etapa.origem = "dispensada", "dispensa"
            etapa.detalhe = f"Dispensada — {reg.justificativa}"
            etapa.feita_em = reg.registrada_em
            etapa.feita_por = _nome_do_autor(reg)
        elif not aplicavel:
            etapa.estado, etapa.origem = "dispensada", "configuracao"
            etapa.detalhe = nao_se_aplica
        elif feita:
            etapa.estado, etapa.detalhe, etapa.feita_em = "concluida", feita_txt, feita_em
        elif reg is not None and reg.estado == ContratacaoEtapaRegistro.Estado.CONCLUIDA:
            etapa.estado, etapa.origem = "concluida", "manual"
            etapa.detalhe = reg.justificativa or "Registrada pelo TI."
            etapa.feita_em = reg.registrada_em
            etapa.feita_por = _nome_do_autor(reg)
        else:
            etapa.detalhe = pendente
        return etapa

    usa_equipamento = bool(c.precisa_computador or ativas or entregues or c.item_entregue_id)
    sem_equip = "A vaga não precisa de equipamento."

    etapas = []

    # 1 · Cadastro do colaborador
    if c.nome_contratado:
        falta_cadastro = f"{c.nome_contratado} foi informado, mas ainda não tem cadastro de colaborador."
    else:
        falta_cadastro = "A vaga ainda não foi preenchida."
    etapas.append(montar(
        CADASTRO, "RH", feita=bool(c.colaborador_id), pendente=falta_cadastro,
        feita_txt=c.colaborador.nome if c.colaborador_id else "",
    ))

    # 2 · Equipamentos reservados
    if ativas or entregues:
        reservada = True
    else:
        reservada = bool(c.item_entregue_id)
    pend_equip = "Nenhum equipamento reservado para a vaga."
    cedidas = [r for r in reservas_ if r.estado == ReservaEquipamento.Estado.CEDIDA and not r.substituida_por_id]
    if cedidas and not (ativas or entregues):
        pend_equip = "O equipamento reservado foi usado em outra necessidade — falta nova reserva."
    etapas.append(montar(
        RESERVA, "TI", aplicavel=usa_equipamento, feita=reservada, pendente=pend_equip,
        nao_se_aplica=sem_equip,
        feita_txt=", ".join(r.item_rotulo for r in (ativas + entregues)[:3]) or "Equipamento entregue registrado.",
    ))

    # 3 · Configuração e preparação (o chamado da máquina)
    ch_maq = chamados.get(TipoChamadoContratacao.MAQUINA)
    etapas.append(montar(
        PREPARACAO, "TI", aplicavel=usa_equipamento, feita=concluido(TipoChamadoContratacao.MAQUINA),
        pendente=("Chamado de máquina não solicitado." if ch_maq is None
                  else f"Chamado de máquina: {_nome_do_chamado(ch_maq)}."),
        nao_se_aplica=sem_equip, feita_txt="Chamado de máquina concluído.",
    ))

    # 4 · Entrega — por movimentação patrimonial, nunca por declaração
    entrega_ok = False
    if entregues and not ativas:
        entrega_ok = True
    elif not ativas and c.item_entregue_id and c.colaborador_id:
        entrega_ok = entrega_efetiva(c.item_entregue, c.colaborador)
    if ativas:
        pend_entrega = (
            f"{len(ativas)} equipamento(s) reservado(s) aguardando a movimentação de entrega."
        )
    elif c.item_entregue_id and not entrega_ok:
        pend_entrega = "Equipamento informado, mas sem movimentação de entrega em nome do colaborador."
    else:
        pend_entrega = "Nenhuma movimentação de entrega registrada."
    etapas.append(montar(
        ENTREGA, "TI", aplicavel=usa_equipamento, feita=entrega_ok, pendente=pend_entrega,
        nao_se_aplica=sem_equip, feita_txt="Entrega confirmada por movimentação patrimonial.",
        feita_em=c.equipamento_entregue_em,
    ))

    # 5 · Usuário e e-mail
    pede_usuario = bool(c.usuario_rede or c.email_corporativo)
    ch_usr = chamados.get(TipoChamadoContratacao.USUARIO_EMAIL)
    etapas.append(montar(
        USUARIO_EMAIL, "TI", aplicavel=pede_usuario, feita=concluido(TipoChamadoContratacao.USUARIO_EMAIL),
        pendente=("Chamado de usuário e e-mail não solicitado." if ch_usr is None
                  else f"Chamado de usuário e e-mail: {_nome_do_chamado(ch_usr)}."),
        nao_se_aplica="A vaga não pede usuário de rede nem e-mail.",
        feita_txt="Chamado de usuário e e-mail concluído.",
    ))

    # 6 · Liberação dos sistemas (acessos e assentos de licença)
    sistemas_pend = [s for s in sistemas if not s.provisionado]
    licencas_pend = [lic for lic in licencas if not lic.provisionada]
    partes = []
    if sistemas_pend:
        nomes = ", ".join(s.sistema.nome for s in sistemas_pend[:3])
        partes.append(f"Acesso pendente: {nomes}" + (f" +{len(sistemas_pend) - 3}" if len(sistemas_pend) > 3 else ""))
    if licencas_pend:
        nomes = ", ".join(lic.licenca.nome for lic in licencas_pend[:2])
        partes.append(f"Licença pendente: {nomes}" + (f" +{len(licencas_pend) - 2}" if len(licencas_pend) > 2 else ""))
    etapas.append(montar(
        SISTEMAS, "TI", aplicavel=bool(sistemas or licencas), feita=not partes,
        pendente="; ".join(partes), nao_se_aplica="A vaga não pede acesso a sistemas nem licença.",
        feita_txt="Acessos e licenças liberados.",
    ))

    # 7 · Orientações de uso dos sistemas
    ch_sis = chamados.get(TipoChamadoContratacao.SISTEMAS)
    etapas.append(montar(
        ORIENTACOES, "TI", aplicavel=bool(sistemas or licencas or ch_sis),
        feita=concluido(TipoChamadoContratacao.SISTEMAS),
        pendente=("Orientações ainda não registradas pelo TI." if ch_sis is None
                  else f"Chamado de orientação aos sistemas: {_nome_do_chamado(ch_sis)}."),
        nao_se_aplica="A vaga não usa sistemas que peçam orientação.",
        feita_txt="Chamado de orientação concluído.",
    ))

    # 8 · Integração do colaborador (confirmada pelo RH)
    etapas.append(montar(
        INTEGRACAO, "RH", feita=bool(c.integracao_confirmada_em),
        pendente="Integração ainda não confirmada pelo RH.",
        feita_txt=c.integracao_observacao or "Integração confirmada.",
        feita_em=c.integracao_confirmada_em,
    ))
    if c.integracao_confirmada_por_id:
        etapas[-1].feita_por = _nome_de_usuario(c.integracao_confirmada_por)

    # 9 · Confirmação final do RH — é o ato de concluir
    etapas.append(montar(
        CONFIRMACAO, "RH", feita=(c.status == StatusContratacaoChoices.CONCLUIDA),
        pendente="Quando todas as etapas acima estiverem feitas ou dispensadas, o RH conclui a contratação.",
        feita_txt="Contratação concluída.", feita_em=c.concluida_em,
    ))
    return etapas


def _nome_de_usuario(user) -> str:
    if user is None:
        return ""
    return (user.get_full_name() or user.get_username() or "").strip()


def _nome_do_autor(registro) -> str:
    return _nome_de_usuario(registro.criado_por)


def pendencias(contratacao) -> list[str]:
    """O que falta para a contratação poder ser concluída, em frases curtas.

    São as etapas OBRIGATÓRIAS ainda pendentes (as dispensadas e as que não se
    aplicam não entram), menos a confirmação final — que é o próprio ato de
    concluir. É a lista do card, do detalhe e da trava de `concluir`: as três
    leem da mesma avaliação, então não divergem."""
    return pendencias_das(etapas_do_processo(contratacao))


def pendencias_das(etapas) -> list[str]:
    """`pendencias` sobre etapas JÁ avaliadas — para a tela que também precisa
    do placar e não quer avaliar o processo duas vezes."""
    return [e.detalhe or e.rotulo for e in etapas if e.pendente and e.chave != CONFIRMACAO]


def resumo_processo(etapas) -> dict:
    """Placar das etapas: quantas aplicáveis, quantas feitas. As dispensadas
    saem da conta — não são pendência nem mérito."""
    aplicaveis = [e for e in etapas if not e.dispensada]
    feitas = [e for e in aplicaveis if e.concluida]
    total = len(aplicaveis)
    return {
        "total": total, "feitas": len(feitas), "faltam": total - len(feitas),
        "pct": round(len(feitas) * 100 / total) if total else 100,
        "dispensadas": len(etapas) - total,
    }


def situacao_integracao(contratacao) -> dict:
    """Em que pé está a integração — outro eixo, independente do status da vaga
    e dos chamados de TI. A vaga pode estar "Preenchida" com a integração ainda
    em andamento."""
    c = contratacao
    if c.integracao_confirmada_em:
        return {"estado": "confirmada", "rotulo": "Integração confirmada", "tom": "ok"}
    if c.status == StatusContratacaoChoices.CANCELADA:
        return {"estado": "cancelada", "rotulo": "Contratação cancelada", "tom": "neutro"}
    if c.status == StatusContratacaoChoices.CONCLUIDA:
        return {"estado": "nao_registrada", "rotulo": "Concluída sem registro de integração", "tom": "neutro"}
    if c.status == StatusContratacaoChoices.ABERTA:
        return {"estado": "nao_iniciada", "rotulo": "Integração não iniciada", "tom": "neutro"}
    return {"estado": "em_andamento", "rotulo": "Integração em andamento", "tom": "aviso"}


def resumo_provisionamento(contratacao) -> dict:
    """Placar de provisionamento: quantos itens da lista de preparação já
    estão feitos. O denominador inclui o equipamento (quando a vaga pede um),
    porque para quem vai começar a trabalhar a máquina conta tanto quanto o
    acesso."""
    sistemas = list(contratacao.sistemas.all())
    pedidos = [s for s in sistemas if s.necessario]
    licencas = list(contratacao.licencas_necessarias.all())

    total = len(pedidos) + len(licencas)
    feitos = len([s for s in pedidos if s.provisionado]) + len([lic for lic in licencas if lic.provisionada])

    if contratacao.precisa_computador:
        total += 1
        if contratacao.item_entregue_id:
            feitos += 1

    pct = round(feitos * 100 / total) if total else 100
    return {
        "total": total,
        "feitos": feitos,
        "faltam": max(total - feitos, 0),
        "pct": pct,
        "completo": feitos >= total,
    }


def painel(filtros=None, hoje=None) -> dict:
    """Monta o painel: KPIs do topo e as colunas por etapa.

    Custo fixo por tela: uma consulta para os cards e uma para o histórico do
    mês, independentemente de haver 3 ou 300 contratações — o mesmo cuidado
    que a regra 47 do CLAUDE.md registra para o painel de metas."""
    hoje = hoje or datetime.date.today()
    filtros = filtros or {}

    qs = queryset_base().prefetch_related(*PREFETCH_DO_PROCESSO)

    busca = (filtros.get("q") or "").strip()
    if busca:
        qs = qs.filter(
            Q(cargo__icontains=busca)
            | Q(colaborador_nome__icontains=busca)
            | Q(colaborador__nome__icontains=busca)
            | Q(gestor__icontains=busca)
        )
    if filtros.get("centro_custo"):
        qs = qs.filter(centro_custo_id=filtros["centro_custo"])
    if filtros.get("localidade"):
        qs = qs.filter(localidade_id=filtros["localidade"])

    abertas = [c for c in qs if c.status != StatusContratacaoChoices.CANCELADA]

    # Vaga sem equipamento reservado é PENDÊNCIA, não etapa: o selo do cartão e o
    # KPI saem de UMA consulta, e nada disso muda o status da vaga.
    sem_equipamento = reservas.vagas_sem_equipamento(abertas)
    for c in abertas:
        c.sem_equipamento = sem_equipamento.get(c.pk, "")

    # Concluída é etapa final, não arquivo morto: o quadro mostra as do mês
    # corrente e para por aí. Sem o corte, a quarta coluna cresce para sempre
    # e deixa de ser legível justamente onde se quer ver o que fechou agora.
    inicio_mes = hoje.replace(day=1)

    colunas = []
    for etapa in ETAPAS:
        cards = [c for c in abertas if c.status == etapa["chave"]]
        if etapa["chave"] == StatusContratacaoChoices.CONCLUIDA:
            cards = [
                c for c in cards
                if c.concluida_em and timezone.localtime(c.concluida_em).date() >= inicio_mes
            ]
        cards.sort(key=_ordem_do_card)
        colunas.append({
            **etapa,
            "cards": cards,
            "total": len(cards),
        })

    em_andamento = [
        c for c in abertas
        if c.status != StatusContratacaoChoices.CONCLUIDA
    ]
    atrasadas = [c for c in em_andamento if c.atrasada]
    concluidas_mes = [
        c for c in abertas
        if c.status == StatusContratacaoChoices.CONCLUIDA
        and c.concluida_em and timezone.localtime(c.concluida_em).date() >= inicio_mes
    ]

    total_ciclo = len(em_andamento) + len(concluidas_mes)
    pct = round(len(concluidas_mes) * 100 / total_ciclo) if total_ciclo else 0

    return {
        "colunas": colunas,
        "kpis": {
            "em_andamento": len(em_andamento),
            "abertas": len([c for c in em_andamento if c.status == StatusContratacaoChoices.ABERTA]),
            "aguardando_equipamento": len([
                c for c in em_andamento
                if c.precisa_computador and not c.item_entregue_id
            ]),
            "sem_equipamento": len([c for c in em_andamento if c.sem_equipamento]),
            "atrasadas": len(atrasadas),
            "concluidas_mes": len(concluidas_mes),
        },
        "pct_mes": pct,
        "atrasadas": atrasadas,
        "mes_referencia": inicio_mes,
        "tempo_medio_dias": _tempo_medio_de_conclusao(concluidas_mes),
    }


def _ordem_do_card(contratacao):
    """Atrasada primeiro, depois a que começa antes, depois a mais antiga. A
    ordenação é a única priorização que o quadro oferece — e quem precisa
    aparecer no topo é quem já passou da data."""
    sem_data = datetime.date.max
    return (
        0 if contratacao.atrasada else 1,
        contratacao.data_prevista_inicio or sem_data,
        contratacao.created_at,
    )


def _tempo_medio_de_conclusao(concluidas) -> float | None:
    """Dias entre abrir a vaga e concluir, em média. `None` quando ainda não
    fechou nenhuma no mês — média de lista vazia seria 0, que se lê como
    "fecha no mesmo dia"."""
    prazos = []
    for c in concluidas:
        if not c.concluida_em:
            continue
        prazos.append((timezone.localtime(c.concluida_em).date() - timezone.localtime(c.created_at).date()).days)
    if not prazos:
        return None
    return round(sum(prazos) / len(prazos), 1)


# ─────────────────────────────────────────────────────────────
# Escrita
# ─────────────────────────────────────────────────────────────

@transaction.atomic
def criar(*, form, user):
    """Abre a vaga e materializa as perguntas de sistema marcadas."""
    contratacao = form.save(commit=False)
    contratacao.criado_por = user
    contratacao.atualizado_por = user
    contratacao.save()

    _sincronizar_sistemas(contratacao, form.sistemas_marcados(), user)
    _sincronizar_licencas(contratacao, form.licencas_marcadas(), user)

    registrar_evento(
        contratacao, "criacao",
        f"Contratação aberta para {contratacao.cargo}.", user,
    )
    return contratacao


@transaction.atomic
def atualizar(*, contratacao, form, user):
    contratacao = form.save(commit=False)
    contratacao.atualizado_por = user
    contratacao.save()

    _sincronizar_sistemas(contratacao, form.sistemas_marcados(), user)
    _sincronizar_licencas(contratacao, form.licencas_marcadas(), user)

    registrar_evento(contratacao, "edicao", "Dados da contratação atualizados.", user)
    return contratacao


def _sincronizar_sistemas(contratacao, marcados, user):
    """Alinha `ContratacaoSistema` ao que o formulário marcou.

    O que NÃO é marcado tem `necessario=False` em vez de ser apagado quando
    já foi provisionado: apagar a linha de um acesso que o TI criou jogaria
    fora a data em que ele foi criado — o único registro de que aquele acesso
    existe."""
    desejados = {sistema.pk: espelho for sistema, espelho in marcados}
    existentes = {s.sistema_id: s for s in contratacao.sistemas.all()}

    for sistema_id, espelho in desejados.items():
        linha = existentes.get(sistema_id)
        if linha is None:
            ContratacaoSistema.objects.create(
                contratacao=contratacao,
                sistema_id=sistema_id,
                necessario=True,
                perfil_espelho=espelho,
                criado_por=user,
                atualizado_por=user,
            )
            continue
        linha.necessario = True
        linha.perfil_espelho = espelho
        linha.atualizado_por = user
        linha.save(update_fields=["necessario", "perfil_espelho", "atualizado_por", "updated_at"])

    for sistema_id, linha in existentes.items():
        if sistema_id in desejados:
            continue
        if linha.provisionado:
            linha.necessario = False
            linha.atualizado_por = user
            linha.save(update_fields=["necessario", "atualizado_por", "updated_at"])
        else:
            linha.delete()


def _sincronizar_licencas(contratacao, licencas, user):
    """Alinha `ContratacaoLicenca` ao que foi marcado na lista de acessos.

    `licencas` vem de `form.licencas_marcadas()`: [(Licenca, espelho)] — a
    mesma forma que `_sincronizar_sistemas` recebe, porque na tela as duas
    são a mesma linha.

    A diferença de regra fica no DESMARCAR: sistema já provisionado vira
    `necessario=False` e a linha fica; licença não tem esse campo, então a
    linha só é apagada enquanto o assento não foi atribuído. Apagar depois
    jogaria fora a data da atribuição — o único registro de que o assento
    saiu do lote."""
    desejadas = {lic.pk: espelho for lic, espelho in licencas}
    existentes = {lic.licenca_id: lic for lic in contratacao.licencas_necessarias.all()}

    for licenca_id, espelho in desejadas.items():
        linha = existentes.get(licenca_id)
        if linha is None:
            ContratacaoLicenca.objects.create(
                contratacao=contratacao, licenca_id=licenca_id,
                perfil_espelho=espelho,
                criado_por=user, atualizado_por=user,
            )
            continue
        if linha.perfil_espelho != espelho:
            linha.perfil_espelho = espelho
            linha.atualizado_por = user
            linha.save(update_fields=["perfil_espelho", "atualizado_por", "updated_at"])

    for licenca_id, linha in existentes.items():
        if licenca_id in desejadas or linha.provisionada:
            continue
        linha.delete()


def registrar_evento(contratacao, tipo, descricao, user):
    return ContratacaoEvento.objects.create(
        contratacao=contratacao, tipo=tipo, descricao=descricao,
        criado_por=user, atualizado_por=user,
    )


# Campos do contratado que o RH informa além do nome. O cargo, o setor e a
# unidade NÃO estão aqui: já são da vaga, e repeti-los criaria duas verdades.
CAMPOS_DO_CONTRATADO = (
    "colaborador_email", "colaborador_telefone", "colaborador_observacoes", "data_admissao",
)


class TransicaoRecusada(Exception):
    """A regra de negócio disse não a uma mudança de etapa/preenchimento. A
    mensagem já vem em texto de tela."""


@transaction.atomic
def definir_contratado(*, contratacao, colaborador, nome, user, extras=None):
    """Etapa 2. Avança o status só quando a vaga ainda estava em aberto —
    corrigir o nome de quem assumiu não pode empurrar uma contratação que já
    estava na etapa de entrega para trás.

    `extras` (opcional) traz os dados que o RH informa ao preencher
    (`CAMPOS_DO_CONTRATADO`). Quem preencheu fica em `preenchida_por` e a
    transição de status entra no texto do evento. NÃO toca nos chamados de TI:
    o chamado é um processo próprio, e preencher a vaga não conclui nenhum."""
    anterior = contratacao.status
    contratacao.colaborador = colaborador
    contratacao.colaborador_nome = (nome or "").strip()
    campos = ["colaborador", "colaborador_nome", "atualizado_por", "updated_at"]

    for campo in CAMPOS_DO_CONTRATADO:
        if extras and campo in extras:
            setattr(contratacao, campo, extras[campo])
            campos.append(campo)

    transicionou = contratacao.status == StatusContratacaoChoices.ABERTA
    if transicionou:
        contratacao.status = StatusContratacaoChoices.VAGA_PREENCHIDA
        contratacao.vaga_preenchida_em = timezone.now()
        contratacao.preenchida_por = user
        campos += ["status", "vaga_preenchida_em", "preenchida_por"]

    contratacao.atualizado_por = user
    contratacao.save(update_fields=campos)

    # As reservas da vaga passam a ser do colaborador cadastrado (ou deixam de
    # ser, se a vaga ficou só com o nome).
    reservas.associar_colaborador(contratacao, colaborador, user)

    nome_final = contratacao.nome_contratado or "contratado sem nome informado"
    if transicionou:
        descricao = (
            f"Vaga preenchida por {nome_final} "
            f"(status: {StatusContratacaoChoices(anterior).label} → "
            f"{contratacao.get_status_display()})."
        )
    else:
        descricao = f"Contratado atualizado: {nome_final}."
    registrar_evento(contratacao, "etapa", descricao, user)
    return contratacao


# ─────────────────────────────────────────────────────────────
# Preenchimento da vaga pelo RH
# ─────────────────────────────────────────────────────────────
# O RH encerra o processo seletivo: marca a vaga como preenchida, corrige o que
# digitou errado e reabre a vaga se a contratação cair. Três operações com a
# mesma disciplina: capacidade conferida AQUI (a terceira camada, depois do
# middleware e do decorador), a vaga TRAVADA e o estado relido dentro da
# transação, e o estado de antes guardado em `ContratacaoVagaHistorico`.

def _exigir_preencher(user) -> None:
    if not ar.pode_preencher_vaga(user):
        raise PermissionDenied("Seu acesso não permite alterar o preenchimento de uma vaga.")


def _foto_do_contratado(contratacao) -> dict:
    """O contratado como a vaga o tem AGORA, em tipos simples (vai para JSON).
    `colaborador` guarda o id e o nome do cadastro: o nome do cadastro pode mudar
    depois, e o histórico precisa dizer quem era na época."""
    return {
        "colaborador_id": contratacao.colaborador_id,
        "colaborador_nome": contratacao.nome_contratado,
        "colaborador_email": contratacao.colaborador_email,
        "colaborador_telefone": contratacao.colaborador_telefone,
        "colaborador_observacoes": contratacao.colaborador_observacoes,
        "data_admissao": contratacao.data_admissao.isoformat() if contratacao.data_admissao else "",
    }


_ROTULO_CAMPO = {
    "colaborador_nome": "nome",
    "colaborador_email": "e-mail",
    "colaborador_telefone": "telefone",
    "colaborador_observacoes": "observações",
    "data_admissao": "data de admissão",
}


def _exigir_motivo(motivo: str, *, acao: str) -> str:
    motivo = (motivo or "").strip()
    if len(motivo) < 3:
        raise TransicaoRecusada(f"Informe o motivo para {acao}.")
    return motivo[:500]


def _aplicar_dados(contratacao, dados: dict) -> None:
    contratacao.colaborador = dados.get("colaborador")
    contratacao.colaborador_nome = (dados.get("colaborador_nome") or "").strip()
    contratacao.colaborador_email = (dados.get("colaborador_email") or "").strip()
    contratacao.colaborador_telefone = (dados.get("colaborador_telefone") or "").strip()
    contratacao.colaborador_observacoes = (dados.get("colaborador_observacoes") or "").strip()
    contratacao.data_admissao = dados.get("data_admissao") or None


def _historico(contratacao, acao, *, status_antes, user, motivo="", antes=None, depois=None):
    return ContratacaoVagaHistorico.objects.create(
        contratacao=contratacao, acao=acao,
        status_antes=status_antes, status_depois=contratacao.status,
        motivo=motivo, antes=antes or {}, depois=depois or {},
        criado_por=user, atualizado_por=user,
    )


@transaction.atomic
def preencher_vaga(*, contratacao_pk: int, dados: dict, user):
    """Marca a vaga EM ABERTO como preenchida e registra o contratado.

    `dados`: o `cleaned_data` do formulário (colaborador, colaborador_nome,
    colaborador_email, colaborador_telefone, data_admissao, colaborador_observacoes).
    Recusa vaga que não está em aberto: corrigir quem foi contratado é
    `corrigir_colaborador`, com motivo."""
    _exigir_preencher(user)
    vaga = (
        Contratacao.objects.select_for_update().select_related("colaborador")
        .get(pk=contratacao_pk)
    )
    if vaga.status != StatusContratacaoChoices.ABERTA:
        raise TransicaoRecusada(
            "Só uma vaga em aberto pode ser marcada como preenchida — esta está em "
            f"“{vaga.get_status_display()}”. Para ajustar os dados do contratado, "
            "use “Corrigir dados”."
        )
    status_antes = vaga.status
    definir_contratado(
        contratacao=vaga, colaborador=dados.get("colaborador"),
        nome=dados.get("colaborador_nome"), user=user,
        extras={
            "colaborador_email": (dados.get("colaborador_email") or "").strip(),
            "colaborador_telefone": (dados.get("colaborador_telefone") or "").strip(),
            "colaborador_observacoes": (dados.get("colaborador_observacoes") or "").strip(),
            "data_admissao": dados.get("data_admissao") or None,
        },
    )
    _historico(vaga, ContratacaoVagaHistorico.Acao.PREENCHIMENTO,
               status_antes=status_antes, user=user, depois=_foto_do_contratado(vaga))

    # Cadastrar o colaborador no sistema com os dados da vaga: tudo ou nada —
    # se o cadastro for recusado (homônimo, matrícula em uso), a vaga NÃO fica
    # preenchida pela metade; a transação desfaz e a janela volta com o motivo.
    if dados.get("cadastrar_colaborador") and not dados.get("colaborador"):
        _criar_colaborador_da_vaga(
            vaga, matricula=dados.get("matricula"),
            area_administrativa=bool(dados.get("area_administrativa")), user=user,
        )
    return vaga


def _criar_colaborador_da_vaga(vaga, *, matricula="", area_administrativa=False, user):
    """Cria o `Usuario` (colaborador) a partir do que a VAGA já sabe — nome,
    e-mail, data de admissão, centro de custo, unidade e função — sem pedir de
    novo ao RH. Só a matrícula e a marca "área administrativa" são complemento.

    Não cria duplicado: colaborador ativo com o mesmo nome, e-mail ou matrícula
    recusa o cadastro e aponta para o existente (o RH o seleciona em
    "Colaborador cadastrado"). A hierarquia (diretor, gestor, supervisor…) NÃO
    é preenchida aqui: ela vem da planilha do RH (regra 10 do CLAUDE.md)."""
    from services.movimentacao_service import MovimentacaoEstoqueService

    nome = (vaga.colaborador_nome or "").strip()
    if not nome:
        raise TransicaoRecusada("Informe o nome do contratado antes de cadastrar o colaborador.")
    email = (vaga.colaborador_email or "").strip()
    matricula = (matricula or "").strip()

    homonimo = Usuario.objects.filter(status="ativo", nome__iexact=nome).first()
    if homonimo is not None:
        raise TransicaoRecusada(
            f"Já existe um colaborador ativo chamado “{homonimo.nome}”"
            + (f" (matrícula {homonimo.matricula})" if homonimo.matricula else "")
            + ". Para não duplicar o cadastro, selecione-o em “Colaborador cadastrado”."
        )
    if matricula and Usuario.objects.filter(matricula=matricula).exists():
        raise TransicaoRecusada(f"A matrícula {matricula} já está em uso por outro colaborador.")
    if email and Usuario.objects.filter(status="ativo", email__iexact=email).exists():
        raise TransicaoRecusada("Este e-mail já pertence a outro colaborador ativo.")

    usuario = Usuario.objects.create(
        nome=nome[:100], status="ativo",
        # None, e não "": `matricula` é única, e dois cadastros sem matrícula
        # com "" colidiriam entre si.
        matricula=matricula or None,
        data_inicio=vaga.data_admissao or datetime.date.today(),
        pmb=MovimentacaoEstoqueService.pmb_por_centro_custo(vaga.centro_custo),
        email=email or None,
        centro_custo=vaga.centro_custo, localidade=vaga.localidade, funcao=vaga.funcao,
        area_administrativa=bool(area_administrativa),
        criado_por=user, atualizado_por=user,
    )
    vaga.colaborador = usuario
    vaga.atualizado_por = user
    vaga.save(update_fields=["colaborador", "atualizado_por", "updated_at"])
    reservas.associar_colaborador(vaga, usuario, user)
    _historico(vaga, ContratacaoVagaHistorico.Acao.CADASTRO, status_antes=vaga.status,
               user=user, depois=_foto_do_contratado(vaga))
    registrar_evento(
        vaga, "colaborador",
        f"Colaborador cadastrado no sistema a partir da vaga: {usuario.nome}.", user,
    )
    return usuario


@transaction.atomic
def cadastrar_colaborador(*, contratacao_pk: int, dados: dict, user):
    """Cadastra o colaborador de uma vaga JÁ preenchida que ficou só com o nome.
    Mesma regra do cadastro feito junto com o preenchimento."""
    _exigir_preencher(user)
    vaga = (
        Contratacao.objects.select_for_update().select_related("centro_custo", "localidade", "funcao")
        .get(pk=contratacao_pk)
    )
    if vaga.status not in (StatusContratacaoChoices.VAGA_PREENCHIDA, StatusContratacaoChoices.EQUIPAMENTO_ENTREGUE):
        raise TransicaoRecusada(
            "O colaborador só é cadastrado com a vaga preenchida e em andamento — esta está em "
            f"“{vaga.get_status_display()}”."
        )
    if vaga.colaborador_id:
        raise TransicaoRecusada("Esta vaga já tem um colaborador cadastrado.")
    return _criar_colaborador_da_vaga(
        vaga, matricula=dados.get("matricula"),
        area_administrativa=bool(dados.get("area_administrativa")), user=user,
    )


@transaction.atomic
def corrigir_colaborador(*, contratacao_pk: int, dados: dict, motivo: str, user):
    """Corrige os dados do contratado de uma vaga que JÁ tem contratado. Exige
    motivo, não muda o status e guarda o que mudou (de → para). Sem mudança
    nenhuma, recusa: um registro de "correção" vazio só poluiria a linha do tempo."""
    _exigir_preencher(user)
    motivo = _exigir_motivo(motivo, acao="corrigir os dados do contratado")
    vaga = (
        Contratacao.objects.select_for_update().select_related("colaborador")
        .get(pk=contratacao_pk)
    )
    if vaga.status in (StatusContratacaoChoices.ABERTA, StatusContratacaoChoices.CANCELADA):
        raise TransicaoRecusada(
            "Só se corrige o contratado de uma vaga que já foi preenchida "
            f"— esta está em “{vaga.get_status_display()}”."
        )

    antes = _foto_do_contratado(vaga)
    _aplicar_dados(vaga, dados)
    depois = _foto_do_contratado(vaga)
    mudou = [c for c in _ROTULO_CAMPO if antes.get(c) != depois.get(c)]
    if antes["colaborador_id"] != depois["colaborador_id"] and "colaborador_nome" not in mudou:
        mudou.append("colaborador_nome")  # trocou o cadastro mantendo o mesmo nome escrito
    if not mudou:
        raise TransicaoRecusada("Nenhum dado foi alterado.")

    vaga.atualizado_por = user
    vaga.save(update_fields=[
        "colaborador", "colaborador_nome", "colaborador_email", "colaborador_telefone",
        "colaborador_observacoes", "data_admissao", "atualizado_por", "updated_at",
    ])
    reservas.associar_colaborador(vaga, vaga.colaborador, user)
    _historico(vaga, ContratacaoVagaHistorico.Acao.CORRECAO,
               status_antes=vaga.status, user=user, motivo=motivo, antes=antes, depois=depois)
    registrar_evento(
        vaga, "edicao",
        "Dados do contratado corrigidos (" + ", ".join(_ROTULO_CAMPO[c] for c in mudou)
        + f"). Motivo: {motivo}"[:140],
        user,
    )
    return vaga


@transaction.atomic
def reabrir_vaga(*, contratacao_pk: int, motivo: str, user):
    """Devolve uma vaga PREENCHIDA para "Em aberto" (a contratação não se
    concretizou). Exige motivo.

    Só a etapa "Vaga preenchida": depois dela já há equipamento entregue ou
    vaga concluída, que é trabalho da TI — reabrir isso é `reabrir`, no
    Andamento. Os dados do contratado saem da vaga (ela volta a não ter
    ninguém) e ficam no histórico. Os chamados de TI NÃO são tocados: o que a TI
    já preparou continua visível, e é o RH quem decide, com a TI, o que fazer
    com eles."""
    _exigir_preencher(user)
    motivo = _exigir_motivo(motivo, acao="reabrir a vaga")
    vaga = (
        Contratacao.objects.select_for_update().select_related("colaborador")
        .get(pk=contratacao_pk)
    )
    if vaga.status != StatusContratacaoChoices.VAGA_PREENCHIDA:
        raise TransicaoRecusada(
            "Só uma vaga em “Vaga preenchida” pode ser reaberta por aqui — esta está em "
            f"“{vaga.get_status_display()}”. Etapas seguintes são do TI, em Andamento."
        )

    antes = _foto_do_contratado(vaga)
    if vaga.integracao_confirmada_em:
        antes["integracao_confirmada_em"] = vaga.integracao_confirmada_em.isoformat()
    status_antes = vaga.status
    vaga.colaborador = None
    vaga.integracao_confirmada_em = None
    vaga.integracao_confirmada_por = None
    vaga.integracao_observacao = ""
    vaga.colaborador_nome = ""
    vaga.colaborador_email = ""
    vaga.colaborador_telefone = ""
    vaga.colaborador_observacoes = ""
    vaga.data_admissao = None
    vaga.preenchida_por = None
    vaga.vaga_preenchida_em = None
    vaga.status = StatusContratacaoChoices.ABERTA
    vaga.atualizado_por = user
    vaga.save(update_fields=[
        "colaborador", "colaborador_nome", "colaborador_email", "colaborador_telefone",
        "colaborador_observacoes", "data_admissao", "preenchida_por", "vaga_preenchida_em",
        "status", "integracao_confirmada_em", "integracao_confirmada_por",
        "integracao_observacao", "atualizado_por", "updated_at",
    ])
    # As reservas continuam da VAGA (o equipamento segue reservado), só deixam de
    # ter colaborador: a vaga voltou a não ter ninguém.
    reservas.associar_colaborador(vaga, None, user)
    _historico(vaga, ContratacaoVagaHistorico.Acao.REABERTURA,
               status_antes=status_antes, user=user, motivo=motivo, antes=antes)
    registrar_evento(
        vaga, "etapa",
        f"Vaga reaberta (status: {StatusContratacaoChoices(status_antes).label} → "
        f"{vaga.get_status_display()}). Motivo: {motivo}"[:250],
        user,
    )
    return vaga


@transaction.atomic
def registrar_entrega(*, contratacao, item, sem_equipamento, user):
    """Etapa 3. Liga a vaga ao equipamento que JÁ foi entregue — a transferência
    de estoque continua sendo feita em Movimentações (ver o cabeçalho).

    A entrega só vale com a movimentação patrimonial de entrega, em nome do
    colaborador da vaga: sem ela o sistema recusa. Equipamento reservado não
    passa por aqui — a própria movimentação de entrega registra isso na vaga."""
    if sem_equipamento:
        contratacao.computador = TipoComputadorChoices.NENHUM
        contratacao.item_entregue = None
        descricao = "Contratação marcada como sem equipamento."
        # Sem equipamento não combina com reserva ativa: o que estava reservado
        # volta para o Backup.
        reservas.liberar_da_vaga(
            contratacao, motivo_tipo=ReservaEquipamento.Motivo.MANUAL,
            motivo="Contratação marcada como sem equipamento", user=user,
        )
    else:
        if not contratacao.colaborador_id:
            raise TransicaoRecusada(
                "Cadastre o colaborador da vaga antes de registrar a entrega — a entrega é "
                "feita em nome dele."
            )
        if not entrega_efetiva(item, contratacao.colaborador):
            raise TransicaoRecusada(
                f"Não há movimentação de entrega de “{item.nome}” em nome de "
                f"{contratacao.colaborador.nome}. Faça a entrega em Movimentações (ou pelo "
                "termo com assinatura) e registre depois."
            )
        contratacao.item_entregue = item
        descricao = f"Equipamento entregue: {item.nome}"
        if item.numero_serie:
            descricao += f" (série {item.numero_serie})"

    contratacao.equipamento_entregue_em = timezone.now()
    contratacao.atualizado_por = user
    campos = [
        "item_entregue", "computador", "equipamento_entregue_em",
        "atualizado_por", "updated_at",
    ]

    if _ordem_etapa(contratacao.status) < _ORDEM_ETAPA[StatusContratacaoChoices.EQUIPAMENTO_ENTREGUE]:
        contratacao.status = StatusContratacaoChoices.EQUIPAMENTO_ENTREGUE
        campos.append("status")
        if not contratacao.vaga_preenchida_em:
            contratacao.vaga_preenchida_em = timezone.now()
            campos.append("vaga_preenchida_em")

    contratacao.save(update_fields=campos)
    registrar_evento(contratacao, "etapa", descricao, user)
    return contratacao


def _exigir_concluir(user) -> None:
    if not ar.pode_concluir_contratacao(user):
        raise PermissionDenied("Seu acesso não permite confirmar a integração nem concluir a contratação.")


def _recarregar(contratacao_pk):
    """A vaga TRAVADA e com tudo que a avaliação das etapas lê. É sobre o
    estado de AGORA, dentro da transação, que se decide — não sobre o que a
    tela mostrava quando foi aberta."""
    # Tranca a linha numa consulta SIMPLES e relê com tudo: `queryset_base` tem
    # `annotate(Count)`, e `FOR UPDATE` com GROUP BY não existe em todo banco.
    Contratacao.objects.select_for_update().filter(pk=contratacao_pk).first()
    return queryset_base().prefetch_related(*PREFETCH_DO_PROCESSO).get(pk=contratacao_pk)


@transaction.atomic
def concluir(*, contratacao, user):
    """Etapa final. Só conclui quando TODA etapa obrigatória está feita ou
    dispensada: o cadastro do colaborador, a reserva e a entrega do equipamento
    (por movimentação), o usuário e e-mail, os sistemas, as orientações e a
    integração confirmada.

    Devolve `(True, [])` ou `(False, faltas)` — com as frases do que falta. Não
    há "concluir mesmo assim": "concluída" tira a vaga do quadro, e uma
    contratação concluída com etapa em aberto esconde o pendente. A saída
    legítima para o que não se aplica é DISPENSAR a etapa, com justificativa.

    O que a conclusão NÃO faz: encerrar chamado de TI. O chamado tem andamento
    próprio e é lido aqui como fato (uma das etapas)."""
    _exigir_concluir(user)
    vaga = _recarregar(contratacao.pk)

    if vaga.status == StatusContratacaoChoices.CONCLUIDA:
        return True, []
    if vaga.status not in (StatusContratacaoChoices.VAGA_PREENCHIDA, StatusContratacaoChoices.EQUIPAMENTO_ENTREGUE):
        return False, [
            "A vaga precisa estar preenchida para a contratação ser concluída "
            f"(situação atual: {vaga.get_status_display()})."
        ]

    faltas = pendencias(vaga)
    if faltas:
        return False, faltas

    vaga.status = StatusContratacaoChoices.CONCLUIDA
    vaga.concluida_em = timezone.now()
    vaga.atualizado_por = user
    vaga.save(update_fields=["status", "concluida_em", "atualizado_por", "updated_at"])
    registrar_evento(vaga, "etapa", "Contratação concluída — todas as etapas feitas ou dispensadas.", user)
    # O objeto que o chamador segurou reflete o novo estado.
    contratacao.status = vaga.status
    contratacao.concluida_em = vaga.concluida_em
    return True, []


@transaction.atomic
def confirmar_integracao(*, contratacao_pk: int, observacao: str = "", user) -> Contratacao:
    """O RH confirma que o colaborador foi integrado. Grava quem, quando e a
    observação. Não conclui a contratação: a conclusão é outro ato, e só passa
    com o restante em dia."""
    _exigir_concluir(user)
    vaga = Contratacao.objects.select_for_update().get(pk=contratacao_pk)
    if vaga.status not in (StatusContratacaoChoices.VAGA_PREENCHIDA, StatusContratacaoChoices.EQUIPAMENTO_ENTREGUE):
        raise TransicaoRecusada(
            "A integração só se confirma com a vaga preenchida e em andamento — esta está em "
            f"“{vaga.get_status_display()}”."
        )
    if vaga.integracao_confirmada_em:
        raise TransicaoRecusada("A integração já foi confirmada.")
    vaga.integracao_confirmada_em = timezone.now()
    vaga.integracao_confirmada_por = user
    vaga.integracao_observacao = (observacao or "").strip()[:255]
    vaga.atualizado_por = user
    vaga.save(update_fields=[
        "integracao_confirmada_em", "integracao_confirmada_por", "integracao_observacao",
        "atualizado_por", "updated_at",
    ])
    registrar_evento(vaga, "integracao", "Integração do colaborador confirmada.", user)
    return vaga


@transaction.atomic
def desfazer_integracao(*, contratacao_pk: int, motivo: str, user) -> Contratacao:
    """Desfaz a confirmação feita por engano. Exige motivo e só vale enquanto a
    contratação não foi concluída (depois, é `reabrir`)."""
    _exigir_concluir(user)
    motivo = _exigir_motivo(motivo, acao="desfazer a confirmação da integração")
    vaga = Contratacao.objects.select_for_update().get(pk=contratacao_pk)
    if vaga.status == StatusContratacaoChoices.CONCLUIDA:
        raise TransicaoRecusada("A contratação já foi concluída — reabra-a para corrigir a integração.")
    if not vaga.integracao_confirmada_em:
        raise TransicaoRecusada("A integração não está confirmada.")
    vaga.integracao_confirmada_em = None
    vaga.integracao_confirmada_por = None
    vaga.integracao_observacao = ""
    vaga.atualizado_por = user
    vaga.save(update_fields=[
        "integracao_confirmada_em", "integracao_confirmada_por", "integracao_observacao",
        "atualizado_por", "updated_at",
    ])
    registrar_evento(vaga, "integracao", f"Confirmação da integração desfeita. Motivo: {motivo}"[:250], user)
    return vaga


# ── Dispensar / registrar etapas (decisão do TI) ─────────────────────

def _exigir_ti(user) -> None:
    if not ar.pode_gerenciar_contratacao(user):
        raise PermissionDenied("Só o TI dispensa ou registra etapas do processo.")


def _etapa_valida(chave: str) -> str:
    if chave not in ROTULO_DA_ETAPA:
        raise TransicaoRecusada("Etapa desconhecida.")
    return chave


def _vigente(vaga, chave):
    return (
        ContratacaoEtapaRegistro.objects.select_for_update()
        .filter(contratacao=vaga, chave=chave, desfeita_em__isnull=True).first()
    )


@transaction.atomic
def dispensar_etapa(*, contratacao_pk: int, chave: str, justificativa: str, user):
    """O TI diz que uma etapa NÃO SE APLICA a esta contratação, e por quê. A
    justificativa é obrigatória e fica no registro e na linha do tempo: é a
    única saída para concluir sem a etapa, e precisa deixar rastro."""
    _exigir_ti(user)
    chave = _etapa_valida(chave)
    if chave in _NAO_DISPENSAVEIS:
        raise TransicaoRecusada(f"“{ROTULO_DA_ETAPA[chave]}” não pode ser dispensada.")
    justificativa = _exigir_motivo(justificativa, acao="dispensar a etapa")
    vaga = _recarregar(contratacao_pk)
    if vaga.encerrada:
        raise TransicaoRecusada(f"A contratação está “{vaga.get_status_display()}”.")
    etapa = next(e for e in etapas_do_processo(vaga) if e.chave == chave)
    if etapa.concluida:
        raise TransicaoRecusada("Esta etapa já está concluída — não há o que dispensar.")
    if etapa.dispensada:
        raise TransicaoRecusada("Esta etapa já está dispensada.")
    ContratacaoEtapaRegistro.objects.create(
        contratacao=vaga, chave=chave, estado=ContratacaoEtapaRegistro.Estado.DISPENSADA,
        justificativa=justificativa[:255], criado_por=user, atualizado_por=user,
    )
    registrar_evento(vaga, "etapa", f"Etapa dispensada: {ROTULO_DA_ETAPA[chave]}. {justificativa}"[:250], user)
    return vaga


@transaction.atomic
def registrar_etapa(*, contratacao_pk: int, chave: str, observacao: str, user):
    """O TI registra que uma etapa SEM fato que a prove foi feita (as orientações
    de uso dos sistemas). Fica com a observação, o autor e o horário."""
    _exigir_ti(user)
    chave = _etapa_valida(chave)
    if chave not in _REGISTRAVEIS:
        raise TransicaoRecusada(f"“{ROTULO_DA_ETAPA[chave]}” não se registra à mão — ela se comprova pelo próprio processo.")
    vaga = _recarregar(contratacao_pk)
    if vaga.encerrada:
        raise TransicaoRecusada(f"A contratação está “{vaga.get_status_display()}”.")
    if _vigente(vaga, chave) is not None:
        raise TransicaoRecusada("Esta etapa já tem registro.")
    ContratacaoEtapaRegistro.objects.create(
        contratacao=vaga, chave=chave, estado=ContratacaoEtapaRegistro.Estado.CONCLUIDA,
        justificativa=(observacao or "").strip()[:255], criado_por=user, atualizado_por=user,
    )
    registrar_evento(vaga, "etapa", f"Etapa registrada: {ROTULO_DA_ETAPA[chave]}.", user)
    return vaga


@transaction.atomic
def desfazer_registro_de_etapa(*, contratacao_pk: int, chave: str, motivo: str, user):
    """Desfaz uma dispensa ou um registro manual. O registro NÃO é apagado: fica
    como histórico, marcado como desfeito (por quem e quando)."""
    _exigir_ti(user)
    chave = _etapa_valida(chave)
    motivo = _exigir_motivo(motivo, acao="desfazer o registro da etapa")
    vaga = _recarregar(contratacao_pk)
    if vaga.status == StatusContratacaoChoices.CONCLUIDA:
        raise TransicaoRecusada("A contratação já foi concluída — reabra-a para alterar etapas.")
    registro = _vigente(vaga, chave)
    if registro is None:
        raise TransicaoRecusada("Esta etapa não tem registro a desfazer.")
    registro.desfeita_em = timezone.now()
    registro.desfeita_por = user
    registro.atualizado_por = user
    registro.save(update_fields=["desfeita_em", "desfeita_por", "atualizado_por", "updated_at"])
    registrar_evento(
        vaga, "etapa",
        f"Registro desfeito ({registro.get_estado_display().lower()}): {ROTULO_DA_ETAPA[chave]}. {motivo}"[:250],
        user,
    )
    return vaga


@transaction.atomic
def cancelar(*, contratacao, motivo, user):
    contratacao.status = StatusContratacaoChoices.CANCELADA
    contratacao.cancelada_em = timezone.now()
    contratacao.motivo_cancelamento = (motivo or "").strip()[:255]
    contratacao.atualizado_por = user
    contratacao.save(update_fields=[
        "status", "cancelada_em", "motivo_cancelamento", "atualizado_por", "updated_at",
    ])
    # Vaga cancelada sem contratação: o equipamento reservado volta ao Backup
    # (o que já foi ENTREGUE não é tocado) e a reserva fica como histórico.
    reservas.liberar_da_vaga(
        contratacao, motivo_tipo=ReservaEquipamento.Motivo.CANCELAMENTO,
        motivo=f"Vaga cancelada. {contratacao.motivo_cancelamento}".strip(), user=user,
    )
    registrar_evento(
        contratacao, "cancelamento",
        f"Contratação cancelada. {contratacao.motivo_cancelamento}".strip(), user,
    )
    return contratacao


@transaction.atomic
def reabrir(*, contratacao, user):
    """Volta uma contratação concluída/cancelada para a etapa que os fatos
    dela sustentam — não para "em aberto" às cegas. Reabrir e perder o
    equipamento já registrado obrigaria a redigitar o que já é verdade."""
    if contratacao.item_entregue_id or contratacao.equipamento_entregue_em:
        destino = StatusContratacaoChoices.EQUIPAMENTO_ENTREGUE
    elif contratacao.colaborador_id or contratacao.colaborador_nome:
        destino = StatusContratacaoChoices.VAGA_PREENCHIDA
    else:
        destino = StatusContratacaoChoices.ABERTA

    contratacao.status = destino
    contratacao.concluida_em = None
    contratacao.cancelada_em = None
    contratacao.motivo_cancelamento = ""
    contratacao.atualizado_por = user
    contratacao.save(update_fields=[
        "status", "concluida_em", "cancelada_em", "motivo_cancelamento",
        "atualizado_por", "updated_at",
    ])
    registrar_evento(
        contratacao, "etapa",
        f"Contratação reaberta em “{contratacao.get_status_display()}”.", user,
    )
    return contratacao


@transaction.atomic
def alternar_sistema(*, linha, user):
    """Marca/desmarca um acesso como criado. Devolve a linha atualizada."""
    linha.provisionado = not linha.provisionado
    linha.provisionado_em = timezone.now() if linha.provisionado else None
    linha.atualizado_por = user
    linha.save(update_fields=["provisionado", "provisionado_em", "atualizado_por", "updated_at"])

    verbo = "criado" if linha.provisionado else "desmarcado"
    registrar_evento(
        linha.contratacao, "provisionamento",
        f"Acesso {verbo}: {linha.sistema.nome}.", user,
    )
    return linha


@transaction.atomic
def alternar_licenca(*, linha, user):
    linha.provisionada = not linha.provisionada
    linha.provisionada_em = timezone.now() if linha.provisionada else None
    linha.atualizado_por = user
    linha.save(update_fields=["provisionada", "provisionada_em", "atualizado_por", "updated_at"])

    verbo = "atribuído" if linha.provisionada else "desmarcado"
    registrar_evento(
        linha.contratacao, "provisionamento",
        f"Assento de licença {verbo}: {linha.licenca.nome}.", user,
    )
    return linha


def _ordem_etapa(status) -> int:
    """Posição da etapa no fluxo. Cancelada fica fora da régua (-1): ela não
    é um ponto do caminho, é uma saída dele."""
    return _ORDEM_ETAPA.get(status, -1)


def catalogo_de_sistemas():
    return SistemaCorporativo.objects.select_related("licenca").order_by("ordem", "nome")
