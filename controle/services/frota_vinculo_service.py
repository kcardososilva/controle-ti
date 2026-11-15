"""
Vínculo do chip com o seu DESTINO e as MOVIMENTAÇÕES do chip.

O destino é um veículo (`FrotaVeiculo`) OU um equipamento do estoque de TI (`Item`: tablet, celular —
os subtipos com `aceita_chip`). Cada operação recebe qualquer um dos dois e o service decide com
`_Destino`; quem chama não precisa saber a diferença. Cada tipo de item continua no módulo dele: o
tablet/celular é do TI, o veículo é do Agro, e o chip é o que liga os dois mundos.

Camadas: views -> este service -> `m2m_monitoramento_service` (desativar/reativar o
monitoramento, abrir ocorrência de evento) -> models. O `Item` é só REFERÊNCIA: nenhuma operação
daqui grava em `Item`, `MovimentacaoItem` nem `ItemStatusHistorico` (regra 62) — o status do
tablet continua sendo do TI, e a sessão do chip continua sendo da conectividade.

Decisões que moldam este módulo — e não são óbvias olhando o código:

  • A GARANTIA É O BANCO. "Um chip, um vínculo ativo" é a `UniqueConstraint` parcial de
    `FrotaVinculo` (chip, quando `fim_em` é nulo). O service confere antes para dar uma
    mensagem boa, mas quem impede dois vínculos com duas pessoas clicando ao mesmo tempo
    é a restrição: o INSERT que perde a corrida levanta `IntegrityError`, capturado
    dentro de um `atomic` interno (savepoint) e convertido em `ValidationError("Este chip
    acabou de ser vinculado por outra pessoa…")`. (`select_for_update` não faz nada no
    SQLite; ele está aí para o dia em que o banco mudar, mas NÃO é o que protege.)
  • TODA OPERAÇÃO É UMA TRANSAÇÃO. Dentro dela o service RELÊ o chip, o veículo e o
    vínculo — o que a tela mostrava pode já ter mudado. Grava `FrotaMovimentacao` (imutável)
    + `M2MEvento` e atualiza `M2MChip.situacao_uso`, que SÓ este módulo escreve.
  • AS OPERAÇÕES SOBRE VÍNCULO EXISTENTE RECEBEM O ID DO VÍNCULO ESPERADO, não o do chip:
    se a tela abriu com o vínculo X e ele já foi encerrado por outra pessoa, a operação
    é RECUSADA ("já desvinculado por fulano em 12/11 14:05") em vez de agir sobre o
    vínculo novo que a outra pessoa criou.
  • MOTIVO É OBRIGATÓRIO (exceto na entrada) e `usuario` também: sem os dois a operação
    não é registrável. `FrotaMovimentacao.usuario_nome` guarda o login para sobreviver à
    exclusão do usuário.
  • VEÍCULO `desativado` NÃO RECEBE CHIP; `manutencao`/`inativo` recebe, com aviso (a
    vinculação é do controle de chips, não do status operacional — que são coisas
    separadas). Com o EQUIPAMENTO DE TI é igual: `descarte`/`devolvido`/excluído não recebe;
    `manutencao`/`defeito`/`pausado` recebe, com aviso. E só recebe quem o subtipo marca com
    `aceita_chip` — um monitor não vira destino de chip por engano.
  • ALERTAS DE EVENTO (`chip_desvinculado`, `alteracao_vinculo`) são informativos e só
    existem se a regra correspondente (`FrotaRegraAlerta`) estiver ligada; uma falha ao
    abri-los nunca desfaz a movimentação.

Contratos de leitura para as telas (Fase E): `historico_chip`, `historico_movimentacoes`,
`agrupar_por_operacao`, `chips_disponiveis`, `formatar_duracao` e `exportar_movimentacoes_xlsx`.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, time as hora_do_dia, timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from ProjetoEstoque.models import (
    FrotaImportacaoChips,
    FrotaMovimentacao,
    FrotaVeiculo,
    FrotaVinculo,
    Item,
    M2MChip,
    M2MEvento,
    StatusItemChoices,
    rotulo_do_equipamento_ti,
)
from services import m2m_monitoramento_service as m2m

logger = logging.getLogger(__name__)

Tipo = FrotaMovimentacao.Tipo
Uso = M2MChip.SituacaoUso
# Situações em que o chip está LIVRE (sem vínculo, monitorado): é para onde ele vai ao sair do veículo/equipamento
# e entre elas que `mudar_situacao` o move. `inativo` fica de fora (desliga o monitoramento: `inativar`/`retornar_disponibilidade`).
SITUACOES_LIVRES = (Uso.DISPONIVEL.value, Uso.RESERVADO.value, Uso.PAUSADO.value, Uso.DEFEITO.value, Uso.MANUTENCAO.value)
DESTINOS_DO_CHIP = SITUACOES_LIVRES   # para onde o chip vai ao sair do veículo
# Situações em que o chip NÃO pode ser vinculado até voltar à disponibilidade (o reservado pode: é para isso que se reserva).
SITUACOES_QUE_NAO_VINCULAM = (Uso.MANUTENCAO.value, Uso.DEFEITO.value, Uso.PAUSADO.value, Uso.INATIVO.value)
MOTIVO_MAXIMO = 255
_MENSAGEM_DESTINO_DO_CHIP = "Escolha para onde o chip vai: disponível, reservado, pausado, com defeito ou em manutenção."


@dataclass
class ResultadoMovimentacao:
    """O que uma operação fez.

    `movimentacoes`: as `FrotaMovimentacao` gravadas, na ordem (a substituição grava duas,
    com o mesmo `operacao`). `movimentacao` é a primeira. `vinculo`: o vínculo ATIVO criado
    (vincular, transferir, substituir) ou `None`. `vinculo_encerrado`: o que foi encerrado
    (desvincular, transferir, substituir, inativar) ou `None`. `avisos`: textos para mostrar
    junto da confirmação (ex.: "o veículo está em manutenção"); o registro foi feito.
    `operacao`: o UUID que agrupa as linhas."""

    operacao: uuid.UUID
    movimentacoes: list = field(default_factory=list)
    vinculo: FrotaVinculo | None = None
    vinculo_encerrado: FrotaVinculo | None = None
    avisos: list = field(default_factory=list)

    @property
    def movimentacao(self) -> FrotaMovimentacao:
        return self.movimentacoes[0]


# ═════════════════════════════════════════════════════════════
# Apoio
# ═════════════════════════════════════════════════════════════

def _exigir_usuario(usuario):
    if usuario is None or not getattr(usuario, "pk", None):
        raise ValidationError("Informe quem está registrando a movimentação.")
    return usuario


def _motivo(texto, obrigatorio: bool = True) -> str:
    motivo = (texto or "").strip()
    if obrigatorio and not motivo:
        raise ValidationError({"motivo": "Informe o motivo da movimentação."})
    if len(motivo) > MOTIVO_MAXIMO:
        raise ValidationError({"motivo": f"O motivo aceita até {MOTIVO_MAXIMO} caracteres."})
    return motivo


def _nome_do_veiculo(veiculo: FrotaVeiculo | None) -> str:
    return f"{veiculo.codigo} · {veiculo.nome}" if veiculo is not None else ""


@dataclass(frozen=True, eq=False)
class _Destino:
    """Para onde o chip vai / de onde ele sai: UM veículo OU UM equipamento do estoque de TI.
    É o que deixa o resto do service igual para os dois — só aqui se sabe qual é qual."""

    veiculo: FrotaVeiculo | None = None
    item: Item | None = None

    @property
    def tipo(self) -> str:
        return "veiculo" if self.veiculo is not None else "item"

    @property
    def pk(self) -> int:
        return (self.veiculo if self.veiculo is not None else self.item).pk

    @property
    def rotulo(self) -> str:
        return _nome_do_veiculo(self.veiculo) if self.veiculo is not None else rotulo_do_equipamento_ti(self.item)

    @property
    def completo(self) -> str:
        """`veículo VEI-001 · Trator` ou `equipamento Galaxy Tab A7 · R9XR…`. Os dois são masculinos, então
        "ao / do / no / para o" servem na frente de qualquer um."""
        return f"{'veículo' if self.veiculo is not None else 'equipamento'} {self.rotulo}"


def _como_destino(valor) -> _Destino:
    """Aceita o que a view tem na mão: um `FrotaVeiculo`, um `Item` (ou um `_Destino` já pronto)."""
    if isinstance(valor, _Destino):
        return valor
    if isinstance(valor, FrotaVeiculo):
        return _Destino(veiculo=valor)
    if isinstance(valor, Item):
        return _Destino(item=valor)
    raise ValidationError("Escolha o veículo ou o equipamento que recebe o chip.")


def _destino_do_vinculo(vinculo: FrotaVinculo) -> _Destino:
    return _Destino(
        veiculo=vinculo.veiculo if vinculo.veiculo_id else None,
        item=vinculo.item if vinculo.item_id else None,
    )


def descrever_destino(vinculo: FrotaVinculo) -> str:
    """`veículo VEI-001 · Trator` ou `equipamento Galaxy Tab A7 · R9XR…` — o texto pronto para mensagens e
    rótulos das telas (o vínculo deve vir com `veiculo`/`item` carregados)."""
    return _destino_do_vinculo(vinculo).completo


def equipamento_aceita_chip(item: Item) -> bool:
    """O equipamento de TI pode receber chip? Só o que o subtipo marca (`aceita_chip`), vivo e que não seja
    de consumo. É a pergunta da FICHA ("mostro o cartão de chip?"); a recusa com motivo é do `vincular`."""
    return bool(
        item.subtipo_id and item.subtipo.aceita_chip and not item.excluido and item.item_consumo != "sim"
    )


def _quando(momento) -> str:
    return timezone.localtime(momento).strftime("%d/%m %H:%M")


def _quem(usuario) -> str:
    if usuario is None:
        return "outro usuário"
    return (usuario.get_full_name() or usuario.get_username()).strip()


def _reler_chip(chip: M2MChip) -> M2MChip:
    """O chip como está AGORA no banco (a tela pode estar velha)."""
    try:
        return M2MChip.objects.select_for_update().select_related("operadora").get(pk=chip.pk)
    except M2MChip.DoesNotExist:
        raise ValidationError("Chip não encontrado.") from None


def _reler_veiculo(veiculo: FrotaVeiculo) -> FrotaVeiculo:
    try:
        return FrotaVeiculo.objects.select_for_update().get(pk=veiculo.pk)
    except FrotaVeiculo.DoesNotExist:
        raise ValidationError({"veiculo": "Veículo não encontrado."}) from None


def _reler_destino(destino: _Destino) -> _Destino:
    """O destino como está AGORA no banco (a tela pode estar velha). O equipamento é relido com o
    subtipo, que decide se ele aceita chip; o `Item` excluído (soft delete) também volta, para a
    recusa dizer o motivo certo em vez de "não encontrado"."""
    if destino.veiculo is not None:
        return _Destino(veiculo=_reler_veiculo(destino.veiculo))
    try:
        item = Item.all_objects.select_for_update().select_related("subtipo").get(pk=destino.item.pk)
    except Item.DoesNotExist:
        raise ValidationError({"item": "Equipamento não encontrado."}) from None
    return _Destino(item=item)


def _vinculo_ativo_do_chip(chip: M2MChip) -> FrotaVinculo | None:
    return FrotaVinculo.objects.filter(chip=chip, fim_em__isnull=True).select_related("veiculo", "item").first()


def _mensagem_ja_encerrado(vinculo: FrotaVinculo) -> str:
    """"Este chip já foi desvinculado por fulano em 12/11 14:05" — com o verbo certo para
    como o vínculo acabou (desvinculação, transferência, substituição ou inativação)."""
    mov = vinculo.mov_fim if vinculo.mov_fim_id else None
    transferido = "transferido para outro equipamento" if mov is not None and mov.item_destino_id else "transferido para outro veículo"
    verbos = {
        Tipo.TRANSFERENCIA: transferido,
        Tipo.SUBSTITUICAO: "substituído por outro chip",
        Tipo.INATIVACAO: "inativado",
    }
    tipo = mov.tipo if mov is not None else Tipo.DESVINCULACAO
    verbo = verbos.get(tipo, "desvinculado")
    quando = f" em {_quando(vinculo.fim_em)}" if vinculo.fim_em else ""
    onde = f"veículo {vinculo.veiculo.codigo}" if vinculo.veiculo_id else _destino_do_vinculo(vinculo).completo
    return (
        f"Este chip já foi {verbo} por {_quem(vinculo.fim_por)}{quando} ({onde}). "
        "Atualize a página para ver a situação atual."
    )


def _vinculo_esperado(vinculo_id) -> FrotaVinculo:
    """O vínculo que a tela mostrava, se ele AINDA está ativo; senão `ValidationError`
    dizendo quem o encerrou e quando."""
    try:
        pk = int(vinculo_id)
    except (TypeError, ValueError):
        raise ValidationError("Vínculo não encontrado.") from None
    vinculo = (
        FrotaVinculo.objects.select_related("chip", "veiculo", "item", "fim_por", "mov_fim").filter(pk=pk).first()
    )
    if vinculo is None:
        raise ValidationError("Vínculo não encontrado.")
    if vinculo.fim_em is not None:
        raise ValidationError(_mensagem_ja_encerrado(vinculo))
    return vinculo


def _gravar_movimentacao(
    tipo, chip, usuario, motivo, *, operacao, origem: _Destino | None = None, destino: _Destino | None = None,
    chip_relacionado=None, observacoes="", importacao=None, agora=None,
) -> FrotaMovimentacao:
    """Grava a linha imutável. `origem`/`destino` (um `_Destino` ou `None`) viram as FKs certas: o
    veículo vai para `veiculo_*` e o equipamento de TI para `item_*` — nunca os dois do mesmo lado."""
    return FrotaMovimentacao.objects.create(
        tipo=tipo, chip=chip, chip_relacionado=chip_relacionado, operacao=operacao,
        veiculo_origem=origem.veiculo if origem else None, item_origem=origem.item if origem else None,
        veiculo_destino=destino.veiculo if destino else None, item_destino=destino.item if destino else None,
        motivo=motivo, observacoes=(observacoes or "").strip(), usuario=usuario,
        usuario_nome=usuario.get_username(), em=agora or timezone.now(), importacao=importacao,
    )


_EVENTO_DO_TIPO = {
    Tipo.ENTRADA: M2MEvento.Tipo.ENTRADA,
    Tipo.VINCULACAO: M2MEvento.Tipo.VINCULADO,
    Tipo.DESVINCULACAO: M2MEvento.Tipo.DESVINCULADO,
    Tipo.TRANSFERENCIA: M2MEvento.Tipo.TRANSFERIDO,
    Tipo.SUBSTITUICAO: M2MEvento.Tipo.SUBSTITUIDO,
    Tipo.RETORNO: M2MEvento.Tipo.RETORNO_DISPONIBILIDADE,
    Tipo.INATIVACAO: M2MEvento.Tipo.INATIVADO,
    Tipo.SITUACAO: M2MEvento.Tipo.SITUACAO_MUDOU,
}


def _rotulo_da_situacao(valor: str) -> str:
    return dict(Uso.choices).get(valor, valor)


def _gravar_evento(mov: FrotaMovimentacao, chip: M2MChip, usuario) -> None:
    """Espelha a movimentação em `M2MEvento` (a trilha do chip) — o id dela vai em `dados`.
    `veiculo_*` continua como sempre foi (eventos antigos e leituras que dependem dele); `origem_texto`
    e `destino_texto` dizem o lugar com o tipo ("veículo X" / "equipamento Y") para a frase da trilha.
    `situacao_anterior` é a situação de ANTES (a que `_gravar_situacao` deixou no chip; sem ela, a operação não
    mudou a situação) — o evento antigo não a tem, e a trilha continua legível sem."""
    origem = _Destino(veiculo=mov.veiculo_origem, item=mov.item_origem) if (mov.veiculo_origem_id or mov.item_origem_id) else None
    destino = _Destino(veiculo=mov.veiculo_destino, item=mov.item_destino) if (mov.veiculo_destino_id or mov.item_destino_id) else None
    anterior = getattr(chip, "_situacao_anterior", chip.situacao_uso)
    M2MEvento.objects.create(
        chip=chip, tipo=_EVENTO_DO_TIPO[mov.tipo], usuario=usuario, em=mov.em,
        dados={
            "movimentacao_id": mov.pk, "operacao": str(mov.operacao), "motivo": mov.motivo,
            "veiculo_origem": _nome_do_veiculo(mov.veiculo_origem), "veiculo_destino": _nome_do_veiculo(mov.veiculo_destino),
            "origem_texto": origem.completo if origem else "", "destino_texto": destino.completo if destino else "",
            "chip_relacionado": mov.chip_relacionado.nome if mov.chip_relacionado_id else "",
            "situacao_uso": chip.situacao_uso, "situacao_uso_rotulo": chip.get_situacao_uso_display(),
            "situacao_anterior": anterior, "situacao_anterior_rotulo": _rotulo_da_situacao(anterior),
        },
    )


def _gravar_situacao(chip: M2MChip, nova: str, usuario, agora) -> None:
    """ÚNICO ponto do sistema que escreve `situacao_uso`. `situacao_uso_em` (desde quando) só anda quando a situação
    MUDA de fato — transferir um chip vinculado não zera o "há N dias". A situação de antes fica em
    `chip._situacao_anterior` para `_gravar_evento`, que roda logo depois com o mesmo objeto."""
    anterior = chip.situacao_uso
    campos = {"situacao_uso": nova, "atualizado_por": usuario, "updated_at": agora}
    if nova != anterior or chip.situacao_uso_em is None:
        campos["situacao_uso_em"] = agora
        chip.situacao_uso_em = agora
    chip._situacao_anterior = anterior
    chip.situacao_uso = nova
    M2MChip.objects.filter(pk=chip.pk).update(**campos)


def _abrir_vinculo(chip, destino: _Destino, usuario, mov, agora) -> FrotaVinculo:
    """Cria o vínculo ATIVO. Quem perde a corrida contra outra pessoa leva o
    `IntegrityError` da restrição parcial — convertido aqui."""
    try:
        with transaction.atomic():
            return FrotaVinculo.objects.create(
                chip=chip, veiculo=destino.veiculo, item=destino.item, inicio_em=agora, inicio_por=usuario,
                mov_inicio=mov,
            )
    except IntegrityError:
        raise ValidationError(
            "Este chip acabou de ser vinculado por outra pessoa. Atualize a página para ver o vínculo atual."
        ) from None


def _encerrar_vinculo(vinculo: FrotaVinculo, usuario, mov, motivo, agora) -> FrotaVinculo:
    """Encerra o período de forma ATÔMICA e condicional (`WHERE fim_em IS NULL`): se outra
    pessoa o encerrou entre a leitura e aqui, nada é gravado e a operação é recusada."""
    mudou = FrotaVinculo.objects.filter(pk=vinculo.pk, fim_em__isnull=True).update(
        fim_em=agora, fim_por=usuario, motivo_fim=motivo, mov_fim=mov,
    )
    if not mudou:
        atual = FrotaVinculo.objects.select_related("veiculo", "item", "fim_por", "mov_fim").get(pk=vinculo.pk)
        raise ValidationError(_mensagem_ja_encerrado(atual))
    vinculo.fim_em, vinculo.fim_por, vinculo.motivo_fim, vinculo.mov_fim = agora, usuario, motivo, mov
    return vinculo


def _conferir_veiculo_recebe(veiculo: FrotaVeiculo) -> list[str]:
    """Recusa veículo desativado; devolve o aviso para manutenção/inativo."""
    if veiculo.status == FrotaVeiculo.Status.DESATIVADO:
        raise ValidationError({"veiculo": f"O veículo {veiculo.codigo} está desativado e não recebe chips."})
    if veiculo.status in (FrotaVeiculo.Status.MANUTENCAO, FrotaVeiculo.Status.INATIVO):
        return [
            f"O veículo {veiculo.codigo} está {veiculo.get_status_display().lower()}; "
            "o chip foi vinculado mesmo assim."
        ]
    return []


def _conferir_equipamento_recebe(item: Item) -> list[str]:
    """Recusa o equipamento de TI que não pode receber chip; devolve o aviso para o que está parado.

    Quem decide se o TIPO aceita chip é o subtipo (`aceita_chip`, marcado pelo TI) — assim o monitor
    ou o switch não viram destino por engano, e um tipo novo (roteador 4G) é só marcar. O `status`
    é do TI e daqui só se LÊ: descartado/devolvido não recebe; manutenção/defeito/pausado recebe,
    com aviso (mesma régua do veículo)."""
    nome = rotulo_do_equipamento_ti(item)
    if item.excluido:
        raise ValidationError({"item": f"O equipamento {nome} foi excluído do estoque e não recebe chips."})
    if item.item_consumo == "sim":
        raise ValidationError({"item": f"{nome} é item de consumo e não recebe chip."})
    if item.subtipo_id is None:
        raise ValidationError({
            "item": f"O equipamento {nome} não tem tipo (subtipo) definido, então não dá para saber se ele usa chip. "
                    "Defina o tipo no cadastro do equipamento."
        })
    if not item.subtipo.aceita_chip:
        raise ValidationError({
            "item": f"O tipo \"{item.subtipo.nome}\" não está marcado para receber chip. "
                    "Marque \"Aceita chip\" no cadastro do subtipo, se ele usa SIM."
        })
    if item.status in (StatusItemChoices.DESCARTE, StatusItemChoices.DEVOLVIDO):
        raise ValidationError({"item": f"O equipamento {nome} está em \"{item.get_status_display()}\" e não recebe chips."})
    if item.status in (StatusItemChoices.MANUTENCAO, StatusItemChoices.DEFEITO, StatusItemChoices.PAUSADO):
        return [f"O equipamento {nome} está em {item.get_status_display().lower()}; o chip foi vinculado mesmo assim."]
    return []


def _conferir_destino_recebe(destino: _Destino) -> list[str]:
    if destino.veiculo is not None:
        return _conferir_veiculo_recebe(destino.veiculo)
    return _conferir_equipamento_recebe(destino.item)


def _conferir_chip_para_vincular(chip: M2MChip) -> None:
    """O chip precisa estar livre, sem vínculo ativo e em situação que aceite vínculo: disponível ou reservado
    (reservar é separar o chip para alguém, então o vínculo é o fim esperado da reserva). Em manutenção, com defeito,
    pausado ou inativo ele volta antes à disponibilidade."""
    ativo = _vinculo_ativo_do_chip(chip)
    if ativo is not None or chip.situacao_uso == Uso.VINCULADO:
        onde = f" ao {_destino_do_vinculo(ativo).completo} desde {_quando(ativo.inicio_em)}" if ativo else ""
        raise ValidationError(
            f"Este chip já está vinculado{onde}. Para mudá-lo de lugar, use transferir ou substituir."
        )
    if chip.situacao_uso in SITUACOES_QUE_NAO_VINCULAM:
        raise ValidationError(
            f"Este chip está {chip.get_situacao_uso_display().lower()}. Devolva-o à disponibilidade antes de vinculá-lo."
        )


def _avisar_evento(tipo: str, chip: M2MChip, destino: _Destino | None, descricao: str, usuario, agora) -> None:
    """Abre a ocorrência informativa (se a regra está ligada). Falhar aqui NUNCA desfaz a
    movimentação: savepoint próprio e o erro só vai para o log. A ocorrência guarda o VEÍCULO (a
    coluna que ela tem); com equipamento de TI como destino fica sem veículo e a descrição diz onde."""
    try:
        with transaction.atomic():
            m2m.abrir_ocorrencia_de_evento(
                tipo, chip, destino.veiculo if destino else None, descricao, usuario=usuario, agora=agora,
            )
    except Exception:  # noqa: BLE001 — alerta é acessório da movimentação
        logger.exception("Frotas: a ocorrência de evento (%s) não foi aberta.", tipo)


# ═════════════════════════════════════════════════════════════
# Operações
# ═════════════════════════════════════════════════════════════

def registrar_entrada(
    chip: M2MChip, usuario, importacao: FrotaImportacaoChips | None = None, observacoes: str = "",
) -> ResultadoMovimentacao:
    """Registra a ENTRADA do chip no controle (movimentação `entrada`, sem motivo, mais
    `M2MEvento`). Chamada por `m2m_monitoramento_service.criar_chip` — a cada chip
    cadastrado, individualmente ou pela planilha (`importacao`). `usuario` é obrigatório.
    Não mexe em `situacao_uso` (o chip nasce `disponivel`). Devolve `ResultadoMovimentacao`."""
    _exigir_usuario(usuario)
    agora = timezone.now()
    with transaction.atomic():
        operacao = uuid.uuid4()
        mov = _gravar_movimentacao(
            Tipo.ENTRADA, chip, usuario, "", operacao=operacao, observacoes=observacoes, importacao=importacao, agora=agora,
        )
        _gravar_evento(mov, chip, usuario)
    return ResultadoMovimentacao(operacao=operacao, movimentacoes=[mov])


def vincular(chip: M2MChip, destino, usuario, motivo: str, *, observacoes: str = "") -> ResultadoMovimentacao:
    """Vincula um chip DISPONÍVEL (ou RESERVADO) a um destino: um `FrotaVeiculo` OU um `Item` do estoque de TI
    (tablet, celular). O destino pode ter vários chips.

    Recusa (`ValidationError`): sem `usuario` ou `motivo`; chip já vinculado (a mensagem diz a que
    destino e desde quando), em manutenção, com defeito, pausado ou inativo; veículo `desativado`; equipamento de TI de um
    subtipo que não aceita chip, excluído, em descarte ou devolvido. Destino parado (veículo em
    manutenção/inativo; equipamento em manutenção/defeito/pausado) recebe o chip, com um texto em
    `avisos`. Se outra pessoa vincular o mesmo chip ao mesmo tempo, quem perde recebe "Este chip
    acabou de ser vinculado por outra pessoa". Grava a movimentação `vinculacao` e o `M2MEvento`,
    põe o chip em `vinculado` e, se a regra estiver ligada, abre `alteracao_vinculo`.
    NUNCA grava no `Item`. Devolve `ResultadoMovimentacao` (`vinculo` = o novo)."""
    _exigir_usuario(usuario)
    motivo = _motivo(motivo)
    agora = timezone.now()
    with transaction.atomic():
        chip = _reler_chip(chip)
        destino = _reler_destino(_como_destino(destino))
        _conferir_chip_para_vincular(chip)
        avisos = _conferir_destino_recebe(destino)
        operacao = uuid.uuid4()
        mov = _gravar_movimentacao(
            Tipo.VINCULACAO, chip, usuario, motivo, operacao=operacao, destino=destino,
            observacoes=observacoes, agora=agora,
        )
        vinculo = _abrir_vinculo(chip, destino, usuario, mov, agora)
        _gravar_situacao(chip, Uso.VINCULADO, usuario, agora)
        _gravar_evento(mov, chip, usuario)
        _avisar_evento(
            "alteracao_vinculo", chip, destino,
            f"O chip {chip.nome} foi vinculado ao {destino.completo} por {_quem(usuario)} "
            f"em {_quando(agora)}. Motivo: {motivo}",
            usuario, agora,
        )
    return ResultadoMovimentacao(operacao=operacao, movimentacoes=[mov], vinculo=vinculo, avisos=avisos)


def desvincular(
    vinculo_id_esperado, usuario, motivo: str, destino: str = "disponivel", *, observacoes: str = "",
) -> ResultadoMovimentacao:
    """Encerra o vínculo `vinculo_id_esperado`; o chip continua cadastrado e vai para
    `destino` (uma das `SITUACOES_LIVRES`: disponível, reservado, pausado, com defeito ou em manutenção).

    Se o vínculo JÁ foi encerrado (por outra pessoa, ou por transferência/substituição/
    inativação), recusa com "Este chip já foi desvinculado por fulano em 12/11 14:05…" —
    nunca age sobre o vínculo novo que outra pessoa possa ter criado. Grava `desvinculacao`
    e o `M2MEvento` e, se a regra estiver ligada, abre `chip_desvinculado`.
    Devolve `ResultadoMovimentacao` (`vinculo_encerrado`)."""
    _exigir_usuario(usuario)
    motivo = _motivo(motivo)
    if destino not in DESTINOS_DO_CHIP:
        raise ValidationError({"destino": _MENSAGEM_DESTINO_DO_CHIP})
    agora = timezone.now()
    with transaction.atomic():
        vinculo = _vinculo_esperado(vinculo_id_esperado)
        chip = _reler_chip(vinculo.chip)
        origem = _destino_do_vinculo(vinculo)
        operacao = uuid.uuid4()
        mov = _gravar_movimentacao(
            Tipo.DESVINCULACAO, chip, usuario, motivo, operacao=operacao, origem=origem,
            observacoes=observacoes, agora=agora,
        )
        _encerrar_vinculo(vinculo, usuario, mov, motivo, agora)
        _gravar_situacao(chip, destino, usuario, agora)
        _gravar_evento(mov, chip, usuario)
        _avisar_evento(
            "chip_desvinculado", chip, origem,
            f"O chip {chip.nome} foi desvinculado do {origem.completo} por {_quem(usuario)} "
            f"em {_quando(agora)} e ficou {chip.get_situacao_uso_display().lower()}. Motivo: {motivo}",
            usuario, agora,
        )
    return ResultadoMovimentacao(operacao=operacao, movimentacoes=[mov], vinculo_encerrado=vinculo)


def transferir(
    vinculo_id_esperado, destino, usuario, motivo: str, *, observacoes: str = "",
) -> ResultadoMovimentacao:
    """Passa o chip do vínculo `vinculo_id_esperado` para outro destino (um `FrotaVeiculo` OU um
    `Item` do TI — de qualquer um para qualquer um), numa transação: encerra o vínculo anterior e
    cria o novo, gerando UMA movimentação `transferencia` com origem e destino. Os dois períodos
    ficam no histórico.

    Recusa: vínculo já encerrado (ver `desvincular`); destino igual à origem; destino que não recebe
    chip (as mesmas regras de `vincular`). Destino parado recebe com aviso. O chip segue
    `vinculado`. Se a regra estiver ligada, abre `alteracao_vinculo`.
    Devolve `ResultadoMovimentacao` (`vinculo` = o novo, `vinculo_encerrado` = o antigo)."""
    _exigir_usuario(usuario)
    motivo = _motivo(motivo)
    agora = timezone.now()
    with transaction.atomic():
        antigo = _vinculo_esperado(vinculo_id_esperado)
        chip = _reler_chip(antigo.chip)
        origem = _destino_do_vinculo(antigo)
        destino = _reler_destino(_como_destino(destino))
        if destino.tipo == origem.tipo and destino.pk == origem.pk:
            raise ValidationError({
                "veiculo_destino": f"O {'veículo' if destino.veiculo is not None else 'equipamento'} de destino é o mesmo de origem."
            })
        avisos = _conferir_destino_recebe(destino)
        operacao = uuid.uuid4()
        mov = _gravar_movimentacao(
            Tipo.TRANSFERENCIA, chip, usuario, motivo, operacao=operacao, origem=origem,
            destino=destino, observacoes=observacoes, agora=agora,
        )
        _encerrar_vinculo(antigo, usuario, mov, motivo, agora)       # antes de criar o novo: a restrição é por chip
        novo = _abrir_vinculo(chip, destino, usuario, mov, agora)
        if chip.situacao_uso != Uso.VINCULADO:
            _gravar_situacao(chip, Uso.VINCULADO, usuario, agora)
        _gravar_evento(mov, chip, usuario)
        _avisar_evento(
            "alteracao_vinculo", chip, destino,
            f"O chip {chip.nome} foi transferido do {origem.completo} para o {destino.completo} "
            f"por {_quem(usuario)} em {_quando(agora)}. Motivo: {motivo}",
            usuario, agora,
        )
    return ResultadoMovimentacao(
        operacao=operacao, movimentacoes=[mov], vinculo=novo, vinculo_encerrado=antigo, avisos=avisos,
    )


def substituir(
    vinculo_id_esperado, chip_novo: M2MChip, usuario, motivo: str, destino_antigo: str = "disponivel",
    *, observacoes: str = "",
) -> ResultadoMovimentacao:
    """Troca o chip do vínculo `vinculo_id_esperado` por `chip_novo` NO MESMO destino (veículo ou
    equipamento de TI): o chip antigo sai (vai para `destino_antigo`, uma das `SITUACOES_LIVRES`: o caso comum da
    troca é o chip que parou de funcionar, que sai `defeito`) e o novo entra. Grava DUAS movimentações `substituicao` com o mesmo `operacao` (UUID) — uma por
    chip, cada uma com o outro em `chip_relacionado`.

    Recusa: vínculo já encerrado; `chip_novo` igual ao atual, já vinculado, em manutenção ou
    inativo (mesmas regras de `vincular`). Se a regra estiver ligada, abre
    `alteracao_vinculo` no chip novo. Devolve `ResultadoMovimentacao`
    (`movimentacoes` = [antigo, novo]; `vinculo` = o do chip novo)."""
    _exigir_usuario(usuario)
    motivo = _motivo(motivo)
    if destino_antigo not in DESTINOS_DO_CHIP:
        raise ValidationError({"destino_antigo": _MENSAGEM_DESTINO_DO_CHIP})
    agora = timezone.now()
    with transaction.atomic():
        antigo = _vinculo_esperado(vinculo_id_esperado)
        chip_saindo = _reler_chip(antigo.chip)
        local = "veículo" if antigo.veiculo_id else "equipamento"
        if chip_novo.pk == chip_saindo.pk:
            raise ValidationError({"chip_novo": f"O chip novo é o mesmo que já está no {local}."})
        chip_entrando = _reler_chip(chip_novo)
        try:
            _conferir_chip_para_vincular(chip_entrando)
        except ValidationError as exc:
            raise ValidationError({"chip_novo": " ".join(exc.messages)}) from None
        lugar = _destino_do_vinculo(antigo)
        operacao = uuid.uuid4()
        mov_saida = _gravar_movimentacao(
            Tipo.SUBSTITUICAO, chip_saindo, usuario, motivo, operacao=operacao, origem=lugar,
            chip_relacionado=chip_entrando, observacoes=observacoes, agora=agora,
        )
        mov_entrada = _gravar_movimentacao(
            Tipo.SUBSTITUICAO, chip_entrando, usuario, motivo, operacao=operacao, destino=lugar,
            chip_relacionado=chip_saindo, observacoes=observacoes, agora=agora,
        )
        _encerrar_vinculo(antigo, usuario, mov_saida, motivo, agora)
        novo = _abrir_vinculo(chip_entrando, lugar, usuario, mov_entrada, agora)
        _gravar_situacao(chip_saindo, destino_antigo, usuario, agora)
        _gravar_situacao(chip_entrando, Uso.VINCULADO, usuario, agora)
        _gravar_evento(mov_saida, chip_saindo, usuario)
        _gravar_evento(mov_entrada, chip_entrando, usuario)
        _avisar_evento(
            "alteracao_vinculo", chip_entrando, lugar,
            f"O chip {chip_saindo.nome} foi substituído pelo chip {chip_entrando.nome} no {lugar.completo} "
            f"por {_quem(usuario)} em {_quando(agora)}. Motivo: {motivo}",
            usuario, agora,
        )
    return ResultadoMovimentacao(
        operacao=operacao, movimentacoes=[mov_saida, mov_entrada], vinculo=novo, vinculo_encerrado=antigo,
    )


def retornar_disponibilidade(
    chip: M2MChip, usuario, motivo: str, retomar_monitoramento: bool = True, *, observacoes: str = "",
) -> ResultadoMovimentacao:
    """Devolve à disponibilidade um chip em `manutencao`, `defeito`, `pausado`, `reservado` ou `inativo`
    (movimentação `retorno`). Com `retomar_monitoramento=True` (padrão) e o monitoramento desligado
    (o que `inativar` faz), reativa-o (`m2m.reativar`: zera as falhas antigas e marca o
    chip para a próxima rodada). Recusa chip que já está disponível ou que está vinculado (esse
    sai pelo `desvincular`/`substituir`). Devolve `ResultadoMovimentacao`."""
    _exigir_usuario(usuario)
    motivo = _motivo(motivo)
    agora = timezone.now()
    with transaction.atomic():
        chip = _reler_chip(chip)
        if chip.situacao_uso in (Uso.DISPONIVEL, Uso.VINCULADO):
            raise ValidationError(
                f"Só chip parado, reservado ou inativo volta à disponibilidade (este está {chip.get_situacao_uso_display().lower()})."
            )
        operacao = uuid.uuid4()
        mov = _gravar_movimentacao(Tipo.RETORNO, chip, usuario, motivo, operacao=operacao, observacoes=observacoes, agora=agora)
        _gravar_situacao(chip, Uso.DISPONIVEL, usuario, agora)
        _gravar_evento(mov, chip, usuario)
        if retomar_monitoramento and not chip.monitorado:
            m2m.reativar(chip, usuario, f"Chip devolvido à disponibilidade: {motivo}"[:MOTIVO_MAXIMO])
    return ResultadoMovimentacao(operacao=operacao, movimentacoes=[mov])


def inativar(chip: M2MChip, usuario, motivo: str, *, observacoes: str = "") -> ResultadoMovimentacao:
    """Inativa o chip (movimentação `inativacao`): encerra o vínculo, se houver, marca
    `situacao_uso=inativo` e desliga o monitoramento (`m2m.desativar`: as ocorrências em
    aberto são encerradas e o histórico fica). O chip nunca é apagado; `retornar_disponibilidade`
    o traz de volta. Recusa chip que já está inativo. Se havia vínculo, abre
    `chip_desvinculado` (regra permitindo). Devolve `ResultadoMovimentacao`
    (`vinculo_encerrado` se havia)."""
    _exigir_usuario(usuario)
    motivo = _motivo(motivo)
    agora = timezone.now()
    with transaction.atomic():
        chip = _reler_chip(chip)
        if chip.situacao_uso == Uso.INATIVO:
            raise ValidationError("Este chip já está inativo.")
        vinculo = _vinculo_ativo_do_chip(chip)
        origem = _destino_do_vinculo(vinculo) if vinculo else None
        operacao = uuid.uuid4()
        mov = _gravar_movimentacao(
            Tipo.INATIVACAO, chip, usuario, motivo, operacao=operacao,
            origem=origem, observacoes=observacoes, agora=agora,
        )
        if vinculo is not None:
            _encerrar_vinculo(vinculo, usuario, mov, motivo, agora)
        _gravar_situacao(chip, Uso.INATIVO, usuario, agora)
        if chip.monitorado:
            m2m.desativar(chip, usuario, f"Chip inativado: {motivo}"[:MOTIVO_MAXIMO])
        _gravar_evento(mov, chip, usuario)
        if vinculo is not None:
            _avisar_evento(
                "chip_desvinculado", chip, origem,
                f"O chip {chip.nome} foi inativado e desvinculado do {origem.completo} "
                f"por {_quem(usuario)} em {_quando(agora)}. Motivo: {motivo}",
                usuario, agora,
            )
    return ResultadoMovimentacao(operacao=operacao, movimentacoes=[mov], vinculo_encerrado=vinculo)


def mudar_situacao(chip: M2MChip, nova: str, usuario, motivo: str, *, observacoes: str = "") -> ResultadoMovimentacao:
    """A porta ÚNICA para mudar a situação de um chip que NÃO está vinculado — a que a tela chama.

    Entre as `SITUACOES_LIVRES` (disponível, reservado, pausado, com defeito, em manutenção) grava a movimentação
    `situacao` ("De X para Y"), o `M2MEvento` e `situacao_uso_em`. Os dois destinos que já tinham operação própria
    ficam com ela, para a regra morar num lugar só: `disponivel` → `retornar_disponibilidade` (e retoma o
    monitoramento, se estava desligado) e `inativo` → `inativar` (que também desliga o monitoramento e, se houver
    vínculo, o encerra). Mudar entre as situações livres NÃO liga nem desliga o monitoramento: chip com defeito ou
    pausado continua sendo consultado, porque o que a API diz dele é justamente o que ajuda a decidir.

    Recusa: `vinculado` como destino (vincular pede veículo ou equipamento — `vincular`); chip vinculado (sai por
    `desvincular` ou `substituir`, que já pedem a nova situação), chip inativo (volta antes à disponibilidade),
    situação igual à atual e motivo vazio. Devolve `ResultadoMovimentacao`."""
    nova = (nova or "").strip()
    if nova not in {valor for valor, _ in Uso.choices}:
        raise ValidationError({"situacao": "Escolha uma situação válida para o chip."})
    if nova == Uso.VINCULADO:
        raise ValidationError({"situacao": "Para vincular o chip, use Vincular: é preciso dizer a qual veículo ou equipamento."})
    if nova == Uso.DISPONIVEL:
        return retornar_disponibilidade(chip, usuario, motivo, observacoes=observacoes)
    if nova == Uso.INATIVO:
        return inativar(chip, usuario, motivo, observacoes=observacoes)
    _exigir_usuario(usuario)
    motivo = _motivo(motivo)
    agora = timezone.now()
    with transaction.atomic():
        chip = _reler_chip(chip)
        atual = chip.situacao_uso
        if atual == nova:
            raise ValidationError(f"Este chip já está {chip.get_situacao_uso_display().lower()}.")
        if atual == Uso.VINCULADO or _vinculo_ativo_do_chip(chip) is not None:
            raise ValidationError(
                "Este chip está vinculado. Desvincule-o escolhendo a nova situação, ou substitua-o, antes de mudar a situação."
            )
        if atual not in SITUACOES_LIVRES:
            raise ValidationError("Este chip está inativo. Devolva-o à disponibilidade antes de mudar a situação.")
        operacao = uuid.uuid4()
        texto = f"De {_rotulo_da_situacao(atual).lower()} para {_rotulo_da_situacao(nova).lower()}."
        mov = _gravar_movimentacao(
            Tipo.SITUACAO, chip, usuario, motivo, operacao=operacao,
            observacoes=f"{texto} {observacoes}".strip(), agora=agora,
        )
        _gravar_situacao(chip, nova, usuario, agora)
        _gravar_evento(mov, chip, usuario)
    return ResultadoMovimentacao(operacao=operacao, movimentacoes=[mov])


# ═════════════════════════════════════════════════════════════
# Leituras para as telas
# ═════════════════════════════════════════════════════════════

def formatar_duracao(duracao: timedelta | None) -> str:
    """"menos de 1 min" · "12 min" · "3 h 20 min" · "2 dias 4 h" (`—` sem valor)."""
    if duracao is None:
        return "—"
    minutos = max(0, int(duracao.total_seconds() // 60))
    if minutos < 1:
        return "menos de 1 min"
    if minutos < 60:
        return f"{minutos} min"
    horas, resto = divmod(minutos, 60)
    if horas < 24:
        return f"{horas} h {resto} min" if resto else f"{horas} h"
    dias, horas = divmod(horas, 24)
    texto = f"{dias} dia{'s' if dias != 1 else ''}"
    return f"{texto} {horas} h" if horas else texto


def historico_chip(chip: M2MChip) -> list[dict]:
    """Os PERÍODOS do chip por destino, do mais novo para o mais antigo (o ativo, se
    houver, é o primeiro). Cada item:

        {"vinculo_id", "veiculo": FrotaVeiculo ou None, "item": Item ou None,
         "destino_tipo": "veiculo"|"item", "destino_rotulo": "COD · nome" / "nome · série",
         "inicio_em", "fim_em" (None se ativo),
         "ativo": bool, "duracao": timedelta, "duracao_txt": "3 dias 4 h",
         "inicio_por": login, "fim_por": login ("" se ativo), "motivo_fim": str,
         "inicio_tipo": tipo da movimentação que abriu, "fim_tipo": tipo da que fechou ("" se ativo)}

    Uma consulta, com `select_related` — sem N+1."""
    vinculos = (
        FrotaVinculo.objects.filter(chip=chip)
        .select_related("veiculo", "veiculo__tipo", "item", "inicio_por", "fim_por", "mov_inicio", "mov_fim")
        .order_by("-inicio_em", "-pk")
    )
    saida = []
    for v in vinculos:
        duracao = v.duracao
        saida.append({
            "vinculo_id": v.pk, "veiculo": v.veiculo, "item": v.item, "destino_tipo": v.destino_tipo,
            "destino_rotulo": v.destino_rotulo, "inicio_em": v.inicio_em, "fim_em": v.fim_em,
            "ativo": v.fim_em is None, "duracao": duracao, "duracao_txt": formatar_duracao(duracao),
            "inicio_por": v.inicio_por.get_username() if v.inicio_por_id else "",
            "fim_por": v.fim_por.get_username() if v.fim_por_id else "",
            "motivo_fim": v.motivo_fim,
            "inicio_tipo": v.mov_inicio.tipo if v.mov_inicio_id else "",
            "fim_tipo": v.mov_fim.tipo if v.mov_fim_id else "",
        })
    return saida


def chips_do_equipamento(item: Item, *, anteriores: int = 5) -> dict:
    """O que a FICHA de um equipamento do estoque de TI mostra do(s) chip(s) dele — só LEITURA:

        {"ativos": [{"vinculo", "chip", "inicio_em", "inicio_por", "duracao_txt"}],   # o chip já passou por `anotar_chips`
         "anteriores": [{"vinculo", "chip", "inicio_em", "fim_em", "duracao_txt", "fim_por", "motivo_fim", "fim_tipo"}],
         "total_anteriores": int}

    `ativos` traz a conectividade de cada chip como a lista de chips a mostra (sessão, frescor, consumo, falha):
    o cartão da ficha NÃO reinterpreta nada — o que a API disse é lido de `anotar_chips`. `anteriores` são os
    últimos períodos encerrados (quem teve chip neste aparelho, e por quê saiu), no máximo `anteriores`."""
    periodos = list(
        FrotaVinculo.objects.filter(item=item)
        .select_related(
            "chip", "chip__operadora", "chip__localidade", "chip__responsavel", "chip__sessao_consulta",
            "chip__consumo_consulta", "inicio_por", "fim_por", "mov_fim",
        )
        .order_by("-inicio_em", "-pk")
    )
    ativos_vinculos = [v for v in periodos if v.fim_em is None]
    encerrados = [v for v in periodos if v.fim_em is not None]
    chips = m2m.anotar_chips([v.chip for v in ativos_vinculos]) if ativos_vinculos else []
    ativos = [
        {
            "vinculo": v, "chip": chip, "inicio_em": v.inicio_em,
            "inicio_por": v.inicio_por.get_username() if v.inicio_por_id else "",
            "duracao_txt": formatar_duracao(v.duracao),
        }
        for v, chip in zip(ativos_vinculos, chips)
    ]
    historico = [
        {
            "vinculo": v, "chip": v.chip, "inicio_em": v.inicio_em, "fim_em": v.fim_em,
            "duracao_txt": formatar_duracao(v.duracao),
            "fim_por": v.fim_por.get_username() if v.fim_por_id else "",
            "motivo_fim": v.motivo_fim, "fim_tipo": v.mov_fim.tipo if v.mov_fim_id else "",
        }
        for v in encerrados[: max(0, anteriores)]
    ]
    return {"ativos": ativos, "anteriores": historico, "total_anteriores": len(encerrados)}


def _digitos(texto) -> str:
    return re.sub(r"\D", "", texto or "")


def _inicio_do_dia(dia):
    return timezone.make_aware(datetime.combine(dia, hora_do_dia.min))


def historico_movimentacoes(filtros: dict | None = None):
    """QuerySet de `FrotaMovimentacao` (mais novas primeiro), com chip, veículos, usuário e
    importação já carregados. `filtros` (todos opcionais; vazio ou inválido é ignorado):

        tipo      um de `FrotaMovimentacao.Tipo`
        chip      id (só dígitos) OU trecho do nome / ICCID / IMSI (de `chip` ou `chip_relacionado`)
        veiculo   id OU trecho do código / nome / placa (origem OU destino)
        usuario   id OU trecho do login
        de, ate   `AAAA-MM-DD` (ate inclusive)
        operacao  UUID (as linhas de uma mesma ação)
        q         busca geral: chip, veículo, motivo ou login

    Para mostrar a substituição como UMA ação, passe o resultado por `agrupar_por_operacao`."""
    filtros = filtros or {}
    qs = FrotaMovimentacao.objects.select_related(
        "chip", "chip__operadora", "chip_relacionado", "veiculo_origem", "veiculo_destino",
        "item_origem", "item_destino", "usuario", "importacao",
        # o tipo de cada ponta, para a lista desenhar o ícone (mesma consulta, sem uma por linha)
        "veiculo_origem__tipo", "veiculo_origem__classe_operacional", "veiculo_destino__tipo", "veiculo_destino__classe_operacional",
        "item_origem__subtipo__categoria", "item_destino__subtipo__categoria",
    )
    if filtros.get("tipo") in Tipo.values:
        qs = qs.filter(tipo=filtros["tipo"])

    def por_chip(valor: str) -> Q:
        if valor.isdigit() and len(valor) <= 9:
            return Q(chip_id=int(valor)) | Q(chip_relacionado_id=int(valor))
        condicao = Q(chip__nome__icontains=valor) | Q(chip_relacionado__nome__icontains=valor)
        digitos = _digitos(valor)
        if len(digitos) >= 3:
            condicao |= (
                Q(chip__iccid__contains=digitos) | Q(chip__imsi__contains=digitos)
                | Q(chip_relacionado__iccid__contains=digitos) | Q(chip_relacionado__imsi__contains=digitos)
            )
        return condicao

    def por_veiculo(valor: str) -> Q:
        if valor.isdigit() and len(valor) <= 9:
            return Q(veiculo_origem_id=int(valor)) | Q(veiculo_destino_id=int(valor))
        placa = valor.replace("-", "").replace(" ", "")
        return (
            Q(veiculo_origem__codigo__icontains=valor) | Q(veiculo_origem__nome__icontains=valor)
            | Q(veiculo_origem__placa__icontains=placa)
            | Q(veiculo_destino__codigo__icontains=valor) | Q(veiculo_destino__nome__icontains=valor)
            | Q(veiculo_destino__placa__icontains=placa)
        )

    chip = str(filtros.get("chip") or "").strip()
    if chip:
        qs = qs.filter(por_chip(chip))
    veiculo = str(filtros.get("veiculo") or "").strip()
    if veiculo:
        qs = qs.filter(por_veiculo(veiculo))
    usuario = str(filtros.get("usuario") or "").strip()
    if usuario:
        qs = qs.filter(Q(usuario_id=int(usuario)) if usuario.isdigit() and len(usuario) <= 9 else Q(usuario_nome__icontains=usuario))
    de, ate = m2m.interpretar_data(filtros.get("de")), m2m.interpretar_data(filtros.get("ate"))
    if de:
        qs = qs.filter(em__gte=_inicio_do_dia(de))
    if ate:
        qs = qs.filter(em__lt=_inicio_do_dia(ate + timedelta(days=1)))
    operacao = str(filtros.get("operacao") or "").strip()
    if operacao:
        try:
            qs = qs.filter(operacao=uuid.UUID(operacao))
        except ValueError:
            pass
    def por_equipamento(valor: str) -> Q:
        """O equipamento de TI (tablet, celular) de qualquer lado da movimentação: nome ou número de série."""
        return (
            Q(item_origem__nome__icontains=valor) | Q(item_origem__numero_serie__icontains=valor)
            | Q(item_destino__nome__icontains=valor) | Q(item_destino__numero_serie__icontains=valor)
        )

    q = str(filtros.get("q") or "").strip()
    if q:
        qs = qs.filter(
            por_chip(q) | por_veiculo(q) | por_equipamento(q) | Q(motivo__icontains=q) | Q(usuario_nome__icontains=q)
        )
    return qs.order_by("-em", "-pk")


def agrupar_por_operacao(movimentacoes) -> list[dict]:
    """Junta as linhas de uma mesma ação (mesmo `operacao`), mantendo a ordem em que
    aparecem: `[{"operacao": UUID, "movimentacoes": [...], "tipo", "em", "usuario_nome", "motivo"}]`.
    Só a substituição tem mais de uma linha; o resto vem com uma."""
    grupos: dict = {}
    for mov in movimentacoes:
        grupo = grupos.get(mov.operacao)
        if grupo is None:
            grupo = grupos[mov.operacao] = {
                "operacao": mov.operacao, "movimentacoes": [], "tipo": mov.tipo, "em": mov.em,
                "usuario_nome": mov.usuario_nome, "motivo": mov.motivo,
            }
        grupo["movimentacoes"].append(mov)
    return list(grupos.values())


def chips_disponiveis(busca: str = "", limite: int = 20) -> list[dict]:
    """Chips DISPONÍVEIS para o seletor de "Vincular chip", no formato do Select2:
    `[{"id", "text", "nome", "iccid", "imsi", "linha", "operadora"}]`. `busca` vazia lista
    os primeiros por nome; com texto, procura em nome, ICCID, IMSI e número da linha.
    Chip vinculado, em manutenção ou inativo nunca aparece."""
    qs = M2MChip.objects.filter(situacao_uso=Uso.DISPONIVEL).select_related("operadora")
    busca = (busca or "").strip()
    if busca:
        condicao = Q(nome__icontains=busca)
        digitos = _digitos(busca)
        if len(digitos) >= 3:
            condicao |= Q(iccid__contains=digitos) | Q(imsi__contains=digitos) | Q(numero_linha__contains=digitos)
        qs = qs.filter(condicao)
    saida = []
    for chip in qs.order_by("nome", "pk")[: max(1, limite)]:
        partes = [chip.nome, chip.iccid or chip.imsi, chip.numero_linha, chip.operadora.nome]
        saida.append({
            "id": chip.pk, "text": " · ".join(p for p in partes if p), "nome": chip.nome,
            "iccid": chip.iccid, "imsi": chip.imsi, "linha": chip.numero_linha, "operadora": chip.operadora.nome,
        })
    return saida


# ═════════════════════════════════════════════════════════════
# Exportação das movimentações (Excel)
# ═════════════════════════════════════════════════════════════

LIMITE_DA_EXPORTACAO = 20000
_PREFIXOS_DE_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def _texto_seguro(valor) -> str:
    """Texto para o Excel: o que começa com `=`, `+`, `-`, `@` (ou tab/CR) ganha um apóstrofo na
    frente — motivo, observação e nome de veículo são digitados por gente e não podem virar
    fórmula quando o arquivo é aberto (injeção de fórmula)."""
    texto = "" if valor is None else str(valor)
    return "'" + texto if texto[:1] in _PREFIXOS_DE_FORMULA else texto


def exportar_movimentacoes_xlsx(filtros: dict | None = None) -> bytes:
    """As movimentações que o `filtros` seleciona (os MESMOS de `historico_movimentacoes`), em
    Excel: aba "Movimentações" (uma linha por registro, mais novas primeiro, no máximo
    `LIMITE_DA_EXPORTACAO`) e aba "Filtros" (o que foi pedido, quantas linhas saíram e se o teto
    cortou). TODA célula de texto passa por `_texto_seguro` e é gravada como TEXTO — nada vira
    fórmula. A substituição sai como duas linhas com a MESMA `Operação`. Devolve os bytes do .xlsx."""
    import io

    from openpyxl import Workbook
    from openpyxl.styles import Font

    filtros = {k: v for k, v in (filtros or {}).items() if v not in (None, "")}
    consulta = historico_movimentacoes(filtros)
    total = consulta.count()
    linhas = list(consulta[:LIMITE_DA_EXPORTACAO])

    wb = Workbook()
    ws = wb.active
    ws.title = "Movimentações"

    def texto(planilha, linha, coluna, valor):
        celula = planilha.cell(row=linha, column=coluna)
        celula.value = _texto_seguro(valor)
        celula.data_type = "s"
        return celula

    cabecalho = [
        "Data e hora", "Tipo", "Chip", "ICCID", "IMSI", "Chip relacionado", "Origem (veículo ou equipamento)",
        "Destino (veículo ou equipamento)", "Motivo", "Observações", "Usuário", "Operação", "Importação",
    ]
    for coluna, titulo in enumerate(cabecalho, start=1):
        texto(ws, 1, coluna, titulo).font = Font(bold=True)
    for i, m in enumerate(linhas, start=2):
        valores = (
            timezone.localtime(m.em).strftime("%d/%m/%Y %H:%M:%S"), m.get_tipo_display(), m.chip.nome, m.chip.iccid,
            m.chip.imsi, m.chip_relacionado.nome if m.chip_relacionado_id else "", m.origem_rotulo,
            m.destino_rotulo, m.motivo, m.observacoes, m.usuario_nome, str(m.operacao),
            m.importacao.arquivo_nome if m.importacao_id else "",
        )
        for coluna, valor in enumerate(valores, start=1):
            texto(ws, i, coluna, valor)
    for coluna, largura in zip("ABCDEFGHIJKLM", (20, 22, 26, 24, 18, 26, 30, 30, 40, 30, 18, 38, 28)):
        ws.column_dimensions[coluna].width = largura
    ws.freeze_panes = "A2"

    resumo = wb.create_sheet("Filtros")
    itens = [("Gerado em", timezone.localtime(timezone.now()).strftime("%d/%m/%Y %H:%M")), ("Movimentações no filtro", total),
             ("Linhas exportadas", len(linhas)), ("Cortado pelo limite", "sim" if total > len(linhas) else "não"), ("", "")]
    itens += [(f"Filtro: {chave}", valor) for chave, valor in sorted(filtros.items())]
    for i, (rotulo, valor) in enumerate(itens, start=1):
        texto(resumo, i, 1, rotulo).font = Font(bold=True)
        if isinstance(valor, int):
            resumo.cell(row=i, column=2, value=valor)
        else:
            texto(resumo, i, 2, valor)
    resumo.column_dimensions["A"].width = 30
    resumo.column_dimensions["B"].width = 50
    saida = io.BytesIO()
    wb.save(saida)
    return saida.getvalue()
