# Tabela virtual FTS5 (SQLite) para busca por relevância no campo "descricao"
# de ItemPadraoDatasul (WHERE ... MATCH) — tela /requisicoes/catalogo/.
# Sincronizada por signal (`_item_padrao_sincronizar_fts` em
# ProjetoEstoque/signals.py): o catálogo só é criado/atualizado via .save()
# (views/requisicoes.py e a importação de planilha em
# services/requisicao_service.py::importar_catalogo_datasul), nunca em massa.
#
# `codigo` (código Datasul, numérico) fica de fora de propósito — mesmo
# motivo de RequisicaoItem (migration 0151): busca por fragmento no meio da
# string, que o FTS5 (prefixo de token) não cobre. icontains nele continua
# sem mudança (ver services/busca_fts.py: buscar_item_padrao_ids).
from django.db import migrations

CREATE_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS itempadraodatasul_busca_fts USING fts5(
    itempadraodatasul_id UNINDEXED,
    descricao,
    tokenize = "unicode61 remove_diacritics 2"
);
"""

DROP_SQL = "DROP TABLE IF EXISTS itempadraodatasul_busca_fts;"


class Migration(migrations.Migration):

    dependencies = [
        ("ProjetoEstoque", "0152_backfill_requisicaoitem_busca_fts"),
    ]

    operations = [
        migrations.RunSQL(sql=CREATE_SQL, reverse_sql=DROP_SQL),
    ]
