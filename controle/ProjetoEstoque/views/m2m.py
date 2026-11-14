"""
Telas de monitoramento da conectividade (as que mostram e as que têm formulário): chips, sessões,
consumo, alertas e a configuração da integração TIM M2M.

As views NÃO têm regra de negócio: validam a entrada, chamam
`services/m2m_monitoramento_service.py` (e os de frota/vínculo) e renderizam. O que uma tela
precisa e o service ainda não tinha foi acrescentado LÁ (com teste), nunca aqui.

Acesso — `exige_frota("<codename>")` (`frota_comum.py`), a mesma régua do resto do módulo Frotas e
Conectividade; cada tela exige a SUA permissão:

    ver_chips                 lista e detalhe do chip (e a visão geral, junto de `ver_frota`)
    importar_chips            cadastrar/editar chip, opções do formulário, ligar/desligar o monitoramento
    consultar_conectividade   sessões, consumo, JSON da resposta guardada e o "Atualizar"
    ver_historico_frota       alertas e ocorrências
    tratar_ocorrencias        assumir, resolver, descartar e comentar ocorrência
    administrar_frota_config  token, frequência, limites, mapeamento (e, em `frota.py`, tipos,
                              operadoras e regras de alerta)

As ações de MOVIMENTAÇÃO do chip (vincular, desvincular, transferir, substituir, devolver, inativar)
estão em `frota_api.py`; as telas daqui só trazem os botões, para quem pode usá-los.

Quem não é autenticado vai para o login (302); quem é, mas não tem a permissão, recebe 403. O token
NUNCA entra em contexto de template: a tela de configuração só recebe a origem (`.env` ou banco) e os
4 últimos caracteres.

Os POSTs e o JSON ficam em `m2m_api.py`.
"""

from datetime import timedelta

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from ProjetoEstoque.models import (
    ConectividadeOperadora,
    FrotaVinculo,
    Localidade,
    M2MChip,
    M2MConsulta,
    M2MIntegracao,
    M2MOcorrencia,
    Usuario,
)
from services import agro_localizacao_service as gps
from services import frota_vinculo_service as vinculos
from services import m2m_analise_service as analise
from services import m2m_dados_service as dados_svc
from services import m2m_monitoramento_service as m2m
from services import tim_m2m_service as tim

from .chip_estoque_ti import contexto_do_estoque_ti
from .frota_comum import (  # noqa: F401  (re-exportados: o `m2m_api.py` os importa daqui)
    PERMISSOES,
    contexto_base,
    erros_de,
    exige_api,
    exige_frota,
    filtros_ativos as _filtros_ativos,
    filtros_do_get as _filtros_do_get,
    paginar,
    pode,
    pode_ver_api,
    rotulo_de_id,
    segmentos_de,
    voltar_para,
)

POR_PAGINA = 25


# ═════════════════════════════════════════════════════════════
# Visão geral
# ═════════════════════════════════════════════════════════════

@exige_frota("ver_frota", "ver_chips")
def m2m_visao_geral(request):
    """Chips e Conectividade › Visão geral (`/agro/conectividade/`): a régua de conectividade, a
    faixa da TIM e o que pede atenção nos chips. Quem só vê a frota é levado à visão geral dela.
    O corpo mora em `frota.visao_geral_da_conectividade` (a visão geral de Frotas e Maquinários é a lista de equipamentos)."""
    if not pode(request, "ver_chips"):
        return redirect("frota_visao_geral")
    from .frota import visao_geral_da_conectividade
    return visao_geral_da_conectividade(request)


# ═════════════════════════════════════════════════════════════
# Chips: lista
# ═════════════════════════════════════════════════════════════

FILTROS_DE_CHIPS = (
    "q", "iccid", "imsi", "veiculo", "operadora", "situacao_uso", "estoque_ti", "sessao", "conectividade", "situacao", "localidade",
    "setor", "de", "ate", "ordem",
    # os que o dashboard de análise envia (m2m_analise_service): o gráfico abre a lista com o MESMO critério
    "uso", "unidade", "inatividade", "consumo", "periodo", "escopo",
)


# O que a lista aceita de quem NÃO vê API: sem sessão, sem consulta, sem consumo, sem inatividade. Filtrar por esses critérios
# já seria ler o que a API disse (`?sessao=ativa` lista quem tem sessão ativa), e ordenar por "atenção" ou "consumo" também.
FILTROS_DE_API_NA_LISTA = ("sessao", "conectividade", "situacao", "de", "ate", "inatividade", "consumo", "periodo")
ORDENS_DE_API_NA_LISTA = ("atencao", "consumo", "consulta")
FILTROS_DE_CHIPS_SEM_API = tuple(f for f in FILTROS_DE_CHIPS if f not in FILTROS_DE_API_NA_LISTA)


ROTULOS_DE_CONECTIVIDADE = {
    "ativa": "Sessão ativa", "sem_sessao": "Sem sessão", "nao_interpretado": "Não interpretado",
    "falha_consulta": "Falha de consulta", "nao_consultado": "Não consultado",
}


def _rotulos_de_filtro_de_chips(localidades, operadoras=()) -> dict:
    nomes_localidade = dict(localidades)
    return {
        "q": ("Busca", None), "iccid": ("ICCID", None), "imsi": ("IMSI", None), "veiculo": ("Veículo", None),
        "operadora": ("Operadora", lambda v: dict(operadoras).get(int(v), v) if str(v).isdigit() else v),
        "situacao_uso": ("Situação de uso", lambda v: dict(M2MChip.SituacaoUso.choices).get(v, v)),
        "estoque_ti": ("Estoque de TI", lambda v: {"com": "com item no estoque", "sem": "sem item no estoque"}.get(v, v)),
        "sessao": ("Sessão", lambda v: dict(M2MChip.SessaoEstado.choices).get(v, v)),
        "conectividade": ("Conectividade", lambda v: ROTULOS_DE_CONECTIVIDADE.get(v, v)),
        "situacao": ("Monitoramento", lambda v: m2m.ROTULO_SITUACAO.get(v, v)),
        "localidade": ("Unidade", lambda v: nomes_localidade.get(int(v), v) if str(v).isdigit() else v),
        "setor": ("Setor", None), "de": ("Consultado desde", None), "ate": ("Consultado até", None),
        "uso": ("Uso", lambda v: m2m.ROTULO_DE_USO.get(v, v)),
        "unidade": ("Unidade do equipamento", lambda v: nomes_localidade.get(int(v), v) if str(v).isdigit() else v),
        "inatividade": ("Inatividade", lambda v: analise.ROTULO_DA_FAIXA.get(v, v)),
        "consumo": ("Consumo", lambda v: analise.ROTULO_DA_CATEGORIA.get(v, v)),
        "periodo": ("Período do consumo", lambda v: {"api": "período da TIM"}.get(v, f"{v} dias")),
        "escopo": ("Chips analisados", lambda v: dict(analise.ESCOPOS).get(v, v)),
    }


@exige_frota("ver_chips")
def m2m_chips(request):
    api = pode_ver_api(request)
    filtros = _filtros_do_get(request, FILTROS_DE_CHIPS if api else FILTROS_DE_CHIPS_SEM_API)
    if not api and (filtros.get("ordem") in ORDENS_DE_API_NA_LISTA or not filtros.get("ordem")):
        filtros["ordem"] = "nome"          # o padrão da lista é "quem precisa de atenção" — uma ordem que é a da API
    page_obj, paginas, querystring = paginar(request, m2m.listar_chips(filtros))
    chips = m2m.anotar_chips(page_obj.object_list)
    opcoes = m2m.opcoes_de_filtro_de_chips()
    ativos = _filtros_ativos(request, filtros, _rotulos_de_filtro_de_chips(opcoes["localidades"], opcoes["operadoras"]))
    cfg = M2MIntegracao.get()
    # "Todos · Vinculados · Disponíveis": a relação de chips em uso x livres, com a contagem de cada um.
    # Manutenção e inativos só aparecem quando existe algum (senão seriam dois zeros sem uso).
    por_uso = dict(M2MChip.objects.values_list("situacao_uso").annotate(n=Count("pk")))
    vinculado, disponivel, manutencao, inativo = (
        M2MChip.SituacaoUso.VINCULADO.value, M2MChip.SituacaoUso.DISPONIVEL.value,
        M2MChip.SituacaoUso.MANUTENCAO.value, M2MChip.SituacaoUso.INATIVO.value,
    )
    opcoes_de_uso = [
        ("", "Todos", sum(por_uso.values())),
        (vinculado, "Vinculados", por_uso.get(vinculado, 0)),
        (disponivel, "Disponíveis", por_uso.get(disponivel, 0)),
    ] + [
        (uso, rotulo, por_uso[uso]) for uso, rotulo in ((manutencao, "Em manutenção"), (inativo, "Inativos"))
        if por_uso.get(uso)
    ]
    return render(request, "front/m2m/chips_list.html", contexto_base(
        request, "chips",
        chips=chips, page_obj=page_obj, paginas=paginas, querystring=querystring,
        filtros=filtros, filtros_ativos=ativos, opcoes=opcoes,
        segmentos_uso=segmentos_de(request, "situacao_uso", opcoes_de_uso),
        sessoes=M2MChip.SessaoEstado.choices,
        situacoes=list(m2m.ROTULO_SITUACAO.items()),
        consumo_base=m2m.base_de_consumo(cfg.mapeamento),
        mapeado=m2m.mapeamento_configurado(cfg.mapeamento),
        total_geral=M2MChip.objects.count(),
        voltar=request.get_full_path(),
    ))


# ═════════════════════════════════════════════════════════════
# Chip: detalhe
# ═════════════════════════════════════════════════════════════

FONTES_DO_HISTORICO = (
    ("consulta", "Consultas"), ("evento", "Eventos"), ("ocorrencia", "Ocorrências"),
)
# Quem não vê API só tem o histórico de cadastro e movimentação: consulta e ocorrência são o que a operadora disse.
FONTES_DO_HISTORICO_SEM_API = (("evento", "Eventos"),)


def _localizacao_do_chip(request, chip, vinculo_atual) -> dict:
    if not pode(request, "ver_frota"):
        return {"localizacao_chip": None, "posicoes_chip": []}
    achado = gps.localizacao_do_chip(chip)
    posicoes = gps.historico_do_veiculo(achado["veiculo"], limite=5) if achado["veiculo"] is not None else []
    return {"localizacao_chip": achado, "posicoes_chip": posicoes}


@exige_frota("ver_chips")
def m2m_chip_detalhe(request, pk):
    chip = get_object_or_404(
        M2MChip.objects.select_related(
            "operadora", "localidade", "responsavel", "criado_por", "atualizado_por", "desativado_por",
            "sessao_consulta", "consumo_consulta",
        ),
        pk=pk,
    )
    cfg = M2MIntegracao.get()
    api = pode_ver_api(request)
    fontes_do_historico = FONTES_DO_HISTORICO if api else FONTES_DO_HISTORICO_SEM_API
    validas = {chave for chave, _ in fontes_do_historico}
    fontes = [f for f in request.GET.getlist("hist") if f in validas]
    # Sem API, nada da operadora é nem calculado: a série de consumo, as sessões de dados, a fita das consultas, as
    # ocorrências e o limite mensal (que só serve ao alerta de consumo) ficam vazios, e o template não os desenha.
    serie = m2m.serie_consumo(chip) if api else {"pontos": [], "unidade": "", "suficiente": False}
    pontos = [
        {"em": timezone.localtime(p["em"]).isoformat(), "valor": p["valor"], "bytes": p["bytes"]} for p in serie["pontos"]
    ]
    limite_valor, limite_unidade = m2m.volume_de_bytes(chip.limite_mensal_bytes, cfg.mapeamento) if api else ("", "")
    vinculo_atual = FrotaVinculo.objects.filter(chip=chip, fim_em__isnull=True).select_related("veiculo", "item").first()
    return render(request, "front/m2m/chip_detalhe.html", contexto_base(
        request, "chips",
        chip=chip, veiculo_atual=vinculo_atual.veiculo if vinculo_atual else None,
        # O chip também pode estar num tablet/celular do ESTOQUE DE TI (`FrotaVinculo.item`): a tela mostra o
        # equipamento com link para a ficha dele, no TI. `destino_atual` é o texto pronto ("veículo X" / "equipamento Y").
        equipamento_atual=vinculo_atual.item if vinculo_atual and vinculo_atual.item_id else None,
        destino_atual=vinculos.descrever_destino(vinculo_atual) if vinculo_atual else "",
        # Para o menu "Movimentar": a URL de transferir/desvincular leva o vínculo aberto, e o
        # diálogo volta para esta mesma página (`voltar`, que o `voltar_para` só aceita do módulo).
        vinculo_atual_id=vinculo_atual.pk if vinculo_atual else None,
        voltar=request.get_full_path(),
        resumo=m2m.resumo_do_chip(chip, cfg) if api else None,
        # Onde está o chip = onde está o veículo em que ele está instalado AGORA (vínculo ativo). Só para quem vê a frota:
        # a posição do veículo é dado do módulo de frotas, não do de chips.
        **_localizacao_do_chip(request, chip, vinculo_atual),
        **contexto_do_estoque_ti(request, chip),
        sessoes_dados=dados_svc.painel_do_chip(chip) if api else None,
        fita=m2m.fita_consultas(chip, 24) if api else None,
        serie=serie,
        serie_json={"pontos": pontos, "unidade": serie["unidade"], "suficiente": serie["suficiente"]},
        linha_do_tempo=m2m.linha_do_tempo(chip, fontes or None, limite=60, com_api=api),
        fontes_do_historico=fontes_do_historico,
        fontes_ativas=fontes,
        ocorrencias_abertas=chip.ocorrencias.filter(estado__in=m2m.ABERTAS).order_by("-detectada_em") if api else [],
        mapeado=m2m.mapeamento_configurado(cfg.mapeamento),
        limite_txt=f"{limite_valor} {limite_unidade}" if (api and chip.limite_mensal_bytes) else "",
        consumo_base=m2m.base_de_consumo(cfg.mapeamento),
    ))


# ═════════════════════════════════════════════════════════════
# Chip: cadastro e edição
# ═════════════════════════════════════════════════════════════

CAMPOS_DO_FORMULARIO = (
    "nome", "iccid", "imsi", "identificador_consulta", "descricao", "localidade", "setor",
    "responsavel", "observacoes",
)


def _valores_iniciais(chip, cfg) -> dict:
    if chip is None:
        valores = {campo: "" for campo in CAMPOS_DO_FORMULARIO}
        valores.update(identificador_consulta="iccid", limite_valor="", limite_unidade="GB")
        return valores
    limite_valor, limite_unidade = m2m.volume_de_bytes(chip.limite_mensal_bytes, cfg.mapeamento)
    valores = {
        "nome": chip.nome, "iccid": chip.iccid, "imsi": chip.imsi, "identificador_consulta": chip.identificador_consulta,
        "descricao": chip.descricao, "setor": chip.setor, "observacoes": chip.observacoes,
        "localidade": chip.localidade_id or "", "responsavel": chip.responsavel_id or "",
        "limite_valor": limite_valor, "limite_unidade": limite_unidade,
    }
    return valores


def _opcao_atual(modelo, pk, rotulo):
    """`(id, texto)` da opção já escolhida — o Select2 AJAX precisa dela no HTML para mostrar o valor."""
    if not str(pk or "").isdigit():
        return None
    objeto = modelo.objects.filter(pk=int(pk)).first()
    return (objeto.pk, rotulo(objeto)) if objeto else None


def _identificador_sem_api(dados: dict, chip) -> str:
    """Quem não vê API não escolhe o identificador de consulta (é o que vai na URL da API): vale o que o chip já usa, se o campo
    ainda estiver preenchido; senão o ICCID, e o IMSI quando só ele foi informado."""
    atual = chip.identificador_consulta if chip is not None else ""
    if atual == "iccid" and dados.get("iccid"):
        return "iccid"
    if atual == "imsi" and dados.get("imsi"):
        return "imsi"
    return "iccid" if dados.get("iccid") else "imsi"


def _formulario_de_chip(request, chip=None):
    cfg = M2MIntegracao.get()
    api = pode_ver_api(request)
    valores = _valores_iniciais(chip, cfg)
    erros: dict = {}
    if request.method == "POST":
        valores = {campo: (request.POST.get(campo) or "").strip() for campo in CAMPOS_DO_FORMULARIO}
        valores["limite_valor"] = (request.POST.get("limite_valor") or "").strip()
        valores["limite_unidade"] = (request.POST.get("limite_unidade") or "GB").strip()
        dados = {campo: valores[campo] for campo in CAMPOS_DO_FORMULARIO}
        if not api:
            # Os campos da API (identificador de consulta e limite mensal, que só serve ao alerta de consumo) não existem para
            # quem não vê API: nunca vêm do POST, e na edição o chip fica com o que já tinha (`editar_chip` só muda as chaves
            # presentes em `dados`).
            dados["identificador_consulta"] = _identificador_sem_api(dados, chip)
            valores["identificador_consulta"] = dados["identificador_consulta"]
        else:
            try:
                dados["limite_mensal_bytes"] = m2m.volume_para_bytes(
                    valores["limite_valor"], valores["limite_unidade"], cfg.mapeamento,
                )
            except ValidationError as exc:
                erros["limite_valor"] = " ".join(exc.messages)
        if not erros:
            try:
                if chip is None:
                    salvo = m2m.criar_chip(dados, request.user)
                    messages.success(request, f"Chip {salvo.nome} cadastrado." + (" Ele entra na próxima rodada de consultas." if api else ""))
                else:
                    salvo = m2m.editar_chip(chip, dados, request.user)
                    messages.success(request, f"Cadastro de {salvo.nome} atualizado.")
                return redirect("m2m_chip_detalhe", pk=salvo.pk)
            except ValidationError as exc:
                erros.update(erros_de(exc))
        if erros:
            messages.error(request, "Revise os campos destacados.")
    contexto = contexto_base(
        request, "chips",
        chip=chip, valores=valores, erros=erros,
        responsavel_atual=_opcao_atual(Usuario, valores.get("responsavel"), lambda u: u.nome),
        localidades=Localidade.objects.order_by("local").values_list("pk", "local"),
        consumo_base=m2m.base_de_consumo(cfg.mapeamento),
        unidades=m2m.UNIDADES_DE_LIMITE,
    )
    return render(request, "front/m2m/chip_form.html", contexto, status=400 if erros else 200)


@exige_frota("importar_chips")
def m2m_chip_novo(request):
    return _formulario_de_chip(request)


@exige_frota("importar_chips")
def m2m_chip_editar(request, pk):
    return _formulario_de_chip(request, get_object_or_404(M2MChip, pk=pk))


# ═════════════════════════════════════════════════════════════
# Histórico de consumo
# ═════════════════════════════════════════════════════════════

def _data_ou_padrao(request, campo: str, padrao):
    """O `date` do parâmetro, o `padrao` se o parâmetro não veio, ou `None` se o
    usuário limpou o campo de propósito (parâmetro presente e vazio)."""
    if campo not in request.GET:
        return padrao
    return m2m.interpretar_data(request.GET.get(campo))


@exige_frota("consultar_conectividade")
def m2m_consumo(request):
    hoje = timezone.localdate()
    primeiro_do_mes = hoje.replace(day=1)
    fim_mes_anterior = primeiro_do_mes - timedelta(days=1)
    inicio_mes_anterior = fim_mes_anterior.replace(day=1)

    cfg = M2MIntegracao.get()
    chip_id = (request.GET.get("chip") or "").strip()
    chip = M2MChip.objects.filter(pk=int(chip_id)).first() if chip_id.isdigit() else None
    de = _data_ou_padrao(request, "de", hoje - timedelta(days=30))
    ate = _data_ou_padrao(request, "ate", hoje)
    a_de = _data_ou_padrao(request, "a_de", inicio_mes_anterior)
    a_ate = _data_ou_padrao(request, "a_ate", fim_mes_anterior)
    b_de = _data_ou_padrao(request, "b_de", primeiro_do_mes)
    b_ate = _data_ou_padrao(request, "b_ate", hoje)

    page_obj, paginas, querystring = paginar(request, m2m.historico_consumo(chip, de, ate))
    base = m2m.base_de_consumo(cfg.mapeamento)
    return render(request, "front/m2m/consumo.html", contexto_base(
        request, "consumo",
        chip=chip, chips=M2MChip.objects.order_by("nome").values_list("pk", "nome"),
        de=de, ate=ate, a_de=a_de, a_ate=a_ate, b_de=b_de, b_ate=b_ate,
        consultas=page_obj.object_list, page_obj=page_obj, paginas=paginas, querystring=querystring,
        falhas_no_periodo=m2m.falhas_de_consumo(chip, de, ate),
        comparacao=m2m.comparar_periodos(a_de, a_ate, b_de, b_ate, chip) if (a_de or a_ate) and (b_de or b_ate) else None,
        mapeado=m2m.mapeamento_configurado(cfg.mapeamento),
        consumo_base=base,
    ))


# ═════════════════════════════════════════════════════════════
# Monitoramento de sessões
# ═════════════════════════════════════════════════════════════

FILTROS_DE_SESSOES = ("q", "conectividade", "situacao")


@exige_frota("consultar_conectividade")
def m2m_sessoes(request):
    cfg = M2MIntegracao.get()
    filtros = _filtros_do_get(request, FILTROS_DE_SESSOES)
    page_obj, paginas, querystring = paginar(request, m2m.listar_chips({**filtros, "ordem": "atencao"}))
    return render(request, "front/m2m/sessoes.html", contexto_base(
        request, "sessoes",
        chips=m2m.anotar_chips(page_obj.object_list, cfg),
        page_obj=page_obj, paginas=paginas, querystring=querystring, filtros=filtros,
        filtros_ativos=_filtros_ativos(request, filtros, {
            "q": ("Busca", None),
            "conectividade": ("Conectividade", lambda v: ROTULOS_DE_CONECTIVIDADE.get(v, v)),
            "situacao": ("Monitoramento", lambda v: m2m.ROTULO_SITUACAO.get(v, v)),
        }),
        conectividade=m2m.resumo_conectividade(),
        mudancas=m2m.mudancas_de_estado(20),
        falhas=m2m.ultimas_consultas(15, tipo="sessao", so_falhas=True),
        mapeado=m2m.mapeamento_configurado(cfg.mapeamento),
    ))


# ═════════════════════════════════════════════════════════════
# Alertas e ocorrências
# ═════════════════════════════════════════════════════════════

FILTROS_DE_ALERTAS = ("estado", "nivel", "tipo", "q")


@exige_frota("ver_historico_frota")
@exige_api
def m2m_alertas(request):
    filtros = _filtros_do_get(request, FILTROS_DE_ALERTAS)
    if not filtros["estado"]:
        filtros["estado"] = "abertas"
    page_obj, paginas, querystring = paginar(request, m2m.listar_ocorrencias(filtros), por_pagina=20)
    abrir = (request.GET.get("abrir") or "").strip()
    return render(request, "front/m2m/alertas.html", contexto_base(
        request, "alertas",
        ocorrencias=page_obj.object_list, page_obj=page_obj, paginas=paginas, querystring=querystring,
        filtros=filtros, abrir=int(abrir) if abrir.isdigit() else None,
        contagem=m2m.contar_ocorrencias_por_estado(),
        niveis=M2MOcorrencia.Nivel.choices, tipos=M2MOcorrencia.Tipo.choices,
        estados=(
            ("abertas", "Abertas"), ("encerradas", "Encerradas"), ("todas", "Todas"),
        ),
        voltar=request.get_full_path(),
    ))


# ═════════════════════════════════════════════════════════════
# Configuração da integração
# ═════════════════════════════════════════════════════════════

CAMPOS_DO_MAPEAMENTO = {
    "sessao": ("campo_estado", "valores_ativa", "valores_sem_sessao", "campos_exibir"),
    "consumo": (
        "campo_volume", "unidade_no_valor", "campo_unidade", "unidade_fixa", "base",
        "campo_periodo_inicio", "campo_periodo_fim",
    ),
    "identidade": ("campo_iccid", "campo_imsi"),
}
UNIDADES_FIXAS = ("B", "KB", "MB", "GB", "TB", "KiB", "MiB", "GiB", "TiB")


def mapeamento_do_post(post) -> dict:
    """O mapeamento a partir dos campos do formulário (`secao__campo`). Só junta
    o que veio; quem valida e normaliza é `m2m.salvar_mapeamento`."""
    return {
        secao: {campo: (post.get(f"{secao}__{campo}") or "").strip() for campo in campos}
        for secao, campos in CAMPOS_DO_MAPEAMENTO.items()
    }


def _mapeamento_para_formulario(mapeamento) -> dict:
    """O mapeamento salvo como texto de formulário: listas viram uma linha por valor."""
    saida = {}
    for secao, campos in CAMPOS_DO_MAPEAMENTO.items():
        origem = (mapeamento or {}).get(secao) or {}
        saida[secao] = {}
        for campo in campos:
            valor = origem.get(campo, "")
            saida[secao][campo] = "\n".join(valor) if isinstance(valor, list) else ("" if valor is None else valor)
    if not saida["consumo"]["base"]:
        saida["consumo"]["base"] = 1000
    return saida


def contexto_de_configuracao(
    request, erros: dict | None = None, mapa_form: dict | None = None, valores: dict | None = None,
) -> dict:
    """O contexto da tela de configuração. Existe como função porque os POSTs
    (`m2m_api`) re-renderizam esta mesma tela quando a validação recusa — com o
    que o usuário digitou (`valores`, `mapa_form`), MENOS o token, que nunca volta
    para o formulário."""
    cfg = {**m2m.configuracao_atual(), **(valores or {})}
    faixa_integracao = m2m.estado_integracao()
    amostras = {tipo: m2m.amostra_de_resposta(tipo) for tipo in m2m.TIPOS_DE_CONSULTA}
    caminhos = {}
    for amostra in amostras.values():
        for c in (amostra["caminhos"] if amostra else []):
            caminhos.setdefault(c["caminho"], c)

    teste = dict(faixa_integracao["ultimo_teste"])
    if teste.get("resultado"):
        teste["em"] = parse_datetime(teste.get("em") or "")
        teste["resultado_rotulo"] = dict(M2MConsulta.Resultado.choices).get(teste["resultado"], teste["resultado"])
    try:
        conta = m2m.plano_atual(
            intervalo_ativo=int(cfg["intervalo_minutos"]), intervalo_repouso=int(cfg["intervalo_repouso_minutos"]),
            limite=int(cfg["limite_req_minuto"]),
        )
    except (TypeError, ValueError):
        conta = m2m.plano_atual()
    return contexto_base(
        request, "config",
        cfg=cfg,
        integracao=faixa_integracao,
        erros=erros or {},
        conta=conta,
        mapa=mapa_form or _mapeamento_para_formulario(cfg["mapeamento"]),
        mapeado=m2m.mapeamento_configurado(cfg["mapeamento"]),
        unidades_fixas=UNIDADES_FIXAS,
        amostras=amostras,
        caminhos=list(caminhos.values()),
        chips_para_teste=M2MChip.objects.order_by("nome").values_list("pk", "nome"),
        ultimas_falhas=m2m.ultimas_consultas(8, so_falhas=True),
        eventos=m2m.eventos_recentes(8, so_da_integracao=True),
        comando_gerar_chave=tim.COMANDO_GERAR_CHAVE,
        resultado_do_teste=teste,
    )


@exige_frota("administrar_frota_config")
def m2m_config(request):
    return render(request, "front/m2m/config.html", contexto_de_configuracao(request))
