"""
Frotas e Maquinários — Relatórios: o ponto de partida das saídas do módulo. Cada número aqui é a MESMA contagem da lista que ele
abre (nada é recalculado por fora), e cada exportação é a da tela correspondente, no mesmo recorte.

Acesso: `ver_frota`; baixar o Excel pede `exportar_frota` (sem ela o botão não aparece e a rota de exportação responde 403).
"""

from django.shortcuts import render

from services import frota_equipamento_import_service as imp
from services import frota_relatorio_service as relatorios
from services import frota_service as frota

from .frota_comum import contexto_base, exige_frota, migalhas, pode


@exige_frota("ver_frota")
def frota_relatorios(request):
    pode_importar = pode(request, "importar_equipamentos_frota")
    ultima = next(iter(imp.listar_importacoes(limite=1)), None) if pode_importar else None
    return render(request, "front/agro/frota/relatorios.html", contexto_base(
        request, "relatorios", faixa=False, migalhas=migalhas(("Relatórios", None)),
        kpis=frota.indicadores_equipamentos(), por_categoria=relatorios.quadro_por_categoria(), por_classe=relatorios.quadro_por_classe(),
        conferencias=relatorios.conferencias_do_cadastro(), ultima_importacao=ultima,
    ))
