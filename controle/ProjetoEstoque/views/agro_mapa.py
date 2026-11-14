"""
O mapa do Zelo Agro (`/agro/mapa/`): veículos e máquinas, torres, estações e enlaces PTP em OpenStreetMap.

A tela e o JSON passam pela MESMA régua de permissão (`_permissoes`): veículos com `ver_frota`, estruturas e
enlaces com `ver_tec_estoque`, ICCID e estado dos chips com `ver_chips`. Quem não tem nenhuma das duas primeiras
recebe 403 — a localização dos ativos é dado da empresa. Os dados vêm de `services/agro_mapa_service.py`
(nenhuma posição é deduzida; cada ponto diz de onde vem). Nada aqui chama a API da TIM: o mapa lê o banco.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods

from services import agro_localizacao_service as localizacao
from services import agro_mapa_service as mapa
from services import ambiente_service as amb

# onde o mapa abre quando não há nenhum ponto: a Fazenda Karitel (a mesma coordenada de teste do Quiosque).
CENTRO_PADRAO = (-14.7763, -45.5634)
ZOOM_PADRAO = 10


def _permissoes(request) -> dict:
    u = request.user
    return {
        "ver_frota": u.has_perm("ProjetoEstoque.ver_frota"),
        "ver_infra": u.has_perm("ProjetoEstoque.ver_tec_estoque"),
        "ver_chips": u.has_perm("ProjetoEstoque.ver_chips"),
        # o que a API disse sobre o chip (estado, sessão, atividade): só com permissão de API (Portal do Colaborador)
        "ver_api": amb.pode_ver_api(u),
    }


def _pode_configurar(request) -> bool:
    """Os limites de frescor da posição GPS são configuração do módulo: mesma permissão dos demais ajustes de Frotas."""
    return request.user.has_perm("ProjetoEstoque.administrar_frota_config")


def _exigir_alguma(permissoes: dict) -> None:
    if not (permissoes["ver_frota"] or permissoes["ver_infra"]):
        raise PermissionDenied


def _filtros_do_get(request) -> dict:
    filtros = {c: (request.GET.get(c) or "").strip() for c in (*mapa.FILTROS_DE_VEICULO, mapa.FOCO, "fazenda", "tipo_estrutura", "estado_enlace")}
    pedidas = request.GET.getlist("camada")
    if "nenhuma" in pedidas:                      # o usuário desmarcou todas as camadas: o mapa fica vazio, não "tudo"
        filtros["camadas"] = ["nenhuma"]
    else:
        camadas = [c for c in pedidas if c in ("veiculos", "estruturas", "enlaces")]
        if camadas:
            filtros["camadas"] = camadas
    return {k: v for k, v in filtros.items() if v}


@login_required
@require_GET
def agro_mapa(request):
    """A página do mapa. Os filtros da URL (`?tipo=…&localidade=…`) são os mesmos da lista de veículos: o botão
    "Mapa" da lista leva os filtros junto, e o "Lista" do mapa traz de volta."""
    permissoes = _permissoes(request)
    _exigir_alguma(permissoes)
    filtros = _filtros_do_get(request)
    configuracao = {
        "url_dados": reverse("agro_mapa_dados"),
        "centro": list(CENTRO_PADRAO), "zoom": ZOOM_PADRAO,
        "permissoes": permissoes,
        "filtros": {k: v for k, v in filtros.items() if k not in ("camadas", mapa.FOCO)},
        "camadas": filtros.get("camadas") or [],
        # o veículo a destacar e centralizar (vindo do detalhe de um chip): só vale com `ver_frota`, como o resto do veículo
        "foco": int(filtros[mapa.FOCO]) if permissoes["ver_frota"] and str(filtros.get(mapa.FOCO, "")).isdigit() else None,
        "limites_gps": localizacao.limites(),
    }
    return render(request, "front/agro/mapa/mapa.html", {
        **permissoes,
        "opcoes": mapa.opcoes_dos_filtros(ver_frota=permissoes["ver_frota"], ver_infra=permissoes["ver_infra"], ver_api=permissoes["ver_api"]),
        "filtros": filtros,
        "config_mapa": configuracao,
        "url_lista": reverse("frota_visao_geral"),
        "pode_ver_estoque": permissoes["ver_infra"],
        "pode_configurar": _pode_configurar(request),
        "tiles_atribuicao_url": "https://www.openstreetmap.org/copyright",
    })


@login_required
@require_GET
def agro_mapa_dados(request):
    """O JSON do mapa, já filtrado pela permissão (ver `agro_mapa_service.montar_mapa`). Só lê o banco."""
    permissoes = _permissoes(request)
    _exigir_alguma(permissoes)
    dados = mapa.montar_mapa(_filtros_do_get(request), **permissoes)
    resposta = JsonResponse(dados)
    resposta["Cache-Control"] = "private, no-store"          # localização de ativos: nunca em cache compartilhado
    return resposta


@login_required
@require_http_methods(["GET", "POST"])
def agro_mapa_config(request):
    """Quando uma posição GPS deixa de ser "atual" (`FrotaLocalizacaoConfig`). INDEPENDENTE da sincronização do consumo
    da TIM: a posição vem de outra fonte, com outro ritmo. Só quem administra a configuração do módulo."""
    if not _pode_configurar(request):
        raise PermissionDenied
    erros: dict = {}
    valores = localizacao.limites()
    if request.method == "POST":
        valores = {c: request.POST.get(c, "") for c in ("minutos_atualizada", "horas_sem_atualizacao")}
        try:
            localizacao.salvar_configuracao(valores, request.user)
        except ValidationError as exc:
            erros = exc.message_dict
        else:
            messages.success(request, "Limites da localização GPS salvos.")
            return redirect("agro_mapa_config")
    return render(request, "front/agro/mapa/config.html", {"valores": valores, "erros": erros})
