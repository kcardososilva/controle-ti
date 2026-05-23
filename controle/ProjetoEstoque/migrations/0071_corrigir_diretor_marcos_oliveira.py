"""
Corrige o campo 'diretor' dos 15 colaboradores que têm gestor='MARCOS OLIVEIRA'
(MARCOS ANTONIO SOUSA OLIVEIRA, ID=53).

Causa: o resolvedor de nomes abreviados da migration 0069 escolheu
MARCOS ANTONIO DE OLIVEIRA (sob CARLOS OLIVEIRA) em vez de
MARCOS ANTONIO SOUSA OLIVEIRA (sob GISELE NASCENTE) ao processar
a abreviatura 'MARCOS OLIVEIRA' — ambos têm first='marcos' e last='oliveira'.

Correção: diretor = 'GISELE NASCENTE' para todos com gestor='MARCOS OLIVEIRA'.
"""
from django.db import migrations


def corrigir_diretor_marcos(apps, schema_editor):
    Usuario = apps.get_model("ProjetoEstoque", "Usuario")
    atualizados = Usuario.objects.filter(
        gestor="MARCOS OLIVEIRA",
        diretor="CARLOS OLIVEIRA",
    ).update(diretor="GISELE NASCENTE")
    # print informativo — visible em migrate --verbosity 2
    _ = atualizados  # noqa: F841


def reverter(apps, schema_editor):
    Usuario = apps.get_model("ProjetoEstoque", "Usuario")
    Usuario.objects.filter(
        gestor="MARCOS OLIVEIRA",
        diretor="GISELE NASCENTE",
    ).update(diretor="CARLOS OLIVEIRA")


class Migration(migrations.Migration):

    dependencies = [
        ("ProjetoEstoque", "0070_corrigir_coordenador_duplicado"),
    ]

    operations = [
        migrations.RunPython(corrigir_diretor_marcos, reverter),
    ]
