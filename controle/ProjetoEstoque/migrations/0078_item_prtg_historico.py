from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('ProjetoEstoque', '0077_item_status_historico'),
    ]

    operations = [
        migrations.CreateModel(
            name='ItemPRTGHistorico',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('prtg_objid', models.IntegerField(verbose_name='PRTG ObjID')),
                ('status_anterior', models.CharField(blank=True, default='', max_length=20)),
                ('status_novo', models.CharField(max_length=20)),
                ('registrado_em', models.DateTimeField(auto_now_add=True)),
                ('item', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='prtg_historico',
                    to='ProjetoEstoque.item',
                    verbose_name='Equipamento',
                )),
            ],
            options={
                'verbose_name': 'Histórico PRTG do Item',
                'verbose_name_plural': 'Históricos PRTG dos Itens',
                'ordering': ['-registrado_em'],
            },
        ),
        migrations.AddIndex(
            model_name='itemprtghistorico',
            index=models.Index(fields=['item', '-registrado_em'], name='projetoesto_item_prtg_idx'),
        ),
    ]
