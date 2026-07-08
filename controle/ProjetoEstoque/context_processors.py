"""Context processors globais (injetados em todos os templates via settings)."""


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
