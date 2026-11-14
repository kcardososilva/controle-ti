"""
Chips do Agro × estoque de TI (regra 66): o chip passa a ter o PATRIMÔNIO dele no estoque de TI.

No estoque de TI o chip é um `Item` (subtipo CHIP: ICCID no número de série, valor, nota fiscal, termo de
responsabilidade, movimentações); em Frotas e Conectividade ele é um `M2MChip` (linha, operadora, sessão, consumo,
em que veículo ou tablet está). Eram dois cadastros do mesmo SIM, sem ligação — hoje, 50 dos 205 chips existiam
nos dois. Esta migration cria o que liga um ao outro, e NADA mais:

  • `Subtipo.e_chip`        o TI marca o tipo cujos itens SÃO chips (o `aceita_chip` marca onde o chip VAI);
  • `M2MChip.item_ti`       a ligação 1 para 1 com o item do estoque (nula = ainda sem patrimônio), com quem/quando/como;
  • `M2MEvento.tipo`        dois tipos novos de auditoria (ligado / desligado).

A parte de dados só marca `e_chip` no subtipo chamado "CHIP" (sem distinguir maiúsculas), uma vez — como a 0201 fez
com `aceita_chip`. Não liga nenhum chip a nenhum item: quem conciliar é o TI, na tela de conciliação, olhando o que o
sistema propõe (mesmo ICCID). ADITIVA: três colunas anuláveis e uma booleana com padrão; nenhuma linha existente muda.
Produção: `migrate` (regra 7). Reversa: remove as colunas (a parte de dados não tem o que desfazer).
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

APP = "ProjetoEstoque"


def marcar_subtipo_chip(apps, schema_editor):
    Subtipo = apps.get_model(APP, "Subtipo")
    for subtipo in Subtipo.objects.filter(e_chip=False, aceita_chip=False):
        if (subtipo.nome or "").strip().lower() == "chip":
            subtipo.e_chip = True
            subtipo.save(update_fields=["e_chip"])


class Migration(migrations.Migration):

    dependencies = [
        (APP, "0205_agro_infraestrutura_e_localizacao"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="subtipo",
            name="e_chip",
            field=models.BooleanField(
                default=False, verbose_name="É chip (SIM)",
                help_text="Marque no tipo cujos itens SÃO chips (ex.: CHIP). Esses itens podem ser ligados ao chip de conectividade (linha, operadora, sessão e consumo) pelo ICCID. Um tipo não é chip e aceita chip ao mesmo tempo.",
            ),
        ),
        migrations.AddField(
            model_name="m2mchip",
            name="item_ti",
            field=models.OneToOneField(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="chip_conectividade",
                to="ProjetoEstoque.item", verbose_name="Item no estoque de TI",
                help_text="O chip como PATRIMÔNIO no estoque de TI. Ligação 1 para 1: um item só tem um chip, e um chip só tem um item.",
            ),
        ),
        migrations.AddField(model_name="m2mchip", name="item_ti_em", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(
            model_name="m2mchip",
            name="item_ti_por",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="m2mchip",
            name="item_ti_origem",
            field=models.CharField(
                blank=True, default="", max_length=16,
                choices=[
                    ("manual", "Ligado à mão"),
                    ("iccid", "Mesmo ICCID (automático)"),
                    ("chip_do_item", "Chip criado a partir do item"),
                    ("item_do_chip", "Item criado a partir do chip"),
                ],
                help_text="Como a ligação nasceu; vazio quando não há item.",
            ),
        ),
        migrations.AlterField(
            model_name="m2mevento",
            name="tipo",
            field=models.CharField(
                max_length=25,
                choices=[
                    ("cadastro", "Chip cadastrado"), ("edicao", "Chip editado"),
                    ("desativado", "Monitoramento desativado"), ("reativado", "Monitoramento reativado"),
                    ("token_alterado", "Token alterado"), ("config_alterada", "Configuração alterada"),
                    ("mapeamento_alterado", "Mapeamento alterado"), ("teste_conexao", "Teste de conexão"),
                    ("estado_sessao_mudou", "Estado da sessão mudou"), ("entrada", "Entrada do chip"),
                    ("vinculado", "Chip vinculado"), ("desvinculado", "Chip desvinculado"),
                    ("transferido", "Chip transferido"), ("substituido", "Chip substituído"),
                    ("retorno_disponibilidade", "Chip devolvido à disponibilidade"), ("inativado", "Chip inativado"),
                    ("item_removido", "Vínculo antigo com equipamento removido"), ("config_frota", "Cadastro de apoio alterado"),
                    ("item_ti_ligado", "Chip ligado ao estoque de TI"), ("item_ti_desligado", "Chip desligado do estoque de TI"),
                ],
            ),
        ),
        migrations.RunPython(marcar_subtipo_chip, migrations.RunPython.noop),
    ]
