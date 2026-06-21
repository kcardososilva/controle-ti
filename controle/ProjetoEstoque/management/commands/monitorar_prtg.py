"""
Coleta o status PRTG de TODOS os devices monitorados e registra cada MUDANÇA de
status como um evento no histórico (ItemPRTGHistorico), datando pelo momento real
da transição reportado pelo PRTG.

Diferente da tela de detalhe (que só gravava ao ser aberta), este comando deve
rodar periodicamente para que o status seja registrado como eventos que ocorrem,
mantendo o histórico completo para os relatórios de disponibilidade.

Uso:
    python manage.py monitorar_prtg

Agendamento (Agendador de Tarefas do Windows — executar a cada 5 minutos),
dentro de controle/, em um terminal Administrador:

    schtasks /Create /TN "ControleTI - Monitorar PRTG" /SC MINUTE /MO 5 ^
      /TR "C:\\caminho\\python.exe C:\\caminho\\manage.py monitorar_prtg" /F
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Coleta o status PRTG dos equipamentos monitorados e registra mudanças no histórico."

    def handle(self, *args, **options):
        from services.prtg_monitor_service import coletar_status

        stats = coletar_status()

        if not stats.get("ok"):
            self.stderr.write(self.style.ERROR(f"Falha na coleta PRTG: {stats.get('erro')}"))
            return

        self.stdout.write(self.style.SUCCESS(
            f"Monitoração PRTG concluída: "
            f"{stats['devices']} device(s) no PRTG, "
            f"{stats['vinculados']} vinculado(s) a equipamento(s), "
            f"{stats['eventos']} evento(s) novo(s) registrado(s)."
        ))
