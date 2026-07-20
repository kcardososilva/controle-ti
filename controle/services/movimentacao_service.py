import logging
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from ProjetoEstoque.models import (
    Item,
    ItemColaborador,
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
    SEPARACAO_ENVIO = "separacao_envio"
    SEPARACAO_DEVOLUCAO = "separacao_devolucao"
    DEVOLUCAO_LOCACAO = "devolucao_locacao"

    @staticmethod
    def preencher_auditoria(obj, user, criando=True):
        if criando and hasattr(obj, "criado_por") and not getattr(obj, "criado_por_id", None):
            obj.criado_por = user

        if hasattr(obj, "atualizado_por"):
            obj.atualizado_por = user

    @classmethod
    def _sync_vinculo_compartilhado(cls, *, mov, item, user):
        """
        Abre/encerra o vínculo (ItemColaborador) de um equipamento COMPARTILHADO
        de acordo com a transferência de dispositivo:

        - entrega   → cria (ou reaproveita) o vínculo ativo do colaborador;
        - devolução → encerra o vínculo ativo do colaborador selecionado.

        Itens não-compartilhados não passam por aqui (mantêm o detentor único
        derivado da última movimentação).
        """
        from django.utils import timezone

        acao = mov.tipo_transferencia

        if acao == "entrega":
            if not mov.usuario_id:
                return  # o formulário já exige o usuário na entrega

            vinculo = (
                ItemColaborador.objects
                .filter(item=item, colaborador_id=mov.usuario_id, ativo=True)
                .first()
            )

            if vinculo is None:
                vinculo = ItemColaborador(
                    item=item,
                    colaborador_id=mov.usuario_id,
                    ativo=True,
                    data_vinculo=timezone.now(),
                )

            vinculo.movimentacao_entrega = mov
            cls.preencher_auditoria(vinculo, user, criando=(vinculo.pk is None))
            vinculo.save()

        elif acao == "devolucao":
            if not mov.usuario_id:
                raise ValidationError(
                    "Para devolver um equipamento compartilhado, selecione no campo "
                    "“Usuário” qual colaborador está devolvendo o item."
                )

            vinculo = (
                ItemColaborador.objects
                .filter(item=item, colaborador_id=mov.usuario_id, ativo=True)
                .first()
            )

            if vinculo is not None:
                vinculo.ativo = False
                vinculo.data_devolucao = timezone.now()
                vinculo.movimentacao_devolucao = mov
                cls.preencher_auditoria(vinculo, user, criando=False)
                vinculo.save(update_fields=[
                    "ativo",
                    "data_devolucao",
                    "movimentacao_devolucao",
                    "updated_at",
                    "atualizado_por",
                ])

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
        """Adapta o `MovimentacaoItemForm` (tela de Movimentações) pro núcleo
        `registrar_entrada()` — mantém as duas chamadas (form da tela e
        chamada direta, ex.: recebimento de compra via Requisição) na mesma
        lógica de negócio, sem duplicar efeitos colaterais (lote, e-mail...)."""
        return cls.registrar_entrada(
            item=form.cleaned_data["item"],
            fornecedor=form.cleaned_data["lote_fornecedor"],
            data_entrada=form.cleaned_data["lote_data_entrada"],
            numero_nf=form.cleaned_data["lote_numero_nf"],
            quantidade=form.cleaned_data["lote_quantidade"],
            custo_unitario=form.cleaned_data["lote_custo_unitario"],
            observacao_lote=form.cleaned_data.get("lote_observacao_tecnica"),
            localidade_destino=form.cleaned_data["localidade_destino"],
            centro_custo_destino=form.cleaned_data["centro_custo_destino"],
            observacao=form.cleaned_data.get("observacao"),
            user=user,
        )

    @classmethod
    def registrar_entrada(cls, *, item, fornecedor, data_entrada, numero_nf, quantidade,
                           custo_unitario, observacao_lote, localidade_destino,
                           centro_custo_destino, observacao, user):
        """Núcleo da Entrada de estoque — cria o `LoteEstoque`/`ItemLote`, a
        `MovimentacaoItem` e atualiza o `Item`. Chamado tanto pelo form da
        tela de Movimentações (`_registrar_entrada`) quanto diretamente por
        outros fluxos (ex.: `RequisicaoService.finalizar_compra_estoque`)."""
        item = (
            Item.objects
            .select_for_update()
            .get(pk=item.pk)
        )

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

        # E-mail de entrada — foco em estoque (canal "entrada_estoque",
        # configurável no gerenciador de notificações).
        _mov_ref = mov

        def _enviar_email_entrada():
            try:
                from services.email_alertas import alerta_entrada_estoque
                alerta_entrada_estoque(_mov_ref)
            except Exception as exc:
                logger.warning("email entrada: falha ao enviar: %s", exc)

        transaction.on_commit(_enviar_email_entrada)

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

        # E-mail de baixa — foco em estoque (canal "baixa_estoque", configurável
        # no gerenciador de notificações). Não chama `alerta_movimentacao` aqui:
        # para baixa ela caía sempre na lista crua do .env (canal "movimentacao_
        # transacional" é dinâmico e ignora customização), duplicando o aviso
        # deste mesmo evento para os mesmos destinatários.
        _mov_ref = mov
        _qtd_restante = item.quantidade

        def _enviar_email_baixa():
            try:
                from services.email_alertas import alerta_baixa_estoque
                alerta_baixa_estoque(_mov_ref, qtd_restante=_qtd_restante)
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
        # Retorno de manutenção com fornecedor: a origem real é o fornecedor (ver
        # `fornecedor_manutencao`), não a localidade/CC do item — que, como o envio
        # nunca a altera, ainda seria a localidade "de casa", não a de onde o
        # equipamento está de fato voltando.
        if mov.tipo_movimentacao == cls.RETORNO_MANUTENCAO and mov.fornecedor_manutencao_id:
            pass
        else:
            mov.localidade_origem = item.localidade
            mov.centro_custo_origem = item.centro_custo

        if mov.tipo_movimentacao in {
            cls.ENVIO_MANUTENCAO,
            cls.RETORNO_MANUTENCAO,
            cls.RETORNO,
            cls.SEPARACAO_ENVIO,
            cls.SEPARACAO_DEVOLUCAO,
            cls.DEVOLUCAO_LOCACAO,
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
            # Item compartilhado tem vários detentores: não dá para inferir um
            # único usuário/CC da "última entrega". A escolha de quem devolve é
            # feita explicitamente no formulário e tratada pelo vínculo.
            and not item.compartilhado
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

                # Rastreabilidade: puxa automaticamente a pessoa a quem o item está
                # vinculado (quem recebeu na última entrega), já que o formulário de
                # devolução não pede o usuário. Mantém o que veio informado, se houver.
                if not mov.usuario_id and ultima_entrega.usuario_id:
                    mov.usuario = ultima_entrega.usuario

        cls.preencher_auditoria(mov, user, criando=True)
        mov.full_clean()
        mov.save()

        update_fields = []

        if mov.tipo_movimentacao == cls.ENVIO_MANUTENCAO:
            # Enviar para manutenção NÃO dá baixa na quantidade: o equipamento
            # continua sendo o mesmo ativo, apenas muda de status para "Manutenção".
            item.status = StatusItemChoices.MANUTENCAO
            update_fields.append("status")

            # Abre a Ordem de Manutenção do Portal do Fornecedor quando há um
            # fornecedor de manutenção definido (import tardio evita ciclo).
            if mov.fornecedor_manutencao_id:
                from services.ordem_manutencao_service import OrdemManutencaoService
                OrdemManutencaoService.abrir(
                    item=item,
                    fornecedor=mov.fornecedor_manutencao,
                    movimentacao=mov,
                    user=user,
                )

        elif mov.tipo_movimentacao in {cls.SEPARACAO_ENVIO, cls.SEPARACAO_DEVOLUCAO}:
            # Área de estágio (ver SeparacaoItem): NÃO altera status, localidade
            # nem centro de custo do item — só registra que ele está fisicamente
            # separado, aguardando despacho real (envio_manutencao ou
            # devolucao_locacao), disparado depois pela tela de Separação.
            from ProjetoEstoque.models import TipoSeparacaoChoices
            from services.separacao_service import SeparacaoService

            tipo_sep = (
                TipoSeparacaoChoices.ENVIO
                if mov.tipo_movimentacao == cls.SEPARACAO_ENVIO
                else TipoSeparacaoChoices.DEVOLUCAO
            )
            SeparacaoService.estagiar(
                item=item,
                tipo=tipo_sep,
                fornecedor=mov.fornecedor_manutencao,
                observacao=mov.observacao,
                mov=mov,
                user=user,
            )

        elif mov.tipo_movimentacao == cls.DEVOLUCAO_LOCACAO:
            # Devolução definitiva do item locado à locadora — o congelamento do
            # período de cobrança (LocacaoPeriodo) acontece sozinho via o signal
            # já existente (services/locacao_service.py), que trata "devolvido"
            # como status congelante.
            item.status = StatusItemChoices.DEVOLVIDO
            update_fields.append("status")

        elif mov.tipo_movimentacao in {cls.RETORNO_MANUTENCAO, cls.RETORNO}:
            # Retorno de manutenção mantém a quantidade inalterada (espelha o envio):
            # apenas atualiza o status e, se informada, a localidade de destino.
            item.status = mov.status_retorno or StatusItemChoices.BACKUP
            update_fields.append("status")

            if mov.localidade_destino:
                item.localidade = mov.localidade_destino
                update_fields.append("localidade")

        elif mov.tipo_movimentacao == cls.TRANSFERENCIA and item.compartilhado:
            # ── Equipamento COMPARTILHADO ──────────────────────────────────
            # Pode ficar com vários colaboradores ao mesmo tempo. Não sobrescreve
            # localidade/CC do item (ele é um ativo compartilhado, "casa fixa").
            # A entrega abre um vínculo; a devolução encerra o do colaborador
            # selecionado. O status do item só volta para Backup quando NÃO
            # restar nenhum colaborador vinculado.
            cls._sync_vinculo_compartilhado(mov=mov, item=item, user=user)

            existe_vinculo_ativo = ItemColaborador.objects.filter(
                item=item, ativo=True
            ).exists()

            if mov.tipo_transferencia == "entrega" and item.status == StatusItemChoices.BACKUP:
                item.status = StatusItemChoices.ATIVO
                update_fields.append("status")

            elif (
                mov.tipo_transferencia == "devolucao"
                and not existe_vinculo_ativo
                and item.status == StatusItemChoices.ATIVO
            ):
                item.status = StatusItemChoices.BACKUP
                update_fields.append("status")

        elif mov.tipo_movimentacao == cls.TRANSFERENCIA:
            if mov.localidade_destino:
                item.localidade = mov.localidade_destino
                update_fields.append("localidade")

            if _devolucao_restore_cc:
                # Restaura o CC original do item (pode ser None se não tinha CC antes da entrega)
                item.centro_custo = _restore_cc
                update_fields.append("centro_custo")
            elif (
                mov.tipo_transferencia == "entrega"
                and mov.usuario_id
                and mov.usuario.centro_custo_id
            ):
                # Item segue o CC do detentor: ao entregar a um colaborador, o
                # centro de custo do equipamento passa a ser o do colaborador.
                item.centro_custo = mov.usuario.centro_custo
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

            if mov.usuario_id and mov.usuario.centro_custo_id:
                # Item segue o CC do detentor (mesmo critério da entrega).
                item.centro_custo = mov.usuario.centro_custo
                update_fields.append("centro_custo")
            elif mov.centro_custo_destino:
                item.centro_custo = mov.centro_custo_destino
                update_fields.append("centro_custo")

            if mov.status_transferencia:
                item.status = mov.status_transferencia
                update_fields.append("status")

            # Renomeação do equipamento na transferência de equipamento.
            # Prioridade:
            #   1) Se o operador preencheu "Renomear Equipamento", esse nome prevalece.
            #   2) Caso contrário, ao marcar o equipamento como "Defeito", o nome passa
            #      a ser igual ao modelo do equipamento (padronização dos itens com
            #      defeito). Só aplica quando o modelo está preenchido.
            # Em ambos os casos só renomeia quando o nome final difere do atual e
            # registra a alteração na observação da movimentação (auditoria).
            novo_nome = (form.cleaned_data.get("novo_nome") or "").strip()
            nome_atual = (item.nome or "").strip()
            modelo_atual = (item.modelo or "").strip()

            nome_final = None
            if novo_nome:
                nome_final = novo_nome
            elif mov.status_transferencia == StatusItemChoices.DEFEITO and modelo_atual:
                nome_final = modelo_atual

            if nome_final and nome_final != nome_atual:
                item.nome = nome_final
                update_fields.append("nome")
                nota = f'Renomeado: "{nome_atual}" → "{nome_final}".'
                mov.observacao = f"{mov.observacao}\n{nota}".strip() if mov.observacao else nota
                mov.save(update_fields=["observacao", "updated_at"])

        if update_fields:
            cls.preencher_auditoria(item, user, criando=False)

            if hasattr(item, "atualizado_por"):
                update_fields.append("atualizado_por")

            item.save(update_fields=list(set(update_fields)))

        # E-mail ao entregar/devolver item ou transferir equipamento (roda após
        # commit da transação) — canais "movimentacao_transacional" e
        # "transferencia_equipamento", ambos configuráveis no gerenciador.
        if (
            mov.tipo_movimentacao == cls.TRANSFERENCIA
            and getattr(mov, "tipo_transferencia", None) in ("entrega", "devolucao")
        ) or mov.tipo_movimentacao == cls.TRANSFERENCIA_EQUIPAMENTO:
            _mov_ref = mov

            def _enviar_email_movimentacao():
                try:
                    from services.email_alertas import alerta_movimentacao
                    alerta_movimentacao(_mov_ref)
                except Exception as exc:
                    logger.warning("email movimentacao: falha ao enviar: %s", exc)

            transaction.on_commit(_enviar_email_movimentacao)

        return mov

    # ── Reversão ─────────────────────────────────────────────────────────
    # Só Entrada e Baixa são revertidas por aqui: são os únicos tipos cujo
    # efeito é puramente um contador de estoque (item.quantidade / ItemLote.
    # quantidade_disponivel), então a reversão é mecânica e sem ambiguidade.
    # Transferência, envio/retorno de manutenção, separação e devolução de
    # locação já têm fluxo próprio de cancelamento (Portal do Fornecedor /
    # Locação) — desfazê-los aqui arriscaria dessincronizar esses fluxos.
    REVERTIVEIS = {ENTRADA, BAIXA}

    @classmethod
    @transaction.atomic
    def reverter(cls, *, movimentacao_id, user):
        """
        Desfaz o efeito de estoque de uma movimentação de Entrada ou Baixa.
        A movimentação NUNCA é apagada nem gera uma nova movimentação
        "espelho" — só é marcada como `revertida`, preservando o registro de
        que a operação aconteceu e foi desfeita depois.

        Só a movimentação mais recente (não revertida) do item pode ser
        revertida: é a única forma de garantir que a reversão restaura
        exatamente o estado anterior, sem invalidar algo que aconteceu
        depois dela.
        """
        mov = (
            MovimentacaoItem.objects
            .select_for_update()
            .select_related("item")
            .get(pk=movimentacao_id)
        )

        if mov.revertida:
            raise ValidationError("Esta movimentação já foi revertida.")

        if mov.tipo_movimentacao not in cls.REVERTIVEIS:
            raise ValidationError(
                "Só é possível reverter movimentações de Entrada ou Baixa de estoque."
            )

        item = Item.objects.select_for_update().get(pk=mov.item_id)

        ultima_mov = (
            MovimentacaoItem.objects
            .filter(item=item, revertida=False)
            .order_by("-created_at", "-id")
            .first()
        )

        if ultima_mov is None or ultima_mov.pk != mov.pk:
            raise ValidationError(
                "Só é possível reverter a movimentação mais recente deste item — "
                "existem movimentações mais novas registradas para ele."
            )

        if mov.tipo_movimentacao == cls.ENTRADA:
            cls._reverter_entrada(mov=mov, item=item, user=user)
        else:
            cls._reverter_baixa(mov=mov, item=item, user=user)

        mov.revertida = True
        mov.revertida_em = timezone.now()
        mov.revertida_por = user
        mov.save(update_fields=["revertida", "revertida_em", "revertida_por", "updated_at"])

        return mov

    @classmethod
    def _reverter_entrada(cls, *, mov, item, user):
        item_lote = (
            ItemLote.objects
            .select_for_update()
            .filter(item=item, lote_id=mov.lote_id)
            .first()
        )

        if item_lote is None:
            raise ValidationError("O lote desta entrada não existe mais — não é possível reverter.")

        if item_lote.quantidade_disponivel != item_lote.quantidade_entrada:
            raise ValidationError(
                "Este lote já teve saída de estoque — não é possível reverter a entrada."
            )

        if (item.quantidade or 0) < mov.quantidade:
            raise ValidationError("Saldo do item inconsistente — reversão bloqueada.")

        item_lote.quantidade_disponivel = 0
        cls.preencher_auditoria(item_lote, user, criando=False)
        item_lote.full_clean()
        item_lote.save()

        item.quantidade = (item.quantidade or 0) - mov.quantidade
        update_fields = ["quantidade"]

        # Restaura localidade/CC ao estado anterior à entrada — capturado em
        # `mov.localidade_origem`/`centro_custo_origem` no momento do registro.
        if mov.localidade_origem_id:
            item.localidade = mov.localidade_origem
            update_fields.append("localidade")

        if mov.centro_custo_origem_id:
            item.centro_custo = mov.centro_custo_origem
            update_fields.append("centro_custo")

        cls.preencher_auditoria(item, user, criando=False)
        if hasattr(item, "atualizado_por"):
            update_fields.append("atualizado_por")
        item.save(update_fields=list(set(update_fields)))

    @classmethod
    def _reverter_baixa(cls, *, mov, item, user):
        item_lote = (
            ItemLote.objects
            .select_for_update()
            .filter(item=item, lote_id=mov.lote_id)
            .first()
        )

        if item_lote is None:
            raise ValidationError("O lote desta baixa não existe mais — não é possível reverter.")

        nova_disponivel = item_lote.quantidade_disponivel + mov.quantidade

        if nova_disponivel > item_lote.quantidade_entrada:
            raise ValidationError("Saldo do lote inconsistente — reversão bloqueada.")

        item_lote.quantidade_disponivel = nova_disponivel
        cls.preencher_auditoria(item_lote, user, criando=False)
        item_lote.full_clean()
        item_lote.save()

        item.quantidade = (item.quantidade or 0) + mov.quantidade
        cls.preencher_auditoria(item, user, criando=False)
        item.save(update_fields=[
            "quantidade",
            "atualizado_por",
        ] if hasattr(item, "atualizado_por") else [
            "quantidade",
        ])