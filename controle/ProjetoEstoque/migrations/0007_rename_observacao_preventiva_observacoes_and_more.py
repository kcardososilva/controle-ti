# Generated by Django 5.2.1 on 2025-06-06 16:18

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ProjetoEstoque', '0006_remove_equipamento_atualizado_por_and_more'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RenameField(
            model_name='preventiva',
            old_name='observacao',
            new_name='observacoes',
        ),
        migrations.RemoveField(
            model_name='preventiva',
            name='data_ultima_preventiva',
        ),
        migrations.AddField(
            model_name='preventiva',
            name='data_anterior',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='preventiva',
            name='data_ultima',
            field=models.DateField(default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='preventiva',
            name='usuario',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL),
        ),
        migrations.AlterField(
            model_name='equipamento',
            name='data_limite_preventiva',
            field=models.PositiveIntegerField(blank=True, help_text='Intervalo em meses', null=True),
        ),
        migrations.AlterField(
            model_name='equipamento',
            name='precisa_preventiva',
            field=models.CharField(choices=[('sim', 'Sim'), ('nao', 'Não')], default='nao', max_length=3),
        ),
    ]
