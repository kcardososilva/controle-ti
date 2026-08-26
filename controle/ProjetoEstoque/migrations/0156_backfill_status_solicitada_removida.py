# O status "Solicitada" (Requisicao) foi retirado do fluxo — a etapa
# intermediária entre Rascunho e Enviada para Aprovação deixou de existir (a
# coluna "Solicitado" também saiu do quadro Kanban). Qualquer requisição que
# ainda esteja parada nesse status (ou guardando-o em `status_anterior_pausa`
# pra retomar de uma pausa) volta para "Rascunho" — o estado mais próximo
# semanticamente (nunca foi enviada pra aprovação de verdade).
#
# Irreversível por natureza: depois de rodar, não há como saber quais linhas
# eram "solicitada" antes (o valor já virou "rascunho").
from django.db import migrations


def normalizar(apps, schema_editor):
    Requisicao = apps.get_model("ProjetoEstoque", "Requisicao")
    Requisicao.objects.filter(status="solicitada").update(status="rascunho")
    Requisicao.objects.filter(status_anterior_pausa="solicitada").update(status_anterior_pausa="rascunho")


class Migration(migrations.Migration):

    dependencies = [
        ("ProjetoEstoque", "0155_requisicaoitem_numero_nf_and_more"),
    ]

    operations = [
        migrations.RunPython(normalizar, migrations.RunPython.noop),
    ]
