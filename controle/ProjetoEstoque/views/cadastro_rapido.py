"""Cadastro rápido de subtipo e localidade (popup do formulário de item). A regra vive em
`services/cadastro_rapido_service.py`; aqui só a porta HTTP: método, login e o JSON de volta."""
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from services import cadastro_rapido_service as rapido


def _resposta(resultado):
    if resultado.ok:
        return JsonResponse({"ok": True, "id": resultado.id, "texto": resultado.texto, "ja_existia": resultado.ja_existia})
    return JsonResponse({"ok": False, "erros": resultado.erros}, status=400)


@login_required
@require_POST
def subtipo_cadastro_rapido(request):
    return _resposta(rapido.criar_subtipo(request.POST, request.user))


@login_required
@require_POST
def localidade_cadastro_rapido(request):
    return _resposta(rapido.criar_localidade(request.POST, request.user))
