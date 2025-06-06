# Generated by Django 5.2.1 on 2025-06-04 17:34

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ProjetoEstoque', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='equipamento',
            name='created_at',
            field=models.DateTimeField(auto_now_add=True, null=True),
        ),
        migrations.AddField(
            model_name='equipamento',
            name='updated_at',
            field=models.DateTimeField(auto_now=True),
        ),
        migrations.AlterField(
            model_name='equipamento',
            name='status',
            field=models.CharField(choices=[('ativo', 'Ativo'), ('backup', 'Backup'), ('manutencao', 'Manutenção'), ('queimado', 'Defeito')], default='ativo', max_length=20),
        ),
        migrations.AlterField(
            model_name='equipamento',
            name='subtipo',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to='ProjetoEstoque.subtipo'),
        ),
    ]
