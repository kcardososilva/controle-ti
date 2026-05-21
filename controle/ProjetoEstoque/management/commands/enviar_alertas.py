"""
Comando de gestão para envio dos alertas periódicos por e-mail.

Uso:
    python manage.py enviar_alertas                          # todos os alertas avulsos
    python manage.py enviar_alertas --tipo preventivas
    python manage.py enviar_alertas --tipo estoque
    python manage.py enviar_alertas --tipo licencas
    python manage.py enviar_alertas --tipo diario            # relatório diário consolidado
    python manage.py enviar_alertas --tipo diario --horas 48 # últimas 48h de movimentações
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Envia alertas periódicos por e-mail (preventivas, estoque, licenças, relatório diário)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--tipo",
            choices=["preventivas", "estoque", "licencas", "todos", "diario"],
            default="todos",
            help="Tipo de alerta a enviar (padrão: todos). Use 'diario' para o relatório consolidado.",
        )
        parser.add_argument(
            "--horas",
            type=int,
            default=24,
            help="Janela de horas para baixas/movimentações no relatório diário (padrão: 24).",
        )

    def handle(self, *args, **options):
        from services.email_alertas import (
            alerta_estoque_critico,
            alerta_licencas_desligados,
            alerta_preventivas_proximas,
            relatorio_diario,
        )

        tipo = options["tipo"]
        horas = options["horas"]
        enviados = 0
        erros = 0

        def _rodar(nome, func, **kwargs):
            nonlocal enviados, erros
            self.stdout.write(f"  → {nome}... ", ending="")
            try:
                ok = func(**kwargs)
                if ok:
                    self.stdout.write(self.style.SUCCESS("enviado"))
                    enviados += 1
                else:
                    self.stdout.write(self.style.WARNING("sem dados (nenhum e-mail enviado)"))
            except Exception as exc:
                self.stdout.write(self.style.ERROR(f"ERRO: {exc}"))
                erros += 1

        self.stdout.write(self.style.MIGRATE_HEADING("Controle TI — Alertas por E-mail"))

        if tipo == "diario":
            _rodar(f"Relatório diário consolidado (últimas {horas}h)", relatorio_diario, horas=horas)
        else:
            if tipo in ("preventivas", "todos"):
                _rodar("Preventivas nos próximos 7 dias", alerta_preventivas_proximas)

            if tipo in ("estoque", "todos"):
                _rodar("Estoque crítico (consumo < 2 un.)", alerta_estoque_critico)

            if tipo in ("licencas", "todos"):
                _rodar("Licenças de usuários desligados", alerta_licencas_desligados)

        self.stdout.write("")
        if erros:
            self.stdout.write(self.style.ERROR(f"Concluído com {erros} erro(s). {enviados} enviado(s)."))
        else:
            self.stdout.write(self.style.SUCCESS(f"Concluído. {enviados} alerta(s) enviado(s)."))
