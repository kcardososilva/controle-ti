"""
OrdemManutencaoService — máquina de estados da manutenção externa (Portal do Fornecedor).

Fluxo:
    aguardando_recebimento → recebido → em_avaliacao
        ├─ em_reparo → reparado ──────────────→ (TI) concluido
        └─ sem_reparo → substituto_enviado ───→ (TI) concluido

Regras:
  • Toda transição passa por `transicionar()` — valida o caminho e o ator.
  • Cada transição grava um OrdemManutencaoEvento (timeline/auditoria).
  • Efeitos colaterais (status do item, criação do substituto, movimentações)
    ficam no service — nunca na view (CLAUDE.md regra 2).
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
    MovimentacaoItem,
    OrdemManutencao,
    OrdemManutencaoEvento,
    SimNaoChoices,
    StatusItemChoices,
    StatusOrdemManutencaoChoices,
    TipoMovimentacaoChoices,
)

logger = logging.getLogger(__name__)
S = StatusOrdemManutencaoChoices

# Caminhos permitidos: estado atual → próximos estados válidos.
TRANSICOES = {
    S.AGUARDANDO_RECEBIMENTO: {S.RECEBIDO, S.CANCELADO},
    S.RECEBIDO:               {S.EM_AVALIACAO, S.CANCELADO},
    S.EM_AVALIACAO:           {S.EM_REPARO, S.SEM_REPARO, S.CANCELADO},
    S.EM_REPARO:              {S.REPARADO, S.SEM_REPARO, S.CANCELADO},
    S.REPARADO:               {S.DEVOLVIDO, S.CANCELADO},
    S.DEVOLVIDO:              {S.CONCLUIDO},
    S.SEM_REPARO:             {S.SUBSTITUTO_ENVIADO, S.CANCELADO},
    S.SUBSTITUTO_ENVIADO:     {S.CONCLUIDO},
    S.CONCLUIDO:              set(),
    S.CANCELADO:              set(),
}

# Quem dispara cada transição de destino: "fornecedor" (portal) ou "ti" (interno).
ATOR = {
    S.RECEBIDO:           "fornecedor",
    S.EM_AVALIACAO:       "fornecedor",
    S.EM_REPARO:          "fornecedor",
    S.SEM_REPARO:         "fornecedor",
    S.REPARADO:           "fornecedor",
    S.DEVOLVIDO:          "fornecedor",
    S.SUBSTITUTO_ENVIADO: "fornecedor",
    S.CONCLUIDO:          "ti",
    S.CANCELADO:          "ti",
}


class OrdemManutencaoService:

    # ── Auditoria ──────────────────────────────────────────────────────────
    @staticmethod
    def _audit(obj, user, criando=True):
        if criando and hasattr(obj, "criado_por") and not getattr(obj, "criado_por_id", None):
            obj.criado_por = user
        if hasattr(obj, "atualizado_por"):
            obj.atualizado_por = user

    @classmethod
    def _registrar_evento(cls, ordem, status, observacao, user):
        ev = OrdemManutencaoEvento(ordem=ordem, status=status, observacao=(observacao or "").strip())
        cls._audit(ev, user, criando=True)
        ev.save()
        return ev

    # ── Abertura (gatilho do envio para manutenção) ────────────────────────
    @classmethod
    def abrir(cls, *, item, fornecedor, movimentacao=None, user=None):
        """
        Cria a OS no envio para manutenção. Idempotente: se já houver uma OS
        aberta para o item, reutiliza (evita duplicar em reenvios).
        """
        if fornecedor is None:
            return None

        existente = (
            OrdemManutencao.objects
            .filter(item=item)
            .exclude(status__in=[S.CONCLUIDO, S.CANCELADO])
            .first()
        )
        if existente:
            return existente

        ordem = OrdemManutencao(
            item=item,
            fornecedor=fornecedor,
            movimentacao_origem=movimentacao,
            status=S.AGUARDANDO_RECEBIMENTO,
            chamado=getattr(movimentacao, "chamado", None),
        )
        cls._audit(ordem, user, criando=True)
        ordem.save()
        cls._registrar_evento(ordem, S.AGUARDANDO_RECEBIMENTO,
                              "Equipamento enviado para manutenção.", user)
        return ordem

    # ── Consulta ───────────────────────────────────────────────────────────
    @classmethod
    def transicoes_validas(cls, ordem):
        return TRANSICOES.get(ordem.status, set())

    # ── Transição ──────────────────────────────────────────────────────────
    @classmethod
    @transaction.atomic
    def transicionar(cls, *, ordem, novo_status, user, observacao="", ator="fornecedor", **extra):
        ordem = OrdemManutencao.objects.select_for_update().get(pk=ordem.pk)
        novo_status = str(novo_status)

        if novo_status not in {str(s) for s in cls.transicoes_validas(ordem)}:
            raise ValidationError(
                f"Transição inválida: {ordem.get_status_display()} → {novo_status}."
            )

        esperado = ATOR.get(novo_status)
        if esperado and esperado != ator:
            raise ValidationError("Esta ação não é permitida para o seu perfil.")

        # Efeitos colaterais rodam ANTES de gravar o novo status
        # (handlers leem `ordem.status` = estado de origem).
        handler = {
            S.EM_REPARO:          cls._on_diagnostico,
            S.SEM_REPARO:         cls._on_sem_reparo,
            S.DEVOLVIDO:          cls._on_devolvido,
            S.SUBSTITUTO_ENVIADO: cls._on_substituto_enviado,
            S.CONCLUIDO:          cls._on_concluido,
        }.get(novo_status)
        if handler:
            handler(ordem, user, extra)

        ordem.status = novo_status
        if novo_status in (S.CONCLUIDO, S.CANCELADO):
            ordem.finalizada_em = timezone.now()
        cls._audit(ordem, user, criando=False)
        ordem.save()

        cls._registrar_evento(ordem, novo_status, observacao, user)
        return ordem

    # ── Parsers ────────────────────────────────────────────────────────────
    @staticmethod
    def _parse_valor(v):
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
            raise ValidationError("Valor da substituição inválido.")

    @staticmethod
    def _parse_data(v):
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
                raise ValidationError("Data da substituição inválida.")

    # ── Handlers de efeito colateral ───────────────────────────────────────
    @classmethod
    def _on_diagnostico(cls, ordem, user, extra):
        diag = (extra.get("diagnostico") or "").strip()
        if diag:
            ordem.diagnostico = diag

    @staticmethod
    def _loc_id(valor):
        return int(valor) if (valor and str(valor).isdigit()) else None

    @classmethod
    def _on_sem_reparo(cls, ordem, user, extra):
        cls._on_diagnostico(ordem, user, extra)
        # Equipamento sem reparo → fica PAUSADO no sistema até a troca ser concluída.
        item = ordem.item
        item.status = StatusItemChoices.PAUSADO
        cls._audit(item, user, criando=False)
        item.save(update_fields=["status", "atualizado_por", "updated_at"])

    @classmethod
    def _on_devolvido(cls, ordem, user, extra):
        # Na devolução do equipamento reparado, o fornecedor informa a localidade
        # de destino e o valor do reparo realizado.
        loc_id = cls._loc_id(extra.get("localidade_devolucao"))
        if loc_id:
            ordem.devolucao_localidade_id = loc_id
        valor_reparo = cls._parse_valor(extra.get("reparo_valor"))
        if valor_reparo is not None:
            ordem.reparo_valor = valor_reparo

    @classmethod
    def _on_substituto_enviado(cls, ordem, user, extra):
        cls._on_diagnostico(ordem, user, extra)

        # Dados do contrato de substituição (obrigatórios)
        contrato = (extra.get("contrato") or "").strip()
        valor = cls._parse_valor(extra.get("valor"))
        data = cls._parse_data(extra.get("data"))
        if not contrato or valor is None or data is None:
            raise ValidationError("Informe o contrato, o valor e a data da substituição.")

        # Locação: o fornecedor informa se o substituto entra como locação ou compra.
        locado = SimNaoChoices.SIM if (extra.get("locado") == "sim") else SimNaoChoices.NAO

        ordem.substituto_contrato = contrato
        ordem.substituto_valor = valor
        ordem.substituto_data = data
        ordem.substituto_locado = locado

        antigo = ordem.item
        regime = "Locação" if locado == SimNaoChoices.SIM else "Compra"
        loc_sub_id = cls._loc_id(extra.get("localidade_substituto")) or antigo.localidade_id
        substituto = Item(
            nome=(extra.get("nome") or antigo.nome),
            numero_serie=(extra.get("numero_serie") or "").strip() or None,
            marca=(extra.get("marca") or antigo.marca),
            modelo=(extra.get("modelo") or antigo.modelo),
            status=StatusItemChoices.PAUSADO,
            fornecedor=ordem.fornecedor,
            categoria=antigo.categoria,
            subtipo=antigo.subtipo,
            localidade_id=loc_sub_id,
            centro_custo=antigo.centro_custo,
            locado=locado,
            # Compra → valor é aquisição; Locação → valor é mensal (vai para a Locacao).
            valor=(None if locado == SimNaoChoices.SIM else valor),
            data_compra=data,
            observacoes=(
                f"Substituto do equipamento '{antigo.nome}' — OS #{ordem.pk}. "
                f"Regime: {regime}. Contrato: {contrato}."
            ),
        )
        cls._audit(substituto, user, criando=True)
        substituto.save()
        ordem.item_substituto = substituto

        # Quando entra como locação, cria o contrato de Locacao (alimenta os
        # dashboards de custo via valor_mensal).
        if locado == SimNaoChoices.SIM:
            locacao = Locacao(
                equipamento=substituto,
                valor_mensal=valor,
                data_entrada=data,
                contrato=contrato,
                fornecedor=ordem.fornecedor,
            )
            cls._audit(locacao, user, criando=True)
            locacao.save()

    @classmethod
    def _on_concluido(cls, ordem, user, extra):
        antigo = ordem.item

        if ordem.status == S.DEVOLVIDO:
            destino = extra.get("status_retorno") or StatusItemChoices.BACKUP
            antigo.status = destino
            campos = ["status", "atualizado_por", "updated_at"]
            # Aplica a localidade informada pelo fornecedor na devolução.
            if ordem.devolucao_localidade_id:
                antigo.localidade_id = ordem.devolucao_localidade_id
                campos.append("localidade")
            cls._audit(antigo, user, criando=False)
            antigo.save(update_fields=campos)
            obs = f"Equipamento reparado e devolvido ao TI. OS #{ordem.pk}."
            if ordem.reparo_valor:
                obs += f" Valor do reparo: R$ {ordem.reparo_valor}."
            cls._mov_retorno(ordem, antigo, destino, obs, user, custo=ordem.reparo_valor)

        elif ordem.status == S.SUBSTITUTO_ENVIADO:
            # Item substituído volta ao fornecedor → PAUSADO (congela o aluguel).
            antigo.status = StatusItemChoices.PAUSADO
            cls._audit(antigo, user, criando=False)
            antigo.save(update_fields=["status", "atualizado_por", "updated_at"])
            cls._mov_retorno(ordem, antigo, StatusItemChoices.PAUSADO,
                             f"Equipamento sem reparo — substituído e devolvido ao fornecedor. OS #{ordem.pk}.", user)

            sub = ordem.item_substituto
            if sub:
                sub.status = StatusItemChoices.BACKUP
                cls._audit(sub, user, criando=False)
                sub.save(update_fields=["status", "atualizado_por", "updated_at"])
                cls._mov_entrada_substituto(ordem, sub, user)

    # ── Movimentações de auditoria ─────────────────────────────────────────
    @classmethod
    def _mov_retorno(cls, ordem, item, status_retorno, observacao, user, custo=None):
        mov = MovimentacaoItem(
            tipo_movimentacao=TipoMovimentacaoChoices.RETORNO_MANUTENCAO,
            item=item,
            quantidade=item.quantidade or 1,
            localidade_origem=item.localidade,
            centro_custo_origem=item.centro_custo,
            fornecedor_manutencao=ordem.fornecedor,
            status_retorno=status_retorno,
            chamado=ordem.chamado,
            observacao=observacao,
            custo=custo or Decimal("0.00"),
        )
        cls._audit(mov, user, criando=True)
        mov.save()
        return mov

    @classmethod
    def _mov_entrada_substituto(cls, ordem, substituto, user):
        mov = MovimentacaoItem(
            tipo_movimentacao=TipoMovimentacaoChoices.ENTRADA,
            item=substituto,
            quantidade=substituto.quantidade or 1,
            localidade_destino=substituto.localidade,
            centro_custo_destino=substituto.centro_custo,
            fornecedor_manutencao=ordem.fornecedor,
            observacao=f"Entrada por substituição em manutenção. OS #{ordem.pk}.",
        )
        cls._audit(mov, user, criando=True)
        mov.save()
        return mov
