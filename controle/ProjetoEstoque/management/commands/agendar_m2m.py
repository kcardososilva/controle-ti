"""
Registra/lista/remove a tarefa agendada de `monitorar_m2m` no Agendador de
Tarefas do Windows (schtasks). Mesmo padrão de `agendar_chamados`/`agendar_prtg`.

Sem esta tarefa rodando, os chips TIM M2M NÃO são consultados: o monitoramento só
acontece quando alguém aperta "Atualizar" na tela. (A faixa de saúde da integração
mostra a hora da última execução e avisa quando a tarefa está parada.) É ESTA tarefa
que sincroniza sozinho, com o navegador fechado — nenhuma tela dispara consulta.

O intervalo da tarefa é só o "tique": a cada tique o comando olha quais chips já
passaram do intervalo configurado na tela de Configuração e consulta esses. A
frequência POR CHIP não vem daqui. O padrão é 1 minuto (o ciclo cabe no tique e se
limita ao limite de requisições por minuto da API); se o tique for ≤ 5 min a tarefa
usa `pythonw` (sem janela de console a cada execução) quando ele existir.

Uso:
    python manage.py agendar_m2m criar                  # a cada 1 min
    python manage.py agendar_m2m criar --intervalo 5    # a cada 5 min
    python manage.py agendar_m2m listar
    python manage.py agendar_m2m remover
    python manage.py agendar_m2m executar               # um ciclo agora (teste)

A tarefa não se registra sozinha em produção — rode `criar` na máquina do servidor.
"""

import subprocess
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

NOME_TAREFA = "ControleEstoque_MonitorarM2M"
TIQUE_PADRAO = 1


class Command(BaseCommand):
    help = "Gerencia a tarefa agendada do monitoramento TIM M2M (Windows)"

    def add_arguments(self, parser):
        parser.add_argument("acao", choices=["criar", "listar", "remover", "executar"])
        parser.add_argument("--intervalo", type=int, default=TIQUE_PADRAO, dest="intervalo",
                            help=f"Intervalo (tique) da tarefa em minutos (padrão: {TIQUE_PADRAO})")

    def handle(self, *args, **options):
        acao = options["acao"]
        if acao == "executar":
            from django.core.management import call_command
            call_command("monitorar_m2m")
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

    def _executavel(self, intervalo):
        """`pythonw` ao lado do `python` quando o tique é curto: a cada minuto uma janela de console piscando
        na tela de quem usa o computador é inaceitável, e o comando não precisa dela (o resultado vai para o
        banco e para logs/erros.log)."""
        if intervalo <= 5:
            sem_janela = Path(sys.executable).with_name("pythonw.exe")
            if sem_janela.exists():
                return str(sem_janela)
        return sys.executable

    def _criar(self, intervalo):
        if intervalo < 1:
            raise CommandError("O intervalo mínimo é de 1 minuto.")
        # `--tique` grava o intervalo da tarefa para o painel saber quando ela parou.
        comando = f'"{self._executavel(intervalo)}" "{self._manage_py()}" monitorar_m2m --tique {intervalo}'
        self.stdout.write(self.style.MIGRATE_HEADING("Registrando tarefa no Agendador..."))
        self.stdout.write(f"  Tarefa    : {NOME_TAREFA}\n  Intervalo : a cada {intervalo} minuto(s)\n  Comando   : {comando}\n")
        self._schtasks(["/Delete", "/TN", NOME_TAREFA, "/F"])
        # Sem `/RL HIGHEST`: o comando só lê a rede e grava o banco; exigir elevação impedia criar a tarefa
        # num terminal comum, e uma tarefa que ninguém consegue registrar é uma tarefa que não existe.
        rc, out, err = self._schtasks([
            "/Create", "/TN", NOME_TAREFA, "/TR", comando, "/SC", "MINUTE", "/MO", str(intervalo), "/F",
        ])
        if rc == 0:
            self.stdout.write(self.style.SUCCESS(f"Tarefa '{NOME_TAREFA}' criada: monitora os chips a cada {intervalo} minuto(s)."))
            return
        # Existir uma tarefa com esse nome NÃO prova que é a nova: se a antiga foi criada num terminal de
        # Administrador, um terminal comum não consegue apagá-la nem trocá-la, e a criação falha com "acesso
        # negado" — a antiga continua lá, no intervalo antigo. Só vale se o comando gravado é o desta chamada.
        rc_q, saida_q, _ = self._schtasks(["/Query", "/TN", NOME_TAREFA, "/FO", "LIST", "/V"])
        if rc_q == 0 and f"monitorar_m2m --tique {intervalo}" in saida_q and ("pythonw" in saida_q.lower() or "python" in saida_q.lower()):
            self.stdout.write(self.style.SUCCESS("Tarefa encontrada no agendador com o comando desta chamada — criação bem-sucedida."))
            return
        raise CommandError(
            f"Não foi possível criar a tarefa ({(err or out).strip()}). "
            + ("A tarefa antiga continua registrada, com o intervalo antigo. " if rc_q == 0 else "")
            + "Rode este comando num terminal aberto como Administrador (a tarefa antiga foi criada assim)."
        )

    def _listar(self):
        rc, out, _ = self._schtasks(["/Query", "/TN", NOME_TAREFA, "/FO", "LIST", "/V"])
        if rc != 0:
            self.stdout.write(self.style.WARNING(
                f"Tarefa '{NOME_TAREFA}' não encontrada. Use: python manage.py agendar_m2m criar\n"
                "Sem ela, os chips TIM M2M só são consultados quando alguém atualiza pela tela."
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
