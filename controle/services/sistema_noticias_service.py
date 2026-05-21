from collections import defaultdict
from decimal import Decimal
from datetime import timedelta

from django.db.models import Count
from django.urls import reverse, NoReverseMatch
from django.utils import timezone

from ProjetoEstoque.models import (
    Usuario,
    Item,
    Locacao,
    Subtipo,
    Localidade,
    CentroCusto,
    MovimentacaoItem,
    Preventiva,
    PreventivaExecucao,
    Licenca,
    LicencaLote,
    MovimentacaoLicenca,
    StatusUsuarioChoices,
    StatusItemChoices,
    TipoMovLicencaChoices,
    TipoMovimentacaoChoices,
    TipoTransferenciaChoices,
)


class SistemaNoticiasService:
    def __init__(self):
        self.today = timezone.localdate()
        self.now = timezone.now()

    # =========================================================
    # HELPERS
    # =========================================================
    def safe_decimal(self, value):
        if value is None:
            return Decimal("0.00")
        try:
            return Decimal(value)
        except Exception:
            return Decimal("0.00")

    def reverse_first(self, route_names, pk=None):
        for name in route_names:
            try:
                if pk is None:
                    return reverse(name)
                return reverse(name, args=[pk])
            except NoReverseMatch:
                continue
        return "#"

    def percentage_list(self, rows, total_key="total"):
        total_max = max((row.get(total_key, 0) for row in rows), default=0)
        for row in rows:
            row["pct"] = int((row.get(total_key, 0) / total_max) * 100) if total_max > 0 else 0
        return rows

    # =========================================================
    # SNAPSHOTS
    # =========================================================
    def get_active_items_by_user(self):
        latest_by_item = {}
        result = defaultdict(list)

        qs = (
            MovimentacaoItem.objects
            .select_related("item", "usuario")
            .filter(item__isnull=False)
            .order_by("item_id", "-created_at", "-id")
        )

        for mov in qs:
            if mov.item_id in latest_by_item:
                continue

            latest_by_item[mov.item_id] = mov

            if not mov.usuario_id:
                continue

            if mov.tipo_movimentacao in [
                TipoMovimentacaoChoices.BAIXA,
                TipoMovimentacaoChoices.ENTRADA,
                TipoMovimentacaoChoices.ENVIO_MANUTENCAO,
            ]:
                continue

            if (
                mov.tipo_movimentacao == TipoMovimentacaoChoices.TRANSFERENCIA
                and mov.tipo_transferencia == TipoTransferenciaChoices.DEVOLUCAO
            ):
                continue

            result[mov.usuario_id].append(mov.item)

        return result

    def get_active_licenses_by_user(self):
        processed = set()
        result = defaultdict(list)

        qs = (
            MovimentacaoLicenca.objects
            .select_related("usuario", "licenca", "lote")
            .filter(usuario__isnull=False, licenca__isnull=False)
            .order_by("usuario_id", "licenca_id", "lote_id", "-created_at", "-id")
        )

        for mov in qs:
            key = (mov.usuario_id, mov.licenca_id, mov.lote_id)

            if key in processed:
                continue

            processed.add(key)

            if mov.tipo == TipoMovLicencaChoices.ATRIBUICAO:
                result[mov.usuario_id].append(mov)

        return result

    def get_active_license_monthly_cost(self):
        processed = set()
        total = Decimal("0.00")

        qs = (
            MovimentacaoLicenca.objects
            .select_related("licenca", "lote", "usuario")
            .filter(usuario__isnull=False, licenca__isnull=False)
            .order_by("usuario_id", "licenca_id", "lote_id", "-created_at", "-id")
        )

        for mov in qs:
            key = (mov.usuario_id, mov.licenca_id, mov.lote_id)

            if key in processed:
                continue
            processed.add(key)

            if mov.tipo != TipoMovLicencaChoices.ATRIBUICAO:
                continue

            lote = mov.lote
            custo_base = self.safe_decimal(getattr(mov, "valor_unitario", None))
            quantidade_lote = Decimal("1.00")
            valor_ciclo = custo_base
            periodicidade = ""

            if lote:
                quantidade_lote = self.safe_decimal(getattr(lote, "quantidade_total", None)) or Decimal("1.00")
                valor_ciclo = self.safe_decimal(getattr(lote, "custo_ciclo", None)) or custo_base
                periodicidade = str(getattr(lote, "periodicidade", "") or "").lower()
            else:
                periodicidade = str(getattr(mov.licenca, "periodicidade", "") or "").lower()

            custo_mensal = Decimal("0.00")

            if periodicidade == "anual":
                custo_anual = valor_ciclo / quantidade_lote
                custo_mensal = custo_anual / Decimal("12")
            elif periodicidade == "semestral":
                custo_mensal = custo_base / Decimal("6")
            elif periodicidade == "trimestral":
                custo_mensal = custo_base / Decimal("3")
            else:
                custo_mensal = custo_base

            total += custo_mensal

        return total

    # =========================================================
    # KPIS
    # =========================================================
    def build_kpis(self):
        total_usuarios = Usuario.objects.count()
        usuarios_ativos = Usuario.objects.filter(status=StatusUsuarioChoices.ATIVO).count()
        usuarios_desligados = Usuario.objects.filter(status=StatusUsuarioChoices.DESLIGADO).count()

        total_itens = Item.objects.count()
        itens_ativos = Item.objects.filter(status=StatusItemChoices.ATIVO).count()
        itens_manutencao = Item.objects.filter(
            status__in=[StatusItemChoices.MANUTENCAO, StatusItemChoices.DEFEITO]
        ).count()

        total_licencas = Licenca.objects.count()
        total_lotes_licenca = LicencaLote.objects.count()

        preventivas_vencidas = Preventiva.objects.filter(data_proxima__lt=self.today).count()
        preventivas_programadas = Preventiva.objects.count()

        ultimos_7 = self.today - timedelta(days=7)
        ultimos_30 = self.today - timedelta(days=30)

        mov_itens_7d = MovimentacaoItem.objects.filter(created_at__date__gte=ultimos_7).count()
        mov_licencas_7d = MovimentacaoLicenca.objects.filter(created_at__date__gte=ultimos_7).count()
        usuarios_novos_30d = Usuario.objects.filter(created_at__date__gte=ultimos_30).count()
        itens_novos_30d = Item.objects.filter(created_at__date__gte=ultimos_30).count()

        custo_locacoes = (
            Locacao.objects.filter(valor_mensal__isnull=False)
            .count()
        )
        total_locacao_mensal = Decimal("0.00")
        for loc in Locacao.objects.filter(valor_mensal__isnull=False):
            total_locacao_mensal += self.safe_decimal(loc.valor_mensal)

        total_licenca_mensal = self.get_active_license_monthly_cost()
        burn_rate_total = total_locacao_mensal + total_licenca_mensal

        active_items_by_user = self.get_active_items_by_user()
        active_licenses_by_user = self.get_active_licenses_by_user()

        desligados_com_pendencia = 0
        for usuario in Usuario.objects.filter(status=StatusUsuarioChoices.DESLIGADO):
            if active_items_by_user.get(usuario.pk) or active_licenses_by_user.get(usuario.pk):
                desligados_com_pendencia += 1

        return {
            "total_usuarios": total_usuarios,
            "usuarios_ativos": usuarios_ativos,
            "usuarios_desligados": usuarios_desligados,
            "total_itens": total_itens,
            "itens_ativos": itens_ativos,
            "itens_manutencao": itens_manutencao,
            "total_licencas": total_licencas,
            "total_lotes_licenca": total_lotes_licenca,
            "preventivas_vencidas": preventivas_vencidas,
            "preventivas_programadas": preventivas_programadas,
            "mov_itens_7d": mov_itens_7d,
            "mov_licencas_7d": mov_licencas_7d,
            "usuarios_novos_30d": usuarios_novos_30d,
            "itens_novos_30d": itens_novos_30d,
            "total_locacao_mensal": total_locacao_mensal,
            "total_licenca_mensal": total_licenca_mensal,
            "burn_rate_total": burn_rate_total,
            "desligados_com_pendencia": desligados_com_pendencia,
            "locacoes_ativas": custo_locacoes,
        }

    # =========================================================
    # CARROSSEL
    # =========================================================
    def build_hero_slides(self, kpi):
        slides = []

        slides.append({
            "tone": "danger" if kpi["preventivas_vencidas"] > 0 else "success",
            "icon": "fa-screwdriver-wrench",
            "eyebrow": "Manutenção preventiva",
            "title": "Preventivas exigindo atenção",
            "headline": f"{kpi['preventivas_vencidas']} preventiva(s) vencida(s)",
            "description": (
                "Acompanhe os equipamentos com preventiva vencida e priorize a programação "
                "para reduzir risco operacional."
            ),
            "cta_label": "Ver equipamentos",
            "cta_url": self.reverse_first(["equipamento_list"]),
        })

        slides.append({
            "tone": "warning" if kpi["itens_manutencao"] > 0 else "info",
            "icon": "fa-toolbox",
            "eyebrow": "Situação da frota / ativos",
            "title": "Itens em manutenção e defeito",
            "headline": f"{kpi['itens_manutencao']} item(ns) fora do ideal",
            "description": (
                "Monitore os ativos em manutenção ou com defeito para acelerar a tomada "
                "de decisão e acompanhar liberação."
            ),
            "cta_label": "Abrir equipamentos",
            "cta_url": self.reverse_first(["equipamento_list"]),
        })

        slides.append({
            "tone": "danger" if kpi["desligados_com_pendencia"] > 0 else "success",
            "icon": "fa-user-clock",
            "eyebrow": "Governança e rastreabilidade",
            "title": "Desligados com pendências operacionais",
            "headline": f"{kpi['desligados_com_pendencia']} usuário(s) com item/licença ativa",
            "description": (
                "A tela cruza desligamento com ativos vinculados e ajuda a reduzir risco "
                "de perda patrimonial ou licença sem devolução."
            ),
            "cta_label": "Abrir dashboard usuários",
            "cta_url": self.reverse_first(["usuario_dashboard"]),
        })

        slides.append({
            "tone": "primary",
            "icon": "fa-chart-line",
            "eyebrow": "Dados do sistema",
            "title": "Pulso operacional do ambiente",
            "headline": f"Burn rate mensal estimado: R$ {kpi['burn_rate_total']:.2f}",
            "description": (
                "Valor consolidado de locações e licenças ativas, útil para leitura rápida "
                "do custo recorrente do sistema."
            ),
            "cta_label": "Abrir inteligência",
            "cta_url": self.reverse_first(["sistema_inteligencia_dashboard"]),
        })

        return slides

    def build_ticker(self, kpi):
        items = [
            f"{kpi['mov_itens_7d']} movimentações de itens nos últimos 7 dias",
            f"{kpi['mov_licencas_7d']} movimentações de licenças nos últimos 7 dias",
            f"{kpi['usuarios_novos_30d']} novo(s) usuário(s) nos últimos 30 dias",
            f"{kpi['itens_novos_30d']} novo(s) item(ns) cadastrados nos últimos 30 dias",
            f"{kpi['preventivas_programadas']} preventiva(s) cadastrada(s) no sistema",
            f"{kpi['total_lotes_licenca']} lote(s) de licença cadastrados",
        ]
        return items

    # =========================================================
    # ATUALIZAÇÕES
    # =========================================================
    def build_updates(self, kpi):
        return [
            {
                "title": "Usuários cadastrados recentemente",
                "value": kpi["usuarios_novos_30d"],
                "icon": "fa-user-plus",
                "tone": "info",
                "description": "Novos usuários incluídos nos últimos 30 dias.",
            },
            {
                "title": "Itens cadastrados recentemente",
                "value": kpi["itens_novos_30d"],
                "icon": "fa-box-open",
                "tone": "primary",
                "description": "Novos equipamentos/itens registrados nos últimos 30 dias.",
            },
            {
                "title": "Movimentações de itens",
                "value": kpi["mov_itens_7d"],
                "icon": "fa-right-left",
                "tone": "warning",
                "description": "Movimentações de itens registradas nos últimos 7 dias.",
            },
            {
                "title": "Movimentações de licenças",
                "value": kpi["mov_licencas_7d"],
                "icon": "fa-key",
                "tone": "success",
                "description": "Atribuições e devoluções de licenças nos últimos 7 dias.",
            },
            {
                "title": "Preventivas vencidas",
                "value": kpi["preventivas_vencidas"],
                "icon": "fa-calendar-xmark",
                "tone": "danger",
                "description": "Equipamentos com preventiva vencida exigindo ação.",
            },
            {
                "title": "Desligados com pendência",
                "value": kpi["desligados_com_pendencia"],
                "icon": "fa-user-shield",
                "tone": "danger",
                "description": "Usuários desligados ainda com ativo ou licença vinculada.",
            },
        ]

    # =========================================================
    # FEED DE NOTÍCIAS
    # =========================================================
    def build_news_feed(self):
        feed = []

        # Últimas movimentações de itens
        for mov in (
            MovimentacaoItem.objects
            .select_related("item", "usuario", "localidade_destino", "centro_custo_destino")
            .order_by("-created_at")[:10]
        ):
            item_name = mov.item.nome if mov.item else "Item"
            tipo_label = mov.get_tipo_movimentacao_display()
            destino_local = mov.localidade_destino.local if mov.localidade_destino else "—"
            usuario_nome = mov.usuario.nome if mov.usuario else "—"

            if mov.tipo_movimentacao == TipoMovimentacaoChoices.ENTRADA:
                title = f"Entrada registrada para {item_name}"
            elif mov.tipo_movimentacao == TipoMovimentacaoChoices.BAIXA:
                title = f"Baixa registrada para {item_name}"
            elif mov.tipo_movimentacao == TipoMovimentacaoChoices.TRANSFERENCIA:
                title = f"Transferência registrada para {item_name}"
            elif mov.tipo_movimentacao == TipoMovimentacaoChoices.ENVIO_MANUTENCAO:
                title = f"Envio para manutenção: {item_name}"
            elif mov.tipo_movimentacao == TipoMovimentacaoChoices.RETORNO_MANUTENCAO:
                title = f"Retorno de manutenção: {item_name}"
            else:
                title = f"Movimentação registrada para {item_name}"

            summary = (
                f"{tipo_label} · Quantidade: {mov.quantidade} · "
                f"Usuário: {usuario_nome} · Local destino: {destino_local}"
            )

            feed.append({
                "category": "Movimentação",
                "icon": "fa-right-left",
                "tone": "primary",
                "title": title,
                "summary": summary,
                "timestamp": mov.created_at,
                "timestamp_label": mov.created_at.strftime("%d/%m/%Y %H:%M"),
                "url": self.reverse_first(["movimentacao_detail"], mov.pk),
            })

        # Últimas movimentações de licença
        for mov in (
            MovimentacaoLicenca.objects
            .select_related("licenca", "usuario", "lote")
            .order_by("-created_at")[:10]
        ):
            lic_name = mov.licenca.nome if mov.licenca else "Licença"
            usuario_nome = mov.usuario.nome if mov.usuario else "—"
            tipo_label = mov.get_tipo_display()

            title = f"{tipo_label}: {lic_name}"
            summary = f"Usuário: {usuario_nome} · Lote: #{mov.lote_id or '—'}"

            feed.append({
                "category": "Licenças",
                "icon": "fa-certificate",
                "tone": "success",
                "title": title,
                "summary": summary,
                "timestamp": mov.created_at,
                "timestamp_label": mov.created_at.strftime("%d/%m/%Y %H:%M"),
                "url": self.reverse_first(["licenca_detail"], mov.licenca_id),
            })

        # Últimas preventivas executadas
        for execucao in (
            PreventivaExecucao.objects
            .select_related("preventiva", "preventiva__equipamento")
            .order_by("-created_at")[:10]
        ):
            equipamento = (
                execucao.preventiva.equipamento.nome
                if execucao.preventiva and execucao.preventiva.equipamento
                else "Equipamento"
            )

            title = f"Preventiva executada em {equipamento}"
            summary = (
                f"Execução registrada em {execucao.data_execucao.strftime('%d/%m/%Y')} "
                f"com observação operacional disponível."
            )

            feed.append({
                "category": "Preventivas",
                "icon": "fa-screwdriver-wrench",
                "tone": "warning",
                "title": title,
                "summary": summary,
                "timestamp": execucao.created_at,
                "timestamp_label": execucao.created_at.strftime("%d/%m/%Y %H:%M"),
                "url": self.reverse_first(["equipamento_detalhe", "item_detail"], execucao.preventiva.equipamento_id),
            })

        # Últimos usuários criados
        for usuario in Usuario.objects.order_by("-created_at")[:8]:
            title = f"Usuário cadastrado: {usuario.nome}"
            summary = (
                f"Matrícula: {usuario.matricula or '—'} · "
                f"Status: {usuario.get_status_display()} · "
                f"Centro de custo: {usuario.centro_custo or '—'}"
            )

            feed.append({
                "category": "Cadastros",
                "icon": "fa-user-plus",
                "tone": "info",
                "title": title,
                "summary": summary,
                "timestamp": usuario.created_at,
                "timestamp_label": usuario.created_at.strftime("%d/%m/%Y %H:%M"),
                "url": self.reverse_first(["usuario_detail"], usuario.pk),
            })

        feed = sorted(feed, key=lambda x: x["timestamp"], reverse=True)
        return feed[:18]

    # =========================================================
    # DADOS / PAINÉIS
    # =========================================================
    def build_panels(self):
        # Status dos itens
        itens_status = []
        for value, label in Item._meta.get_field("status").choices:
            total = Item.objects.filter(status=value).count()
            itens_status.append({
                "label": label,
                "value": value,
                "total": total,
            })
        itens_status = self.percentage_list(itens_status, total_key="total")

        # Top subtipos
        top_subtipos = list(
            Subtipo.objects.annotate(total=Count("item"))
            .order_by("-total", "nome")
            .values("nome", "total")[:8]
        )
        top_subtipos = [{"label": row["nome"], "total": row["total"]} for row in top_subtipos]
        top_subtipos = self.percentage_list(top_subtipos)

        # Top localidades por itens
        top_localidades = list(
            Localidade.objects.annotate(total=Count("item"))
            .order_by("-total", "local")
            .values("local", "total")[:8]
        )
        top_localidades = [{"label": row["local"], "total": row["total"]} for row in top_localidades]
        top_localidades = self.percentage_list(top_localidades)

        # Agenda crítica
        agenda_critica = []
        for preventiva in (
            Preventiva.objects
            .select_related("equipamento")
            .filter(data_proxima__isnull=False)
            .order_by("data_proxima")[:8]
        ):
            dias = (preventiva.data_proxima - self.today).days
            agenda_critica.append({
                "item": preventiva.equipamento.nome if preventiva.equipamento else "Equipamento",
                "date": preventiva.data_proxima,
                "dias": dias,
                "status": "vencida" if dias < 0 else ("urgente" if dias <= 7 else "planejada"),
                "url": self.reverse_first(
                    ["equipamento_detalhe", "item_detail"],
                    preventiva.equipamento_id
                ),
            })

        # Governança: desligados com pendência
        active_items_by_user = self.get_active_items_by_user()
        active_licenses_by_user = self.get_active_licenses_by_user()

        governanca = []
        for usuario in Usuario.objects.filter(status=StatusUsuarioChoices.DESLIGADO).order_by("nome"):
            itens = active_items_by_user.get(usuario.pk, [])
            licencas = active_licenses_by_user.get(usuario.pk, [])

            if itens or licencas:
                governanca.append({
                    "name": usuario.nome,
                    "itens": len(itens),
                    "licencas": len(licencas),
                    "url": self.reverse_first(["usuario_detail"], usuario.pk),
                })

        return {
            "itens_status": itens_status,
            "top_subtipos": top_subtipos,
            "top_localidades": top_localidades,
            "agenda_critica": agenda_critica,
            "governanca": governanca[:8],
        }

    # =========================================================
    # BUILD FINAL
    # =========================================================
    def build(self):
        kpi = self.build_kpis()

        return {
            "kpi": kpi,
            "hero_slides": self.build_hero_slides(kpi),
            "ticker_items": self.build_ticker(kpi),
            "updates": self.build_updates(kpi),
            "news_feed": self.build_news_feed(),
            "panels": self.build_panels(),
        }