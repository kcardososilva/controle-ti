from django.db import migrations

# Categorias padrão pedidas para o Kanban de Requisições — reaproveita a
# tabela Categoria já existente (Item/Subtipo), em vez de um enum paralelo.
_SEED = [
    "Ferramentas",
    "Materiais de Infraestrutura",
    "Materiais de TI",
    "Materiais de Escritório",
]


def seed(apps, schema_editor):
    Categoria = apps.get_model("ProjetoEstoque", "Categoria")
    for nome in _SEED:
        Categoria.objects.get_or_create(nome=nome)


def unseed(apps, schema_editor):
    Categoria = apps.get_model("ProjetoEstoque", "Categoria")
    Categoria.objects.filter(nome__in=_SEED).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("ProjetoEstoque", "0134_requisicao_requisicaoitem_comentariorequisicaoitem_and_more"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
