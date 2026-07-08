"""Notificações internas (sino do topo)."""
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from ..models import Notificacao


@login_required
@require_POST
def notificacoes_marcar_lidas(request):
    """Marca todas as notificações como lidas (chamado ao abrir o painel do sino)."""
    Notificacao.objects.filter(lida=False).update(lida=True)
    return JsonResponse({"ok": True})
