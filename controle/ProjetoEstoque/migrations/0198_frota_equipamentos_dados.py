"""
Frotas e Maquinários — dados: as 3 permissões do inventário e o que os tipos esperam ter.

Esta migration NÃO muda o esquema (isso foi a 0197). Ela:

  1. cria as 3 permissões novas (`importar_equipamentos_frota`, `validar_inventario_frota`,
     `exportar_frota`), AQUI e não só pelo `post_migrate` — o grupo abaixo precisa delas já nesta
     migration (mesmo padrão da 0191/0194);
  2. coloca as 3 no grupo "Frotas e Conectividade", o que opera a frota. O grupo de consulta
     ("Frotas — Consulta") fica como está: importar, validar o inventário e exportar não são
     leitura. Quem já tinha o grupo passa a poder usar o que antes não existia — nenhuma permissão
     que existia é tirada de ninguém;
  3. marca que os tipos "Caminhão" e "Veículo utilitário" ESPERAM placa e chassi (`espera_placa`,
     `espera_chassi`): é a regra que faz "sem placa" ser pendência num caminhão e só um fato num
     gerador. Só toca em tipo que ainda está no padrão (as colunas acabaram de nascer, então nenhum
     está editado), e nunca desliga nada. Tipo renomeado ou apagado em produção simplesmente não
     casa, sem erro.

Os nomes dos grupos e os códigos estão repetidos aqui de propósito: migration é o retrato do dia e
não importa model. Aditiva e idempotente (`get_or_create`). Reversa: remove as 3 permissões (a
relação com o grupo some junto) e desliga as marcas dos tipos.
"""

from django.db import migrations

APP = "ProjetoEstoque"
GRUPO_FROTAS = "Frotas e Conectividade"

PERMISSOES = (
    ("importar_equipamentos_frota", "Pode importar equipamentos motorizados por planilha"),
    ("validar_inventario_frota", "Pode validar o inventário: a situação e o cadastro dos equipamentos importados"),
    ("exportar_frota", "Pode exportar a frota e emitir os relatórios de frotas e maquinários"),
)

TIPOS_EMPLACADOS = ("Caminhão", "Veículo utilitário")


def criar(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")
    Group = apps.get_model("auth", "Group")
    tipo, _ = ContentType.objects.get_or_create(app_label=APP, model="frotaveiculo")
    criadas = [
        Permission.objects.get_or_create(content_type=tipo, codename=codigo, defaults={"name": nome})[0]
        for codigo, nome in PERMISSOES
    ]
    grupo, _ = Group.objects.get_or_create(name=GRUPO_FROTAS)
    grupo.permissions.add(*criadas)

    apps.get_model(APP, "FrotaTipo").objects.filter(
        nome__in=TIPOS_EMPLACADOS, espera_placa=False, espera_chassi=False,
    ).update(espera_placa=True, espera_chassi=True)


def remover(apps, schema_editor):
    apps.get_model("auth", "Permission").objects.filter(
        content_type__app_label=APP, codename__in=[c for c, _ in PERMISSOES],
    ).delete()
    apps.get_model(APP, "FrotaTipo").objects.filter(nome__in=TIPOS_EMPLACADOS).update(espera_placa=False, espera_chassi=False)


class Migration(migrations.Migration):

    dependencies = [
        ("ProjetoEstoque", "0197_frota_equipamentos"),
    ]

    operations = [
        migrations.RunPython(criar, remover),
    ]
