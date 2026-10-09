"""
Marca os termos JÁ assinados como "sem registro de envio".

O campo `via_email_status` nasceu com default `nao_enviada`, que é o certo para
sessão nova — ela começa sem via enviada. Para os termos assinados ANTES deste
rastreamento, porém, o default afirmaria algo falso: naquele desenho o envio
automático já rodava (via `on_commit`), só não gravava o resultado em lugar
nenhum durável. Deixá-los como "não enviada" mandaria o TI reenviar via que
provavelmente já saiu, e — pior — faria a tela acusar um defeito que não é esse.

`anterior` diz o que de fato se sabe: não há registro. O botão de reenviar
continua disponível para quem quiser garantir.
"""
from django.db import migrations, models


def marcar_anteriores(apps, schema_editor):
    TermoAssinatura = apps.get_model("ProjetoEstoque", "TermoAssinatura")
    TermoAssinatura.objects.filter(
        status="assinado", via_email_status="nao_enviada",
    ).update(
        via_email_status="anterior",
        via_email_detalhe="Termo assinado antes de o registro de envio da via existir.",
    )


def desmarcar_anteriores(apps, schema_editor):
    """Volta ao default. Reversível de verdade: o campo é descritivo, não
    carrega efeito, e o estado anterior era exatamente `nao_enviada`."""
    TermoAssinatura = apps.get_model("ProjetoEstoque", "TermoAssinatura")
    TermoAssinatura.objects.filter(via_email_status="anterior").update(
        via_email_status="nao_enviada", via_email_detalhe="",
    )


class Migration(migrations.Migration):

    dependencies = [
        ('ProjetoEstoque', '0172_termo_via_email_rastreada'),
    ]

    operations = [
        migrations.AlterField(
            model_name='termoassinatura',
            name='via_email_status',
            field=models.CharField(choices=[('nao_enviada', 'Via ainda não enviada'), ('enviada', 'Via enviada'), ('falhou', 'Falha no envio'), ('sem_email', 'Colaborador sem e-mail no cadastro'), ('anterior', 'Sem registro (anterior ao rastreamento)')], db_index=True, default='nao_enviada', max_length=12, verbose_name='Via por e-mail'),
        ),
        migrations.RunPython(marcar_anteriores, desmarcar_anteriores),
    ]
