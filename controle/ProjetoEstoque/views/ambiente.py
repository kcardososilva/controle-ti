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
    """A porta do Zelo Agro: o dashboard que junta os três módulos. Cada bloco aparece
    só para quem tem a permissão do módulo dele (e nem é calculado para os outros);
    os números vêm dos mesmos services das telas de cada módulo — nada é recalculado
    aqui — e cada um diz de onde vem. O período (7/30/90 dias) vale para o consumo
    de dados."""
    from services import agro_dash_service as dash

    user = request.user
    acesso = {
        "frota": user.has_perm("ProjetoEstoque.ver_frota"),
        "chips": user.has_perm("ProjetoEstoque.ver_chips"),
        "historico": user.has_perm("ProjetoEstoque.ver_historico_frota"),
        "infra": user.has_perm("ProjetoEstoque.ver_tec_estoque"),
    }
    # Quem administra o patrimônio dos chips no TI (acesso ao ambiente TI + vincular chips) também vê a pendência do estoque.
    acesso["estoque_ti"] = acesso["chips"] and user.has_perm("ProjetoEstoque.vincular_chips") and amb.pode_acessar(user, amb.TI)
    acesso["conectividade"] = acesso["chips"] or acesso["historico"] or user.has_perm("ProjetoEstoque.consultar_conectividade")
    # o que a operadora respondeu (sessão, consumo, alertas): sem permissão de API o painel mostra o chip só como cadastro e vínculo
    acesso["api"] = amb.pode_ver_api(user)
    dias = dash.periodo_pedido(request.GET.get("periodo"))
    return render(request, "front/agro/visao_geral.html", {
        "painel": dash.painel_da_porta(acesso, dias),
        "periodo": dias, "periodos": dash.PERIODOS,
        "pode_ver_mapa": acesso["frota"] or acesso["infra"],
        "pode_ver_chips": acesso["chips"],
        "pode_ver_api": acesso["api"],
    })
