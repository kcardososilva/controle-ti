import re
import time

from django.core.cache import cache
from django.shortcuts import redirect

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
        return user.groups.filter(name=_GRUPO_TV).exists()
