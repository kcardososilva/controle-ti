from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Importa/atualiza Licenças Office (chave por equipamento) a partir de uma planilha "
        ".xlsx — colunas: Produto, Serial, Status, Conta, ID Principal Dell, Data da Licença, "
        "Estação, Usuário vinculado com a máquina. Upsert por Serial; vincula automaticamente "
        "ao Item pelo nome da Estação, quando encontrado. Mesma lógica usada pelo upload via "
        "tela (/licencas-office/importar/)."
    )

    def add_arguments(self, parser):
        parser.add_argument("arquivo", type=str, help="Caminho do arquivo Excel (.xlsx).")
        parser.add_argument(
            "--usuario",
            type=str,
            default=None,
            help="Username a registrar como autor (auditoria). Padrão: o primeiro superusuário.",
        )

    def handle(self, *args, **options):
        from services.licenca_office_import_service import LicencaOfficeImportService

        caminho = Path(options["arquivo"])
        if not caminho.exists():
            raise CommandError(f"Arquivo não encontrado: {caminho.resolve()}")

        User = get_user_model()
        if options["usuario"]:
            user = User.objects.filter(username=options["usuario"]).first()
            if user is None:
                raise CommandError(f'Usuário "{options["usuario"]}" não encontrado.')
        else:
            user = User.objects.filter(is_superuser=True).order_by("id").first()
            if user is None:
                raise CommandError("Nenhum superusuário encontrado — informe --usuario.")

        with open(caminho, "rb") as arquivo:
            try:
                criados, atualizados, vinculados, erros = LicencaOfficeImportService.importar(
                    arquivo=arquivo, user=user,
                )
            except ValidationError as exc:
                raise CommandError("; ".join(exc.messages))

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("=== RESUMO DA IMPORTAÇÃO ==="))
        self.stdout.write(f"Criadas: {criados}")
        self.stdout.write(f"Atualizadas: {atualizados}")
        self.stdout.write(f"Vinculadas automaticamente a um equipamento: {vinculados}")
        self.stdout.write(f"Avisos: {len(erros)}")

        if erros:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING("=== AVISOS ==="))
            for erro in erros:
                self.stdout.write(f" - {erro}")
