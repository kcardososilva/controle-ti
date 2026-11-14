"""
Portal do Colaborador (Zelo Agro) — o grupo "Zelo Agro — Colaborador" com as permissões de USO do módulo.

Só dados (nenhuma tabela muda). O grupo prende o login no Agro (`ambiente_service.GRUPOS_SOMENTE_AGRO`) e leva as
permissões de ver, movimentar e cadastrar — e NENHUMA de API (`consultar_conectividade`, `administrar_frota_config`,
`tratar_ocorrencias`), nem as de administrar (configuração, catálogo, enlaces) ou de importar planilha em lote. As
permissões são criadas AQUI (`get_or_create`) e não só pelo `post_migrate`, porque o grupo precisa delas já nesta migration
— o mesmo padrão das 0191, 0192 e 0194.

A lista é o RETRATO de `services/colaborador_agro_service.PERMISSOES` (migration não importa service), e
`tests/test_colaborador_agro.py` falha se as duas divergirem. Aditiva e idempotente: rodar de novo não duplica nada.
Reversa: apaga o grupo (as permissões ficam; pertencem aos models).

Produção: `migrate`. Depois, a pessoa é liberada em /acessos/restritos/ (tela de administrador).
"""

from django.db import migrations

APP = "ProjetoEstoque"
GRUPO = "Zelo Agro — Colaborador"

# (model dono da permissão, codename, nome) — o dono é o mesmo de `Meta.permissions`, senão o `post_migrate` criaria uma
# segunda linha com o mesmo codename.
PERMISSOES = (
    ("preferenciaambiente", "acessar_zelo_agro", "Pode acessar o ambiente Zelo Agro (visão geral)"),
    ("frotaveiculo", "ver_frota", "Pode ver os veículos e equipamentos da frota"),
    ("frotaveiculo", "editar_frota", "Pode cadastrar e editar veículos e equipamentos da frota"),
    ("frotaveiculo", "exportar_frota", "Pode exportar a frota e emitir os relatórios de frotas e maquinários"),
    ("m2mchip", "ver_chips", "Pode ver os chips de conectividade"),
    ("m2mchip", "importar_chips", "Pode cadastrar chips (individualmente ou por planilha)"),
    ("m2mchip", "vincular_chips", "Pode vincular chips a veículos"),
    ("m2mchip", "transferir_chips", "Pode desvincular, transferir, substituir, devolver à disponibilidade e inativar chips"),
    ("m2mchip", "ver_historico_frota", "Pode ver o histórico de movimentações, os alertas e as ocorrências"),
    ("tecequipamento", "ver_tec_estoque", "Pode ver o estoque tecnológico (equipamentos, materiais e catálogo)"),
    ("tecequipamento", "cadastrar_tec_equipamento", "Pode cadastrar e editar equipamentos e materiais"),
    ("tecequipamento", "instalar_tec_equipamento", "Pode instalar, retirar, substituir e desvincular equipamentos"),
    ("tecequipamento", "transferir_tec_equipamento", "Pode transferir equipamentos entre máquinas, estruturas e pessoas"),
    ("tecequipamento", "ver_tec_historico", "Pode consultar o histórico e os relatórios da tecnologia agrícola"),
    ("tecequipamento", "abrir_tec_manutencao", "Pode abrir e acompanhar manutenções de equipamentos"),
    ("tecequipamento", "gerenciar_tec_estrutura", "Pode gerenciar torres e estações"),
)


def criar_grupo(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")
    Group = apps.get_model("auth", "Group")
    grupo, _ = Group.objects.get_or_create(name=GRUPO)
    for modelo, codename, nome in PERMISSOES:
        tipo, _ = ContentType.objects.get_or_create(app_label=APP, model=modelo)
        permissao, _ = Permission.objects.get_or_create(content_type=tipo, codename=codename, defaults={"name": nome})
        grupo.permissions.add(permissao)


def remover_grupo(apps, schema_editor):
    apps.get_model("auth", "Group").objects.filter(name=GRUPO).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("ProjetoEstoque", "0208_ninja_licenca_liberada"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(criar_grupo, remover_grupo),
    ]
