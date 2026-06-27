from django.db import migrations

GRUPO_FORNECEDOR = "Fornecedor"


def criar_grupo(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.get_or_create(name=GRUPO_FORNECEDOR)


def remover_grupo(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name=GRUPO_FORNECEDOR).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("ProjetoEstoque", "0091_perfilfornecedor"),
    ]

    operations = [
        migrations.RunPython(criar_grupo, remover_grupo),
    ]
