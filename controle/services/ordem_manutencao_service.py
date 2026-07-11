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
from datetime import date as _date, timedelta
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from ProjetoEstoque.models import (
    Item,
    Locacao,
    MovimentacaoItem,
    OrdemManutencao,
    OrdemManutencaoEvento,
    OrdemManutencaoOrcamento,
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
    # Após avaliar, o fornecedor envia o orçamento (aguardando_aprovacao), decide
    # pela troca (sem_reparo), declara sem condições de reparo (sem_condicoes:
    # devolver p/ o TI descartar) ou solicita descarte no próprio local
    # (descarte_local_solicitado: precisa de aprovação do TI).
    S.EM_AVALIACAO:           {S.AGUARDANDO_APROVACAO, S.SEM_REPARO, S.SEM_CONDICOES,
                               S.DESCARTE_LOCAL_SOLICITADO, S.CANCELADO},
    # TI decide: aprova (segue p/ reparo) ou reprova (devolve com avaliação técnica).
    S.AGUARDANDO_APROVACAO:   {S.APROVADO, S.REPROVADO, S.CANCELADO},
    S.APROVADO:               {S.EM_REPARO, S.CANCELADO},
    # Reprovado: o fornecedor pode REENVIAR um novo orçamento (volta p/ aprovação)
    # ou devolver o equipamento ao TI com a avaliação técnica.
    S.REPROVADO:              {S.AGUARDANDO_APROVACAO, S.DEVOLVIDO, S.CANCELADO},
    S.EM_REPARO:              {S.REPARADO, S.SEM_REPARO, S.CANCELADO},
    S.REPARADO:               {S.DEVOLVIDO, S.CANCELADO},
    S.DEVOLVIDO:              {S.CONCLUIDO},
    S.SEM_REPARO:             {S.SUBSTITUTO_ENVIADO, S.CANCELADO},
    S.SUBSTITUTO_ENVIADO:     {S.CONCLUIDO},
    # Sem condições de reparo: o fornecedor devolve o equipamento à fazenda
    # (devolvido_descarte) e o TI confirma o recebimento, armazenando p/ descarte.
    S.SEM_CONDICOES:          {S.DEVOLVIDO_DESCARTE, S.CANCELADO},
    S.DEVOLVIDO_DESCARTE:     {S.DESCARTADO, S.CANCELADO},
    # Descarte local: fornecedor solicita descartar no próprio local; o TI aprova
    # (→ fornecedor confirma o descarte) ou recusa (→ sem_condicoes: devolver p/
    # o TI receber e descartar).
    S.DESCARTE_LOCAL_SOLICITADO: {S.DESCARTE_LOCAL_APROVADO, S.SEM_CONDICOES, S.CANCELADO},
    S.DESCARTE_LOCAL_APROVADO:   {S.DESCARTADO, S.CANCELADO},
    # Troca antecipada: substituto a caminho → TI recebe → TI envia o defeituoso →
    # fornecedor recebe o defeituoso → fornecedor envia a proposta e conclui.
    S.TROCA_ANT_SUBSTITUTO_ENVIADO:  {S.TROCA_ANT_SUBSTITUTO_RECEBIDO, S.CANCELADO},
    S.TROCA_ANT_SUBSTITUTO_RECEBIDO: {S.TROCA_ANT_DEFEITUOSO_ENVIADO, S.CANCELADO},
    S.TROCA_ANT_DEFEITUOSO_ENVIADO:  {S.TROCA_ANT_DEFEITUOSO_RECEBIDO, S.CANCELADO},
    S.TROCA_ANT_DEFEITUOSO_RECEBIDO: {S.CONCLUIDO, S.CANCELADO},
    S.DESCARTADO:             set(),
    S.CONCLUIDO:              set(),
    S.CANCELADO:              set(),
}

# Quem dispara cada transição de destino: "fornecedor" (portal) ou "ti" (interno).
ATOR = {
    S.RECEBIDO:              "fornecedor",
    S.EM_AVALIACAO:          "fornecedor",
    S.AGUARDANDO_APROVACAO:  "fornecedor",
    S.APROVADO:              "ti",
    S.REPROVADO:             "ti",
    S.EM_REPARO:             "fornecedor",
    S.SEM_REPARO:            "fornecedor",
    S.REPARADO:              "fornecedor",
    S.DEVOLVIDO:             "fornecedor",
    S.SUBSTITUTO_ENVIADO:    "fornecedor",
    S.SEM_CONDICOES:         "fornecedor",
    S.DEVOLVIDO_DESCARTE:    "fornecedor",
    S.DESCARTE_LOCAL_SOLICITADO: "fornecedor",
    S.DESCARTE_LOCAL_APROVADO:   "ti",
    S.TROCA_ANT_SUBSTITUTO_RECEBIDO: "ti",         # TI recebe o substituto
    S.TROCA_ANT_DEFEITUOSO_ENVIADO:  "ti",         # TI envia o defeituoso
    S.TROCA_ANT_DEFEITUOSO_RECEBIDO: "fornecedor", # fornecedor recebe o defeituoso
    S.DESCARTADO:            "ti",
    S.CONCLUIDO:             "ti",
    S.CANCELADO:             "ti",
}

# Override de ator por aresta (origem, destino), quando o mesmo destino pode ser
# alcançado por atores diferentes conforme a origem. Tem prioridade sobre ATOR.
#   • descartado: normalmente o TI confirma (via sem_condicoes); no fluxo de
#     descarte local é o FORNECEDOR quem confirma (após o TI aprovar).
#   • sem_condicoes: normalmente o fornecedor declara (via em_avaliacao); quando
#     o TI recusa um descarte local ele empurra p/ sem_condicoes (devolução).
EDGE_ATOR = {
    (S.DESCARTE_LOCAL_APROVADO, S.DESCARTADO):        "fornecedor",
    (S.DESCARTE_LOCAL_SOLICITADO, S.SEM_CONDICOES):   "ti",
    # Troca antecipada: quem conclui (envia a proposta) é o FORNECEDOR, não o TI.
    (S.TROCA_ANT_DEFEITUOSO_RECEBIDO, S.CONCLUIDO):   "fornecedor",
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

    # ── Notificação interna (sino) + e-mail ao TI ──────────────────────────
    @classmethod
    def _notificar(cls, ordem, novo_status, ator, user, observacao=""):
        """A cada movimentação de manutenção (fornecedor ↔ TI): cria a notificação
        interna do sino e agenda o e-mail ao time de TI. Resiliente — nunca quebra
        a transição; o e-mail respeita o toggle global e roda após o commit."""
        from ProjetoEstoque.models import Notificacao
        status_label = dict(StatusOrdemManutencaoChoices.choices).get(str(novo_status), str(novo_status))
        origem = "Fornecedor" if ator == "fornecedor" else ("TI" if ator == "ti" else "Sistema")
        try:
            item_nome = ordem.item.nome if ordem.item else "Equipamento"
            forn = ordem.fornecedor.nome if ordem.fornecedor else "—"
            def _rev(name):
                try:
                    return reverse(name, args=[ordem.pk])
                except Exception:
                    return ""
            Notificacao.objects.create(
                titulo=f"OS #{ordem.pk} — {status_label}",
                mensagem=f"{item_nome} · {forn} · ação por {origem}.",
                url=_rev("manutencao_recebimento_detail"),
                portal_url=_rev("portal_manutencao_detail"),
                icone="fa-screwdriver-wrench",
                categoria="manutencao",
                fornecedor=ordem.fornecedor,
            )
        except Exception:
            logger.exception("Falha ao criar notificação da OS %s", getattr(ordem, "pk", "?"))

        pk, _status, _ator, _obs = ordem.pk, str(novo_status), ator, (observacao or "").strip()

        def _mail():
            try:
                from services.email_alertas import alerta_movimentacao_manutencao
                alerta_movimentacao_manutencao(pk, _status, _ator, observacao=_obs)
            except Exception:
                logger.exception("Falha ao enviar e-mail da OS %s", pk)

        transaction.on_commit(_mail)

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
            .exclude(status__in=[S.CONCLUIDO, S.CANCELADO, S.DESCARTADO])
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
        cls._notificar(ordem, S.AGUARDANDO_RECEBIMENTO, "ti", user)
        return ordem

    # ── Abertura de TROCA ANTECIPADA (fornecedor, pelo Portal) ─────────────
    @classmethod
    @transaction.atomic
    def abrir_troca_antecipada(cls, *, item_defeituoso, fornecedor, user,
                               sub_modelo, sub_serie="", sub_marca="",
                               sub_data_contrato=None):
        """
        Abre uma OS de troca antecipada: o fornecedor manda um substituto ANTES
        de o defeituoso ser enviado, para não deixar o equipamento parado.

        Só é permitido para equipamentos com status DEFEITO. Cria o Item
        substituto já em estoque como PAUSADO (em trânsito p/ a fazenda),
        herdando categoria/subtipo/localidade/centro de custo/regime do
        defeituoso. Ele é ativado (BACKUP) quando o TI confirmar o recebimento.
        `sub_modelo` também é usado como nome/identificação do item (o sistema
        não pede um nome separado do fornecedor).
        `sub_data_contrato` = data de contrato do equipamento (vira a data de
        entrada da locação do substituto, quando locado).
        """
        if fornecedor is None:
            raise ValidationError("Fornecedor inválido.")
        if not (sub_modelo or "").strip():
            raise ValidationError("Informe o modelo do equipamento substituto.")
        if item_defeituoso.status != StatusItemChoices.DEFEITO:
            raise ValidationError(
                "A troca antecipada só é permitida para equipamentos com status Defeito."
            )
        data_contrato = cls._parse_data(sub_data_contrato)

        # Bloqueia OS aberta duplicada para o mesmo item (evita trocas concorrentes).
        existente = (
            OrdemManutencao.objects
            .filter(item=item_defeituoso)
            .exclude(status__in=[S.CONCLUIDO, S.CANCELADO, S.DESCARTADO])
            .first()
        )
        if existente:
            raise ValidationError(
                f"Já existe uma ordem de manutenção aberta (OS #{existente.pk}) para este equipamento."
            )

        modelo_substituto = (sub_modelo or "").strip() or item_defeituoso.modelo
        substituto = Item(
            nome=modelo_substituto,
            numero_serie=(sub_serie or "").strip() or None,
            marca=(sub_marca or "").strip() or item_defeituoso.marca,
            modelo=modelo_substituto,
            status=StatusItemChoices.PAUSADO,  # a caminho — ativado no recebimento
            fornecedor=fornecedor,
            categoria=item_defeituoso.categoria,
            subtipo=item_defeituoso.subtipo,
            localidade_id=item_defeituoso.localidade_id,
            centro_custo=item_defeituoso.centro_custo,
            locado=item_defeituoso.locado,
            observacoes=(
                f"Substituto (troca antecipada) do equipamento '{item_defeituoso.nome}'"
                + (f" — série {item_defeituoso.numero_serie}" if item_defeituoso.numero_serie else "")
                + "."
            ),
        )
        cls._audit(substituto, user, criando=True)
        substituto.save()

        ordem = OrdemManutencao(
            item=item_defeituoso,
            fornecedor=fornecedor,
            item_substituto=substituto,
            troca_antecipada=True,
            # Data de contrato do equipamento (informada pelo fornecedor); vira a
            # data de entrada da locação do substituto quando o TI o receber.
            substituto_data=data_contrato,
            status=S.TROCA_ANT_SUBSTITUTO_ENVIADO,
        )
        cls._audit(ordem, user, criando=True)
        ordem.save()
        cls._registrar_evento(
            ordem, S.TROCA_ANT_SUBSTITUTO_ENVIADO,
            f"Troca antecipada aberta — substituto '{substituto.nome}'"
            + (f" (série {substituto.numero_serie})" if substituto.numero_serie else "")
            + " a caminho da fazenda.",
            user,
        )
        cls._notificar(ordem, S.TROCA_ANT_SUBSTITUTO_ENVIADO, "fornecedor", user)
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

        # Ator esperado: override por aresta (origem→destino) tem prioridade.
        esperado = EDGE_ATOR.get((ordem.status, novo_status)) or ATOR.get(novo_status)
        if esperado and esperado != ator:
            raise ValidationError("Esta ação não é permitida para o seu perfil.")

        # Efeitos colaterais rodam ANTES de gravar o novo status
        # (handlers leem `ordem.status` = estado de origem). Para handlers que
        # precisam saber o DESTINO da transição (ex.: decidir orçamento), o
        # destino e a observação são injetados no dict `extra` sob chaves
        # prefixadas (`_novo_status`, `_observacao`) para não colidir com
        # campos de formulário reais.
        handler = {
            S.AGUARDANDO_APROVACAO: cls._on_aguardando_aprovacao,
            S.APROVADO:             cls._on_decisao_ti,
            S.REPROVADO:            cls._on_decisao_ti,
            S.EM_REPARO:            cls._on_diagnostico,
            S.REPARADO:             cls._on_reparado,
            S.SEM_REPARO:           cls._on_sem_reparo,
            S.SEM_CONDICOES:        cls._on_sem_condicoes,
            S.DEVOLVIDO_DESCARTE:   cls._on_devolvido_descarte,
            S.DESCARTE_LOCAL_SOLICITADO: cls._on_descarte_local_solicitado,
            S.DESCARTE_LOCAL_APROVADO:   cls._on_descarte_local_aprovado,
            S.DESCARTADO:           cls._on_descartado,
            S.DEVOLVIDO:            cls._on_devolvido,
            S.SUBSTITUTO_ENVIADO:   cls._on_substituto_enviado,
            S.TROCA_ANT_SUBSTITUTO_RECEBIDO: cls._on_troca_ant_substituto_recebido,
            S.TROCA_ANT_DEFEITUOSO_ENVIADO:  cls._on_troca_ant_defeituoso_enviado,
            S.TROCA_ANT_DEFEITUOSO_RECEBIDO: cls._on_troca_ant_defeituoso_recebido,
            S.CANCELADO:            cls._on_cancelado,
            S.CONCLUIDO:            cls._on_concluido,
        }.get(novo_status)
        if handler:
            extra_ctx = dict(extra)
            extra_ctx["_novo_status"] = novo_status
            extra_ctx["_observacao"] = observacao
            handler(ordem, user, extra_ctx)

        ordem.status = novo_status
        if novo_status in (S.CONCLUIDO, S.CANCELADO, S.DESCARTADO):
            ordem.finalizada_em = timezone.now()
        cls._audit(ordem, user, criando=False)
        ordem.save()

        cls._registrar_evento(ordem, novo_status, observacao, user)
        cls._notificar(ordem, novo_status, ator, user, observacao=observacao)
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
            raise ValidationError("Valor informado inválido.")

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

    @staticmethod
    def _parse_meses(v):
        """Lê o tempo de contrato (meses). Retorna um inteiro > 0 ou None."""
        s = (v or "").strip()
        if not s:
            return None
        try:
            meses = int(s)
        except (TypeError, ValueError):
            raise ValidationError("Tempo de contrato em meses inválido.")
        if meses <= 0:
            raise ValidationError("O tempo de contrato deve ser maior que zero.")
        return meses

    @staticmethod
    def _parse_garantia(extra):
        """Lê (tem_garantia, garantia_dias) do form do fornecedor.

        Se marcar que TEM garantia, o prazo em dias é obrigatório e > 0. A
        contagem só começa depois, quando o TI confirma o recebimento.
        """
        tem = SimNaoChoices.SIM if (extra.get("tem_garantia") == "sim") else SimNaoChoices.NAO
        if tem != SimNaoChoices.SIM:
            return SimNaoChoices.NAO, None
        raw = (extra.get("garantia_dias") or "").strip()
        try:
            dias = int(raw)
        except (TypeError, ValueError):
            raise ValidationError("Informe o prazo de garantia em dias.")
        if dias <= 0:
            raise ValidationError("O prazo de garantia deve ser maior que zero.")
        return SimNaoChoices.SIM, dias

    # ── Handlers de efeito colateral ───────────────────────────────────────
    @classmethod
    def _on_diagnostico(cls, ordem, user, extra):
        diag = (extra.get("diagnostico") or "").strip()
        if diag:
            ordem.diagnostico = diag

    @classmethod
    def _on_aguardando_aprovacao(cls, ordem, user, extra):
        """Fornecedor envia o orçamento do reparo ao TI para aprovação — inclusive
        um NOVO orçamento após uma reprovação (revisão). Nesse caso a decisão
        anterior do TI é zerada para reabrir um ciclo de aprovação limpo."""
        cls._on_diagnostico(ordem, user, extra)
        orcamento = cls._parse_valor(extra.get("valor_orcamento"))
        if orcamento is None:
            raise ValidationError("Informe o valor do orçamento do reparo.")
        ordem.valor_orcamento = orcamento

        # Cria um novo registro de orçamento no histórico versionado.
        numero = ordem.orcamentos.count() + 1
        OrdemManutencaoOrcamento.objects.create(
            ordem=ordem,
            numero=numero,
            valor=orcamento,
            status=OrdemManutencaoOrcamento.StatusOrcamentoChoices.PROPOSTO,
        )

        # Novo ciclo de aprovação (inclusive revisão após reprovação): limpa a
        # decisão anterior do TI. O histórico fica na timeline de eventos.
        ordem.aprovado_por = None
        ordem.decisao_em = None

    @classmethod
    def _on_decisao_ti(cls, ordem, user, extra):
        """TI aprova ou reprova o orçamento (registra autor e data da decisão).
        Atualiza o status do último orçamento no histórico.

        IMPORTANTE: não usar `ordem.status` aqui — neste ponto ele ainda é o
        estado de ORIGEM (sempre `aguardando_aprovacao`, já que é o único
        caminho válido para chegar em aprovado/reprovado). O destino real da
        transição vem em `extra['_novo_status']` (injetado por `transicionar`).
        """
        ordem.aprovado_por = user
        ordem.decisao_em = timezone.now()

        destino = extra.get("_novo_status")
        ultimo_orcamento = ordem.orcamentos.last()
        if ultimo_orcamento:
            if destino == S.APROVADO:
                ultimo_orcamento.status = OrdemManutencaoOrcamento.StatusOrcamentoChoices.APROVADO
                ultimo_orcamento.motivo_rejeicao = None
            elif destino == S.REPROVADO:
                ultimo_orcamento.status = OrdemManutencaoOrcamento.StatusOrcamentoChoices.REPROVADO
                motivo = (extra.get("_observacao") or "").strip()
                if motivo:
                    ultimo_orcamento.motivo_rejeicao = motivo
            ultimo_orcamento.save(update_fields=["status", "motivo_rejeicao"])

    @classmethod
    def _on_reparado(cls, ordem, user, extra):
        """Fornecedor conclui o reparo: valor do conserto + valor total (extras)."""
        conserto = cls._parse_valor(extra.get("valor_conserto"))
        total = cls._parse_valor(extra.get("valor_total"))
        if conserto is None:
            conserto = ordem.valor_orcamento  # fallback: usa o orçamento aprovado
        if conserto is None:
            raise ValidationError("Informe o valor do conserto.")
        ordem.valor_conserto = conserto
        # O valor total inclui extras (película, capa, etc.) e nunca é menor que o conserto.
        ordem.valor_total = total if total is not None else conserto
        # Garantia do reparo (a contagem só inicia na confirmação do TI).
        ordem.tem_garantia, ordem.garantia_dias = cls._parse_garantia(extra)

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
    def _registrar_avaliacao_descarte(cls, ordem, extra):
        """Grava o motivo (diagnóstico) e o valor da avaliação do descarte.
        Reaproveita os campos da avaliação: o motivo vai em `diagnostico` e o
        valor em `valor_avaliacao_tecnica` (mesmo campo do fluxo de reprovação,
        que também é um desfecho sem reparo). Tolera reenvio vazio: se o form não
        trouxer os dados (ex.: o TI recusando um descarte local já preenchido),
        mantém o que já estava gravado."""
        motivo = (extra.get("diagnostico") or "").strip() or (ordem.diagnostico or "").strip()
        if not motivo:
            raise ValidationError("Informe o diagnóstico técnico (motivo do descarte).")
        ordem.diagnostico = motivo
        valor = cls._parse_valor(extra.get("valor_orcamento"))
        if valor is None:
            valor = ordem.valor_avaliacao_tecnica
        if valor is None:
            raise ValidationError("Informe o valor da avaliação para o descarte.")
        ordem.valor_avaliacao_tecnica = valor

    @classmethod
    def _on_sem_condicoes(cls, ordem, user, extra):
        """Fornecedor declara que o equipamento não tem condições de reparo. O
        próximo passo é o fornecedor DEVOLVER o equipamento à fazenda
        (devolvido_descarte) para o TI receber e descartar. Também é o destino
        quando o TI RECUSA um descarte local — aí os campos já estão preenchidos
        (o helper tolera reenvio vazio)."""
        cls._registrar_avaliacao_descarte(ordem, extra)

    @classmethod
    def _on_devolvido_descarte(cls, ordem, user, extra):
        """Fornecedor devolve o equipamento à fazenda para o TI descartar,
        informando a localidade de destino (onde o TI vai receber e armazenar).
        O item segue em MANUTENCAO (em trânsito) até o TI confirmar o recebimento."""
        loc_id = cls._loc_id(extra.get("localidade_devolucao"))
        if not loc_id:
            raise ValidationError("Informe a localidade de destino da devolução.")
        ordem.devolucao_localidade_id = loc_id

    @classmethod
    def _on_descarte_local_solicitado(cls, ordem, user, extra):
        """Fornecedor declara sem condições de reparo e SOLICITA descartar no
        próprio local (sem devolver ao TI). Fica aguardando a aprovação do TI.
        Usa os mesmos campos da avaliação (motivo + valor)."""
        cls._registrar_avaliacao_descarte(ordem, extra)

    @classmethod
    def _on_descarte_local_aprovado(cls, ordem, user, extra):
        """TI aprova o descarte local — registra autor e data da decisão. O
        equipamento só passa a DESCARTE quando o fornecedor confirmar."""
        cls._on_decisao_ti(ordem, user, extra)

    @classmethod
    def _on_descartado(cls, ordem, user, extra):
        """Confirma o descarte: o equipamento vai para o status DESCARTE (fim de
        vida) e registra-se uma movimentação de retorno com o custo informado.
        Se locado, o signal de Item congela o período de aluguel. Dois caminhos:
          • do fornecedor, via descarte_local_aprovado (descartou no próprio local);
          • do TI, via devolvido_descarte (recebeu o equipamento e o armazena para
            descarte, aplicando a localidade que o fornecedor informou na devolução)."""
        descarte_local = ordem.status == S.DESCARTE_LOCAL_APROVADO
        item = ordem.item
        item.status = StatusItemChoices.DESCARTE
        campos = ["status", "atualizado_por", "updated_at"]
        # Recebimento p/ descarte: armazena o item na localidade informada pelo fornecedor.
        if not descarte_local and ordem.devolucao_localidade_id:
            item.localidade_id = ordem.devolucao_localidade_id
            campos.append("localidade")
        cls._audit(item, user, criando=False)
        item.save(update_fields=campos)

        if descarte_local:
            obs = f"Descarte local realizado pelo fornecedor (autorizado pelo TI). OS #{ordem.pk}."
        else:
            obs = f"Equipamento recebido e armazenado para descarte. OS #{ordem.pk}."
        if ordem.diagnostico:
            obs += f" Motivo: {ordem.diagnostico}."
        if ordem.valor_avaliacao_tecnica:
            obs += f" Valor informado: R$ {ordem.valor_avaliacao_tecnica}."
        cls._mov_retorno(ordem, item, StatusItemChoices.DESCARTE, obs, user,
                         custo=ordem.valor_avaliacao_tecnica)

    @classmethod
    def _on_devolvido(cls, ordem, user, extra):
        # Na devolução, o fornecedor informa a localidade de destino. O custo do
        # retorno (reparo_valor, usado na movimentação de retorno) depende do caminho:
        #   • vindo de REPARADO  → valor total (conserto + extras)
        #   • vindo de REPROVADO → valor da avaliação técnica
        loc_id = cls._loc_id(extra.get("localidade_devolucao"))
        if loc_id:
            ordem.devolucao_localidade_id = loc_id

        if ordem.status == S.REPROVADO:
            avaliacao = cls._parse_valor(extra.get("valor_avaliacao_tecnica"))
            if avaliacao is not None:
                ordem.valor_avaliacao_tecnica = avaliacao
            ordem.reparo_valor = ordem.valor_avaliacao_tecnica or Decimal("0.00")
        else:
            # Compatibilidade: aceita reparo_valor enviado direto; senão usa o total/conserto.
            valor_reparo = cls._parse_valor(extra.get("reparo_valor"))
            if valor_reparo is not None:
                ordem.reparo_valor = valor_reparo
            elif ordem.valor_total is not None:
                ordem.reparo_valor = ordem.valor_total
            elif ordem.valor_conserto is not None:
                ordem.reparo_valor = ordem.valor_conserto

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

        # Tempo de contrato em meses — obrigatório só quando a troca é locação
        # (alimenta Locacao.tempo_locado → vencimento do contrato nos dashboards).
        tempo_meses = None
        if locado == SimNaoChoices.SIM:
            tempo_meses = cls._parse_meses(extra.get("tempo_contrato_meses"))
            if tempo_meses is None:
                raise ValidationError("Informe o tempo do contrato de locação em meses.")

        ordem.substituto_contrato = contrato
        ordem.substituto_valor = valor
        ordem.substituto_data = data
        ordem.substituto_locado = locado
        ordem.substituto_tempo_meses = tempo_meses
        # Garantia da troca (a contagem só inicia na confirmação do TI).
        ordem.tem_garantia, ordem.garantia_dias = cls._parse_garantia(extra)

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
                tempo_locado=tempo_meses,
                valor_mensal=valor,
                data_entrada=data,
                contrato=contrato,
                fornecedor=ordem.fornecedor,
            )
            cls._audit(locacao, user, criando=True)
            locacao.save()

    # ── Troca antecipada ───────────────────────────────────────────────────
    @classmethod
    def _on_troca_ant_substituto_recebido(cls, ordem, user, extra):
        """TI recebe o substituto da troca antecipada: ativa em estoque (BACKUP,
        ou o status escolhido), registra a entrada e, se o defeituoso é locado,
        cria a Locacao do substituto continuando o contrato — assim o custo mensal
        segue no equipamento que está em uso."""
        sub = ordem.item_substituto
        if sub is None:
            raise ValidationError("Substituto não encontrado para esta troca.")
        sub.status = extra.get("status_retorno") or StatusItemChoices.BACKUP
        cls._audit(sub, user, criando=False)
        sub.save(update_fields=["status", "atualizado_por", "updated_at"])
        cls._mov_entrada_substituto(ordem, sub, user)

        # Continuidade da locação: se o defeituoso é locado, o substituto assume
        # o contrato (mesmos termos). O defeituoso congela o aluguel ao ir PAUSADO.
        antigo = ordem.item
        if antigo.locado == SimNaoChoices.SIM and sub.locado == SimNaoChoices.SIM:
            try:
                loc_antiga = antigo.locacao
            except Locacao.DoesNotExist:
                loc_antiga = None
            ja_tem = Locacao.objects.filter(equipamento=sub).exists()
            if loc_antiga and not ja_tem:
                nova = Locacao(
                    equipamento=sub,
                    tempo_locado=loc_antiga.tempo_locado,
                    valor_mensal=loc_antiga.valor_mensal,
                    # Data de contrato informada na abertura (fallback: hoje).
                    data_entrada=ordem.substituto_data or timezone.localdate(),
                    contrato=loc_antiga.contrato,
                    fornecedor=ordem.fornecedor,
                )
                cls._audit(nova, user, criando=True)
                nova.save()

    @classmethod
    def _on_troca_ant_defeituoso_enviado(cls, ordem, user, extra):
        """TI envia o equipamento defeituoso ao fornecedor (prioritário). O item
        vai para MANUTENCAO e registra-se a movimentação de envio."""
        antigo = ordem.item
        antigo.status = StatusItemChoices.MANUTENCAO
        cls._audit(antigo, user, criando=False)
        antigo.save(update_fields=["status", "atualizado_por", "updated_at"])
        cls._mov_envio_manutencao(ordem, antigo, user)

    @classmethod
    def _on_troca_ant_defeituoso_recebido(cls, ordem, user, extra):
        """Fornecedor recebe o equipamento defeituoso → PAUSADO (estoque do
        fornecedor). O signal de Item congela o período de locação, se houver."""
        antigo = ordem.item
        antigo.status = StatusItemChoices.PAUSADO
        cls._audit(antigo, user, criando=False)
        antigo.save(update_fields=["status", "atualizado_por", "updated_at"])

    @classmethod
    def _on_cancelado(cls, ordem, user, extra):
        """Cancelamento. Na troca antecipada, se o substituto ainda não foi
        recebido (nunca entrou em uso), remove o Item substituto criado na
        abertura para não deixar item órfão no inventário."""
        if ordem.troca_antecipada and ordem.status == S.TROCA_ANT_SUBSTITUTO_ENVIADO:
            sub = ordem.item_substituto
            if sub and sub.status == StatusItemChoices.PAUSADO:
                ordem.item_substituto = None  # evita FK órfã no save do transicionar
                sub.delete()

    @classmethod
    def _iniciar_garantia(cls, ordem):
        """Inicia a contagem da garantia na confirmação de recebimento pelo TI.

        A partir daqui o item está na garantia do reparo/troca; quando
        `garantia_fim` for ultrapassado, deixa de estar coberto.
        """
        if ordem.tem_garantia == SimNaoChoices.SIM and ordem.garantia_dias:
            inicio = timezone.localdate()
            ordem.garantia_inicio = inicio
            ordem.garantia_fim = inicio + timedelta(days=ordem.garantia_dias)

    @classmethod
    def _on_concluido(cls, ordem, user, extra):
        antigo = ordem.item

        # A garantia do reparo/troca passa a valer no recebimento pelo TI.
        cls._iniciar_garantia(ordem)

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
            # Distingue o desfecho (reparado x reprovado/avaliação técnica) na auditoria.
            if ordem.valor_avaliacao_tecnica and not ordem.valor_conserto:
                obs = f"Equipamento devolvido SEM reparo (orçamento reprovado pelo TI). OS #{ordem.pk}."
                obs += f" Avaliação técnica: R$ {ordem.valor_avaliacao_tecnica}."
            else:
                obs = f"Equipamento reparado e devolvido ao TI. OS #{ordem.pk}."
                if ordem.reparo_valor:
                    obs += f" Valor total: R$ {ordem.reparo_valor}."
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

        elif ordem.status == S.TROCA_ANT_DEFEITUOSO_RECEBIDO:
            # Troca antecipada: o fornecedor envia a proposta de reparo e encerra.
            # O defeituoso já está PAUSADO no estoque do fornecedor e o substituto
            # já está ativo na fazenda — sem mudança de status de item aqui.
            orcamento = cls._parse_valor(extra.get("valor_orcamento"))
            if orcamento is None:
                raise ValidationError("Informe o valor da proposta de reparo.")
            ordem.valor_orcamento = orcamento
            cls._on_diagnostico(ordem, user, extra)
            # Registra o valor da proposta como gasto de manutenção do equipamento
            # trocado — sem esta movimentação o custo não aparece no histórico do
            # item nem entra no somatório de `custo_manutencao` da tela de detalhe.
            cls._mov_retorno(
                ordem, antigo, antigo.status,
                f"Proposta de reparo recebida do fornecedor (troca antecipada). OS #{ordem.pk}. Valor: R$ {orcamento}.",
                user, custo=orcamento,
            )

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
    def _mov_envio_manutencao(cls, ordem, item, user):
        mov = MovimentacaoItem(
            tipo_movimentacao=TipoMovimentacaoChoices.ENVIO_MANUTENCAO,
            item=item,
            quantidade=item.quantidade or 1,
            localidade_origem=item.localidade,
            centro_custo_origem=item.centro_custo,
            fornecedor_manutencao=ordem.fornecedor,
            chamado=ordem.chamado,
            observacao=f"Envio para manutenção (troca antecipada). OS #{ordem.pk}.",
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
