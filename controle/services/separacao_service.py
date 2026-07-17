"""
SeparacaoService — área de estágio "Remessa para Envio" / "Remessa para
Devolução" (ver ProjetoEstoque.models.SeparacaoItem / LoteSeparacao).

Um item entra em remessa através de uma movimentação (`separacao_envio` ou
`separacao_devolucao`, ver MovimentacaoEstoqueService), pode ser agrupado em um
lote nomeado, e por fim é despachado — o que dispara a movimentação REAL
(`envio_manutencao` ou `devolucao_locacao`) através do
MovimentacaoEstoqueService, reaproveitando 100% das regras de negócio já
existentes (abertura de Ordem de Manutenção, mudança de status do item,
congelamento do período de locação, etc.). Este serviço nunca duplica essas
regras — ele só organiza o "antes" (estágio) e delega o "despacho de verdade".
"""
import logging

from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from ProjetoEstoque.models import (
    Locacao,
    LoteSeparacao,
    Notificacao,
    SeparacaoItem,
    StatusSeparacaoChoices,
    TipoSeparacaoChoices,
)

# Mesmo limiar usado em `avisos_contratos_vencer` (views/relatorios.py) — um
# único critério de "contrato perto de vencer" em todo o sistema.
_DIAS_ALERTA_CONTRATO = 60

logger = logging.getLogger(__name__)

_ICONES = {
    TipoSeparacaoChoices.ENVIO: "fa-box-archive",
    TipoSeparacaoChoices.DEVOLUCAO: "fa-box-open",
}


def _rev(name, args=None):
    try:
        return reverse(name, args=args)
    except Exception:
        return ""


def _notificar(*, titulo, mensagem, fornecedor, url_name, portal_url_name, url_args=None, icone="fa-box"):
    """Sino dual TI/Portal — mesmo padrão de services/ordem_manutencao_service.py.
    Nunca deve quebrar o fluxo principal de estágio/despacho."""
    try:
        Notificacao.objects.create(
            titulo=titulo,
            mensagem=mensagem,
            url=_rev(url_name, url_args),
            portal_url=_rev(portal_url_name),
            icone=icone,
            categoria="separacao",
            fornecedor=fornecedor,
        )
    except Exception:
        logger.exception("Falha ao criar notificação de separação")


class SeparacaoService:

    @staticmethod
    def estagiar(*, item, tipo, fornecedor, observacao, mov, user):
        """Cria (ou reabre) o registro de separação aberta do item. Chamado pelo
        MovimentacaoEstoqueService ao registrar uma movimentação `separacao_envio`
        ou `separacao_devolucao`."""
        if fornecedor is None:
            raise ValidationError("Informe o fornecedor de destino da remessa.")

        separacao, criado = SeparacaoItem.objects.update_or_create(
            item=item,
            status=StatusSeparacaoChoices.ABERTO,
            defaults={
                "tipo": tipo,
                "fornecedor": fornecedor,
                "observacoes": observacao or "",
                "movimentacao_entrada": mov,
                "atualizado_por": user,
            },
        )
        if criado:
            separacao.criado_por = user
            separacao.save(update_fields=["criado_por"])

        rotulo = "Envio" if tipo == TipoSeparacaoChoices.ENVIO else "Devolução"
        lista_interna = "separacao_envio_list" if tipo == TipoSeparacaoChoices.ENVIO else "separacao_devolucao_list"
        lista_portal = "portal_separacao_envio_list" if tipo == TipoSeparacaoChoices.ENVIO else "portal_separacao_devolucao_list"
        _notificar(
            titulo=f"Item em remessa para {rotulo}",
            mensagem=f"{item.nome} · {fornecedor.nome}",
            fornecedor=fornecedor,
            url_name=lista_interna,
            portal_url_name=lista_portal,
            icone=_ICONES.get(tipo, "fa-box"),
        )
        return separacao

    @staticmethod
    def _recalcular_status_lote(*, lote, user):
        """Reflete no lote o que já aconteceu com os itens: só passa a ENVIADO
        quando não sobra item ABERTO e pelo menos um já foi de fato despachado
        (nunca por só remover/cancelar tudo). Chamado após qualquer ação que
        mude o status de um item de um lote — despacho individual, despacho em
        lote, ou remoção — pra nunca deixar o lote preso em "Em separação"
        mesmo com tudo já resolvido."""
        if lote is None or lote.status != StatusSeparacaoChoices.ABERTO:
            return
        if lote.itens.filter(status=StatusSeparacaoChoices.ABERTO).exists():
            return
        if not lote.itens.filter(status=StatusSeparacaoChoices.ENVIADO).exists():
            return
        lote.status = StatusSeparacaoChoices.ENVIADO
        lote.enviado_em = timezone.now()
        lote.atualizado_por = user
        lote.save(update_fields=["status", "enviado_em", "atualizado_por", "updated_at"])

    @staticmethod
    def vincular_item(*, lote, separacao, user):
        """Move um item ainda solto (sem lote) para dentro de um lote aberto já
        existente — mesma validação de compatibilidade (tipo/fornecedor) de
        `criar_lote`, só que contra um lote que já existe."""
        if lote.status != StatusSeparacaoChoices.ABERTO:
            raise ValidationError("Este lote já foi despachado — não é mais possível adicionar itens.")
        if separacao.status != StatusSeparacaoChoices.ABERTO or separacao.lote_id:
            raise ValidationError(
                f'O item "{separacao.item.nome}" não está disponível para entrar em um lote.'
            )
        if separacao.tipo != lote.tipo:
            raise ValidationError(f'O item "{separacao.item.nome}" não é do mesmo tipo de remessa do lote.')
        if separacao.fornecedor_id != lote.fornecedor_id:
            raise ValidationError(f'O item "{separacao.item.nome}" é de um fornecedor diferente do lote.')

        separacao.lote = lote
        separacao.atualizado_por = user
        separacao.save(update_fields=["lote", "atualizado_por", "updated_at"])
        return separacao

    @staticmethod
    def desvincular_item(*, separacao, user):
        """Tira o item do lote sem cancelar a remessa — ele volta a ficar solto
        (arrependimento de agrupamento, diferente de `remover_item`, que cancela
        a remessa por completo)."""
        if not separacao.lote_id:
            raise ValidationError("Este item já não está em nenhum lote.")
        if separacao.lote.status != StatusSeparacaoChoices.ABERTO:
            raise ValidationError("Este lote já foi despachado — não é mais possível remover itens.")
        if separacao.status != StatusSeparacaoChoices.ABERTO:
            raise ValidationError(f'O item "{separacao.item.nome}" já não está mais em remessa aberta.')

        lote = separacao.lote
        separacao.lote = None
        separacao.atualizado_por = user
        separacao.save(update_fields=["lote", "atualizado_por", "updated_at"])
        SeparacaoService._recalcular_status_lote(lote=lote, user=user)
        return separacao

    @staticmethod
    def remover_item(*, separacao, user):
        """Tira o item da remessa sem despachar (arrependimento)."""
        if separacao.status != StatusSeparacaoChoices.ABERTO:
            raise ValidationError("Este item não está mais em remessa aberta.")

        separacao.status = StatusSeparacaoChoices.CANCELADO
        separacao.atualizado_por = user
        separacao.save(update_fields=["status", "atualizado_por", "updated_at"])
        if separacao.lote_id:
            SeparacaoService._recalcular_status_lote(lote=separacao.lote, user=user)
        return separacao

    @staticmethod
    def criar_lote(*, nome, tipo, fornecedor, separacoes, user):
        """Agrupa itens soltos (mesmo tipo/fornecedor, ainda abertos e sem lote)
        num LoteSeparacao nomeado."""
        if not separacoes:
            raise ValidationError("Selecione ao menos um item para criar o lote.")

        for sep in separacoes:
            if sep.status != StatusSeparacaoChoices.ABERTO or sep.lote_id:
                raise ValidationError(
                    f'O item "{sep.item.nome}" não está disponível para entrar em um novo lote.'
                )
            if sep.tipo != tipo:
                raise ValidationError(
                    f'O item "{sep.item.nome}" não é do mesmo tipo de remessa do lote.'
                )
            if sep.fornecedor_id != fornecedor.id:
                raise ValidationError(
                    f'O item "{sep.item.nome}" é de um fornecedor diferente do lote.'
                )

        lote = LoteSeparacao(nome=nome, tipo=tipo, fornecedor=fornecedor)
        lote.criado_por = user
        lote.atualizado_por = user
        lote.full_clean()
        lote.save()

        for sep in separacoes:
            sep.lote = lote
            sep.atualizado_por = user
            sep.save(update_fields=["lote", "atualizado_por", "updated_at"])

        return lote

    @staticmethod
    def excluir_lote(*, lote, user):
        """Desfaz o lote (os itens voltam a ficar soltos, ainda abertos) — não
        despacha nada. Só permitido enquanto o lote ainda não foi enviado."""
        if lote.status != StatusSeparacaoChoices.ABERTO:
            raise ValidationError("Só é possível desfazer um lote que ainda não foi despachado.")

        lote.itens.update(lote=None)
        lote.delete()

    @staticmethod
    def info_equipamento(separacao):
        """Resumo do equipamento (e, na Devolução, do contrato de Locação) para
        exibição no drawer de detalhe (TI) e no Portal do Fornecedor. Uma única
        fonte de dados usada pelos dois lados, para nunca divergir."""
        item = separacao.item
        info = {
            "nome": item.nome,
            "marca": item.marca or "",
            "modelo": item.modelo or "",
            "numero_serie": item.numero_serie or "",
            "status": item.get_status_display(),
            "status_slug": item.status,
            "categoria": str(item.categoria) if item.categoria_id else "",
            "subtipo": str(item.subtipo) if item.subtipo_id else "",
            "localidade": str(item.localidade) if item.localidade_id else "",
            "centro_custo": str(item.centro_custo) if item.centro_custo_id else "",
            "fornecedor_remessa": separacao.fornecedor.nome,
            "observacao": separacao.observacoes or "",
            "criado_em": separacao.created_at.strftime("%d/%m/%Y %H:%M"),
            "criado_por": (
                (separacao.criado_por.get_full_name() or separacao.criado_por.username)
                if separacao.criado_por_id else "—"
            ),
            "locacao": None,
        }
        if separacao.tipo == TipoSeparacaoChoices.DEVOLUCAO and str(item.locado) == "sim":
            try:
                loc = item.locacao
            except Locacao.DoesNotExist:
                loc = None
            if loc:
                venc = loc.data_vencimento
                info["locacao"] = {
                    "fornecedor": loc.fornecedor.nome if loc.fornecedor_id else "",
                    "data_entrada": loc.data_entrada.strftime("%d/%m/%Y") if loc.data_entrada else None,
                    "tempo_locado": loc.tempo_locado,
                    "valor_mensal": (
                        f"{loc.valor_mensal:.2f}".replace(".", ",") if loc.valor_mensal is not None else None
                    ),
                    "data_vencimento": venc.strftime("%d/%m/%Y") if venc else None,
                    "contrato_vencido": loc.contrato_vencido,
                }
        return info

    @staticmethod
    def badge_contrato(item):
        """Badge de urgência do contrato de Locação ('vencido há X dias' /
        'vence em X dias'), para as listagens de Remessa para Devolução. Usa o
        mesmo limiar de `avisos_contratos_vencer`. None quando não se aplica."""
        if str(item.locado) != "sim":
            return None
        try:
            loc = item.locacao
        except Locacao.DoesNotExist:
            return None
        venc = loc.data_vencimento
        if not venc:
            return None

        dias = (venc - timezone.localdate()).days
        if dias < 0:
            d = abs(dias)
            return {"label": f"Contrato vencido há {d} dia{'s' if d != 1 else ''}", "css": "danger"}
        if dias <= _DIAS_ALERTA_CONTRATO:
            return {"label": f"Contrato vence em {dias} dia{'s' if dias != 1 else ''}", "css": "warning"}
        return {"label": f"Contrato válido até {venc.strftime('%d/%m/%Y')}", "css": "ok"}

    @staticmethod
    def despachar_item(*, separacao, user):
        """Dispara a movimentação REAL (envio_manutencao ou devolucao_locacao)
        através do MovimentacaoEstoqueService, reaproveitando toda a validação e
        os efeitos colaterais já existentes."""
        from ProjetoEstoque.forms import MovimentacaoItemForm
        from services.movimentacao_service import MovimentacaoEstoqueService

        if separacao.status != StatusSeparacaoChoices.ABERTO:
            raise ValidationError(f'O item "{separacao.item.nome}" já não está mais em remessa aberta.')

        if separacao.tipo == TipoSeparacaoChoices.ENVIO:
            tipo_mov = "envio_manutencao"
            observacao = separacao.observacoes or (
                f"Despacho da remessa para envio ao fornecedor {separacao.fornecedor.nome}."
            )
        else:
            tipo_mov = "devolucao_locacao"
            observacao = separacao.observacoes or (
                f"Devolução de locação ao fornecedor {separacao.fornecedor.nome}."
            )

        form = MovimentacaoItemForm(data={
            "tipo_movimentacao": tipo_mov,
            "item": separacao.item_id,
            "fornecedor_manutencao": separacao.fornecedor_id,
            "observacao": observacao,
        })
        if not form.is_valid():
            erros = "; ".join(
                f"{campo}: {', '.join(msgs)}" for campo, msgs in form.errors.items()
            )
            raise ValidationError(f'Não foi possível despachar "{separacao.item.nome}": {erros}')

        mov = MovimentacaoEstoqueService.registrar(form=form, user=user)

        separacao.status = StatusSeparacaoChoices.ENVIADO
        separacao.enviado_em = timezone.now()
        separacao.movimentacao_despacho = mov
        separacao.atualizado_por = user
        separacao.save(update_fields=[
            "status", "enviado_em", "movimentacao_despacho", "atualizado_por", "updated_at",
        ])
        if separacao.lote_id:
            SeparacaoService._recalcular_status_lote(lote=separacao.lote, user=user)

        rotulo = "envio" if separacao.tipo == TipoSeparacaoChoices.ENVIO else "devolução"
        lista_portal = (
            "portal_separacao_envio_list"
            if separacao.tipo == TipoSeparacaoChoices.ENVIO
            else "portal_separacao_devolucao_list"
        )
        _notificar(
            titulo=f"Despacho de {rotulo} realizado",
            mensagem=f"{separacao.item.nome} · {separacao.fornecedor.nome}",
            fornecedor=separacao.fornecedor,
            url_name="movimentacao_detail",
            url_args=[mov.pk],
            portal_url_name=lista_portal,
            icone=_ICONES.get(separacao.tipo, "fa-box"),
        )
        return mov

    @classmethod
    @transaction.atomic
    def despachar_lote(cls, *, lote, user):
        """Despacha todos os itens ainda abertos do lote, um a um, na mesma
        transação. Marca o lote como enviado quando não sobrar item aberto."""
        if lote.status != StatusSeparacaoChoices.ABERTO:
            raise ValidationError("Este lote já foi despachado.")

        abertos = list(
            lote.itens.select_for_update().filter(status=StatusSeparacaoChoices.ABERTO)
        )
        if not abertos:
            raise ValidationError("Não há itens abertos neste lote para despachar.")

        for sep in abertos:
            cls.despachar_item(separacao=sep, user=user)
        # `despachar_item` já chama `_recalcular_status_lote` a cada item —
        # mantido aqui como no-op de segurança (idempotente) caso a lista de
        # itens do lote mude no meio da transação.
        cls._recalcular_status_lote(lote=lote, user=user)
