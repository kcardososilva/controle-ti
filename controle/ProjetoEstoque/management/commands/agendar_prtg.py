"""
Comando de gestão para registrar/listar/remover a tarefa agendada do coletor de
status PRTG (`monitorar_prtg`) no Windows Task Scheduler (schtasks).

Sem esta tarefa agendada e rodando, o histórico PRTG nunca é atualizado e o
alerta por e-mail de equipamento offline/instável NUNCA dispara — a coleta é a
única origem de eventos (ver services/prtg_monitor_service.coletar_status).

Uso:
    python manage.py agendar_prtg criar                 # cria tarefa a cada 5 min
    python manage.py agendar_prtg criar --intervalo 10   # a cada 10 min
    python manage.py agendar_prtg listar                 # mostra status da tarefa
    python manage.py agendar_prtg remover                # remove a tarefa agendada
    python manage.py agendar_prtg executar                # roda a coleta agora (teste)
"""

import subprocess
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

NOME_TAREFA = "ControleEstoque_MonitorarPRTG"


class Command(BaseCommand):
    help = "Gerencia a tarefa agendada do coletor de status PRTG no Windows Task Scheduler"

    def add_arguments(self, parser):
        parser.add_argument(
            "acao",
            choices=["criar", "listar", "remover", "executar"],
            help="Ação a realizar: criar | listar | remover | executar",
        )
        parser.add_argument(
            "--intervalo",
            type=int,
            default=5,
            dest="intervalo",
            help="Intervalo de execução em minutos (padrão: 5)",
        )

    def handle(self, *args, **options):
        acao = options["acao"]
        intervalo = options["intervalo"]

        if sys.platform != "win32":
            raise CommandError(
                "Este comando usa o Windows Task Scheduler (schtasks) e só funciona no Windows."
            )

        if acao == "criar":
            self._criar(intervalo)
        elif acao == "listar":
            self._listar()
        elif acao == "remover":
            self._remover()
        elif acao == "executar":
            self._executar()

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _python_exe(self):
        return sys.executable

    def _manage_py(self):
        # commands/ → management/ → ProjetoEstoque/ → controle/ → manage.py
        base = Path(__file__).resolve().parents[4]
        manage = base / "controle" / "manage.py"
        if not manage.exists():
            from django.conf import settings
            manage = Path(settings.BASE_DIR) / "manage.py"
        if not manage.exists():
            raise CommandError(f"manage.py não encontrado. Caminho tentado: {manage}")
        return str(manage)

    def _schtasks(self, args: list[str], capture=True):
        cmd = ["schtasks"] + args
        result = subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            encoding="cp850",
            errors="replace",
        )
        return result.returncode, result.stdout, result.stderr

    # ── Ações ─────────────────────────────────────────────────────────────────

    def _criar(self, intervalo: int):
        python = self._python_exe()
        manage = self._manage_py()
        comando = f'"{python}" "{manage}" monitorar_prtg'

        self.stdout.write(self.style.MIGRATE_HEADING("Registrando tarefa no Task Scheduler…"))
        self.stdout.write(f"  Tarefa    : {NOME_TAREFA}")
        self.stdout.write(f"  Intervalo : a cada {intervalo} minuto(s)")
        self.stdout.write(f"  Python    : {python}")
        self.stdout.write(f"  manage    : {manage}")
        self.stdout.write("")

        self._schtasks(["/Delete", "/TN", NOME_TAREFA, "/F"])

        rc, stdout, stderr = self._schtasks([
            "/Create",
            "/TN", NOME_TAREFA,
            "/TR", comando,
            "/SC", "MINUTE",
            "/MO", str(intervalo),
            "/RL", "HIGHEST",
            "/F",
        ])

        if rc == 0:
            self.stdout.write(self.style.SUCCESS(
                f"Tarefa '{NOME_TAREFA}' criada com sucesso. "
                f"A coleta PRTG rodará a cada {intervalo} minuto(s)."
            ))
        else:
            self.stdout.write(self.style.WARNING(f"schtasks retornou código {rc}."))
            if stdout:
                self.stdout.write(stdout.strip())
            if stderr:
                self.stdout.write(self.style.ERROR(stderr.strip()))
            rc2, out2, _ = self._schtasks(["/Query", "/TN", NOME_TAREFA, "/FO", "LIST"])
            if rc2 == 0:
                self.stdout.write(self.style.SUCCESS("Tarefa encontrada no agendador — criação bem-sucedida."))
            else:
                raise CommandError(
                    "Não foi possível criar a tarefa. "
                    "Tente executar o terminal como Administrador."
                )

    def _listar(self):
        self.stdout.write(self.style.MIGRATE_HEADING(f"Status da tarefa: {NOME_TAREFA}"))
        rc, stdout, stderr = self._schtasks(["/Query", "/TN", NOME_TAREFA, "/FO", "LIST", "/V"])

        if rc == 0:
            linhas_relevantes = [
                "Nome da Tarefa", "Task To Run", "Scheduled Task State",
                "Next Run Time", "Last Run Time", "Last Result",
                "Run As User", "Schedule Type", "Start Time", "Repeat: Every",
                "TaskName", "Status", "Next Run", "Last Run",
            ]
            for linha in stdout.splitlines():
                if any(chave in linha for chave in linhas_relevantes):
                    self.stdout.write(f"  {linha.strip()}")
            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS("Tarefa encontrada e ativa."))
        else:
            self.stdout.write(self.style.WARNING(
                f"Tarefa '{NOME_TAREFA}' não encontrada no Task Scheduler.\n"
                "Use: python manage.py agendar_prtg criar\n"
                "Sem esta tarefa, o histórico PRTG não é atualizado e o alerta de "
                "equipamento offline/instável nunca dispara."
            ))

    def _remover(self):
        self.stdout.write(self.style.MIGRATE_HEADING(f"Removendo tarefa: {NOME_TAREFA}"))
        rc, stdout, stderr = self._schtasks(["/Delete", "/TN", NOME_TAREFA, "/F"])

        if rc == 0:
            self.stdout.write(self.style.SUCCESS(f"Tarefa '{NOME_TAREFA}' removida com sucesso."))
        else:
            if "não existe" in stderr.lower() or "does not exist" in stderr.lower() or rc == 1:
                self.stdout.write(self.style.WARNING(f"Tarefa '{NOME_TAREFA}' não existia no agendador."))
            else:
                self.stdout.write(self.style.ERROR(f"Erro ao remover: {stderr.strip() or stdout.strip()}"))

    def _executar(self):
        """Dispara a coleta PRTG imediatamente (útil para testes)."""
        self.stdout.write(self.style.MIGRATE_HEADING("Executando coleta PRTG agora…"))
        from services.prtg_monitor_service import coletar_status
        stats = coletar_status()

        if not stats.get("ok"):
            raise CommandError(f"Falha na coleta PRTG: {stats.get('erro')}")

        self.stdout.write(self.style.SUCCESS(
            f"Coleta concluída: {stats['devices']} device(s), "
            f"{stats['vinculados']} vinculado(s) a equipamento(s), "
            f"{stats['eventos']} evento(s) novo(s)."
        ))
        self.stdout.write(
            f"  Transições: {stats.get('alarmes', 0)} alarme(s), "
            f"{stats.get('recuperados', 0)} recuperado(s) · "
            f"e-mail {'enviado' if stats.get('email_enviado') else 'não enviado'}."
        )
