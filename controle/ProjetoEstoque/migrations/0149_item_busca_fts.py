# Tabela virtual FTS5 (SQLite) para busca por relevância no campo "nome" de
# Item (WHERE ... MATCH), com tokenização Unicode + remoção de acento.
#
# Diferente de usuario_busca_fts (migration 0147), aqui a sincronização é por
# TRIGGER SQL, não por signal Django: o projeto já tem scripts de correção em
# massa que usam Item.objects.bulk_update(...) (ver deploy_scripts_routerlink/),
# que não dispara post_save. Trigger é a nível de banco — sobrevive a
# bulk_update/bulk_create futuros sem exigir que quem escrever o script lembre
# de ressincronizar o índice.
#
# numero_serie/modelo ficam de fora de propósito: são buscados por fragmento
# no meio da string (ex.: últimos dígitos de série), e o FTS5 (unicode61) só
# casa por prefixo de token — trocaria o comportamento atual por um pior
# nesses dois campos. icontains neles continua sem mudança.
#
# sqlite3 só executa um statement por vez (cursor.execute), por isso cada
# CREATE vira uma RunSQL própria em vez de um bloco só.
from django.db import migrations

CREATE_TABLE_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS item_busca_fts USING fts5(
    item_id UNINDEXED,
    nome,
    tokenize = "unicode61 remove_diacritics 2"
);
"""
DROP_TABLE_SQL = "DROP TABLE IF EXISTS item_busca_fts;"

CREATE_TRIGGER_AI_SQL = """
CREATE TRIGGER IF NOT EXISTS item_busca_fts_ai AFTER INSERT ON ProjetoEstoque_item BEGIN
    INSERT INTO item_busca_fts (item_id, nome) VALUES (new.id, new.nome);
END;
"""
DROP_TRIGGER_AI_SQL = "DROP TRIGGER IF EXISTS item_busca_fts_ai;"

CREATE_TRIGGER_AU_SQL = """
CREATE TRIGGER IF NOT EXISTS item_busca_fts_au AFTER UPDATE ON ProjetoEstoque_item BEGIN
    DELETE FROM item_busca_fts WHERE item_id = old.id;
    INSERT INTO item_busca_fts (item_id, nome) VALUES (new.id, new.nome);
END;
"""
DROP_TRIGGER_AU_SQL = "DROP TRIGGER IF EXISTS item_busca_fts_au;"

CREATE_TRIGGER_AD_SQL = """
CREATE TRIGGER IF NOT EXISTS item_busca_fts_ad AFTER DELETE ON ProjetoEstoque_item BEGIN
    DELETE FROM item_busca_fts WHERE item_id = old.id;
END;
"""
DROP_TRIGGER_AD_SQL = "DROP TRIGGER IF EXISTS item_busca_fts_ad;"


class Migration(migrations.Migration):

    dependencies = [
        ("ProjetoEstoque", "0148_backfill_usuario_busca_fts"),
    ]

    operations = [
        migrations.RunSQL(sql=CREATE_TABLE_SQL, reverse_sql=DROP_TABLE_SQL),
        migrations.RunSQL(sql=CREATE_TRIGGER_AI_SQL, reverse_sql=DROP_TRIGGER_AI_SQL),
        migrations.RunSQL(sql=CREATE_TRIGGER_AU_SQL, reverse_sql=DROP_TRIGGER_AU_SQL),
        migrations.RunSQL(sql=CREATE_TRIGGER_AD_SQL, reverse_sql=DROP_TRIGGER_AD_SQL),
    ]
