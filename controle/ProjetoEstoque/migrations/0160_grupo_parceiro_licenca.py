from django.db import migrations

GRUPO_PARCEIRO_LICENCA = "Parceiro de Licenças"


def criar_grupo(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.get_or_create(name=GRUPO_PARCEIRO_LICENCA)


def remover_grupo(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name=GRUPO_PARCEIRO_LICENCA).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("ProjetoEstoque", "0159_perfilparceirolicenca"),
    ]

    operations = [
        migrations.RunPython(criar_grupo, remover_grupo),
    ]
