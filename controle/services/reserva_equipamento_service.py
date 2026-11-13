"""
Reserva de equipamentos para vagas em aberto — regra de negócio.

O TI olha as vagas abertas, escolhe no estoque de BACKUP os equipamentos que
aquela contratação vai precisar e os reserva. Este módulo é a fonte única de
cinco perguntas que as telas, os formulários de movimentação e o e-mail de
alerta precisam responder do mesmo jeito:

  1. O QUE pode ser reservado (`itens_elegiveis`);
  2. COMO reservar e liberar sem deixar o inventário inconsistente
     (`reservar`, `liberar`, `liberar_da_vaga`);
  3. O QUE acontece quando a vaga ganha um colaborador (`associar_colaborador`);
  4. O QUE acontece quando o equipamento é movimentado — entregue ao
     colaborador da vaga (fecha a reserva) ou usado em outra necessidade
     (cede a reserva) — (`tratar_movimentacao`);
  5. QUEM está sem equipamento e se o TI já foi avisado
     (`pendencias_de_equipamento`, `alertar_vaga_sem_equipamento`).

Quatro decisões que a tela não mostra e que sustentam o resto:

  • RESERVA NÃO É ENTREGA. Reservar só troca o status do equipamento
    (Backup → Reservado) e grava `ReservaEquipamento`. Não move patrimônio, não
    troca centro de custo, não dá baixa e não gera termo. A entrega é a
    movimentação feita pelo TI em nome do colaborador (`MovimentacaoEstoqueService`),
    e é ELA que fecha a reserva — por isso o gancho `tratar_movimentacao`.

  • A UNICIDADE É DO BANCO. `reserva_ativa_unica_por_item` impede o mesmo
    equipamento em duas vagas: dois TIs clicando juntos esbarram no mesmo
    INSERT, e o segundo recebe "já reservado". Um `if` na view não aguenta duas
    requisições simultâneas; a constraint aguenta.

  • NADA É APAGADO. Liberar, ceder e entregar mudam o `estado` da linha e gravam
    quem, quando e por quê. É dali que sai "que máquinas já passaram por esta
    vaga" e "este equipamento já foi reservado para quem".

  • O STATUS DA VAGA NÃO MUDA POR FALTA DE EQUIPAMENTO. Vaga sem máquina é uma
    PENDÊNCIA (`pendencias_de_equipamento`), tratada separada: nunca preenche,
    cancela nem conclui a vaga.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from ProjetoEstoque.models import (
    Contratacao,
    ContratacaoEvento,
    Item,
    ReservaEquipamento as Reserva,
    SimNaoChoices,
    StatusContratacaoChoices,
    StatusItemChoices,
)
from services import acesso_restrito_service as ar

logger = logging.getLogger(__name__)

Estado = Reserva.Estado
Motivo = Reserva.Motivo

# Tipos que o TI reserva no dia a dia — o que o seletor mostra por padrão. O
# resto do estoque ("outros tipos") fica a um clique: o critério de elegibilidade
# (`itens_elegiveis`) não depende desta lista. `Subtipo.alocado` NÃO serve de
# critério: no cadastro real, Notebook, Desktop, Monitor, Celular e Tablet estão
# como "não alocado".
TIPOS_USUAIS = ("Notebook", "Desktop", "Monitor", "Tablet", "Celular", "IP-Phone")

# Vaga que ainda pode ter equipamento reservado, e vaga que já fechou.
_STATUS_VAGA_ENCERRADA = (StatusContratacaoChoices.CONCLUIDA, StatusContratacaoChoices.CANCELADA)


class ReservaRecusada(Exception):
    """A regra de negócio disse não. A mensagem já vem em texto de tela."""


@dataclass
class ResultadoMovimentacao:
    """O que `tratar_movimentacao` decidiu sobre uma movimentação de item
    reservado. A view usa para escolher a mensagem e levar o TI à vaga."""
    tipo: str                      # "entrega" | "cessao"
    reserva: Reserva
    candidatos: int = 0            # outros equipamentos em Backup do mesmo tipo
    alerta_disparado: bool = False
    avisos: list = field(default_factory=list)


# ─────────────────────────────────────────────────────────────
# Rótulos e permissão
# ─────────────────────────────────────────────────────────────

def rotulo_item(item) -> str:
    """Como o equipamento aparece nas telas, no histórico e no e-mail. Só o que
    identifica: tipo, nome/modelo e número de série."""
    partes = []
    if item.subtipo_id and item.subtipo.nome and item.subtipo.nome.lower() not in (item.nome or "").lower():
        partes.append(item.subtipo.nome)
    partes.append(item.nome or f"Equipamento {item.pk}")
    if item.numero_serie:
        partes.append(f"série {item.numero_serie}")
    return " · ".join(partes)[:200]


def rotulo_vaga(vaga) -> str:
    return f"Vaga nº {vaga.pk} — {vaga.cargo}"[:200]


def _exigir_ti(user) -> None:
    # Terceira camada de autorização (as outras são o middleware e o decorador
    # da view). Reservar é ATENDER a vaga, e atender é do TI.
    if not ar.pode_gerenciar_contratacao(user):
        raise PermissionDenied("Só o TI reserva ou libera equipamentos.")


def _evento(vaga, descricao: str, user) -> None:
    if vaga is None:
        return
    ContratacaoEvento.objects.create(
        contratacao=vaga, tipo="reserva", descricao=descricao[:255],
        criado_por=user, atualizado_por=user,
    )


def _exigir_motivo(motivo: str, *, acao: str) -> str:
    motivo = (motivo or "").strip()
    if len(motivo) < 3:
        raise ReservaRecusada(f"Informe o motivo para {acao}.")
    return motivo[:500]


def _salvar_item(item, *, status, user) -> None:
    """Muda o status pelo `save()` — e não por `update()` — para o histórico de
    status e as regras que dependem dele (preventiva, locação, alerta de
    computador parado) enxergarem a mudança como em qualquer outra."""
    item.status = status
    item.atualizado_por = user if (user and getattr(user, "is_authenticated", False)) else item.atualizado_por
    item.save(update_fields=["status", "atualizado_por", "updated_at"])


# ─────────────────────────────────────────────────────────────
# Leitura
# ─────────────────────────────────────────────────────────────

def itens_elegiveis(*, tipo: str = "", busca: str = "", todos_os_tipos: bool = False):
    """Equipamentos que PODEM ser reservados: em Backup, não de consumo, não
    excluídos e sem reserva ativa. É a regra — vale para qualquer tipo.

    `tipo`/`todos_os_tipos` só filtram o que o seletor MOSTRA: por padrão os
    tipos do dia a dia (`TIPOS_USUAIS`); `todos_os_tipos=True` libera "outros
    tipos" do estoque."""
    qs = (
        Item.objects
        .filter(status=StatusItemChoices.BACKUP, item_consumo=SimNaoChoices.NAO)
        .exclude(reservas__estado=Estado.ATIVA)
        .select_related("subtipo", "localidade", "centro_custo")
    )
    tipo = (tipo or "").strip()
    if tipo:
        qs = qs.filter(subtipo__nome__icontains=tipo)
    elif not todos_os_tipos:
        filtro = Q()
        for nome in TIPOS_USUAIS:
            filtro |= Q(subtipo__nome__icontains=nome)
        qs = qs.filter(filtro)

    busca = (busca or "").strip()
    if busca:
        qs = qs.filter(
            Q(nome__icontains=busca) | Q(numero_serie__icontains=busca)
            | Q(marca__icontains=busca) | Q(modelo__icontains=busca)
        )
    return qs.order_by("subtipo__nome", "nome", "pk")


def candidatos_para_substituir(reserva):
    """Outros equipamentos em Backup do MESMO tipo do que foi usado em outra
    necessidade — a resposta a "dá para repor a reserva da vaga?"."""
    if not reserva.item.subtipo_id:
        return Item.objects.none()
    return itens_elegiveis(todos_os_tipos=True).filter(
        subtipo_id=reserva.item.subtipo_id,
    ).exclude(pk=reserva.item_id)


def reserva_ativa_do_item(item):
    """A reserva que prende este equipamento agora (ou `None`)."""
    return (
        Reserva.objects.select_related("contratacao", "colaborador")
        .filter(item=item, estado=Estado.ATIVA).first()
    )


def reservas_da_vaga(vaga) -> list:
    return list(
        vaga.reservas.select_related(
            "item", "item__subtipo", "colaborador", "encerrada_por", "criado_por", "substituida_por",
        ).order_by("-reservado_em", "-pk")
    )


def _cobertas(vaga) -> set:
    """Tipos (`subtipo_id`) que a vaga TEM no momento: reservado ou já entregue."""
    return set(
        Reserva.objects.filter(contratacao=vaga, estado__in=(Estado.ATIVA, Estado.ENTREGUE))
        .values_list("item__subtipo_id", flat=True)
    )


def pendencias_de_equipamento(vaga) -> dict:
    """O que falta de equipamento nesta vaga — SEM mexer no status dela.

    `cedidas`: reservas cujo equipamento foi usado em outra necessidade e que
    ninguém repôs (e cujo tipo a vaga não tem de volta). `sem_reserva`: a vaga
    precisa de computador e nunca teve nada reservado nem entregue.
    `pendente` é qualquer um dos dois, e só vale para vaga que ainda pode
    receber equipamento."""
    vazio = {"cedidas": [], "sem_reserva": False, "pendente": False}
    if vaga.encerrada:
        return vazio

    reservas = list(vaga.reservas.select_related("item", "item__subtipo").all())
    cobertas = {
        r.item.subtipo_id for r in reservas if r.estado in (Estado.ATIVA, Estado.ENTREGUE)
    }
    cedidas = [
        r for r in reservas
        if r.estado == Estado.CEDIDA and r.substituida_por_id is None
        and r.item.subtipo_id not in cobertas
    ]
    tem_algo = any(r.estado in (Estado.ATIVA, Estado.ENTREGUE) for r in reservas)
    sem_reserva = (
        vaga.precisa_computador and not tem_algo and not vaga.item_entregue_id and not cedidas
    )
    return {"cedidas": cedidas, "sem_reserva": bool(sem_reserva), "pendente": bool(cedidas or sem_reserva)}


def vagas_sem_equipamento(vagas) -> dict:
    """`{pk: "cedida" | "sem_reserva"}` para as vagas de `vagas` que estão
    pendentes de equipamento. UMA consulta para qualquer quantidade de vagas —
    é o que o quadro usa para pôr o selo em cada cartão."""
    candidatas = [v for v in vagas if not v.encerrada and v.status != StatusContratacaoChoices.EQUIPAMENTO_ENTREGUE]
    if not candidatas:
        return {}
    ids = [v.pk for v in candidatas]
    com_reserva = set(
        Reserva.objects.filter(contratacao_id__in=ids, estado__in=(Estado.ATIVA, Estado.ENTREGUE))
        .values_list("contratacao_id", flat=True)
    )
    com_cedida = set(
        Reserva.objects.filter(contratacao_id__in=ids, estado=Estado.CEDIDA, substituida_por__isnull=True)
        .values_list("contratacao_id", flat=True)
    )
    saida = {}
    for v in candidatas:
        if v.pk in com_cedida and v.pk not in com_reserva:
            saida[v.pk] = "cedida"
        elif v.precisa_computador and v.pk not in com_reserva and not v.item_entregue_id:
            saida[v.pk] = "sem_reserva"
    return saida


# ─────────────────────────────────────────────────────────────
# Reservar
# ─────────────────────────────────────────────────────────────

def reservar(*, contratacao_pk: int, item_ids, user, adicional: bool = False, substitui_pk=None) -> list:
    """Reserva os equipamentos `item_ids` para a vaga. Tudo ou nada: se UM não
    puder ser reservado, nenhum é (a transação desfaz) e a mensagem diz qual.

    A disponibilidade é REVALIDADA aqui, com o equipamento travado — o que a
    tela mostrava quando foi aberta não vale. `adicional=True` é a confirmação
    do TI de que a vaga precisa de mais de um equipamento do mesmo tipo; sem
    ela, reservar um segundo notebook para a mesma vaga é recusado (quase
    sempre é um clique repetido ou a vaga errada)."""
    _exigir_ti(user)
    ids = list(dict.fromkeys(int(i) for i in item_ids if str(i).strip()))
    if not ids:
        raise ReservaRecusada("Selecione ao menos um equipamento para reservar.")

    criadas = []
    with transaction.atomic():
        try:
            vaga = Contratacao.objects.select_for_update().select_related("colaborador").get(pk=contratacao_pk)
        except Contratacao.DoesNotExist:
            raise ReservaRecusada("Esta vaga não existe mais.") from None
        if vaga.encerrada:
            raise ReservaRecusada(
                f"Esta vaga está “{vaga.get_status_display()}” — não recebe mais equipamento reservado."
            )

        ja_na_vaga = set(
            Reserva.objects.filter(contratacao=vaga, estado__in=(Estado.ATIVA, Estado.ENTREGUE))
            .values_list("item__subtipo_id", flat=True)
        )
        tipos_neste_pedido = set()

        for item_id in ids:
            item = (
                Item.objects.select_for_update().select_related("subtipo")
                .filter(pk=item_id).first()
            )
            if item is None:
                raise ReservaRecusada("Um dos equipamentos escolhidos não existe mais.")
            rotulo = rotulo_item(item)

            if item.item_consumo == SimNaoChoices.SIM:
                raise ReservaRecusada(f"“{rotulo}” é material de consumo e não pode ser reservado.")
            if item.status != StatusItemChoices.BACKUP:
                if item.status == StatusItemChoices.RESERVADO:
                    outra = reserva_ativa_do_item(item)
                    destino = outra.vaga_rotulo if outra else "outra vaga"
                    raise ReservaRecusada(f"“{rotulo}” já está reservado para {destino}.")
                raise ReservaRecusada(
                    f"Só equipamento em Backup pode ser reservado — “{rotulo}” está "
                    f"“{item.get_status_display()}”."
                )

            tipo_id = item.subtipo_id
            if tipo_id and (tipo_id in ja_na_vaga or tipo_id in tipos_neste_pedido) and not adicional:
                raise ReservaRecusada(
                    f"Esta vaga já tem {item.subtipo.nome} reservado ou entregue. Se ela precisa de "
                    "mais de um, marque “A vaga precisa de mais de um equipamento deste tipo”."
                )
            tipos_neste_pedido.add(tipo_id)

            try:
                with transaction.atomic():
                    reserva = Reserva.objects.create(
                        contratacao=vaga, vaga_rotulo=rotulo_vaga(vaga),
                        item=item, item_rotulo=rotulo, colaborador=vaga.colaborador,
                        estado=Estado.ATIVA, status_anterior=item.status,
                        adicional=bool(adicional and (tipo_id in ja_na_vaga)),
                        criado_por=user, atualizado_por=user,
                    )
            except IntegrityError:
                raise ReservaRecusada(f"“{rotulo}” acabou de ser reservado por outra pessoa.") from None

            _salvar_item(item, status=StatusItemChoices.RESERVADO, user=user)
            _evento(vaga, f"Equipamento reservado: {rotulo}.", user)
            criadas.append(reserva)

        _ligar_substituicao(vaga, criadas, substitui_pk)
    return criadas


def _ligar_substituicao(vaga, novas, substitui_pk) -> None:
    """Reservar de volta o tipo que tinha sido usado em outra necessidade resolve
    aquela pendência: a reserva antiga passa a apontar para a nova. Com
    `substitui_pk` (o TI clicou em "reservar substituto" numa pendência) liga
    àquela; sem ele, liga pela coincidência de tipo."""
    for nova in novas:
        alvo = None
        if substitui_pk:
            alvo = Reserva.objects.filter(
                pk=substitui_pk, contratacao=vaga, estado=Estado.CEDIDA, substituida_por__isnull=True,
            ).first()
            substitui_pk = None  # só a primeira nova serve àquela pendência
        if alvo is None and nova.item.subtipo_id:
            alvo = Reserva.objects.filter(
                contratacao=vaga, estado=Estado.CEDIDA, substituida_por__isnull=True,
                item__subtipo_id=nova.item.subtipo_id,
            ).order_by("encerrada_em", "pk").first()
        if alvo is not None:
            alvo.substituida_por = nova
            alvo.save(update_fields=["substituida_por", "updated_at"])
            _evento(
                vaga, f"Reserva reposta: {nova.item_rotulo} no lugar de {alvo.item_rotulo}.", nova.criado_por,
            )


# ─────────────────────────────────────────────────────────────
# Liberar
# ─────────────────────────────────────────────────────────────

def _encerrar(reserva, *, estado, motivo_tipo, motivo, user, movimentacao=None) -> None:
    reserva.estado = estado
    reserva.motivo_tipo = motivo_tipo
    reserva.motivo = (motivo or "")[:500]
    reserva.encerrada_em = timezone.now()
    reserva.encerrada_por = user if (user and getattr(user, "is_authenticated", False)) else None
    reserva.atualizado_por = reserva.encerrada_por
    if movimentacao is not None:
        reserva.movimentacao = movimentacao
    reserva.save()


def _devolver_ao_status_anterior(reserva, user) -> None:
    """Item de volta ao status de antes — só se ele AINDA está Reservado. Se
    alguém já o mexeu (manutenção, entrega), o status atual vale e não se pisa."""
    item = Item.objects.select_for_update().filter(pk=reserva.item_id).first()
    if item is None or item.status != StatusItemChoices.RESERVADO:
        return
    _salvar_item(item, status=reserva.status_anterior or StatusItemChoices.BACKUP, user=user)


@transaction.atomic
def liberar(*, reserva_pk: int, motivo: str, user) -> Reserva:
    """O TI desfaz uma reserva: o equipamento volta para o Backup e a linha fica
    como histórico (`liberada`). Exige motivo. Só reserva ATIVA — equipamento já
    entregue não se libera por aqui (é devolução, em Movimentações)."""
    _exigir_ti(user)
    motivo = _exigir_motivo(motivo, acao="liberar o equipamento")
    reserva = Reserva.objects.select_for_update().select_related("contratacao").filter(pk=reserva_pk).first()
    if reserva is None:
        raise ReservaRecusada("Esta reserva não existe.")
    if reserva.estado != Estado.ATIVA:
        raise ReservaRecusada(
            f"Esta reserva já está “{reserva.get_estado_display()}” — só a reserva ativa pode ser liberada. "
            "Equipamento entregue volta pela devolução, em Movimentações."
        )
    _encerrar(reserva, estado=Estado.LIBERADA, motivo_tipo=Motivo.MANUAL, motivo=motivo, user=user)
    _devolver_ao_status_anterior(reserva, user)
    _evento(reserva.contratacao, f"Reserva liberada: {reserva.item_rotulo}. Motivo: {motivo}", user)
    return reserva


def liberar_da_vaga(vaga, *, motivo_tipo: str, motivo: str, user) -> list:
    """Libera TODAS as reservas ativas da vaga — para quando ela é cancelada ou
    excluída sem ter sido preenchida. Roda na transação de quem chama (cancelar
    e excluir já são atômicos) e SEM checar permissão: quem decidiu cancelar já
    foi autorizado. Equipamento já ENTREGUE não é tocado."""
    liberadas = []
    for reserva in Reserva.objects.select_for_update().filter(contratacao=vaga, estado=Estado.ATIVA):
        _encerrar(reserva, estado=Estado.LIBERADA, motivo_tipo=motivo_tipo, motivo=motivo, user=user)
        _devolver_ao_status_anterior(reserva, user)
        liberadas.append(reserva)
    if liberadas:
        nomes = "; ".join(r.item_rotulo for r in liberadas)
        _evento(vaga, f"Reserva(s) liberada(s) — {motivo}: {nomes}", user)
    return liberadas


# ─────────────────────────────────────────────────────────────
# O colaborador entra na vaga
# ─────────────────────────────────────────────────────────────

@transaction.atomic
def associar_colaborador(vaga, colaborador, user) -> int:
    """Liga as reservas ATIVAS da vaga ao colaborador (o nome vira cadastro, ou
    muda o cadastro). Devolve quantas. Com `colaborador=None` desfaz o elo — a
    vaga reaberta volta a não ter ninguém, mas as reservas continuam dela."""
    reservas = list(Reserva.objects.select_for_update().filter(contratacao=vaga, estado=Estado.ATIVA))
    mudaram = [r for r in reservas if r.colaborador_id != (colaborador.pk if colaborador else None)]
    for reserva in mudaram:
        reserva.colaborador = colaborador
        reserva.atualizado_por = user
        reserva.save(update_fields=["colaborador", "atualizado_por", "updated_at"])
    if mudaram:
        if colaborador:
            _evento(vaga, f"{len(mudaram)} reserva(s) associada(s) a {colaborador.nome}.", user)
        else:
            _evento(vaga, f"{len(mudaram)} reserva(s) desassociada(s) do colaborador (vaga reaberta).", user)
    return len(mudaram)


# ─────────────────────────────────────────────────────────────
# O equipamento é movimentado
# ─────────────────────────────────────────────────────────────

def exige_confirmacao(item, *, tipo_movimentacao: str, tipo_transferencia: str, usuario_id) -> "Reserva | None":
    """A reserva que PRECISA de confirmação para esta movimentação, ou `None`.

    Entregar o equipamento reservado ao colaborador da própria vaga é o uso
    previsto — não pede nada. Qualquer outra coisa (entregar a outra pessoa,
    mandar para manutenção, transferir) tira o equipamento de uma vaga: pede
    confirmação e justificativa, e é isso que o formulário usa para avisar."""
    if item.status != StatusItemChoices.RESERVADO:
        return None
    reserva = reserva_ativa_do_item(item)
    if reserva is None:
        return None
    if (
        tipo_movimentacao == "transferencia" and tipo_transferencia == "entrega"
        and usuario_id and reserva.colaborador_id == usuario_id
    ):
        return None
    return reserva


def tratar_movimentacao(*, item, mov, confirmou: bool, justificativa: str, user):
    """Gancho do `MovimentacaoEstoqueService`, chamado DENTRO da transação da
    movimentação, logo depois de ela ser gravada e ANTES de o status do item
    mudar. Devolve `ResultadoMovimentacao` quando o item estava reservado
    (ou `None`).

    • Entrega ao colaborador da vaga → a reserva vira ENTREGUE, guardando a
      movimentação como prova, e a vaga registra a entrega.
    • Qualquer outro uso → a reserva é CEDIDA: exige confirmação e
      justificativa (a regra é daqui, não só do formulário), registra o que
      aconteceu, procura substituto em Backup e, se não houver, dispara o
      alerta de vaga sem equipamento.
    """
    reserva = (
        Reserva.objects.select_for_update().select_related("contratacao", "item", "item__subtipo")
        .filter(item=item, estado=Estado.ATIVA).first()
    )
    if reserva is None:
        return None
    vaga = reserva.contratacao

    entrega_prevista = exige_confirmacao(
        item, tipo_movimentacao=mov.tipo_movimentacao,
        tipo_transferencia=mov.tipo_transferencia or "", usuario_id=mov.usuario_id,
    ) is None
    if entrega_prevista:
        _encerrar(reserva, estado=Estado.ENTREGUE, motivo_tipo=Motivo.ENTREGA,
                  motivo="Entrega por movimentação patrimonial.", user=user, movimentacao=mov)
        _evento(vaga, f"Equipamento entregue por movimentação: {reserva.item_rotulo}.", user)
        _registrar_entrega_na_vaga(vaga, reserva, mov, user)
        return ResultadoMovimentacao(tipo="entrega", reserva=reserva)

    justificativa = (justificativa or "").strip()
    if not confirmou or len(justificativa) < 5:
        raise ValidationError(
            f"“{reserva.item_rotulo}” está reservado para {reserva.vaga_rotulo}. Para usá-lo em "
            "outra necessidade, confirme o uso e informe a justificativa."
        )

    _encerrar(reserva, estado=Estado.CEDIDA, motivo_tipo=Motivo.REPOSICAO,
              motivo=justificativa, user=user, movimentacao=mov)
    _evento(
        vaga, f"Equipamento reservado usado em outra necessidade: {reserva.item_rotulo}. "
              f"Motivo: {justificativa}", user,
    )
    return _depois_da_cessao(reserva, user)


@transaction.atomic
def item_saiu_de_reservado(item, status_anterior: str, user=None):
    """Rede de segurança (signal): o status do equipamento deixou de ser
    Reservado por um caminho que não passou pelo fluxo de reserva (um reparo
    aberto direto, um ajuste de cadastro). A reserva não pode ficar "ativa"
    sobre um equipamento que já não está reservado — vira CEDIDA e a vaga fica
    pendente, com alerta se não houver substituto. Quando o fluxo normal já
    fechou a reserva (entrega, cessão, liberação) não há nada ativo aqui."""
    if status_anterior != StatusItemChoices.RESERVADO or item.status == StatusItemChoices.RESERVADO:
        return None
    reserva = (
        Reserva.objects.select_for_update().select_related("contratacao", "item", "item__subtipo")
        .filter(item=item, estado=Estado.ATIVA).first()
    )
    if reserva is None:
        return None
    _encerrar(
        reserva, estado=Estado.CEDIDA, motivo_tipo=Motivo.STATUS_ALTERADO, user=user,
        motivo=f"O status do equipamento foi alterado de Reservado para {item.get_status_display()} "
               "fora do fluxo de reserva.",
    )
    _evento(
        reserva.contratacao,
        f"Reserva desfeita: {reserva.item_rotulo} saiu de “Reservado” para “{item.get_status_display()}”.",
        user,
    )
    return _depois_da_cessao(reserva, user)


def _depois_da_cessao(reserva, user) -> ResultadoMovimentacao:
    """A vaga perdeu um equipamento reservado. Procura reposição em Backup; se
    NÃO há, o TI é avisado por e-mail. A vaga continua com o status que tinha."""
    candidatos = candidatos_para_substituir(reserva).count()
    resultado = ResultadoMovimentacao(tipo="cessao", reserva=reserva, candidatos=candidatos)
    vaga = reserva.contratacao
    if vaga is None or vaga.encerrada:
        return resultado
    if candidatos == 0:
        resultado.alerta_disparado = _agendar_alerta(reserva)
    return resultado


def _registrar_entrega_na_vaga(vaga, reserva, mov, user) -> None:
    """A vaga passa a saber que o equipamento FOI entregue — pela movimentação,
    não por declaração. Avança para "Equipamento entregue" quando não resta
    nada reservado por entregar; o computador principal (notebook/desktop) vira
    o `item_entregue`."""
    if vaga is None:
        return
    vaga = Contratacao.objects.select_for_update().get(pk=vaga.pk)
    agora = timezone.now()
    campos = ["equipamento_entregue_em", "atualizado_por", "updated_at"]
    vaga.equipamento_entregue_em = agora
    vaga.atualizado_por = user

    nome_tipo = (reserva.item.subtipo.nome if reserva.item.subtipo_id else "").lower()
    eh_computador = any(t in nome_tipo for t in ("notebook", "desktop"))
    if eh_computador or not vaga.item_entregue_id:
        vaga.item_entregue = reserva.item
        campos.append("item_entregue")

    restam = Reserva.objects.filter(contratacao=vaga, estado=Estado.ATIVA).exists()
    if not restam and vaga.status in (StatusContratacaoChoices.ABERTA, StatusContratacaoChoices.VAGA_PREENCHIDA):
        vaga.status = StatusContratacaoChoices.EQUIPAMENTO_ENTREGUE
        campos.append("status")
        if not vaga.vaga_preenchida_em:
            vaga.vaga_preenchida_em = agora
            campos.append("vaga_preenchida_em")
    vaga.save(update_fields=campos)


# ─────────────────────────────────────────────────────────────
# Alerta: vaga sem equipamento
# ─────────────────────────────────────────────────────────────

def _agendar_alerta(reserva) -> bool:
    """Marca a ocorrência como "alertando" e agenda o envio para DEPOIS do
    commit (SMTP lento não pode segurar lock do banco). O UPDATE condicional é o
    que garante UM e-mail por ocorrência: dois caminhos que chegam aqui ao mesmo
    tempo, ou a mesma tela aberta de novo, não mandam de novo."""
    pegou = Reserva.objects.filter(pk=reserva.pk, alerta_em__isnull=True).update(
        alerta_em=timezone.now(), alerta_resultado="Envio em andamento.",
    )
    if not pegou:
        return False
    pk = reserva.pk
    transaction.on_commit(lambda: enviar_alerta(pk))
    return True


def enviar_alerta(reserva_pk: int) -> bool:
    """Monta e envia o e-mail de uma ocorrência já "reservada" por
    `_agendar_alerta`. Nunca levanta: o resultado fica na reserva. Falha libera
    a ocorrência (`alerta_em=None`) para o TI tentar de novo pela tela."""
    from services import email_alertas

    reserva = (
        Reserva.objects.select_related("contratacao", "item", "item__subtipo", "movimentacao",
                                       "encerrada_por", "movimentacao__item")
        .filter(pk=reserva_pk).first()
    )
    if reserva is None:
        return False
    try:
        resultado = email_alertas.alerta_vaga_sem_equipamento(reserva)
    except Exception as exc:  # noqa: BLE001 — a regra é justamente não deixar escapar
        logger.exception("reserva %s: erro ao enviar o alerta de vaga sem equipamento", reserva_pk)
        resultado = email_alertas.ResultadoEnvio(False, f"Erro ao preparar o e-mail: {str(exc)[:200]}")

    if resultado.ok:
        Reserva.objects.filter(pk=reserva_pk).update(
            alerta_em=timezone.now(),
            alerta_resultado=f"Enviado para {len(resultado.destinatarios)} destinatário(s).",
        )
    else:
        Reserva.objects.filter(pk=reserva_pk).update(
            alerta_em=None, alerta_resultado=f"Não enviado: {resultado.motivo}"[:255],
        )
    return resultado.ok


def reenviar_alerta(*, reserva_pk: int, user) -> bool:
    """O TI manda de novo o alerta que NÃO saiu. Recusa o que já foi enviado
    (duplicaria) e a ocorrência que já foi resolvida."""
    _exigir_ti(user)
    with transaction.atomic():
        reserva = Reserva.objects.select_for_update().select_related("contratacao").filter(pk=reserva_pk).first()
        if reserva is None or reserva.estado != Estado.CEDIDA:
            raise ReservaRecusada("Esta ocorrência não existe.")
        if reserva.substituida_por_id:
            raise ReservaRecusada("A reserva desta vaga já foi reposta — não há o que avisar.")
        vaga = reserva.contratacao
        if vaga is None or vaga.encerrada:
            raise ReservaRecusada("A vaga já foi encerrada.")
        if reserva.alerta_em:
            raise ReservaRecusada("O alerta desta ocorrência já foi enviado.")
        if not _agendar_alerta(reserva):
            raise ReservaRecusada("O alerta desta ocorrência já está sendo enviado.")
    return True
