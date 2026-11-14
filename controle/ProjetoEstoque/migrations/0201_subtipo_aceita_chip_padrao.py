"""
Conectividade — quais tipos de equipamento do estoque de TI aceitam chip.

A migration anterior (0200) criou `Subtipo.aceita_chip` (padrão `False`) e deixou o vínculo de chip
apontar para um equipamento do estoque de TI. Esta só marca, uma vez, os tipos que o TI usa com chip
hoje — **Tablet** e **Celular** — para a ficha desses equipamentos já oferecer "Vincular chip".

Não muda o esquema. Só liga o marcador em quem ainda está desligado, pelo NOME do subtipo (sem
distinguir maiúsculas): subtipo com outro nome não é tocado, e quem desmarcar depois no cadastro do
subtipo não é refeito (a migration roda uma vez). Reversa: não faz nada — desligar o marcador de volta
seria desfazer uma escolha que o TI pode ter ajustado.
"""

from django.db import migrations

APP = "ProjetoEstoque"
NOMES_QUE_USAM_CHIP = ("tablet", "celular")


def marcar(apps, schema_editor):
    Subtipo = apps.get_model(APP, "Subtipo")
    for subtipo in Subtipo.objects.filter(aceita_chip=False):
        if (subtipo.nome or "").strip().lower() in NOMES_QUE_USAM_CHIP:
            subtipo.aceita_chip = True
            subtipo.save(update_fields=["aceita_chip"])


class Migration(migrations.Migration):
    dependencies = [(APP, "0200_frota_chip_em_equipamento_ti")]

    operations = [migrations.RunPython(marcar, migrations.RunPython.noop)]
