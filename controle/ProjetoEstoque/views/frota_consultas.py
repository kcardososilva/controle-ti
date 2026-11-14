"""
Frotas e Maquinários — as consultas da frota inteira: Movimentações, Histórico, Composição tecnológica e Relatórios.

Movimentações e Histórico são UMA tela (`_tela_de_eventos`) com dois recortes da mesma linha do tempo do cadastro
(`frota_historico_service`): o primeiro só o que muda lugar, responsável ou situação; o segundo, tudo. Nada aqui
grava. As views validam a entrada, chamam os services e renderizam.

Acesso: Movimentações e Histórico pedem `ver_historico_frota`; a Composição, `ver_frota` e `ver_tec_estoque` (o
equipamento tecnológico é do estoque tecnológico); os Relatórios, `ver_frota` — e cada exportação, `exportar_frota`.
"""

from django.http import HttpResponse
from django.shortcuts import render

from ProjetoEstoque.models import FrotaNatureza
from services import frota_historico_service as historico
from services import frota_relatorio_service as relatorios

from .frota_comum import (
    contexto_base,
    exige_frota,
    filtros_ativos,
    filtros_do_get,
    migalhas,
    negar_sem,
    paginar,
)

TIPO_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
FILTROS_DE_EVENTOS = ("q", "tipo", "usuario", "natureza", "de", "ate")
POR_PAGINA_DOS_EVENTOS = 30

TELAS_DE_EVENTOS = {
    True: {
        "aba": "mov_equip", "migalha": "Movimentações", "titulo": "Movimentações",
        "descricao": "As mudanças de lugar, de responsável e de situação dos equipamentos, com a data, o motivo e quem registrou.",
        "rota": "frota_equip_movimentacoes", "arquivo": "movimentacoes_frota", "vazio": "Nenhuma movimentação registrada",
        "vazio_texto": "Mudanças de fazenda, setor, centro de custo, responsável e situação aparecem aqui.",
    },
    False: {
        "aba": "historico_equip", "migalha": "Histórico", "titulo": "Histórico",
        "descricao": "Tudo o que aconteceu com o cadastro dos equipamentos: cadastro, edição, movimentação, situação e validação.",
        "rota": "frota_equip_historico", "arquivo": "historico_frota", "vazio": "Nenhum registro no histórico",
        "vazio_texto": "O cadastro e as mudanças dos equipamentos ficam registrados aqui, sem poder ser editados.",
    },
}


def _tela_de_eventos(request, so_movimentacoes: bool):
    info = TELAS_DE_EVENTOS[so_movimentacoes]
    filtros = filtros_do_get(request, FILTROS_DE_EVENTOS)
    if request.GET.get("formato") == "xlsx":
        negar_sem(request, "exportar_frota")
        resposta = HttpResponse(relatorios.exportar_eventos_xlsx(filtros, so_movimentacoes=so_movimentacoes), content_type=TIPO_XLSX)
        resposta["Content-Disposition"] = f'attachment; filename="{relatorios.nome_do_arquivo(info["arquivo"])}"'
        return resposta

    consulta = historico.eventos_da_frota(filtros, so_movimentacoes=so_movimentacoes)
    page_obj, paginas, querystring = paginar(request, consulta, por_pagina=POR_PAGINA_DOS_EVENTOS)
    tipos = (
        [(chave, rotulo) for chave, (rotulo, _) in historico.TIPOS_DE_MOVIMENTACAO.items()]
        if so_movimentacoes else list(historico.TIPOS_DE_HISTORICO)
    )
    rotulos = {
        "q": ("Equipamento", None), "tipo": ("Tipo", lambda v: dict(tipos).get(v, v)), "usuario": ("Registrado por", None),
        "natureza": ("Natureza", lambda v: dict(FrotaNatureza.choices).get(v, v)), "de": ("De", None), "ate": ("Até", None),
    }
    exportar = request.GET.copy()
    exportar.pop("page", None)
    exportar["formato"] = "xlsx"
    return render(request, "front/agro/frota/eventos.html", contexto_base(
        request, info["aba"], faixa=False,
        migalhas=migalhas((info["migalha"], None)), tela=info, so_movimentacoes=so_movimentacoes,
        eventos=[historico.descrever(e) for e in page_obj.object_list], page_obj=page_obj, paginas=paginas, querystring=querystring,
        filtros=filtros, filtros_ativos=filtros_ativos(request, filtros, rotulos), tipos=tipos, naturezas=FrotaNatureza.choices,
        total_geral=consulta.model.objects.count() if not so_movimentacoes else historico.eventos_da_frota({}, so_movimentacoes=True).count(),
        total_filtrado=page_obj.paginator.count, url_exportar="?" + exportar.urlencode(),
    ))


@exige_frota("ver_historico_frota")
def frota_equip_movimentacoes(request):
    """Movimentações da frota: o que mudou os equipamentos de lugar, de responsável ou de situação."""
    return _tela_de_eventos(request, True)


@exige_frota("ver_historico_frota")
def frota_equip_historico(request):
    """Histórico da frota: todos os eventos do cadastro dos equipamentos."""
    return _tela_de_eventos(request, False)
