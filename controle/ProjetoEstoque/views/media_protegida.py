"""
Entrega autenticada dos arquivos de `MEDIA_ROOT` (`/media/...`).

Substitui o `django.views.static.serve` cru que estava em `controle/urls.py`
sem decorador nenhum. A regra de quem pode ler o quê fica em
`services/media_acesso_service.py`; aqui só se aplica a decisão e delega a
entrega do arquivo para o `serve` do Django — que já trata `safe_join`
(barreira de path traversal), Content-Type, If-Modified-Since e Range. Essa
delegação é deliberada: reimplementar o corpo da resposta na mão quebraria o
visualizador de PDF do navegador, que pede faixas de bytes.
"""
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.views.static import serve as _serve_estatico
from django.conf import settings

from services.media_acesso_service import pode_ver_media


@login_required
def media_protegida(request, path):
    """
    GET /media/<path> — só para quem pode ver aquele arquivo.

    Quem não pode recebe 404, não 403: para um fornecedor sondando caminhos de
    outro fornecedor, "não existe" e "existe mas não é seu" têm que ser a
    mesma resposta. Usuário interno passa sempre, então o 404 nunca confunde
    quem é do TI.
    """
    if not pode_ver_media(request.user, path.replace("\\", "/")):
        raise Http404("Arquivo não encontrado.")

    return _serve_estatico(request, path, document_root=settings.MEDIA_ROOT)
