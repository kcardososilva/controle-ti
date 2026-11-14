"""
Portal do Colaborador (Zelo Agro) — o perfil de quem USA o Agro e só o Agro.

O pedido (2026-11-14): colaboradores que **visualizam, movimentam, cadastram e abrem o detalhe** no Zelo Agro, **sem ver
nada que seja da API** (a integração com a operadora: sessão, consumo, consultas, sincronização, alertas de conectividade e a
configuração) e **sem alcançar o Zelo TI**.

Por que isto NÃO é um perfil de `acesso_restrito_service` (Portal de Contratações, Consulta Quiosque):

  • aqueles perfis prendem a pessoa a UMA tela por prefixo de caminho e trocam o menu lateral por um reduzido. O colaborador usa o
    ambiente Agro inteiro, com o menu dele, e o que o define é a PERMISSÃO — a régua que o módulo já usa em toda view
    (`exige_frota`, `exige_tec`, `has_perm`). Um segundo mecanismo de autorização para o mesmo módulo é a divergência de amanhã;
  • "sem TI" é o que o grupo de `ambiente_service.GRUPOS_SOMENTE_AGRO` já faz: o middleware responde 403 a qualquer caminho fora
    de `/agro/` e o seletor de ambiente só oferece o Agro. O grupo do colaborador entra naquela lista;
  • "sem API" é a AUSÊNCIA das permissões de `ambiente_service.PERMISSOES_API` no grupo — e o resto do sistema respeita isso
    perguntando `pode_ver_api` (telas de chip, veículo, mapa, painéis e menu).

A lista `PERMISSOES` é a fonte única do que o perfil faz; a migration `0209` é o retrato dela (migration não importa service) e há
teste que confere que as duas não divergem. **Estender o perfil é acrescentar um codename aqui** — e uma migration nova, para quem
já tem o grupo; `sincronizar_grupo` (chamado a cada liberação) também acrescenta, mas nunca TIRA o que um administrador pôs à mão,
exceto as permissões de API: essas nunca ficam no grupo.

Mexer em quem recebe o perfil (conceder/revogar) acontece na tela de Acessos (`/acessos/restritos/`), só para administrador.
"""
from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse

from ProjetoEstoque.middleware import _grupos_do_usuario
from ProjetoEstoque.models import (
    GRUPO_COLABORADOR_AGRO,
    GRUPO_CONSULTA_QUIOSQUE,
    GRUPO_FORNECEDOR,
    GRUPO_PARCEIRO_LICENCA,
    GRUPO_PORTAL_CONTRATACAO,
)
from services import ambiente_service as amb
from services.acesso_restrito_service import _SENHA_MIN, esquecer_cache
from services.perfil_acesso_service import _INTERNOS, GRUPO_TV

CHAVE = "colaborador_agro"
ROTULO = "Colaborador do Agro"
DETALHE = "Ver, movimentar e cadastrar no Zelo Agro · sem dados da API · sem o TI"

APP = "ProjetoEstoque"

# O que o colaborador faz. Cada linha é um verbo do pedido; nenhuma permissão de API (`ambiente_service.PERMISSOES_API`) entra
# — há teste que falha se entrar —, nem as de ADMINISTRAR (configuração, catálogo, enlaces), nem as de importar planilha em lote.
PERMISSOES = (
    "acessar_zelo_agro",
    # visualizar e abrir o detalhe
    "ver_frota", "ver_chips", "ver_historico_frota", "ver_tec_estoque", "ver_tec_historico", "exportar_frota",
    # movimentar
    "vincular_chips", "transferir_chips", "instalar_tec_equipamento", "transferir_tec_equipamento", "abrir_tec_manutencao",
    # cadastrar
    "editar_frota", "importar_chips", "cadastrar_tec_equipamento", "gerenciar_tec_estrutura",
)

# Quem já pertence a uma destas áreas NÃO recebe o perfil: ou o middleware dela já o prende em outro lugar (portais externos,
# acessos restritos) e o Agro nunca abriria, ou é do TI de verdade e o perfil o deixaria sem o TI por engano.
_GRUPOS_INCOMPATIVEIS = frozenset({
    GRUPO_FORNECEDOR, GRUPO_PARCEIRO_LICENCA, GRUPO_PORTAL_CONTRATACAO, GRUPO_CONSULTA_QUIOSQUE, GRUPO_TV, *_INTERNOS,
})


def _sem_prefixo(codenames) -> list:
    return [c.split(".", 1)[1] for c in codenames]


def sincronizar_grupo():
    """O `Group` do perfil, com as permissões de `PERMISSOES` e SEM nenhuma de API. Idempotente.

    Aditivo de propósito: não troca o conjunto inteiro (`set`), porque um administrador pode ter acrescentado uma permissão
    de uso pelo Django Admin e apagá-la em silêncio a cada liberação seria desfazer decisão dele. A única coisa que sai é
    permissão de API — o perfil é, por definição, o que não vê API."""
    from django.contrib.auth.models import Group, Permission

    grupo, _ = Group.objects.get_or_create(name=GRUPO_COLABORADOR_AGRO)
    permissoes = {p.codename: p for p in Permission.objects.filter(content_type__app_label=APP, codename__in=PERMISSOES)}
    faltam = [c for c in PERMISSOES if c not in permissoes]
    if faltam:
        # só acontece se o `migrate` não rodou: as permissões nascem dos models.
        raise ValidationError("As permissões do perfil ainda não existem no banco (" + ", ".join(faltam) + "). Rode o migrate.")
    grupo.permissions.add(*permissoes.values())
    grupo.permissions.remove(*Permission.objects.filter(content_type__app_label=APP, codename__in=_sem_prefixo(amb.PERMISSOES_API)))
    return grupo


def e_colaborador(user) -> bool:
    return bool(getattr(user, "is_authenticated", False)) and GRUPO_COLABORADOR_AGRO in _grupos_do_usuario(user)


def usuarios():
    from django.contrib.auth.models import Group

    grupo = Group.objects.filter(name=GRUPO_COLABORADOR_AGRO).first()
    if grupo is None:
        return []
    return list(grupo.user_set.filter(is_active=True).order_by("first_name", "last_name", "username"))


def candidatos():
    """Logins que podem receber o perfil: ativos, não-administradores, ainda fora do grupo e de nenhuma área incompatível."""
    from django.contrib.auth import get_user_model

    return (
        get_user_model().objects.filter(is_active=True, is_staff=False, is_superuser=False)
        .exclude(groups__name=GRUPO_COLABORADOR_AGRO)
        .exclude(groups__name__in=_GRUPOS_INCOMPATIVEIS)
        .order_by("first_name", "last_name", "username")
    )


def conceder(*, chave=CHAVE, username, email="", senha="", user=None):
    """Dá o perfil a `username` (cria o login se não existir). Devolve `(usuario, criado)`; erro vira `ValidationError` com
    texto pronto para a tela.

    Recusas, cada uma por um motivo concreto: administrador (staff passa por cima de tudo, o perfil não valeria nada); quem é de
    portal externo, acesso restrito ou grupo do TI (ver `_GRUPOS_INCOMPATIVEIS`). Login existente e DESATIVADO é reativado —
    é o caminho de volta de quem teve o acesso revogado."""
    from django.contrib.auth import get_user_model

    if chave != CHAVE:
        raise ValidationError("Perfil de acesso desconhecido.")
    username = (username or "").strip()
    email = (email or "").strip()
    senha = (senha or "").strip()
    if not username:
        raise ValidationError("Informe o nome de usuário.")
    if senha and len(senha) < _SENHA_MIN:
        raise ValidationError(f"A senha deve ter ao menos {_SENHA_MIN} caracteres.")

    AuthUser = get_user_model()
    with transaction.atomic():
        grupo = sincronizar_grupo()
        existente = AuthUser.objects.filter(username=username).first()

        if existente:
            if existente.is_staff or existente.is_superuser:
                raise ValidationError(
                    f"“{username}” é administrador do sistema. O perfil de colaborador não se aplica a staff/superusuário — "
                    "ele continuaria alcançando tudo."
                )
            incompativeis = sorted(set(existente.groups.values_list("name", flat=True)) & _GRUPOS_INCOMPATIVEIS)
            if incompativeis:
                raise ValidationError(
                    f"“{username}” já tem outro acesso (“{'”, “'.join(incompativeis)}”). O perfil de colaborador prende o login no "
                    "Zelo Agro: tire a pessoa daquele grupo antes, se é isso mesmo que se quer."
                )
            existente.groups.add(grupo)
            existente.is_active = True
            if email:
                existente.email = email
            if senha:
                existente.set_password(senha)
            existente.save()
            esquecer_cache(existente.pk)
            return existente, False

        if not senha:
            raise ValidationError("Informe uma senha para o novo usuário.")
        novo = AuthUser.objects.create_user(username=username, email=email, password=senha, is_staff=False, is_superuser=False)
        novo.groups.add(grupo)
        esquecer_cache(novo.pk)
        return novo, True


def revogar(*, chave=CHAVE, usuario):
    """Tira o perfil e DESATIVA o login.

    Diferente do que os perfis restritos fazem (eles só tiram o grupo): sem o grupo de `GRUPOS_SOMENTE_AGRO` a pessoa deixa
    de estar presa ao Agro, e o TI é o padrão de todo login interno — "revogar" passaria a ABRIR o TI. Um acesso que some tem
    de reduzir o que a pessoa alcança, nunca ampliar. O login não é apagado (é `criado_por` em meia tabela do sistema); a
    liberação de novo o reativa."""
    if chave != CHAVE:
        raise ValidationError("Perfil de acesso desconhecido.")
    from django.contrib.auth.models import Group

    grupo = Group.objects.filter(name=GRUPO_COLABORADOR_AGRO).first()
    if grupo is not None:
        usuario.groups.remove(grupo)
    usuario.is_active = False
    usuario.save(update_fields=["is_active"])
    esquecer_cache(usuario.pk)


def painel() -> dict:
    """O cartão da tela de Acessos, no mesmo formato dos perfis restritos (`acesso_restrito_service.painel_de_acessos`)."""
    return {
        "chave": CHAVE, "rotulo": ROTULO, "detalhe": DETALHE,
        "inicio": reverse("agro_visao_geral"),
        "usuarios": usuarios(), "candidatos": list(candidatos()),
        "agro": True,
    }
