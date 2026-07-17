"""
LoteEnvioFornecedorService — "carrinho" que o próprio fornecedor monta no Portal
para organizar o envio de trocas antecipadas e cadastro de equipamento novo ao TI.

Direção oposta a `SeparacaoService`/`LoteSeparacao` (que só o TI cria, TI→fornecedor):
aqui é o FORNECEDOR quem monta o lote (fornecedor→TI). Enquanto um item está em
RASCUNHO é só dado bruto de formulário — nenhuma OrdemManutencao/Item real existe.
Só no envio (`enviar_item`/`enviar_lote`) a linha vira, de fato, uma troca antecipada
real (via `OrdemManutencaoService.abrir_troca_antecipada`) ou um `Item` novo
(PAUSADO, mesmo precedente do substituto de troca antecipada).
"""
import logging
from datetime import date as _date
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from ProjetoEstoque.models import (
    Item,
    Locacao,
    LoteEnvioFornecedor,
    LoteEnvioFornecedorAnexo,
    LoteEnvioFornecedorItem,
    MovimentacaoItem,
    OrdemManutencao,
    SimNaoChoices,
    StatusItemChoices,
    StatusItemLoteEnvioChoices,
    StatusLoteEnvioFornecedorChoices,
    StatusOrdemManutencaoChoices,
    TipoItemLoteEnvioChoices,
    TipoMovimentacaoChoices,
)

logger = logging.getLogger(__name__)
S_LOTE = StatusLoteEnvioFornecedorChoices
S_ITEM = StatusItemLoteEnvioChoices
S_OS = StatusOrdemManutencaoChoices


class LoteEnvioFornecedorService:

    # ── Auditoria / parsers (mesmo padrão de OrdemManutencaoService) ───────
    @staticmethod
    def _audit(obj, user, criando=True):
        if criando and hasattr(obj, "criado_por") and not getattr(obj, "criado_por_id", None):
            obj.criado_por = user
        if hasattr(obj, "atualizado_por"):
            obj.atualizado_por = user

    @staticmethod
    def _parse_valor(v):
        if isinstance(v, Decimal):
            return v
        s = (v or "").strip().replace(" ", "")
        if not s:
            return None
        if "," in s and "." in s:
            s = s.replace(".", "").replace(",", ".")
        elif "," in s:
            s = s.replace(",", ".")
        try:
            return Decimal(s)
        except (InvalidOperation, ValueError):
            raise ValidationError("Valor informado inválido.")

    @staticmethod
    def _parse_data(v):
        if isinstance(v, _date):
            return v
        s = (v or "").strip()
        if not s:
            return None
        try:
            return _date.fromisoformat(s)
        except ValueError:
            try:
                d, m, a = s.split("/")
                return _date(int(a), int(m), int(d))
            except Exception:
                raise ValidationError("Data informada inválida.")

    @staticmethod
    def _validar_numero_serie_disponivel(numero_serie, *, excluir_item_lote_id=None):
        """Impede o fornecedor de cadastrar (ou editar para) um número de série
        que já existe no sistema — seja como `Item` real (equipamento já
        materializado, qualquer status) ou como rascunho de outro item de
        equipamento novo ainda não enviado (o `Item` só é criado em
        `enviar_item`, então dois rascunhos duplicados passariam despercebidos
        até os dois serem enviados). Vazio/branco não é checado — número de
        série é opcional para equipamentos genéricos."""
        serie = (numero_serie or "").strip()
        if not serie:
            return
        if Item.objects.filter(numero_serie__iexact=serie).exists():
            raise ValidationError(
                f'Já existe um equipamento cadastrado no sistema com o número de série "{serie}". '
                "Verifique se não é o mesmo equipamento antes de continuar."
            )
        rascunhos = LoteEnvioFornecedorItem.objects.filter(
            tipo=TipoItemLoteEnvioChoices.EQUIPAMENTO_NOVO,
            status=StatusItemLoteEnvioChoices.RASCUNHO,
            novo_numero_serie__iexact=serie,
        )
        if excluir_item_lote_id:
            rascunhos = rascunhos.exclude(pk=excluir_item_lote_id)
        if rascunhos.exists():
            raise ValidationError(
                f'Já existe outro equipamento em rascunho aguardando envio com o número de série "{serie}".'
            )

    @staticmethod
    def _parse_meses(v):
        s = (v or "").strip() if not isinstance(v, int) else str(v)
        if not s:
            return None
        try:
            meses = int(s)
        except (TypeError, ValueError):
            raise ValidationError("Tempo de contrato em meses inválido.")
        if meses <= 0:
            raise ValidationError("O tempo de contrato deve ser maior que zero.")
        return meses

    # ── Consulta: badge "troca antecipada em andamento" ─────────────────────
    _OS_ABERTAS_EXCLUI = [S_OS.CONCLUIDO, S_OS.CANCELADO, S_OS.DESCARTADO]

    @classmethod
    def ordem_troca_aberta(cls, item):
        """A OS de troca antecipada aberta (não-terminal) deste item, ou None."""
        return (
            OrdemManutencao.objects
            .filter(item=item, troca_antecipada=True)
            .exclude(status__in=cls._OS_ABERTAS_EXCLUI)
            .order_by("-created_at")
            .first()
        )

    @classmethod
    def item_tem_troca_pendente(cls, item) -> bool:
        """True se o item já tem uma troca antecipada em andamento (OS aberta) OU
        um rascunho aberto no lote de envio — usado para o badge visual e para
        impedir o fornecedor de duplicar a troca por engano."""
        if cls.ordem_troca_aberta(item) is not None:
            return True
        return LoteEnvioFornecedorItem.objects.filter(
            item_defeituoso=item, status=S_ITEM.RASCUNHO,
        ).exists()

    @classmethod
    def itens_com_troca_pendente_ids(cls, itens) -> set:
        """Versão em lote de `item_tem_troca_pendente`, para não gerar N+1 em listas."""
        ids = [i.pk for i in itens]
        if not ids:
            return set()
        aberta_ids = set(
            OrdemManutencao.objects.filter(
                item_id__in=ids, troca_antecipada=True,
            ).exclude(status__in=cls._OS_ABERTAS_EXCLUI).values_list("item_id", flat=True)
        )
        rascunho_ids = set(
            LoteEnvioFornecedorItem.objects.filter(
                item_defeituoso_id__in=ids, status=S_ITEM.RASCUNHO,
            ).values_list("item_defeituoso_id", flat=True)
        )
        return aberta_ids | rascunho_ids

    # ── Consulta: elegibilidade p/ retorno de reparo concluído ──────────────
    _STATUS_ELEGIVEIS_RETORNO = [S_OS.REPARADO, S_OS.REPROVADO]

    @classmethod
    def rascunho_retorno_ativo(cls, ordem):
        """O rascunho de retorno (REPARO_CONCLUIDO) já aberto para esta OS, se
        houver — usado tanto para bloquear duplicidade quanto para linkar o
        fornecedor direto ao lote onde o item já está separado."""
        return LoteEnvioFornecedorItem.objects.filter(
            ordem=ordem, status=S_ITEM.RASCUNHO, tipo=TipoItemLoteEnvioChoices.REPARO_CONCLUIDO,
        ).select_related("lote").first()

    @classmethod
    def ordem_elegivel_para_retorno(cls, ordem) -> bool:
        """True se a OS já concluiu (ou teve reprovado) o reparo e ainda não
        tem um rascunho de retorno em aberto no Lote de Envio."""
        if ordem.status not in cls._STATUS_ELEGIVEIS_RETORNO:
            return False
        return cls.rascunho_retorno_ativo(ordem) is None

    @classmethod
    def itens_aguardando_devolucao_ids(cls, itens) -> set:
        """Itens cujo substituto já foi recebido pelo TI mas o defeituoso ainda não
        foi enviado de volta ao fornecedor — precisam do aviso "Devolver ao
        fornecedor". Calculado em lote para não gerar N+1 em listas."""
        ids = [i.pk for i in itens]
        if not ids:
            return set()
        return set(
            OrdemManutencao.objects.filter(
                item_id__in=ids, troca_antecipada=True,
                status=S_OS.TROCA_ANT_SUBSTITUTO_RECEBIDO,
            ).values_list("item_id", flat=True)
        )

    # ── Lote (carrinho) ──────────────────────────────────────────────────────
    @classmethod
    def lotes_abertos(cls, fornecedor):
        """Todos os lotes ABERTOS do fornecedor (pode ter vários simultâneos —
        ex.: remessas físicas separadas), mais recente primeiro."""
        return LoteEnvioFornecedor.objects.filter(
            fornecedor=fornecedor, status=S_LOTE.ABERTO,
        ).order_by("-created_at")

    @classmethod
    @transaction.atomic
    def criar_lote(cls, *, fornecedor, user, nome=None):
        nome = (nome or "").strip() or f"Lote {timezone.localdate():%d/%m/%Y}"
        lote = LoteEnvioFornecedor(nome=nome, fornecedor=fornecedor, status=S_LOTE.ABERTO)
        cls._audit(lote, user, criando=True)
        lote.save()
        return lote

    @classmethod
    @transaction.atomic
    def resolver_lote(cls, *, fornecedor, user, lote_id=None, nome_novo=None):
        """Resolve em qual lote ABERTO um item novo deve entrar: um lote
        específico (`lote_id`), um lote novo nomeado (`nome_novo`), ou — se nada
        foi informado — reaproveita o lote aberto mais recente do fornecedor
        (criando um se não houver nenhum, para não quebrar chamadas antigas)."""
        if lote_id:
            lote = (
                LoteEnvioFornecedor.objects
                .select_for_update()
                .filter(pk=lote_id, fornecedor=fornecedor, status=S_LOTE.ABERTO)
                .first()
            )
            if not lote:
                raise ValidationError("Lote de envio inválido ou já enviado ao TI.")
            return lote
        if (nome_novo or "").strip():
            return cls.criar_lote(fornecedor=fornecedor, user=user, nome=nome_novo)
        existente = cls.lotes_abertos(fornecedor).first()
        if existente:
            return existente
        return cls.criar_lote(fornecedor=fornecedor, user=user)

    @classmethod
    def renomear_lote(cls, *, lote, user, nome):
        nome = (nome or "").strip()
        if not nome:
            raise ValidationError("Informe um nome para o lote.")
        lote.nome = nome
        cls._audit(lote, user, criando=False)
        lote.save(update_fields=["nome", "atualizado_por", "updated_at"])
        return lote

    @classmethod
    def excluir_lote(cls, *, lote, user):
        if lote.status != S_LOTE.ABERTO:
            raise ValidationError(
                "Só é possível excluir um lote enquanto ele está em organização "
                "(nada foi enviado ainda)."
            )
        lote.delete()

    # ── Itens do lote — criação (rascunho) ───────────────────────────────────
    @classmethod
    def adicionar_item_troca_antecipada(cls, *, fornecedor, user, item_defeituoso,
                                        sub_modelo, sub_serie="", sub_marca="",
                                        sub_data_contrato=None, lote_id=None, lote_nome_novo=None):
        if item_defeituoso.status != StatusItemChoices.DEFEITO:
            raise ValidationError(
                "A troca antecipada só é permitida para equipamentos com status Defeito."
            )
        if not (sub_modelo or "").strip():
            raise ValidationError("Informe o modelo do equipamento substituto.")
        if cls.item_tem_troca_pendente(item_defeituoso):
            raise ValidationError(
                "Já existe uma troca antecipada em andamento (ou em rascunho) para este equipamento."
            )

        lote = cls.resolver_lote(
            fornecedor=fornecedor, user=user, lote_id=lote_id, nome_novo=lote_nome_novo,
        )
        item_lote = LoteEnvioFornecedorItem(
            lote=lote,
            tipo=TipoItemLoteEnvioChoices.TROCA_ANTECIPADA,
            status=S_ITEM.RASCUNHO,
            item_defeituoso=item_defeituoso,
            sub_modelo=(sub_modelo or "").strip(),
            sub_serie=(sub_serie or "").strip(),
            sub_marca=(sub_marca or "").strip(),
            sub_data_contrato=cls._parse_data(sub_data_contrato),
        )
        cls._audit(item_lote, user, criando=True)
        item_lote.save()
        return item_lote

    @classmethod
    def adicionar_item_equipamento_novo(cls, *, fornecedor, user, novo_nome, novo_numero_serie="",
                                        novo_marca="", novo_modelo="", novo_categoria=None,
                                        novo_subtipo=None, novo_localidade=None, novo_centro_custo=None,
                                        novo_locado="nao", novo_pmb="nao", novo_valor=None, novo_contrato="",
                                        novo_tempo_contrato_meses=None, novo_cobranca_proximo_ano="nao",
                                        lote_id=None, lote_nome_novo=None):
        if not (novo_nome or "").strip():
            raise ValidationError("Informe o nome/modelo do equipamento.")
        if not (novo_categoria and novo_subtipo and novo_localidade):
            raise ValidationError("Informe categoria, subtipo e localidade do equipamento.")
        cls._validar_numero_serie_disponivel(novo_numero_serie)

        lote = cls.resolver_lote(
            fornecedor=fornecedor, user=user, lote_id=lote_id, nome_novo=lote_nome_novo,
        )
        locado = SimNaoChoices.SIM if novo_locado == "sim" else SimNaoChoices.NAO
        pmb = SimNaoChoices.SIM if novo_pmb == "sim" else SimNaoChoices.NAO
        valor = cls._parse_valor(novo_valor)
        if valor is None:
            raise ValidationError("Informe o valor do equipamento.")
        tempo_meses = cls._parse_meses(novo_tempo_contrato_meses) if locado == SimNaoChoices.SIM else None
        if locado == SimNaoChoices.SIM and tempo_meses is None:
            raise ValidationError("Informe o tempo do contrato de locação em meses.")
        cobranca_proximo_ano = (
            SimNaoChoices.SIM if (locado == SimNaoChoices.SIM and novo_cobranca_proximo_ano == "sim")
            else SimNaoChoices.NAO
        )

        item_lote = LoteEnvioFornecedorItem(
            lote=lote,
            tipo=TipoItemLoteEnvioChoices.EQUIPAMENTO_NOVO,
            status=S_ITEM.RASCUNHO,
            novo_nome=(novo_nome or "").strip(),
            novo_numero_serie=(novo_numero_serie or "").strip(),
            novo_marca=(novo_marca or "").strip(),
            novo_modelo=(novo_modelo or "").strip(),
            novo_categoria=novo_categoria,
            novo_subtipo=novo_subtipo,
            novo_localidade=novo_localidade,
            novo_centro_custo=novo_centro_custo,
            novo_locado=locado,
            novo_pmb=pmb,
            novo_valor=valor,
            novo_contrato=(novo_contrato or "").strip(),
            novo_tempo_contrato_meses=tempo_meses,
            novo_cobranca_proximo_ano=cobranca_proximo_ano,
        )
        cls._audit(item_lote, user, criando=True)
        item_lote.save()
        return item_lote

    @classmethod
    def adicionar_item_reparo_concluido(cls, *, fornecedor, user, ordem, localidade_devolucao_id,
                                        valor_avaliacao_tecnica=None, lote_id=None, lote_nome_novo=None):
        """Separa uma OS de reparo normal (já REPARADO/REPROVADO) para retorno
        físico ao TI através do Lote de Envio — mesmo mecanismo já usado para
        troca antecipada/equipamento novo (NF, múltiplos itens, envio em lote).
        Só cria o rascunho: a OS só transiciona para DEVOLVIDO no envio de
        verdade (ver `enviar_item`), nunca aqui."""
        if ordem.fornecedor_id != fornecedor.id:
            raise ValidationError("Esta Ordem de Manutenção não pertence a este fornecedor.")
        if not cls.ordem_elegivel_para_retorno(ordem):
            raise ValidationError(
                "Esta Ordem de Manutenção não está disponível para separação (já "
                "separada em outro lote ou fora do status Reparado/Reprovado)."
            )
        if not localidade_devolucao_id:
            raise ValidationError("Informe a localidade de destino da devolução.")

        # Reprovado (sem reparo) exige o valor da avaliação técnica — mesma regra
        # já validada em `_on_devolvido` (ordem_manutencao_service.py), aqui
        # antecipada pra não deixar o fornecedor criar um rascunho inválido.
        avaliacao = None
        if ordem.status == S_OS.REPROVADO:
            avaliacao = cls._parse_valor(valor_avaliacao_tecnica)
            if avaliacao is None:
                raise ValidationError("Informe o valor da avaliação técnica.")

        lote = cls.resolver_lote(
            fornecedor=fornecedor, user=user, lote_id=lote_id, nome_novo=lote_nome_novo,
        )
        item_lote = LoteEnvioFornecedorItem(
            lote=lote,
            tipo=TipoItemLoteEnvioChoices.REPARO_CONCLUIDO,
            status=S_ITEM.RASCUNHO,
            ordem=ordem,
            localidade_devolucao_id=localidade_devolucao_id,
            valor_avaliacao_tecnica=avaliacao,
        )
        cls._audit(item_lote, user, criando=True)
        item_lote.save()
        return item_lote

    # ── Itens do lote — edição/exclusão (só enquanto rascunho) ───────────────
    @staticmethod
    def _garantir_rascunho(item_lote):
        if item_lote.status != S_ITEM.RASCUNHO:
            raise ValidationError("Este item já foi enviado e não pode mais ser editado/excluído.")

    @classmethod
    def editar_item_troca_antecipada(cls, *, item_lote, user, sub_modelo, sub_serie="",
                                     sub_marca="", sub_data_contrato=None):
        cls._garantir_rascunho(item_lote)
        if not (sub_modelo or "").strip():
            raise ValidationError("Informe o modelo do equipamento substituto.")
        item_lote.sub_modelo = (sub_modelo or "").strip()
        item_lote.sub_serie = (sub_serie or "").strip()
        item_lote.sub_marca = (sub_marca or "").strip()
        item_lote.sub_data_contrato = cls._parse_data(sub_data_contrato)
        cls._audit(item_lote, user, criando=False)
        item_lote.save()
        return item_lote

    @classmethod
    def editar_item_equipamento_novo(cls, *, item_lote, user, novo_nome, novo_numero_serie="",
                                     novo_marca="", novo_modelo="", novo_categoria=None,
                                     novo_subtipo=None, novo_localidade=None, novo_centro_custo=None,
                                     novo_locado="nao", novo_pmb="nao", novo_valor=None, novo_contrato="",
                                     novo_tempo_contrato_meses=None, novo_cobranca_proximo_ano="nao"):
        cls._garantir_rascunho(item_lote)
        if not (novo_nome or "").strip():
            raise ValidationError("Informe o nome/modelo do equipamento.")
        if not (novo_categoria and novo_subtipo and novo_localidade):
            raise ValidationError("Informe categoria, subtipo e localidade do equipamento.")
        cls._validar_numero_serie_disponivel(novo_numero_serie, excluir_item_lote_id=item_lote.pk)
        locado = SimNaoChoices.SIM if novo_locado == "sim" else SimNaoChoices.NAO
        pmb = SimNaoChoices.SIM if novo_pmb == "sim" else SimNaoChoices.NAO
        valor = cls._parse_valor(novo_valor)
        if valor is None:
            raise ValidationError("Informe o valor do equipamento.")
        tempo_meses = cls._parse_meses(novo_tempo_contrato_meses) if locado == SimNaoChoices.SIM else None
        if locado == SimNaoChoices.SIM and tempo_meses is None:
            raise ValidationError("Informe o tempo do contrato de locação em meses.")
        cobranca_proximo_ano = (
            SimNaoChoices.SIM if (locado == SimNaoChoices.SIM and novo_cobranca_proximo_ano == "sim")
            else SimNaoChoices.NAO
        )

        item_lote.novo_nome = (novo_nome or "").strip()
        item_lote.novo_numero_serie = (novo_numero_serie or "").strip()
        item_lote.novo_marca = (novo_marca or "").strip()
        item_lote.novo_modelo = (novo_modelo or "").strip()
        item_lote.novo_categoria = novo_categoria
        item_lote.novo_subtipo = novo_subtipo
        item_lote.novo_localidade = novo_localidade
        item_lote.novo_centro_custo = novo_centro_custo
        item_lote.novo_locado = locado
        item_lote.novo_pmb = pmb
        item_lote.novo_valor = valor
        item_lote.novo_contrato = (novo_contrato or "").strip()
        item_lote.novo_tempo_contrato_meses = tempo_meses
        item_lote.novo_cobranca_proximo_ano = cobranca_proximo_ano
        cls._audit(item_lote, user, criando=False)
        item_lote.save()
        return item_lote

    @classmethod
    def excluir_item(cls, *, item_lote, user):
        cls._garantir_rascunho(item_lote)
        item_lote.delete()

    # ── Materialização (envio de verdade) ────────────────────────────────────
    @classmethod
    def _criar_equipamento_novo(cls, item_lote, user):
        fornecedor = item_lote.lote.fornecedor
        item = Item(
            nome=item_lote.novo_nome,
            numero_serie=item_lote.novo_numero_serie or None,
            marca=item_lote.novo_marca or None,
            modelo=item_lote.novo_modelo or None,
            status=StatusItemChoices.PAUSADO,  # a caminho — ativado na confirmação do TI
            fornecedor=fornecedor,
            categoria=item_lote.novo_categoria,
            subtipo=item_lote.novo_subtipo,
            localidade=item_lote.novo_localidade,
            centro_custo=item_lote.novo_centro_custo,
            locado=item_lote.novo_locado,
            pmb=item_lote.novo_pmb,
            valor=(None if item_lote.novo_locado == SimNaoChoices.SIM else item_lote.novo_valor),
            data_compra=timezone.localdate() if item_lote.novo_locado != SimNaoChoices.SIM else None,
            observacoes=(
                f"Equipamento novo cadastrado pelo fornecedor {fornecedor.nome} "
                f"— Lote de Envio #{item_lote.lote_id}."
            ),
        )
        cls._audit(item, user, criando=True)
        item.save()

        if item_lote.novo_locado == SimNaoChoices.SIM:
            locacao = Locacao(
                equipamento=item,
                tempo_locado=item_lote.novo_tempo_contrato_meses,
                valor_mensal=item_lote.novo_valor,
                data_entrada=timezone.localdate(),
                contrato=item_lote.novo_contrato,
                fornecedor=fornecedor,
            )
            cls._audit(locacao, user, criando=True)
            locacao.save()
        return item

    @classmethod
    @transaction.atomic
    def enviar_item(cls, *, item_lote, user):
        cls._garantir_rascunho(item_lote)

        if item_lote.tipo == TipoItemLoteEnvioChoices.TROCA_ANTECIPADA:
            from services.ordem_manutencao_service import OrdemManutencaoService
            # Re-checagem de elegibilidade no momento do envio (defesa em profundidade
            # contra um rascunho "envelhecido" — o item pode ter deixado de estar em
            # Defeito ou ganho outra OS aberta nesse meio-tempo). O próprio
            # abrir_troca_antecipada já valida isso e levanta ValidationError.
            item_lote.item_defeituoso.refresh_from_db()
            data_contrato = item_lote.sub_data_contrato.isoformat() if item_lote.sub_data_contrato else ""
            ordem = OrdemManutencaoService.abrir_troca_antecipada(
                item_defeituoso=item_lote.item_defeituoso,
                fornecedor=item_lote.lote.fornecedor,
                user=user,
                sub_modelo=item_lote.sub_modelo,
                sub_serie=item_lote.sub_serie,
                sub_marca=item_lote.sub_marca,
                sub_data_contrato=data_contrato,
            )
            item_lote.ordem = ordem
            item_lote.item_resultado = ordem.item_substituto
        elif item_lote.tipo == TipoItemLoteEnvioChoices.REPARO_CONCLUIDO:
            from services.ordem_manutencao_service import OrdemManutencaoService
            # A OS já existe desde a criação do rascunho — o envio é o que de
            # fato transiciona ela para DEVOLVIDO (reaproveita _on_devolvido,
            # sem duplicar regra nenhuma).
            # `_on_devolvido` espera valores em string (mesmo formato do POST
            # direto do formulário) — nunca um Decimal cru.
            OrdemManutencaoService.transicionar(
                ordem=item_lote.ordem, novo_status=S_OS.DEVOLVIDO, user=user, ator="fornecedor",
                localidade_devolucao=str(item_lote.localidade_devolucao_id or ""),
                valor_avaliacao_tecnica=(
                    str(item_lote.valor_avaliacao_tecnica)
                    if item_lote.valor_avaliacao_tecnica is not None else ""
                ),
            )
            item_lote.item_resultado = item_lote.ordem.item
        else:
            item_lote.item_resultado = cls._criar_equipamento_novo(item_lote, user)

        item_lote.status = S_ITEM.ENVIADO
        item_lote.enviado_em = timezone.now()
        cls._audit(item_lote, user, criando=False)
        item_lote.save()

        lote = item_lote.lote
        if lote.status == S_LOTE.ABERTO and not lote.itens.filter(status=S_ITEM.RASCUNHO).exists():
            lote.status = S_LOTE.ENVIADO
            lote.enviado_em = timezone.now()
            cls._audit(lote, user, criando=False)
            lote.save(update_fields=["status", "enviado_em", "atualizado_por", "updated_at"])
        return item_lote

    @classmethod
    @transaction.atomic
    def enviar_lote(cls, *, lote, user):
        itens_rascunho = list(lote.itens.filter(status=S_ITEM.RASCUNHO))
        if not itens_rascunho:
            raise ValidationError("Não há itens em rascunho para enviar neste lote.")
        for item_lote in itens_rascunho:
            cls.enviar_item(item_lote=item_lote, user=user)
        return lote

    # ── Confirmação em lote (TI) — conveniência sobre o mesmo processo já
    # existente: cada item só avança UMA etapa (a que já é a próxima ação válida
    # do TI naquele ponto). Nunca pula aprovação de orçamento ou outra decisão
    # que exige julgamento humano — esses ficam para o fluxo individual. ────────
    @classmethod
    @transaction.atomic
    def confirmar_recebimento_lote(cls, *, lote, user, status_retorno=None):
        from services.ordem_manutencao_service import OrdemManutencaoService

        confirmados = 0
        ignorados = 0
        itens = (
            lote.itens
            .filter(status=S_ITEM.ENVIADO)
            .select_related("ordem")
            .select_for_update()
        )
        for item_lote in itens:
            if item_lote.tipo == TipoItemLoteEnvioChoices.EQUIPAMENTO_NOVO:
                cls.confirmar_equipamento_novo(
                    item_lote=item_lote, user=user, status_retorno=status_retorno,
                )
                confirmados += 1
            elif (item_lote.tipo == TipoItemLoteEnvioChoices.REPARO_CONCLUIDO
                    and item_lote.ordem_id and item_lote.ordem.status == S_OS.DEVOLVIDO):
                cls.confirmar_reparo_concluido(
                    item_lote=item_lote, user=user, status_retorno=status_retorno,
                )
                confirmados += 1
            elif item_lote.ordem_id and item_lote.ordem.status == S_OS.TROCA_ANT_SUBSTITUTO_ENVIADO:
                OrdemManutencaoService.transicionar(
                    ordem=item_lote.ordem, novo_status=S_OS.TROCA_ANT_SUBSTITUTO_RECEBIDO,
                    user=user, ator="ti", status_retorno=status_retorno,
                )
                confirmados += 1
            else:
                ignorados += 1

        if confirmados == 0 and ignorados == 0:
            raise ValidationError("Não há itens aguardando confirmação de recebimento neste lote.")
        return {"confirmados": confirmados, "ignorados": ignorados}

    # ── Histórico consolidado do lote ────────────────────────────────────────
    @classmethod
    def montar_historico_lote(cls, lote):
        """Linha do tempo do lote: mescla os marcos do lote/itens com os eventos
        reais das OS de troca antecipada geradas — sem modelo de auditoria novo,
        só junta o que já existe (`AuditModel` + `OrdemManutencaoEvento`)."""
        eventos = [{
            "quando": lote.created_at, "icone": "fa-dolly",
            "titulo": "Lote criado", "detalhe": f'"{lote.nome}"', "autor": "",
        }]
        if lote.enviado_em:
            eventos.append({
                "quando": lote.enviado_em, "icone": "fa-paper-plane",
                "titulo": "Lote enviado ao TI", "detalhe": "", "autor": "",
            })

        for item_lote in lote.itens.select_related("item_defeituoso", "ordem", "ordem__item", "ordem__aprovado_por"):
            if item_lote.item_defeituoso_id:
                nome = item_lote.item_defeituoso.nome
            elif item_lote.tipo == TipoItemLoteEnvioChoices.REPARO_CONCLUIDO and item_lote.ordem_id:
                nome = item_lote.ordem.item.nome
            else:
                nome = item_lote.novo_nome or "Equipamento novo"
            if item_lote.enviado_em:
                eventos.append({
                    "quando": item_lote.enviado_em, "icone": "fa-paper-plane",
                    "titulo": f"Item enviado — {nome}", "detalhe": item_lote.get_tipo_display(), "autor": "",
                })
            if item_lote.tipo in (TipoItemLoteEnvioChoices.TROCA_ANTECIPADA, TipoItemLoteEnvioChoices.REPARO_CONCLUIDO) and item_lote.ordem_id:
                for ev in item_lote.ordem.eventos.select_related("criado_por").all():
                    autor = ""
                    if ev.criado_por_id:
                        autor = ev.criado_por.get_full_name() or ev.criado_por.username
                    eventos.append({
                        "quando": ev.created_at, "icone": "fa-screwdriver-wrench",
                        "titulo": f"OS #{item_lote.ordem_id} — {ev.get_status_display()}",
                        "detalhe": ev.observacao or "", "autor": autor,
                    })
            elif item_lote.recebido_em:
                eventos.append({
                    "quando": item_lote.recebido_em, "icone": "fa-circle-check",
                    "titulo": f"Item recebido — {nome}", "detalhe": "", "autor": "",
                })

        eventos.sort(key=lambda e: e["quando"])
        return eventos

    # ── Confirmação do TI (só para equipamento novo — troca antecipada já usa
    # o fluxo de recebimentos existente da própria OrdemManutencao) ─────────
    @classmethod
    @transaction.atomic
    def confirmar_equipamento_novo(cls, *, item_lote, user, status_retorno=None):
        if item_lote.tipo != TipoItemLoteEnvioChoices.EQUIPAMENTO_NOVO:
            raise ValidationError("Esta ação só se aplica a itens de cadastro de equipamento novo.")
        if item_lote.status != S_ITEM.ENVIADO:
            raise ValidationError("Este item ainda não foi enviado pelo fornecedor.")
        item = item_lote.item_resultado
        if item is None:
            raise ValidationError("Equipamento não encontrado para este item.")

        destino = status_retorno or StatusItemChoices.BACKUP

        # Início da cobrança do aluguel (ver LocacaoPeriodo/locacao_service): se o
        # fornecedor marcou "cobrança só a partir do próximo ano", o período começa
        # em 1º de janeiro do ano seguinte a esta confirmação; senão, começa na
        # data em que o fornecedor cadastrou o item neste lote de envio.
        if item.locado == SimNaoChoices.SIM:
            if item_lote.novo_cobranca_proximo_ano == SimNaoChoices.SIM:
                ano_base = timezone.localdate().year
                item._locacao_data_inicio_override = _date(ano_base + 1, 1, 1)
            else:
                item._locacao_data_inicio_override = item_lote.created_at.date()

        item.status = destino
        cls._audit(item, user, criando=False)
        item.save(update_fields=["status", "atualizado_por", "updated_at"])

        mov = MovimentacaoItem(
            tipo_movimentacao=TipoMovimentacaoChoices.ENTRADA,
            item=item,
            quantidade=item.quantidade or 1,
            localidade_destino=item.localidade,
            centro_custo_destino=item.centro_custo,
            fornecedor_manutencao=item_lote.lote.fornecedor,
            observacao=(
                f"Entrada de equipamento novo cadastrado pelo fornecedor "
                f"— Lote de Envio #{item_lote.lote_id}."
            ),
        )
        cls._audit(mov, user, criando=True)
        mov.save()

        item_lote.status = S_ITEM.RECEBIDO
        item_lote.recebido_em = timezone.now()
        cls._audit(item_lote, user, criando=False)
        item_lote.save(update_fields=["status", "recebido_em", "atualizado_por", "updated_at"])
        return item_lote

    # ── Confirmação do TI (REPARO_CONCLUIDO isolado — a confirmação em lote
    # já cobre o caso em massa via confirmar_recebimento_lote) ──────────────
    @classmethod
    @transaction.atomic
    def confirmar_reparo_concluido(cls, *, item_lote, user, status_retorno=None):
        """Confirma o recebimento físico do retorno de um reparo concluído —
        conclui a OS (reaproveita `_on_concluido`, mesmo efeito colateral do
        botão direto "Concluir" em Recebimentos, incluindo a movimentação de
        retorno). As informações do lote (fornecedor, NF, nome) continuam
        disponíveis depois via `LoteEnvioFornecedorItem.objects.filter(ordem=...)`."""
        if item_lote.tipo != TipoItemLoteEnvioChoices.REPARO_CONCLUIDO:
            raise ValidationError("Esta ação só se aplica a itens de retorno de reparo concluído.")
        if item_lote.status != S_ITEM.ENVIADO:
            raise ValidationError("Este item ainda não foi enviado pelo fornecedor.")
        if not item_lote.ordem_id or item_lote.ordem.status != S_OS.DEVOLVIDO:
            raise ValidationError("A Ordem de Manutenção não está aguardando confirmação de recebimento.")

        from services.ordem_manutencao_service import OrdemManutencaoService
        OrdemManutencaoService.transicionar(
            ordem=item_lote.ordem, novo_status=S_OS.CONCLUIDO, user=user, ator="ti",
            status_retorno=status_retorno,
        )

        item_lote.status = S_ITEM.RECEBIDO
        item_lote.recebido_em = timezone.now()
        cls._audit(item_lote, user, criando=False)
        item_lote.save(update_fields=["status", "recebido_em", "atualizado_por", "updated_at"])
        return item_lote

    # ── Nota Fiscal do lote (mesmo padrão de OrdemManutencaoAnexo) ──────────
    @classmethod
    def anexar_nf(cls, *, lote, arquivos, descricao, user, origem=None):
        origem = origem or LoteEnvioFornecedorAnexo.OrigemAnexo.FORNECEDOR
        criados = []
        for arq in arquivos:
            anexo = LoteEnvioFornecedorAnexo(
                lote=lote, arquivo=arq, origem=origem, descricao=(descricao or "").strip(),
            )
            cls._audit(anexo, user, criando=True)
            anexo.save()
            criados.append(anexo)
        return criados

    @classmethod
    def excluir_nf(cls, *, anexo, user):
        if anexo.arquivo:
            anexo.arquivo.delete(save=False)  # remove o arquivo físico também
        anexo.delete()
