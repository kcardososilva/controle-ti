"""
Chips (Zelo Agro) — a situação ganha Reservado, Pausado e Com defeito, e passa a lembrar DESDE QUANDO vale.

Aditiva: uma coluna nula e quatro `AlterField` que só mudam as opções (nenhuma delas altera a tabela).
- `M2MChip.situacao_uso` ganha `reservado`, `pausado` e `defeito`;
- `M2MChip.situacao_uso_em`: desde quando o chip está na situação atual (a tela diz "há N dias" e a sugestão "parado há
  muito" lê daqui). É gravada por `frota_vinculo_service._gravar_situacao`, só quando a situação muda de fato;
- `FrotaMovimentacao.tipo` ganha `situacao` (a mudança de situação sem sair do lugar);
- `M2MEvento.tipo` ganha `situacao_mudou` (o espelho dela na trilha do chip).

O preenchimento de `situacao_uso_em` nos chips que já existem é APROXIMADO e é dito assim na tela: a data da última
movimentação do chip, ou a do cadastro quando ele nunca se moveu. Chip vinculado e transferido há pouco mostra a data da
transferência, não a da primeira vinculação. Só preenche o que está vazio, então rodar de novo não muda nada.

Reversa: tira a coluna (o preenchimento não precisa de volta). Produção: `migrate`.
"""

from django.db import migrations, models
from django.db.models import DateTimeField, F, OuterRef, Subquery
from django.db.models.functions import Coalesce

APP = "ProjetoEstoque"


def preencher_situacao_uso_em(apps, schema_editor):
    M2MChip = apps.get_model(APP, "M2MChip")
    FrotaMovimentacao = apps.get_model(APP, "FrotaMovimentacao")
    ultima = FrotaMovimentacao.objects.filter(chip=OuterRef("pk")).order_by("-em", "-pk").values("em")[:1]
    M2MChip.objects.filter(situacao_uso_em__isnull=True).update(
        situacao_uso_em=Coalesce(Subquery(ultima), F("created_at"), output_field=DateTimeField()),
    )


class Migration(migrations.Migration):

    dependencies = [
        ("ProjetoEstoque", "0209_grupo_colaborador_agro"),
    ]

    operations = [
        migrations.AddField(
            model_name="m2mchip",
            name="situacao_uso_em",
            field=models.DateTimeField(
                blank=True, null=True,
                help_text="Desde quando o chip está nesta situação. Gravada junto de `situacao_uso`, só quando ela muda de fato.",
            ),
        ),
        migrations.AlterField(
            model_name="m2mchip",
            name="situacao_uso",
            field=models.CharField(
                choices=[
                    ("disponivel", "Disponível"), ("vinculado", "Vinculado"), ("reservado", "Reservado"),
                    ("pausado", "Pausado"), ("defeito", "Com defeito"), ("manutencao", "Em manutenção"),
                    ("inativo", "Inativo"),
                ],
                db_index=True, default="disponivel", max_length=12,
                help_text="Gravada só pelo serviço de vínculo. Não confundir com `monitorado` (consulta à API).",
            ),
        ),
        migrations.AlterField(
            model_name="frotamovimentacao",
            name="tipo",
            field=models.CharField(
                choices=[
                    ("entrada", "Entrada"), ("vinculacao", "Vinculação"), ("transferencia", "Transferência"),
                    ("desvinculacao", "Desvinculação"), ("retorno", "Retorno à disponibilidade"),
                    ("substituicao", "Substituição"), ("inativacao", "Inativação"), ("situacao", "Mudança de situação"),
                ],
                db_index=True, max_length=15,
            ),
        ),
        migrations.AlterField(
            model_name="m2mevento",
            name="tipo",
            field=models.CharField(
                choices=[
                    ("cadastro", "Chip cadastrado"), ("edicao", "Chip editado"),
                    ("desativado", "Monitoramento desativado"), ("reativado", "Monitoramento reativado"),
                    ("token_alterado", "Token alterado"), ("config_alterada", "Configuração alterada"),
                    ("mapeamento_alterado", "Mapeamento alterado"), ("teste_conexao", "Teste de conexão"),
                    ("estado_sessao_mudou", "Estado da sessão mudou"), ("entrada", "Entrada do chip"),
                    ("vinculado", "Chip vinculado"), ("desvinculado", "Chip desvinculado"),
                    ("transferido", "Chip transferido"), ("substituido", "Chip substituído"),
                    ("retorno_disponibilidade", "Chip devolvido à disponibilidade"), ("inativado", "Chip inativado"),
                    ("situacao_mudou", "Situação do chip mudou"),
                    ("item_removido", "Vínculo antigo com equipamento removido"),
                    ("config_frota", "Cadastro de apoio alterado"), ("item_ti_ligado", "Chip ligado ao estoque de TI"),
                    ("item_ti_desligado", "Chip desligado do estoque de TI"),
                ],
                max_length=25,
            ),
        ),
        migrations.RunPython(preencher_situacao_uso_em, migrations.RunPython.noop),
    ]
