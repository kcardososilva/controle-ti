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


# ─── Middleware: perfis de ACESSO RESTRITO (uma tela só) ──────────────────────
#
# Os três middlewares acima nasceram um por grupo, e cada novo grupo custava
# outra cópia do mesmo bloco de nove linhas. Os perfis restritos (Portal de
# Contratações e Consulta Quiosque) entram por UM middleware só, que lê a
# tabela de `services/acesso_restrito_service.py`: perfil novo é uma entrada
# naquele dicionário, não uma classe nova aqui e uma linha nova no settings.
#
# Por que o import é no `__init__` e não no topo do arquivo: o service importa
# `_grupos_do_usuario` DESTE módulo (como `perfil_acesso_service` já faz), então
# um import no topo seria circular. O `__init__` roda uma vez, quando o Django
# monta a cadeia de middleware — depois do registro de apps, e nunca por
# requisição.


class AcessoRestritoMiddleware:
    """
    Prende cada perfil restrito à sua área e manda o resto de volta para a tela
    inicial dele. Quem tem MAIS DE UM perfil (Contratações + Quiosque) alcança a
    união das áreas — cada uma com as travas do próprio perfil.

    É a 1ª das 3 camadas (middleware + decorador de capacidade + botão escondido
    no template). As três leem a MESMA tabela — ver o cabeçalho do service.

    Diferença importante em relação ao `FornecedorAccessMiddleware`: aqui o
    bloqueio não para no prefixo. `/quiosque/` guarda as telas de consulta e as
    de gestão lado a lado, então o perfil de consulta precisa alcançar o
    prefixo e ser barrado DENTRO dele — é o que `caminho_liberado` resolve.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        from services import acesso_restrito_service as ar
        self._ar = ar

    def __call__(self, request):
        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated:
            chaves = self._ar.perfis_de(user)
            if chaves and not self._ar.caminho_liberado_em(request.path, chaves):
                # Quem acumula perfis volta para a tela inicial da área em que
                # estava; dali o menu mostra as duas.
                return redirect(self._ar.url_de_retorno(request.path, chaves))
        return self.get_response(request)


# ─── Middleware: AMBIENTES do Zelo (TI · Agro) ────────────────────────────────
#
# Mesmo desenho do `AcessoRestritoMiddleware`: a régua é a tabela de
# `services/ambiente_service.py`, lida também pelo menu e pelo decorador das
# views. Roda DEPOIS dos middlewares de portal/perfil restrito: quem é preso
# por eles já foi devolvido para a própria área antes de chegar aqui.


class AmbienteMiddleware:
    """
    A ROTA define o ambiente (`/agro/…` é Zelo Agro; o resto é Zelo TI). Quem
    não alcança o ambiente do caminho recebe 403 — a mesma resposta que o
    módulo já dava a quem não tem a permissão da tela. Superusuário passa por cima, como em todo o
    sistema; staff não: "acesso somente ao Agro" precisa valer para qualquer
    login, e o TI continua aberto a todo interno por padrão.

    Também anota `request.ambiente` (para o shell desenhar a identidade certa)
    e guarda o ambiente da vez quando ele MUDA — é isso que faz o próximo
    acesso abrir no último ambiente usado. Trocar de ambiente não concede nada:
    só a permissão concede.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        from services import ambiente_service as amb
        self._amb = amb

    def __call__(self, request):
        amb = self._amb
        user = getattr(request, 'user', None)
        request.ambiente = None
        if user is None or not user.is_authenticated or amb.fora_dos_ambientes(user):
            return self.get_response(request)

        chave = amb.ambiente_do_caminho(request.path)
        if chave is None:
            # Caminho neutro (conta, sessão, arquivo, sino): veste o ambiente
            # que a pessoa estava usando, sem decidir acesso nem mudar nada.
            if not request.path.startswith(('/static/', '/media/')):
                request.ambiente = amb.ambiente_preferido(user, getattr(request, 'session', None))
            return self.get_response(request)

        if not amb.pode_acessar(user, chave):
            # 403 (a tela de erro do sistema), não um redirecionamento: quem colou
            # um link do Agro sem ter o Agro precisa ver que não tem acesso, não
            # cair no painel do TI sem explicação. A raiz `/` é neutra e leva cada
            # um ao ambiente que alcança (views.ambiente.inicio).
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied

        request.ambiente = chave
        # Só navegação de verdade conta como "usar o ambiente": um POST de
        # formulário ou uma chamada AJAX não muda a preferência de ninguém.
        if request.method == 'GET' and request.headers.get('x-requested-with') != 'XMLHttpRequest':
            amb.registrar_uso(user, chave, getattr(request, 'session', None))
        return self.get_response(request)
