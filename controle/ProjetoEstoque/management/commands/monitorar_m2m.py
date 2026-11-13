"""
Um ciclo de monitoramento dos chips TIM M2M.

Consulta sessão e consumo dos chips "vencidos" (os que já passaram do intervalo
configurado em Monitoramento TIM M2M > Configuração), grava cada consulta no
histórico, abre/encerra ocorrências e avisa por e-mail o que for novo. Pausa a
cada 401 ou 429 e nunca martela uma credencial recusada.

Uso:
    python manage.py monitorar_m2m             # um ciclo agora
    python manage.py monitorar_m2m --tique 15  # idem, informando o tique da tarefa

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

from django.core.management.base import BaseCommand, CommandError

from services import m2m_monitoramento_service as m2m


def _cp1252(texto: str) -> str:
    """Troca o que o console do Windows não consegue escrever (ex.: um emoji no nome do chip)."""
    return str(texto).encode("cp1252", errors="replace").decode("cp1252")


class Command(BaseCommand):
    help = "Executa um ciclo de monitoramento dos chips TIM M2M (sessao e consumo)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tique", type=int, default=None, dest="tique",
            help="Intervalo, em minutos, da tarefa do Agendador (só para o painel saber se ela parou). "
                 "Se omitido, o valor gravado não muda.",
        )

    def handle(self, *args, **options):
        tique = options["tique"]
        if tique is not None and tique < 1:
            raise CommandError("--tique deve ser de pelo menos 1 minuto.")

        resumo = m2m.executar_ciclo("agendada", tique_minutos=tique)

        if resumo.exige_atencao:
            # Uma mensagem só, no stderr, e código de saída 1 (sem traceback).
            if not resumo.executou:
                raise CommandError(_cp1252(f"Monitoramento TIM M2M parado: {resumo.mensagem}"))
            self.stdout.write(_cp1252(resumo.texto()))
            raise CommandError(_cp1252(resumo.erro or "O ciclo terminou com erro."))

        texto = _cp1252(resumo.texto())
        self.stdout.write(texto if resumo.executou else self.style.WARNING(texto))
