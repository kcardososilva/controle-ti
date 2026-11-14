"""
Frotas e Maquinários — a tela principal: indicadores, pesquisa, filtros e a listagem de equipamentos.

UMA tela, três entradas (`frota_visao_geral`, `frota_equipamentos_proprios`, `frota_equipamentos_terceiros`): próprio
e terceiro são filtros sobre a MESMA base, não páginas diferentes. A rota só pré-escolhe a natureza; tudo o mais
(busca, filtros, ordem, tamanho da página, exportação) é igual nas três. `frota_veiculos` é o endereço de antes
da reformulação e abre a mesma tela — links e favoritos antigos continuam valendo.

As views NÃO têm regra de negócio: validam a entrada, chamam os services (`frota_service`, `frota_qualidade_service`,
`frota_relatorio_service`) e renderizam. O que a tela precisa e o service ainda não tinha foi acrescentado LÁ.

Acesso: `ver_frota` (quem só vê chips é levado à visão geral da Conectividade); exportar pede `exportar_frota`;
validar em lote pede `validar_inventario_frota`. Esconder um botão nunca é autorização: cada view confere.
"""

from django.contrib import messages
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from ProjetoEstoque.models import (
    ConectividadeOperadora,
    FrotaCentroCusto,
    FrotaClasseMecanica,
    FrotaClasseOperacional,
    FrotaEmpresa,
    FrotaNatureza,
    FrotaTipo,
    FrotaVeiculo,
    Localidade,
)
from ProjetoEstoque.templatetags.m2m_tags import ROTULOS_CURTOS_DE_CONECTIVIDADE
from services import agro_dash_service as dash
from services import frota_qualidade_service as qualidade
from services import frota_relatorio_service as relatorios
from services import frota_service as frota

from .frota_comum import (
    contexto_base,
    exige_frota,
    filtros_ativos,
    filtros_do_get,
    migalhas,
    negar_sem,
    paginar,
    pode,
    pode_ver_api,
    rotulo_de_id,
    voltar_para,
)

# Os filtros que a tela entende. `chip`, `conectividade`, `operadora`, `de` e `ate` são os da conectividade
# (links da Visão geral da Conectividade chegam com eles) e ficam num bloco próprio do painel de filtros.
FILTROS_DE_EQUIPAMENTOS = (
    "q", "natureza", "status", "validacao", "pendencia", "tipo", "classe_operacional", "classe_mecanica", "setor",
    "centro_custo", "empresa", "ano_fabricacao", "ano_modelo", "localidade", "chip", "conectividade", "operadora",
    "de", "ate", "ordem", "dir", "por_pagina", "fabricante", "localizacao",
)
# Dos filtros acima, os que são da API (o estado da conectividade e as datas da consulta): quem não vê API não filtra por eles — filtrar
# por "sessão ativa" já seria ler o que a operadora disse. `chip` e `operadora` são cadastro e vínculo e ficam.
FILTROS_DE_API_NOS_EQUIPAMENTOS = ("conectividade", "de", "ate")
POR_PAGINA = (10, 25, 50, 100)
POR_PAGINA_PADRAO = 25
ORDEM_PADRAO = "legado"

# As colunas que se ordenam clicando no cabeçalho: (chave de `frota.ORDENS_DE_VEICULOS`, rótulo).
COLUNAS_ORDENAVEIS = (
    ("legado", "Código"), ("nome", "Equipamento"), ("classe", "Classe"), ("modelo", "Modelo"), ("empresa", "Empresa"),
    ("setor", "Setor"), ("placa", "Placa"), ("ano", "Ano"), ("situacao", "Situação"),
)

ESCOPOS = {
    "": {
        "natureza": "", "migalha": "", "descricao": "Gerencie veículos, máquinas e equipamentos motorizados da operação.",
        "aba": "equipamentos", "rota": "frota_visao_geral",
    },
    FrotaNatureza.PROPRIO: {
        "natureza": FrotaNatureza.PROPRIO, "migalha": "Próprios", "aba": "equipamentos",
        "descricao": "Os equipamentos motorizados da empresa — o patrimônio próprio.", "rota": "frota_equipamentos_proprios",
    },
    FrotaNatureza.TERCEIRO: {
        "natureza": FrotaNatureza.TERCEIRO, "migalha": "Terceiros", "aba": "equipamentos",
        "descricao": "Os equipamentos de terceiros que operam na empresa. Não entram na conta do patrimônio próprio.",
        "rota": "frota_equipamentos_terceiros",
    },
}


def _distintos(campo: str, *, texto: bool = False) -> list:
    """Os valores que o campo tem de fato no cadastro, sem repetir e sem os vazios (o vazio de texto é `""`; o de
    número, `NULL`)."""
    consulta = FrotaVeiculo.objects.exclude(**{f"{campo}__isnull": True})
    if texto:
        consulta = consulta.exclude(**{campo: ""})
    return sorted(set(consulta.values_list(campo, flat=True)))


def opcoes_de_equipamentos() -> dict:
    """O que os selects do painel de filtros oferecem — só o que de fato existe no cadastro."""
    return {
        "tipos": list(FrotaTipo.objects.order_by("nome").values_list("pk", "nome")),
        "classes_operacionais": list(FrotaClasseOperacional.objects.order_by("nome").values_list("pk", "nome")),
        "classes_mecanicas": list(FrotaClasseMecanica.objects.order_by("nome").values_list("pk", "nome")),
        "centros_de_custo": [
            (pk, f"{codigo} · {nome}" if codigo else nome)
            for pk, codigo, nome in FrotaCentroCusto.objects.order_by("codigo_erp", "nome").values_list("pk", "codigo_erp", "nome")
        ],
        "empresas": list(FrotaEmpresa.objects.order_by("nome").values_list("pk", "nome")),
        "setores": [(s, s) for s in sorted(_distintos("setor", texto=True), key=str.casefold)],
        "anos_fabricacao": [(a, a) for a in sorted(_distintos("ano_fabricacao"), reverse=True)],
        "anos_modelo": [(a, a) for a in sorted(_distintos("ano_modelo"), reverse=True)],
        # as fazendas sempre; unidade de outro tipo só se algum equipamento (cadastrado antes) ainda a usa
        "localidades": list(
            Localidade.objects.filter(Q(veiculos_frota__isnull=False) | Q(local__in=[n for _, n in frota.FAZENDAS]))
            .distinct().order_by("local").values_list("pk", "local")
        ),
        "status": list(FrotaVeiculo.Status.choices),
        "fabricantes": list(FrotaVeiculo.objects.exclude(fabricante="").order_by("fabricante").values_list("fabricante", flat=True).distinct()),
        "validacao": [("pendente", "A validar"), ("validado", "Validado")],
        "pendencias": [(chave, rotulo) for chave, (rotulo, _) in qualidade.PENDENCIAS.items()],
        "natureza": list(FrotaNatureza.choices),
        "chip": [("com", "Com chip vinculado"), ("sem", "Sem chip")],
        "conectividade": [(chave, ROTULOS_CURTOS_DE_CONECTIVIDADE[chave]) for chave in frota.ESTADOS_DE_CONECTIVIDADE],
        "operadoras": list(ConectividadeOperadora.objects.filter(chips__isnull=False).distinct().order_by("nome").values_list("pk", "nome")),
    }


def _rotulos_dos_filtros(opcoes: dict) -> dict:
    por_id = lambda chave: (lambda v: rotulo_de_id(opcoes[chave], v))        # noqa: E731
    return {
        "q": ("Busca", None), "natureza": ("Natureza", lambda v: dict(opcoes["natureza"]).get(v, v)),
        "status": ("Situação", lambda v: dict(opcoes["status"]).get(v, v)),
        "validacao": ("Validação", lambda v: dict(opcoes["validacao"]).get(v, v)),
        "pendencia": ("Pendência", lambda v: dict(opcoes["pendencias"]).get(v, v)),
        "tipo": ("Categoria", por_id("tipos")), "classe_operacional": ("Classe operacional", por_id("classes_operacionais")),
        "classe_mecanica": ("Classe mecânica", por_id("classes_mecanicas")), "setor": ("Setor", None),
        "centro_custo": ("Centro de custo", por_id("centros_de_custo")), "empresa": ("Empresa", por_id("empresas")),
        "ano_fabricacao": ("Ano de fabricação", None), "ano_modelo": ("Ano-modelo", None),
        "localidade": ("Fazenda", por_id("localidades")),
        "chip": ("Chip", lambda v: dict(opcoes["chip"]).get(v, v)),
        "conectividade": ("Conectividade", lambda v: dict(opcoes["conectividade"]).get(v, v)),
        "operadora": ("Operadora do chip", por_id("operadoras")), "de": ("Consultado desde", None), "ate": ("Consultado até", None),
        "fabricante": ("Fabricante", None),
        "localizacao": ("Localização no mapa", lambda v: {"com": "com coordenada", "sem": "sem coordenada"}.get(v, v)),
    }


def _cabecalhos_da_tabela(request, filtros: dict) -> list[dict]:
    """O cabeçalho clicável de cada coluna ordenável: o link alterna crescente/decrescente e volta à página 1."""
    atual, descendente = filtros["ordem"], filtros["dir"] == "desc"
    cabecalhos = []
    for chave, rotulo in COLUNAS_ORDENAVEIS:
        parametros = request.GET.copy()
        parametros.pop("page", None)
        parametros["ordem"] = chave
        parametros["dir"] = "desc" if (atual == chave and not descendente) else "asc"
        sentido = ("desc" if descendente else "asc") if atual == chave else ""
        cabecalhos.append({
            "chave": chave, "rotulo": rotulo, "url": "?" + parametros.urlencode(), "sentido": sentido,
            "aria": {"asc": "ascending", "desc": "descending"}.get(sentido, "none"),
        })
    return cabecalhos


def _segmentos(request, rota: str, indicadores: dict) -> list[dict]:
    """O filtro rápido do alto da lista: Todos · Próprios · Terceiros (a natureza, que muda de ENDEREÇO) e
    Em uso · Em manutenção (a situação, que muda a consulta). Cada contagem sai do mesmo cadastro da lista."""
    def url(nome_da_rota, status=None):
        parametros = request.GET.copy()
        for campo in ("page", "natureza", "status"):
            parametros.pop(campo, None)
        if status:
            parametros["status"] = status
        consulta = parametros.urlencode()
        return reverse(nome_da_rota) + (f"?{consulta}" if consulta else "")

    status_atual = (request.GET.get("status") or "").strip()
    excluidos = indicadores["desativados"]
    return [
        {"rotulo": "Todos", "n": indicadores["total"] - excluidos, "url": reverse("frota_visao_geral"),
         "ativo": rota == "frota_visao_geral" and not status_atual and not request.GET.get("natureza")},
        {"rotulo": "Próprios", "n": indicadores["proprios"], "url": url("frota_equipamentos_proprios", status_atual),
         "ativo": rota == "frota_equipamentos_proprios"},
        {"rotulo": "Terceiros", "n": indicadores["terceiros"], "url": url("frota_equipamentos_terceiros", status_atual),
         "ativo": rota == "frota_equipamentos_terceiros"},
        {"rotulo": "Em uso", "n": indicadores["em_uso"], "url": url(rota, "ativo"), "ativo": status_atual == "ativo", "grupo": 2},
        {"rotulo": "Em manutenção", "n": indicadores["em_manutencao"], "url": url(rota, "manutencao"), "ativo": status_atual == "manutencao"},
        # Só aparece quando há o que ver: é por aqui que o excluído continua acessível (e pode ser restaurado).
        *([{"rotulo": "Excluídos", "n": excluidos, "url": url(rota, "desativado"), "ativo": status_atual == "desativado", "grupo": 3}]
          if excluidos or status_atual == "desativado" else []),
    ]


def _tela_de_equipamentos(request, escopo: str):
    info = ESCOPOS[escopo]
    filtros = filtros_do_get(request, FILTROS_DE_EQUIPAMENTOS)
    if not pode_ver_api(request):
        for chave in FILTROS_DE_API_NOS_EQUIPAMENTOS:
            filtros[chave] = ""
    if info["natureza"]:
        filtros["natureza"] = info["natureza"]
    elif filtros["natureza"] not in FrotaNatureza.values:
        filtros["natureza"] = ""
    if filtros["ordem"] not in frota.ORDENS_DE_VEICULOS or filtros["ordem"] == "atencao":
        filtros["ordem"] = ORDEM_PADRAO
    if filtros["dir"] not in ("asc", "desc"):
        filtros["dir"] = "asc"
    # Excluir é desativar (regra do módulo): o excluído sai da lista, do Excel e das contagens, e só volta quando a
    # Situação "Desativado" é pedida (o segmento "Excluídos"). Sem isso o botão Excluir não pareceria fazer nada.
    if filtros["status"] not in frota.Status.values:
        filtros["sem_excluidos"] = "1"

    if request.GET.get("formato") == "xlsx":
        negar_sem(request, "exportar_frota")
        conteudo = relatorios.exportar_equipamentos_xlsx(filtros)
        resposta = HttpResponse(conteudo, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        resposta["Content-Disposition"] = f'attachment; filename="{relatorios.nome_do_arquivo("equipamentos")}"'
        return resposta

    por_pagina = int(filtros["por_pagina"]) if filtros["por_pagina"].isdigit() and int(filtros["por_pagina"]) in POR_PAGINA else POR_PAGINA_PADRAO
    consulta = frota.listar_veiculos(filtros)
    page_obj, paginas, querystring = paginar(request, consulta, por_pagina=por_pagina)
    opcoes = opcoes_de_equipamentos()
    indicadores = frota.indicadores_equipamentos()
    # filtros em uso para as etiquetas: sem a ordem, sem o tamanho da página e sem a natureza que a ROTA já diz
    em_uso = {k: v for k, v in filtros.items() if k not in ("ordem", "dir", "por_pagina", "sem_excluidos") and not (k == "natureza" and info["natureza"])}
    ativos = filtros_ativos(request, em_uso, _rotulos_dos_filtros(opcoes))
    avancado = any(filtros[c] for c in (
        "tipo", "classe_operacional", "classe_mecanica", "setor", "centro_custo", "empresa", "ano_fabricacao", "ano_modelo",
        "localidade", "validacao", "pendencia", "chip", "conectividade", "operadora", "de", "ate", "fabricante", "localizacao",
    ))
    exportar = request.GET.copy()
    exportar.pop("page", None)
    exportar["formato"] = "xlsx"
    return render(request, "front/agro/frota/equipamentos.html", contexto_base(
        request, info["aba"], faixa=False,
        veiculos=list(page_obj.object_list), page_obj=page_obj, paginas=paginas, querystring=querystring,
        filtros=filtros, filtros_ativos=ativos, filtros_avancados_abertos=avancado, opcoes=opcoes,
        migalhas=migalhas(*([(info["migalha"], None)] if info["migalha"] else [])),
        kpis=indicadores, total_geral=indicadores["total"] - indicadores["desativados"], total_filtrado=page_obj.paginator.count,
        escopo=info, rota_atual=info["rota"], segmentos=_segmentos(request, info["rota"], indicadores),
        cabecalhos=_cabecalhos_da_tabela(request, filtros), por_pagina=por_pagina, opcoes_por_pagina=POR_PAGINA,
        url_exportar="?" + exportar.urlencode(), url_limpar=reverse(info["rota"]),
        com_pendentes=any(v.validacao_pendente for v in page_obj.object_list),
        painel=dash.painel_da_frota(), movimentacoes_recentes=dash.movimentacoes_recentes_da_frota(),
        url_mapa=reverse("agro_mapa") + ("?" + request.GET.urlencode() if request.GET else ""),
    ))


@exige_frota("ver_frota", "ver_chips")
def frota_visao_geral(request):
    """Frotas e Maquinários › Visão geral (`/agro/frotas/`): os indicadores e a listagem de todos os equipamentos.
    Quem só vê chips é levado à visão geral da Conectividade."""
    if not pode(request, "ver_frota"):
        return redirect("m2m_visao_geral")
    return _tela_de_equipamentos(request, "")


@exige_frota("ver_frota")
def frota_veiculos(request):
    """O endereço de antes da reformulação (`/agro/frotas/veiculos/`): abre a mesma tela da Visão geral, com a
    consulta que o link trouxer."""
    return _tela_de_equipamentos(request, "")


@exige_frota("ver_frota")
def frota_equipamentos_proprios(request):
    return _tela_de_equipamentos(request, FrotaNatureza.PROPRIO)


@exige_frota("ver_frota")
def frota_equipamentos_terceiros(request):
    return _tela_de_equipamentos(request, FrotaNatureza.TERCEIRO)


@exige_frota("validar_inventario_frota")
@require_POST
def frota_equipamentos_validar_lote(request):
    """O responsável pelo inventário confirma, de uma vez, os equipamentos marcados na lista. Cada um recebe o seu
    evento de validação; quem já estava validado é ignorado (e contado), sem erro."""
    resultado = frota.validar_varios(request.POST.getlist("ids"), request.user)
    if not resultado["validados"] and not resultado["ignorados"]:
        messages.warning(request, "Marque os equipamentos que deseja validar.")
    else:
        mensagem = f"{resultado['validados']} equipamento{'s' if resultado['validados'] != 1 else ''} validado{'s' if resultado['validados'] != 1 else ''}."
        if resultado["ignorados"]:
            mensagem += f" {resultado['ignorados']} já estava{'m' if resultado['ignorados'] != 1 else ''} validado{'s' if resultado['ignorados'] != 1 else ''}."
        messages.success(request, mensagem)
    return redirect(voltar_para(request, reverse("frota_visao_geral")))
