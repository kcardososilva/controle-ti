from django.db import migrations


def criar_grupo(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Group.objects.get_or_create(name='Visualizador TV')


def remover_grupo(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Group.objects.filter(name='Visualizador TV').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('ProjetoEstoque', '0074_preventiva_data_agendamento'),
        ('auth', '0012_alter_user_first_name_max_length'),
    ]

    operations = [
        migrations.RunPython(criar_grupo, remover_grupo),
    ]
