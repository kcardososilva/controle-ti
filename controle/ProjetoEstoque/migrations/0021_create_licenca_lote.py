from django.db import migrations, models
import django.db.models.deletion

class Migration(migrations.Migration):
    dependencies = [
        ('ProjetoEstoque', '0020_alter_licenca_custo'),
    ]

    operations = [
        migrations.CreateModel(
            name="LicencaLote",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("criado_por", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="licencalote_criador", to="auth.user")),
                ("atualizado_por", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="licencalote_atualizador", to="auth.user")),
                ("quantidade_total", models.PositiveIntegerField()),
                ("quantidade_disponivel", models.PositiveIntegerField()),
                ("custo_ciclo", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ("data_compra", models.DateField(blank=True, null=True)),
                ("observacao", models.TextField(blank=True, null=True)),
                ("licenca", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="lotes", to="ProjetoEstoque.licenca")),
                ("fornecedor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to="ProjetoEstoque.fornecedor")),
                ("centro_custo", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to="ProjetoEstoque.centrocusto")),
            ],
            options={"ordering": ["-created_at", "-id"]},
        ),
    ]
