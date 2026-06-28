"""
Reconcilia o Centro de Custo dos itens com o CC do seu detentor atual.

Regra de negócio (decisão do usuário): um equipamento pertence ao centro de
custo do colaborador que o detém. Quando um item NÃO compartilhado está sob posse
de um colaborador cujo CC difere do CC cadastrado no item, o item é reatribuído ao
CC do detentor.

O detentor atual é a última movimentação de transferência (entrega/equipamento)
do item para um usuário, desde que não tenha sido devolvida.

Uso:
    python manage.py reconciliar_cc_itens --dry-run   # apenas relatório
    python manage.py reconciliar_cc_itens             # aplica as reatribuições
"""
from django.core.management.base import BaseCommand
from django.db.models import OuterRef, Subquery

from ProjetoEstoque.models import (
    Item,
    MovimentacaoItem,
    TipoMovimentacaoChoices,
    TipoTransferenciaChoices,
)


class Command(BaseCommand):
    help = "Reatribui o centro de custo dos itens ao CC do detentor atual."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Não grava nada — apenas mostra o que seria reatribuído.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        vinculo = (
            MovimentacaoItem.objects
            .filter(
                item_id=OuterRef("pk"),
                usuario__isnull=False,
                tipo_movimentacao__in=[
                    TipoMovimentacaoChoices.TRANSFERENCIA,
                    TipoMovimentacaoChoices.TRANSFERENCIA_EQUIPAMENTO,
                ],
            )
            .order_by("-created_at", "-pk")
        )

        itens = (
            Item.objects
            .filter(compartilhado=False)
            .annotate(
                det_usuario_id=Subquery(vinculo.values("usuario_id")[:1]),
                det_tipo_transf=Subquery(vinculo.values("tipo_transferencia")[:1]),
            )
            .select_related("centro_custo")
        )

        # Mapa usuario_id -> centro_custo_id (apenas detentores resolvidos)
        det_ids = {i.det_usuario_id for i in itens if i.det_usuario_id}
        from ProjetoEstoque.models import Usuario
        cc_por_usuario = dict(
            Usuario.objects.filter(pk__in=det_ids)
            .exclude(centro_custo__isnull=True)
            .values_list("pk", "centro_custo_id")
        )
        nome_cc = {}  # cache para relatório

        alteracoes = []
        for item in itens:
            uid = item.det_usuario_id
            if not uid:
                continue
            # Detentor que devolveu o item não conta como detentor atual.
            if item.det_tipo_transf == TipoTransferenciaChoices.DEVOLUCAO:
                continue
            novo_cc_id = cc_por_usuario.get(uid)
            if not novo_cc_id:
                continue
            if item.centro_custo_id == novo_cc_id:
                continue
            alteracoes.append((item, item.centro_custo_id, novo_cc_id))

        if not alteracoes:
            self.stdout.write(self.style.SUCCESS("Nada a reconciliar — todos os itens já seguem o CC do detentor."))
            return

        from ProjetoEstoque.models import CentroCusto
        ccs = {c.pk: c for c in CentroCusto.objects.filter(
            pk__in={a[1] for a in alteracoes if a[1]} | {a[2] for a in alteracoes}
        )}

        def _rotulo(cc_id):
            if not cc_id:
                return "—"
            cc = ccs.get(cc_id)
            return f"{cc.numero}/{cc.departamento}" if cc else str(cc_id)

        self.stdout.write(self.style.WARNING(f"{len(alteracoes)} item(ns) a reatribuir:"))
        for item, antigo, novo in alteracoes:
            self.stdout.write(f"  - {item.nome[:40]:40}  {_rotulo(antigo)}  ->  {_rotulo(novo)}")

        if dry_run:
            self.stdout.write(self.style.NOTICE("\n[dry-run] Nenhuma alteração gravada."))
            return

        atualizados = 0
        for item, _antigo, novo in alteracoes:
            item.centro_custo_id = novo
            item.save(update_fields=["centro_custo", "updated_at"])
            atualizados += 1

        self.stdout.write(self.style.SUCCESS(f"\n{atualizados} item(ns) reatribuído(s) ao CC do detentor."))
