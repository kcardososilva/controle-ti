"""
As tags da biblioteca de ícones do Zelo (`services/icone_service.py`).

    {% zelo_icone "trator" %}                              um ícone pelo id
    {% zelo_icone_item item %}                             o ícone do subtipo de um Item do estoque de TI
    {% zelo_icone_veiculo veiculo %}                       o ícone de um veículo/máquina do Agro (classe, depois tipo)
    {% zelo_icone_tipo "Access-Point" ambiente="ti" %}     o ícone de um tipo pelo NOME
    {{ nome|zelo_icone_id:"agro" }}                        só o id (atributo `data-icone` de <option>, JSON…)

A tela nunca decide qual desenho usar: pergunta. O ícone é decorativo por padrão (`aria-hidden`), porque o nome do tipo
está escrito ao lado; passe `titulo=` quando ele for a única indicação.
"""

from django import template
from django.templatetags.static import static
from django.utils.html import format_html

from ProjetoEstoque.context_processors import ASSETS_VERSION
from services import icone_service as icones

register = template.Library()


def url_do_sprite() -> str:
    """A URL do sprite, com o número de versão dos estáticos (o projeto não usa manifesto de hash)."""
    return f"{static(icones.CAMINHO_ESTATICO_SPRITE)}?v={ASSETS_VERSION}"


def _desenho(ident: str, classe: str = "", titulo: str = ""):
    classes = f"zi {classe}".strip()
    formato = icones.formato_de(ident) if icones.existe(ident) else ""
    if not formato:
        # id que não existe ou sem arquivo: nada de ícone quebrado nem de erro na tela — o espaço some e o texto fica.
        return ""
    if formato == "png":
        return format_html(
            '<img class="{}" src="{}?v={}" alt="{}" data-icone="{}" loading="lazy" decoding="async">',
            classes, static(icones.caminho_estatico_png(ident)), ASSETS_VERSION, titulo, ident,
        )
    alvo = f"{url_do_sprite()}#{icones.simbolo(ident)}"
    if titulo:
        return format_html(
            '<svg class="{}" role="img" aria-label="{}" focusable="false" data-icone="{}"><title>{}</title><use href="{}"></use></svg>',
            classes, titulo, ident, titulo, alvo,
        )
    return format_html(
        '<svg class="{}" aria-hidden="true" focusable="false" data-icone="{}"><use href="{}"></use></svg>', classes, ident, alvo,
    )


@register.simple_tag
def zelo_sprite_url():
    """A URL do sprite, para o `data-sprite` do `zelo-icones.js` (o .js não passa pelo motor de template)."""
    return url_do_sprite()


@register.simple_tag
def zelo_icone(ident, classe="", titulo=""):
    return _desenho(ident, classe, titulo)


@register.simple_tag
def zelo_icone_tipo(nome, ambiente="ti", categoria=None, classe="", titulo=""):
    return _desenho(icones.icone_id(nome, ambiente=ambiente, categoria=categoria), classe, titulo)


@register.simple_tag
def zelo_icone_item(item, classe="", titulo=""):
    """O ícone de um `Item` do estoque de TI, pelo subtipo (e a categoria como segunda chance). Em lista, carregue
    `subtipo__categoria` com `select_related`: a tag lê esses dois nomes."""
    subtipo = getattr(item, "subtipo", None)
    if subtipo is None:
        return _desenho(icones.ICONE_PADRAO[icones.AMBIENTE_TI], classe, titulo)
    return _desenho(
        icones.icone_id(subtipo.nome, ambiente=icones.AMBIENTE_TI, categoria=lambda: getattr(subtipo.categoria, "nome", None)),
        classe, titulo,
    )


@register.simple_tag
def zelo_icone_subtipo(subtipo, classe="", titulo=""):
    """O ícone de um `Subtipo` do Zelo TI (cadastro de subtipos, filtros, selos)."""
    return _desenho(
        icones.icone_id(subtipo.nome, ambiente=icones.AMBIENTE_TI, categoria=lambda: getattr(subtipo.categoria, "nome", None)),
        classe, titulo,
    )


@register.simple_tag
def zelo_icone_veiculo(veiculo, classe="", titulo=""):
    """O ícone de um veículo/máquina: a classe operacional primeiro, depois o tipo. Em lista, carregue `tipo` e
    `classe_operacional` com `select_related`."""
    return _desenho(icones.icone_do_veiculo(getattr(veiculo, "classe_operacional", None), getattr(veiculo, "tipo", None)), classe, titulo)


@register.simple_tag
def zelo_icone_estrutura(tipo, classe="", titulo=""):
    """O ícone de uma estrutura georreferenciada (`TecEstrutura.tipo`: torre, estação, base, ponto de referência)."""
    return _desenho(icones.icone_da_estrutura(tipo), classe, titulo)


@register.filter
def zelo_icone_id(nome, ambiente="ti"):
    return icones.icone_id(nome, ambiente=ambiente)
