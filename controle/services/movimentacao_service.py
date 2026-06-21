import logging
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from ProjetoEstoque.models import (
    Item,
    ItemLote,
    LoteEstoque,
    MovimentacaoItem,
    StatusItemChoices,
)

logger = logging.getLogger(__name__)


class MovimentacaoEstoqueService:
    ENTRADA = "entrada"
    BAIXA = "baixa"
    TRANSFERENCIA = "transferencia"
    TRANSFERENCIA_EQUIPAMENTO = "transferencia_equipamento"
    ENVIO_MANUTENCAO = "envio_manutencao"
    RETORNO_MANUTENCAO = "retorno_manutencao"
    RETORNO = "retorno"

    @staticmethod
    def preencher_auditoria(obj, user, criando=True):
        if criando and hasattr(obj, "criado_por") and not getattr(obj, "criado_por_id", None):
            obj.criado_por = user

        if hasattr(obj, "atualizado_por"):
            obj.atualizado_por = user

    @classmethod
    @transaction.atomic
    def registrar(cls, *, form, user):
        tipo = form.cleaned_data["tipo_movimentacao"]

        if tipo == cls.ENTRADA:
            return cls._registrar_entrada(form=form, user=user)

        if tipo == cls.BAIXA:
            return cls._registrar_baixa(form=form, user=user)

        return cls._registrar_movimentacao_padrao(form=form, user=user)

    @classmethod
    def _registrar_entrada(cls, *, form, user):
        item = (
            Item.objects
            .select_for_update()
            .get(pk=form.cleaned_data["item"].pk)
        )

        fornecedor = form.cleaned_data["lote_fornecedor"]
        data_entrada = form.cleaned_data["lote_data_entrada"]
        numero_nf = form.cleaned_data["lote_numero_nf"]
        quantidade = form.cleaned_data["lote_quantidade"]
        custo_unitario = form.cleaned_data["lote_custo_unitario"]
        observacao_lote = form.cleaned_data.get("lote_observacao_tecnica")

        localidade_destino = form.cleaned_data["localidade_destino"]
        centro_custo_destino = form.cleaned_data["centro_custo_destino"]
        observacao = form.cleaned_data.get("observacao")

        lote = LoteEstoque(
            fornecedor=fornecedor,
            data_entrada=data_entrada,
            numero_nf=numero_nf,
            quantidade=quantidade,
            custo_unitario=custo_unitario,
            observacao_tecnica=observacao_lote,
        )

        cls.preencher_auditoria(lote, user, criando=True)
        lote.full_clean()
        lote.save()

        item_lote = ItemLote(
            item=item,
            lote=lote,
            quantidade_entrada=quantidade,
            quantidade_disponivel=quantidade,
            custo_unitario=custo_unitario,
        )

        cls.preencher_auditoria(item_lote, user, criando=True)
        item_lote.full_clean()
        item_lote.save()

        custo_total = Decimal(quantidade) * custo_unitario

        mov = MovimentacaoItem(
            tipo_movimentacao=cls.ENTRADA,
            item=item,
            lote=lote,
            quantidade=quantidade,
            localidade_origem=item.localidade,
            centro_custo_origem=item.centro_custo,
            localidade_destino=localidade_destino,
            centro_custo_destino=centro_custo_destino,
            numero_pedido=numero_nf,
            observacao=observacao,
            custo=custo_total,
        )

        cls.preencher_auditoria(mov, user, criando=True)
        mov.full_clean()
        mov.save()

        item.tem_lote = True
        item.quantidade = (item.quantidade or 0) + quantidade
        item.valor = custo_unitario
        item.fornecedor = fornecedor
        item.numero_pedido = numero_nf
        item.data_compra = data_entrada
        item.localidade = localidade_destino
        item.centro_custo = centro_custo_destino

        cls.preencher_auditoria(item, user, criando=False)
        item.full_clean()
        item.save(update_fields=[
            "tem_lote",
            "quantidade",
            "valor",
            "fornecedor",
            "numero_pedido",
            "data_compra",
            "localidade",
            "centro_custo",
            "atualizado_por",
        ] if hasattr(item, "atualizado_por") else [
            "tem_lote",
            "quantidade",
            "valor",
            "fornecedor",
            "numero_pedido",
            "data_compra",
            "localidade",
            "centro_custo",
        ])

        return mov

    @classmethod
    def _registrar_baixa(cls, *, form, user):
        item = (
            Item.objects
            .select_for_update()
            .get(pk=form.cleaned_data["item"].pk)
        )

        lote = form.cleaned_data["lote"]
        quantidade = form.cleaned_data["quantidade"]

        item_lote = (
            ItemLote.objects
            .select_for_update()
            .filter(item=item, lote=lote)
            .first()
        )

        if not item_lote:
            raise ValidationError("O lote selecionado não pertence ao item informado.")

        if item_lote.quantidade_disponivel < quantidade:
            raise ValidationError(
                f"Saldo insuficiente no lote. Disponível: {item_lote.quantidade_disponivel}."
            )

        if (item.quantidade or 0) < quantidade:
            raise ValidationError(
                f"Saldo insuficiente no item. Disponível: {item.quantidade or 0}."
            )

        custo_unitario = item_lote.custo_unitario or Decimal("0.00")
        custo_total = Decimal(quantidade) * custo_unitario

        mov = form.save(commit=False)
        mov.item = item
        mov.lote = lote
        mov.quantidade = quantidade
        mov.localidade_origem = item.localidade
        mov.centro_custo_origem = item.centro_custo
        mov.custo = custo_total

        cls.preencher_auditoria(mov, user, criando=True)
        mov.full_clean()
        mov.save()

        item_lote.quantidade_disponivel -= quantidade
        cls.preencher_auditoria(item_lote, user, criando=False)
        item_lote.full_clean()
        item_lote.save()

        item.quantidade = max(0, (item.quantidade or 0) - quantidade)
        cls.preencher_auditoria(item, user, criando=False)
        item.save(update_fields=[
            "quantidade",
            "atualizado_por",
        ] if hasattr(item, "atualizado_por") else [
            "quantidade",
        ])

        # E-mails de baixa: (1) foco em estoque, (2) dados da movimentação
        _mov_ref = mov
        _qtd_restante = item.quantidade

        def _enviar_email_baixa():
            try:
                from services.email_alertas import alerta_baixa_estoque, alerta_movimentacao
                alerta_baixa_estoque(_mov_ref, qtd_restante=_qtd_restante)
                alerta_movimentacao(_mov_ref)
            except Exception as exc:
                logger.warning("email baixa: falha ao enviar: %s", exc)

        transaction.on_commit(_enviar_email_baixa)

        return mov

    @classmethod
    def _registrar_movimentacao_padrao(cls, *, form, user):
        mov = form.save(commit=False)

        item = (
            Item.objects
            .select_for_update()
            .get(pk=mov.item_id)
        )

        mov.item = item
        mov.localidade_origem = item.localidade
        mov.centro_custo_origem = item.centro_custo

        if mov.tipo_movimentacao in {
            cls.ENVIO_MANUTENCAO,
            cls.RETORNO_MANUTENCAO,
            cls.RETORNO,
        }:
            mov.usuario = None

        if (
            mov.tipo_movimentacao == cls.TRANSFERENCIA
            and mov.tipo_transferencia == "entrega"
            and not mov.centro_custo_destino
            and mov.usuario
            and mov.usuario.centro_custo
        ):
            mov.centro_custo_destino = mov.usuario.centro_custo

        _devolucao_restore_cc = False
        _restore_cc = None

        if (
            mov.tipo_movimentacao == cls.TRANSFERENCIA
            and mov.tipo_transferencia == "devolucao"
        ):
            ultima_entrega = (
                MovimentacaoItem.objects
                .filter(
                    item=item,
                    tipo_movimentacao=cls.TRANSFERENCIA,
                    tipo_transferencia="entrega",
                )
                .order_by("-created_at")
                .first()
            )

            if ultima_entrega is not None:
                _devolucao_restore_cc = True
                _restore_cc = ultima_entrega.centro_custo_origem
                mov.centro_custo_destino = ultima_entrega.centro_custo_origem

        cls.preencher_auditoria(mov, user, criando=True)
        mov.full_clean()
        mov.save()

        update_fields = []

        if mov.tipo_movimentacao == cls.ENVIO_MANUTENCAO:
            # Enviar para manutenção NÃO dá baixa na quantidade: o equipamento
            # continua sendo o mesmo ativo, apenas muda de status para "Manutenção".
            item.status = StatusItemChoices.MANUTENCAO
            update_fields.append("status")

        elif mov.tipo_movimentacao in {cls.RETORNO_MANUTENCAO, cls.RETORNO}:
            # Retorno de manutenção mantém a quantidade inalterada (espelha o envio):
            # apenas atualiza o status e, se informada, a localidade de destino.
            item.status = mov.status_retorno or StatusItemChoices.BACKUP
            update_fields.append("status")

            if mov.localidade_destino:
                item.localidade = mov.localidade_destino
                update_fields.append("localidade")

        elif mov.tipo_movimentacao == cls.TRANSFERENCIA:
            if mov.localidade_destino:
                item.localidade = mov.localidade_destino
                update_fields.append("localidade")

            if _devolucao_restore_cc:
                # Restaura o CC original do item (pode ser None se não tinha CC antes da entrega)
                item.centro_custo = _restore_cc
                update_fields.append("centro_custo")
            elif mov.centro_custo_destino:
                item.centro_custo = mov.centro_custo_destino
                update_fields.append("centro_custo")

            if mov.tipo_transferencia == "entrega" and item.status == StatusItemChoices.BACKUP:
                item.status = StatusItemChoices.ATIVO
                update_fields.append("status")

            elif mov.tipo_transferencia == "devolucao" and item.status == StatusItemChoices.ATIVO:
                item.status = StatusItemChoices.BACKUP
                update_fields.append("status")

        elif mov.tipo_movimentacao == cls.TRANSFERENCIA_EQUIPAMENTO:
            if mov.localidade_destino:
                item.localidade = mov.localidade_destino
                update_fields.append("localidade")

            if mov.centro_custo_destino:
                item.centro_custo = mov.centro_custo_destino
                update_fields.append("centro_custo")

            if mov.status_transferencia:
                item.status = mov.status_transferencia
                update_fields.append("status")

            # Renomear o equipamento direto na transferência (opcional).
            # Só renomeia quando o campo veio preenchido e é diferente do atual;
            # registra a alteração na observação da movimentação (auditoria).
            novo_nome = (form.cleaned_data.get("novo_nome") or "").strip()
            nome_atual = (item.nome or "").strip()
            if novo_nome and novo_nome != nome_atual:
                item.nome = novo_nome
                update_fields.append("nome")
                nota = f'Renomeado: "{nome_atual}" → "{novo_nome}".'
                mov.observacao = f"{mov.observacao}\n{nota}".strip() if mov.observacao else nota
                mov.save(update_fields=["observacao", "updated_at"])

        if update_fields:
            cls.preencher_auditoria(item, user, criando=False)

            if hasattr(item, "atualizado_por"):
                update_fields.append("atualizado_por")

            item.save(update_fields=list(set(update_fields)))

        # E-mail ao entregar ou devolver item (roda após commit da transação)
        if (
            mov.tipo_movimentacao == cls.TRANSFERENCIA
            and getattr(mov, "tipo_transferencia", None) in ("entrega", "devolucao")
        ):
            _mov_ref = mov

            def _enviar_email_movimentacao():
                try:
                    from services.email_alertas import alerta_movimentacao
                    alerta_movimentacao(_mov_ref)
                except Exception as exc:
                    logger.warning("email movimentacao: falha ao enviar: %s", exc)

            transaction.on_commit(_enviar_email_movimentacao)

        return mov