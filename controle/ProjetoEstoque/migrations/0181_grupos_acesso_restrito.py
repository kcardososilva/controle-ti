"""
Cria os dois grupos de acesso restrito.

Mesmo padrão da 0075 (grupo "Visualizador TV"): o grupo precisa EXISTIR antes
de alguém poder ser colocado nele, e a tela de gestão não deveria ter que criá-lo
na primeira visita.

O reverse remove os grupos — e, com eles, o vínculo dos usuários (o M2M morre
com o Group). Isso é o certo para um downgrade: deixar o grupo órfão para trás
significaria que um `migrate` de volta mantém gente autorizada a uma área que o
código daquela versão não conhece.
"""
from django.db import migrations

GRUPOS = ("Portal de Contratações", "Consulta Quiosque")


def criar(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    for nome in GRUPOS:
        Group.objects.get_or_create(name=nome)


def remover(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name__in=GRUPOS).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("ProjetoEstoque", "0180_sistemas_corporativos_iniciais"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunPython(criar, remover),
    ]
