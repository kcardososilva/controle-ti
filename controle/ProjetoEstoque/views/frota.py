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
    FrotaCentroCusto,
    FrotaClasseMecanica,
    FrotaClasseOperacional,
    FrotaEmpresa,
    FrotaImportacaoChips,
    FrotaMovimentacao,
    FrotaNatureza,
    FrotaTipo,
    FrotaVeiculo,
    M2MIntegracao,
    M2MOcorrencia,
    Usuario,
)
from services import agro_dash_service as dash
from services import agro_localizacao_service as gps
from services import m2m_analise_service as analise
from services import frota_historico_service as historico
from services import frota_import_service as imp
from services import frota_qualidade_service as qualidade
from services import frota_service as frota
from services import frota_vinculo_service as vinculos
from services import m2m_monitoramento_service as m2m
from services import tec_instalacao_service as instalacoes_tec

from .tec_comum import campo
from .frota_comum import (
    contexto_base,
    erro_de_fazenda,
    erros_de,
    exige_frota,
    filtros_ativos,
    filtros_do_get,
    migalhas,
    paginar,
    pode,
    pode_ver_api,
)


# ═════════════════════════════════════════════════════════════
# Veículo: detalhe
# ═════════════════════════════════════════════════════════════

# A linha do tempo mostra o CADASTRO (inclusive as movimentações e a validação), as MOVIMENTAÇÕES DE CHIP (que
# já descrevem cada vínculo, com motivo e responsável), as INSTALAÇÕES de equipamento tecnológico e as
# OCORRÊNCIAS. A fonte `vinculo` do service não é pedida: vínculo e movimentação de chip são o mesmo fato por
# ângulos diferentes, e mostrar os dois duplicaria cada linha.
FONTES_DO_HISTORICO_DO_VEICULO = (
    ("evento", "Cadastro"), ("movimentacao", "Chips"), ("instalacao", "Equipamentos tecnológicos"),
    ("ocorrencia", "Ocorrências"),
)


def _fontes_permitidas(request) -> list:
    """O cadastro é do veículo (`ver_frota`); chip e ocorrência pedem a permissão de histórico, e o equipamento
    tecnológico, a do estoque tecnológico."""
    liberadas = {
        "evento": True, "movimentacao": pode(request, "ver_historico_frota"),
        # a ocorrência é alerta de conectividade — o que a API disse; quem não vê API não a tem na linha do tempo
        "ocorrencia": pode(request, "ver_historico_frota") and pode_ver_api(request),
        "instalacao": request.user.has_perm("ProjetoEstoque.ver_tec_estoque"),
    }
    return [f for f in FONTES_DO_HISTORICO_DO_VEICULO if liberadas[f[0]]]


@exige_frota("ver_frota")
def frota_veiculo_detalhe(request, pk):
    veiculo = get_object_or_404(
        FrotaVeiculo.objects.select_related(
            "tipo", "localidade", "responsavel", "criado_por", "atualizado_por", "empresa", "classe_operacional",
            "classe_mecanica", "centro_custo",
        ),
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

    permitidas = _fontes_permitidas(request)
    validas = {chave for chave, _ in permitidas}
    pedidas = [f for f in request.GET.getlist("hist") if f in validas]
    linha_do_tempo = frota.linha_do_tempo_veiculo(veiculo, pedidas or sorted(validas), limite=60)

    # Composição tecnológica (equipamentos e material instalados, mais os chips que `conect` já traz): só
    # para quem vê o estoque tecnológico. O equipamento é do módulo de estoque; a máquina só o recebe.
    pode_tec = request.user.has_perm("ProjetoEstoque.ver_tec_estoque")
    composicao = instalacoes_tec.composicao_do_veiculo(veiculo) if pode_tec else None
    anteriores = instalacoes_tec.historico_do_veiculo(veiculo, limite=10) if pode_tec else []
    total_composicao = (composicao["total"] + (len(conect["chips"]) if pode_chips else (1 if conect["chips"] else 0))) if pode_tec else 0

    pode_editar = pode(request, "editar_frota")
    codigo = veiculo.codigo_legado or veiculo.codigo
    return render(request, "front/m2m/veiculo_detalhe.html", contexto_base(
        request, "veiculos", faixa=False,
        migalhas=migalhas((codigo, None)),
        pode_ver_tec=pode_tec, composicao=composicao, anteriores=anteriores, total_composicao=total_composicao,
        pode_instalar_tec=request.user.has_perm("ProjetoEstoque.instalar_tec_equipamento"),
        pode_transferir_tec=request.user.has_perm("ProjetoEstoque.transferir_tec_equipamento"),
        veiculo=veiculo, conect=conect, exige_chip=frota.exige_conectividade(veiculo),
        # onde o equipamento está e de onde vem essa informação (o detalhe do veículo já exige `ver_frota`)
        localizacao=gps.localizacao_do_veiculo(veiculo), posicoes=gps.historico_do_veiculo(veiculo, limite=10),
        conferencias=qualidade.conferencias_do_veiculo(veiculo),
        movimentacoes=historico.movimentacoes_do_veiculo(veiculo, limite=60),
        exclusao=frota.verificar_exclusao(veiculo) if pode_editar else None,
        fazendas=frota.fazendas() if pode_editar else [],
        centros_de_custo=list(FrotaCentroCusto.objects.filter(ativo=True).order_by("codigo_erp", "nome")) if pode_editar else [],
        linha_do_tempo=linha_do_tempo, fontes_do_historico=permitidas, fontes_ativas=pedidas,
        status_opcoes=list(FrotaVeiculo.Status.choices) if pode_editar else [],
        voltar=request.get_full_path(),
    ))


# ═════════════════════════════════════════════════════════════
# Veículo: cadastro e edição
# ═════════════════════════════════════════════════════════════

CAMPOS_DO_FORMULARIO_DE_VEICULO = (
    "codigo", "codigo_legado", "nome", "tipo", "classe_operacional", "classe_mecanica", "natureza", "fabricante", "modelo",
    "ano_fabricacao", "ano_modelo", "chassi_serie", "empresa", "centro_custo", "data_compra", "data_inicio", "localidade",
    "setor", "area_operacao", "responsavel", "placa", "renavam", "observacoes", "latitude", "longitude",
)
# O que o formulário mostra em data: a classe sugere a categoria e a empresa sugere a natureza (o `frota-equipamentos.js`
# lê isto por `json_script`; um .js em static/ é servido cru e não tem acesso ao banco).
_DATAS_DO_FORMULARIO = ("data_compra", "data_inicio")


def _valores_iniciais_do_veiculo(veiculo) -> dict:
    if veiculo is None:
        valores = {campo: "" for campo in CAMPOS_DO_FORMULARIO_DE_VEICULO}
        valores.update(exige_conectividade="", status=FrotaVeiculo.Status.ATIVO, natureza=FrotaNatureza.PROPRIO)
        return valores
    sobreposto = veiculo.exige_conectividade
    valores = {
        campo: ("" if getattr(veiculo, campo) is None else getattr(veiculo, campo))
        for campo in CAMPOS_DO_FORMULARIO_DE_VEICULO
        if campo not in ("tipo", "classe_operacional", "classe_mecanica", "empresa", "centro_custo", "localidade", "responsavel", "latitude", "longitude", *_DATAS_DO_FORMULARIO)
    }
    valores.update(
        tipo=veiculo.tipo_id, classe_operacional=veiculo.classe_operacional_id or "", classe_mecanica=veiculo.classe_mecanica_id or "",
        empresa=veiculo.empresa_id or "", centro_custo=veiculo.centro_custo_id or "", localidade=veiculo.localidade_id or "",
        responsavel=veiculo.responsavel_id or "",
        latitude="" if veiculo.latitude is None else str(veiculo.latitude),
        longitude="" if veiculo.longitude is None else str(veiculo.longitude),
        data_compra=veiculo.data_compra.isoformat() if veiculo.data_compra else "",
        data_inicio=veiculo.data_inicio.isoformat() if veiculo.data_inicio else "",
        exige_conectividade="" if sobreposto is None else ("sim" if sobreposto else "nao"), status=veiculo.status,
    )
    return valores


def _opcao_atual(modelo, pk, rotulo):
    """`(id, texto)` da opção já escolhida — o Select2 AJAX precisa dela no HTML para mostrar o valor."""
    if not str(pk or "").isdigit():
        return None
    objeto = modelo.objects.filter(pk=int(pk)).first()
    return (objeto.pk, rotulo(objeto)) if objeto else None


def _ativos_e_atual(modelo, atual_pk, ordem=("nome",)):
    """Os cadastros ATIVOS mais o que o equipamento já usa (um item inativado não pode sumir da edição)."""
    return modelo.objects.filter(Q(ativo=True) | Q(pk=atual_pk or 0)).order_by(*ordem)


def _com_texto_atual(valores: list, atual: str) -> list:
    """Os valores já existentes para o campo de texto com opções (modelo, setor), mais o que está digitado agora."""
    return sorted({*valores, *([atual] if atual else [])}, key=str.casefold)


def _paineis_do_formulario(request, veiculo, valores: dict, erros: dict, fazendas) -> tuple[list, dict]:
    """As SETE seções do formulário (Identificação, Classificação, Dados técnicos, Empresa e centro de custo, Datas,
    Localização e responsável, Documentos e observações) como `[{"titulo", "campos": [campo(...)]}]` — o mesmo
    descritor do formulário do estoque tecnológico (`tec_comum.campo`) — e a configuração que o JS lê: o que cada classe
    sugere de categoria, cada empresa de natureza e cada categoria de placa e chassi."""
    editando = veiculo is not None
    atual = lambda campo: getattr(veiculo, campo, None) if editando else None            # noqa: E731
    tipos = list(_ativos_e_atual(FrotaTipo, atual("tipo_id")))
    classes = list(_ativos_e_atual(FrotaClasseOperacional, atual("classe_operacional_id")).select_related("categoria"))
    mecanicas = list(_ativos_e_atual(FrotaClasseMecanica, atual("classe_mecanica_id")))
    empresas = list(_ativos_e_atual(FrotaEmpresa, atual("empresa_id")))
    centros = list(_ativos_e_atual(FrotaCentroCusto, atual("centro_custo_id"), ("codigo_erp", "nome")))
    modelos = _com_texto_atual(list(FrotaVeiculo.objects.exclude(modelo="").values_list("modelo", flat=True).distinct()), valores.get("modelo", ""))
    fabricantes = _com_texto_atual(list(FrotaVeiculo.objects.exclude(fabricante="").values_list("fabricante", flat=True).distinct()), valores.get("fabricante", ""))
    # modelo -> os fabricantes com que ele aparece no cadastro: é o que o JS usa para mostrar só os modelos do fabricante escolhido
    modelo_fabricante: dict = {}
    for fab, mod in FrotaVeiculo.objects.exclude(modelo="").exclude(fabricante="").values_list("fabricante", "modelo").distinct():
        modelo_fabricante.setdefault(mod, []).append(fab)
    setores = _com_texto_atual(list(FrotaVeiculo.objects.exclude(setor="").values_list("setor", flat=True).distinct()), valores.get("setor", ""))
    opcoes_de_fazenda = [(f.pk, f.local) for f in fazendas]
    # a unidade antiga do equipamento em edição continua à vista (senão salvar o cadastro a apagaria)
    if editando and veiculo.localidade_id and veiculo.localidade_id not in {pk for pk, _ in opcoes_de_fazenda}:
        opcoes_de_fazenda.append((veiculo.localidade_id, veiculo.localidade.local))
    responsavel = _opcao_atual(Usuario, valores.get("responsavel"), lambda u: u.nome)
    s2 = lambda texto="": {"m2m-s2": "local", "placeholder": texto}                         # noqa: E731
    por_texto = lambda texto="": {"agro-tags": "1", "placeholder": texto}                    # noqa: E731

    paineis = [
        {"id": "ident", "titulo": "Identificação", "campos": [
            campo("codigo", "Identificador interno", mono=True, maximo=30, obrigatorio=editando,
                  dica="Único; gravado em caixa alta." if editando else "Deixe em branco para o sistema gerar (MQ-00001…). Único."),
            campo("codigo_legado", "Código original (ERP)", mono=True, maximo=30, dica="O código do equipamento no ERP, como veio. Único; fica separado do interno."),
            campo("nome", "Equipamento (nome ou descrição)", obrigatorio=True, maximo=120, largo=True),
            *([campo("status", "Situação inicial", "select", opcoes=list(FrotaVeiculo.Status.choices), dica="Depois, a situação muda pelo detalhe, com motivo.")] if not editando else []),
        ]},
        {"id": "classif", "titulo": "Classificação", "novo_tipo": True, "campos": [
            campo("tipo", "Categoria", "select", obrigatorio=True, vazio="Escolher a categoria",
                  opcoes=[(t.pk, f"{t.nome}{'' if t.ativo else ' (inativo)'}") for t in tipos], dados=s2("Escolher a categoria"),
                  dica="Falta a categoria? Use “Nova categoria”." if pode(request, "administrar_frota_config")
                  else "Falta a categoria? Peça a quem administra a configuração para cadastrá-la."),
            campo("classe_operacional", "Classe operacional", "select", vazio="Sem classe", opcoes=[(c.pk, c.nome) for c in classes],
                  dica="A categoria acompanha a classe; você pode trocá-la.", dados=s2("Sem classe")),
            campo("classe_mecanica", "Classe mecânica", "select", vazio="Sem classe", opcoes=[(c.pk, c.nome) for c in mecanicas], dados=s2("Sem classe")),
            campo("natureza", "Natureza do ativo", "select", obrigatorio=True, opcoes=list(FrotaNatureza.choices),
                  dica="Próprio ou de terceiro. Terceiros não entram na conta do patrimônio próprio."),
            campo("exige_conectividade", "Exige chip de conectividade?", "select",
                  opcoes=[("", "Herdar da categoria"), ("sim", "Sim, sempre"), ("nao", "Não")]),
        ]},
        {"id": "tecnico", "titulo": "Dados técnicos", "campos": [
            campo("fabricante", "Fabricante", "select", vazio="—", opcoes=[(f, f) for f in fabricantes], dados=por_texto("Escolher ou digitar o fabricante"),
                  dica="Escolha um fabricante já cadastrado ou digite um novo. Ele filtra os modelos."),
            campo("modelo", "Modelo", "select", vazio="—", opcoes=[(m, m) for m in modelos], dados=por_texto("Escolher ou digitar o modelo"),
                  dica="Escolha um modelo já cadastrado (os do fabricante escolhido) ou digite um novo."),
            campo("ano_fabricacao", "Ano de fabricação", mono=True, maximo=4, placeholder="AAAA"),
            campo("ano_modelo", "Ano-modelo", mono=True, maximo=4, placeholder="AAAA"),
            campo("chassi_serie", "Chassi / nº de série", mono=True, maximo=30, dica="Quando aplicável. Não é exigido em equipamento que não tem.", dados={"agro-espera": "chassi"}),
        ]},
        {"id": "empresa", "titulo": "Empresa e centro de custo", "campos": [
            campo("empresa", "Empresa", "select", vazio="Sem empresa", opcoes=[(e.pk, e.nome) for e in empresas],
                  dica="A natureza do ativo acompanha a empresa; você pode trocá-la.", dados=s2("Sem empresa")),
            campo("centro_custo", "Centro de custo", "select", vazio="Sem centro de custo",
                  opcoes=[(c.pk, f"{c.codigo_erp} · {c.nome}" if c.codigo_erp else c.nome) for c in centros], dados=s2("Sem centro de custo")),
        ]},
        {"id": "datas", "titulo": "Datas", "campos": [
            campo("data_compra", "Data de compra", "date"),
            campo("data_inicio", "Data de início", "date", dica="Quando o equipamento começou a operar."),
        ]},
        {"id": "local", "titulo": "Localização e responsável", "campos": [
            campo("localidade", "Fazenda", "select", vazio="Sem fazenda", opcoes=opcoes_de_fazenda),
            campo("setor", "Setor", "select", vazio="—", opcoes=[(s, s) for s in setores], dados=por_texto("Escolher ou digitar o setor")),
            campo("area_operacao", "Área de operação", maximo=100, placeholder="Ex.: Plantio, Colheita"),
            campo("latitude", "Latitude", mono=True, maximo=20, placeholder="-14,776300",
                  dica="Opcional. A coordenada que você cadastra aqui aparece no mapa como “coordenada cadastral fixa”: não é a posição atual da máquina, e o sistema nunca a deduz do chip."),
            campo("longitude", "Longitude", mono=True, maximo=20, placeholder="-45,563400", dica="Graus decimais, junto com a latitude."),
            campo("responsavel", "Responsável", "select", vazio="—", opcoes=[responsavel] if responsavel else [],
                  dados={"m2m-s2": "ajax", "fonte": "frota_opcoes", "campo": "responsavel", "placeholder": "Buscar colaborador ativo"}),
        ]},
        {"id": "docs", "titulo": "Documentos e observações", "campos": [
            campo("placa", "Placa / identificação", mono=True, maximo=8, dados={"agro-espera": "placa"},
                  dica="Placa do veículo ou a identificação interna do equipamento. Única; não é exigida onde não se aplica."),
            campo("renavam", "RENAVAM", mono=True, maximo=14, placeholder="11 dígitos", dica="Quando aplicável."),
            campo("observacoes", "Observações", "textarea", largo=True),
        ]},
    ]
    for painel in paineis:
        for c in painel["campos"]:
            c["valor"] = valores.get(c["nome"], "")
            c["erro"] = erros.get(c["nome"], "")
    config = {
        "classe_categoria": {str(c.pk): str(c.categoria_id) for c in classes if c.categoria_id},
        "empresa_natureza": {str(e.pk): e.natureza_padrao for e in empresas},
        "tipo_espera": {str(t.pk): {"placa": t.espera_placa, "chassi": t.espera_chassi} for t in tipos},
        "modelo_fabricante": modelo_fabricante,
    }
    return paineis, config


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
        if not dados["natureza"]:
            del dados["natureza"]                                          # não enviada: vale a que o equipamento já tem (ou a padrão)
        # A fazenda é uma de DUAS (a regra é a de `frota_comum.erro_de_fazenda`)
        erro_da_fazenda = erro_de_fazenda(valores.get("localidade"), veiculo)
        if erro_da_fazenda:
            erros["localidade"] = erro_da_fazenda
        else:
            try:
                if veiculo is None:
                    salvo = frota.criar_veiculo({**dados, "status": valores["status"]}, request.user)
                    messages.success(request, f"Equipamento {salvo.codigo_legado or salvo.codigo} cadastrado.")
                else:
                    salvo = frota.editar_veiculo(veiculo, dados, request.user)
                    messages.success(request, f"Cadastro de {salvo.codigo_legado or salvo.codigo} salvo.")
                return redirect("frota_veiculo_detalhe", pk=salvo.pk)
            except ValidationError as exc:
                erros = erros_de(exc)
        messages.error(request, "Revise os campos destacados.")

    paineis, config = _paineis_do_formulario(request, veiculo, valores, erros, fazendas_do_formulario)
    codigo = (veiculo.codigo_legado or veiculo.codigo) if veiculo else ""
    contexto = contexto_base(
        request, "veiculos", faixa=False,
        migalhas=migalhas(*([(codigo, reverse("frota_veiculo_detalhe", args=[veiculo.pk])), ("Editar", None)] if veiculo else [("Novo equipamento", None)])),
        veiculo=veiculo, erros=erros, paineis=paineis, config_do_formulario=config,
        titulo=f"Editar {veiculo.nome}" if veiculo else "Novo equipamento",
        voltar_url=reverse("frota_veiculo_detalhe", args=[veiculo.pk]) if veiculo else reverse("frota_visao_geral"),
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

def visao_geral_da_conectividade(request):
    """Chips e Conectividade › Visão geral: a régua de conectividade, a faixa da TIM, chips desatualizados/com falha/
    pendentes, alertas em aberto, movimentações recentes e os passos da configuração da TIM.

    (A Visão geral de Frotas e Maquinários é a lista de equipamentos, em `frota_equipamentos.py`. Até 2026-11-13 as duas
    seções dividiam esta função, com um recorte para cada; o da frota saiu junto com a reformulação.) A conectividade é o
    que a API respondeu, e o status do veículo é decisão de quem opera: por isso as duas leituras nunca dividem o mesmo
    cartão. Cada bloco aparece só para quem tem a permissão dele."""
    ver_frota, ver_chips = pode(request, "ver_frota"), pode(request, "ver_chips")
    ver_historico = pode(request, "ver_historico_frota")
    cfg = M2MIntegracao.get()
    api = pode_ver_api(request)           # quem não vê API recebe a conectividade só como cadastro e vínculo (Portal do Colaborador)
    indicadores = frota.indicadores_frota(com_api=api)
    atencao = frota.area_de_atencao(limite=6, com_api=api)
    mapeado = m2m.mapeamento_configurado(cfg.mapeamento)

    kpis_conectividade = indicadores["conectividade"] if ver_chips else None

    # Checklist de configuração inicial: `acao` é o rótulo do botão de cada etapa pendente.
    passos = []
    if kpis_conectividade is not None:
        estado = m2m.estado_integracao()["estado"] if api else ""
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
        ]
        if api:        # configurar a integração e validar o mapeamento são passos da API
            passos += [
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
        request, "visao", faixa=True,
        kpis_conectividade=kpis_conectividade, atencao=atencao, mapeado=mapeado,
        passos=passos, mostrar_passos=not all(p["feito"] for p in passos),
        passos_feitos=passos_feitos, passos_total=len(passos),
        passos_pct=round(100 * passos_feitos / len(passos)) if passos else 0,
        sem_mapeamento=bool(api and kpis_conectividade and kpis_conectividade["chips_total"] and not mapeado["sessao"]),
        ver_historico=ver_historico,
    )
    if kpis_conectividade is not None:
        # O dashboard analítico: período (7/30/90 dias) e fazenda valem para a série e para os maiores consumos; o
        # resto (sincronização, falhas) é do momento. Tudo agregado, sem consulta por chip.
        # `unidade` é o nome novo do filtro (a unidade do EQUIPAMENTO onde o chip está); `fazenda` segue aceito.
        fazenda = (request.GET.get("unidade") or request.GET.get("fazenda") or "").strip()
        analise_painel = analise.painel_de_analise(request.GET.get("periodo"), request.GET.get("escopo"), fazenda, com_api=api)
        if api:
            # a série diária e os dias dela seguem o MESMO período da análise ("api" = desde o início que a TIM informou)
            escolhido = analise_painel["periodo"]
            dias = int(escolhido) if escolhido != "api" else (analise_painel["consumo"]["janela"]["dias"] or dash.PERIODO_PADRAO)
            contexto.update(painel_conect=dash.painel_de_conectividade(dias, fazenda), periodo=dias, periodos=dash.PERIODOS)
        contexto.update(
            analise=analise_painel,
            fazenda_pedida=fazenda if fazenda.isdigit() else "", fazendas=[(f.pk, f.local) for f in frota.fazendas()],
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

    if kpis_conectividade is not None and api:
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
