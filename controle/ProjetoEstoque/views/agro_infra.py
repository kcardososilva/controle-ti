"""
Infraestrutura do Zelo Agro: visão geral (dashboard), estruturas (torres, estações, bases, pontos de referência) e
enlaces ponto a ponto (PTP).

Acesso, pela régua do estoque tecnológico (`exige_tec`): ver = `ver_tec_estoque`; cadastrar e corrigir estrutura =
`gerenciar_tec_estrutura`; cadastrar e corrigir enlace = `administrar_tec_enlace` (as duas já existiam, reservadas
para esta área). A regra é do service (`services/agro_infra_service.py`); aqui só se valida a entrada, chama-se o
service e renderiza. Os formulários são listas de campos (`tec_comum.formulario`), com o topo desta área.
"""

from django.contrib import messages
from django.shortcuts import get_object_or_404, render
from django.urls import reverse

from ProjetoEstoque.models import TecEnlace, TecEquipamento, TecEstrutura
from ProjetoEstoque.views.tec_comum import campo, contexto_base, exige_tec, filtros_ativos, filtros_do_get, formulario, opcoes_de, paginar, pode
from services import agro_dash_service as dash
from services import agro_infra_service as infra
from services import frota_service

TOPO = "front/agro/infra/_topo.html"


def _contexto(request, aba: str, **extra) -> dict:
    """O que todas as telas da infraestrutura recebem: a aba, o que a pessoa pode fazer e o topo."""
    return contexto_base(
        request, "infra", infra_aba=aba, topo_template=TOPO,
        pode_estrutura=pode(request, "gerenciar_tec_estrutura"), pode_enlace=pode(request, "administrar_tec_enlace"),
        pode_mapa=request.user.has_perm("ProjetoEstoque.ver_frota") or pode(request, "ver_tec_estoque"),
        **extra,
    )


# ═════════════════════════════════════════════════════════════
# Visão geral
# ═════════════════════════════════════════════════════════════

@exige_tec("ver_tec_estoque")
def infra_visao_geral(request):
    """O dashboard da infraestrutura: totais, distribuição dos enlaces pelo estado REGISTRADO, o que precisa de atenção
    (enlace sem estado informado, em manutenção ou indisponível; estrutura sem coordenada) e o que mudou por último."""
    ind = infra.indicadores_infra()
    lista_enlaces = reverse("infra_enlaces")
    estados = [
        (rotulo, ind["enlaces_por_estado"].get(valor, 0), f"{lista_enlaces}?estado={valor}")
        for valor, rotulo in TecEnlace.Estado.choices
    ]
    estrutura_por_tipo = [
        (rotulo, TecEstrutura.objects.filter(ativo=True, tipo=valor).count(), f"{reverse('infra_estruturas')}?tipo={valor}")
        for valor, rotulo in TecEstrutura.Tipo.choices
    ]
    atencao_enlaces = list(
        infra.listar_enlaces({"ativo": "1"}).exclude(estado__in=[TecEnlace.Estado.OPERACIONAL, TecEnlace.Estado.PLANEJADO])[:8]
    )
    sem_coordenada = list(infra.listar_estruturas({"ativo": "1", "coordenada": "sem"})[:8])
    recentes = list(
        TecEnlace.objects.select_related("estrutura_a", "estrutura_b", "estado_por").filter(estado_em__isnull=False)
        .order_by("-estado_em")[:6]
    )
    return render(request, "front/agro/infra/visao_geral.html", _contexto(
        request, "visao", ind=ind, vazio=not (ind["enlaces"] or sum(v for _, v, _ in estrutura_por_tipo)),
        estados=dash.barras(estados), tipos=dash.barras(estrutura_por_tipo),
        atencao_enlaces=atencao_enlaces, sem_coordenada=sem_coordenada, recentes=recentes,
    ))


# ═════════════════════════════════════════════════════════════
# Estruturas
# ═════════════════════════════════════════════════════════════

FILTROS_DE_ESTRUTURA = ("q", "tipo", "situacao", "fazenda", "coordenada", "ativo")
ROTULOS_DE_FILTRO_ESTRUTURA = {
    "q": ("Busca", None), "tipo": ("Tipo", lambda v: dict(TecEstrutura.Tipo.choices).get(v, v)),
    "situacao": ("Situação", lambda v: dict(TecEstrutura.Situacao.choices).get(v, v)),
    "coordenada": ("Localização", lambda v: {"com": "com coordenada", "sem": "sem coordenada"}.get(v, v)),
    "ativo": ("Cadastro", lambda v: {"1": "ativas", "0": "inativas"}.get(v, v)),
}


@exige_tec("ver_tec_estoque")
def infra_estruturas(request):
    filtros = filtros_do_get(request, FILTROS_DE_ESTRUTURA)
    consulta = infra.listar_estruturas(filtros)
    page_obj, paginas, querystring = paginar(request, consulta)
    fazendas = [(f.pk, f.local) for f in frota_service.fazendas()]
    rotulos = {**ROTULOS_DE_FILTRO_ESTRUTURA, "fazenda": ("Fazenda", lambda v: dict((str(k), n) for k, n in fazendas).get(v, v))}
    return render(request, "front/agro/infra/estruturas.html", _contexto(
        request, "estruturas", estruturas=list(page_obj.object_list), page_obj=page_obj, paginas=paginas, querystring=querystring,
        filtros=filtros, filtros_ativos=filtros_ativos(request, filtros, rotulos), total_geral=TecEstrutura.objects.count(),
        opcoes={"tipos": list(TecEstrutura.Tipo.choices), "situacoes": list(TecEstrutura.Situacao.choices), "fazendas": fazendas},
    ))


def _campos_da_estrutura(e: TecEstrutura | None = None) -> list:
    campos = [
        campo("nome", "Nome", obrigatorio=True, maximo=120, valor=getattr(e, "nome", ""), dica="Único. Ex.: Torre Karitel Norte."),
        campo("tipo", "Tipo", "select", obrigatorio=True, opcoes=list(TecEstrutura.Tipo.choices), valor=getattr(e, "tipo", "torre")),
        campo("situacao", "Situação", "select", obrigatorio=True, opcoes=list(TecEstrutura.Situacao.choices), valor=getattr(e, "situacao", "em_operacao")),
        campo("fazenda", "Unidade / fazenda", "select", opcoes=[(f.pk, f.local) for f in frota_service.fazendas()], vazio="Sem fazenda", valor=getattr(e, "fazenda_id", "")),
        campo("latitude", "Latitude", mono=True, maximo=20, valor=getattr(e, "latitude", "") if e and e.tem_coordenada else "", placeholder="-14,776300",
              dica="Graus decimais. Cadastre a coordenada que você mediu ou conferiu: o sistema não a deduz de nada."),
        campo("longitude", "Longitude", mono=True, maximo=20, valor=getattr(e, "longitude", "") if e and e.tem_coordenada else "", placeholder="-45,563400"),
        campo("altura_m", "Altura (m)", "number", mono=True, valor=getattr(e, "altura_m", "") if e and e.altura_m is not None else "", placeholder="0", dica="Opcional."),
        campo("observacoes", "Observações", "textarea", valor=getattr(e, "observacoes", ""), largo=True),
    ]
    if e is not None:
        campos.append(campo("ativo", "Cadastro ativo", "checkbox", valor=e.ativo, dica="Desmarque para tirar a estrutura das listas e do mapa sem apagar o histórico."))
    return campos


SECOES_DA_ESTRUTURA = [
    ("Identificação", ["nome", "tipo", "situacao", "fazenda"]),
    ("Localização", ["latitude", "longitude", "altura_m"]),
    ("Observações", ["observacoes", "ativo"]),
]


@exige_tec("gerenciar_tec_estrutura")
def infra_estrutura_nova(request):
    def executar(dados, token):
        estrutura = infra.salvar_estrutura(dados, request.user)
        return f"{estrutura.nome} cadastrada.", reverse("infra_estruturas")

    return formulario(
        request, aba="infra", titulo="Nova estrutura", descricao="Torre, estação, base de comunicação ou ponto de referência.",
        voltar_url=reverse("infra_estruturas"), voltar_rotulo="Estruturas", campos=_campos_da_estrutura(), executar=executar,
        rotulo_enviar="Cadastrar estrutura", secoes=SECOES_DA_ESTRUTURA, tem_token=False, infra_aba="estruturas", topo_template=TOPO,
        pode_estrutura=True, pode_enlace=pode(request, "administrar_tec_enlace"), pode_mapa=True,
    )


@exige_tec("gerenciar_tec_estrutura")
def infra_estrutura_editar(request, pk):
    estrutura = get_object_or_404(TecEstrutura.objects.select_related("fazenda"), pk=pk)

    def executar(dados, token):
        infra.salvar_estrutura(dados, request.user, estrutura)
        return f"{estrutura.nome} salva.", reverse("infra_estruturas")

    return formulario(
        request, aba="infra", titulo=f"Editar {estrutura.nome}", descricao="Corrige o cadastro. Trocar a coordenada atualiza a data da localização.",
        voltar_url=reverse("infra_estruturas"), voltar_rotulo="Estruturas", campos=_campos_da_estrutura(estrutura), executar=executar,
        secoes=SECOES_DA_ESTRUTURA, tem_token=False, infra_aba="estruturas", topo_template=TOPO,
        pode_estrutura=True, pode_enlace=pode(request, "administrar_tec_enlace"), pode_mapa=True,
    )


# ═════════════════════════════════════════════════════════════
# Enlaces PTP
# ═════════════════════════════════════════════════════════════

FILTROS_DE_ENLACE = ("q", "estado", "estrutura", "fazenda", "ativo")


@exige_tec("ver_tec_estoque")
def infra_enlaces(request):
    filtros = filtros_do_get(request, FILTROS_DE_ENLACE)
    consulta = infra.listar_enlaces(filtros)
    page_obj, paginas, querystring = paginar(request, consulta)
    enlaces = list(page_obj.object_list)
    for e in enlaces:
        e.distancia_km = infra.distancia_do_enlace_km(e)
    fazendas = [(f.pk, f.local) for f in frota_service.fazendas()]
    estruturas = [(s.pk, s.nome) for s in TecEstrutura.objects.filter(ativo=True).order_by("nome")]
    rotulos = {
        "q": ("Busca", None), "estado": ("Estado", lambda v: dict(TecEnlace.Estado.choices).get(v, v)),
        "estrutura": ("Ponta", lambda v: dict((str(k), n) for k, n in estruturas).get(v, v)),
        "fazenda": ("Fazenda", lambda v: dict((str(k), n) for k, n in fazendas).get(v, v)),
        "ativo": ("Cadastro", lambda v: {"1": "ativos", "0": "inativos"}.get(v, v)),
    }
    return render(request, "front/agro/infra/enlaces.html", _contexto(
        request, "enlaces", enlaces=enlaces, page_obj=page_obj, paginas=paginas, querystring=querystring, filtros=filtros,
        filtros_ativos=filtros_ativos(request, filtros, rotulos), total_geral=TecEnlace.objects.count(),
        opcoes={"estados": list(TecEnlace.Estado.choices), "estruturas": estruturas, "fazendas": fazendas},
    ))


def _opcoes_de_radio(atual=None) -> list:
    """Os rádios que dá para escolher numa ponta: equipamentos de REDE que não foram baixados (um já escolhido
    continua na lista mesmo baixado, para não sumir da edição)."""
    qs = TecEquipamento.objects.exclude(situacao=TecEquipamento.Situacao.INATIVO).filter(modelo__categoria__grupo="rede")
    return opcoes_de(qs.select_related("modelo__fabricante", "modelo__categoria").order_by("codigo")[:1000], lambda e: f"{e.codigo} · {e.modelo}", atual=atual)


def _campos_do_enlace(e: TecEnlace | None = None) -> list:
    campos = [
        campo("identificacao", "Identificação do enlace", obrigatorio=True, maximo=80, valor=getattr(e, "identificacao", ""), dica="Único. Ex.: PTP Karitel – Rio do Meio."),
        campo("estrutura_a", "Ponta A", "select", obrigatorio=True, opcoes=opcoes_de(TecEstrutura.objects.filter(ativo=True).order_by("nome"), atual=getattr(e, "estrutura_a", None)),
              vazio="Escolher a estrutura", valor=getattr(e, "estrutura_a_id", "")),
        campo("estrutura_b", "Ponta B", "select", obrigatorio=True, opcoes=opcoes_de(TecEstrutura.objects.filter(ativo=True).order_by("nome"), atual=getattr(e, "estrutura_b", None)),
              vazio="Escolher a estrutura", valor=getattr(e, "estrutura_b_id", "")),
        campo("equipamento_a", "Rádio da ponta A", "select", opcoes=_opcoes_de_radio(getattr(e, "equipamento_a", None)), vazio="Sem rádio informado",
              valor=getattr(e, "equipamento_a_id", ""), dica="Só referência: quem instala e retira o rádio é o estoque tecnológico."),
        campo("equipamento_b", "Rádio da ponta B", "select", opcoes=_opcoes_de_radio(getattr(e, "equipamento_b", None)), vazio="Sem rádio informado", valor=getattr(e, "equipamento_b_id", "")),
        campo("estado", "Estado do enlace", "select", obrigatorio=True, opcoes=list(TecEnlace.Estado.choices), valor=getattr(e, "estado", "nao_informado"),
              dica="O que alguém registrou. Rádio cadastrado nas pontas não faz o enlace operacional. A mudança fica com data e autor."),
        campo("frequencia_ghz", "Frequência (GHz)", "number", mono=True, valor=getattr(e, "frequencia_ghz", "") if e and e.frequencia_ghz is not None else "", placeholder="5,8", dica="Opcional."),
        campo("observacoes", "Observações", "textarea", valor=getattr(e, "observacoes", ""), largo=True),
    ]
    if e is not None:
        campos.append(campo("ativo", "Cadastro ativo", "checkbox", valor=e.ativo))
    return campos


SECOES_DO_ENLACE = [
    ("Identificação", ["identificacao", "estado", "frequencia_ghz"]),
    ("Pontas", ["estrutura_a", "equipamento_a", "estrutura_b", "equipamento_b"]),
    ("Observações", ["observacoes", "ativo"]),
]


@exige_tec("administrar_tec_enlace")
def infra_enlace_novo(request):
    def executar(dados, token):
        enlace = infra.salvar_enlace(dados, request.user)
        return f"Enlace {enlace.identificacao} cadastrado.", reverse("infra_enlaces")

    return formulario(
        request, aba="infra", titulo="Novo enlace PTP", descricao="Registra o enlace entre duas estruturas. O mapa o desenha como uma linha entre as coordenadas das pontas.",
        voltar_url=reverse("infra_enlaces"), voltar_rotulo="Enlaces PTP", campos=_campos_do_enlace(), executar=executar,
        rotulo_enviar="Cadastrar enlace", secoes=SECOES_DO_ENLACE, tem_token=False, infra_aba="enlaces", topo_template=TOPO,
        pode_estrutura=pode(request, "gerenciar_tec_estrutura"), pode_enlace=True, pode_mapa=True,
    )


@exige_tec("administrar_tec_enlace")
def infra_enlace_editar(request, pk):
    enlace = get_object_or_404(TecEnlace.objects.select_related("estrutura_a", "estrutura_b", "equipamento_a", "equipamento_b"), pk=pk)

    def executar(dados, token):
        infra.salvar_enlace(dados, request.user, enlace)
        return f"Enlace {enlace.identificacao} salvo.", reverse("infra_enlaces")

    return formulario(
        request, aba="infra", titulo=f"Editar {enlace.identificacao}", descricao="Corrige o cadastro. Mudar o estado registra quem mudou e quando.",
        voltar_url=reverse("infra_enlaces"), voltar_rotulo="Enlaces PTP", campos=_campos_do_enlace(enlace), executar=executar,
        secoes=SECOES_DO_ENLACE, tem_token=False, infra_aba="enlaces", topo_template=TOPO,
        pode_estrutura=pode(request, "gerenciar_tec_estrutura"), pode_enlace=True, pode_mapa=True,
    )
