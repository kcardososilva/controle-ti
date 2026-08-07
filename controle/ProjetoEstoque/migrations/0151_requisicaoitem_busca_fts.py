# Tabela virtual FTS5 (SQLite) para busca por relevância no campo "descricao"
# de RequisicaoItem (WHERE ... MATCH) — tela /requisicoes/itens/. Sincronizada
# por signal (`_requisicao_item_sincronizar_fts` em ProjetoEstoque/signals.py),
# não trigger: RequisicaoItem só é criado/atualizado via .save() (ver
# services/requisicao_service.py); os únicos `.update()` em massa no model
# tocam apenas status/atualizado_por/updated_at, nunca descricao/codigo.
#
# `codigo` (código Datasul, numérico) fica de fora de propósito — é buscado
# por fragmento no meio da string (ex.: últimos dígitos), e o FTS5 (unicode61)
# só casa por prefixo de token. icontains nele continua sem mudança (ver
# services/busca_fts.py: buscar_requisicao_item_ids combina os dois).
from django.db import migrations

CREATE_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS requisicaoitem_busca_fts USING fts5(
    requisicaoitem_id UNINDEXED,
    descricao,
    tokenize = "unicode61 remove_diacritics 2"
);
"""

DROP_SQL = "DROP TABLE IF EXISTS requisicaoitem_busca_fts;"


class Migration(migrations.Migration):

    dependencies = [
        ("ProjetoEstoque", "0150_backfill_item_busca_fts"),
    ]

    operations = [
        migrations.RunSQL(sql=CREATE_SQL, reverse_sql=DROP_SQL),
    ]
