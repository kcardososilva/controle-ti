# Popula usuario_busca_fts com os colaboradores já cadastrados. Daqui em
# diante o signal `_usuario_sincronizar_fts` mantém a tabela em dia sozinho.
from django.db import migrations


def popular(apps, schema_editor):
    Usuario = apps.get_model("ProjetoEstoque", "Usuario")
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("DELETE FROM usuario_busca_fts")
        for usuario_id, nome, email, matricula in Usuario.objects.values_list(
            "id", "nome", "email", "matricula"
        ):
            cursor.execute(
                "INSERT INTO usuario_busca_fts (usuario_id, nome, email, matricula) "
                "VALUES (%s, %s, %s, %s)",
                [usuario_id, nome or "", email or "", matricula or ""],
            )


def limpar(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("DELETE FROM usuario_busca_fts")


class Migration(migrations.Migration):

    dependencies = [
        ("ProjetoEstoque", "0147_usuario_busca_fts"),
    ]

    operations = [
        migrations.RunPython(popular, limpar),
    ]
