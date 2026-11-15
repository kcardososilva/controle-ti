"""
Situação do chip: o que o sistema SUGERE — e só isso.

A situação do chip (`M2MChip.situacao_uso`: disponível, vinculado, reservado, pausado, com defeito, em manutenção,
inativo) é decisão de gente e só `frota_vinculo_service` a escreve. Este módulo LÊ sinais que já existem no sistema e
os transforma em sugestões para a tela do chip, cada uma com a ação que o usuário pode escolher — e cada ação passa
pelo mesmo endpoint de sempre, que valida de novo no servidor. Nada aqui grava, em lugar nenhum.

Por que sugerir e não aplicar: o status do tablet NÃO é a sessão do chip, nos dois sentidos (regra 62-b). Uma consulta
que falhou, uma rede fora do ar ou um tablet na oficina não provam que o chip quebrou; marcar defeito sozinho por causa
disso esconderia chip bom e contaria como "com defeito" o que é instabilidade. O sinal vira pergunta, a pessoa responde.

As quatro sugestões (`codigo`):
  • `equipamento_parado`    — o chip está vinculado a um equipamento de TI em defeito, manutenção ou pausado.
  • `diverge_do_estoque`    — o item do chip no estoque de TI está em outra situação (`situacao_sugerida_pelo_item`).
  • `sem_sessao_vinculado`  — o chip vinculado está com a ocorrência "sem sessão" em aberto. O limite de tempo é o das regras
                             de alerta que o administrador já configura: nenhum número novo mora aqui. Só para quem vê API.
  • `parado_ha_muito`       — manutenção, defeito, pausado ou reservado há mais de `DIAS_PARA_REAVALIAR` dias.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.utils import timezone

from ProjetoEstoque.models import FrotaVinculo, Item, M2MChip, M2MOcorrencia, StatusItemChoices
from services import chip_estoque_ti_service as estoque_ti

Uso = M2MChip.SituacaoUso

# Quanto tempo um chip pode ficar em manutenção, com defeito, pausado ou reservado antes de a tela perguntar o que
# fazer com ele. É uma escolha de partida (30 dias), não uma regra do negócio: mudar é mudar este número.
DIAS_PARA_REAVALIAR = 30
SITUACOES_QUE_PEDEM_DECISAO = (Uso.MANUTENCAO.value, Uso.DEFEITO.value, Uso.PAUSADO.value, Uso.RESERVADO.value)
# Equipamento de TI que "não está bem": é a mesma régua que `frota_vinculo_service` usa para avisar ao vincular.
STATUS_DE_EQUIPAMENTO_PARADO = (StatusItemChoices.DEFEITO, StatusItemChoices.MANUTENCAO, StatusItemChoices.PAUSADO)

ACAO_MUDAR = "mudar"                # abre o diálogo "Mudar situação" já com a situação proposta
ACAO_DESVINCULAR = "desvincular"    # abre o diálogo "Desvincular" já com o destino proposto (chip vinculado não muda direto)


@dataclass(frozen=True)
class Sugestao:
    codigo: str
    titulo: str
    detalhe: str
    acao: str = ""                  # `ACAO_MUDAR` | `ACAO_DESVINCULAR` | "" (só informa)
    situacao: str = ""              # a situação que a ação propõe
    tom: str = "atencao"            # info | atencao

    @property
    def rotulo_da_acao(self) -> str:
        if not self.acao:
            return ""
        alvo = dict(Uso.choices).get(self.situacao, self.situacao).lower()
        return f"Marcar como {alvo}" if self.acao == ACAO_MUDAR else f"Desvincular e marcar como {alvo}"


def _rotulo(situacao: str) -> str:
    return dict(Uso.choices).get(situacao, situacao).lower()


def _dias(desde, agora) -> int:
    return max(0, (agora - desde).days) if desde else 0


def sugestoes_para(chip: M2MChip, *, com_api: bool = False, agora=None) -> list[Sugestao]:
    """As sugestões de situação para UM chip (lista vazia quando não há o que sugerir). `com_api`: quem vê API — só
    então a sugestão baseada na sessão (que é dado da operadora) entra. Faz até 3 consultas pequenas, por chip: é tela de
    detalhe, não lista."""
    agora = agora or timezone.now()
    achados: list[Sugestao] = []
    vinculo = FrotaVinculo.objects.filter(chip=chip, fim_em__isnull=True).select_related("veiculo", "item").first()
    equipamento = vinculo.item if vinculo is not None and vinculo.item_id else None

    if equipamento is not None and equipamento.status in STATUS_DE_EQUIPAMENTO_PARADO:
        achados.append(Sugestao(
            codigo="equipamento_parado",
            titulo=f"O equipamento está {equipamento.get_status_display().lower()}",
            detalhe=(
                f"O chip está vinculado a {equipamento.nome}, que consta como “{equipamento.get_status_display()}” no "
                "estoque de TI. Se o chip saiu do aparelho, desvincule e diga onde ele ficou."
            ),
            acao=ACAO_DESVINCULAR, situacao=Uso.DISPONIVEL.value, tom="info",
        ))

    if chip.item_ti_id:
        item = Item.all_objects.filter(pk=chip.item_ti_id).first()
        sugerida = estoque_ti.situacao_sugerida_pelo_item(chip, item) if item is not None else ""
        if sugerida:
            achados.append(Sugestao(
                codigo="diverge_do_estoque",
                titulo=f"O estoque de TI diz “{item.get_status_display()}”",
                detalhe=(
                    f"O item deste chip está “{item.get_status_display()}” no estoque de TI e aqui o chip está "
                    f"{_rotulo(chip.situacao_uso)}. Nada foi mudado: confirme se o chip passa a ficar {_rotulo(sugerida)}."
                ),
                acao=ACAO_MUDAR, situacao=sugerida, tom="info",
            ))

    if com_api and vinculo is not None and chip.situacao_uso == Uso.VINCULADO:
        aberta = chip.ocorrencias.filter(
            tipo=M2MOcorrencia.Tipo.SEM_SESSAO, estado__in=M2MOcorrencia.ESTADOS_ABERTOS,
        ).order_by("-detectada_em").first()
        if aberta is not None:
            achados.append(Sugestao(
                codigo="sem_sessao_vinculado",
                titulo="Vinculado e sem sessão",
                detalhe=(
                    f"O alerta “Sem sessão” está aberto desde {timezone.localtime(aberta.detectada_em):%d/%m %H:%M}. "
                    "Pode ser o SIM, o aparelho ou só um equipamento desligado: a consulta sozinha não diz qual — "
                    "confira antes de marcar defeito."
                ),
                acao=ACAO_DESVINCULAR, situacao=Uso.DEFEITO.value,
            ))

    if chip.situacao_uso in SITUACOES_QUE_PEDEM_DECISAO and chip.situacao_uso_em:
        dias = _dias(chip.situacao_uso_em, agora)
        if dias >= DIAS_PARA_REAVALIAR:
            achados.append(Sugestao(
                codigo="parado_ha_muito",
                titulo=f"{chip.get_situacao_uso_display()} há {dias} dias",
                detalhe=(
                    f"O chip está {_rotulo(chip.situacao_uso)} desde {timezone.localtime(chip.situacao_uso_em):%d/%m/%Y}. "
                    "Vale decidir: devolver à disponibilidade, mudar a situação ou inativar."
                ),
            ))
    return achados


def dias_na_situacao(chip: M2MChip, agora=None) -> int | None:
    """Há quantos dias o chip está na situação atual, ou `None` quando não se sabe (chip criado fora do cadastro, antes
    da migration 0210)."""
    if not chip.situacao_uso_em:
        return None
    return _dias(chip.situacao_uso_em, agora or timezone.now())
