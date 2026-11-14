"""
Chips do Agro × estoque de TI (regra 66): UM chip, dois cadastros, uma ligação.

No estoque de TI o chip é um `Item` do tipo marcado "É chip (SIM)": número de série = ICCID, valor, nota fiscal, termo de
responsabilidade por responsável, movimentações, status. Em Frotas e Conectividade ele é um `M2MChip`: linha, operadora,
sessão, consumo, em que veículo ou tablet está. Eram dois cadastros do mesmo SIM sem ligação nenhuma; aqui mora o que os
liga, sem fundir nem duplicar nada:

  • QUEM É DONO DE QUÊ. O TI (o `Item`) é dono do PATRIMÔNIO: valor, NF, fornecedor, centro de custo, status, termo.
    A conectividade (o `M2MChip`) é dona da LINHA: ICCID/IMSI, número, operadora, sessão, consumo, vínculo operacional.
    Cada tela mostra o dado do outro lado LIDO, com a origem dita — nunca uma cópia que envelhece.
  • A CHAVE É O ICCID. `M2MChip.iccid` × `Item.numero_serie`, comparados pelos 19 primeiros dígitos (o 20º é o dígito
    verificador, que algumas fontes imprimem e outras não). A ligação (`M2MChip.item_ti`) é 1 para 1, garantida pelo
    BANCO (`OneToOneField`), e só REFERÊNCIA: nenhuma operação daqui altera um `Item` existente, uma movimentação ou o
    status dele. A única escrita no estoque é CRIAR o item de um chip que ainda não tem patrimônio
    (`cadastrar_itens_dos_chips`), por ação explícita do TI.
  • NADA SE LIGA SOZINHO NA DÚVIDA. Dois identificadores diferentes pedem confirmação (`forcar`); ICCID repetido em mais de
    um chip ou item vira "ambíguo" e não é ligado; só o par 1 × 1 de mesmo ICCID entra na conciliação automática.
  • TUDO FICA NO REGISTRO. Ligar e desligar gravam `M2MEvento` (quem, quando, como, qual item). Desligar exige motivo.
  • DIVERGÊNCIA É APONTADA, NUNCA CORRIGIDA. Item descartado com chip ainda monitorado, chip inativo com item ativo, chip em
    uso com o item parado em Backup, item no nome de uma pessoa com o chip num veículo: `divergencias_do_par` diz, a
    tela mostra, e quem decide é o TI. Leitura não escreve.

Todas as funções de gravação levantam `ValidationError` com a frase do que fazer.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from ProjetoEstoque.models import (
    Categoria,
    CentroCusto,
    ConectividadeOperadora,
    Fornecedor,
    Item,
    ItemColaborador,
    ItemStatusHistorico,
    Localidade,
    M2MChip,
    M2MEvento,
    MovimentacaoItem,
    SimNaoChoices,
    StatusAssinaturaTermoChoices,
    StatusItemChoices,
    Subtipo,
    TermoAssinatura,
    TipoMovimentacaoChoices,
    TipoTransferenciaChoices,
)

User = get_user_model()

CODIGO_IDENTIFICADORES_DIFERENTES = "identificadores_diferentes"
STATUS_FORA_DE_USO = (StatusItemChoices.DESCARTE, StatusItemChoices.DEVOLVIDO)
STATUS_PARADO = (StatusItemChoices.BACKUP, StatusItemChoices.ESTOQUE)
STATUS_VIVO = (
    StatusItemChoices.ATIVO, StatusItemChoices.BACKUP, StatusItemChoices.ESTOQUE, StatusItemChoices.RESERVADO,
    StatusItemChoices.PAUSADO, StatusItemChoices.MANUTENCAO, StatusItemChoices.DEFEITO,
)


def _usuario(usuario):
    return usuario if getattr(usuario, "pk", None) else None


# ═════════════════════════════════════════════════════════════
# A chave da ligação
# ═════════════════════════════════════════════════════════════

def chave_iccid(valor) -> str:
    """Os 19 primeiros dígitos do ICCID, ou `""` se o valor não parece um (série "S/N", vazia, curta demais). O 20º
    dígito é o verificador: comparar sem ele deixa `...4995` e `...499` (impresso sem verificador) serem o mesmo SIM."""
    digitos = re.sub(r"\D", "", str(valor or ""))
    return digitos[:19] if len(digitos) >= 18 else ""


def itens_de_chip():
    """Os itens do estoque de TI que SÃO chips: tipo marcado "É chip (SIM)", não de consumo, não excluídos."""
    return Item.objects.filter(subtipo__e_chip=True, item_consumo=SimNaoChoices.NAO).select_related(
        "subtipo", "localidade", "centro_custo", "fornecedor",
    )


def subtipos_de_chip() -> list[Subtipo]:
    return list(Subtipo.objects.filter(e_chip=True).select_related("categoria").order_by("nome"))


def _conferir_item_elegivel(item: Item) -> None:
    if item.excluido:
        raise ValidationError("O item está excluído do estoque de TI e não pode ser ligado a um chip.")
    if item.subtipo_id is None or not item.subtipo.e_chip:
        raise ValidationError(
            "O tipo deste item não está marcado como chip. No cadastro de tipos, marque “É chip (SIM)” no tipo dos chips."
        )
    if item.item_consumo == SimNaoChoices.SIM:
        raise ValidationError("Item de consumo não pode ser ligado a um chip: cada chip é um item individual.")


# ═════════════════════════════════════════════════════════════
# Ligar e desligar
# ═════════════════════════════════════════════════════════════

@dataclass
class ResultadoLigacao:
    chip: M2MChip
    item: Item
    avisos: list = field(default_factory=list)


def vincular(chip: M2MChip, item: Item, usuario=None, *, origem: str = "manual", forcar: bool = False) -> ResultadoLigacao:
    """Liga o chip ao item do estoque de TI. Recusa: item excluído, de tipo que não é chip ou de consumo; chip já ligado
    (a outro item ou a este); item já ligado a outro chip; identificadores diferentes (ICCID do chip × série do item) —
    esta com `code="identificadores_diferentes"`: a tela pergunta e reenvia com `forcar=True`. Grava `M2MEvento`.
    A ligação é UPDATE condicional (`item_ti IS NULL`) e o `OneToOneField` é a segunda trava: duas pessoas ao mesmo tempo
    não ligam o mesmo chip duas vezes nem o mesmo item a dois chips."""
    with transaction.atomic():
        atual = M2MChip.objects.select_related("item_ti").get(pk=chip.pk)
        item = Item.all_objects.select_related("subtipo").get(pk=item.pk)
        _conferir_item_elegivel(item)
        if atual.item_ti_id == item.pk:
            raise ValidationError("Este chip já está ligado a este item.")
        if atual.item_ti_id:
            raise ValidationError(
                f"Este chip já está ligado ao item “{atual.item_ti.nome}” (nº {atual.item_ti.numero_serie or 's/ nº'}). "
                "Desligue antes de ligar a outro."
            )
        outro = M2MChip.objects.filter(item_ti=item).exclude(pk=atual.pk).first()
        if outro:
            raise ValidationError(f"Este item já está ligado ao chip {outro.nome}. Cada item tem um chip só.")
        chave_chip, chave_item = chave_iccid(atual.iccid), chave_iccid(item.numero_serie)
        diferentes = bool(chave_chip and chave_item and chave_chip != chave_item)
        if diferentes and not forcar:
            raise ValidationError(
                f"O ICCID do chip ({atual.iccid}) é diferente do número de série do item ({item.numero_serie}). "
                "Se é o mesmo SIM, confirme a ligação.",
                code=CODIGO_IDENTIFICADORES_DIFERENTES,
            )
        agora = timezone.now()
        try:
            tomou = M2MChip.objects.filter(pk=atual.pk, item_ti__isnull=True).update(
                item_ti=item, item_ti_em=agora, item_ti_por=_usuario(usuario), item_ti_origem=origem,
                atualizado_por=_usuario(usuario), updated_at=agora,
            )
        except IntegrityError:
            raise ValidationError("Este item acabou de ser ligado a outro chip por outra pessoa.") from None
        if not tomou:
            raise ValidationError("Este chip acabou de ser ligado por outra pessoa. Recarregue a tela.")
        M2MEvento.objects.create(
            chip=atual, tipo=M2MEvento.Tipo.ITEM_TI_LIGADO, usuario=_usuario(usuario),
            dados={
                "item": item.pk, "item_nome": item.nome, "numero_serie": item.numero_serie or "", "origem": origem,
                "identificadores_diferentes": diferentes,
            },
        )
    atual.refresh_from_db()
    avisos = [d["texto"] for d in divergencias_do_par(atual, item)]
    if diferentes:
        avisos.insert(0, "Ligado mesmo com identificadores diferentes: confira se é o mesmo SIM.")
    return ResultadoLigacao(chip=atual, item=item, avisos=avisos)


def desvincular(chip: M2MChip, usuario=None, motivo: str = "") -> M2MChip:
    """Desfaz a ligação (o chip e o item continuam existindo, cada um no seu cadastro). O MOTIVO é obrigatório."""
    motivo = " ".join(str(motivo or "").split())
    if not motivo:
        raise ValidationError({"motivo": "Informe o motivo de desligar o chip do estoque de TI."})
    if len(motivo) > 255:
        raise ValidationError({"motivo": "O motivo aceita até 255 caracteres."})
    with transaction.atomic():
        atual = M2MChip.objects.select_related("item_ti").get(pk=chip.pk)
        if not atual.item_ti_id:
            raise ValidationError("Este chip não está ligado a nenhum item do estoque de TI.")
        item = atual.item_ti
        agora = timezone.now()
        tomou = M2MChip.objects.filter(pk=atual.pk, item_ti_id=item.pk).update(
            item_ti=None, item_ti_em=None, item_ti_por=None, item_ti_origem="", atualizado_por=_usuario(usuario), updated_at=agora,
        )
        if not tomou:
            raise ValidationError("Esta ligação já foi desfeita por outra pessoa. Recarregue a tela.")
        M2MEvento.objects.create(
            chip=atual, tipo=M2MEvento.Tipo.ITEM_TI_DESLIGADO, usuario=_usuario(usuario),
            dados={"item": item.pk, "item_nome": item.nome, "numero_serie": item.numero_serie or "", "motivo": motivo},
        )
    atual.refresh_from_db()
    return atual


def vincular_se_unico(chip: M2MChip, usuario=None) -> Item | None:
    """Chip recém-criado: se existe UM item de chip livre com o mesmo ICCID (e nenhum outro chip o disputa), liga. É o que
    faz o chip cadastrado ou importado já nascer integrado ao patrimônio. Em qualquer dúvida (zero, vários) não faz nada."""
    chave = chave_iccid(chip.iccid)
    if not chave or chip.item_ti_id:
        return None
    ligados = set(M2MChip.objects.filter(item_ti__isnull=False).values_list("item_ti_id", flat=True))
    candidatos = [i for i in itens_de_chip() if i.pk not in ligados and chave_iccid(i.numero_serie) == chave]
    if len(candidatos) != 1:
        return None
    concorrentes = [c for c in M2MChip.objects.filter(item_ti__isnull=True).exclude(pk=chip.pk) if chave_iccid(c.iccid) == chave]
    if concorrentes:
        return None
    try:
        vincular(chip, candidatos[0], usuario, origem="iccid")
    except ValidationError:
        return None
    chip.refresh_from_db()
    return candidatos[0]


# ═════════════════════════════════════════════════════════════
# A conciliação: o que o sistema propõe e o que sobra
# ═════════════════════════════════════════════════════════════

@dataclass
class Conciliacao:
    ligados: list            # [(chip, item)]
    automaticos: list        # [(chip, item)] — 1 × 1 pelo ICCID, ambos livres
    ambiguos: list           # [{"chave", "chips", "itens"}]
    itens_sem_chip: list     # itens de chip livres, sem chip de mesmo ICCID
    chips_sem_item: list     # chips livres, sem item de mesmo ICCID
    itens_sem_iccid: list    # subconjunto de `itens_sem_chip` cuja série não é um ICCID
    total_chips: int
    total_itens: int


def conciliacao() -> Conciliacao:
    """Cruza TODOS os chips com TODOS os itens de chip, em duas leituras (sem consulta por linha)."""
    chips = list(M2MChip.objects.select_related("operadora", "item_ti", "item_ti__subtipo").order_by("nome", "pk"))
    itens = list(itens_de_chip().order_by("nome", "pk"))
    ligados_ids = {c.item_ti_id for c in chips if c.item_ti_id}
    chips_livres = [c for c in chips if not c.item_ti_id]
    itens_livres = [i for i in itens if i.pk not in ligados_ids]

    por_chave_chip: dict[str, list] = defaultdict(list)
    for c in chips_livres:
        if chave_iccid(c.iccid):
            por_chave_chip[chave_iccid(c.iccid)].append(c)
    por_chave_item: dict[str, list] = defaultdict(list)
    for i in itens_livres:
        if chave_iccid(i.numero_serie):
            por_chave_item[chave_iccid(i.numero_serie)].append(i)

    automaticos, ambiguos = [], []
    for chave in sorted(set(por_chave_chip) & set(por_chave_item)):
        cs, its = por_chave_chip[chave], por_chave_item[chave]
        if len(cs) == 1 and len(its) == 1:
            automaticos.append((cs[0], its[0]))
        else:
            ambiguos.append({"chave": chave, "chips": cs, "itens": its})
    ambiguos_itens = {i.pk for a in ambiguos for i in a["itens"]}
    ambiguos_chips = {c.pk for a in ambiguos for c in a["chips"]}
    pareados_itens = {i.pk for _, i in automaticos} | ambiguos_itens
    pareados_chips = {c.pk for c, _ in automaticos} | ambiguos_chips
    itens_sem_chip = [i for i in itens_livres if i.pk not in pareados_itens]
    return Conciliacao(
        ligados=[(c, c.item_ti) for c in chips if c.item_ti_id],
        automaticos=automaticos,
        ambiguos=ambiguos,
        itens_sem_chip=itens_sem_chip,
        chips_sem_item=[c for c in chips_livres if c.pk not in pareados_chips],
        itens_sem_iccid=[i for i in itens_sem_chip if not chave_iccid(i.numero_serie)],
        total_chips=len(chips),
        total_itens=len(itens),
    )


@dataclass
class ResumoConciliacao:
    ligados: int = 0
    falhas: list = field(default_factory=list)       # [(chip, texto)]


def conciliar_automaticamente(usuario=None) -> ResumoConciliacao:
    """Liga todos os pares 1 × 1 de mesmo ICCID. Cada par é uma transação (um que falha não desfaz os outros) e cada um
    grava o seu evento. Idempotente: rodar de novo não acha mais nada para ligar."""
    resumo = ResumoConciliacao()
    for chip, item in conciliacao().automaticos:
        try:
            vincular(chip, item, usuario, origem="iccid")
            resumo.ligados += 1
        except ValidationError as exc:
            resumo.falhas.append((chip, " ".join(exc.messages)))
    return resumo


def contar_chips_sem_item() -> int:
    return M2MChip.objects.filter(item_ti__isnull=True).count()


def sugestoes_para_item(item: Item, limite: int = 5) -> list[M2MChip]:
    """Chips livres que podem ser deste item: o de mesmo ICCID primeiro."""
    chave = chave_iccid(item.numero_serie)
    livres = M2MChip.objects.filter(item_ti__isnull=True).select_related("operadora")
    iguais = [c for c in livres if chave and chave_iccid(c.iccid) == chave]
    return iguais[:limite]


def opcoes_de_chips_sem_item(busca: str = "", limite: int = 20) -> list[dict]:
    """`[{"id", "text"}]` dos chips livres para o seletor da ficha do item. A busca vale para nome, ICCID, IMSI e linha."""
    qs = M2MChip.objects.filter(item_ti__isnull=True).order_by("nome")
    termo = (busca or "").strip()
    if termo:
        digitos = re.sub(r"\D", "", termo)
        from django.db.models import Q
        filtro = Q(nome__icontains=termo)
        if len(digitos) >= 3:
            filtro |= Q(iccid__contains=digitos) | Q(imsi__contains=digitos) | Q(numero_linha__contains=digitos)
        qs = qs.filter(filtro)
    return [
        {"id": c.pk, "text": f"{c.nome} · {c.iccid or c.imsi}" + (f" · {c.numero_linha}" if c.numero_linha else "")}
        for c in qs[:limite]
    ]


# ═════════════════════════════════════════════════════════════
# O patrimônio do chip, lido do estoque de TI
# ═════════════════════════════════════════════════════════════

@dataclass
class Detentor:
    rotulo: str
    colaborador: object = None     # `Usuario` quando é pessoa
    varios: list = field(default_factory=list)   # item compartilhado: os colaboradores com vínculo ativo


def _detentor_da_movimentacao(ultima) -> Detentor:
    """A MESMA regra da ficha do equipamento (`equipamento_detalhe`): a última movimentação decide; na devolução o
    `usuario` é quem devolveu, não quem detém. Se a ficha mudar, mude aqui junto: as duas telas têm de concordar."""
    if ultima is None:
        return Detentor("Em estoque / Não definido")
    eh_devolucao = (
        ultima.tipo_movimentacao == TipoMovimentacaoChoices.TRANSFERENCIA
        and ultima.tipo_transferencia == TipoTransferenciaChoices.DEVOLUCAO
    )
    if ultima.usuario and not eh_devolucao:
        return Detentor(f"Usuário: {ultima.usuario.nome}", colaborador=ultima.usuario)
    if ultima.centro_custo_destino:
        return Detentor(f"Setor: {ultima.centro_custo_destino.departamento}")
    if ultima.localidade_destino:
        return Detentor(f"Local: {ultima.localidade_destino.local}")
    if ultima.fornecedor_manutencao:
        return Detentor(f"Externo: {ultima.fornecedor_manutencao.nome}")
    return Detentor("Em estoque / Não definido")


def detentores_de(itens) -> dict[int, Detentor]:
    """`{item_id: Detentor}` para vários itens em DUAS consultas (última movimentação e vínculos de compartilhado)."""
    itens = list(itens)
    ids = [i.pk for i in itens]
    ultimas: dict[int, MovimentacaoItem] = {}
    for mov in (
        MovimentacaoItem.objects.filter(item_id__in=ids)
        .select_related("usuario", "centro_custo_destino", "localidade_destino", "fornecedor_manutencao")
        .order_by("item_id", "-created_at")
    ):
        ultimas.setdefault(mov.item_id, mov)
    compartilhados = {i.pk for i in itens if i.compartilhado}
    vinculos: dict[int, list] = defaultdict(list)
    if compartilhados:
        for v in ItemColaborador.objects.filter(item_id__in=compartilhados, ativo=True).select_related("colaborador"):
            vinculos[v.item_id].append(v.colaborador)
    resultado = {}
    for item in itens:
        if item.compartilhado:
            pessoas = vinculos.get(item.pk, [])
            resultado[item.pk] = Detentor(
                ", ".join(p.nome for p in pessoas) if pessoas else "Compartilhado / Sem vínculo ativo", varios=pessoas,
            )
        else:
            resultado[item.pk] = _detentor_da_movimentacao(ultimas.get(item.pk))
    return resultado


def termo_do_item(item: Item) -> dict:
    """O termo de responsabilidade do item: o último ASSINADO DIGITALMENTE (com quem assinou, quando e o código de
    verificação) e/ou o último PDF anexado a uma movimentação (papel escaneado). `situacao`: `digital`, `anexado` ou `sem`.
    Chip em estoque normalmente não tem termo — só passa a ter quando é entregue a alguém."""
    digital = (
        TermoAssinatura.objects.filter(itens=item, status=StatusAssinaturaTermoChoices.ASSINADO)
        .select_related("colaborador").order_by("-assinado_em", "-pk").first()
    )
    anexo = (
        MovimentacaoItem.objects.filter(item=item, revertida=False).exclude(termo_pdf="").exclude(termo_pdf__isnull=True)
        .order_by("-created_at").first()
    )
    if digital:
        return {
            "situacao": "digital", "rotulo": "Assinado digitalmente", "colaborador": digital.colaborador,
            "em": digital.assinado_em, "codigo": digital.codigo_verificacao, "tipo": digital.get_tipo_display(),
            "url": reverse("termo_assinatura_painel", args=[digital.pk]),
        }
    if anexo:
        return {
            "situacao": "anexado", "rotulo": "Termo anexado (papel)", "colaborador": anexo.usuario,
            "em": anexo.created_at, "codigo": "", "tipo": anexo.get_tipo_movimentacao_display(), "url": anexo.termo_pdf.url,
        }
    return {"situacao": "sem", "rotulo": "Sem termo", "colaborador": None, "em": None, "codigo": "", "tipo": "", "url": ""}


def ficha_do_item(item: Item, detentor: Detentor | None = None) -> dict:
    """Tudo do patrimônio que a tela do chip mostra, LIDO do `Item` (nada é copiado para o chip)."""
    detentor = detentor or detentores_de([item])[item.pk]
    return {
        "item": item, "url": reverse("equipamento_detalhe", args=[item.pk]),
        "status": item.status, "status_rotulo": item.get_status_display(), "excluido": item.excluido,
        "valor": item.valor, "numero_nf": item.numero_nf or "", "numero_pedido": item.numero_pedido or "",
        "data_compra": item.data_compra, "fornecedor": item.fornecedor, "centro_custo": item.centro_custo,
        "localidade": item.localidade, "detentor": detentor, "termo": termo_do_item(item),
    }


def onde_esta(chip: M2MChip) -> str:
    """Onde o chip está AGORA na operação (`COD · nome` do veículo, ou o tablet/celular), ou `""` se não está vinculado."""
    vinculo = chip.vinculos.filter(fim_em__isnull=True).select_related("veiculo", "item").first()
    return vinculo.destino_rotulo if vinculo else ""


def divergencias_do_par(chip: M2MChip, item: Item, detentor: Detentor | None = None, destino: str | None = None) -> list[dict]:
    """O que o chip e o item dizem de DIFERENTE: `[{"codigo", "tom" (erro|atencao|info), "texto"}]`. Só aponta."""
    achados = []
    ck, ik = chave_iccid(chip.iccid), chave_iccid(item.numero_serie)
    if ck and ik and ck != ik:
        achados.append({"codigo": "iccid", "tom": "erro", "texto": (
            f"O ICCID do chip ({chip.iccid}) é diferente do número de série do item ({item.numero_serie}): confira se é o mesmo SIM."
        )})
    em_uso = chip.situacao_uso in (M2MChip.SituacaoUso.VINCULADO, M2MChip.SituacaoUso.DISPONIVEL)
    if item.excluido and (chip.monitorado or em_uso):
        achados.append({"codigo": "item_excluido", "tom": "erro", "texto": (
            "O item foi excluído do estoque de TI, mas o chip segue ativo na conectividade."
        )})
    elif item.status in STATUS_FORA_DE_USO and (chip.monitorado or em_uso):
        achados.append({"codigo": "item_fora_de_uso", "tom": "atencao", "texto": (
            f"O item está em “{item.get_status_display()}” no TI, mas o chip segue {chip.get_situacao_uso_display().lower()} "
            "e monitorado na conectividade."
        )})
    if chip.situacao_uso == M2MChip.SituacaoUso.INATIVO and item.status in STATUS_VIVO and not item.excluido:
        achados.append({"codigo": "chip_inativo", "tom": "info", "texto": (
            f"O chip está inativo na conectividade, mas o item ainda consta como “{item.get_status_display()}” no TI: "
            "dê a baixa no estoque se o SIM saiu de uso."
        )})
    if chip.situacao_uso == M2MChip.SituacaoUso.VINCULADO:
        onde = destino if destino is not None else onde_esta(chip)
        if item.status in STATUS_PARADO:
            achados.append({"codigo": "item_parado", "tom": "info", "texto": (
                f"O chip está em uso ({onde or 'vinculado'}), mas o item consta como “{item.get_status_display()}” no TI."
            )})
        det = detentor or detentores_de([item])[item.pk]
        if det.colaborador is not None or det.varios:
            achados.append({"codigo": "item_com_pessoa", "tom": "info", "texto": (
                f"O item está no nome de {det.rotulo.replace('Usuário: ', '')} (termo de responsabilidade), "
                f"mas o chip está em {onde or 'um veículo/equipamento'}."
            )})
    return achados


def divergencias_dos_ligados(ligados) -> dict[int, list]:
    """`{chip_id: [divergências]}` para os pares ligados, com o detentor e o destino lidos em lote."""
    ligados = list(ligados)
    det = detentores_de([item for _, item in ligados])
    from ProjetoEstoque.models import FrotaVinculo
    destinos = {}
    for v in FrotaVinculo.objects.filter(chip_id__in=[c.pk for c, _ in ligados], fim_em__isnull=True).select_related("veiculo", "item"):
        destinos[v.chip_id] = v.destino_rotulo
    return {
        chip.pk: divergencias_do_par(chip, item, det[item.pk], destinos.get(chip.pk, ""))
        for chip, item in ligados
    }


def resumo_do_patrimonio() -> dict:
    """Os totais da conciliação: quantos ligados e o VALOR de aquisição desses itens (do que o TI cadastrou)."""
    from django.db.models import Count, Sum
    ligados = Item.all_objects.filter(chip_conectividade__isnull=False).aggregate(n=Count("pk"), valor=Sum("valor"))
    return {"ligados": ligados["n"] or 0, "valor": ligados["valor"] or Decimal("0.00")}


# ═════════════════════════════════════════════════════════════
# Criar de um lado a partir do outro
# ═════════════════════════════════════════════════════════════

def _nome_livre_do_chip(iccid: str) -> str:
    base = f"Chip {iccid[-6:]}"
    nome, n = base, 2
    while M2MChip.objects.filter(nome__iexact=nome).exists():
        nome = f"{base}-{n}"
        n += 1
    return nome


def criar_chip_do_item(item: Item, usuario=None, *, operadora=None) -> M2MChip:
    """O item do estoque de TI existe e o chip de conectividade ainda não: cria o chip (ICCID = série do item; operadora
    pela marca do item, senão a padrão; unidade = a do item) e liga, tudo numa transação. O item NÃO é alterado."""
    from services import m2m_monitoramento_service as m2m
    from services import tim_m2m_service as tim

    item = Item.all_objects.select_related("subtipo", "localidade").get(pk=item.pk)
    _conferir_item_elegivel(item)
    if M2MChip.objects.filter(item_ti=item).exists():
        raise ValidationError("Este item já está ligado a um chip.")
    try:
        iccid = tim.normalizar_iccid(item.numero_serie)
    except ValidationError:
        raise ValidationError(
            f"O número de série do item ({item.numero_serie or 'vazio'}) não é um ICCID válido: corrija-o no cadastro do item."
        ) from None
    if M2MChip.objects.filter(iccid=iccid).exists():
        raise ValidationError("Já existe um chip de conectividade com este ICCID: ligue-o a este item em vez de criar outro.")
    if operadora is None and item.marca:
        operadora = ConectividadeOperadora.objects.filter(ativo=True, nome__iexact=item.marca.strip()).first()
    dados = {
        "nome": _nome_livre_do_chip(iccid), "iccid": iccid, "descricao": item.nome, "localidade": item.localidade_id,
        **({"operadora": operadora} if operadora else {}),
    }
    return m2m.criar_chip(dados, usuario, item_ti=item, item_ti_origem="chip_do_item")


CAMPOS_DO_PADRAO = (
    "nome", "marca", "modelo", "valor", "categoria", "fornecedor", "numero_nf", "numero_pedido", "data_compra",
    "centro_custo", "localidade", "status",
)


def padrao_de_estoque() -> dict:
    """Como um chip de TI PARADO EM ESTOQUE é cadastrado, lido dos chips que JÁ estão no estoque — não de um valor fixo.

    Cada campo é o valor MAIS COMUM entre os itens de chip (`consistencia` diz "N de M" para cada um, para ninguém
    confiar num padrão que quase não existe). Três coisas NÃO são o estado atual do item, e sim o de ANTES da entrega:
    o centro de custo, a localidade e o status. Quando um chip é entregue a uma pessoa, a movimentação de entrega leva o
    item para o centro de custo e a localidade dela e o deixa "Ativo"; copiar o estado de um item entregue faria todo
    chip novo nascer no nome de quem recebeu os primeiros. Por isso, para o item que teve entrega, valem a origem da
    movimentação (centro de custo e localidade) e o status do primeiro registro do histórico; para o que nunca saiu, o
    estado atual. Nada disto cria vínculo com colaborador, movimentação nem termo.

        {"amostra": n, "nome", "marca", "modelo", "valor", "categoria", "fornecedor", "numero_nf", "numero_pedido",
         "data_compra", "centro_custo", "localidade", "status",            # o valor mais comum (None = sem valor)
         "consistencia": {campo: (valor, quantos_tem_esse_valor)}}
    Sem nenhum chip no estoque ainda, `amostra` é 0 e tudo vem vazio, com status Backup."""
    from collections import Counter

    itens = list(itens_de_chip())
    vazio = {campo: None for campo in CAMPOS_DO_PADRAO}
    if not itens:
        return {**vazio, "status": StatusItemChoices.BACKUP, "amostra": 0, "consistencia": {}}
    ids = [i.pk for i in itens]
    entregas: dict[int, MovimentacaoItem] = {}
    for mov in MovimentacaoItem.objects.filter(
        item_id__in=ids, tipo_movimentacao=TipoMovimentacaoChoices.TRANSFERENCIA,
        tipo_transferencia=TipoTransferenciaChoices.ENTREGA, revertida=False,
    ).order_by("item_id", "created_at", "pk"):
        entregas.setdefault(mov.item_id, mov)
    iniciais: dict[int, str] = {}
    for h in ItemStatusHistorico.objects.filter(item_id__in=ids).order_by("item_id", "pk"):
        iniciais.setdefault(h.item_id, h.status_novo)

    def estado_de_estoque(item: Item) -> dict:
        entrega = entregas.get(item.pk)
        if entrega is None:
            return {"centro_custo": item.centro_custo_id, "localidade": item.localidade_id, "status": item.status}
        return {
            "centro_custo": entrega.centro_custo_origem_id, "localidade": entrega.localidade_origem_id,
            "status": iniciais.get(item.pk) or StatusItemChoices.BACKUP,
        }

    contagens: dict[str, Counter] = {campo: Counter() for campo in CAMPOS_DO_PADRAO}
    for item in itens:
        valores = {
            "nome": item.nome, "marca": item.marca or None, "modelo": item.modelo or None, "valor": item.valor,
            "categoria": item.categoria_id, "fornecedor": item.fornecedor_id, "numero_nf": item.numero_nf or None,
            "numero_pedido": item.numero_pedido or None, "data_compra": item.data_compra, **estado_de_estoque(item),
        }
        for campo in CAMPOS_DO_PADRAO:
            contagens[campo][valores[campo]] += 1
    padrao = dict(vazio)
    consistencia = {}
    for campo, contador in contagens.items():
        valor, n = contador.most_common(1)[0]
        padrao[campo] = valor
        consistencia[campo] = (valor, n)
    if padrao["status"] not in STATUS_DE_CADASTRO:
        padrao["status"] = StatusItemChoices.BACKUP
    return {**padrao, "amostra": len(itens), "consistencia": consistencia}


def padroes_de_cadastro() -> dict:
    """O formulário "Cadastrar no estoque de TI" já vem preenchido com o `padrao_de_estoque` (texto vazio onde não há)."""
    padrao = padrao_de_estoque()
    return {
        campo: ("" if padrao[campo] is None else (padrao[campo].isoformat() if campo == "data_compra" else padrao[campo]))
        for campo in CAMPOS_DO_PADRAO
    }


STATUS_DE_CADASTRO = (
    StatusItemChoices.ATIVO, StatusItemChoices.BACKUP, StatusItemChoices.ESTOQUE, StatusItemChoices.PAUSADO,
)


def _limpar_dados_do_item(dados: dict) -> dict:
    erros = {}
    v = {}
    nome = " ".join(str(dados.get("nome") or "").split())
    if not nome:
        erros["nome"] = "Informe o nome do item (ex.: TIMCHIP)."
    elif len(nome) > 100:
        erros["nome"] = "O nome aceita até 100 caracteres."
    v["nome"] = nome
    for campo, limite, rotulo in (("marca", 100, "A marca"), ("modelo", 100, "O modelo")):
        valor = " ".join(str(dados.get(campo) or "").split())
        if len(valor) > limite:
            erros[campo] = f"{rotulo} aceita até {limite} caracteres."
        v[campo] = valor or None
    obs = str(dados.get("observacoes") or "").strip()
    if len(obs) > 500:
        erros["observacoes"] = "A observação aceita até 500 caracteres."
    v["observacoes"] = obs or None
    nf = " ".join(str(dados.get("numero_nf") or "").split())
    if len(nf) > 60:
        erros["numero_nf"] = "A NF aceita até 60 caracteres."
    v["numero_nf"] = nf or None
    pedido = " ".join(str(dados.get("numero_pedido") or "").split())
    if len(pedido) > 100:
        erros["numero_pedido"] = "O pedido aceita até 100 caracteres."
    v["numero_pedido"] = pedido or None
    bruto = str(dados.get("valor") or "").strip().replace("R$", "").replace(" ", "")
    if bruto:
        if "," in bruto:
            bruto = bruto.replace(".", "").replace(",", ".")
        try:
            valor = Decimal(bruto)
            if valor < 0 or valor >= Decimal("100000000"):
                raise InvalidOperation
            v["valor"] = valor.quantize(Decimal("0.01"))
        except InvalidOperation:
            erros["valor"] = "O valor deve ser um número maior ou igual a zero (ex.: 15,00)."
    else:
        v["valor"] = None
    data = dados.get("data_compra")
    if data in (None, ""):
        v["data_compra"] = None
    elif hasattr(data, "year"):
        v["data_compra"] = data
    else:
        from datetime import date
        try:
            v["data_compra"] = date.fromisoformat(str(data).strip())
        except ValueError:
            erros["data_compra"] = "Informe a data de compra como AAAA-MM-DD."
    if v.get("data_compra") and v["data_compra"] > timezone.localdate():
        erros["data_compra"] = "A data de compra não pode ser futura."

    def achar(modelo, campo, rotulo):
        pk = dados.get(campo)
        if pk in (None, ""):
            return None
        try:
            return modelo.objects.get(pk=int(getattr(pk, "pk", pk)))
        except (TypeError, ValueError, modelo.DoesNotExist):
            erros[campo] = f"Escolha {rotulo} válido."
            return None

    v["fornecedor"] = achar(Fornecedor, "fornecedor", "um fornecedor")
    v["centro_custo"] = achar(CentroCusto, "centro_custo", "um centro de custo")
    v["localidade"] = achar(Localidade, "localidade", "uma localidade")
    status = dados.get("status") or StatusItemChoices.BACKUP
    if status not in STATUS_DE_CADASTRO:
        erros["status"] = "Escolha o status do chip novo: Ativo, Backup, Estoque ou Pausado."
    v["status"] = status
    if erros:
        raise ValidationError(erros)
    return v


@dataclass
class ResumoCadastro:
    criados: list = field(default_factory=list)      # [(chip, item)]
    falhas: list = field(default_factory=list)       # [(chip, texto)]


_DO_TIPO = object()      # "a categoria é a do tipo do item" (o padrão); passe `None` ou um id para outra


def cadastrar_itens_dos_chips(
    chips, dados: dict, usuario=None, *, subtipo: Subtipo | None = None, categoria=_DO_TIPO,
) -> ResumoCadastro:
    """CRIA no estoque de TI o item (patrimônio) de cada chip que ainda não tem: `dados` é comum ao lote (nome, marca,
    modelo, valor, NF, pedido, data de compra, fornecedor, centro de custo, localidade, status) — chips de uma mesma nota
    fiscal chegam iguais (`observacoes` também entra). O número de série do item é o ICCID do chip; a categoria é a do tipo
    "chip", a não ser que `categoria` diga outra (`None` = sem categoria, como os chips que o TI já tem). Um item por transação:
    um chip com problema não derruba os outros. NUNCA altera item existente. Levanta `ValidationError` se os dados
    comuns são inválidos ou se o estoque não tem um tipo marcado como chip (ou tem vários e `subtipo` não foi escolhido)."""
    valores = _limpar_dados_do_item(dados)
    tipos = subtipos_de_chip()
    if subtipo is None:
        if not tipos:
            raise ValidationError("Antes de cadastrar, marque como “É chip (SIM)” o tipo dos chips, no cadastro de tipos.")
        if len(tipos) > 1:
            raise ValidationError({"subtipo": "Há mais de um tipo marcado como chip: escolha qual usar."})
        subtipo = tipos[0]
    elif not subtipo.e_chip:
        raise ValidationError({"subtipo": "Este tipo não está marcado como chip."})
    resumo = ResumoCadastro()
    for chip in chips:
        try:
            with transaction.atomic():
                chip = M2MChip.objects.get(pk=chip.pk)
                if chip.item_ti_id:
                    raise ValidationError("Este chip já tem item no estoque de TI.")
                if not chip.iccid:
                    raise ValidationError("O chip não tem ICCID: o número de série do item seria vazio.")
                chave = chave_iccid(chip.iccid)
                ligados = set(M2MChip.objects.filter(item_ti__isnull=False).values_list("item_ti_id", flat=True))
                for existente in itens_de_chip():
                    if existente.pk not in ligados and chave and chave_iccid(existente.numero_serie) == chave:
                        raise ValidationError(
                            f"Já existe o item “{existente.nome}” (nº {existente.numero_serie}) com este ICCID: ligue-o em vez de criar outro."
                        )
                item = Item(
                    **valores, numero_serie=chip.iccid, subtipo=subtipo,
                    categoria=subtipo.categoria if categoria is _DO_TIPO else (
                        Categoria.objects.filter(pk=getattr(categoria, "pk", categoria)).first() if categoria is not None else None
                    ),
                    item_consumo=SimNaoChoices.NAO, quantidade=1, tem_lote=False,
                    criado_por=_usuario(usuario), atualizado_por=_usuario(usuario),
                )
                item.full_clean()
                item.save()
                vincular(chip, item, usuario, origem="item_do_chip")
                resumo.criados.append((chip, item))
        except ValidationError as exc:
            resumo.falhas.append((chip, " ".join(exc.messages)))
    return resumo
