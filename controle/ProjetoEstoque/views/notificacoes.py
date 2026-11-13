"""Notificações internas (sino do topo)."""
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from ..models import Notificacao


@login_required
@require_POST
def notificacoes_marcar_lidas(request):
    """Marca como lidas as notificações do ambiente em que o sino foi aberto
    (`ambiente` no POST: `ti` ou `agro`) — abrir o sino no Agro não pode dar
    por lidas as do TI, que a pessoa nem viu."""
    from services.ambiente_service import AGRO, TI, notificacoes_do_ambiente
    chave = request.POST.get("ambiente")
    chave = chave if chave in (TI, AGRO) else TI
    notificacoes_do_ambiente(Notificacao.objects.filter(lida=False), chave).update(lida=True)
    return JsonResponse({"ok": True})
