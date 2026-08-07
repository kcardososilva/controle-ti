# Popula item_busca_fts com os equipamentos já cadastrados. Daqui em diante
# os triggers da migration 0149 mantêm a tabela em dia sozinhos.
from django.db import migrations


def popular(apps, schema_editor):
    Item = apps.get_model("ProjetoEstoque", "Item")
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("DELETE FROM item_busca_fts")
        for item_id, nome in Item.objects.values_list("id", "nome"):
            cursor.execute(
                "INSERT INTO item_busca_fts (item_id, nome) VALUES (%s, %s)",
                [item_id, nome or ""],
            )


def limpar(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("DELETE FROM item_busca_fts")


class Migration(migrations.Migration):

    dependencies = [
        ("ProjetoEstoque", "0149_item_busca_fts"),
    ]

    operations = [
        migrations.RunPython(popular, limpar),
    ]
