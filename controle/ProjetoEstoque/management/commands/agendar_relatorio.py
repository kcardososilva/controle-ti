"""
Comando de gestão para registrar/listar/remover a tarefa agendada de relatório diário
no Windows Task Scheduler (schtasks).

Uso:
    python manage.py agendar_relatorio criar              # cria tarefa às 08:00 todos os dias
    python manage.py agendar_relatorio criar --hora 07:30
    python manage.py agendar_relatorio criar --hora 08:00 --horas-janela 48
    python manage.py agendar_relatorio listar             # mostra status da tarefa
    python manage.py agendar_relatorio remover            # remove a tarefa agendada
    python manage.py agendar_relatorio executar           # dispara o relatório agora (teste)
"""

import subprocess
import sys
import os
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

NOME_TAREFA = "ControleEstoque_RelatorioDiario"


class Command(BaseCommand):
    help = "Gerencia a tarefa agendada de relatório diário no Windows Task Scheduler"

    def add_arguments(self, parser):
        parser.add_argument(
            "acao",
            choices=["criar", "listar", "remover", "executar"],
            help="Ação a realizar: criar | listar | remover | executar",
        )
        parser.add_argument(
            "--hora",
            default="08:00",
            help="Horário de execução diária no formato HH:MM (padrão: 08:00)",
        )
        parser.add_argument(
            "--horas-janela",
            type=int,
            default=24,
            dest="horas_janela",
            help="Janela de horas para baixas/movimentações no relatório (padrão: 24)",
        )

    def handle(self, *args, **options):
        acao = options["acao"]
        hora = options["hora"]
        horas_janela = options["horas_janela"]

        if sys.platform != "win32":
            raise CommandError(
                "Este comando usa o Windows Task Scheduler (schtasks) e só funciona no Windows."
            )

        if acao == "criar":
            self._criar(hora, horas_janela)
        elif acao == "listar":
            self._listar()
        elif acao == "remover":
            self._remover()
        elif acao == "executar":
            self._executar(horas_janela)

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _python_exe(self):
        """Retorna o caminho absoluto do interpretador Python ativo."""
        return sys.executable

    def _manage_py(self):
        """Retorna o caminho absoluto do manage.py do projeto."""
        # manage.py está dois níveis acima do diretório deste arquivo:
        # commands/ → management/ → ProjetoEstoque/ → controle/ → manage.py
        base = Path(__file__).resolve().parents[4]
        manage = base / "controle" / "manage.py"
        if not manage.exists():
            # fallback: procura a partir de BASE_DIR do Django
            from django.conf import settings
            manage = Path(settings.BASE_DIR) / "manage.py"
        if not manage.exists():
            raise CommandError(f"manage.py não encontrado. Caminho tentado: {manage}")
        return str(manage)

    def _schtasks(self, args: list[str], capture=True):
        """Executa schtasks e retorna (returncode, stdout, stderr)."""
        cmd = ["schtasks"] + args
        result = subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            encoding="cp850",   # encoding padrão do console Windows
            errors="replace",
        )
        return result.returncode, result.stdout, result.stderr

    # ── Ações ─────────────────────────────────────────────────────────────────

    def _criar(self, hora: str, horas_janela: int):
        python = self._python_exe()
        manage = self._manage_py()

        # Comando que a tarefa vai executar
        comando = f'"{python}" "{manage}" enviar_alertas --tipo diario --horas {horas_janela}'

        self.stdout.write(self.style.MIGRATE_HEADING("Registrando tarefa no Task Scheduler…"))
        self.stdout.write(f"  Tarefa  : {NOME_TAREFA}")
        self.stdout.write(f"  Horário : diariamente às {hora}")
        self.stdout.write(f"  Janela  : últimas {horas_janela}h")
        self.stdout.write(f"  Python  : {python}")
        self.stdout.write(f"  manage  : {manage}")
        self.stdout.write("")

        # Remove tarefa anterior caso exista (ignora erro se não existe)
        self._schtasks(["/Delete", "/TN", NOME_TAREFA, "/F"])

        # Cria nova tarefa agendada
        rc, stdout, stderr = self._schtasks([
            "/Create",
            "/TN", NOME_TAREFA,
            "/TR", comando,
            "/SC", "DAILY",
            "/ST", hora,
            "/RL", "HIGHEST",      # roda com privilégios elevados
            "/F",                  # sobrescreve se já existe
        ])

        if rc == 0:
            self.stdout.write(self.style.SUCCESS(
                f"Tarefa '{NOME_TAREFA}' criada com sucesso. "
                f"O relatório será enviado diariamente às {hora}."
            ))
        else:
            # schtasks às vezes cria com aviso mas rc != 0 — verifica mesmo assim
            self.stdout.write(self.style.WARNING(f"schtasks retornou código {rc}."))
            if stdout:
                self.stdout.write(stdout.strip())
            if stderr:
                self.stdout.write(self.style.ERROR(stderr.strip()))
            # Verifica se foi criada mesmo assim
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
            # Filtra as linhas mais relevantes
            linhas_relevantes = [
                "Nome da Tarefa", "Task To Run", "Scheduled Task State",
                "Next Run Time", "Last Run Time", "Last Result",
                "Run As User", "Schedule Type", "Start Time",
                # EN labels (depends on locale)
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
                "Use: python manage.py agendar_relatorio criar"
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

    def _executar(self, horas_janela: int):
        """Dispara o relatório diário imediatamente (útil para testes)."""
        self.stdout.write(self.style.MIGRATE_HEADING("Executando relatório diário agora…"))
        from services.email_alertas import relatorio_diario
        try:
            ok = relatorio_diario(horas=horas_janela)
            if ok:
                self.stdout.write(self.style.SUCCESS("Relatório enviado com sucesso."))
            else:
                self.stdout.write(self.style.WARNING("Relatório processado, mas e-mail não foi enviado (verifique as configurações SMTP)."))
        except Exception as exc:
            raise CommandError(f"Falha ao enviar relatório: {exc}") from exc
