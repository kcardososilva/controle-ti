import re
import time

from django.core.cache import cache
from django.shortcuts import redirect


def _grupos_do_usuario(user):
    """
    Nomes dos grupos do usuário, cacheados por 60s.

    Sem isto, TVAccessMiddleware e FornecedorAccessMiddleware rodariam um
    `user.groups.filter(...).exists()` em TODA requisição autenticada
    (2 queries por page-load). Com o cache, é no máximo 1 query por usuário
    a cada 60s. Mudanças de grupo passam a valer em até 60s.
    """
    chave = f'grupos_usuario_{user.id}'
    nomes = cache.get(chave)
    if nomes is None:
        nomes = list(user.groups.values_list('name', flat=True))
        cache.set(chave, nomes, 60)
    return nomes


# (tentativas_mínimas, segundos_de_espera) — do mais restritivo ao menos
_COOLDOWN = [(10, 600), (5, 60), (3, 10)]
_LOGIN_PATH = '/login/'


def _get_wait(fails: int) -> int:
    return next((s for n, s in _COOLDOWN if fails >= n), 0)


class LoginThrottleMiddleware:
    """
    Cooldown progressivo por IP no endpoint de login.
    Sem dependências externas — usa o cache Django (LocMemCache).

    Tentativas erradas → espera:
      1–2  → sem espera
      3–4  → 10 s
      5–9  → 60 s
      10+  → 10 min

    Sem lockout permanente: o usuário acessa após aguardar o período.
    O contador é zerado automaticamente após um login bem-sucedido.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        is_login_post = (
            request.method == 'POST'
            and request.path.rstrip('/') == _LOGIN_PATH.rstrip('/')
        )

        if is_login_post:
            ip = request.META.get('REMOTE_ADDR', 'unknown')
            cache_key = f'login_throttle_{ip}'
            data = cache.get(cache_key)

            if data:
                fails, unblock_at = data
                remaining = int(unblock_at - time.time())
                if remaining > 0:
                    return self._bloqueado(remaining)

        response = self.get_response(request)

        if is_login_post:
            ip = request.META.get('REMOTE_ADDR', 'unknown')
            cache_key = f'login_throttle_{ip}'

            if response.status_code == 302 and getattr(request, 'user', None) and request.user.is_authenticated:
                # Login bem-sucedido — zera o contador
                cache.delete(cache_key)
            elif response.status_code == 200:
                # Login falhou (form re-renderizado com erros)
                data = cache.get(cache_key, (0, 0))
                fails = data[0] + 1
                wait = _get_wait(fails)
                unblock_at = time.time() + wait if wait else 0
                cache.set(cache_key, (fails, unblock_at), 3600)

        return response

    @staticmethod
    def _bloqueado(remaining: int):
        from django.http import HttpResponse
        html = (
            '<!DOCTYPE html><html lang="pt-BR">'
            '<head><meta charset="UTF-8">'
            f'<meta http-equiv="refresh" content="{remaining};url=/login/">'
            '<title>Acesso temporariamente bloqueado</title>'
            '<style>body{font-family:system-ui,sans-serif;display:flex;align-items:center;'
            'justify-content:center;height:100vh;margin:0;background:#f5f5f7}'
            '.box{text-align:center;padding:2rem;background:#fff;border-radius:16px;'
            'box-shadow:0 4px 24px rgba(0,0,0,.1);max-width:360px}'
            'h2{margin:0 0 .5rem;font-size:1.4rem;color:#1d1d1f}'
            'p{color:#6e6e73;margin:.25rem 0}'
            '.count{font-size:2.5rem;font-weight:700;color:#0071e3;margin:.75rem 0}'
            'small{font-size:.75rem;color:#aaa}</style></head>'
            '<body><div class="box">'
            '<p style="font-size:2rem">🔒</p>'
            '<h2>Muitas tentativas</h2>'
            f'<div class="count">{remaining}s</div>'
            '<p>Redirecionando automaticamente…</p>'
            '<small>Verifique suas credenciais antes de tentar novamente.</small>'
            '</div></body></html>'
        )
        return HttpResponse(html, status=429)


# ─── Middleware: Visualizador TV ──────────────────────────────────────────────

_TV_PERMITIDO = re.compile(
    r'^(/plantas/\d+/tv/'           # modo TV de uma planta
    r'|/plantas/tv/'                # seletor de plantas TV
    r'|/plantas/api/prtg-status/'   # API PRTG (usada pelo canvas TV)
    r'|/static/'                    # arquivos estáticos
    r'|/login/'                     # login
    r'|/logout/'                    # logout
    r')'
)

_GRUPO_TV = 'Visualizador TV'


class TVAccessMiddleware:
    """
    Usuários do grupo 'Visualizador TV' só podem acessar o modo TV das plantas.
    Qualquer outra URL é redirecionada para /plantas/tv/ (seletor de plantas).
    Usuários staff e superusuários não são afetados.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, 'user', None)
        if (
            user is not None
            and user.is_authenticated
            and not user.is_staff
            and not user.is_superuser
            and self._is_tv_only(user)
            and not _TV_PERMITIDO.match(request.path)
        ):
            return redirect('/plantas/tv/')
        return self.get_response(request)

    @staticmethod
    def _is_tv_only(user) -> bool:
        return _GRUPO_TV in _grupos_do_usuario(user)


# ─── Middleware: Portais externos (Fornecedor / Parceiro de Licenças) ─────────
#
# Um mesmo login pode pertencer aos dois grupos ao mesmo tempo (ex.: um
# fornecedor que também é parceiro de licenças de software) — cada middleware
# abaixo libera as URLs do SEU próprio portal e também as do(s) outro(s)
# portal(is) a que o usuário pertença, em vez de expulsá-lo só por não estar
# na própria área. Sem isso, pertencer aos dois grupos gerava um loop de
# redirecionamento (cada middleware jogava o usuário pra URL do outro).

from ProjetoEstoque.models import GRUPO_FORNECEDOR, GRUPO_PARCEIRO_LICENCA  # noqa: E402

_COMUM_PERMITIDO = ('/static/', '/media/', '/login/', '/logout/')

# grupo → prefixo de URL do respectivo portal
_PORTAL_POR_GRUPO = {
    GRUPO_FORNECEDOR: '/portal/',
    GRUPO_PARCEIRO_LICENCA: '/portal-licencas/',
}


def _prefixos_de_portais_do_usuario(user):
    """Prefixos de URL de TODOS os portais externos a que o usuário pertence."""
    grupos = _grupos_do_usuario(user)
    return [prefixo for grupo, prefixo in _PORTAL_POR_GRUPO.items() if grupo in grupos]


def _path_liberado_para_portais(path, user) -> bool:
    if path.startswith(_COMUM_PERMITIDO):
        return True
    return any(path.startswith(prefixo) for prefixo in _prefixos_de_portais_do_usuario(user))


class FornecedorAccessMiddleware:
    """
    Usuários do grupo 'Fornecedor' (Portal do Fornecedor) só podem acessar as
    URLs sob /portal/ — mais as de qualquer outro portal externo a que também
    pertençam (ex.: /portal-licencas/, se também for Parceiro de Licenças).
    Qualquer outra rota é redirecionada para /portal/. Usuários staff e
    superusuários não são afetados.

    Espelha TVAccessMiddleware — é a 1ª das 3 camadas de isolamento
    (middleware + @fornecedor_required + queryset filtrado).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, 'user', None)
        if (
            user is not None
            and user.is_authenticated
            and not user.is_staff
            and not user.is_superuser
            and self._is_fornecedor(user)
            and not _path_liberado_para_portais(request.path, user)
        ):
            return redirect('/portal/')
        return self.get_response(request)

    @staticmethod
    def _is_fornecedor(user) -> bool:
        return GRUPO_FORNECEDOR in _grupos_do_usuario(user)


class LicencaOfficeAccessMiddleware:
    """
    Usuários do grupo 'Parceiro de Licenças' (Portal de Licenças Office) só
    podem acessar as URLs sob /portal-licencas/ — mais as de qualquer outro
    portal externo a que também pertençam (ex.: /portal/, se também for
    Fornecedor). Qualquer outra rota é redirecionada pra lá. Usuários staff e
    superusuários não são afetados.

    Espelha FornecedorAccessMiddleware — mesma defesa em profundidade, mas
    para um módulo diferente.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, 'user', None)
        if (
            user is not None
            and user.is_authenticated
            and not user.is_staff
            and not user.is_superuser
            and self._is_parceiro_licenca(user)
            and not _path_liberado_para_portais(request.path, user)
        ):
            return redirect('/portal-licencas/')
        return self.get_response(request)

    @staticmethod
    def _is_parceiro_licenca(user) -> bool:
        return GRUPO_PARCEIRO_LICENCA in _grupos_do_usuario(user)
