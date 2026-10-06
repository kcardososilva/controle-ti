"""Context processors globais (injetados em todos os templates via settings)."""


# ─── Versão dos arquivos estáticos (cache-busting) ───────────────────────────
#
# O projeto usa CompressedStaticFilesStorage SEM manifesto (decisão registrada
# no settings.py: manifesto quebraria referências estáticas existentes). Sem
# manifesto não há hash no nome do arquivo, então o navegador decide sozinho
# quando revalidar o CSS — e um usuário com a folha antiga em cache vê a tela
# quebrada depois de um deploy.
#
# SUBIR ESTE NÚMERO a cada mudança em qualquer arquivo de static/css/. É a
# única coisa que precisa ser feita: todo template monta a URL como
# `{% static 'css/x.css' %}?v={{ assets_version }}`.
# v5 do design system (tokens.css + ui-kit.css + componentes do base.css
# reescritos). Sem subir isto, quem tem a folha v4 em cache recebe o HTML novo
# com o CSS antigo — e a tela sai quebrada de um jeito difícil de diagnosticar,
# porque no computador de quem fez o deploy está tudo certo.
ASSETS_VERSION = "20261005e"


def assets(request):
    """Expõe `assets_version` para o cache-busting dos arquivos estáticos."""
    return {"assets_version": ASSETS_VERSION}


def _prep(notificacoes, *, portal):
    """Anota cada notificação com `nao_lida` e `link` conforme o público (interno
    ou portal do fornecedor), para o template do sino ser agnóstico."""
    itens = list(notificacoes)
    for n in itens:
        if portal:
            n.nao_lida = not n.lida_fornecedor
            n.link = n.portal_url or n.url or "#"
        else:
            n.nao_lida = not n.lida
            n.link = n.url or "#"
    return itens


def notificacoes(request):
    """Alimenta o sino de notificações do topo com as mais recentes e a contagem
    de não lidas. Usuários internos (TI) veem tudo pelo `base.html`; fornecedores
    veem só as notificações da própria empresa pelo `portal_base.html`."""
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}
    try:
        from .models import Notificacao
        perfil = getattr(user, "perfil_fornecedor", None)
        if perfil is not None and getattr(perfil, "ativo", False):
            # Fornecedor: só as notificações da própria empresa.
            base = Notificacao.objects.filter(fornecedor=perfil.fornecedor)
            recentes = _prep(base[:12], portal=True)
            nao_lidas = base.filter(lida_fornecedor=False).count()
            return {
                "notificacoes_recentes": recentes,
                "notificacoes_nao_lidas": nao_lidas,
                "notif_marcar_url": "portal_notificacoes_marcar_lidas",
            }
        # Interno/TI: todas.
        recentes = _prep(Notificacao.objects.all()[:12], portal=False)
        nao_lidas = Notificacao.objects.filter(lida=False).count()
        return {
            "notificacoes_recentes": recentes,
            "notificacoes_nao_lidas": nao_lidas,
            "notif_marcar_url": "notificacoes_marcar_lidas",
        }
    except Exception:
        return {}


def perfil_acesso(request):
    """
    Alimenta o chip de perfil do topo com o papel REAL do usuário.

    Era uma string fixa "Administrador" no base.html — todo mundo, inclusive
    fornecedor e visualizador de TV, se via como administrador. O cálculo fica
    em services/perfil_acesso_service.py (fonte de verdade: grupo Django +
    perfil externo, a mesma do middleware).
    """
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}
    try:
        from services.perfil_acesso_service import perfil_de_acesso
        return {"perfil_acesso": perfil_de_acesso(user)}
    except Exception:
        # O chip não pode derrubar a tela: sem o contexto, o template cai no
        # rótulo neutro do `{% if %}`.
        return {}
