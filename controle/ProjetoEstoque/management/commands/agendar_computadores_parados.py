"""
Tarefa SEMANAL do aviso de computadores parados (backup / pausado) no
Windows Task Scheduler.

Sem esta tarefa, a lista só sai quando alguém movimenta um equipamento (o
signal em `ProjetoEstoque/signals.py`) ou quando alguém roda o comando à
mão — e aí a máquina que foi para backup antes do módulo existir nunca é
cobrada. A revisão semanal é o que transforma o alerta em rotina.

Uso:
    python manage.py agendar_computadores_parados criar              # segunda, 08:00
    python manage.py agendar_computadores_parados criar --dia SEG --hora 07:30
    python manage.py agendar_computadores_parados listar
    python manage.py agendar_computadores_parados remover
    python manage.py agendar_computadores_parados executar           # dispara agora (teste)
"""

import subprocess
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

NOME_TAREFA = "ControleEstoque_ComputadoresParados"

# `schtasks /D` fala inglês independentemente do idioma do Windows, então o
# comando aceita a abreviação em português e traduz — digitar "WED" para
# agendar numa quarta é o tipo de detalhe que faz a tarefa nascer no dia
# errado sem ninguém perceber.
_DIAS = {
    "SEG": "MON", "TER": "TUE", "QUA": "WED", "QUI": "THU",
    "SEX": "FRI", "SAB": "SAT", "DOM": "SUN",
    "MON": "MON", "TUE": "TUE", "WED": "WED", "THU": "THU",
    "FRI": "FRI", "SAT": "SAT", "SUN": "SUN",
}


class Command(BaseCommand):
    help = "Gerencia a tarefa semanal do aviso de computadores parados (backup/pausado)"

    # O `agendar_ninja_licencas` herda tudo isto e só troca estes três nomes e o que `_executar` dispara: o agendamento
    # é o mesmo, e duas cópias do código de `schtasks` divergiriam na primeira correção.
    nome_tarefa = NOME_TAREFA
    tipo_alerta = "computadores"
    nome_comando = "agendar_computadores_parados"
    sem_dados = "nenhum notebook/desktop em backup ou pausado"

    def add_arguments(self, parser):
        parser.add_argument(
            "acao", choices=["criar", "listar", "remover", "executar"],
            help="Ação a realizar: criar | listar | remover | executar",
        )
        parser.add_argument(
            "--dia", default="SEG",
            help="Dia da semana (SEG…DOM ou MON…SUN). Padrão: SEG — a conferência "
                 "cai no começo da semana, antes do fechamento das faturas.",
        )
        parser.add_argument(
            "--hora", default="08:00",
            help="Horário no formato HH:MM (padrão: 08:00).",
        )

    def handle(self, *args, **options):
        acao = options["acao"]

        if acao != "executar" and sys.platform != "win32":
            raise CommandError(
                "Este comando usa o Windows Task Scheduler (schtasks) e só funciona no Windows."
            )

        if acao == "criar":
            self._criar(options["dia"], options["hora"])
        elif acao == "listar":
            self._listar()
        elif acao == "remover":
            self._remover()
        else:
            self._executar()

    # ── Helpers ─────────────────────────────────────────────────────────

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
        result = subprocess.run(
            ["schtasks"] + args, capture_output=True, text=True,
            encoding="cp850", errors="replace",
        )
        return result.returncode, result.stdout, result.stderr

    # ── Ações ───────────────────────────────────────────────────────────

    def _criar(self, dia: str, hora: str):
        dia_schtasks = _DIAS.get(dia.strip().upper())
        if not dia_schtasks:
            raise CommandError(
                f"Dia inválido: {dia}. Use SEG, TER, QUA, QUI, SEX, SAB ou DOM."
            )

        python = sys.executable
        manage = self._manage_py()
        comando = f'"{python}" "{manage}" enviar_alertas --tipo {self.tipo_alerta}'

        self.stdout.write(self.style.MIGRATE_HEADING("Registrando tarefa semanal…"))
        self.stdout.write(f"  Tarefa  : {self.nome_tarefa}")
        self.stdout.write(f"  Quando  : toda semana, {dia_schtasks} às {hora}")
        self.stdout.write(f"  Comando : {comando}")
        self.stdout.write("")

        self._schtasks(["/Delete", "/TN", self.nome_tarefa, "/F"])
        rc, stdout, stderr = self._schtasks([
            "/Create", "/TN", self.nome_tarefa, "/TR", comando,
            "/SC", "WEEKLY", "/D", dia_schtasks, "/ST", hora,
            "/RL", "HIGHEST", "/F",
        ])

        if rc == 0:
            self.stdout.write(self.style.SUCCESS(
                f"Tarefa '{self.nome_tarefa}' criada. A lista sai toda {dia_schtasks} às {hora}."
            ))
            return

        self.stdout.write(self.style.WARNING(f"schtasks retornou código {rc}."))
        if stdout:
            self.stdout.write(stdout.strip())
        if stderr:
            self.stdout.write(self.style.ERROR(stderr.strip()))
        rc2, _, _ = self._schtasks(["/Query", "/TN", self.nome_tarefa, "/FO", "LIST"])
        if rc2 == 0:
            self.stdout.write(self.style.SUCCESS("Tarefa encontrada no agendador — criada mesmo assim."))
        else:
            raise CommandError(
                "Não foi possível criar a tarefa. Tente executar o terminal como Administrador."
            )

    def _listar(self):
        self.stdout.write(self.style.MIGRATE_HEADING(f"Status da tarefa: {self.nome_tarefa}"))
        rc, stdout, _ = self._schtasks(["/Query", "/TN", self.nome_tarefa, "/FO", "LIST", "/V"])
        if rc != 0:
            self.stdout.write(self.style.WARNING(
                f"Tarefa '{self.nome_tarefa}' não encontrada.\n"
                f"Use: python manage.py {self.nome_comando} criar"
            ))
            return

        chaves = [
            "Nome da Tarefa", "Task To Run", "Scheduled Task State", "Next Run Time",
            "Last Run Time", "Last Result", "Run As User", "Schedule Type", "Start Time",
            "TaskName", "Status", "Next Run", "Last Run",
        ]
        for linha in stdout.splitlines():
            if any(chave in linha for chave in chaves):
                self.stdout.write(f"  {linha.strip()}")
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Tarefa encontrada e ativa."))

    def _remover(self):
        self.stdout.write(self.style.MIGRATE_HEADING(f"Removendo tarefa: {self.nome_tarefa}"))
        rc, stdout, stderr = self._schtasks(["/Delete", "/TN", self.nome_tarefa, "/F"])
        if rc == 0:
            self.stdout.write(self.style.SUCCESS(f"Tarefa '{self.nome_tarefa}' removida."))
        elif "não existe" in stderr.lower() or "does not exist" in stderr.lower() or rc == 1:
            self.stdout.write(self.style.WARNING(f"Tarefa '{self.nome_tarefa}' não existia."))
        else:
            self.stdout.write(self.style.ERROR(f"Erro ao remover: {stderr.strip() or stdout.strip()}"))

    def _enviar(self) -> bool:
        from services.email_alertas import alerta_computadores_parados
        return alerta_computadores_parados()

    def _executar(self):
        self.stdout.write(self.style.MIGRATE_HEADING("Enviando a lista agora…"))
        try:
            ok = self._enviar()
        except Exception as exc:
            raise CommandError(f"Falha ao enviar: {exc}") from exc

        if ok:
            self.stdout.write(self.style.SUCCESS("Lista enviada."))
        else:
            self.stdout.write(self.style.WARNING(
                f"Nada enviado — {self.sem_dados} (ou o canal está desativado em Notificações)."
            ))
