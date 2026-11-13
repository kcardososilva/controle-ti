"""
Tecnologia agrícola do Zelo Agro — dados: permissões, grupos e o que o usuário NOMEOU.

  1. As 13 permissões (`PERMISSOES_TEC`), criadas AQUI e não só pelo `post_migrate`,
     porque os grupos abaixo precisam delas já nesta migration (mesmo padrão da 0191/0192).
  2. Dois grupos: "Tecnologia Agrícola — Gestão" (tudo, menos a administração de locais e
     unidades) e "Tecnologia Agrícola — Consulta" (só leitura e histórico).
  3. Sementes: os QUATRO fabricantes que a spec nomeia (Solinftec, John Deere, Stara,
     Motorola), as unidades de medida comuns e as categorias que a spec lista. NENHUM
     modelo: G5, StarFire, JDLink… são exemplos do texto, não cadastro — o catálogo é o que
     a empresa realmente usa, e quem cadastra é o Agro.

Aditiva e idempotente (`get_or_create`): rodar de novo não duplica nem sobrescreve o que o
usuário editou. Os nomes e codenames estão repetidos aqui de propósito: migration não
importa model. Reversa: só remove os grupos (permissões e sementes somem com as tabelas, na
reversa da 0193).
"""

from django.db import migrations

APP = "ProjetoEstoque"
GRUPO_GESTAO = "Tecnologia Agrícola — Gestão"
GRUPO_CONSULTA = "Tecnologia Agrícola — Consulta"

PERMISSOES = [
    ("ver_tec_estoque", "Pode ver o estoque tecnológico (equipamentos, materiais e catálogo)"),
    ("cadastrar_tec_equipamento", "Pode cadastrar e editar equipamentos e materiais"),
    ("importar_tec_material", "Pode importar equipamentos e materiais por planilha"),
    ("administrar_tec_catalogo", "Pode administrar fabricantes, categorias, modelos e fornecedores"),
    ("instalar_tec_equipamento", "Pode instalar, retirar, substituir e desvincular equipamentos"),
    ("transferir_tec_equipamento", "Pode transferir equipamentos entre máquinas, estruturas e pessoas"),
    ("administrar_tec_enlace", "Pode administrar os enlaces ponto a ponto"),
    ("gerenciar_tec_estrutura", "Pode gerenciar torres e estações"),
    ("gerenciar_tec_radio", "Pode gerenciar a radiocomunicação"),
    ("ver_tec_historico", "Pode consultar o histórico e os relatórios da tecnologia agrícola"),
    ("abrir_tec_manutencao", "Pode abrir e acompanhar manutenções de equipamentos"),
    ("concluir_tec_manutencao", "Pode concluir ou cancelar manutenções de equipamentos"),
    ("administrar_tec_config", "Pode administrar os locais de armazenamento e as unidades de medida"),
]
SO_LEITURA = {"ver_tec_estoque", "ver_tec_historico"}
SO_ADMINISTRADOR = {"administrar_tec_config"}

FABRICANTES = ["Solinftec", "John Deere", "Stara", "Motorola"]

# sigla, nome, fracionável
UNIDADES = [
    ("un", "Unidade", False),
    ("par", "Par", False),
    ("kit", "Kit", False),
    ("cx", "Caixa", False),
    ("rolo", "Rolo", False),
    ("m", "Metro", True),
]

# nome, grupo, controle padrão — SÓ o que a spec lista (partes 2, 3, 4 e 7)
INDIVIDUAL, QUANTIDADE = "individual", "quantidade"
CATEGORIAS = [
    ("Monitor / display", "agricultura_precisao", INDIVIDUAL),
    ("Receptor GPS / GNSS", "agricultura_precisao", INDIVIDUAL),
    ("Controlador eletrônico", "agricultura_precisao", INDIVIDUAL),
    ("Componente RTK", "agricultura_precisao", INDIVIDUAL),
    ("Módulo embarcado", "agricultura_precisao", INDIVIDUAL),
    ("Modem / módulo de comunicação", "conectividade", INDIVIDUAL),
    ("Sensor", "sensores", INDIVIDUAL),
    ("Rádio portátil (HT)", "radiocomunicacao", INDIVIDUAL),
    ("Rádio móvel", "radiocomunicacao", INDIVIDUAL),
    ("Rádio base", "radiocomunicacao", INDIVIDUAL),
    ("Repetidora", "radiocomunicacao", INDIVIDUAL),
    ("Antena de radiocomunicação", "radiocomunicacao", INDIVIDUAL),
    ("Microfone", "radiocomunicacao", INDIVIDUAL),
    ("Acessório de radiocomunicação", "radiocomunicacao", QUANTIDADE),
    ("Antena direcional", "rede", INDIVIDUAL),
    ("Antena omnidirecional", "rede", INDIVIDUAL),
    ("Rádio de enlace (PTP)", "rede", INDIVIDUAL),
    ("Access point", "rede", INDIVIDUAL),
    ("Roteador", "rede", INDIVIDUAL),
    ("Switch", "rede", INDIVIDUAL),
    ("Nobreak", "energia", INDIVIDUAL),
    ("Fonte de alimentação", "energia", QUANTIDADE),
    ("Injetor PoE", "energia", QUANTIDADE),
    ("Bateria", "energia", QUANTIDADE),
    ("Carregador", "energia", QUANTIDADE),
    ("Cabo", "cabeamento", QUANTIDADE),
    ("Conector", "cabeamento", QUANTIDADE),
    ("Adaptador", "cabeamento", QUANTIDADE),
    ("Chicote", "cabeamento", QUANTIDADE),
    ("Acessório de instalação", "cabeamento", QUANTIDADE),
    ("Componente de reposição", "cabeamento", QUANTIDADE),
    ("Outro equipamento", "outro", INDIVIDUAL),
]


def criar(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")
    Group = apps.get_model("auth", "Group")
    tipo, _ = ContentType.objects.get_or_create(app_label=APP, model="tecequipamento")
    permissoes = {}
    for codigo, nome in PERMISSOES:
        permissoes[codigo], _ = Permission.objects.get_or_create(content_type=tipo, codename=codigo, defaults={"name": nome})
    gestao, _ = Group.objects.get_or_create(name=GRUPO_GESTAO)
    gestao.permissions.add(*[p for c, p in permissoes.items() if c not in SO_ADMINISTRADOR])
    consulta, _ = Group.objects.get_or_create(name=GRUPO_CONSULTA)
    consulta.permissions.add(*[p for c, p in permissoes.items() if c in SO_LEITURA])

    Fabricante = apps.get_model(APP, "TecFabricante")
    for nome in FABRICANTES:
        Fabricante.objects.get_or_create(nome=nome)
    Unidade = apps.get_model(APP, "AgroUnidadeMedida")
    for sigla, nome, fracionavel in UNIDADES:
        Unidade.objects.get_or_create(sigla=sigla, defaults={"nome": nome, "fracionavel": fracionavel})
    Categoria = apps.get_model(APP, "TecCategoria")
    for nome, grupo, controle in CATEGORIAS:
        Categoria.objects.get_or_create(nome=nome, defaults={"grupo": grupo, "controle_padrao": controle})


def remover(apps, schema_editor):
    apps.get_model("auth", "Group").objects.filter(name__in=[GRUPO_GESTAO, GRUPO_CONSULTA]).delete()


class Migration(migrations.Migration):

    dependencies = [
        (APP, "0193_tec_catalogo_estoque"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(criar, remover),
    ]
