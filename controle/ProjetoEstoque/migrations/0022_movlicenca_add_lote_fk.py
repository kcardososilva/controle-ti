from django.db import migrations

def seed_lotes(apps, schema_editor):
    Licenca = apps.get_model("ProjetoEstoque", "Licenca")
    LicencaLote = apps.get_model("ProjetoEstoque", "LicencaLote")
    for lic in Licenca.objects.all():
        qtd = lic.quantidade or 0
        LicencaLote.objects.create(
            licenca_id=lic.id,
            quantidade_total=qtd,
            quantidade_disponivel=qtd,
            custo_ciclo=lic.custo,  # usa custo atual da Licenca como custo padrão do 1º lote
            data_compra=lic.data_inicio,
            fornecedor_id=lic.fornecedor_id,
            centro_custo_id=lic.centro_custo_id,
            criado_por_id=lic.criado_por_id,
            atualizado_por_id=lic.atualizado_por_id,
        )

class Migration(migrations.Migration):
    dependencies = [
        ('ProjetoEstoque', '0021_create_licenca_lote'),
    ]
    operations = [migrations.RunPython(seed_lotes, migrations.RunPython.noop)]
