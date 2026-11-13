"""
Ambientes do Zelo (TI · Agro) — esquema e grupo.

  1. `PreferenciaAmbiente` (o último ambiente usado por login) — aditivo.
  2. a permissão `acessar_zelo_agro` (criada AQUI, não só pelo `post_migrate`,
     porque o grupo abaixo precisa dela já nesta migration — mesmo padrão da 0191);
  3. o grupo "Zelo Agro — Exclusivo", que prende o login no Agro. Ele já leva
     `acessar_zelo_agro`: quem está preso no Agro tem de alcançar pelo menos a
     visão geral, nunca ficar sem ambiente nenhum.

Nenhum dado existente muda: o TI continua sendo o padrão de todo usuário interno
(não há permissão de TI a distribuir), e quem já tem permissão de Frotas já
alcança o Agro por ela (ver `services/ambiente_service.py`). O nome do grupo e o
codename estão repetidos aqui de propósito: migration não importa model.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

APP = "ProjetoEstoque"
GRUPO_SOMENTE_AGRO = "Zelo Agro — Exclusivo"
PERMISSAO = ("acessar_zelo_agro", "Pode acessar o ambiente Zelo Agro (visão geral)")


def criar_grupo(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")
    Group = apps.get_model("auth", "Group")
    tipo, _ = ContentType.objects.get_or_create(app_label=APP, model="preferenciaambiente")
    permissao, _ = Permission.objects.get_or_create(
        content_type=tipo, codename=PERMISSAO[0], defaults={"name": PERMISSAO[1]},
    )
    grupo, _ = Group.objects.get_or_create(name=GRUPO_SOMENTE_AGRO)
    grupo.permissions.add(permissao)


def remover_grupo(apps, schema_editor):
    apps.get_model("auth", "Group").objects.filter(name=GRUPO_SOMENTE_AGRO).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('ProjetoEstoque', '0191_frota_dados_permissoes'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.CreateModel(
            name='PreferenciaAmbiente',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('ambiente', models.CharField(choices=[('ti', 'Zelo TI'), ('agro', 'Zelo Agro')], default='ti', max_length=8)),
                ('atualizado_em', models.DateTimeField(auto_now=True)),
                ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='preferencia_ambiente', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'Preferência de ambiente',
                'verbose_name_plural': 'Preferências de ambiente',
                'permissions': [('acessar_zelo_agro', 'Pode acessar o ambiente Zelo Agro (visão geral)')],
            },
        ),
        migrations.RunPython(criar_grupo, remover_grupo),
    ]
