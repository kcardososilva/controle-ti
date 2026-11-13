"""
Frotas e Conectividade — esquema (Fase D).

Cria operadoras, tipos de veículo, veículos, vínculos, movimentações, eventos,
importações e regras de alerta; acrescenta `operadora`, `numero_linha` e
`situacao_uso` ao chip; torna o chip da ocorrência opcional (e acrescenta o veículo);
e REMOVE `M2MChip.item` (o vínculo com o estoque de TI).

Três cuidados nesta migration:
  • a TIM é semeada logo depois da tabela de operadoras (aqui, e não na 0191, porque a
    operadora dos chips existentes precisa dela antes de o campo ficar obrigatório);
  • `operadora` entra NULA, é preenchida com a TIM e só então vira obrigatória;
  • o RunPython que registra em M2MEvento o `Item` de cada chip roda ANTES do RemoveField.
Os dados semeados (tipos, regras de alerta) e as permissões ficam na 0191.

Reversível: o reverso devolve o `item` a partir dos eventos e desfaz o resto na ordem inversa.
"""

import ProjetoEstoque.models
import django.db.models.deletion
import django.utils.timezone
import uuid
from django.conf import settings
from django.db import migrations, models


def semear_operadora_tim(apps, schema_editor):
    """A TIM, com a integração que o módulo já tinha. Idempotente."""
    Operadora = apps.get_model("ProjetoEstoque", "ConectividadeOperadora")
    Operadora.objects.get_or_create(nome="TIM", defaults={"integracao": "tim_m2m", "ativo": True})


def preencher_operadora_e_registrar_item(apps, schema_editor):
    """Todo chip que já existia é TIM. E o equipamento (`Item`) que algum chip tinha
    não se perde em silêncio: vira um M2MEvento `item_removido` antes de o campo sumir."""
    Chip = apps.get_model("ProjetoEstoque", "M2MChip")
    Evento = apps.get_model("ProjetoEstoque", "M2MEvento")
    Operadora = apps.get_model("ProjetoEstoque", "ConectividadeOperadora")

    tim = Operadora.objects.get(nome="TIM")
    Chip.objects.filter(operadora__isnull=True).update(operadora=tim)
    for chip in Chip.objects.exclude(item__isnull=True).select_related("item"):
        Evento.objects.create(
            chip=chip, tipo="item_removido", usuario=None,
            dados={
                "item_id": chip.item_id, "item_nome": chip.item.nome, "numero_serie": chip.item.numero_serie or "",
                "motivo": "O vínculo direto com equipamento do estoque de TI saiu; o veículo passou a ser o equipamento do chip.",
            },
        )


def restaurar_item_dos_eventos(apps, schema_editor):
    """Reverso: o campo `item` já voltou (vazio); devolve o equipamento a partir dos
    eventos gravados na ida, se o `Item` ainda existe, e apaga esses eventos."""
    Chip = apps.get_model("ProjetoEstoque", "M2MChip")
    Evento = apps.get_model("ProjetoEstoque", "M2MEvento")
    Item = apps.get_model("ProjetoEstoque", "Item")

    for evento in Evento.objects.filter(tipo="item_removido").exclude(chip__isnull=True):
        item_id = (evento.dados or {}).get("item_id")
        if item_id and Item.objects.filter(pk=item_id).exists():
            Chip.objects.filter(pk=evento.chip_id).update(item_id=item_id)
    Evento.objects.filter(tipo="item_removido").delete()


class Migration(migrations.Migration):

    dependencies = [
        ('ProjetoEstoque', '0189_m2m_grupo_permissoes'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ConectividadeOperadora',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('nome', models.CharField(max_length=80, unique=True)),
                ('integracao', models.CharField(blank=True, choices=[('tim_m2m', 'TIM M2M (API)')], default='', help_text='Vazio = sem integração: os chips são cadastrados e controlados, mas não consultados pela API.', max_length=20)),
                ('ativo', models.BooleanField(default=True)),
            ],
            options={
                'verbose_name': 'Operadora de conectividade',
                'verbose_name_plural': 'Operadoras de conectividade',
                'ordering': ['nome'],
            },
        ),
        migrations.RunPython(semear_operadora_tim, migrations.RunPython.noop),
        migrations.CreateModel(
            name='FrotaEvento',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('tipo', models.CharField(choices=[('cadastro', 'Veículo cadastrado'), ('edicao', 'Veículo editado'), ('status', 'Status alterado')], max_length=10)),
                ('dados', models.JSONField(blank=True, default=dict)),
                ('em', models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options={
                'verbose_name': 'Evento do veículo',
                'verbose_name_plural': 'Eventos dos veículos',
                'ordering': ['-em', '-pk'],
            },
        ),
        migrations.CreateModel(
            name='FrotaImportacaoChips',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('arquivo_nome', models.CharField(max_length=255)),
                ('sha256', models.CharField(db_index=True, max_length=64)),
                ('formato', models.CharField(choices=[('xlsx', 'Excel (.xlsx)'), ('csv', 'CSV')], max_length=5)),
                ('estado', models.CharField(choices=[('previa', 'Prévia'), ('confirmada', 'Confirmada'), ('cancelada', 'Cancelada')], db_index=True, default='previa', max_length=10)),
                ('criada_em', models.DateTimeField(default=django.utils.timezone.now)),
                ('confirmada_em', models.DateTimeField(blank=True, null=True)),
                ('linhas', models.PositiveIntegerField(default=0)),
                ('validas', models.PositiveIntegerField(default=0)),
                ('importados', models.PositiveIntegerField(default=0)),
                ('existentes', models.PositiveIntegerField(default=0)),
                ('rejeitados', models.PositiveIntegerField(default=0)),
                ('avisos', models.PositiveIntegerField(default=0, help_text='Linhas com pelo menos um aviso.')),
            ],
            options={
                'verbose_name': 'Importação de chips',
                'verbose_name_plural': 'Importações de chips',
                'ordering': ['-criada_em', '-pk'],
            },
        ),
        migrations.CreateModel(
            name='FrotaImportacaoLinha',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('numero', models.PositiveIntegerField(help_text='Número da linha na planilha (a do cabeçalho é a 1).')),
                ('dados', models.JSONField(blank=True, default=dict)),
                ('situacao', models.CharField(choices=[('valida', 'Válida'), ('existente', 'Já cadastrada'), ('rejeitada', 'Rejeitada'), ('importada', 'Importada')], db_index=True, max_length=10)),
                ('motivos', models.JSONField(blank=True, default=list)),
                ('avisos', models.JSONField(blank=True, default=list)),
            ],
            options={
                'verbose_name': 'Linha de importação de chips',
                'verbose_name_plural': 'Linhas de importação de chips',
                'ordering': ['importacao', 'numero'],
            },
        ),
        migrations.CreateModel(
            name='FrotaMovimentacao',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('tipo', models.CharField(choices=[('entrada', 'Entrada'), ('vinculacao', 'Vinculação'), ('transferencia', 'Transferência'), ('desvinculacao', 'Desvinculação'), ('retorno', 'Retorno à disponibilidade'), ('substituicao', 'Substituição'), ('inativacao', 'Inativação')], db_index=True, max_length=15)),
                ('operacao', models.UUIDField(db_index=True, default=uuid.uuid4, help_text='Agrupa as linhas de uma mesma ação.')),
                ('motivo', models.CharField(blank=True, default='', max_length=255)),
                ('observacoes', models.TextField(blank=True, default='')),
                ('usuario_nome', models.CharField(blank=True, default='', help_text='O login no momento (sobrevive à exclusão do usuário).', max_length=150)),
                ('em', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
            ],
            options={
                'verbose_name': 'Movimentação de chip',
                'verbose_name_plural': 'Movimentações de chips',
                'ordering': ['-em', '-pk'],
            },
        ),
        migrations.CreateModel(
            name='FrotaRegraAlerta',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('tipo', models.CharField(choices=[('falha_recorrente', 'Falhas de consulta seguidas'), ('sem_sessao', 'Sem sessão'), ('desatualizado', 'Dados desatualizados'), ('consumo_elevado', 'Consumo elevado'), ('identificador_divergente', 'Identificador divergente'), ('veiculo_sem_chip', 'Veículo sem chip'), ('chip_desvinculado', 'Chip desvinculado'), ('alteracao_vinculo', 'Alteração de vínculo'), ('veiculo_sem_atualizacao', 'Veículo sem atualização de conectividade')], max_length=30, unique=True)),
                ('gera_ocorrencia', models.BooleanField(default=True)),
                ('nivel', models.CharField(choices=[('info', 'Informativo'), ('atencao', 'Atenção'), ('critico', 'Crítico')], default='atencao', max_length=10)),
                ('envia_email', models.BooleanField(default=False)),
                ('destinatarios_emails', models.TextField(blank=True, default='', help_text='E-mails separados por vírgula, ponto e vírgula ou quebra de linha.')),
            ],
            options={
                'verbose_name': 'Regra de alerta',
                'verbose_name_plural': 'Regras de alerta',
                'ordering': ['tipo'],
            },
        ),
        migrations.CreateModel(
            name='FrotaTipo',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('nome', models.CharField(max_length=80, unique=True)),
                ('exige_conectividade', models.BooleanField(default=False, help_text='Veículo ativo deste tipo, sem chip vinculado, gera o alerta "Veículo sem chip".')),
                ('ativo', models.BooleanField(default=True)),
            ],
            options={
                'verbose_name': 'Tipo de veículo',
                'verbose_name_plural': 'Tipos de veículo',
                'ordering': ['nome'],
            },
        ),
        migrations.CreateModel(
            name='FrotaVeiculo',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('codigo', models.CharField(help_text='Código interno (frota/patrimônio). Único; gravado em caixa alta.', max_length=30, unique=True)),
                ('nome', models.CharField(max_length=120)),
                ('fabricante', models.CharField(blank=True, default='', max_length=80)),
                ('modelo', models.CharField(blank=True, default='', max_length=80)),
                ('ano_fabricacao', models.PositiveSmallIntegerField(blank=True, null=True)),
                ('placa', models.CharField(blank=True, default='', help_text='AAA9999 (antiga) ou AAA9A99 (Mercosul). Única quando preenchida.', max_length=8)),
                ('renavam', models.CharField(blank=True, default='', help_text='11 dígitos.', max_length=11)),
                ('chassi_serie', models.CharField(blank=True, default='', help_text='Único quando preenchido.', max_length=30, verbose_name='Chassi / nº de série')),
                ('setor', models.CharField(blank=True, default='', max_length=100)),
                ('area_operacao', models.CharField(blank=True, default='', max_length=100, verbose_name='Área de operação')),
                ('observacoes', models.TextField(blank=True, default='')),
                ('status', models.CharField(choices=[('ativo', 'Ativo'), ('manutencao', 'Em manutenção'), ('inativo', 'Inativo'), ('desativado', 'Desativado')], db_index=True, default='ativo', max_length=12)),
                ('exige_conectividade', models.BooleanField(blank=True, help_text='Vazio = herda do tipo. Sim/Não sobrepõe o padrão do tipo.', null=True)),
            ],
            options={
                'verbose_name': 'Veículo / equipamento',
                'verbose_name_plural': 'Veículos e equipamentos',
                'ordering': ['codigo'],
                'permissions': [('ver_frota', 'Pode ver os veículos e equipamentos da frota'), ('editar_frota', 'Pode cadastrar e editar veículos e equipamentos da frota')],
            },
        ),
        migrations.CreateModel(
            name='FrotaVinculo',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('inicio_em', models.DateTimeField(default=django.utils.timezone.now)),
                ('fim_em', models.DateTimeField(blank=True, null=True)),
                ('motivo_fim', models.CharField(blank=True, default='', max_length=255)),
            ],
            options={
                'verbose_name': 'Vínculo chip-veículo',
                'verbose_name_plural': 'Vínculos chip-veículo',
                'ordering': ['-inicio_em', '-pk'],
            },
        ),
        migrations.AlterModelOptions(
            name='m2mchip',
            options={'ordering': ['nome'], 'permissions': [('ver_chips', 'Pode ver os chips de conectividade'), ('importar_chips', 'Pode cadastrar chips (individualmente ou por planilha)'), ('vincular_chips', 'Pode vincular chips a veículos'), ('transferir_chips', 'Pode desvincular, transferir, substituir, devolver à disponibilidade e inativar chips'), ('consultar_conectividade', 'Pode ver sessão e consumo dos chips e atualizar a consulta'), ('ver_historico_frota', 'Pode ver o histórico de movimentações, os alertas e as ocorrências'), ('tratar_ocorrencias', 'Pode tratar as ocorrências de conectividade'), ('administrar_frota_config', 'Pode administrar a integração, os tipos de veículo, as operadoras e as regras de alerta')], 'verbose_name': 'Chip de conectividade', 'verbose_name_plural': 'Chips de conectividade'},
        ),
        migrations.AlterModelOptions(
            name='m2mocorrencia',
            options={'ordering': ['-detectada_em', '-pk'], 'verbose_name': 'Ocorrência de conectividade', 'verbose_name_plural': 'Ocorrências de conectividade'},
        ),
        migrations.RemoveConstraint(
            model_name='m2mocorrencia',
            name='m2m_ocorrencia_aberta_unica',
        ),
        # A operadora entra NULA, é preenchida com a TIM nos chips que já existem e só depois
        # fica obrigatória. O RunPython também registra em M2MEvento o equipamento (Item) de
        # cada chip que tinha um — ANTES de o campo `item` ser removido logo abaixo.
        migrations.AddField(
            model_name='m2mchip',
            name='operadora',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, related_name='chips', to='ProjetoEstoque.conectividadeoperadora'),
        ),
        migrations.RunPython(preencher_operadora_e_registrar_item, restaurar_item_dos_eventos),
        migrations.AlterField(
            model_name='m2mchip',
            name='operadora',
            field=models.ForeignKey(default=ProjetoEstoque.models.operadora_padrao_id, on_delete=django.db.models.deletion.PROTECT, related_name='chips', to='ProjetoEstoque.conectividadeoperadora'),
        ),
        migrations.RemoveField(
            model_name='m2mchip',
            name='item',
        ),
        migrations.AddField(
            model_name='m2mchip',
            name='numero_linha',
            field=models.CharField(blank=True, default='', help_text='Número da linha, só dígitos (com DDD). Opcional; não é único.', max_length=15),
        ),
        migrations.AddField(
            model_name='m2mchip',
            name='situacao_uso',
            field=models.CharField(choices=[('disponivel', 'Disponível'), ('vinculado', 'Vinculado'), ('manutencao', 'Em manutenção'), ('inativo', 'Inativo')], db_index=True, default='disponivel', help_text='Gravada só pelo serviço de vínculo. Não confundir com `monitorado` (consulta à API).', max_length=12),
        ),
        migrations.AlterField(
            model_name='m2mevento',
            name='tipo',
            field=models.CharField(choices=[('cadastro', 'Chip cadastrado'), ('edicao', 'Chip editado'), ('desativado', 'Monitoramento desativado'), ('reativado', 'Monitoramento reativado'), ('token_alterado', 'Token alterado'), ('config_alterada', 'Configuração alterada'), ('mapeamento_alterado', 'Mapeamento alterado'), ('teste_conexao', 'Teste de conexão'), ('estado_sessao_mudou', 'Estado da sessão mudou'), ('entrada', 'Entrada do chip'), ('vinculado', 'Chip vinculado a veículo'), ('desvinculado', 'Chip desvinculado'), ('transferido', 'Chip transferido de veículo'), ('substituido', 'Chip substituído'), ('retorno_disponibilidade', 'Chip devolvido à disponibilidade'), ('inativado', 'Chip inativado'), ('item_removido', 'Vínculo antigo com equipamento removido'), ('config_frota', 'Cadastro de apoio alterado')], max_length=25),
        ),
        migrations.AlterField(
            model_name='m2mocorrencia',
            name='chip',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='ocorrencias', to='ProjetoEstoque.m2mchip'),
        ),
        migrations.AlterField(
            model_name='m2mocorrencia',
            name='tipo',
            field=models.CharField(choices=[('falha_recorrente', 'Falhas de consulta seguidas'), ('sem_sessao', 'Sem sessão'), ('desatualizado', 'Dados desatualizados'), ('consumo_elevado', 'Consumo elevado'), ('identificador_divergente', 'Identificador divergente'), ('veiculo_sem_chip', 'Veículo sem chip'), ('chip_desvinculado', 'Chip desvinculado'), ('alteracao_vinculo', 'Alteração de vínculo'), ('veiculo_sem_atualizacao', 'Veículo sem atualização de conectividade')], max_length=30),
        ),
        migrations.AddField(
            model_name='frotaevento',
            name='usuario',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='frotaimportacaochips',
            name='confirmada_por',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='frotaimportacaochips',
            name='criada_por',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='frotaimportacaolinha',
            name='chip',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='ProjetoEstoque.m2mchip'),
        ),
        migrations.AddField(
            model_name='frotaimportacaolinha',
            name='importacao',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='itens', to='ProjetoEstoque.frotaimportacaochips'),
        ),
        migrations.AddField(
            model_name='frotamovimentacao',
            name='chip',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='movimentacoes', to='ProjetoEstoque.m2mchip'),
        ),
        migrations.AddField(
            model_name='frotamovimentacao',
            name='chip_relacionado',
            field=models.ForeignKey(blank=True, help_text='O outro chip da substituição.', null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='ProjetoEstoque.m2mchip'),
        ),
        migrations.AddField(
            model_name='frotamovimentacao',
            name='importacao',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='movimentacoes', to='ProjetoEstoque.frotaimportacaochips'),
        ),
        migrations.AddField(
            model_name='frotamovimentacao',
            name='usuario',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='frotaregraalerta',
            name='destinatarios_usuarios',
            field=models.ManyToManyField(blank=True, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='frotaveiculo',
            name='atualizado_por',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='%(class)s_atualizador', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='frotaveiculo',
            name='criado_por',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='%(class)s_criador', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='frotaveiculo',
            name='localidade',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='veiculos_frota', to='ProjetoEstoque.localidade', verbose_name='Unidade / fazenda'),
        ),
        migrations.AddField(
            model_name='frotaveiculo',
            name='responsavel',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='veiculos_frota', to='ProjetoEstoque.usuario', verbose_name='Responsável'),
        ),
        migrations.AddField(
            model_name='frotaveiculo',
            name='tipo',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='veiculos', to='ProjetoEstoque.frotatipo'),
        ),
        migrations.AddField(
            model_name='frotamovimentacao',
            name='veiculo_destino',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='movimentacoes_de_entrada', to='ProjetoEstoque.frotaveiculo'),
        ),
        migrations.AddField(
            model_name='frotamovimentacao',
            name='veiculo_origem',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='movimentacoes_de_saida', to='ProjetoEstoque.frotaveiculo'),
        ),
        migrations.AddField(
            model_name='frotaevento',
            name='veiculo',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='eventos', to='ProjetoEstoque.frotaveiculo'),
        ),
        migrations.AddField(
            model_name='m2mocorrencia',
            name='veiculo',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='ocorrencias', to='ProjetoEstoque.frotaveiculo'),
        ),
        migrations.AddConstraint(
            model_name='m2mocorrencia',
            constraint=models.UniqueConstraint(condition=models.Q(('chip__isnull', False), ('estado__in', ['aberta', 'em_tratamento'])), fields=('chip', 'tipo'), name='m2m_ocorrencia_aberta_unica'),
        ),
        migrations.AddConstraint(
            model_name='m2mocorrencia',
            constraint=models.UniqueConstraint(condition=models.Q(('chip__isnull', True), ('estado__in', ['aberta', 'em_tratamento']), ('veiculo__isnull', False)), fields=('veiculo', 'tipo'), name='m2m_ocorrencia_veiculo_aberta_unica'),
        ),
        migrations.AddConstraint(
            model_name='m2mocorrencia',
            constraint=models.CheckConstraint(condition=models.Q(('chip__isnull', False), ('veiculo__isnull', False), _connector='OR'), name='m2m_ocorrencia_tem_alvo'),
        ),
        migrations.AddField(
            model_name='frotavinculo',
            name='chip',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='vinculos', to='ProjetoEstoque.m2mchip'),
        ),
        migrations.AddField(
            model_name='frotavinculo',
            name='fim_por',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='frotavinculo',
            name='inicio_por',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='frotavinculo',
            name='mov_fim',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='ProjetoEstoque.frotamovimentacao'),
        ),
        migrations.AddField(
            model_name='frotavinculo',
            name='mov_inicio',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='ProjetoEstoque.frotamovimentacao'),
        ),
        migrations.AddField(
            model_name='frotavinculo',
            name='veiculo',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='vinculos', to='ProjetoEstoque.frotaveiculo'),
        ),
        migrations.AddIndex(
            model_name='frotaimportacaolinha',
            index=models.Index(fields=['importacao', 'situacao'], name='frota_imp_linha_sit_idx'),
        ),
        migrations.AddConstraint(
            model_name='frotaveiculo',
            constraint=models.UniqueConstraint(condition=models.Q(('placa', ''), _negated=True), fields=('placa',), name='frota_veiculo_placa_unica'),
        ),
        migrations.AddConstraint(
            model_name='frotaveiculo',
            constraint=models.UniqueConstraint(condition=models.Q(('chassi_serie', ''), _negated=True), fields=('chassi_serie',), name='frota_veiculo_chassi_unico'),
        ),
        migrations.AddIndex(
            model_name='frotamovimentacao',
            index=models.Index(fields=['chip', '-em'], name='frota_mov_chip_idx'),
        ),
        migrations.AddIndex(
            model_name='frotaevento',
            index=models.Index(fields=['veiculo', '-em'], name='frota_evento_veiculo_idx'),
        ),
        migrations.AddIndex(
            model_name='frotavinculo',
            index=models.Index(fields=['veiculo', 'fim_em'], name='frota_vinculo_veiculo_idx'),
        ),
        migrations.AddIndex(
            model_name='frotavinculo',
            index=models.Index(fields=['chip', '-inicio_em'], name='frota_vinculo_chip_idx'),
        ),
        migrations.AddConstraint(
            model_name='frotavinculo',
            constraint=models.UniqueConstraint(condition=models.Q(('fim_em__isnull', True)), fields=('chip',), name='frota_vinculo_chip_ativo_unico'),
        ),
        migrations.AddConstraint(
            model_name='frotavinculo',
            constraint=models.CheckConstraint(condition=models.Q(('fim_em__isnull', True), ('fim_em__gte', models.F('inicio_em')), _connector='OR'), name='frota_vinculo_fim_depois_do_inicio'),
        ),
    ]
