"""
Frotas e Conectividade — dados e permissões (Fase D).

Esta migration NÃO muda o esquema (isso foi a 0190). Ela:

  1. semeia os 7 tipos de veículo (Trator, Caminhão, Pulverizador, Colheitadeira,
     Plantadeira, Veículo utilitário, Outros) — todos com `exige_conectividade=False`:
     quem decide que um tipo exige chip é o TI, depois de cadastrar a frota (ligar
     por padrão abriria um alerta por veículo cadastrado sem chip);
  2. semeia uma regra de alerta por tipo de ocorrência (`FrotaRegraAlerta`), levando
     para as regras a lista de destinatários que o canal `m2m_ocorrencias` tinha
     customizada (o canal passa a `dinamico`: quem recebe é a regra);
  3. cria as 10 permissões novas (mesmo padrão da 0189: o `post_migrate` só roda depois
     de todas as migrations, então ContentType e Permission são criados aqui, com
     `get_or_create`, e ele depois encontra tudo pronto);
  4. MAPEIA as 3 permissões antigas (`ver_m2m`, `operar_m2m`, `configurar_m2m`) para as
     novas — em GRUPOS e em USUÁRIOS (`user_permissions`):

         ver        -> ver_frota, ver_chips, consultar_conectividade, ver_historico_frota
         operar     -> as de `ver` + editar_frota, importar_chips, vincular_chips,
                       transferir_chips, tratar_ocorrencias
         configurar -> administrar_frota_config

  5. renomeia o grupo "Monitoramento M2M" para "Frotas e Conectividade" (as 9 permissões,
     todas menos `administrar_frota_config`, que é explícita) e cria "Frotas — Consulta"
     (as 4 de leitura). Se o grupo antigo não existir, cria o novo; se os dois existirem,
     junta;
  6. apaga as 3 permissões antigas (com elas, o vínculo de quem as tinha — já mapeado).

Os nomes dos grupos e os códigos de permissão estão repetidos aqui de propósito:
migration não importa model (o `models.py` muda, a migration é um retrato do dia). Um
teste (`test_frota.PermissoesTests`) confere que os dois lados não divergem.

Reversível: recria as 3 antigas, mapeia de volta (quem tem qualquer uma das de operação
volta a ter `operar_m2m`; `administrar_frota_config` volta a `configurar_m2m`; o resto de
leitura a `ver_m2m`), devolve o nome do grupo e remove as 10 novas e o grupo de consulta.
As linhas de tipos e regras ficam (a 0190 as derruba junto com as tabelas se for
revertida também); o canal `m2m_ocorrencias` volta a `fixo`.
"""
from django.db import migrations

APP = "ProjetoEstoque"

GRUPO_ANTIGO = "Monitoramento M2M"
GRUPO_FROTAS = "Frotas e Conectividade"
GRUPO_FROTAS_CONSULTA = "Frotas — Consulta"

ANTIGAS = (
    ("ver_m2m", "Pode ver o monitoramento TIM M2M"),
    ("operar_m2m", "Pode operar o monitoramento TIM M2M (cadastrar chips, atualizar, tratar alertas)"),
    ("configurar_m2m", "Pode configurar a integração TIM M2M (token, frequência, mapeamento)"),
)

# (modelo, codename, nome)
NOVAS = (
    ("frotaveiculo", "ver_frota", "Pode ver os veículos e equipamentos da frota"),
    ("frotaveiculo", "editar_frota", "Pode cadastrar e editar veículos e equipamentos da frota"),
    ("m2mchip", "ver_chips", "Pode ver os chips de conectividade"),
    ("m2mchip", "importar_chips", "Pode cadastrar chips (individualmente ou por planilha)"),
    ("m2mchip", "vincular_chips", "Pode vincular chips a veículos"),
    ("m2mchip", "transferir_chips", "Pode desvincular, transferir, substituir, devolver à disponibilidade e inativar chips"),
    ("m2mchip", "consultar_conectividade", "Pode ver sessão e consumo dos chips e atualizar a consulta"),
    ("m2mchip", "ver_historico_frota", "Pode ver o histórico de movimentações, os alertas e as ocorrências"),
    ("m2mchip", "tratar_ocorrencias", "Pode tratar as ocorrências de conectividade"),
    ("m2mchip", "administrar_frota_config", "Pode administrar a integração, os tipos de veículo, as operadoras e as regras de alerta"),
)

VER = ("ver_frota", "ver_chips", "consultar_conectividade", "ver_historico_frota")
OPERAR_EXTRA = ("editar_frota", "importar_chips", "vincular_chips", "transferir_chips", "tratar_ocorrencias")
CONFIGURAR = ("administrar_frota_config",)

# antiga -> novas
MAPA = {
    "ver_m2m": VER,
    "operar_m2m": VER + OPERAR_EXTRA,
    "configurar_m2m": CONFIGURAR,
}
NO_GRUPO_FROTAS = VER + OPERAR_EXTRA          # 9: tudo, menos administrar_frota_config
NO_GRUPO_CONSULTA = VER

TIPOS = ("Trator", "Caminhão", "Pulverizador", "Colheitadeira", "Plantadeira", "Veículo utilitário", "Outros")

# tipo -> (gera_ocorrencia, nivel, envia_email). Os dois eventos do vínculo nascem
# DESLIGADOS: toda movimentação geraria uma ocorrência; o TI liga se quiser acompanhar.
REGRAS = {
    "falha_recorrente": (True, "atencao", True),
    "sem_sessao": (True, "atencao", True),
    "desatualizado": (True, "atencao", True),
    "consumo_elevado": (True, "atencao", True),
    "identificador_divergente": (True, "atencao", True),
    "veiculo_sem_chip": (True, "atencao", True),
    "veiculo_sem_atualizacao": (True, "atencao", True),
    "chip_desvinculado": (False, "info", False),
    "alteracao_vinculo": (False, "info", False),
}
CANAL = "m2m_ocorrencias"


# ─────────────────────────────────────────────────────────────
# Dados
# ─────────────────────────────────────────────────────────────

def semear_tipos(apps, schema_editor):
    Tipo = apps.get_model(APP, "FrotaTipo")
    for nome in TIPOS:
        Tipo.objects.get_or_create(nome=nome, defaults={"exige_conectividade": False, "ativo": True})


def semear_regras(apps, schema_editor):
    Regra = apps.get_model(APP, "FrotaRegraAlerta")
    Canal = apps.get_model(APP, "CanalNotificacao")

    canal = Canal.objects.filter(codigo=CANAL).first()
    herdados = ""
    if canal is not None:
        if canal.destinatarios_customizados_ativo:
            herdados = canal.destinatarios_customizados or ""
        # O canal passa a ser `dinamico`: a lista de destinatários mora nas regras.
        Canal.objects.filter(pk=canal.pk).update(
            tipo_destinatarios="dinamico", destino_gerenciado_em="Frotas e Conectividade › Configuração",
        )
    for tipo, (gera, nivel, email) in REGRAS.items():
        Regra.objects.get_or_create(
            tipo=tipo,
            defaults={
                "gera_ocorrencia": gera, "nivel": nivel, "envia_email": email,
                "destinatarios_emails": herdados if email else "",
            },
        )


def desfazer_regras(apps, schema_editor):
    """O canal volta a ser lista fixa (a que a 0189 deixou)."""
    Canal = apps.get_model(APP, "CanalNotificacao")
    Canal.objects.filter(codigo=CANAL).update(tipo_destinatarios="fixo", destino_gerenciado_em="")


# ─────────────────────────────────────────────────────────────
# Permissões e grupos
# ─────────────────────────────────────────────────────────────

def _criar_novas(apps):
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    novas = {}
    for modelo, codigo, nome in NOVAS:
        tipo, _ = ContentType.objects.get_or_create(app_label=APP, model=modelo)
        novas[codigo], _ = Permission.objects.get_or_create(content_type=tipo, codename=codigo, defaults={"name": nome})
    return novas


def _antigas_existentes(apps):
    Permission = apps.get_model("auth", "Permission")
    return {
        p.codename: p
        for p in Permission.objects.filter(
            content_type__app_label=APP, content_type__model="m2mchip", codename__in=[c for c, _ in ANTIGAS],
        )
    }


def migrar_permissoes(apps, schema_editor):
    """Cria as novas, mapeia as antigas (grupos e usuários), acerta os dois grupos e
    apaga as antigas. Idempotente."""
    Group = apps.get_model("auth", "Group")
    User = apps.get_model("auth", "User")

    novas = _criar_novas(apps)
    antigas = _antigas_existentes(apps)

    def novas_de(codigos_antigos):
        saida = set()
        for codigo in codigos_antigos:
            saida.update(MAPA.get(codigo, ()))
        return [novas[c] for c in sorted(saida)]

    for grupo in Group.objects.filter(permissions__in=list(antigas.values())).distinct():
        tinha = [p.codename for p in grupo.permissions.all() if p.codename in antigas]
        grupo.permissions.add(*novas_de(tinha))
    for usuario in User.objects.filter(user_permissions__in=list(antigas.values())).distinct():
        tinha = [p.codename for p in usuario.user_permissions.all() if p.codename in antigas]
        usuario.user_permissions.add(*novas_de(tinha))

    # grupo "Frotas e Conectividade": renomeia o antigo (ou junta, ou cria)
    antigo = Group.objects.filter(name=GRUPO_ANTIGO).first()
    atual = Group.objects.filter(name=GRUPO_FROTAS).first()
    if antigo is not None and atual is None:
        antigo.name = GRUPO_FROTAS
        antigo.save()
        atual = antigo
    elif antigo is not None and atual is not None:
        for usuario in antigo.user_set.all():
            usuario.groups.add(atual)
        antigo.delete()
    elif atual is None:
        atual = Group.objects.create(name=GRUPO_FROTAS)
    atual.permissions.add(*(novas[c] for c in NO_GRUPO_FROTAS))

    consulta, _ = Group.objects.get_or_create(name=GRUPO_FROTAS_CONSULTA)
    consulta.permissions.add(*(novas[c] for c in NO_GRUPO_CONSULTA))

    # as antigas saem (com elas, o vínculo de quem as tinha — já mapeado acima)
    for permissao in antigas.values():
        permissao.delete()


def desfazer_permissoes(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    User = apps.get_model("auth", "User")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    tipo, _ = ContentType.objects.get_or_create(app_label=APP, model="m2mchip")
    antigas = {
        codigo: Permission.objects.get_or_create(content_type=tipo, codename=codigo, defaults={"name": nome})[0]
        for codigo, nome in ANTIGAS
    }
    codigos_novos = [c for _, c, _ in NOVAS]

    def antigas_de(codigos):
        codigos = set(codigos)
        saida = []
        if codigos & set(CONFIGURAR):
            saida.append("configurar_m2m")
        if codigos & set(OPERAR_EXTRA):
            saida.append("operar_m2m")
        if codigos & set(VER):
            saida.append("ver_m2m")
        return [antigas[c] for c in saida]

    for grupo in Group.objects.filter(permissions__codename__in=codigos_novos).distinct():
        grupo.permissions.add(*antigas_de(p.codename for p in grupo.permissions.all()))
    for usuario in User.objects.filter(user_permissions__codename__in=codigos_novos).distinct():
        usuario.user_permissions.add(*antigas_de(p.codename for p in usuario.user_permissions.all()))

    consulta = Group.objects.filter(name=GRUPO_FROTAS_CONSULTA).first()
    if consulta is not None:
        for usuario in consulta.user_set.all():          # quem só tinha o grupo de consulta não fica sem nada
            usuario.user_permissions.add(antigas["ver_m2m"])
        consulta.delete()
    atual = Group.objects.filter(name=GRUPO_FROTAS).first()
    if atual is not None and not Group.objects.filter(name=GRUPO_ANTIGO).exists():
        atual.name = GRUPO_ANTIGO
        atual.save()

    Permission.objects.filter(content_type__app_label=APP, codename__in=codigos_novos).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("ProjetoEstoque", "0190_frota_conectividade"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(semear_tipos, migrations.RunPython.noop),
        migrations.RunPython(semear_regras, desfazer_regras),
        migrations.RunPython(migrar_permissoes, desfazer_permissoes),
    ]
