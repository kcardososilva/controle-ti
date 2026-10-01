"""
ParceiroLicencaAcessoService — provisionamento de logins do Portal de
Licenças Office.

Centraliza a criação/vínculo/suspensão/revogação do acesso (usuário Django +
grupo "Parceiro de Licenças" + PerfilParceiroLicenca). Regra de negócio fora
da view (CLAUDE.md regra 2). Espelha FornecedorAcessoService — mesmo desenho,
outro grupo/perfil.

Um mesmo usuário Django pode ter `perfil_fornecedor` e `perfil_parceiro_licenca`
simultaneamente (são OneToOneField's independentes): é assim que uma mesma
empresa/pessoa (ex.: um fornecedor que também repassa licenças) enxerga os
dois portais com um único login. `vincular_usuario_existente` é o atalho
"inteligente" pra isso — reaproveita um usuário que já tem acesso ao Portal
do Fornecedor em vez de exigir um novo cadastro de usuário/senha.
"""
from django.contrib.auth.models import User, Group
from django.core.exceptions import ValidationError
from django.db import transaction

from ProjetoEstoque.models import PerfilParceiroLicenca, GRUPO_PARCEIRO_LICENCA

_SENHA_MIN = 6


class ParceiroLicencaAcessoService:

    @staticmethod
    def _grupo():
        grupo, _ = Group.objects.get_or_create(name=GRUPO_PARCEIRO_LICENCA)
        return grupo

    @classmethod
    @transaction.atomic
    def provisionar(cls, *, parceiro, username, email="", senha="", user=None):
        """
        Cria um novo usuário OU vincula um usuário existente a `parceiro`.
        Retorna (perfil, criado_bool). Lança ValidationError em caso de erro.
        """
        username = (username or "").strip()
        email = (email or "").strip()
        senha = (senha or "").strip()

        if not username:
            raise ValidationError("Informe o nome de usuário.")
        if senha and len(senha) < _SENHA_MIN:
            raise ValidationError(f"A senha deve ter ao menos {_SENHA_MIN} caracteres.")

        grupo = cls._grupo()
        existente = User.objects.filter(username=username).first()

        # ── Vincular usuário existente ──────────────────────────────────────
        if existente:
            ja = (
                PerfilParceiroLicenca.objects
                .filter(usuario=existente)
                .select_related("parceiro")
                .first()
            )
            if ja:
                raise ValidationError(
                    f"O usuário '{username}' já está vinculado ao Portal de Licenças "
                    f"como parceiro de {ja.parceiro.nome}."
                )
            existente.groups.add(grupo)
            existente.is_active = True
            if email:
                existente.email = email
            if senha:
                existente.set_password(senha)
            existente.save()
            perfil = PerfilParceiroLicenca.objects.create(
                usuario=existente, parceiro=parceiro, ativo=True,
                criado_por=user, atualizado_por=user,
            )
            return perfil, False

        # ── Criar usuário novo ──────────────────────────────────────────────
        if not senha:
            raise ValidationError("Informe uma senha para o novo usuário.")

        novo = User.objects.create_user(
            username=username, email=email, password=senha,
            is_staff=False, is_superuser=False,
        )
        novo.groups.add(grupo)
        perfil = PerfilParceiroLicenca.objects.create(
            usuario=novo, parceiro=parceiro, ativo=True,
            criado_por=user, atualizado_por=user,
        )
        return perfil, True

    @classmethod
    @transaction.atomic
    def vincular_usuario_existente(cls, *, parceiro, usuario, user=None):
        """
        Atalho "inteligente": concede acesso ao Portal de Licenças a um
        usuário Django que JÁ existe (tipicamente já tem `perfil_fornecedor`),
        sem pedir um novo usuário/senha — um único login passa a abrir os
        dois portais.
        """
        ja = (
            PerfilParceiroLicenca.objects
            .filter(usuario=usuario)
            .select_related("parceiro")
            .first()
        )
        if ja:
            raise ValidationError(
                f"O usuário '{usuario.username}' já está vinculado ao Portal de Licenças "
                f"como parceiro de {ja.parceiro.nome}."
            )
        grupo = cls._grupo()
        usuario.groups.add(grupo)
        if not usuario.is_active:
            usuario.is_active = True
            usuario.save(update_fields=["is_active"])
        perfil = PerfilParceiroLicenca.objects.create(
            usuario=usuario, parceiro=parceiro, ativo=True,
            criado_por=user, atualizado_por=user,
        )
        return perfil

    @staticmethod
    def definir_ativo(perfil, ativo, user=None):
        """Suspende/reativa: sincroniza PerfilParceiroLicenca.ativo e User.is_active."""
        perfil.ativo = bool(ativo)
        if user is not None:
            perfil.atualizado_por = user
        perfil.save(update_fields=["ativo", "atualizado_por", "updated_at"])
        perfil.usuario.is_active = bool(ativo)
        perfil.usuario.save(update_fields=["is_active"])
        return perfil

    @staticmethod
    def definir_pode_ver_colaboradores(perfil, valor, user=None):
        """Libera/revoga, pra este parceiro, a visão da lista de colaboradores
        no Portal de Licenças — desligado por padrão (ver docstring do campo)."""
        perfil.pode_ver_colaboradores = bool(valor)
        if user is not None:
            perfil.atualizado_por = user
        perfil.save(update_fields=["pode_ver_colaboradores", "atualizado_por", "updated_at"])
        return perfil

    @staticmethod
    def resetar_senha(perfil, senha):
        senha = (senha or "").strip()
        if not senha or len(senha) < _SENHA_MIN:
            raise ValidationError(f"A senha deve ter ao menos {_SENHA_MIN} caracteres.")
        perfil.usuario.set_password(senha)
        perfil.usuario.save()
        return perfil

    @staticmethod
    def atualizar_email(perfil, email):
        email = (email or "").strip()
        if email and email != perfil.usuario.email:
            perfil.usuario.email = email
            perfil.usuario.save(update_fields=["email"])
        return perfil

    @classmethod
    @transaction.atomic
    def revogar(cls, perfil):
        """
        Revoga o acesso: tira do grupo, desativa o login SE ele não tiver
        também `perfil_fornecedor` ativo (nesse caso o login continua valendo
        pro Portal do Fornecedor), e remove o vínculo. O usuário Django é
        mantido (auditoria/histórico).
        """
        usuario = perfil.usuario
        grupo = Group.objects.filter(name=GRUPO_PARCEIRO_LICENCA).first()
        if grupo:
            usuario.groups.remove(grupo)
        tem_outro_acesso = hasattr(usuario, "perfil_fornecedor") and usuario.perfil_fornecedor.ativo
        if not tem_outro_acesso:
            usuario.is_active = False
            usuario.save(update_fields=["is_active"])
        perfil.delete()
