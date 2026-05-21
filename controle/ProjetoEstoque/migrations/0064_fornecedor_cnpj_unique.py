from django.db import migrations, models


def dedup_cnpj(apps, schema_editor):
    """
    Para cada CNPJ que aparece mais de uma vez, renomeia as ocorrências
    posteriores à primeira adicionando um sufixo '#2', '#3', etc.
    Garante que o campo fique único antes do AlterField ser aplicado.
    """
    Fornecedor = apps.get_model("ProjetoEstoque", "Fornecedor")

    from collections import defaultdict
    grupos = defaultdict(list)
    for f in Fornecedor.objects.order_by("id"):
        grupos[f.cnpj].append(f)

    for cnpj, fornecedores in grupos.items():
        if len(fornecedores) <= 1:
            continue
        for idx, f in enumerate(fornecedores[1:], start=2):
            novo = f"{cnpj}#{idx}"
            # Garante unicidade mesmo que já exista outro sufixo
            while Fornecedor.objects.filter(cnpj=novo).exists():
                idx += 1
                novo = f"{cnpj}#{idx}"
            f.cnpj = novo
            f.save(update_fields=["cnpj"])


class Migration(migrations.Migration):

    dependencies = [
        ("ProjetoEstoque", "0063_indexes_meta_improvements"),
    ]

    operations = [
        # Primeiro: deduplicar dados legados
        migrations.RunPython(dedup_cnpj, migrations.RunPython.noop),

        # Depois: aplicar a constraint unique
        migrations.AlterField(
            model_name="fornecedor",
            name="cnpj",
            field=models.CharField(max_length=18, unique=True),
        ),
    ]
