"""
Um ciclo de monitoramento dos chips TIM M2M.

Consulta sessão e consumo dos chips "vencidos" (os que já passaram do intervalo
configurado em Monitoramento TIM M2M > Configuração), grava cada consulta no
histórico, abre/encerra ocorrências e avisa por e-mail o que for novo. Pausa a
cada 401 ou 429 e nunca martela uma credencial recusada.

Uso:
    python manage.py monitorar_m2m             # um ciclo agora
    python manage.py monitorar_m2m --tique 1   # idem, informando o tique da tarefa (1 min)
    python manage.py monitorar_m2m --todos --usuario fulano   # "Atualizar todos": TODOS os chips, de uma vez

`--todos` é o que o botão "Atualizar todos" da tela dispara (em outro processo, para o navegador não ficar preso).
Respeita o limite de requisições da API como qualquer ciclo: com 200 chips leva alguns minutos, e o que não couber no
tempo do ciclo volta para a fila normal. `--usuario` é o login de quem pediu (as consultas ficam gravadas com ele).

Quem roda isto de tempos em tempos é o Agendador de Tarefas do Windows (a tarefa
não se registra sozinha em produção):
    python manage.py agendar_m2m criar

O código de saída é diferente de zero quando falta configuração, quando a API
recusa a credencial ou quando o ciclo termina em erro — é o que o Agendador
mostra em "Last Result", então um monitoramento parado não passa batida. Pausa por
limite de requisições (429) e "já em andamento" saem com 0.

A saída usa só caracteres do cp1252: o console do Windows (onde o Agendador e o
terminal do servidor rodam) não codifica seta, "check" nem emoji, e o comando
quebraria justamente ao relatar o resultado.
"""

import io
import sys

from django.core.management.base import BaseCommand, CommandError

from services import m2m_monitoramento_service as m2m


def _cp1252(texto: str) -> str:
    """Troca o que o console do Windows não consegue escrever (ex.: um emoji no nome do chip)."""
    return str(texto).encode("cp1252", errors="replace").decode("cp1252")


class Command(BaseCommand):
    help = (
        "Executa um ciclo de monitoramento dos chips TIM M2M (sessao e consumo). Com --tique (a tarefa do Agendador), "
        "repete um ciclo por minuto ate o fim do tique."
    )

    def __init__(self, stdout=None, stderr=None, *args, **kwargs):
        # Sob `pythonw` (a tarefa de 1 em 1 minuto, sem janela) `sys.stdout` e `sys.stderr` são None, e o
        # `OutputWrapper` do Django quebraria no primeiro `write`. O resultado já vai para o banco
        # (`M2MExecucao`) e para o log; aqui só se evita o erro.
        if stdout is None and sys.stdout is None:
            stdout = io.StringIO()
        if stderr is None and sys.stderr is None:
            stderr = io.StringIO()
        super().__init__(stdout, stderr, *args, **kwargs)

    def add_arguments(self, parser):
        parser.add_argument(
            "--tique", type=int, default=None, dest="tique",
            help="Intervalo, em minutos, da tarefa do Agendador (só para o painel saber se ela parou). "
                 "Se omitido, o valor gravado não muda.",
        )
        parser.add_argument(
            "--todos", action="store_true", dest="todos",
            help="Atualiza TODOS os chips consultaveis (o 'Atualizar todos' da tela), e nao so os vencidos.",
        )
        parser.add_argument(
            "--usuario", default="", dest="usuario",
            help="Login de quem pediu o --todos: as consultas ficam gravadas com ele.",
        )

    def handle(self, *args, **options):
        tique = options["tique"]
        if tique is not None and tique < 1:
            raise CommandError("--tique deve ser de pelo menos 1 minuto.")
        if options["usuario"] and not options["todos"]:
            raise CommandError("--usuario so vale junto com --todos.")

        if options["todos"]:
            resumos = [m2m.atualizar_todos(self._usuario(options["usuario"]))]
            self._relatar(resumos)
            return

        # Só a TAREFA (que informa `--tique`) roda ciclos em sequência até o fim do tique. Uma execução manual, sem
        # `--tique` (o terminal, o `agendar_m2m executar`), faz UM ciclo e volta: senão um teste rápido ficaria preso
        # por quase todo o tique da tarefa.
        if tique is None:
            resumos = [m2m.executar_ciclo("agendada")]
        else:
            resumos = m2m.executar_varios_ciclos("agendada", tique_minutos=tique)
        self._relatar(resumos)

    @staticmethod
    def _usuario(login: str):
        """Quem pediu o `--todos`. Login que não existe é erro, não "anônimo": a consulta sem autor esconderia quem gastou o
        limite da API."""
        if not login:
            return None
        from django.contrib.auth import get_user_model

        usuario = get_user_model().objects.filter(**{get_user_model().USERNAME_FIELD: login}).first()
        if usuario is None:
            raise CommandError(_cp1252(f"Usuario '{login}' nao encontrado."))
        return usuario

    def _relatar(self, resumos):
        # A tarefa pode rodar vários ciclos seguidos (um por minuto, até o fim do tique): o código de saída
        # é de problema se QUALQUER deles pediu atenção, e o texto é o do último.
        problema = next((r for r in resumos if r.exige_atencao), None)
        if problema is not None:
            # Uma mensagem só, no stderr, e código de saída 1 (sem traceback).
            if not problema.executou:
                raise CommandError(_cp1252(f"Monitoramento TIM M2M parado: {problema.mensagem}"))
            self.stdout.write(_cp1252(problema.texto()))
            raise CommandError(_cp1252(problema.erro or "O ciclo terminou com erro."))

        resumo = resumos[-1]
        texto = _cp1252(f"{len(resumos)} ciclo(s). " + resumo.texto() if len(resumos) > 1 else resumo.texto())
        self.stdout.write(texto if resumo.executou else self.style.WARNING(texto))
