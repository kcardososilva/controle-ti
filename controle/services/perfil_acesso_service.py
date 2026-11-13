"""
Rótulo do perfil do usuário logado — o texto que aparece no chip do topo.

Antes era a string fixa "Administrador" no `base.html`, exibida para TODO
mundo. Num sistema onde empresas externas entram com login próprio
(fornecedores, parceiro de licenças, visualizador de TV), mostrar
"Administrador" embaixo do nome de um operador é informação errada na tela e
passa a impressão de privilégio que a pessoa não tem.

A fonte de verdade é a MESMA do middleware de acesso: grupo Django + perfil
externo. Não existe segunda régua — se um dia o middleware passar a tratar um
grupo novo, este módulo é o lugar de refletir isso no rótulo.

IMPORTANTE: o rótulo é cosmético. Quem autoriza é o middleware + os
decoradores + o queryset filtrado. Nada aqui deve ser usado para liberar
acesso.
"""
from django.core.cache import cache

from ProjetoEstoque.middleware import _grupos_do_usuario
from ProjetoEstoque.models import (
    GRUPO_CONSULTA_QUIOSQUE,
    GRUPO_FORNECEDOR,
    GRUPO_PARCEIRO_LICENCA,
    GRUPO_PORTAL_CONTRATACAO,
)

# Mesmo nome literal usado por TVAccessMiddleware e media_acesso_service.
GRUPO_TV = "Visualizador TV"

# Grupos internos, do mais para o menos privilegiado. A ordem É a regra de
# precedência: um usuário em "Gestor TI" e "Operador TI" é mostrado como
# Gestor, o papel maior.
_INTERNOS = ("Administrador TI", "Gestor TI", "Operador TI")

_SEM_PERFIL = {
    "rotulo": "Sem perfil",
    "detalhe": "",
    "externo": False,
    "tom": "neutro",
}


def _empresa_fornecedor(user):
    perfil = getattr(user, "perfil_fornecedor", None)
    if perfil is not None and perfil.ativo:
        return perfil.fornecedor.nome
    return ""


def _empresa_parceiro(user):
    perfil = getattr(user, "perfil_parceiro_licenca", None)
    if perfil is not None and perfil.ativo:
        return perfil.fornecedor.nome
    return ""


def _calcular(user) -> dict:
    grupos = set(_grupos_do_usuario(user))

    # ── Externos primeiro ───────────────────────────────────────────────────
    # Um login externo nunca deve ser rotulado por um grupo interno que tenha
    # sobrado de um cadastro antigo: o middleware o prende ao portal de
    # qualquer forma, então o rótulo precisa dizer o que ele REALMENTE é.
    if GRUPO_FORNECEDOR in grupos:
        return {
            "rotulo": "Fornecedor",
            "detalhe": _empresa_fornecedor(user),
            "externo": True,
            "tom": "externo",
        }
    if GRUPO_PARCEIRO_LICENCA in grupos:
        return {
            "rotulo": "Parceiro de Licenças",
            "detalhe": _empresa_parceiro(user),
            "externo": True,
            "tom": "externo",
        }
    if GRUPO_TV in grupos:
        return {
            "rotulo": "Visualizador TV",
            "detalhe": "Somente leitura",
            "externo": True,
            "tom": "tv",
        }
    # Perfis de acesso restrito: pessoas (RH, gestor, terceiro) com UMA tela.
    # Entram aqui, no bloco dos externos, pelo mesmo motivo dos de cima — o
    # middleware prende a pessoa à área dela, e o chip tem de dizer isso em vez
    # de herdar um grupo interno que tenha sobrado de um cadastro antigo.
    if GRUPO_PORTAL_CONTRATACAO in grupos and GRUPO_CONSULTA_QUIOSQUE in grupos:
        # Os dois perfis no mesmo login: o chip diz que a pessoa acumula as duas
        # áreas, em vez de mostrar só a primeira e esconder a outra.
        return {
            "rotulo": "Contratações + Quiosque",
            "detalhe": "Abertura de vagas · consulta de aparelhos",
            "externo": True,
            "tom": "externo",
        }
    if GRUPO_PORTAL_CONTRATACAO in grupos:
        return {
            "rotulo": "Portal de Contratações",
            "detalhe": "Abertura de vagas",
            "externo": True,
            "tom": "externo",
        }
    if GRUPO_CONSULTA_QUIOSQUE in grupos:
        return {
            "rotulo": "Consulta Quiosque",
            "detalhe": "Somente leitura",
            "externo": True,
            "tom": "tv",
        }

    # ── Internos ────────────────────────────────────────────────────────────
    if user.is_superuser:
        return {
            "rotulo": "Administrador",
            "detalhe": "Acesso total",
            "externo": False,
            "tom": "admin",
        }
    for nome in _INTERNOS:
        if nome in grupos:
            return {
                "rotulo": nome,
                "detalhe": "",
                "externo": False,
                "tom": nome.split()[0].lower(),  # administrador | gestor | operador
            }
    if user.is_staff:
        return {
            "rotulo": "Equipe TI",
            "detalhe": "",
            "externo": False,
            "tom": "operador",
        }
    return dict(_SEM_PERFIL)


def perfil_de_acesso(user) -> dict:
    """
    `{"rotulo", "detalhe", "externo", "tom"}` do usuário.

    Cacheado por 60s na mesma janela de `_grupos_do_usuario`: sem isso o chip
    do topo custaria uma query de grupos + uma do perfil externo em cada
    page-load de cada tela. Mudança de grupo ou de empresa aparece em até 60s
    — o mesmo atraso que o middleware já aceita.
    """
    if not getattr(user, "is_authenticated", False):
        return dict(_SEM_PERFIL)

    chave = f"perfil_acesso_{user.id}"
    dados = cache.get(chave)
    if dados is None:
        dados = _calcular(user)
        cache.set(chave, dados, 60)
    return dados
