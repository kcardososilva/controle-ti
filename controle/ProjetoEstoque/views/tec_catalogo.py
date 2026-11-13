"""
Telas do CATÁLOGO da tecnologia agrícola: modelos, fabricantes, categorias, unidades de medida, locais
de armazenamento e fornecedores. É o cadastro de apoio do estoque (`views/tec_estoque.py`).

As seis listas e os seis formulários seguem o mesmo molde, então o que muda de um para o outro é uma
LINHA da tabela `CATALOGOS` (quem lista, quem salva, que colunas, que campos, que permissão) e não
uma view por cadastro. Ver as listas exige `ver_tec_estoque`; criar e editar exige a permissão do
cadastro (`administrar_tec_catalogo` para modelos, fabricantes, categorias e fornecedores;
`administrar_tec_config` para unidades e locais) — conferida AQUI, na view, e não só escondida no botão.

Não há exclusão: cadastro de apoio é referenciado por equipamento, saldo e histórico, então ele se
INATIVA (some das listas de escolha, continua nos registros que já o usam).
"""

from dataclasses import dataclass
from typing import Callable

from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.urls import reverse

from ProjetoEstoque.models import (
    AgroFornecedor,
    AgroLocal,
    AgroUnidadeMedida,
    TecCategoria,
    TecControle,
    TecFabricante,
    TecModelo,
)
from ProjetoEstoque.views.tec_comum import campo, contexto_base, exige_tec, formulario, opcoes_de, paginar, pode, segmentos_de
from services import frota_service
from services import tec_catalogo_service as catalogo


@dataclass
class Catalogo:
    chave: str
    rotulo: str                     # plural, para a aba: "Modelos"
    singular: str                   # "modelo"
    permissao: str                  # quem cria e edita
    colunas: tuple                  # rótulos das colunas (a primeira é o nome do registro)
    listar: Callable                # (busca) -> lista de objetos
    linha: Callable                 # objeto -> lista de células (texto)
    campos: Callable                # (instancia|None) -> lista de `campo(...)`
    salvar: Callable                # (dados, usuario, instancia|None) -> objeto
    obter: Callable                 # pk -> instância (ou 404)
    nome: Callable = str            # objeto -> como o chamamos nas mensagens
    descricao: str = ""


def _num(valor) -> str:
    """Decimal sem zeros à direita e sem notação científica: 100.000 -> "100", 2.50 -> "2.5"."""
    return f"{valor.normalize():f}"


def _sim_nao(valor) -> str:
    return "Sim" if valor else "Não"


def _por_nome(modelo, busca: str):
    qs = modelo.objects.order_by("nome")
    return qs.filter(nome__icontains=busca) if busca else qs


def _grupos_de_categorias(atual=None) -> list:
    categorias = list(TecCategoria.objects.filter(ativo=True).order_by("nome"))
    if atual is not None and atual.pk not in {c.pk for c in categorias}:
        categorias.append(atual)
    rotulos = dict(TecCategoria.Grupo.choices)
    grupos: dict = {}
    for c in sorted(categorias, key=lambda c: (rotulos.get(c.grupo, ""), c.nome)):
        grupos.setdefault(rotulos.get(c.grupo, c.grupo), []).append((c.pk, c.nome))
    return list(grupos.items())


def _ativo(instancia):
    return [campo("ativo", "Ativo", "checkbox", valor=instancia.ativo, dica="Inativo some das listas de escolha, mas continua nos registros que já o usam.")] if instancia else []


# ── um bloco por cadastro ──────────────────────────────────────────────────────

def _campos_modelo(m):
    return [
        campo("fabricante", "Fabricante", "select", obrigatorio=True, opcoes=opcoes_de(TecFabricante.objects.filter(ativo=True).order_by("nome"), atual=getattr(m, "fabricante", None)),
              vazio="Escolher o fabricante", valor=getattr(m, "fabricante_id", "")),
        campo("categoria", "Categoria", "select", obrigatorio=True, grupos=_grupos_de_categorias(getattr(m, "categoria", None)), vazio="Escolher a categoria", valor=getattr(m, "categoria_id", "")),
        campo("nome", "Nome do modelo", obrigatorio=True, maximo=120, valor=getattr(m, "nome", ""), largo=True, dica="Como o fabricante chama (e como aparece na etiqueta)."),
        campo("controle", "Tipo de controle", "select", opcoes=list(TecControle.choices), valor=getattr(m, "controle", ""), vazio="" if m else "Pelo padrão da categoria",
              dica="Individual: cada peça tem número de série. Por quantidade: só o saldo importa (cabo, conector, fonte)."),
        campo("unidade", "Unidade de medida", "select", opcoes=opcoes_de(AgroUnidadeMedida.objects.filter(ativo=True), lambda u: f"{u.sigla} — {u.nome}", atual=getattr(m, "unidade", None)),
              vazio="Sem unidade", valor=getattr(m, "unidade_id", None) or "", dica="Só para material por quantidade."),
        campo("estoque_minimo", "Estoque mínimo", "number", mono=True, valor=(_num(m.estoque_minimo) if m else "0"), dica="Abaixo disto o modelo aparece como falta. 0 desliga o alerta."),
        campo("observacoes", "Observações técnicas", "textarea", valor=getattr(m, "observacoes", ""), largo=True),
        *_ativo(m),
    ]


def _campos_fabricante(f):
    return [campo("nome", "Nome", obrigatorio=True, maximo=80, valor=getattr(f, "nome", ""), largo=True), *_ativo(f)]


def _campos_categoria(c):
    return [
        campo("nome", "Nome", obrigatorio=True, maximo=80, valor=getattr(c, "nome", ""), largo=True),
        campo("grupo", "Grupo", "select", obrigatorio=True, opcoes=list(TecCategoria.Grupo.choices), valor=getattr(c, "grupo", "outro"),
              dica="O grupo decide em que área do módulo a categoria aparece (radiocomunicação, rede, energia…)."),
        campo("controle_padrao", "Controle sugerido", "select", obrigatorio=True, opcoes=list(TecControle.choices), valor=getattr(c, "controle_padrao", "individual"),
              dica="Só preenche o modelo novo; quem decide é o modelo."),
        *_ativo(c),
    ]


def _campos_unidade(u):
    return [
        campo("sigla", "Sigla", obrigatorio=True, maximo=8, mono=True, valor=getattr(u, "sigla", "")),
        campo("nome", "Nome", obrigatorio=True, maximo=40, valor=getattr(u, "nome", "")),
        campo("fracionavel", "Aceita casas decimais", "checkbox", valor=getattr(u, "fracionavel", False), dica="Metro aceita 2,5; unidade e caixa não."),
        *_ativo(u),
    ]


def _campos_local(l):
    return [
        campo("fazenda", "Unidade / fazenda", "select", obrigatorio=True, opcoes=[(f.pk, f.local) for f in frota_service.fazendas()], vazio="Escolher a fazenda", valor=getattr(l, "fazenda_id", "")),
        campo("nome", "Nome do local", obrigatorio=True, maximo=100, valor=getattr(l, "nome", ""), placeholder="Ex.: Almoxarifado, Oficina, Container 2"),
        campo("observacoes", "Observações", maximo=255, valor=getattr(l, "observacoes", ""), largo=True),
        *_ativo(l),
    ]


def _campos_fornecedor(f):
    return [
        campo("nome", "Nome", obrigatorio=True, maximo=120, valor=getattr(f, "nome", ""), largo=True),
        campo("documento", "CNPJ ou CPF", maximo=18, mono=True, valor=getattr(f, "documento", ""), dica="Opcional. Conferido pelos dígitos verificadores; único."),
        campo("contato", "Contato", maximo=120, valor=getattr(f, "contato", "")),
        campo("telefone", "Telefone", maximo=30, valor=getattr(f, "telefone", "")),
        campo("email", "E-mail", maximo=254, valor=getattr(f, "email", "")),
        campo("observacoes", "Observações", "textarea", valor=getattr(f, "observacoes", ""), largo=True),
        *_ativo(f),
    ]


def _documento(valor: str) -> str:
    digitos = "".join(ch for ch in valor if ch.isdigit())
    if len(digitos) == 14:
        return f"{digitos[:2]}.{digitos[2:5]}.{digitos[5:8]}/{digitos[8:12]}-{digitos[12:]}"
    if len(digitos) == 11:
        return f"{digitos[:3]}.{digitos[3:6]}.{digitos[6:9]}-{digitos[9:]}"
    return valor


CATALOGOS = {c.chave: c for c in (
    Catalogo(
        "modelos", "Modelos", "modelo", "administrar_tec_catalogo", ("Modelo", "Fabricante", "Categoria", "Controle", "Mínimo", "Equipamentos"),
        lambda busca: list(catalogo.listar_modelos({"q": busca})),
        lambda m: [m.nome, m.fabricante.nome, m.categoria.nome, m.get_controle_display().split(" (")[0], (_num(m.estoque_minimo) + (f" {m.unidade.sigla}" if m.unidade_id else "")) if m.estoque_minimo > 0 else "—", m.n_equipamentos or "—"],
        _campos_modelo, catalogo.salvar_modelo, lambda pk: get_object_or_404(TecModelo.objects.select_related("fabricante", "categoria", "unidade"), pk=pk),
        descricao="O que a empresa realmente usa: fabricante, categoria e nome, e se cada peça tem número de série ou só importa o saldo.",
    ),
    Catalogo(
        "fabricantes", "Fabricantes", "fabricante", "administrar_tec_catalogo", ("Fabricante", "Modelos", "Equipamentos"),
        lambda busca: list(catalogo.listar_fabricantes().filter(nome__icontains=busca) if busca else catalogo.listar_fabricantes()),
        lambda f: [f.nome, f.n_modelos or "—", f.n_equipamentos or "—"],
        _campos_fabricante, catalogo.salvar_fabricante, lambda pk: get_object_or_404(TecFabricante, pk=pk),
        descricao="Solinftec, John Deere, Stara, Motorola e os demais que a operação usar.",
    ),
    Catalogo(
        "categorias", "Categorias", "categoria", "administrar_tec_catalogo", ("Categoria", "Grupo", "Controle sugerido", "Modelos"),
        lambda busca: list(catalogo.listar_categorias().filter(nome__icontains=busca) if busca else catalogo.listar_categorias()),
        lambda c: [c.nome, c.get_grupo_display(), c.get_controle_padrao_display().split(" (")[0], c.n_modelos or "—"],
        _campos_categoria, catalogo.salvar_categoria, lambda pk: get_object_or_404(TecCategoria, pk=pk),
        descricao="Agrupam o que se instala (monitor, receptor, rádio, antena, cabo…). O grupo decide em que área do módulo ela aparece.",
    ),
    Catalogo(
        "unidades", "Unidades de medida", "unidade de medida", "administrar_tec_config", ("Sigla", "Nome", "Casas decimais", "Modelos"),
        lambda busca: list(catalogo.listar_unidades().filter(sigla__icontains=busca) if busca else catalogo.listar_unidades()),
        lambda u: [u.sigla, u.nome, _sim_nao(u.fracionavel), u.n_modelos or "—"],
        _campos_unidade, catalogo.salvar_unidade, lambda pk: get_object_or_404(AgroUnidadeMedida, pk=pk), nome=lambda u: u.sigla,
        descricao="Como o material por quantidade é contado (un, m, cx, rolo…).",
    ),
    Catalogo(
        "locais", "Locais de armazenamento", "local de armazenamento", "administrar_tec_config", ("Local", "Fazenda", "Equipamentos", "Observações"),
        lambda busca: list(catalogo.listar_locais().filter(nome__icontains=busca) if busca else catalogo.listar_locais()),
        lambda l: [l.nome, l.fazenda.local, l.n_equipamentos or "—", l.observacoes or "—"],
        _campos_local, catalogo.salvar_local, lambda pk: get_object_or_404(AgroLocal.objects.select_related("fazenda"), pk=pk),
        descricao="Onde o material fica guardado quando não está instalado: almoxarifado, oficina, container… sempre numa das duas fazendas.",
    ),
    Catalogo(
        "fornecedores", "Fornecedores", "fornecedor", "administrar_tec_catalogo", ("Fornecedor", "Documento", "Contato", "Telefone", "Equipamentos"),
        lambda busca: list(catalogo.listar_fornecedores(busca=busca)),
        lambda f: [f.nome, _documento(f.documento) or "—", f.contato or "—", f.telefone or "—", f.n_equipamentos or "—"],
        _campos_fornecedor, catalogo.salvar_fornecedor, lambda pk: get_object_or_404(AgroFornecedor, pk=pk),
        descricao="De quem vêm os equipamentos. Cadastro enxuto do Agro, separado dos fornecedores do TI.",
    ),
)}


def _catalogo_ou_404(chave: str) -> Catalogo:
    try:
        return CATALOGOS[chave]
    except KeyError:
        raise Http404("Catálogo inexistente.") from None


def _abas_do_catalogo(request) -> list:
    return [{"chave": c.chave, "rotulo": c.rotulo, "url": reverse("tec_catalogo", args=[c.chave])} for c in CATALOGOS.values()]


@exige_tec("ver_tec_estoque")
def tec_catalogo(request, tipo):
    cat = _catalogo_ou_404(tipo)
    busca = (request.GET.get("q") or "").strip()
    objetos = cat.listar(busca)
    ativos = [o for o in objetos if o.ativo]
    situacao = (request.GET.get("situacao") or "").strip()
    visiveis = [o for o in objetos if (situacao != "ativo" or o.ativo) and (situacao != "inativo" or not o.ativo)]
    segmentos = segmentos_de(request, "situacao", [("", "Todos", len(objetos)), ("ativo", "Ativos", len(ativos)), ("inativo", "Inativos", len(objetos) - len(ativos))])
    page_obj, paginas, querystring = paginar(request, visiveis)
    pode_editar = pode(request, cat.permissao)
    linhas = [{"pk": o.pk, "ativo": o.ativo, "celulas": [{"rotulo": coluna, "valor": valor} for coluna, valor in zip(cat.colunas, cat.linha(o))], "editar": reverse("tec_catalogo_editar", args=[cat.chave, o.pk]) if pode_editar else ""} for o in page_obj.object_list]
    return render(request, "front/agro/tec/catalogo.html", contexto_base(
        request, "catalogo", cat=cat, abas_do_catalogo=_abas_do_catalogo(request), linhas=linhas, page_obj=page_obj, paginas=paginas,
        querystring=querystring, busca=busca, segmentos=segmentos, total_geral=len(objetos), pode_editar_catalogo=pode_editar,
    ))


@exige_tec("administrar_tec_catalogo", "administrar_tec_config")
def tec_catalogo_novo(request, tipo):
    cat = _catalogo_ou_404(tipo)
    if not pode(request, cat.permissao):
        raise PermissionDenied

    def executar(dados, token):
        obj = cat.salvar(dados, request.user, None)
        return f"Cadastro de {cat.singular} “{cat.nome(obj)}” salvo.", reverse("tec_catalogo", args=[cat.chave])

    return formulario(
        request, aba="catalogo", titulo=f"Novo {cat.singular}" if cat.singular[-1] != "a" else f"Nova {cat.singular}", descricao=cat.descricao,
        voltar_url=reverse("tec_catalogo", args=[cat.chave]), voltar_rotulo=cat.rotulo, campos=cat.campos(None), executar=executar,
        tem_token=False, abas_do_catalogo=_abas_do_catalogo(request), cat=cat,
    )


@exige_tec("administrar_tec_catalogo", "administrar_tec_config")
def tec_catalogo_editar(request, tipo, pk):
    cat = _catalogo_ou_404(tipo)
    if not pode(request, cat.permissao):
        raise PermissionDenied
    instancia = cat.obter(pk)

    def executar(dados, token):
        obj = cat.salvar(dados, request.user, instancia)
        return f"Cadastro de {cat.singular} “{cat.nome(obj)}” salvo.", reverse("tec_catalogo", args=[cat.chave])

    return formulario(
        request, aba="catalogo", titulo=f"Editar {cat.singular}: {cat.nome(instancia)}", descricao=cat.descricao,
        voltar_url=reverse("tec_catalogo", args=[cat.chave]), voltar_rotulo=cat.rotulo, campos=cat.campos(instancia), executar=executar,
        tem_token=False, abas_do_catalogo=_abas_do_catalogo(request), cat=cat,
    )
