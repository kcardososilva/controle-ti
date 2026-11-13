"""
Cria o grupo "Monitoramento M2M" e garante as três permissões do módulo.

Mesmo padrão da 0181 (grupos de acesso restrito): o grupo precisa EXISTIR antes
de alguém poder ser colocado nele.

As permissões nascem de `M2MChip.Meta.permissions`, mas quem as cria é o sinal
`post_migrate` — que só roda DEPOIS de todas as migrations. Como esta data
migration roda antes dele, ela cria o ContentType e as permissões por conta
própria (`get_or_create`, idempotente): o `post_migrate` depois encontra tudo
pronto e não duplica nada.

O grupo recebe `ver_m2m` e `operar_m2m`. `configurar_m2m` (token, frequência e
mapeamento da resposta) fica DE FORA de propósito: é permissão explícita, dada a
quem de fato mexe na credencial; superusuário tem todas.

O reverse remove o grupo e as três permissões (com elas, o vínculo de quem as
tinha): deixar permissão órfã de um módulo que a versão anterior do código
desconhece só confundiria quem auditasse os acessos.
"""
from django.db import migrations

GRUPO = "Monitoramento M2M"
APP = "ProjetoEstoque"
MODELO = "m2mchip"
PERMISSOES = (
    ("ver_m2m", "Pode ver o monitoramento TIM M2M"),
    ("operar_m2m", "Pode operar o monitoramento TIM M2M (cadastrar chips, atualizar, tratar alertas)"),
    ("configurar_m2m", "Pode configurar a integração TIM M2M (token, frequência, mapeamento)"),
)
NO_GRUPO = ("ver_m2m", "operar_m2m")


def criar(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    tipo, _ = ContentType.objects.get_or_create(app_label=APP, model=MODELO)
    por_codigo = {}
    for codigo, nome in PERMISSOES:
        por_codigo[codigo], _ = Permission.objects.get_or_create(
            content_type=tipo, codename=codigo, defaults={"name": nome},
        )
    grupo, _ = Group.objects.get_or_create(name=GRUPO)
    grupo.permissions.add(*(por_codigo[c] for c in NO_GRUPO))


def remover(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    Group.objects.filter(name=GRUPO).delete()
    Permission.objects.filter(
        content_type__app_label=APP, content_type__model=MODELO,
        codename__in=[c for c, _ in PERMISSOES],
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("ProjetoEstoque", "0188_m2m_modulo"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(criar, remover),
    ]
