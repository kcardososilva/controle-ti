# Generated by Django 5.2.1 on 2025-06-12 19:14

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ProjetoEstoque', '0021_alter_equipamento_numero_serie'),
    ]

    operations = [
        migrations.AlterField(
            model_name='equipamento',
            name='local',
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
    ]
