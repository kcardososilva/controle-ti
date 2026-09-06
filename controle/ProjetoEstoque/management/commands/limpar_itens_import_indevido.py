from django.core.management.base import BaseCommand
from django.db import transaction

from ProjetoEstoque.models import Item


class Command(BaseCommand):
    help = (
        "Remove itens criados por engano quando uma planilha de OUTRO cadastro "
        "(ex.: colaboradores) foi enviada na tela de Importar Planilha de Equipamentos. "
        "So atinge itens com o 'perfil fantasma': nenhum campo de equipamento "
        "preenchido alem do nome (sem numero de serie, modelo, marca, subtipo, "
        "centro de custo ou valor) e observacoes == 'Importado por planilha.'. "
        "Por padrao roda em modo simulacao (dry-run); use --confirmar para apagar de fato."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--confirmar",
            action="store_true",
            help="Sem esta flag o comando so lista o que seria apagado (dry-run).",
        )
        parser.add_argument(
            "--desde",
            type=str,
            default=None,
            help="Filtra created_at >= AAAA-MM-DD (opcional). Sem isso, considera qualquer data.",
        )
        parser.add_argument(
            "--ate",
            type=str,
            default=None,
            help="Filtra created_at <= AAAA-MM-DD (opcional).",
        )

    def handle(self, *args, **options):
        qs = Item.objects.filter(
            numero_serie__isnull=True,
            modelo__isnull=True,
            marca__isnull=True,
            subtipo__isnull=True,
            centro_custo__isnull=True,
            valor__isnull=True,
            observacoes="Importado por planilha.",
        )

        if options["desde"]:
            qs = qs.filter(created_at__date__gte=options["desde"])

        if options["ate"]:
            qs = qs.filter(created_at__date__lte=options["ate"])

        total = qs.count()

        if total == 0:
            self.stdout.write(self.style.SUCCESS("Nenhum item com o perfil de import indevido encontrado."))
            return

        self.stdout.write(f"Itens encontrados com o perfil de import indevido: {total}")

        amostra = list(qs.order_by("created_at").values_list("id", "nome", "created_at")[:15])
        for item_id, nome, created_at in amostra:
            self.stdout.write(f"  [{item_id}] {nome} — criado em {created_at}")

        if total > len(amostra):
            self.stdout.write(f"  ... e mais {total - len(amostra)} registro(s).")

        if not options["confirmar"]:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                "Modo simulacao (dry-run). Nada foi apagado. "
                "Rode novamente com --confirmar para excluir esses registros."
            ))
            return

        with transaction.atomic():
            apagados, _ = qs.delete()

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"{total} item(ns) removido(s) com sucesso ({apagados} registro(s) no total, incluindo historico de status vinculado)."))
