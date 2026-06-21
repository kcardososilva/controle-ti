"""
Importa a planilha CSV de dispositivos do NinjaOne para o sistema (via CLI).

Uso:
    python manage.py importar_ninja_csv caminho/Devices.csv
"""
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Importa a planilha CSV de dispositivos exportada do NinjaOne."

    def add_arguments(self, parser):
        parser.add_argument("caminho", help="Caminho do arquivo CSV exportado do NinjaOne.")

    def handle(self, *args, **options):
        from services.ninja_service import importar_csv

        caminho = options["caminho"]
        try:
            with open(caminho, "rb") as f:
                res = importar_csv(f)
        except FileNotFoundError:
            raise CommandError(f"Arquivo não encontrado: {caminho}")
        except OSError as exc:
            raise CommandError(f"Não foi possível ler o arquivo: {exc}")

        if not res.get("ok"):
            raise CommandError(res.get("erro") or "Falha na importação.")

        self.stdout.write(self.style.SUCCESS(
            f"OK: {res['total']} dispositivo(s) "
            f"({res['criados']} novo(s), {res['atualizados']} atualizado(s)) | "
            f"{res['vinculados']} vinculado(s) ao estoque | "
            f"{res['sem_serie']} sem número de série."
        ))
        if res.get("locais"):
            self.stdout.write("Por local: " + ", ".join(
                f"{k}={v}" for k, v in sorted(res["locais"].items(), key=lambda x: -x[1])
            ))
