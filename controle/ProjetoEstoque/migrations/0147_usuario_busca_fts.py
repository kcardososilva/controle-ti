# Tabela virtual FTS5 (SQLite) para busca textual de colaboradores por
# relevância (WHERE ... MATCH), com tokenização Unicode + remoção de acento.
# Mantida sincronizada pelo signal `_usuario_sync_fts` (ProjetoEstoque/signals.py)
# a cada save/delete de Usuario — ver services/busca_fts.py.
#
# É uma tabela auxiliar de índice, não uma FK Django: se a migration for
# revertida ou a tabela não existir por qualquer motivo, o signal de sync
# falha silenciosamente (best-effort) e a tela cai de volta pro filtro
# icontains — nunca quebra o cadastro de usuário.
from django.db import migrations

CREATE_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS usuario_busca_fts USING fts5(
    usuario_id UNINDEXED,
    nome,
    email,
    matricula,
    tokenize = "unicode61 remove_diacritics 2"
);
"""

DROP_SQL = "DROP TABLE IF EXISTS usuario_busca_fts;"


class Migration(migrations.Migration):

    dependencies = [
        ("ProjetoEstoque", "0146_sincroniza_pausa_preventivas_status_item"),
    ]

    operations = [
        migrations.RunSQL(sql=CREATE_SQL, reverse_sql=DROP_SQL),
    ]
