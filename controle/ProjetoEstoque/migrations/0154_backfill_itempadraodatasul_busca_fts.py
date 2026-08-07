# Popula itempadraodatasul_busca_fts com os itens padrão já cadastrados.
# Daqui em diante o signal `_item_padrao_sincronizar_fts` mantém a tabela em
# dia sozinho.
from django.db import migrations


def popular(apps, schema_editor):
    ItemPadraoDatasul = apps.get_model("ProjetoEstoque", "ItemPadraoDatasul")
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("DELETE FROM itempadraodatasul_busca_fts")
        for pk, descricao in ItemPadraoDatasul.objects.values_list("id", "descricao"):
            cursor.execute(
                "INSERT INTO itempadraodatasul_busca_fts (itempadraodatasul_id, descricao) VALUES (%s, %s)",
                [pk, descricao or ""],
            )


def limpar(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("DELETE FROM itempadraodatasul_busca_fts")


class Migration(migrations.Migration):

    dependencies = [
        ("ProjetoEstoque", "0153_itempadraodatasul_busca_fts"),
    ]

    operations = [
        migrations.RunPython(popular, limpar),
    ]
