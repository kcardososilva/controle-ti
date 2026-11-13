"""
Ambientes do Zelo (TI · Agro) — as views do próprio mecanismo.

A régua é `services/ambiente_service.py`. Aqui só:

  • `inicio`               a raiz `/` (e o destino depois do login): abre o
                           ambiente que a pessoa usou por último;
  • `rota_legada_frota`    os endereços antigos `/frotas/…` e `/m2m/…`, que
                           respondem 308 para o endereço novo em `/agro/…`;
  • `agro_visao_geral`     a porta do Zelo Agro;
  • `exige_ambiente`       o decorador das views que pertencem a um ambiente
                           (a 2ª camada: o middleware decide pelo caminho, o
                           decorador decide na view — esconder o link não é
                           autorização).
"""
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import Resolver404, resolve, reverse

from services import ambiente_service as amb

from .dashboards import dashboard


def exige_ambiente(chave: str):
    """Login + acesso ao ambiente `chave`; sem ele, 403."""
    def decorador(view):
        @wraps(view)
        @login_required
        def embrulhada(request, *args, **kwargs):
            if not amb.pode_acessar(request.user, chave):
                raise PermissionDenied
            return view(request, *args, **kwargs)
        return embrulhada
    return decorador


@login_required
def inicio(request):
    """`/`: o painel do ambiente da vez. `/dashboard/` continua sendo SEMPRE o
    painel do TI — é para lá que o seletor manda quem escolhe o TI."""
    if amb.ambiente_preferido(request.user, request.session) == amb.AGRO:
        return redirect(amb.url_inicial(amb.AGRO))
    return dashboard(request)


class _Redirecionamento308(HttpResponseRedirect):
    """Permanente E mantém o método (o 301 vira GET em todo navegador)."""
    status_code = 308


def rota_legada_frota(request, antigo: str):
    """Traduz `/frotas/…` e `/m2m/…` para o endereço novo pelo NOME da rota
    (ver `ProjetoEstoque/urls_legado_frota.py`). Sem login, cai no login do
    endereço novo — a tradução não depende de quem pede."""
    try:
        alvo = resolve("/" + antigo, urlconf="ProjetoEstoque.urls_legado_frota")
    except Resolver404:
        raise Http404("Endereço antigo do módulo de Frotas sem equivalente.")
    destino = reverse(alvo.url_name, kwargs=alvo.kwargs)
    if request.META.get("QUERY_STRING"):
        destino += "?" + request.META["QUERY_STRING"]
    return _Redirecionamento308(destino)


@exige_ambiente(amb.AGRO)
def agro_visao_geral(request):
    """A porta do Zelo Agro. Cada grupo aparece só para quem tem a permissão do
    módulo dele; os números vêm dos mesmos services das telas de cada módulo
    (nada é recalculado aqui)."""
    from services import frota_service as frota
    from services import m2m_monitoramento_service as m2m

    user = request.user
    ver_frota = user.has_perm("ProjetoEstoque.ver_frota")
    ver_conectividade = any(
        user.has_perm(f"ProjetoEstoque.{p}")
        for p in ("ver_chips", "consultar_conectividade", "ver_historico_frota")
    )
    indicadores = frota.indicadores_frota() if (ver_frota or ver_conectividade) else None
    return render(request, "front/agro/visao_geral.html", {
        "kpis_frota": indicadores["frota"] if ver_frota else None,
        "kpis_conectividade": indicadores["conectividade"] if ver_conectividade else None,
        "alertas_abertos": (
            m2m.contar_alertas_abertos() if user.has_perm("ProjetoEstoque.ver_historico_frota") else None
        ),
        "pode_ver_chips": user.has_perm("ProjetoEstoque.ver_chips"),
    })
