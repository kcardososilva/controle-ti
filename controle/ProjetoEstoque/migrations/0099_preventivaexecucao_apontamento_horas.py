from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ProjetoEstoque", "0098_item_compartilhado_itemcolaborador"),
    ]

    operations = [
        migrations.AddField(
            model_name="preventivaexecucao",
            name="hora_inicio",
            field=models.TimeField(
                blank=True,
                null=True,
                help_text="Horário em que o técnico iniciou o serviço.",
                verbose_name="Hora de início",
            ),
        ),
        migrations.AddField(
            model_name="preventivaexecucao",
            name="hora_fim",
            field=models.TimeField(
                blank=True,
                null=True,
                help_text="Horário em que o técnico concluiu o serviço.",
                verbose_name="Hora de término",
            ),
        ),
        migrations.AddField(
            model_name="preventivaexecucao",
            name="duracao_minutos",
            field=models.PositiveIntegerField(
                blank=True,
                null=True,
                help_text="Tempo gasto na execução, em minutos. Calculado a partir de hora_inicio e hora_fim.",
                verbose_name="Duração (minutos)",
            ),
        ),
    ]
