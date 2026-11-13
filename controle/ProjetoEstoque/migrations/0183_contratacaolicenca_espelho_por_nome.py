"""Licença da contratação: usuário espelho vira TEXTO, como já é no sistema.

A 0182 criou `perfil_espelho` como FK para `Usuario`. No mesmo dia o módulo
passou a desenhar sistema e licença como UMA lista só ("marque a caixa e diga
de quem copiar o perfil"), e dois tipos de campo para a mesma pergunta
obrigariam a duas versões de cada linha do form, do template e do service.

As quatro operações existem para não perder o que já foi digitado: o nome do
usuário apontado pela FK é copiado para a coluna nova ANTES de a FK sair.
Trocar direto (remove + add) devolveria o campo vazio.
"""

from django.db import migrations, models


def fk_para_nome(apps, schema_editor):
    ContratacaoLicenca = apps.get_model("ProjetoEstoque", "ContratacaoLicenca")
    linhas = ContratacaoLicenca.objects.filter(
        perfil_espelho__isnull=False,
    ).select_related("perfil_espelho")
    for linha in linhas:
        ContratacaoLicenca.objects.filter(pk=linha.pk).update(
            perfil_espelho_nome=(linha.perfil_espelho.nome or "")[:150],
        )


class Migration(migrations.Migration):

    dependencies = [
        ('ProjetoEstoque', '0182_contratacaolicenca_perfil_espelho'),
    ]

    operations = [
        migrations.AddField(
            model_name='contratacaolicenca',
            name='perfil_espelho_nome',
            field=models.CharField(
                blank=True, default='', max_length=150,
                help_text='Nome do usuário de quem copiar o perfil nesta licença.',
                verbose_name='Usuário espelho',
            ),
        ),
        migrations.RunPython(fk_para_nome, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='contratacaolicenca',
            name='perfil_espelho',
        ),
        migrations.RenameField(
            model_name='contratacaolicenca',
            old_name='perfil_espelho_nome',
            new_name='perfil_espelho',
        ),
    ]
