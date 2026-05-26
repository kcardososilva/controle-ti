"""
Cria ou atualiza os grupos de permissão do sistema de TI.

Grupos:
  Administrador TI  — acesso completo (add/change/delete/view em todos os modelos)
  Gestor TI         — add/change/view em tudo; delete apenas em operações de baixo risco
  Operador TI       — view em tudo; add/change em movimentações e comentários; sem delete

Uso:
  python manage.py criar_grupos
  python manage.py criar_grupos --limpar   # remove permissões antes de reatribuir
"""

from django.core.management.base import BaseCommand
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType

APP_LABEL = "ProjetoEstoque"

ALL_MODELS = [
    "item", "itemlote", "loteestoque",
    "licenca", "licenalote", "movimentacaolicenca",
    "movimentacaoitem",
    "usuario",
    "centrocusto", "localidade", "fornecedor",
    "categoria", "subtipo", "funcao",
    "locacao", "comentario",
    "preventiva", "preventivaexecucao", "preventivaresposta",
    "checklistmodelo", "checklistpergunta",
    "ciclomanutencao",
    "plantaprojeto",
]

ACOES_COMPLETAS = ["add", "change", "delete", "view"]
ACOES_SEM_DELETE = ["add", "change", "view"]
ACOES_SOMENTE_VIEW = ["view"]

GESTOR_DELETE_PERMITIDO = [
    "comentario",
    "ciclomanutencao",
    "movimentacaolicenca",
    "preventivaexecucao",
    "preventivaresposta",
]

OPERADOR_ADD_CHANGE = [
    "movimentacaoitem",
    "comentario",
    "preventivaexecucao",
    "preventivaresposta",
]


def _perm(acao, modelo):
    return f"{APP_LABEL}.{acao}_{modelo}"


def _get_perms(codenames):
    perms = []
    for codename in codenames:
        try:
            perms.append(Permission.objects.get(codename=codename))
        except Permission.DoesNotExist:
            pass
    return perms


class Command(BaseCommand):
    help = "Cria ou atualiza grupos de permissão: Administrador TI, Gestor TI, Operador TI"

    def add_arguments(self, parser):
        parser.add_argument(
            "--limpar",
            action="store_true",
            help="Remove todas as permissões dos grupos antes de reatribuir",
        )

    def handle(self, *args, **options):
        limpar = options["limpar"]

        self._criar_grupo_admin(limpar)
        self._criar_grupo_gestor(limpar)
        self._criar_grupo_operador(limpar)

        self.stdout.write(self.style.SUCCESS(
            "\nGrupos criados/atualizados com sucesso:\n"
            "  • Administrador TI — acesso completo\n"
            "  • Gestor TI        — sem delete de dados críticos\n"
            "  • Operador TI      — somente view + movimentações\n\n"
            "Para atribuir um usuário a um grupo:\n"
            "  python manage.py shell\n"
            "  >>> from django.contrib.auth.models import User, Group\n"
            "  >>> u = User.objects.get(username='fulano')\n"
            "  >>> u.groups.set([Group.objects.get(name='Gestor TI')])\n"
        ))

    def _criar_grupo_admin(self, limpar):
        grupo, criado = Group.objects.get_or_create(name="Administrador TI")
        if limpar:
            grupo.permissions.clear()

        codenames = [
            f"{acao}_{modelo}"
            for modelo in ALL_MODELS
            for acao in ACOES_COMPLETAS
        ]
        grupo.permissions.set(_get_perms(codenames))
        self.stdout.write(f"  {'Criado' if criado else 'Atualizado'}: Administrador TI")

    def _criar_grupo_gestor(self, limpar):
        grupo, criado = Group.objects.get_or_create(name="Gestor TI")
        if limpar:
            grupo.permissions.clear()

        codenames = []
        for modelo in ALL_MODELS:
            if modelo in GESTOR_DELETE_PERMITIDO:
                acoes = ACOES_COMPLETAS
            else:
                acoes = ACOES_SEM_DELETE
            for acao in acoes:
                codenames.append(f"{acao}_{modelo}")

        grupo.permissions.set(_get_perms(codenames))
        self.stdout.write(f"  {'Criado' if criado else 'Atualizado'}: Gestor TI")

    def _criar_grupo_operador(self, limpar):
        grupo, criado = Group.objects.get_or_create(name="Operador TI")
        if limpar:
            grupo.permissions.clear()

        codenames = []
        for modelo in ALL_MODELS:
            if modelo in OPERADOR_ADD_CHANGE:
                acoes = ACOES_SEM_DELETE
            else:
                acoes = ACOES_SOMENTE_VIEW
            for acao in acoes:
                codenames.append(f"{acao}_{modelo}")

        grupo.permissions.set(_get_perms(codenames))
        self.stdout.write(f"  {'Criado' if criado else 'Atualizado'}: Operador TI")
