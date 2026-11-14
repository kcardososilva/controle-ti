"""
Busca por texto sem diferença de acento nem de maiúsculas — também no SQLite.

O problema: o `LIKE` do SQLite só ignora a caixa de letras ASCII. Procurar "caminhões" não acha
"CAMINHÕES TABACO" e "grãos" não acha "GRÃOS", justamente os textos que o ERP entrega — tudo em
caixa alta e acentuado. O `icontains` do Django herda a limitação e a falha é silenciosa: a lista
volta vazia, e quem procura conclui que o equipamento não existe.

A solução: `SemAcento(campo)` é uma expressão que o banco calcula (`ZELO_BUSCA(campo)`), e o
termo procurado passa por `normalizar_busca` do lado de cá — os dois chegam ao mesmo formato
(sem acento, em minúsculas, espaços colapsados) e a comparação é um `contains`/igualdade comum:

    qs.alias(nome_busca=SemAcento("nome")).filter(nome_busca__contains=normalizar_busca(termo))

A função é registrada em cada conexão SQLite (gancho `connection_created`, em `apps.py`, o mesmo
que liga o WAL). Em outro banco `SemAcento` cai para `LOWER(campo)`: perde o acento, nunca quebra.
"""

from __future__ import annotations

import unicodedata

from django.db.models import CharField, Func
from django.db.models.functions import Lower

NOME_DA_FUNCAO = "ZELO_BUSCA"


def normalizar_busca(texto) -> str:
    """Sem acento, em minúsculas (`casefold`: "ß" → "ss") e com os espaços colapsados."""
    decomposto = unicodedata.normalize("NFKD", str(texto if texto is not None else ""))
    sem_acento = "".join(c for c in decomposto if not unicodedata.combining(c))
    return " ".join(sem_acento.casefold().split())


def _funcao_do_sqlite(valor):
    return None if valor is None else normalizar_busca(valor)


def registrar_no_sqlite(sender, connection, **kwargs):
    """Gancho de `connection_created`: ensina a conexão SQLite a calcular `ZELO_BUSCA`."""
    if connection.vendor == "sqlite":
        connection.connection.create_function(NOME_DA_FUNCAO, 1, _funcao_do_sqlite, deterministic=True)


class SemAcento(Func):
    """O campo sem acento e em minúsculas. Use com `alias()`/`annotate()` e compare com
    `normalizar_busca(termo)`."""

    function = NOME_DA_FUNCAO
    arity = 1
    output_field = CharField()

    def as_sql(self, compiler, connection, **extra):
        # banco sem a função registrada: tira só a diferença de caixa
        return Lower(*self.get_source_expressions()).as_sql(compiler, connection)

    def as_sqlite(self, compiler, connection, **extra):
        return super().as_sql(compiler, connection, **extra)
