"""
Frotas e Maquinários — Composição tecnológica: o que está instalado em cada máquina da frota, e quais máquinas não têm nada.

É uma leitura sobre o que o estoque tecnológico (`TecInstalacao`) e o vínculo de chip (`FrotaVinculo`) já registraram; quem instala,
retira ou substitui é o estoque tecnológico, e a tela leva até lá. Acesso: `ver_frota` E `ver_tec_estoque` (o equipamento é do
estoque tecnológico; a máquina só o recebe). O chip só aparece para quem também vê chips.
"""

from django.core.exceptions import PermissionDenied
from django.shortcuts import render
from django.urls import reverse

from services import frota_composicao_service as composicao

from .frota_comum import contexto_base, exige_frota, filtros_do_get, migalhas, paginar, pode

VISTAS = (("instalados", "Componentes instalados"), ("sem", "Máquinas sem tecnologia"))


@exige_frota("ver_frota")
def frota_composicao(request):
    """`/agro/frotas/composicao/` — `?vista=instalados|sem`, `q`, `categoria` (id da categoria tecnológica ou `chip`) e `natureza`."""
    if not request.user.has_perm("ProjetoEstoque.ver_tec_estoque"):
        raise PermissionDenied
    com_chips = pode(request, "ver_chips")
    filtros = filtros_do_get(request, ("vista", "q", "categoria", "natureza"))
    vista = filtros["vista"] if filtros["vista"] in dict(VISTAS) else "instalados"
    resumo = composicao.resumo()
    if not com_chips:
        resumo = {**resumo, "chips": None}

    if vista == "sem":
        page_obj, paginas, querystring = paginar(request, composicao.maquinas_sem_tecnologia(filtros))
        linhas = list(page_obj.object_list)
    else:
        page_obj, paginas, querystring = paginar(request, composicao.componentes(filtros, com_chips=com_chips))
        linhas = list(page_obj.object_list)

    categorias = [(str(c["categoria_id"]), c["nome"], c["n"]) for c in resumo["por_categoria"]]
    if resumo["chips"]:
        categorias.append((composicao.CHIP, "Chip", resumo["chips"]))
    segmentos = [
        {"chave": "instalados", "rotulo": "Componentes instalados", "n": resumo["componentes"] + (resumo["chips"] or 0)},
        {"chave": "sem", "rotulo": "Máquinas sem tecnologia", "n": resumo["sem_tecnologia"]},
    ]
    return render(request, "front/agro/frota/composicao.html", contexto_base(
        request, "composicao", faixa=False, migalhas=migalhas(("Composição tecnológica", None)),
        vista=vista, resumo=resumo, filtros=filtros, categorias=categorias, segmentos=segmentos, linhas=linhas,
        page_obj=page_obj, paginas=paginas, querystring=querystring, total_filtrado=page_obj.paginator.count,
        pode_ver_chips=com_chips, url_limpar=reverse("frota_composicao") + f"?vista={vista}",
    ))
