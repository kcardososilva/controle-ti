"""
Ambientes do Zelo — **Zelo TI** e **Zelo Agro** — numa plataforma só.

Mesmo login, mesma sessão, dois ambientes de gestão independentes, cada um com
a própria navegação, a própria cor de identidade e as próprias permissões.
Este módulo é a régua ÚNICA de três perguntas, no mesmo molde da tabela
`PERFIS` de `acesso_restrito_service`:

  1. *Em que ambiente esta tela está?* — `ambiente_do_caminho`. **A rota decide**,
     não uma escolha guardada: é isso que impede abrir uma tela do Agro no
     visual do TI (ou o contrário) por um link colado, um favorito ou uma aba
     velha. A preferência só escolhe para onde ir quando a rota não diz
     (`/`, depois do login, telas neutras como o perfil).
  2. *Esta pessoa pode estar neste ambiente?* — `pode_acessar`, lida pelo
     `AmbienteMiddleware` (caminho) e pelo seletor da barra superior (botão).
  3. *O que o menu lateral mostra?* — `nav_de`, já filtrado por permissão.

Quem alcança cada ambiente:

  • **Zelo TI** é o padrão de todo usuário interno — exatamente como era antes
    de existir o Agro. Não é uma permissão a conceder: um login criado amanhã no
    Django Admin continua entrando no TI. A exceção são os grupos de
    `GRUPOS_SOMENTE_AGRO` ("Zelo Agro — Exclusivo" e "Zelo Agro — Colaborador", este
    o do Portal do Colaborador), que prendem a pessoa no Agro.
    **Revogar um desses grupos devolve o TI à pessoa** (o TI é o padrão): é por isso
    que `colaborador_agro_service.revogar` desativa o login em vez de só tirar o grupo.
  • **Zelo Agro** exige `acessar_zelo_agro` OU qualquer permissão de módulo do
    Agro. Permissão de módulo sem o ambiente não teria onde ser usada, então ela
    já implica o ambiente; `acessar_zelo_agro` sozinha é o "só a visão geral".

**Trocar de ambiente nunca concede nada**: o seletor é um link para a tela
inicial do outro ambiente; quem autoriza é o middleware (caminho) e o decorador
de cada view (ação). Superusuário passa por cima, como em todo o sistema.
Perfis externos e restritos (Fornecedor, Parceiro de Licenças, TV, Portal de
Contratações, Consulta Quiosque) não são assunto daqui: os middlewares deles
rodam antes e já os prendem na área de cada um.
"""
from django.urls import NoReverseMatch, reverse

from ProjetoEstoque.middleware import _grupos_do_usuario
from ProjetoEstoque.models import (
    GRUPO_COLABORADOR_AGRO,
    GRUPO_CONSULTA_QUIOSQUE,
    GRUPO_FORNECEDOR,
    GRUPO_PARCEIRO_LICENCA,
    GRUPO_PORTAL_CONTRATACAO,
    GRUPO_SOMENTE_AGRO,
    PERMISSOES_AGRO_AMBIENTE,
    PERMISSOES_FROTA_CHIP,
    PERMISSOES_FROTA_INVENTARIO,
    PERMISSOES_FROTA_VEICULO,
    PERMISSOES_TEC,
)

TI = "ti"
AGRO = "agro"

# Os grupos que prendem o login no Agro: o "Exclusivo" (só as permissões que a pessoa recebe à parte) e o do Portal do
# Colaborador (`colaborador_agro_service`, com as permissões de uso do módulo já dentro). Para o ambiente os dois são iguais.
GRUPOS_SOMENTE_AGRO = frozenset({GRUPO_SOMENTE_AGRO, GRUPO_COLABORADOR_AGRO})

# O que é "API" no Agro: tudo que vem da integração com a operadora (TIM/Saitro) — sessão, consumo, consultas, sincronização,
# alertas de conectividade e a configuração da integração. É UMA régua para o módulo inteiro: a lista de chips, o detalhe, o
# mapa, os painéis e o menu perguntam `pode_ver_api`, e quem não tem NENHUMA destas permissões não vê dado de API em tela
# nenhuma (o Portal do Colaborador existe em cima disto). A permissão `ver_chips` mostra o chip como cadastro e vínculo;
# o que a API disse sobre ele pede uma destas.
PERMISSOES_API = (
    "ProjetoEstoque.consultar_conectividade",
    "ProjetoEstoque.administrar_frota_config",
    "ProjetoEstoque.tratar_ocorrencias",
)

# Grupos cujo login vive numa área própria, com middleware próprio. Para eles o
# ambiente não se aplica (ver o cabeçalho).
_GRUPOS_FORA_DOS_AMBIENTES = frozenset({
    GRUPO_FORNECEDOR,
    GRUPO_PARCEIRO_LICENCA,
    GRUPO_PORTAL_CONTRATACAO,
    GRUPO_CONSULTA_QUIOSQUE,
    "Visualizador TV",
})

# Caminhos que não pertencem a ambiente nenhum: conta, sessão, arquivos e o
# sino. Abrem com a identidade do ambiente preferido da pessoa.
CAMINHOS_NEUTROS = (
    "/static/",
    "/media/",
    "/login/",
    "/logout/",
    "/senha/",
    "/perfil/",
    "/notificacoes/",
    "/sistema/erro/",
)

# Endereços que pertencem ao Agro. Os dois últimos são os endereços ANTIGOS do
# módulo de Frotas e Conectividade — hoje só redirecionam (ver
# `urls_legado_frota.py`), mas continuam sendo do Agro: um favorito velho não
# pode passar pelo TI no caminho.
PREFIXOS_AGRO = ("/agro/", "/frotas/", "/m2m/")

# Todo codename que dá acesso ao ambiente Agro (`app_label.codename`). As
# listas de permissão de cada módulo continuam sendo a fonte dos codenames.
_PERMISSOES_AGRO = tuple(
    f"ProjetoEstoque.{codigo}"
    for codigo, _ in (
        list(PERMISSOES_AGRO_AMBIENTE) + list(PERMISSOES_FROTA_VEICULO) + list(PERMISSOES_FROTA_CHIP)
        + list(PERMISSOES_FROTA_INVENTARIO) + list(PERMISSOES_TEC)
    )
)

AMBIENTES = {
    TI: {
        "rotulo": "Zelo TI",
        "curto": "TI",
        "subtitulo": "Tecnologia e infraestrutura",
        "inicio": "dashboard",
    },
    AGRO: {
        "rotulo": "Zelo Agro",
        "curto": "Agro",
        "subtitulo": "Operações agrícolas",
        "inicio": "agro_visao_geral",
    },
}

# ─── Menu lateral do Zelo Agro ────────────────────────────────────────────────
#
# Autorizado pelo usuário em 2026-11-13 (regra 16): seletor na barra superior e
# o menu lateral do Agro, com Frotas e Chips e Conectividade como acessos
# independentes, cada um com o próprio submenu.
#
# `perms`  — basta UMA (vazio = basta estar no ambiente). O grupo aparece se
#            algum item dele aparece.
# `ativo`  — nomes de rota que acendem o item; terminado em `*` é prefixo.
# `icone`  — id do sprite SVG do `base.html` (`#ic-*`), nunca Font Awesome:
#            `test_menu_lateral` exige `<use href="#ic-">` em cada `.nav-icon`.
NAV_AGRO = (
    {
        "rotulo": "Visão geral",
        "url": "agro_visao_geral",
        "icone": "ic-sprout",
        "perms": (),
        "ativo": ("agro_visao_geral",),
    },
    {
        "chave": "estoque",
        "rotulo": "Estoque",
        "icone": "ic-box",
        "titulo": "Estoque — equipamentos tecnológicos, materiais, movimentações e catálogo",
        "itens": (
            {"rotulo": "Visão geral", "url": "tec_visao_geral",
             "perms": ("ProjetoEstoque.ver_tec_estoque",),
             "ativo": ("tec_visao_geral",)},
            {"rotulo": "Equipamentos", "url": "tec_equipamentos",
             "perms": ("ProjetoEstoque.ver_tec_estoque",),
             "ativo": ("tec_equipamento*", "tec_instalacao_*", "tec_veiculo_*")},
            {"rotulo": "Materiais", "url": "tec_materiais",
             "perms": ("ProjetoEstoque.ver_tec_estoque",),
             "ativo": ("tec_materiais", "tec_material_*")},
            {"rotulo": "Movimentações", "url": "tec_movimentacoes",
             "perms": ("ProjetoEstoque.ver_tec_historico",),
             "ativo": ("tec_movimentacoes",)},
            {"rotulo": "Catálogo", "url": "tec_catalogo_inicio",
             "perms": ("ProjetoEstoque.ver_tec_estoque",),
             "ativo": ("tec_catalogo*",)},
        ),
    },
    {
        # Reorganizado em 2026-11-13 (prompt de Frotas e Maquinários, Parte 2, autorização para este grupo). "Todos os
        # equipamentos" NÃO é um item à parte: a Visão geral JÁ é a lista de todos, com Próprios e Terceiros como
        # visões da mesma base (o prompt manda não criar página por classificação). O detalhe, o formulário e as ações
        # sobre o equipamento acendem a Visão geral, de onde se chega a eles.
        "chave": "frotas",
        "rotulo": "Frotas e Maquinários",
        "icone": "ic-tractor",
        "titulo": "Frotas e Maquinários — inventário motorizado, composição tecnológica e movimentações",
        "itens": (
            {"rotulo": "Visão geral", "url": "frota_visao_geral",
             "perms": ("ProjetoEstoque.ver_frota",),
             "ativo": ("frota_visao_geral", "frota_veiculo*", "frota_equipamentos_validar_lote", "frota_opcoes",
                       "frota_tipo_rapido")},
            {"rotulo": "Próprios", "url": "frota_equipamentos_proprios",
             "perms": ("ProjetoEstoque.ver_frota",),
             "ativo": ("frota_equipamentos_proprios",)},
            {"rotulo": "Terceiros", "url": "frota_equipamentos_terceiros",
             "perms": ("ProjetoEstoque.ver_frota",),
             "ativo": ("frota_equipamentos_terceiros",)},
            {"rotulo": "Categorias e classes", "url": "frota_categorias",
             "perms": ("ProjetoEstoque.administrar_frota_config",),
             "ativo": ("frota_categorias", "frota_cadastro_salvar", "frota_config_tipo*")},
            {"rotulo": "Importação de planilha", "url": "frota_equip_importar",
             "perms": ("ProjetoEstoque.importar_equipamentos_frota",),
             "ativo": ("frota_equip_importa*",)},
            {"rotulo": "Composição tecnológica", "url": "frota_composicao",
             "perms": ("ProjetoEstoque.ver_tec_estoque",),
             "ativo": ("frota_composicao",)},
            {"rotulo": "Movimentações", "url": "frota_equip_movimentacoes",
             "perms": ("ProjetoEstoque.ver_historico_frota",),
             "ativo": ("frota_equip_movimentacoes",)},
            {"rotulo": "Histórico", "url": "frota_equip_historico",
             "perms": ("ProjetoEstoque.ver_historico_frota",),
             "ativo": ("frota_equip_historico",)},
            {"rotulo": "Relatórios", "url": "frota_relatorios",
             "perms": ("ProjetoEstoque.ver_frota",),
             "ativo": ("frota_relatorios",)},
        ),
    },
    {
        "chave": "conectividade",
        "rotulo": "Chips e Conectividade",
        "icone": "ic-sim",
        "titulo": "Chips e Conectividade — chips, sessões, consumo e alertas da TIM M2M",
        "itens": (
            {"rotulo": "Visão geral", "url": "m2m_visao_geral",
             "perms": ("ProjetoEstoque.ver_chips",),
             "ativo": ("m2m_visao_geral",)},
            {"rotulo": "Chips cadastrados", "url": "m2m_chips",
             "perms": ("ProjetoEstoque.ver_chips",),
             "ativo": ("m2m_chip*", "m2m_consulta_payload")},
            {"rotulo": "Importação de chips", "url": "frota_importar",
             "perms": ("ProjetoEstoque.importar_chips",),
             "ativo": ("frota_importa*", "frota_modelo_planilha")},
            # `"api": True` — o item é da API (sessão, consumo, alertas de conectividade, integração): só aparece para quem
            # tem permissão de API (`pode_ver_api`), qualquer que seja a permissão de ver o histórico. Portal do Colaborador.
            {"rotulo": "Sessões", "url": "m2m_sessoes",
             "perms": ("ProjetoEstoque.consultar_conectividade",), "api": True,
             "ativo": ("m2m_sessoes",)},
            {"rotulo": "Consumo", "url": "m2m_consumo",
             "perms": ("ProjetoEstoque.consultar_conectividade",), "api": True,
             "ativo": ("m2m_consumo",)},
            {"rotulo": "Movimentações", "url": "frota_movimentacoes",
             "perms": ("ProjetoEstoque.ver_historico_frota",),
             "ativo": ("frota_movimentacoes",)},
            {"rotulo": "Alertas", "url": "m2m_alertas",
             "perms": ("ProjetoEstoque.ver_historico_frota",), "api": True,
             "ativo": ("m2m_alerta*",)},
            {"rotulo": "Configuração", "url": "m2m_config",
             "perms": ("ProjetoEstoque.administrar_frota_config",), "api": True,
             "ativo": ("m2m_config*", "m2m_reinterpretar", "frota_config_operadora*",
                       "frota_config_regra*")},
        ),
    },
    {
        # Autorizado pelo pedido de 2026-11-14 (evolução do Zelo Agro: mapa e dashboard de infraestrutura): o grupo da
        # infraestrutura georreferenciada e o mapa. A permissão é a do estoque tecnológico (`ver_tec_estoque`); editar
        # estrutura e enlace têm as suas (`gerenciar_tec_estrutura`, `administrar_tec_enlace`), conferidas na view.
        "chave": "infraestrutura",
        "rotulo": "Infraestrutura",
        "icone": "ic-network",
        "titulo": "Infraestrutura — torres, estações e enlaces ponto a ponto",
        "itens": (
            {"rotulo": "Visão geral", "url": "infra_visao_geral",
             "perms": ("ProjetoEstoque.ver_tec_estoque",),
             "ativo": ("infra_visao_geral",)},
            {"rotulo": "Torres e estações", "url": "infra_estruturas",
             "perms": ("ProjetoEstoque.ver_tec_estoque",),
             "ativo": ("infra_estrutura*",)},
            {"rotulo": "Enlaces PTP", "url": "infra_enlaces",
             "perms": ("ProjetoEstoque.ver_tec_estoque",),
             "ativo": ("infra_enlace*",)},
        ),
    },
    {
        "rotulo": "Mapa",
        "url": "agro_mapa",
        "icone": "ic-map",
        "perms": ("ProjetoEstoque.ver_frota", "ProjetoEstoque.ver_tec_estoque"),
        "ativo": ("agro_mapa*",),
    },
)


# ─── Quem é quem ──────────────────────────────────────────────────────────────

def _superusuario(user) -> bool:
    return bool(getattr(user, "is_superuser", False))


def fora_dos_ambientes(user) -> bool:
    """Login de área própria (portal externo, TV, perfil restrito) — não staff."""
    if getattr(user, "is_staff", False) or _superusuario(user):
        return False
    return bool(_GRUPOS_FORA_DOS_AMBIENTES.intersection(_grupos_do_usuario(user)))


def somente_agro(user) -> bool:
    """Está num grupo que prende a pessoa no Agro? (Superusuário nunca está preso.)"""
    if _superusuario(user):
        return False
    return not GRUPOS_SOMENTE_AGRO.isdisjoint(_grupos_do_usuario(user))


def _tem_alguma(user, perms) -> bool:
    if not perms:
        return True
    todas = user.get_all_permissions()  # cacheado no objeto user pelo backend
    return any(p in todas for p in perms)


def pode_ver_api(user) -> bool:
    """A pessoa enxerga dado de API (sessão, consumo, consultas, alertas, integração)? Ver `PERMISSOES_API`."""
    if not getattr(user, "is_authenticated", False):
        return False
    return _tem_alguma(user, PERMISSOES_API)


def pode_acessar(user, chave: str) -> bool:
    if not getattr(user, "is_authenticated", False) or fora_dos_ambientes(user):
        return False
    if _superusuario(user):
        return True
    if chave == TI:
        return not somente_agro(user)
    if chave == AGRO:
        return _tem_alguma(user, _PERMISSOES_AGRO)
    return False


def ambientes_de(user) -> tuple:
    """Os ambientes que o usuário alcança, na ordem da tabela."""
    return tuple(chave for chave in AMBIENTES if pode_acessar(user, chave))


# ─── Caminho → ambiente ───────────────────────────────────────────────────────

def caminho_neutro(path: str) -> bool:
    # A raiz não é tela de ambiente nenhum: é a porta que ESCOLHE o ambiente
    # (`views.ambiente.inicio`). Se contasse como TI, o middleware gravaria
    # "usou o TI" antes de a view ler a preferência, e a raiz nunca abriria o Agro.
    return path == "/" or path.startswith(CAMINHOS_NEUTROS)


def ambiente_do_caminho(path: str):
    """`agro`, `ti` ou `None` (caminho neutro)."""
    if caminho_neutro(path):
        return None
    if path.startswith(PREFIXOS_AGRO):
        return AGRO
    return TI


def url_inicial(chave: str) -> str:
    return reverse(AMBIENTES[chave]["inicio"])


# ─── Preferência (o ambiente "da vez") ───────────────────────────────────────
#
# Guardada no servidor (`PreferenciaAmbiente`), então vale em outro computador e
# no próximo acesso. A sessão espelha o valor para que o middleware só escreva
# no banco quando o ambiente MUDA — não a cada clique.
_CHAVE_SESSAO = "zelo_ambiente"


def ambiente_preferido(user, session=None):
    """O último ambiente usado — se a pessoa ainda pode usá-lo."""
    chave = session.get(_CHAVE_SESSAO) if session is not None else None
    if chave is None:
        from ProjetoEstoque.models import PreferenciaAmbiente
        chave = (
            PreferenciaAmbiente.objects.filter(user=user)
            .values_list("ambiente", flat=True).first()
        )
    disponiveis = ambientes_de(user)
    if chave in disponiveis:
        return chave
    return disponiveis[0] if disponiveis else None


def registrar_uso(user, chave: str, session=None) -> None:
    """Grava `chave` como o ambiente da vez (só quando muda)."""
    if chave not in AMBIENTES:
        return
    if session is not None and session.get(_CHAVE_SESSAO) == chave:
        return
    from ProjetoEstoque.models import PreferenciaAmbiente
    PreferenciaAmbiente.objects.update_or_create(user=user, defaults={"ambiente": chave})
    if session is not None:
        session[_CHAVE_SESSAO] = chave


# ─── Menu ─────────────────────────────────────────────────────────────────────

def _acende(url_name: str, padroes) -> bool:
    if not url_name:
        return False
    for padrao in padroes:
        if padrao.endswith("*"):
            if url_name.startswith(padrao[:-1]):
                return True
        elif url_name == padrao:
            return True
    return False


def _href(nome: str):
    try:
        return reverse(nome)
    except NoReverseMatch:
        # Item de uma fase ainda não implantada: some do menu em vez de derrubar
        # todas as telas com um 500.
        return None


def nav_de(user, url_name: str = "") -> list:
    """
    O menu lateral do Agro já filtrado e com o item aceso. Cada entrada:
    `{"rotulo","icone","href","ativo"}` (item) ou
    `{"chave","rotulo","icone","titulo","ativo","itens":[{rotulo,href,ativo}]}` (grupo).
    """
    menu = []
    api = pode_ver_api(user)
    for entrada in NAV_AGRO:
        if "itens" not in entrada:
            href = _href(entrada["url"])
            if href and _tem_alguma(user, entrada["perms"]):
                menu.append({
                    "rotulo": entrada["rotulo"], "icone": entrada["icone"], "href": href,
                    "ativo": _acende(url_name, entrada["ativo"]),
                })
            continue
        itens = []
        for item in entrada["itens"]:
            if item.get("api") and not api:
                continue
            href = _href(item["url"])
            if href and _tem_alguma(user, item["perms"]):
                itens.append({
                    "rotulo": item["rotulo"], "href": href,
                    "ativo": _acende(url_name, item["ativo"]),
                })
        if itens:
            menu.append({
                "chave": entrada["chave"], "rotulo": entrada["rotulo"],
                "icone": entrada["icone"], "titulo": entrada["titulo"],
                "ativo": any(i["ativo"] for i in itens), "itens": itens,
            })
    return menu


# ─── Notificações (sino) ──────────────────────────────────────────────────────
#
# O sino não mistura os ambientes: `Notificacao.categoria` começando com `agro`
# é do Agro; o resto é do TI (era tudo que existia antes).
PREFIXO_CATEGORIA_AGRO = "agro"


def notificacoes_do_ambiente(qs, chave):
    if chave == AGRO:
        return qs.filter(categoria__startswith=PREFIXO_CATEGORIA_AGRO)
    return qs.exclude(categoria__startswith=PREFIXO_CATEGORIA_AGRO)


# ─── Contexto de template ────────────────────────────────────────────────────

def contexto(request) -> dict:
    """O que o `base.html` precisa: ambiente atual, os disponíveis e o menu."""
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False) or fora_dos_ambientes(user):
        return {"ambiente_atual": None, "ambientes_disponiveis": (), "nav_ambiente": ()}

    chave = getattr(request, "ambiente", None) or ambiente_do_caminho(request.path)
    if chave is None:
        chave = ambiente_preferido(user, getattr(request, "session", None)) or TI

    disponiveis = [
        {"chave": c, "rotulo": AMBIENTES[c]["rotulo"], "curto": AMBIENTES[c]["curto"],
         "href": url_inicial(c), "ativo": c == chave}
        for c in ambientes_de(user)
    ]
    match = getattr(request, "resolver_match", None)
    url_name = getattr(match, "url_name", "") or ""
    return {
        "ambiente_atual": {"chave": chave, **AMBIENTES[chave]},
        "ambientes_disponiveis": disponiveis,
        "nav_ambiente": nav_de(user, url_name) if chave == AGRO else (),
    }
