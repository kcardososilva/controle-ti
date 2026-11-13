"""
Sincroniza os dispositivos com a API do NinjaOne (via CLI) — a mesma ação do botão
"Sincronizar agora" em /ninja/. Exige que alguém já tenha CONECTADO o sistema ao
NinjaOne (botão "Conectar ao NinjaOne" em /ninja/, feito uma vez por um administrador).

Uso:
    python manage.py sincronizar_ninja                 # sincroniza e grava
    python manage.py sincronizar_ninja --diagnostico   # NÃO grava: mostra o formato da API

Sai com código 1 em qualquer falha (conexão perdida, API fora do ar), para a tarefa
agendada do Windows aparecer como falha em vez de "concluída com sucesso".

O --diagnostico imprime só NOMES de campo e contagens — nunca valores: há login de
usuário e IP de máquina de gente real nessa resposta.
"""
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Sincroniza os dispositivos com a API do NinjaOne (exige a conexão feita em /ninja/)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--diagnostico", action="store_true",
            help="Não grava nada: mostra quantos registros a API devolve e quais campos vêm em cada um.",
        )

    def handle(self, *args, **options):
        from services import ninja_api_service as api

        try:
            if options["diagnostico"]:
                self._diagnostico(api.diagnosticar())
                return
            res = api.sincronizar()
        except api.NinjaErro as exc:
            raise CommandError(str(exc))

        self.stdout.write(self.style.SUCCESS(
            f"OK: {res['total']} dispositivo(s) de {res['lidos_na_api']} lidos na API "
            f"({res['criados']} novo(s), {res['atualizados']} atualizado(s)) | "
            f"{res['vinculados']} vinculado(s) ao estoque | "
            f"{res['sem_serie']} sem numero de serie."
        ))
        for aviso in res.get("avisos", []):
            self.stdout.write(self.style.WARNING(f"AVISO: {aviso}"))

    def _diagnostico(self, d):
        w = self.stdout.write
        w("Organizacoes: %s  campos: %s" % (d["organizacoes"], ", ".join(d["campos_organizacao"]) or "-"))
        w("Locais:       %s  campos: %s" % (d["locais"], ", ".join(d["campos_local"]) or "-"))
        w("Dispositivos: %s  (interpretaveis: %s)" % (d["dispositivos"], d["mapeados"]))
        w("  campos do dispositivo: %s" % (", ".join(d["campos_dispositivo"]) or "-"))
        w("  system: %s" % (", ".join(d["campos_system"]) or "-"))
        w("  os:     %s" % (", ".join(d["campos_os"]) or "-"))
        w("  memory: %s | processors presente: %s" % (", ".join(d["campos_memory"]) or "-", d["tem_processors"]))
        if d["erro_logados"]:
            w(self.style.WARNING("Usuarios logados: indisponivel - %s" % d["erro_logados"]))
        else:
            w("Usuarios logados: %s  campos: %s" % (d["logados"], ", ".join(d["campos_logado"]) or "-"))
