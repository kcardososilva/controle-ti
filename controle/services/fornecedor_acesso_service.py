"""
FornecedorAcessoService — provisionamento de logins do Portal do Fornecedor.

Centraliza a criação/vínculo/suspensão/revogação do acesso (usuário Django +
grupo "Fornecedor" + PerfilFornecedor). Regra de negócio fora da view
(CLAUDE.md regra 2). Usado tanto pela tela por-fornecedor quanto pela central
de Acessos de Fornecedor.
"""
from django.contrib.auth.models import User, Group
from django.core.exceptions import ValidationError
from django.db import transaction

from ProjetoEstoque.models import PerfilFornecedor, GRUPO_FORNECEDOR

_SENHA_MIN = 6


class FornecedorAcessoService:

    @staticmethod
    def _grupo():
        grupo, _ = Group.objects.get_or_create(name=GRUPO_FORNECEDOR)
        return grupo

    @classmethod
    @transaction.atomic
    def provisionar(cls, *, fornecedor, username, email="", senha="", user=None):
        """
        Cria um novo usuário OU vincula um usuário existente a `fornecedor`.
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
                PerfilFornecedor.objects
                .filter(usuario=existente)
                .select_related("fornecedor")
                .first()
            )
            if ja:
                raise ValidationError(
                    f"O usuário '{username}' já está vinculado ao fornecedor "
                    f"{ja.fornecedor.nome}."
                )
            existente.groups.add(grupo)
            existente.is_active = True
            if email:
                existente.email = email
            if senha:
                existente.set_password(senha)
            existente.save()
            perfil = PerfilFornecedor.objects.create(
                usuario=existente, fornecedor=fornecedor, ativo=True,
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
        perfil = PerfilFornecedor.objects.create(
            usuario=novo, fornecedor=fornecedor, ativo=True,
            criado_por=user, atualizado_por=user,
        )
        return perfil, True

    @staticmethod
    def definir_ativo(perfil, ativo, user=None):
        """Suspende/reativa: sincroniza PerfilFornecedor.ativo e User.is_active."""
        perfil.ativo = bool(ativo)
        if user is not None:
            perfil.atualizado_por = user
        perfil.save(update_fields=["ativo", "atualizado_por", "updated_at"])
        perfil.usuario.is_active = bool(ativo)
        perfil.usuario.save(update_fields=["is_active"])
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
        Revoga o acesso: tira do grupo, desativa o login e remove o vínculo.
        O usuário Django é mantido (auditoria/histórico).
        """
        usuario = perfil.usuario
        grupo = Group.objects.filter(name=GRUPO_FORNECEDOR).first()
        if grupo:
            usuario.groups.remove(grupo)
        usuario.is_active = False
        usuario.save(update_fields=["is_active"])
        perfil.delete()
