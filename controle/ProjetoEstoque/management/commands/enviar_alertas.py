"""
Comando de gestão para envio dos alertas periódicos por e-mail.

Uso:
    python manage.py enviar_alertas                          # todos os alertas avulsos
    python manage.py enviar_alertas --tipo preventivas
    python manage.py enviar_alertas --tipo estoque
    python manage.py enviar_alertas --tipo licencas
    python manage.py enviar_alertas --tipo renovacoes          # licenças a vencer
    python manage.py enviar_alertas --tipo renovacoes --dias 90
    python manage.py enviar_alertas --tipo diario            # relatório diário consolidado
    python manage.py enviar_alertas --tipo diario --horas 48 # últimas 48h de movimentações
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Envia alertas periódicos por e-mail (preventivas, estoque, licenças, relatório diário)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--tipo",
            choices=["preventivas", "estoque", "licencas", "renovacoes", "todos", "diario"],
            default="todos",
            help="Tipo de alerta a enviar (padrão: todos). Use 'diario' para o relatório consolidado.",
        )
        parser.add_argument(
            "--horas",
            type=int,
            default=24,
            help="Janela de horas para baixas/movimentações no relatório diário (padrão: 24).",
        )
        # 60 dias é o padrão porque é a janela em que a renovação ainda é uma
        # DECISÃO: dá tempo de medir uso, cortar assento ocioso e negociar.
        # Com 30 já só dá para aprovar o que o fornecedor mandou.
        parser.add_argument(
            "--dias",
            type=int,
            default=60,
            help="Janela de dias do alerta de renovação de licenças (padrão: 60).",
        )

    def handle(self, *args, **options):
        from services.email_alertas import (
            alerta_estoque_critico,
            alerta_licencas_desligados,
            alerta_licencas_vencendo,
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
                _rodar(
                    "Assentos de licença em colaborador não ativo",
                    alerta_licencas_desligados,
                )

            # Renovação entra em "licencas" E em "renovacoes": quem agenda o
            # alerta de licenças quer as duas perdas da mesma família
            # (assento preso e contrato vencendo), e o tipo próprio existe
            # para poder agendar a renovação numa cadência diferente — ela é
            # semanal por natureza, o assento órfão é diário.
            if tipo in ("licencas", "renovacoes", "todos"):
                _rodar(
                    f"Licenças a renovar nos próximos {options['dias']} dias",
                    alerta_licencas_vencendo, dias=options["dias"],
                )

        self.stdout.write("")
        if erros:
            self.stdout.write(self.style.ERROR(f"Concluído com {erros} erro(s). {enviados} enviado(s)."))
        else:
            self.stdout.write(self.style.SUCCESS(f"Concluído. {enviados} alerta(s) enviado(s)."))
