# app_preventivas/migrations/000X_preventiva_execucao.py
from django.db import migrations, models
import django.db.models.deletion

class Migration(migrations.Migration):

    dependencies = [
        ("ProjetoEstoque", '0024_seed_licenca_lotes'),  # ajuste conforme seu último migration
    ]

    operations = [
        migrations.CreateModel(
            name='PreventivaExecucao',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                # Campos AuditModel:
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('criado_por', models.ForeignKey(null=True, blank=True, on_delete=django.db.models.deletion.SET_NULL, to='auth.user', related_name='+')),
                ('atualizado_por', models.ForeignKey(null=True, blank=True, on_delete=django.db.models.deletion.SET_NULL, to='auth.user', related_name='+')),

                ('data_execucao', models.DateField()),
                ('observacao', models.TextField(blank=True, null=True)),
                ('foto_antes', models.ImageField(upload_to='preventivas/%Y/%m/', blank=True, null=True)),
                ('foto_depois', models.ImageField(upload_to='preventivas/%Y/%m/', blank=True, null=True)),

                ('preventiva', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='ProjetoEstoque.preventiva', related_name='execucoes')),
            ],
            options={
                'ordering': ['-data_execucao', '-created_at'],
                'verbose_name': 'Execução de Preventiva',
                'verbose_name_plural': 'Execuções de Preventiva',
            },
        ),
        migrations.AddField(
            model_name='preventivaresposta',
            name='execucao',
            field=models.ForeignKey(null=True, blank=True, on_delete=django.db.models.deletion.CASCADE, to='ProjetoEstoque.preventivaexecucao', related_name='respostas'),
        ),
    ]
