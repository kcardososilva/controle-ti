"""
Quem NÃO consegue recuperar a senha sozinho.

A recuperação por link depende de um e-mail no cadastro do login. Sem e-mail, a
tela responde "se existir, enviamos" (de propósito — não pode revelar quais
logins existem) e nada chega. Para a pessoa, parece que o sistema falhou.

Este comando é o outro lado dessa decisão: ele lista, para o TI, exatamente
quem está nessa situação — para o cadastro ser completado ANTES de alguém
precisar.

Uso:
  python manage.py auditar_recuperacao_senha
  python manage.py auditar_recuperacao_senha --so-externos
"""
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from ProjetoEstoque.models import GRUPO_FORNECEDOR, GRUPO_PARCEIRO_LICENCA

GRUPO_TV = "Visualizador TV"
_EXTERNOS = (GRUPO_FORNECEDOR, GRUPO_PARCEIRO_LICENCA, GRUPO_TV)


class Command(BaseCommand):
    help = "Lista os logins ativos sem e-mail — que não conseguem usar o 'esqueci minha senha'."

    def add_arguments(self, parser):
        parser.add_argument(
            "--so-externos",
            action="store_true",
            help="Só os logins de empresas externas (fornecedor, parceiro, TV).",
        )

    def handle(self, *args, **opts):
        qs = (
            User.objects.filter(is_active=True)
            .prefetch_related("groups")
            .order_by("username")
        )

        sem_email, com_email = [], 0
        for u in qs:
            grupos = {g.name for g in u.groups.all()}
            externo = bool(grupos & set(_EXTERNOS))
            if opts["so_externos"] and not externo:
                continue
            if not (u.email or "").strip():
                sem_email.append((u, "externo" if externo else "interno", grupos))
            else:
                com_email += 1

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Recuperação de senha por link"))
        self.stdout.write(f"  Logins ativos COM e-mail (conseguem recuperar): {com_email}")
        self.stdout.write(f"  Logins ativos SEM e-mail (NÃO conseguem):       {len(sem_email)}")

        if not sem_email:
            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS(
                "  Todos os logins ativos têm e-mail. Nada a corrigir."
            ))
            return

        self.stdout.write("")
        self.stdout.write(self.style.WARNING(
            "  Estes logins pedem redefinição, recebem a mesma tela de sucesso e\n"
            "  nunca recebem o e-mail. Cadastre o endereço no admin do Django\n"
            "  (ou avise que a troca deles continua sendo pelo TI):"
        ))
        self.stdout.write("")
        for u, escopo, grupos in sem_email:
            rotulo = ", ".join(sorted(grupos)) or ("staff" if u.is_staff else "sem grupo")
            nome = u.get_full_name() or "—"
            self.stdout.write(f"    • {u.username:<24} [{escopo}]  {nome}  ({rotulo})")
        self.stdout.write("")
