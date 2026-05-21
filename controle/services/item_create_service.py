from django.core.exceptions import ValidationError
from django.db import transaction

from ProjetoEstoque.models import ItemLote, SimNaoChoices


class ItemCreateService:
    @staticmethod
    def preencher_auditoria(obj, user, criando=True):
        if criando and hasattr(obj, "criado_por") and not getattr(obj, "criado_por_id", None):
            obj.criado_por = user

        if hasattr(obj, "atualizado_por"):
            obj.atualizado_por = user

    @classmethod
    @transaction.atomic
    def criar_item(cls, *, item_form, locacao_form, lote_form, user):
        item = item_form.save(commit=False)

        eh_locado = item.locado == SimNaoChoices.SIM
        eh_consumo = item.item_consumo == SimNaoChoices.SIM

        if eh_locado and eh_consumo:
            raise ValidationError("Item de consumo não pode ser cadastrado como locado.")

        cls.preencher_auditoria(item, user, criando=True)

        lote = None

        if eh_consumo:
            lote = lote_form.save(commit=False)
            cls.preencher_auditoria(lote, user, criando=True)

            lote.full_clean()
            lote.save()

            item.tem_lote = True
            item.quantidade = lote.quantidade
            item.valor = lote.custo_unitario
            item.fornecedor = lote.fornecedor
            item.numero_pedido = lote.numero_nf
            item.data_compra = lote.data_entrada

        else:
            item.tem_lote = False

        if eh_locado:
            item.data_compra = None
            item.numero_pedido = None

        if item.precisa_preventiva == SimNaoChoices.NAO:
            item.data_limite_preventiva = None

        item.full_clean()
        item.save()

        if eh_consumo and lote:
            item_lote = ItemLote(
                item=item,
                lote=lote,
                quantidade_entrada=lote.quantidade,
                quantidade_disponivel=lote.quantidade,
                custo_unitario=lote.custo_unitario,
            )

            cls.preencher_auditoria(item_lote, user, criando=True)
            item_lote.full_clean()
            item_lote.save()

        if eh_locado:
            locacao = locacao_form.save(commit=False)
            locacao.equipamento = item

            # Regra corrigida:
            # Fornecedor da locação usa o fornecedor principal do Item.
            locacao.fornecedor = item.fornecedor

            cls.preencher_auditoria(locacao, user, criando=True)

            locacao.full_clean()
            locacao.save()

        return item