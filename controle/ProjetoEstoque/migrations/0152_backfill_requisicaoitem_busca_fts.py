# Popula requisicaoitem_busca_fts com os itens de requisição já cadastrados.
# Daqui em diante o signal `_requisicao_item_sincronizar_fts` mantém a tabela
# em dia sozinho.
from django.db import migrations


def popular(apps, schema_editor):
    RequisicaoItem = apps.get_model("ProjetoEstoque", "RequisicaoItem")
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("DELETE FROM requisicaoitem_busca_fts")
        for pk, descricao in RequisicaoItem.objects.values_list("id", "descricao"):
            cursor.execute(
                "INSERT INTO requisicaoitem_busca_fts (requisicaoitem_id, descricao) VALUES (%s, %s)",
                [pk, descricao or ""],
            )


def limpar(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("DELETE FROM requisicaoitem_busca_fts")


class Migration(migrations.Migration):

    dependencies = [
        ("ProjetoEstoque", "0151_requisicaoitem_busca_fts"),
    ]

    operations = [
        migrations.RunPython(popular, limpar),
    ]
