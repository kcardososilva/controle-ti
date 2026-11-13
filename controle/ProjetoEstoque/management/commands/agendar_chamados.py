"""
Registra/lista/remove a tarefa agendada de `sincronizar_chamados` no Agendador
de Tarefas do Windows (schtasks). Mesmo padrão de `agendar_prtg`.

Sem esta tarefa rodando, as respostas por e-mail NÃO aparecem na plataforma: a
sincronização só acontece quando alguém a dispara. (A tela dos chamados mostra a
hora da última leitura e avisa quando ela está parada.)

Uso (terminal como Administrador):
    python manage.py agendar_chamados criar                 # a cada 3 min
    python manage.py agendar_chamados criar --intervalo 5   # a cada 5 min
    python manage.py agendar_chamados listar
    python manage.py agendar_chamados remover
    python manage.py agendar_chamados executar              # uma leitura agora (teste)

A tarefa não se registra sozinha em produção — rode `criar` na máquina do servidor.
"""

import subprocess
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

NOME_TAREFA = "ControleEstoque_SincronizarChamados"


class Command(BaseCommand):
    help = "Gerencia a tarefa agendada da sincronização de e-mail dos chamados (Windows)"

    def add_arguments(self, parser):
        parser.add_argument("acao", choices=["criar", "listar", "remover", "executar"])
        parser.add_argument("--intervalo", type=int, default=3, dest="intervalo",
                            help="Intervalo em minutos (padrão: 3)")

    def handle(self, *args, **options):
        acao = options["acao"]
        if acao == "executar":
            from django.core.management import call_command
            call_command("sincronizar_chamados")
            return
        if sys.platform != "win32":
            raise CommandError("Este comando usa o Agendador de Tarefas do Windows (schtasks).")
        if acao == "criar":
            self._criar(options["intervalo"])
        elif acao == "listar":
            self._listar()
        else:
            self._remover()

    def _manage_py(self):
        base = Path(__file__).resolve().parents[4]
        manage = base / "controle" / "manage.py"
        if not manage.exists():
            from django.conf import settings
            manage = Path(settings.BASE_DIR) / "manage.py"
        if not manage.exists():
            raise CommandError(f"manage.py não encontrado. Caminho tentado: {manage}")
        return str(manage)

    def _schtasks(self, args):
        r = subprocess.run(["schtasks"] + args, capture_output=True, text=True,
                           encoding="cp850", errors="replace")
        return r.returncode, r.stdout, r.stderr

    def _criar(self, intervalo):
        if intervalo < 1:
            raise CommandError("O intervalo mínimo é de 1 minuto.")
        comando = f'"{sys.executable}" "{self._manage_py()}" sincronizar_chamados'
        self.stdout.write(self.style.MIGRATE_HEADING("Registrando tarefa no Agendador…"))
        self.stdout.write(f"  Tarefa    : {NOME_TAREFA}\n  Intervalo : a cada {intervalo} minuto(s)\n  Comando   : {comando}\n")
        self._schtasks(["/Delete", "/TN", NOME_TAREFA, "/F"])
        rc, out, err = self._schtasks([
            "/Create", "/TN", NOME_TAREFA, "/TR", comando,
            "/SC", "MINUTE", "/MO", str(intervalo), "/RL", "HIGHEST", "/F",
        ])
        if rc == 0:
            self.stdout.write(self.style.SUCCESS(f"Tarefa '{NOME_TAREFA}' criada: lê a caixa a cada {intervalo} minuto(s)."))
            return
        if self._schtasks(["/Query", "/TN", NOME_TAREFA])[0] == 0:
            self.stdout.write(self.style.SUCCESS("Tarefa encontrada no agendador — criação bem-sucedida."))
            return
        raise CommandError(f"Não foi possível criar a tarefa ({(err or out).strip()}). Tente como Administrador.")

    def _listar(self):
        rc, out, _ = self._schtasks(["/Query", "/TN", NOME_TAREFA, "/FO", "LIST", "/V"])
        if rc != 0:
            self.stdout.write(self.style.WARNING(
                f"Tarefa '{NOME_TAREFA}' não encontrada. Use: python manage.py agendar_chamados criar\n"
                "Sem ela, as respostas por e-mail não aparecem na plataforma."
            ))
            return
        chaves = ("TaskName", "Nome da Tarefa", "Task To Run", "Scheduled Task State", "Next Run Time",
                  "Last Run Time", "Last Result", "Repeat: Every")
        for linha in out.splitlines():
            if any(c in linha for c in chaves):
                self.stdout.write(f"  {linha.strip()}")
        self.stdout.write(self.style.SUCCESS("Tarefa encontrada."))

    def _remover(self):
        rc, _, err = self._schtasks(["/Delete", "/TN", NOME_TAREFA, "/F"])
        if rc == 0:
            self.stdout.write(self.style.SUCCESS(f"Tarefa '{NOME_TAREFA}' removida."))
        else:
            self.stdout.write(self.style.WARNING(f"Tarefa não removida/inexistente: {err.strip()}"))
