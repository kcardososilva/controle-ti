"""Filtros das telas da tecnologia agrícola do Zelo Agro."""

from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()


@register.filter
def qtd(valor):
    """Quantidade sem zeros à direita e com vírgula: 130.500 -> "130,5"; 40.000 -> "40".

    O `floatformat:"-3"` do Django corta o decimal só quando ele é todo zero, mas mantém "130,500";
    numa tela de estoque, "130,500 m" parece uma quantidade com milhar. Sem separador de milhar de
    propósito: o resto do módulo (`unlocalize`) também não usa."""
    if valor in (None, ""):
        return "—"
    try:
        numero = Decimal(str(valor))
    except (InvalidOperation, ValueError):
        return str(valor)
    texto = format(numero.normalize(), "f")
    return texto.replace(".", ",")
