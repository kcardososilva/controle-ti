"""Popups de cadastro rápido (subtipo e localidade) do formulário de item.

Uma tag de inclusão, para o formulário de cadastro e o de edição (que compartilham o template) ganharem os popups sem
que as duas views passem as categorias por contexto: quem esquecesse de passar numa delas teria um popup sem opções.
"""
from django import template

from ProjetoEstoque.models import Categoria, SimNaoChoices

register = template.Library()


@register.inclusion_tag("front/equipamentos/_cadastro_rapido.html")
def cadastro_rapido_dialogos():
    return {
        "categorias": Categoria.objects.order_by("nome"),
        "alocado_opcoes": SimNaoChoices.choices,
    }
