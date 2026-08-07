# Sincroniza o estado de pausa das preventivas existentes com o status atual
# dos equipamentos (regra: contagem de preventiva só corre para item ATIVO).
#
# - Item fora de "ativo" com preventiva rodando → pausa (congela contagem,
#   deixa de aparecer como vencida).
# - Item "ativo" com preventiva pausada (flag defasada) → retoma reiniciando
#   o intervalo completo a partir de hoje (mesma regra de Preventiva.retomar).
#
# Modelos históricos não expõem os métodos pausar()/retomar(), por isso a
# lógica é replicada aqui com updates diretos de campo.
from datetime import date, timedelta

from django.db import migrations


def _intervalo(prev):
    try:
        dias = int(prev.equipamento.data_limite_preventiva or 0)
    except (TypeError, ValueError):
        dias = 0
    if dias > 0:
        return dias
    if prev.checklist_modelo_id and prev.checklist_modelo.intervalo_dias:
        try:
            return int(prev.checklist_modelo.intervalo_dias)
        except (TypeError, ValueError):
            return 0
    return 0


def sincronizar(apps, schema_editor):
    Preventiva = apps.get_model("ProjetoEstoque", "Preventiva")
    hoje = date.today()

    qs = Preventiva.objects.select_related("equipamento", "checklist_modelo")
    for prev in qs:
        ativo = prev.equipamento.status == "ativo"

        if not ativo and not prev.pausada:
            prev.pausada = True
            prev.data_pausada = hoje
            if prev.data_proxima:
                prev.dias_restantes_pausa = max((prev.data_proxima - hoje).days, 0)
            else:
                prev.dias_restantes_pausa = None
            prev.dentro_do_prazo = True
            prev.save(update_fields=[
                "pausada", "data_pausada", "dias_restantes_pausa",
                "dentro_do_prazo", "updated_at",
            ])

        elif ativo and prev.pausada:
            dias = _intervalo(prev)
            prev.pausada = False
            prev.data_reativacao = hoje
            if prev.data_agendamento:
                prev.data_proxima = prev.data_agendamento
            elif dias > 0:
                prev.data_proxima = hoje + timedelta(days=dias)
            elif prev.dias_restantes_pausa is not None:
                prev.data_proxima = hoje + timedelta(days=prev.dias_restantes_pausa)
            prev.dentro_do_prazo = True if not prev.data_proxima else hoje <= prev.data_proxima
            prev.data_pausada = None
            prev.dias_restantes_pausa = None
            prev.save(update_fields=[
                "pausada", "data_reativacao", "data_pausada", "dias_restantes_pausa",
                "data_proxima", "dentro_do_prazo", "updated_at",
            ])


class Migration(migrations.Migration):

    dependencies = [
        ("ProjetoEstoque", "0145_preventiva_data_reativacao"),
    ]

    operations = [
        migrations.RunPython(sincronizar, migrations.RunPython.noop),
    ]
