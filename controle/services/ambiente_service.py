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
    Django Admin continua entrando no TI. A exceção é o grupo
    `GRUPO_SOMENTE_AGRO` ("Zelo Agro — Exclusivo"), que prende a pessoa no Agro.
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
    GRUPO_CONSULTA_QUIOSQUE,
    GRUPO_FORNECEDOR,
    GRUPO_PARCEIRO_LICENCA,
    GRUPO_PORTAL_CONTRATACAO,
    GRUPO_SOMENTE_AGRO,
    PERMISSOES_AGRO_AMBIENTE,
    PERMISSOES_FROTA_CHIP,
    PERMISSOES_FROTA_VEICULO,
    PERMISSOES_TEC,
)

TI = "ti"
AGRO = "agro"

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
        list(PERMISSOES_AGRO_AMBIENTE) + list(PERMISSOES_FROTA_VEICULO) + list(PERMISSOES_FROTA_CHIP) + list(PERMISSOES_TEC)
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
        "rotulo": "Estoque tecnológico",
        "icone": "ic-box",
        "titulo": "Estoque tecnológico — equipamentos, materiais, movimentações e catálogo",
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
        "chave": "frotas",
        "rotulo": "Frotas",
        "icone": "ic-tractor",
        "titulo": "Frotas — veículos e equipamentos agrícolas",
        "itens": (
            {"rotulo": "Visão geral", "url": "frota_visao_geral",
             "perms": ("ProjetoEstoque.ver_frota",),
             "ativo": ("frota_visao_geral",)},
            {"rotulo": "Veículos e equipamentos", "url": "frota_veiculos",
             "perms": ("ProjetoEstoque.ver_frota",),
             "ativo": ("frota_veiculo*",)},
            {"rotulo": "Tipos de veículo", "url": "frota_config_tipos",
             "perms": ("ProjetoEstoque.administrar_frota_config",),
             "ativo": ("frota_config_tipo*",)},
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
            {"rotulo": "Sessões", "url": "m2m_sessoes",
             "perms": ("ProjetoEstoque.consultar_conectividade",),
             "ativo": ("m2m_sessoes",)},
            {"rotulo": "Consumo", "url": "m2m_consumo",
             "perms": ("ProjetoEstoque.consultar_conectividade",),
             "ativo": ("m2m_consumo",)},
            {"rotulo": "Movimentações", "url": "frota_movimentacoes",
             "perms": ("ProjetoEstoque.ver_historico_frota",),
             "ativo": ("frota_movimentacoes",)},
            {"rotulo": "Alertas", "url": "m2m_alertas",
             "perms": ("ProjetoEstoque.ver_historico_frota",),
             "ativo": ("m2m_alerta*",)},
            {"rotulo": "Configuração", "url": "m2m_config",
             "perms": ("ProjetoEstoque.administrar_frota_config",),
             "ativo": ("m2m_config*", "m2m_reinterpretar", "frota_config_operadora*",
                       "frota_config_regra*")},
        ),
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
    """Está no grupo que prende a pessoa no Agro? (Superusuário nunca está preso.)"""
    if _superusuario(user):
        return False
    return GRUPO_SOMENTE_AGRO in _grupos_do_usuario(user)


def _tem_alguma(user, perms) -> bool:
    if not perms:
        return True
    todas = user.get_all_permissions()  # cacheado no objeto user pelo backend
    return any(p in todas for p in perms)


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
