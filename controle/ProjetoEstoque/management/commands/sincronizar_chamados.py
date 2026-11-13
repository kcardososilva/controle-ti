"""
Sincroniza a caixa de e-mail com a conversa dos chamados de contratação.

Lê os e-mails novos da caixa (Microsoft Graph), registra as respostas no chamado
certo, põe em revisão o que não passou nas verificações e reprocessa as falhas.
Sem as chaves `CHAMADOS_GRAPH_*` no .env a sincronização fica desligada — o resto
(envio por SMTP, resposta pela plataforma) funciona do mesmo jeito.

Uso:
    python manage.py sincronizar_chamados                 # uma leitura da caixa
    python manage.py sincronizar_chamados --arquivo a.eml # importa um e-mail salvo (.eml)

Quem roda isto de tempos em tempos é o Agendador de Tarefas do Windows:
    python manage.py agendar_chamados criar

O código de saída é diferente de zero quando a leitura falha — é o que o
Agendador mostra em "Last Result", então uma caixa fora do ar não passa batida.
"""

from django.core.management.base import BaseCommand, CommandError

from services import chamado_email_entrada_service as entrada


class Command(BaseCommand):
    help = "Sincroniza a caixa de e-mail com a conversa dos chamados de contratação."

    def add_arguments(self, parser):
        parser.add_argument(
            "--arquivo", action="append", default=[], dest="arquivos",
            help="Importa um e-mail salvo (.eml) em vez de ler a caixa. Pode repetir.",
        )

    def handle(self, *args, **options):
        if options["arquivos"]:
            self._importar(options["arquivos"])
            return

        resumo = entrada.sincronizar()
        if not resumo.configurada:
            self.stdout.write(self.style.WARNING(
                "Sincronização desligada: faltam CHAMADOS_GRAPH_TENANT_ID, CHAMADOS_GRAPH_CLIENT_ID, "
                "CHAMADOS_GRAPH_CLIENT_SECRET ou CHAMADOS_CAIXA no .env. "
                "Passo a passo: docs/TUTORIAL_CHAMADOS_CONVERSA.md."
            ))
            return
        self.stdout.write(resumo.texto())
        if resumo.erro:
            raise CommandError(f"A leitura da caixa falhou: {resumo.erro}")

    def _importar(self, caminhos):
        for caminho in caminhos:
            try:
                with open(caminho, "rb") as f:
                    raw = f.read()
            except OSError as exc:
                raise CommandError(f"Não consegui ler {caminho}: {exc}") from exc
            r = entrada.tratar_email(raw)
            # Sem seta nem outro caractere fora do cp1252: o console do Windows (onde
            # o Agendador e o terminal do servidor rodam) não o codifica, e o comando
            # quebraria justamente ao relatar o resultado.
            destino = f" - chamado no {r.chamado.pk}" if r.chamado else ""
            self.stdout.write(f"{caminho}: {r.estado}{destino}" + (f" ({r.motivo})" if r.motivo else ""))
