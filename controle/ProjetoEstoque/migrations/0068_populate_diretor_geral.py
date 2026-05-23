from django.db import migrations


DIRETOR_GERAL_PADRAO = "MIGUEL PRADO"


def preencher_diretor_geral(apps, schema_editor):
    Usuario = apps.get_model("ProjetoEstoque", "Usuario")
    Usuario.objects.filter(diretor_geral__isnull=True).update(
        diretor_geral=DIRETOR_GERAL_PADRAO
    )
    Usuario.objects.filter(diretor_geral="").update(
        diretor_geral=DIRETOR_GERAL_PADRAO
    )


def reverter_diretor_geral(apps, schema_editor):
    Usuario = apps.get_model("ProjetoEstoque", "Usuario")
    Usuario.objects.filter(diretor_geral=DIRETOR_GERAL_PADRAO).update(
        diretor_geral=None
    )


class Migration(migrations.Migration):

    dependencies = [
        ("ProjetoEstoque", "0067_usuario_diretor"),
    ]

    operations = [
        migrations.RunPython(
            preencher_diretor_geral,
            reverter_diretor_geral,
        ),
    ]
