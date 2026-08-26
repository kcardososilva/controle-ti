"""
Recalcula o Centro de Custo dos equipamentos COMPARTILHADOS pela regra da
maioria: o CC do item passa a ser o do departamento com MAIS colaboradores
vinculados ativos (ItemColaborador.ativo=True).

A regra é a mesma aplicada automaticamente em toda movimentação por
`MovimentacaoEstoqueService.centro_custo_majoritario()` — este comando existe
para (a) corrigir o passivo de itens que ficaram com o CC parado antes da
regra existir e (b) reconciliar depois de alterações feitas fora do fluxo de
movimentação (ex.: vínculo aberto/encerrado direto no admin).

Itens sem maioria apurável (nenhum vínculo ativo, ou nenhum colaborador
vinculado com CC cadastrado) são PRESERVADOS como estão — o CC nunca é
zerado, para não sumir do rateio de custos.

O PMB é reajustado junto sempre que o CC muda, pelo mesmo critério do
serviço de movimentação (`pmb_por_centro_custo`).

Uso:
    python manage.py recalcular_cc_compartilhados --dry-run   # apenas relatório
    python manage.py recalcular_cc_compartilhados             # aplica as alterações
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from ProjetoEstoque.models import Item
from services.movimentacao_service import MovimentacaoEstoqueService


class Command(BaseCommand):
    help = "Recalcula o centro de custo dos equipamentos compartilhados pela regra da maioria."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Não grava nada — apenas mostra o que seria alterado.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        itens = (
            Item.objects
            .filter(compartilhado=True)
            .select_related("centro_custo")
            .order_by("nome")
        )

        if not itens:
            self.stdout.write(self.style.SUCCESS("Nenhum equipamento compartilhado cadastrado."))
            return

        self.stdout.write(self.style.NOTICE(f"{len(itens)} equipamento(s) compartilhado(s) analisado(s).\n"))

        alteracoes = []
        sem_maioria = 0

        for item in itens:
            cc_maioria, distribuicao = MovimentacaoEstoqueService.centro_custo_majoritario(item)

            if cc_maioria is None:
                sem_maioria += 1
                continue

            if item.centro_custo_id == cc_maioria.pk:
                continue

            alteracoes.append((item, item.centro_custo, cc_maioria, distribuicao))

        if sem_maioria:
            self.stdout.write(
                f"{sem_maioria} item(ns) sem vínculo ativo com CC — centro de custo preservado.\n"
            )

        if not alteracoes:
            self.stdout.write(self.style.SUCCESS(
                "Nada a corrigir — todos os compartilhados já estão no CC majoritário."
            ))
            return

        self.stdout.write(self.style.WARNING(f"{len(alteracoes)} item(ns) com CC divergente da maioria:\n"))

        for item, cc_atual, cc_novo, distribuicao in alteracoes:
            atual = f"{cc_atual.numero}/{cc_atual.departamento}" if cc_atual else "— sem CC —"
            novo = f"{cc_novo.numero}/{cc_novo.departamento}"

            self.stdout.write(f"  {item.nome[:40]}  (NS: {item.numero_serie or '—'})")
            self.stdout.write(f"      de : {atual}")
            self.stdout.write(f"      para: {novo}")
            self.stdout.write(
                "      vínculos: "
                + ", ".join(f"{cc.numero}={total}" for cc, total in distribuicao.items())
            )

        if dry_run:
            self.stdout.write(self.style.NOTICE("\n[dry-run] Nenhuma alteração gravada."))
            return

        atualizados = 0

        with transaction.atomic():
            for item, _cc_atual, cc_novo, _distribuicao in alteracoes:
                item.centro_custo = cc_novo

                update_fields = ["centro_custo", "updated_at"]

                novo_pmb = MovimentacaoEstoqueService.pmb_por_centro_custo(cc_novo)
                if item.pmb != novo_pmb:
                    item.pmb = novo_pmb
                    update_fields.append("pmb")

                item.save(update_fields=update_fields)
                atualizados += 1

        self.stdout.write(self.style.SUCCESS(f"\n{atualizados} item(ns) atualizado(s)."))
