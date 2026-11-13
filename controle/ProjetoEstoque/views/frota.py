"""
Telas de Frotas e Conectividade: veículos e equipamentos (lista, cadastro, detalhe), visão geral,
movimentações, importação de chips e configuração (tipos, operadoras, regras de alerta).

As views NÃO têm regra de negócio: validam a entrada, chamam os services
(`services/frota_service.py`, `frota_vinculo_service.py`, `frota_import_service.py`) e renderizam. O que
uma tela precisa e o service ainda não tinha foi acrescentado LÁ (com teste), nunca aqui.

Acesso — `exige_frota("<codename>")` (login + permissão; sem ela, 403), ver `frota_comum.py`:

    frota_visao_geral         ver_frota OU ver_chips (cada bloco da tela, pela sua permissão)
    frota_veiculos / detalhe  ver_frota
    frota_veiculo_novo/editar editar_frota
    (os POSTs e o JSON ficam em `frota_api.py`)

Quem não é autenticado vai para o login (302); quem é, mas não tem a permissão, recebe 403. As telas
de frota pura NÃO mostram a faixa de saúde da integração TIM: ela só aparece nas de conectividade.
"""

from django.contrib import messages
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from ProjetoEstoque.models import (
    CanalNotificacao,
    ConectividadeOperadora,
    ConfiguracaoSistema,
    FrotaImportacaoChips,
    FrotaMovimentacao,
    FrotaTipo,
    FrotaVeiculo,
    Localidade,
    M2MIntegracao,
    M2MOcorrencia,
    Usuario,
)
from ProjetoEstoque.templatetags.m2m_tags import ROTULOS_CURTOS_DE_CONECTIVIDADE
from services import frota_import_service as imp
from services import frota_service as frota
from services import frota_vinculo_service as vinculos
from services import m2m_monitoramento_service as m2m
from services import tec_instalacao_service as instalacoes_tec

from .frota_comum import (
    SECAO_FROTA,
    contexto_base,
    erros_de,
    exige_frota,
    filtros_ativos,
    filtros_do_get,
    paginar,
    pode,
    rotulo_de_id,
    segmentos_de,
)


# ═════════════════════════════════════════════════════════════
# Veículos: lista
# ═════════════════════════════════════════════════════════════

FILTROS_DE_VEICULOS = (
    "q", "tipo", "localidade", "status", "chip", "conectividade", "operadora", "de", "ate", "ordem",
)


def _opcoes_de_filtro_de_veiculos() -> dict:
    """O que os selects da lista oferecem: só tipos, unidades e operadoras que de fato aparecem."""
    return {
        "tipos": list(FrotaTipo.objects.order_by("nome").values_list("pk", "nome")),
        # as fazendas sempre; unidade de outro tipo só se algum veículo (cadastrado antes) ainda a usa
        "localidades": list(
            Localidade.objects.filter(Q(veiculos_frota__isnull=False) | Q(local__in=[n for _, n in frota.FAZENDAS]))
            .distinct().order_by("local").values_list("pk", "local")
        ),
        "operadoras": list(
            ConectividadeOperadora.objects.filter(chips__isnull=False).distinct().order_by("nome").values_list("pk", "nome")
        ),
        "status": list(FrotaVeiculo.Status.choices),
        "conectividade": [(chave, ROTULOS_CURTOS_DE_CONECTIVIDADE[chave]) for chave in frota.ESTADOS_DE_CONECTIVIDADE],
    }


@exige_frota("ver_frota")
def frota_veiculos(request):
    filtros = filtros_do_get(request, FILTROS_DE_VEICULOS)
    page_obj, paginas, querystring = paginar(request, frota.listar_veiculos(filtros))
    veiculos = frota.anotar_veiculos(page_obj.object_list)
    opcoes = _opcoes_de_filtro_de_veiculos()
    ativos = filtros_ativos(request, filtros, {
        "q": ("Busca", None),
        "tipo": ("Tipo", lambda v: rotulo_de_id(opcoes["tipos"], v)),
        "localidade": ("Fazenda", lambda v: rotulo_de_id(opcoes["localidades"], v)),
        "status": ("Status", lambda v: dict(opcoes["status"]).get(v, v)),
        "chip": ("Chip", lambda v: {"com": "com chip vinculado", "sem": "sem chip"}.get(v, v)),
        "conectividade": ("Conectividade", lambda v: dict(opcoes["conectividade"]).get(v, v)),
        "operadora": ("Operadora do chip", lambda v: rotulo_de_id(opcoes["operadoras"], v)),
        "de": ("Consultado desde", None),
        "ate": ("Consultado até", None),
    })
    total_geral = FrotaVeiculo.objects.count()
    # "Todos · Com chip · Sem chip": cada contagem sai do MESMO filtro da lista, para o número do
    # segmento ser o da lista que ele abre.
    com_chip = frota.listar_veiculos({"chip": "com"}).count()
    segmentos_chip = segmentos_de(request, "chip", [
        ("", "Todos", total_geral), ("com", "Com chip", com_chip), ("sem", "Sem chip", total_geral - com_chip),
    ])
    return render(request, "front/m2m/veiculos_list.html", contexto_base(
        request, "veiculos", faixa=False,
        veiculos=veiculos, page_obj=page_obj, paginas=paginas, querystring=querystring,
        filtros=filtros, filtros_ativos=ativos, opcoes=opcoes,
        total_geral=total_geral, segmentos_chip=segmentos_chip,
    ))


# ═════════════════════════════════════════════════════════════
# Veículo: detalhe
# ═════════════════════════════════════════════════════════════

# A linha do tempo mostra o CADASTRO, as MOVIMENTAÇÕES (que já descrevem cada vínculo, com motivo e
# responsável) e as OCORRÊNCIAS. A fonte `vinculo` do service não é pedida: vínculo e movimentação
# são o mesmo fato por ângulos diferentes, e mostrar os dois duplicaria cada linha.
FONTES_DO_HISTORICO_DO_VEICULO = (
    ("evento", "Cadastro"), ("movimentacao", "Movimentações"), ("ocorrencia", "Ocorrências"),
)


@exige_frota("ver_frota")
def frota_veiculo_detalhe(request, pk):
    veiculo = get_object_or_404(
        FrotaVeiculo.objects.select_related("tipo", "localidade", "responsavel", "criado_por", "atualizado_por"),
        pk=pk,
    )
    cfg = M2MIntegracao.get()
    conect = frota.conectividade_do_veiculo(veiculo, cfg)
    pode_chips, pode_conectividade = pode(request, "ver_chips"), pode(request, "consultar_conectividade")
    if conect["chips"] and (pode_chips or pode_conectividade):
        m2m.anotar_chips([item["chip"] for item in conect["chips"]], cfg)    # consumo formatado, frescor…
    if conect["chips"] and pode_conectividade:
        for item in conect["chips"]:
            item["fita"] = m2m.fita_consultas(item["chip"], 24)

    # Cadastro é do veículo (ver_frota); movimentação e ocorrência pedem a permissão de histórico.
    permitidas = [f for f in FONTES_DO_HISTORICO_DO_VEICULO if f[0] == "evento" or pode(request, "ver_historico_frota")]
    validas = {chave for chave, _ in permitidas}
    pedidas = [f for f in request.GET.getlist("hist") if f in validas]
    linha_do_tempo = frota.linha_do_tempo_veiculo(veiculo, pedidas or sorted(validas), limite=60)

    # Composição tecnológica (equipamentos e material instalados, mais os chips que `conect` já traz): só
    # para quem vê o estoque tecnológico. O equipamento é do módulo de estoque; a máquina só o recebe.
    pode_tec = request.user.has_perm("ProjetoEstoque.ver_tec_estoque")
    composicao = instalacoes_tec.composicao_do_veiculo(veiculo) if pode_tec else None
    anteriores = instalacoes_tec.historico_do_veiculo(veiculo, limite=10) if pode_tec else []
    total_composicao = (composicao["total"] + (len(conect["chips"]) if pode_chips else (1 if conect["chips"] else 0))) if pode_tec else 0

    return render(request, "front/m2m/veiculo_detalhe.html", contexto_base(
        request, "veiculos", faixa=False,
        pode_ver_tec=pode_tec, composicao=composicao, anteriores=anteriores, total_composicao=total_composicao,
        pode_instalar_tec=request.user.has_perm("ProjetoEstoque.instalar_tec_equipamento"),
        pode_transferir_tec=request.user.has_perm("ProjetoEstoque.transferir_tec_equipamento"),
        veiculo=veiculo, conect=conect, exige_chip=frota.exige_conectividade(veiculo),
        linha_do_tempo=linha_do_tempo, fontes_do_historico=permitidas, fontes_ativas=pedidas,
        status_opcoes=list(FrotaVeiculo.Status.choices) if pode(request, "editar_frota") else [],
        voltar=request.get_full_path(),
    ))


# ═════════════════════════════════════════════════════════════
# Veículo: cadastro e edição
# ═════════════════════════════════════════════════════════════

CAMPOS_DO_FORMULARIO_DE_VEICULO = (
    "codigo", "nome", "tipo", "fabricante", "modelo", "ano_fabricacao", "placa", "renavam", "chassi_serie",
    "localidade", "setor", "area_operacao", "responsavel", "observacoes",
)


def _valores_iniciais_do_veiculo(veiculo) -> dict:
    if veiculo is None:
        valores = {campo: "" for campo in CAMPOS_DO_FORMULARIO_DE_VEICULO}
        valores.update(exige_conectividade="", status=FrotaVeiculo.Status.ATIVO)
        return valores
    sobreposto = veiculo.exige_conectividade
    return {
        "codigo": veiculo.codigo, "nome": veiculo.nome, "tipo": veiculo.tipo_id, "fabricante": veiculo.fabricante,
        "modelo": veiculo.modelo, "ano_fabricacao": veiculo.ano_fabricacao or "", "placa": veiculo.placa,
        "renavam": veiculo.renavam, "chassi_serie": veiculo.chassi_serie, "localidade": veiculo.localidade_id or "",
        "setor": veiculo.setor, "area_operacao": veiculo.area_operacao, "responsavel": veiculo.responsavel_id or "",
        "observacoes": veiculo.observacoes,
        "exige_conectividade": "" if sobreposto is None else ("sim" if sobreposto else "nao"),
        "status": veiculo.status,
    }


def _opcao_atual(modelo, pk, rotulo):
    """`(id, texto)` da opção já escolhida — o Select2 AJAX precisa dela no HTML para mostrar o valor."""
    if not str(pk or "").isdigit():
        return None
    objeto = modelo.objects.filter(pk=int(pk)).first()
    return (objeto.pk, rotulo(objeto)) if objeto else None


def _formulario_de_veiculo(request, veiculo=None):
    valores = _valores_iniciais_do_veiculo(veiculo)
    erros: dict = {}
    fazendas_do_formulario = frota.fazendas()
    if request.method == "POST":
        valores = {campo: (request.POST.get(campo) or "").strip() for campo in CAMPOS_DO_FORMULARIO_DE_VEICULO}
        valores["exige_conectividade"] = (request.POST.get("exige_conectividade") or "").strip()
        valores["status"] = (request.POST.get("status") or FrotaVeiculo.Status.ATIVO).strip()
        dados = {campo: valores[campo] for campo in CAMPOS_DO_FORMULARIO_DE_VEICULO}
        dados["exige_conectividade"] = valores["exige_conectividade"]       # "" herda do tipo; sim/nao sobrepõe
        # A fazenda é uma de DUAS. Quem já tem outra unidade (cadastrada antes desta regra) pode
        # continuar com ela ao editar; trocar para qualquer outra que não seja uma das fazendas, não.
        escolhida = valores.get("localidade") or ""
        permitidas = {f.pk for f in fazendas_do_formulario} | {getattr(veiculo, "localidade_id", None)}
        if escolhida and (not escolhida.isdigit() or int(escolhida) not in permitidas):
            erros["localidade"] = "Escolha Fazenda Karitel ou Fazenda Rio do Meio."
        else:
            try:
                if veiculo is None:
                    salvo = frota.criar_veiculo({**dados, "status": valores["status"]}, request.user)
                    messages.success(request, f"Veículo {salvo.codigo} cadastrado.")
                else:
                    salvo = frota.editar_veiculo(veiculo, dados, request.user)
                    messages.success(request, f"Cadastro de {salvo.codigo} salvo.")
                return redirect("frota_veiculo_detalhe", pk=salvo.pk)
            except ValidationError as exc:
                erros = erros_de(exc)
        messages.error(request, "Revise os campos destacados.")

    atual = getattr(veiculo, "tipo_id", None)
    tipos = FrotaTipo.objects.filter(Q(ativo=True) | Q(pk=atual)).order_by("nome")
    opcoes_de_fazenda = [(f.pk, f.local) for f in fazendas_do_formulario]
    # a unidade antiga do veículo em edição continua à vista (senão salvar o cadastro a apagaria)
    if veiculo is not None and veiculo.localidade_id and veiculo.localidade_id not in {pk for pk, _ in opcoes_de_fazenda}:
        opcoes_de_fazenda.append((veiculo.localidade_id, veiculo.localidade.local))
    contexto = contexto_base(
        request, "veiculos", faixa=False,
        veiculo=veiculo, valores=valores, erros=erros, tipos=tipos,
        status_opcoes=list(FrotaVeiculo.Status.choices),
        localidades=opcoes_de_fazenda,
        responsavel_atual=_opcao_atual(Usuario, valores.get("responsavel"), lambda u: u.nome),
    )
    return render(request, "front/m2m/veiculo_form.html", contexto, status=400 if erros else 200)


@exige_frota("editar_frota")
def frota_veiculo_novo(request):
    return _formulario_de_veiculo(request)


@exige_frota("editar_frota")
def frota_veiculo_editar(request, pk):
    return _formulario_de_veiculo(request, get_object_or_404(FrotaVeiculo.objects.select_related("tipo"), pk=pk))


# ═════════════════════════════════════════════════════════════
# Visão geral
# ═════════════════════════════════════════════════════════════

@exige_frota("ver_frota", "ver_chips")
def frota_visao_geral(request):
    """Frotas › Visão geral (`/agro/frotas/`): a régua da frota e os veículos que pedem atenção.
    Quem só vê chips é levado à visão geral da Conectividade."""
    if not pode(request, "ver_frota"):
        return redirect("m2m_visao_geral")
    return visao_geral_da_secao(request, SECAO_FROTA)


def visao_geral_da_secao(request, secao: str):
    """A visão geral de UMA seção do módulo — Frotas ou Chips e Conectividade, que no Zelo Agro são
    acessos independentes (até 2026-11-13 eram uma tela só, com as duas réguas). A regra é a mesma
    para as duas, e por isso é uma função só; o que muda é o recorte:

        Frotas          régua da frota, "veículos sem chip", o passo "Cadastrar veículos";
        Conectividade   régua de conectividade, a faixa da TIM, chips desatualizados/com falha/
                        pendentes, alertas em aberto, movimentações recentes e os passos da TIM.

    O status do veículo é decisão de quem opera; a conectividade é o que a API respondeu — as duas
    réguas nunca dividem o mesmo cartão. Cada bloco aparece só para quem tem a permissão dele."""
    da_frota = secao == SECAO_FROTA
    ver_frota, ver_chips = pode(request, "ver_frota"), pode(request, "ver_chips")
    ver_historico = pode(request, "ver_historico_frota") and not da_frota
    cfg = M2MIntegracao.get()
    indicadores = frota.indicadores_frota()
    atencao = frota.area_de_atencao(limite=6)
    mapeado = m2m.mapeamento_configurado(cfg.mapeamento)

    kpis_frota = indicadores["frota"] if (da_frota and ver_frota) else None
    kpis_conectividade = indicadores["conectividade"] if (not da_frota and ver_chips) else None

    # Checklist de configuração inicial: `acao` é o rótulo do botão de cada etapa pendente.
    passos = []
    if kpis_frota is not None:
        passos.append({
            "feito": indicadores["frota"]["total"] + indicadores["frota"]["desativados"] > 0,
            "rotulo": "Cadastrar veículos",
            "acao": "Cadastrar",
            "url": reverse("frota_veiculo_novo") if pode(request, "editar_frota") else "",
        })
    if kpis_conectividade is not None:
        estado = m2m.estado_integracao()["estado"]
        pode_importar = pode(request, "importar_chips")
        passos += [
            {
                "feito": indicadores["conectividade"]["chips_total"] > 0,
                "rotulo": "Cadastrar ou importar chips",
                "acao": "Importar planilha",
                "url": reverse("frota_importar") if pode_importar else "",
                "acao2": "Cadastrar chip",
                "url2": reverse("m2m_chip_novo") if pode_importar else "",
            },
            {
                "feito": indicadores["conectividade"]["chips_vinculados"] > 0,
                "rotulo": "Vincular chips aos veículos",
                "acao": "Ver veículos",
                "url": reverse("frota_veiculos") if ver_frota else "",
            },
            {
                "feito": estado not in ("nao_configurada", "url_invalida", "token_ilegivel"),
                "rotulo": "Configurar a integração TIM",
                "acao": "Configurar",
                "url": reverse("m2m_config") if pode(request, "administrar_frota_config") else "",
            },
            {
                "feito": mapeado["sessao"] and mapeado["consumo"],
                "rotulo": "Validar o mapeamento dos dados da API",
                "acao": "Mapear campos",
                "url": (reverse("m2m_config") + "#mapeamento") if pode(request, "administrar_frota_config") else "",
            },
        ]
    passos_feitos = sum(1 for p in passos if p["feito"])

    contexto = contexto_base(
        request, "visao_frota" if da_frota else "visao", faixa=not da_frota,
        kpis_frota=kpis_frota, kpis_conectividade=kpis_conectividade, atencao=atencao, mapeado=mapeado,
        passos=passos, mostrar_passos=not all(p["feito"] for p in passos),
        passos_feitos=passos_feitos, passos_total=len(passos),
        passos_pct=round(100 * passos_feitos / len(passos)) if passos else 0,
        sem_mapeamento=bool(kpis_conectividade and kpis_conectividade["chips_total"] and not mapeado["sessao"]),
        ver_historico=ver_historico,
    )

    # A área de atenção é UMA lista: só entram os tipos de pendência que têm ocorrência e que o
    # usuário pode ver. Cada um leva o total, até três exemplos e o link para a lista filtrada.
    pendencias = []

    def _pendencia(tipo: str, titulo: str, bloco: dict, url: str) -> None:
        if bloco["total"]:
            itens = bloco["itens"][:3]
            pendencias.append({
                "tipo": tipo, "titulo": titulo, "total": bloco["total"], "itens": itens, "url": url,
                "restantes": bloco["total"] - len(itens),
            })

    if kpis_frota is not None:
        _pendencia("veiculo", "Veículos sem chip", atencao["veiculos_sem_chip"],
                   reverse("frota_veiculos") + "?chip=sem&status=ativo")
    if kpis_conectividade is not None:
        lista = reverse("m2m_chips") + "?situacao_uso=vinculado&situacao="
        _pendencia("desatualizado", "Chips sem atualização recente", atencao["chips_sem_atualizacao"], lista + "desatualizado")
        _pendencia("falha", "Falhas de comunicação", atencao["falhas_de_comunicacao"], lista + "erro_consulta")
        _pendencia("pendente", "Conectividade pendente", atencao["conectividade_pendente"], lista + "pendente")
    if ver_historico and contexto["m2m_alertas_abertos"]:
        pendencias.append({
            "tipo": "alerta", "titulo": "Alertas em aberto", "total": contexto["m2m_alertas_abertos"],
            "itens": [], "url": reverse("m2m_alertas"),
        })
    contexto["pendencias"] = pendencias
    return render(request, "front/m2m/visao_geral_frota.html", contexto)


# ═════════════════════════════════════════════════════════════
# Movimentações (histórico imutável do chip)
# ═════════════════════════════════════════════════════════════

FILTROS_DE_MOVIMENTACOES = ("tipo", "chip", "veiculo", "usuario", "de", "ate", "q")


@exige_frota("ver_historico_frota")
def frota_movimentacoes(request):
    """O registro imutável das movimentações dos chips, com filtros e exportação para Excel
    (`?formato=xlsx` leva os MESMOS filtros). A substituição grava duas linhas com a mesma `operacao`
    e aparece aqui como UMA ação."""
    filtros = filtros_do_get(request, FILTROS_DE_MOVIMENTACOES)
    if request.GET.get("formato") == "xlsx":
        conteudo = vinculos.exportar_movimentacoes_xlsx(filtros)
        resposta = HttpResponse(
            conteudo, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        resposta["Content-Disposition"] = f'attachment; filename="movimentacoes_{timezone.localdate():%Y%m%d}.xlsx"'
        return resposta

    page_obj, paginas, querystring = paginar(request, vinculos.historico_movimentacoes(filtros), por_pagina=30)
    grupos = vinculos.agrupar_por_operacao(page_obj.object_list)
    ativos = filtros_ativos(request, filtros, {
        "tipo": ("Tipo", lambda v: dict(FrotaMovimentacao.Tipo.choices).get(v, v)),
        "chip": ("Chip", None), "veiculo": ("Veículo", None), "usuario": ("Usuário", None),
        "de": ("De", None), "ate": ("Até", None), "q": ("Busca", None),
    })
    parametros = request.GET.copy()
    parametros.pop("page", None)
    parametros.pop("formato", None)
    parametros["formato"] = "xlsx"
    return render(request, "front/m2m/movimentacoes.html", contexto_base(
        request, "movimentacoes", faixa=False,
        grupos=grupos, page_obj=page_obj, paginas=paginas, querystring=querystring,
        filtros=filtros, filtros_ativos=ativos, tipos=FrotaMovimentacao.Tipo.choices,
        total_geral=FrotaMovimentacao.objects.count(), url_exportar="?" + parametros.urlencode(),
    ))


# ═════════════════════════════════════════════════════════════
# Importação de chips por planilha
# ═════════════════════════════════════════════════════════════

SITUACOES_DA_IMPORTACAO = (
    ("", "Todas"), ("valida", "Válidas"), ("existente", "Já cadastradas"), ("rejeitada", "Rejeitadas"),
    ("importada", "Importadas"),
)


def _contexto_de_importar(request, erro: str = "", **extra) -> dict:
    return contexto_base(
        request, "importar", faixa=False,
        erro=erro, importacoes=imp.listar_importacoes(limite=8),
        colunas=imp.ROTULOS, max_mb=imp.MAX_BYTES // (1024 * 1024), max_linhas=imp.MAX_LINHAS, **extra,
    )


@exige_frota("importar_chips")
def frota_importar(request):
    """Passos 1 e 2 da importação (modelo e envio). O POST lê e VALIDA a planilha — nada é gravado nos
    chips — e leva para a prévia (`frota_importacao`). Arquivo ilegível, grande demais, de formato
    errado ou sem a coluna ICCID/IMSI volta aqui com o motivo e a lista das colunas esperadas."""
    if request.method != "POST":
        return render(request, "front/m2m/importar.html", _contexto_de_importar(request))

    arquivo = request.FILES.get("arquivo")
    if arquivo is None:
        erro = "Escolha a planilha (.xlsx ou .csv) para enviar."
    elif arquivo.size > imp.MAX_BYTES:
        erro = f"O arquivo passa do limite de {imp.MAX_BYTES // (1024 * 1024)} MB."
    else:
        try:
            importacao = imp.analisar(arquivo, request.user)
        except ValidationError as exc:
            erro = " ".join(exc.messages)
        else:
            messages.success(request, "Planilha lida. Confira a prévia antes de importar: nada foi gravado ainda.")
            return redirect("frota_importacao", pk=importacao.pk)
    return render(request, "front/m2m/importar.html", _contexto_de_importar(request, erro=erro), status=400)


@exige_frota("importar_chips")
def frota_importacoes(request):
    """Todas as importações já feitas (a lista de auditoria): quem enviou o quê, e o que aconteceu."""
    page_obj, paginas, querystring = paginar(
        request, FrotaImportacaoChips.objects.select_related("criada_por", "confirmada_por").order_by("-criada_em", "-pk"),
        por_pagina=30,
    )
    return render(request, "front/m2m/importacoes.html", contexto_base(
        request, "importar", faixa=False,
        importacoes=page_obj.object_list, page_obj=page_obj, paginas=paginas, querystring=querystring,
    ))


@exige_frota("importar_chips")
def frota_importacao(request, pk):
    """Passo 3 (prévia: o que cada linha vai virar) e passo 4 (resultado), pela situação da importação."""
    importacao = get_object_or_404(FrotaImportacaoChips.objects.select_related("criada_por", "confirmada_por"), pk=pk)
    situacao = (request.GET.get("situacao") or "").strip()
    if situacao not in dict(SITUACOES_DA_IMPORTACAO):
        situacao = ""
    page_obj, paginas, querystring = paginar(request, imp.linhas_da_importacao(importacao, situacao or None), por_pagina=50)
    linhas = [{"obj": l, **imp.linha_do_relatorio(l)} for l in page_obj.object_list]
    contagem = {
        "": importacao.linhas, "valida": importacao.validas, "existente": importacao.existentes,
        "rejeitada": importacao.rejeitados, "importada": importacao.importados,
    }
    previa_aberta = importacao.estado == FrotaImportacaoChips.Estado.PREVIA and not importacao.expirada
    return render(request, "front/m2m/importacao.html", contexto_base(
        request, "importar", faixa=False,
        importacao=importacao, linhas=linhas, page_obj=page_obj, paginas=paginas, querystring=querystring,
        situacao=situacao, situacoes=[(chave, rotulo, contagem[chave]) for chave, rotulo in SITUACOES_DA_IMPORTACAO],
        previa_aberta=previa_aberta, ja_importado=imp.arquivo_ja_importado(importacao),
        resultado=importacao.estado != FrotaImportacaoChips.Estado.PREVIA,
    ))


# ═════════════════════════════════════════════════════════════
# Configuração: tipos de veículo, operadoras e regras de alerta
# (a quarta aba, "Integração TIM", é `m2m_config` em `m2m.py`)
# ═════════════════════════════════════════════════════════════

@exige_frota("administrar_frota_config")
def frota_config_tipos(request):
    """Tipos de veículo: o nome e se o tipo EXIGE chip (padrão do tipo; o veículo pode sobrepor).
    Tipo inativo some do cadastro de veículo, mas os veículos dele ficam."""
    return render(request, "front/m2m/config_tipos.html", contexto_base(
        request, "config", tipos=frota.listar_tipos(), aba_config="tipos",
    ))


@exige_frota("administrar_frota_config")
def frota_config_operadoras(request):
    """Operadoras: o nome e a INTEGRAÇÃO. Sem integração o chip é cadastrado e controlado, mas nunca
    consultado pela API — a tela diz isso, em vez de mostrá-lo como falha."""
    return render(request, "front/m2m/config_operadoras.html", contexto_base(
        request, "config", operadoras=frota.listar_operadoras(), aba_config="operadoras",
        integracoes=ConectividadeOperadora.Integracao.choices,
    ))


@exige_frota("administrar_frota_config")
def frota_config_regras(request):
    """Regras de alerta: para cada tipo de ocorrência, se gera ocorrência, com que nível, se avisa por
    e-mail e a QUEM. O e-mail só sai se a chave-mestra e o canal da Central de Alertas estiverem ativos."""
    regras = frota.listar_regras()
    for regra in regras:
        regra.ids_usuarios = {u.pk for u in regra.destinatarios_usuarios.all()}
    escolhidos = {pk for regra in regras for pk in regra.ids_usuarios}
    usuarios = (
        User.objects.filter(is_active=True).filter(~Q(email="") | Q(pk__in=escolhidos))
        .order_by("first_name", "username")
    )
    canal = CanalNotificacao.objects.filter(codigo="m2m_ocorrencias").first()
    return render(request, "front/m2m/config_regras.html", contexto_base(
        request, "config", regras=regras, usuarios=usuarios, aba_config="regras", niveis=M2MOcorrencia.Nivel.choices,
        chave_mestra_ativa=ConfiguracaoSistema.get().alertas_email_ativos,
        canal_ativo=canal.ativo if canal else True,
    ))
