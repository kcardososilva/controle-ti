"""
Telas do ESTOQUE TECNOLÓGICO do Zelo Agro: visão geral, equipamentos individuais, materiais por
quantidade e o razão de movimentações. O catálogo (fabricantes, categorias, modelos, locais…) está
em `views/tec_catalogo.py`.

Cada tela exige uma permissão (`exige_tec`) e a regra de negócio mora nos services
(`tec_estoque_service`, `tec_catalogo_service`). As operações (entrada, saída, ajuste, mover,
inativar) são formulários de página inteira, com token de envio único: um segundo clique não registra
duas vezes. Quem pode o quê:

    ver_tec_estoque            ver as listas, os detalhes e o catálogo
    cadastrar_tec_equipamento  cadastrar/editar equipamento, entrada, saída e ajuste de material, inativar
    transferir_tec_equipamento mudar equipamento ou material de local de armazenamento
    ver_tec_historico          o razão de movimentações
"""

from decimal import Decimal

from django.db.models import Count
from django.shortcuts import get_object_or_404, render
from django.urls import reverse

from ProjetoEstoque.models import (
    AgroFornecedor,
    AgroLocal,
    TecCategoria,
    TecControle,
    TecEquipamento,
    TecFabricante,
    TecModelo,
    TecMovimentacao,
)
from ProjetoEstoque.views.tec_comum import (
    POR_PAGINA,  # noqa: F401
    campo,
    contexto_base,
    exige_tec,
    filtros_ativos,
    filtros_do_get,
    formulario,
    opcoes_de,
    paginar,
    rotulo_de_id,
    segmentos_de,
)
from services import frota_service
from services import tec_catalogo_service as catalogo
from services import tec_estoque_service as estoque
from services import tec_instalacao_service as instalacao_service

Situacao = TecEquipamento.Situacao
ROTULO_DA_SITUACAO = dict(Situacao.choices)

FILTROS_DE_EQUIPAMENTOS = ("q", "fabricante", "categoria", "grupo", "modelo", "situacao", "fazenda", "local")
FILTROS_DE_MATERIAIS = ("q", "fabricante", "categoria", "grupo", "local", "abaixo_minimo")
FILTROS_DO_RAZAO = ("q", "tipo", "fabricante", "usuario", "de", "ate")


# ═════════════════════════════════════════════════════════════
# Opções de formulário
# ═════════════════════════════════════════════════════════════

def _modelos_agrupados(controle: str, atual=None) -> list:
    """`[(fabricante, [(pk, "Modelo · categoria")])]` dos modelos ATIVOS de um controle; o modelo
    `atual` entra mesmo inativo."""
    modelos = list(TecModelo.objects.filter(controle=controle, ativo=True).select_related("fabricante", "categoria"))
    if atual is not None and atual.pk not in {m.pk for m in modelos}:
        modelos.append(atual)
    grupos: dict = {}
    for m in sorted(modelos, key=lambda m: (m.fabricante.nome.lower(), m.nome.lower())):
        grupos.setdefault(m.fabricante.nome, []).append((m.pk, f"{m.nome} · {m.categoria.nome}"))
    return list(grupos.items())


def _campos_de_modelo(controle: str, rotulo: str, *, atual=None, valor="", vazio: str, dica: str = "", obrigatorio: bool = True) -> list:
    """Os DOIS campos que escolhem um modelo: o FABRICANTE (auxiliar, só filtra) e o MODELO, que depende dele.

    Escolher o fabricante deixa na lista só os modelos dele e limpa um modelo que não é dele (`data-s2-pai` em
    `static/js/agro-select.js`, que casa o grupo do modelo com o nome do fabricante). Não escolher fabricante
    mostra todos, agrupados por fabricante, e a busca do modelo também acha pelo nome do fabricante. O que o
    service recebe e valida é o modelo (`fabricante_filtro` não entra nos dados): um fabricante que não bate com
    o modelo simplesmente não existe para o servidor, que confere o modelo contra o catálogo."""
    grupos = _modelos_agrupados(controle, atual)
    nomes_de_fabricantes = [nome for nome, _ in grupos]
    fabricantes = TecFabricante.objects.filter(nome__in=nomes_de_fabricantes).order_by("nome")
    fabricante_atual = ""
    if atual is not None:
        fabricante_atual = atual.fabricante_id
    elif valor:
        escolhido = TecModelo.objects.filter(pk=valor).values_list("fabricante_id", flat=True).first()
        fabricante_atual = escolhido or ""
    return [
        campo("fabricante_filtro", "Fabricante", "select", opcoes=[(f.pk, f.nome) for f in fabricantes], vazio="Todos os fabricantes",
              valor=fabricante_atual, auxiliar=True, dica="Opcional: filtra os modelos pelo fabricante."),
        campo("modelo", rotulo, "select", obrigatorio=obrigatorio, grupos=grupos, vazio=vazio, valor=valor or getattr(atual, "pk", ""),
              dados={"s2-pai": "fabricante_filtro"}, dica=dica),
    ]


def _locais_das_fazendas() -> list:
    """O que as telas de estoque oferecem como "local de armazenamento": só a Fazenda Karitel e a
    Fazenda Rio do Meio (`catalogo.locais_das_fazendas`). É escolha de TELA — os services continuam
    aceitando qualquer local ativo do catálogo, então o que já está num Almoxarifado ou numa Oficina
    segue consultável e movível (ver `_locais_com_saldo`)."""
    return opcoes_de(catalogo.locais_das_fazendas())


def _locais_com_saldo(modelo: TecModelo, *, so_com_saldo: bool) -> list:
    """As duas fazendas com o saldo do modelo no rótulo: "Fazenda Karitel — 60 m". `so_com_saldo` tira
    as vazias (saída e transferência só fazem sentido onde há material). Um local antigo do catálogo
    (Almoxarifado, Oficina…) que AINDA tem saldo entra também, pelo nome dele: sumir da lista deixaria
    o material sem como sair."""
    saldos = {s.local_id: s.quantidade for s in estoque.saldos_do_modelo(modelo)}
    sigla = modelo.unidade.sigla if modelo.unidade_id else "un"
    padroes = catalogo.locais_das_fazendas()
    com_saldo = [pk for pk, q in saldos.items() if q > 0 and pk not in {l.pk for l in padroes}]
    antigos = AgroLocal.objects.filter(pk__in=com_saldo, ativo=True).select_related("fazenda").order_by("fazenda__local", "nome")
    opcoes = []
    for local in [*padroes, *antigos]:
        quantidade = saldos.get(local.pk, Decimal("0"))
        if so_com_saldo and quantidade <= 0:
            continue
        opcoes.append((local.pk, f"{local} — {quantidade.normalize():f} {sigla}"))
    return opcoes


def _fornecedores(atual=None) -> list:
    return opcoes_de(AgroFornecedor.objects.filter(ativo=True).order_by("nome"), atual=atual)


# ═════════════════════════════════════════════════════════════
# Visão geral
# ═════════════════════════════════════════════════════════════

@exige_tec("ver_tec_estoque")
def tec_visao_geral(request):
    contexto = contexto_base(
        request, "visao",
        resumo=estoque.resumo_estoque(),
        faltas=estoque.abaixo_do_minimo(limite=8),
        sem_modelos=not TecModelo.objects.filter(ativo=True).exists(),
        situacoes=[(valor, rotulo) for valor, rotulo in Situacao.choices],
    )
    if contexto["pode_historico"]:
        contexto["ultimas"] = list(estoque.historico_movimentacoes()[:8])
    return render(request, "front/agro/tec/visao_geral.html", contexto)


# ═════════════════════════════════════════════════════════════
# Equipamentos individuais
# ═════════════════════════════════════════════════════════════

def _opcoes_de_filtro_de_equipamentos() -> dict:
    return {
        "fabricantes": list(TecFabricante.objects.filter(modelos__isnull=False).distinct().order_by("nome").values_list("pk", "nome")),
        "categorias": list(TecCategoria.objects.filter(modelos__controle=TecControle.INDIVIDUAL).distinct().order_by("nome").values_list("pk", "nome")),
        "grupos": list(TecCategoria.Grupo.choices),
        "fazendas": [(f.pk, f.local) for f in frota_service.fazendas()],
        "locais": [(l.pk, str(l)) for l in AgroLocal.objects.select_related("fazenda").order_by("fazenda__local", "nome")],
        "situacoes": list(Situacao.choices),
    }


@exige_tec("ver_tec_estoque")
def tec_equipamentos(request):
    filtros = filtros_do_get(request, FILTROS_DE_EQUIPAMENTOS)
    consulta = estoque.listar_equipamentos(filtros)
    page_obj, paginas, querystring = paginar(request, consulta)
    opcoes = _opcoes_de_filtro_de_equipamentos()
    # "Todos · Disponíveis · Instalados…": a contagem de cada segmento sai do MESMO filtro da lista
    # (menos a situação), para o número do segmento ser o da lista que ele abre.
    sem_situacao = estoque.listar_equipamentos({**filtros, "situacao": ""}).order_by()
    por_situacao = dict(sem_situacao.values_list("situacao").annotate(n=Count("pk")).values_list("situacao", "n"))
    segmentos = segmentos_de(request, "situacao", [("", "Todos", sum(por_situacao.values()))] + [
        (valor, rotulo, por_situacao.get(valor, 0)) for valor, rotulo in Situacao.choices
    ])
    ativos = filtros_ativos(request, filtros, {
        "q": ("Busca", None),
        "fabricante": ("Fabricante", lambda v: rotulo_de_id(opcoes["fabricantes"], v)),
        "categoria": ("Categoria", lambda v: rotulo_de_id(opcoes["categorias"], v)),
        "grupo": ("Grupo", lambda v: dict(opcoes["grupos"]).get(v, v)),
        "fazenda": ("Fazenda", lambda v: rotulo_de_id(opcoes["fazendas"], v)),
        "local": ("Local", lambda v: rotulo_de_id(opcoes["locais"], v)),
        "situacao": ("Situação", lambda v: ROTULO_DA_SITUACAO.get(v, v)),
        "modelo": ("Modelo", None),
    })
    total_geral = TecEquipamento.objects.count()
    return render(request, "front/agro/tec/equipamentos_list.html", contexto_base(
        request, "equipamentos", equipamentos=page_obj.object_list, page_obj=page_obj, paginas=paginas, querystring=querystring,
        filtros=filtros, filtros_ativos=ativos, opcoes=opcoes, segmentos=segmentos, total_geral=total_geral,
        sem_modelos=not TecModelo.objects.filter(ativo=True, controle=TecControle.INDIVIDUAL).exists(),
    ))


@exige_tec("ver_tec_estoque")
def tec_equipamento_detalhe(request, pk):
    equipamento = get_object_or_404(
        TecEquipamento.objects.select_related("modelo__fabricante", "modelo__categoria", "local__fazenda", "fornecedor", "criado_por", "atualizado_por"),
        pk=pk,
    )
    return render(request, "front/agro/tec/equipamento_detalhe.html", contexto_base(
        request, "equipamentos", equipamento=equipamento, linha_do_tempo=estoque.linha_do_tempo_equipamento(equipamento),
        instalacao=equipamento.instalacao_ativa, periodos=list(instalacao_service.instalacoes_do_equipamento(equipamento)),
        responsavel=instalacao_service.responsavel_do_equipamento(equipamento),
        pode_ver_frota=request.user.has_perm("ProjetoEstoque.ver_frota"),
    ))


def _campos_do_equipamento(equipamento=None) -> list:
    criando = equipamento is None
    campos = [
        *_campos_de_modelo(TecControle.INDIVIDUAL, "Modelo", atual=getattr(equipamento, "modelo", None), vazio="Escolher o modelo",
                           dica="Falta o modelo? Cadastre-o no catálogo (fabricante, categoria e nome)."),
        campo("numero_serie", "Número de série", valor=getattr(equipamento, "numero_serie", ""), maximo=80, mono=True,
              dica="Único por fabricante. Informe o número de série, o patrimônio ou os dois."),
        campo("patrimonio", "Patrimônio / identificação interna", valor=getattr(equipamento, "patrimonio", ""), maximo=40, mono=True, dica="Único."),
        campo("data_aquisicao", "Data de aquisição", "date", valor=equipamento.data_aquisicao.isoformat() if getattr(equipamento, "data_aquisicao", None) else ""),
        campo("fornecedor", "Fornecedor", "select", opcoes=_fornecedores(getattr(equipamento, "fornecedor", None)), vazio="Sem fornecedor",
              valor=getattr(equipamento, "fornecedor_id", "")),
    ]
    if criando:
        campos.insert(1, campo("local", "Local de armazenamento", "select", obrigatorio=True, opcoes=_locais_das_fazendas(), vazio="Escolher o local",
                               dica="Em qual fazenda o equipamento fica guardado até ser instalado."))
        campos.append(campo("documento", "Documento de entrada", maximo=60, placeholder="Ex.: NF 1234", dica="Nota fiscal ou outro documento, se houver."))
    campos.append(campo("observacoes", "Observações técnicas", "textarea", valor=getattr(equipamento, "observacoes", ""), largo=True))
    return campos


@exige_tec("cadastrar_tec_equipamento")
def tec_equipamento_novo(request):
    def executar(dados, token):
        resultado = estoque.criar_equipamento(dados, request.user, token=token)
        eq = resultado.equipamento
        return f"Equipamento {eq.codigo} cadastrado e colocado em {eq.local}.", reverse("tec_equipamento_detalhe", args=[eq.pk]), resultado.repetida

    return formulario(
        request, aba="equipamentos", titulo="Novo equipamento", descricao="Cadastre um equipamento individual: ele entra no estoque e passa a ter histórico próprio.",
        voltar_url=reverse("tec_equipamentos"), voltar_rotulo="Equipamentos", campos=_campos_do_equipamento(), executar=executar, rotulo_enviar="Cadastrar equipamento",
        secoes=[("Identificação", ["fabricante_filtro", "modelo", "numero_serie", "patrimonio"]), ("Entrada no estoque", ["local", "data_aquisicao", "fornecedor", "documento"]), ("Observações", ["observacoes"])],
    )


@exige_tec("cadastrar_tec_equipamento")
def tec_equipamento_editar(request, pk):
    equipamento = get_object_or_404(TecEquipamento.objects.select_related("modelo__fabricante", "fornecedor", "local"), pk=pk)

    def executar(dados, token):
        estoque.editar_equipamento(equipamento, dados, request.user)
        return f"Cadastro de {equipamento.codigo} salvo.", reverse("tec_equipamento_detalhe", args=[equipamento.pk])

    return formulario(
        request, aba="equipamentos", titulo=f"Editar {equipamento.codigo}", descricao="Corrige o cadastro. Local e situação mudam só por operação (mover, instalar, enviar para manutenção…), para o histórico contar a história.",
        voltar_url=reverse("tec_equipamento_detalhe", args=[equipamento.pk]), voltar_rotulo=equipamento.codigo, campos=_campos_do_equipamento(equipamento), executar=executar,
        tem_token=False, secoes=[("Identificação", ["fabricante_filtro", "modelo", "numero_serie", "patrimonio"]), ("Aquisição", ["data_aquisicao", "fornecedor"]), ("Observações", ["observacoes"])],
    )


@exige_tec("transferir_tec_equipamento")
def tec_equipamento_mover(request, pk):
    equipamento = get_object_or_404(TecEquipamento.objects.select_related("modelo__fabricante", "local__fazenda"), pk=pk)
    destinos = [o for o in _locais_das_fazendas() if o[0] != equipamento.local_id]

    def executar(dados, token):
        resultado = estoque.mover_equipamento_de_local(equipamento, dados.get("local"), request.user, dados.get("motivo"), observacoes=dados.get("observacoes", ""), token=token)
        novo = AgroLocal.objects.get(pk=resultado.equipamento.local_id)
        return f"{equipamento.codigo} agora está em {novo}.", reverse("tec_equipamento_detalhe", args=[equipamento.pk]), resultado.repetida

    return formulario(
        request, aba="equipamentos", titulo=f"Mudar {equipamento.codigo} de local", descricao=f"{equipamento.modelo} · hoje em {equipamento.local or 'lugar nenhum'}. Fica registrado no histórico, sem poder ser editado.",
        voltar_url=reverse("tec_equipamento_detalhe", args=[equipamento.pk]), voltar_rotulo=equipamento.codigo, rotulo_enviar="Mudar de local", executar=executar,
        campos=[
            campo("local", "Novo local de armazenamento", "select", obrigatorio=True, opcoes=destinos, vazio="Escolher o local"),
            campo("motivo", "Motivo", obrigatorio=True, maximo=255, largo=True),
            campo("observacoes", "Observações", "textarea", largo=True),
        ],
    )


@exige_tec("cadastrar_tec_equipamento")
def tec_equipamento_inativar(request, pk):
    equipamento = get_object_or_404(TecEquipamento.objects.select_related("modelo__fabricante", "local"), pk=pk)

    def executar(dados, token):
        resultado = estoque.inativar_equipamento(equipamento, request.user, dados.get("motivo"), observacoes=dados.get("observacoes", ""), token=token)
        return f"{equipamento.codigo} foi inativado.", reverse("tec_equipamento_detalhe", args=[equipamento.pk]), resultado.repetida

    return formulario(
        request, aba="equipamentos", titulo=f"Inativar {equipamento.codigo}", descricao=f"{equipamento.modelo} · {ROTULO_DA_SITUACAO[equipamento.situacao]}.",
        voltar_url=reverse("tec_equipamento_detalhe", args=[equipamento.pk]), voltar_rotulo=equipamento.codigo, rotulo_enviar="Inativar equipamento", perigo=True, executar=executar,
        aviso="É uma baixa: o equipamento sai de circulação e não volta a ser escolhido em nenhuma operação. O histórico fica guardado. Um equipamento instalado precisa ser retirado antes.",
        campos=[campo("motivo", "Motivo da baixa", obrigatorio=True, maximo=255, largo=True, placeholder="Ex.: queimou, perdido, descartado"), campo("observacoes", "Observações", "textarea", largo=True)],
    )


# ═════════════════════════════════════════════════════════════
# Materiais por quantidade
# ═════════════════════════════════════════════════════════════

@exige_tec("ver_tec_estoque")
def tec_materiais(request):
    filtros = filtros_do_get(request, FILTROS_DE_MATERIAIS)
    consulta = estoque.listar_materiais(filtros)
    page_obj, paginas, querystring = paginar(request, consulta)
    opcoes = {
        "fabricantes": list(TecFabricante.objects.filter(modelos__controle=TecControle.QUANTIDADE).distinct().order_by("nome").values_list("pk", "nome")),
        "categorias": list(TecCategoria.objects.filter(modelos__controle=TecControle.QUANTIDADE).distinct().order_by("nome").values_list("pk", "nome")),
        "grupos": list(TecCategoria.Grupo.choices),
        "locais": [(l.pk, str(l)) for l in AgroLocal.objects.select_related("fazenda").order_by("fazenda__local", "nome")],
    }
    ativos = filtros_ativos(request, filtros, {
        "q": ("Busca", None),
        "fabricante": ("Fabricante", lambda v: rotulo_de_id(opcoes["fabricantes"], v)),
        "categoria": ("Categoria", lambda v: rotulo_de_id(opcoes["categorias"], v)),
        "grupo": ("Grupo", lambda v: dict(opcoes["grupos"]).get(v, v)),
        "local": ("Local", lambda v: rotulo_de_id(opcoes["locais"], v)),
        "abaixo_minimo": ("Estoque", lambda v: "abaixo do mínimo"),
    })
    total_geral = TecModelo.objects.filter(controle=TecControle.QUANTIDADE).count()
    abaixo = estoque.listar_materiais({**filtros, "abaixo_minimo": "1"}).count()
    segmentos = segmentos_de(request, "abaixo_minimo", [("", "Todos", estoque.listar_materiais({**filtros, "abaixo_minimo": ""}).count()), ("1", "Abaixo do mínimo", abaixo)])
    return render(request, "front/agro/tec/materiais_list.html", contexto_base(
        request, "materiais", materiais=page_obj.object_list, page_obj=page_obj, paginas=paginas, querystring=querystring,
        filtros=filtros, filtros_ativos=ativos, opcoes=opcoes, segmentos=segmentos, total_geral=total_geral,
    ))


@exige_tec("ver_tec_estoque")
def tec_material_detalhe(request, pk):
    modelo = get_object_or_404(TecModelo.objects.select_related("fabricante", "categoria", "unidade"), pk=pk, controle=TecControle.QUANTIDADE)
    saldos = list(estoque.saldos_do_modelo(modelo))
    contexto = contexto_base(
        request, "materiais", modelo=modelo, saldos=saldos, total=estoque.total_disponivel(modelo),
        aplicacoes=instalacao_service.aplicacoes_do_material(modelo), pode_ver_frota=request.user.has_perm("ProjetoEstoque.ver_frota"),
        abaixo_do_minimo=modelo.estoque_minimo > 0 and estoque.total_disponivel(modelo) < modelo.estoque_minimo,
    )
    if contexto["pode_historico"]:
        contexto["movimentacoes"] = list(estoque.historico_movimentacoes({"modelo": modelo.pk})[:15])
    return render(request, "front/agro/tec/material_detalhe.html", contexto)


def _material_ou_404(pk) -> TecModelo:
    return get_object_or_404(TecModelo.objects.select_related("fabricante", "categoria", "unidade"), pk=pk, controle=TecControle.QUANTIDADE)


def _destino_do_material(modelo) -> str:
    return reverse("tec_material_detalhe", args=[modelo.pk])


@exige_tec("cadastrar_tec_equipamento")
def tec_material_entrada(request, pk=None):
    fixo = _material_ou_404(pk) if pk else None

    def executar(dados, token):
        modelo = fixo or dados.get("modelo")
        resultado = estoque.registrar_entrada_material(
            modelo, dados.get("local"), dados.get("quantidade"), request.user, fornecedor=dados.get("fornecedor") or None,
            documento=dados.get("documento", ""), motivo=dados.get("motivo", ""), observacoes=dados.get("observacoes", ""), token=token,
        )
        mov = resultado.movimentacoes[0]
        return f"Entrada de {mov.quantidade.normalize():f} {mov.modelo.unidade.sigla} de {mov.modelo} registrada.", _destino_do_material(mov.modelo), resultado.repetida

    campos = [
        campo("local", "Local de armazenamento", "select", obrigatorio=True, opcoes=_locais_das_fazendas(), vazio="Escolher o local"),
        campo("quantidade", "Quantidade", "number", obrigatorio=True, mono=True, placeholder="0", dica=f"Em {fixo.unidade.nome.lower()}s." if fixo else ""),
        campo("fornecedor", "Fornecedor", "select", opcoes=_fornecedores(), vazio="Sem fornecedor"),
        campo("documento", "Documento", maximo=60, placeholder="Ex.: NF 1234"),
        campo("motivo", "Motivo", maximo=255, largo=True, dica="Opcional na entrada."),
        campo("observacoes", "Observações", "textarea", largo=True),
    ]
    if fixo is None:
        for posicao, c in enumerate(_campos_de_modelo(TecControle.QUANTIDADE, "Material", vazio="Escolher o material",
                                                      dica="Falta o material? Cadastre o modelo no catálogo, com controle por quantidade.")):
            campos.insert(posicao, c)
    voltar = _destino_do_material(fixo) if fixo else reverse("tec_materiais")
    return formulario(
        request, aba="materiais", titulo="Entrada de material", descricao=(f"{fixo} · soma ao saldo do local escolhido." if fixo else "Soma ao saldo do local escolhido. Quantidade inicial também é uma entrada."),
        voltar_url=voltar, voltar_rotulo=str(fixo) if fixo else "Materiais", campos=campos, executar=executar, rotulo_enviar="Registrar entrada",
    )


@exige_tec("cadastrar_tec_equipamento")
def tec_material_saida(request, pk):
    modelo = _material_ou_404(pk)

    def executar(dados, token):
        resultado = estoque.registrar_saida_material(modelo, dados.get("local"), dados.get("quantidade"), request.user, dados.get("motivo"), destino=dados.get("destino", ""), observacoes=dados.get("observacoes", ""), token=token)
        mov = resultado.movimentacoes[0]
        return f"Saída de {mov.quantidade.normalize():f} {modelo.unidade.sigla} de {modelo} registrada.", _destino_do_material(modelo), resultado.repetida

    return formulario(
        request, aba="materiais", titulo="Saída de material", descricao=f"{modelo} · tira do saldo do local. Sem saldo suficiente, a saída é recusada.",
        voltar_url=_destino_do_material(modelo), voltar_rotulo=str(modelo), rotulo_enviar="Registrar saída", executar=executar,
        campos=[
            campo("local", "Sai de qual local", "select", obrigatorio=True, opcoes=_locais_com_saldo(modelo, so_com_saldo=True), vazio="Escolher o local"),
            campo("quantidade", "Quantidade", "number", obrigatorio=True, mono=True, placeholder="0"),
            campo("destino", "Para onde foi", maximo=120, placeholder="Ex.: Consumo na oficina", dica="Opcional. Instalar numa máquina ou torre será uma operação própria."),
            campo("motivo", "Motivo", obrigatorio=True, maximo=255, largo=True),
            campo("observacoes", "Observações", "textarea", largo=True),
        ],
    )


@exige_tec("cadastrar_tec_equipamento")
def tec_material_ajuste(request, pk):
    modelo = _material_ou_404(pk)

    def executar(dados, token):
        resultado = estoque.ajustar_saldo(modelo, dados.get("local"), dados.get("quantidade"), request.user, dados.get("motivo"), observacoes=dados.get("observacoes", ""), token=token)
        return f"Saldo de {modelo} acertado para {resultado.saldo.quantidade.normalize():f} {modelo.unidade.sigla}.", _destino_do_material(modelo), resultado.repetida

    return formulario(
        request, aba="materiais", titulo="Ajustar saldo", descricao=f"{modelo} · informe a quantidade CONTADA. A diferença vira um ajuste no histórico; o saldo nunca é editado direto.",
        voltar_url=_destino_do_material(modelo), voltar_rotulo=str(modelo), rotulo_enviar="Registrar ajuste", executar=executar,
        campos=[
            campo("local", "Local", "select", obrigatorio=True, opcoes=_locais_com_saldo(modelo, so_com_saldo=False), vazio="Escolher o local"),
            campo("quantidade", "Quantidade contada", "number", obrigatorio=True, mono=True, placeholder="0"),
            campo("motivo", "Motivo", obrigatorio=True, maximo=255, largo=True, placeholder="Ex.: contagem do inventário"),
            campo("observacoes", "Observações", "textarea", largo=True),
        ],
    )


@exige_tec("transferir_tec_equipamento")
def tec_material_transferir(request, pk):
    modelo = _material_ou_404(pk)

    def executar(dados, token):
        resultado = estoque.transferir_material_entre_locais(modelo, dados.get("local_origem"), dados.get("local_destino"), dados.get("quantidade"), request.user, dados.get("motivo"), observacoes=dados.get("observacoes", ""), token=token)
        mov = resultado.movimentacoes[0]
        return f"{mov.quantidade.normalize():f} {modelo.unidade.sigla} de {modelo} levados para {mov.destino_rotulo}.", _destino_do_material(modelo), resultado.repetida

    return formulario(
        request, aba="materiais", titulo="Transferir entre locais", descricao=f"{modelo} · tira de um local de armazenamento e põe em outro, numa operação só.",
        voltar_url=_destino_do_material(modelo), voltar_rotulo=str(modelo), rotulo_enviar="Transferir", executar=executar,
        campos=[
            campo("local_origem", "De qual local", "select", obrigatorio=True, opcoes=_locais_com_saldo(modelo, so_com_saldo=True), vazio="Escolher a origem"),
            campo("local_destino", "Para qual local", "select", obrigatorio=True, opcoes=_locais_das_fazendas(), vazio="Escolher o destino"),
            campo("quantidade", "Quantidade", "number", obrigatorio=True, mono=True, placeholder="0"),
            campo("motivo", "Motivo", obrigatorio=True, maximo=255, largo=True),
            campo("observacoes", "Observações", "textarea", largo=True),
        ],
    )


# ═════════════════════════════════════════════════════════════
# Razão de movimentações
# ═════════════════════════════════════════════════════════════

@exige_tec("ver_tec_historico")
def tec_movimentacoes(request):
    filtros = filtros_do_get(request, FILTROS_DO_RAZAO)
    consulta = estoque.historico_movimentacoes(filtros)
    page_obj, paginas, querystring = paginar(request, consulta)
    opcoes = {
        "tipos": list(TecMovimentacao.Tipo.choices),
        "fabricantes": list(TecFabricante.objects.filter(modelos__movimentacoes__isnull=False).distinct().order_by("nome").values_list("pk", "nome")),
    }
    ativos = filtros_ativos(request, filtros, {
        "q": ("Busca", None),
        "tipo": ("Tipo", lambda v: dict(opcoes["tipos"]).get(v, v)),
        "fabricante": ("Fabricante", lambda v: rotulo_de_id(opcoes["fabricantes"], v)),
        "usuario": ("Responsável", None),
        "de": ("De", None),
        "ate": ("Até", None),
    })
    return render(request, "front/agro/tec/movimentacoes.html", contexto_base(
        request, "movimentacoes", movimentacoes=page_obj.object_list, page_obj=page_obj, paginas=paginas, querystring=querystring,
        filtros=filtros, filtros_ativos=ativos, opcoes=opcoes, total_geral=TecMovimentacao.objects.count(),
    ))
