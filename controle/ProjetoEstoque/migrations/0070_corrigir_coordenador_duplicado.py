"""
Corrige dois padrões de erro na coluna 'coordenador' gerados pelo import:

  Regra 1 — coordenador == supervisor (mesmo nome):
    A planilha preenche os dois campos com o mesmo valor quando a área não
    tem Coordenador intermediário. O correto é Coordenador = NULL.
    Afetados: 93 registros.

  Regra 2 — supervisor vazio + coordenador == responsavel (Junção):
    O nome aparece em Coordenador e Junção mas não em Supervisor. A pessoa
    é o Supervisor direto. O correto é mover o valor para Supervisor e
    limpar Coordenador.
    Afetados: 147 registros.

Total: 240 colaboradores corrigidos.
"""
from django.db import migrations


def corrigir_coordenador(apps, schema_editor):
    Usuario = apps.get_model("ProjetoEstoque", "Usuario")

    # Regra 1: coordenador == supervisor → limpar coordenador
    corrigidos_r1 = 0
    qs1 = (
        Usuario.objects
        .exclude(coordenador__isnull=True)
        .exclude(coordenador="")
        .exclude(supervisor__isnull=True)
        .exclude(supervisor="")
    )
    for u in qs1.iterator():
        if (u.coordenador or "").strip().lower() == (u.supervisor or "").strip().lower():
            u.coordenador = None
            u.save(update_fields=["coordenador"])
            corrigidos_r1 += 1

    # Regra 2: supervisor vazio + coordenador == responsavel → mover para supervisor
    corrigidos_r2 = 0
    qs2 = (
        Usuario.objects
        .filter(supervisor__isnull=True)
        .exclude(coordenador__isnull=True)
        .exclude(coordenador="")
        .exclude(responsavel__isnull=True)
        .exclude(responsavel="")
    )
    for u in qs2.iterator():
        if (u.coordenador or "").strip().lower() == (u.responsavel or "").strip().lower():
            u.supervisor  = u.coordenador
            u.coordenador = None
            u.save(update_fields=["supervisor", "coordenador"])
            corrigidos_r2 += 1


def reverter_coordenador(apps, schema_editor):
    # Reversão não é implementada pois os dados originais foram sobrescritos.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("ProjetoEstoque", "0069_populate_diretor_via_cadeia"),
    ]

    operations = [
        migrations.RunPython(corrigir_coordenador, reverter_coordenador),
    ]
