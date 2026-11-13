"""
Perfis de ACESSO RESTRITO — quem entra no sistema para usar UMA tela só.

Existem dois, e os dois nasceram do mesmo pedido: dar a tela certa para quem
não é do TI sem dar o sistema inteiro de brinde.

  • **Portal de Contratações** (grupo `Portal de Contratações`) — RH, gestor de
    área ou terceiro ABRE a vaga e acompanha o andamento. Pede e consulta; não
    provisiona. É o papel que antes preenchia o card do Planner.

  • **Consulta Quiosque** (grupo `Consulta Quiosque`) — enxerga TODOS os
    aparelhos, com ficha, histórico de check-in e localização, e **não muda
    nada**: nem configuração, nem comando remoto, nem matrícula, nem exclusão.

Este módulo é a régua ÚNICA de "o que esta pessoa pode fazer". Quem autoriza o
*caminho* é o `AcessoRestritoMiddleware`; quem autoriza a *ação* são os
decoradores daqui; quem esconde o botão é o `contexto_restrito` no template. As
três camadas leem desta mesma tabela — a lição do Portal do Fornecedor
(CLAUDE.md regra 22 e o cabeçalho de `views/portal_fornecedor.py`): esconder o
botão não é segurança, e autorizar em dois lugares diferentes é garantir que um
dia eles discordem.

Os dois perfis são INDEPENDENTES e se SOMAM no mesmo login: quem está nos dois
grupos alcança as telas de um e as do outro, cada uma com a restrição do seu
próprio perfil. Os grupos nunca são tratados como opção exclusiva — conceder um
não tira o outro, e revogar um não mexe no outro (ver `perfis_de`).

IMPORTANTE: `is_staff` e `is_superuser` passam por cima de tudo aqui, igual aos
quatro middlewares que já existem. Um usuário restrito tem de ser criado SEM
staff — é o que a tela de gestão faz.
"""
from functools import wraps

from django.core.exceptions import PermissionDenied
from django.urls import reverse

from ProjetoEstoque.middleware import _grupos_do_usuario
from ProjetoEstoque.models import (
    GRUPO_CONSULTA_QUIOSQUE,
    GRUPO_FORNECEDOR,
    GRUPO_PARCEIRO_LICENCA,
    GRUPO_PORTAL_CONTRATACAO,
)
from services.perfil_acesso_service import GRUPO_TV

# ─── A tabela ─────────────────────────────────────────────────────────────────
#
# `prefixos`           caminhos que o perfil alcança (o middleware barra o resto);
# `bloqueados`         caminhos DENTRO do prefixo que ainda assim são proibidos —
#                      é como "ver tudo, mexer em nada" se escreve para o
#                      Quiosque, onde leitura e gestão moram sob o mesmo
#                      `/quiosque/`;
# `bloqueados_contem`  trechos que aparecem no MEIO do caminho
#                      (`/quiosque/<pk>/comando/`), onde o pk fica antes;
# `inicio`             para onde o middleware joga quem tenta sair da área;
# `nav`                o menu lateral reduzido que a pessoa vê no lugar do menu
#                      do TI. `icone` é o id do sprite SVG do `base.html`
#                      (`#ic-*`), NÃO classe Font Awesome: o menu lateral é todo
#                      desenhado em sprite, e `test_menu_lateral` exige um
#                      `<use href="#ic-">` dentro de cada `.nav-icon`.
PERFIS = {
    "contratacao": {
        "grupo": GRUPO_PORTAL_CONTRATACAO,
        "rotulo": "Portal de Contratações",
        "detalhe": "Abertura e acompanhamento de vagas",
        "inicio": "contratacao_painel",
        "prefixos": ("/contratacoes/",),
        # O cadastro de sistemas corporativos é configuração do TI: define o que
        # o formulário pergunta. Quem preenche o formulário não decide isso.
        "bloqueados": ("/contratacoes/sistemas/",),
        "bloqueados_contem": (),
        "nav": (
            {"url": "contratacao_painel", "rotulo": "Painel de contratações", "icone": "ic-briefcase"},
            {"url": "contratacao_create", "rotulo": "Abrir nova vaga", "icone": "ic-users"},
            {"url": "chamados_lista", "rotulo": "Chamados de TI", "icone": "ic-wrench"},
        ),
    },
    "quiosque": {
        "grupo": GRUPO_CONSULTA_QUIOSQUE,
        "rotulo": "Consulta Quiosque",
        "detalhe": "Somente leitura",
        "inicio": "quiosque_dashboard",
        "prefixos": ("/quiosque/",),
        # Tudo que ESCREVE no aparelho ou no cadastro. A API do device
        # (`/api/quiosque/`) nem entra na conversa: não está em `prefixos`,
        # então já está fora.
        "bloqueados": (
            "/quiosque/matriculas/",
            "/quiosque/apk/",
            "/quiosque/instalador/",
        ),
        "bloqueados_contem": ("/config/", "/comando/", "/revogar/", "/excluir/"),
        "nav": (
            {"url": "quiosque_dashboard", "rotulo": "Aparelhos", "icone": "ic-mobile"},
            {"url": "quiosque_mapa", "rotulo": "Mapa", "icone": "ic-network"},
            {"url": "quiosque_indicadores", "rotulo": "Indicadores", "icone": "ic-pie"},
        ),
    },
}

# Grupos de portais EXTERNOS: quem está num deles não pede estrutura de vaga
# (ver `pode_solicitar_estrutura`). Cada um tem o próprio middleware.
_GRUPOS_DE_PORTAL_EXTERNO = frozenset({GRUPO_FORNECEDOR, GRUPO_PARCEIRO_LICENCA, GRUPO_TV})

# Caminhos que TODO perfil restrito alcança — sem eles a pessoa não consegue nem
# entrar nem sair, e a tela abre sem CSS.
COMUM_LIBERADO = ("/static/", "/login/", "/logout/")


def _interno(user) -> bool:
    """Staff/superusuário não é restrito, aconteça o que acontecer com os grupos.

    Mesma cláusula dos quatro middlewares que já existem. Está escrita aqui uma
    vez para que "interno" signifique a mesma coisa nas três camadas.
    """
    return bool(getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))


def perfis_de(user) -> tuple:
    """
    TODAS as chaves de perfil restrito do usuário, na ordem da tabela `PERFIS`
    (`()` quando ele é interno, anônimo ou não tem grupo restrito).

    Os perfis se somam. Quem está em `Portal de Contratações` E em `Consulta
    Quiosque` alcança as duas áreas: o caminho é liberado se QUALQUER um dos
    perfis o libera (`caminho_liberado_em`), e cada capacidade é decidida pelo
    perfil do próprio módulo (`pode_gerenciar_*`). Não há conflito a resolver
    porque os `prefixos` não se sobrepõem — e uma restrição de um perfil nunca
    é herdada pelo outro: o Quiosque ser só leitura não faz a vaga ficar só
    leitura.
    """
    if not getattr(user, "is_authenticated", False) or _interno(user):
        return ()
    grupos = set(_grupos_do_usuario(user))
    return tuple(chave for chave, perfil in PERFIS.items() if perfil["grupo"] in grupos)


def perfil_de(user):
    """
    O perfil PRINCIPAL do usuário — o primeiro da tabela entre os que ele tem —,
    ou `None` quando ele é interno.

    Serve só para escolher a tela inicial de quem tenta sair da própria área.
    Não decide acesso: quem decide é `perfis_de` (todos) e `caminho_liberado_em`.
    """
    perfis = perfis_de(user)
    return perfis[0] if perfis else None


def _pertence(user, chave) -> bool:
    return chave in perfis_de(user)


# ─── Caminhos (usado pelo middleware) ────────────────────────────────────────

def caminho_liberado(path: str, chave: str) -> bool:
    """O `path` está dentro da área do perfil `chave`?"""
    if path.startswith(COMUM_LIBERADO):
        return True
    perfil = PERFIS[chave]
    if not path.startswith(perfil["prefixos"]):
        return False
    if perfil["bloqueados"] and path.startswith(perfil["bloqueados"]):
        return False
    return not any(trecho in path for trecho in perfil["bloqueados_contem"])


def caminho_liberado_em(path: str, chaves) -> bool:
    """
    O `path` está dentro da área de ALGUM dos perfis em `chaves`?

    É a união: quem tem dois perfis alcança o que cada um alcança. Cada perfil é
    avaliado com as PRÓPRIAS travas (`bloqueados`/`bloqueados_contem`), então
    "Quiosque só leitura" continua barrando `/quiosque/<pk>/comando/` mesmo para
    quem também tem o Portal de Contratações.
    """
    return any(caminho_liberado(path, chave) for chave in chaves)


def url_inicial(chave: str) -> str:
    return reverse(PERFIS[chave]["inicio"])


def url_de_retorno(path: str, chaves) -> str:
    """
    Para onde devolver quem esbarrou numa trava em `path`.

    Quem tem um perfil só volta para a tela inicial dele. Quem acumula perfis
    volta para a tela inicial da ÁREA em que estava — bater numa trava do
    Quiosque leva de volta ao Quiosque, não para Contratações —; fora de
    qualquer área, vale o primeiro perfil da tabela.
    """
    for chave in chaves:
        if path.startswith(PERFIS[chave]["prefixos"]):
            return url_inicial(chave)
    return url_inicial(chaves[0])


# ─── Capacidades (usado por views e templates) ───────────────────────────────

def pode_gerenciar_contratacao(user) -> bool:
    """
    Pode ATENDER a contratação — avançar etapa, marcar acesso/licença como
    provisionado, excluir, mexer no catálogo de sistemas.

    A linha é entre **pedir** e **atender**. Quem abre a vaga descreve o que
    precisa; quem diz que o usuário de rede foi criado é o TI, porque é o TI
    que cria. Deixar o solicitante marcar "provisionado" transformaria o
    checklist num campo de torcida — e o checklist é a única coisa que responde
    "o que ainda falta para esse colaborador sentar e trabalhar".
    """
    return not _pertence(user, "contratacao")


def pode_solicitar_estrutura(user) -> bool:
    """
    Pode PEDIR a estrutura de uma vaga — abrir os chamados de máquina, usuário
    e e-mail e acesso a sistemas, e tentar de novo o que falhou.

    É o lado de PEDIR da linha de `pode_gerenciar_contratacao`: quem abre a vaga
    é quem sabe do que o novo colaborador vai precisar, então o solicitante
    (Portal de Contratações) pode — e o TI também. Fica de fora:

      • quem só tem o perfil de consulta do Quiosque (não alcança Contratações);
      • quem pertence a um portal externo (Fornecedor, Parceiro de Licenças,
        Visualizador TV): o middleware dele já barra o caminho, e esta checagem
        garante que a AÇÃO também diria não se o caminho um dia se abrisse.

    O que decide é o perfil, não o botão: o template esconde, o decorador barra.
    """
    if not getattr(user, "is_authenticated", False):
        return False
    if _interno(user):
        return True
    perfis = perfis_de(user)
    if perfis:
        return "contratacao" in perfis
    return not (set(_grupos_do_usuario(user)) & _GRUPOS_DE_PORTAL_EXTERNO)


def pode_preencher_vaga(user) -> bool:
    """
    Pode marcar uma vaga como PREENCHIDA, corrigir os dados do contratado e
    reabrir a vaga preenchida.

    Quem encerra o processo seletivo é o RH — não o TI —, então o solicitante do
    Portal de Contratações pode (e o TI também). É a mesma régua de pedir a
    estrutura (`pode_solicitar_estrutura`), com nome próprio para a regra poder
    ser estreitada sem mexer no outro lado: se um dia só parte de quem tem o
    Portal puder encerrar vagas (o grupo hoje inclui gestor de área e
    terceiro), a mudança é nesta função.

    Etapas depois de "Vaga preenchida" (equipamento entregue, concluir, cancelar,
    reabrir uma vaga já entregue/concluída) continuam sendo do TI
    (`pode_gerenciar_contratacao`): são o atendimento da vaga, não o seu pedido.
    """
    return pode_solicitar_estrutura(user)


def pode_acompanhar_chamados(user) -> bool:
    """
    Pode VER os chamados de uma vaga, ler a conversa, responder e baixar anexos.

    Mesma régua de `pode_solicitar_estrutura`: quem alcança o módulo de
    Contratações acompanha os chamados dele. Fica de fora quem só tem a consulta
    do Quiosque e quem é de portal externo (Fornecedor, Parceiro, TV) — para
    eles o middleware já barra o caminho, e esta checagem faz a AÇÃO dizer não
    mesmo que um dia o caminho se abra. Atualizar a situação do chamado, tratar
    a quarentena e a sincronização são do TI (`pode_gerenciar_contratacao`).
    """
    return pode_solicitar_estrutura(user)


def pode_concluir_contratacao(user) -> bool:
    """
    Pode CONFIRMAR A INTEGRAÇÃO do colaborador e CONCLUIR a contratação.

    Quem fecha o processo é o RH: ele acompanha as etapas do TI pela própria vaga
    e diz quando o colaborador foi integrado. O TI também pode (cobre a ausência
    do RH). A conclusão só passa quando todas as etapas obrigatórias estão feitas
    ou dispensadas — essa trava é do service, não desta função. Dispensar uma
    etapa, registrar orientações e reservar equipamento seguem sendo do TI
    (`pode_gerenciar_contratacao`): é o atendimento da vaga.

    Nome próprio, mesma régua de `pode_preencher_vaga`, pelo mesmo motivo: se um
    dia só parte de quem tem o Portal puder concluir, a mudança é aqui.
    """
    return pode_preencher_vaga(user)


def pode_gerenciar_quiosque(user) -> bool:
    """
    Pode MEXER no aparelho — configuração, comando remoto, matrícula, APK,
    instalador, revogar, excluir.

    Para o perfil de consulta isto é `False` por definição: foi o pedido.
    """
    return not _pertence(user, "quiosque")


# ─── Decoradores ─────────────────────────────────────────────────────────────

def _barra(verificador, mensagem):
    """Fabrica o decorador de uma capacidade.

    Responde **403**, não um redirecionamento com `messages`: estes endpoints
    são POST de botão que o perfil restrito nem vê. Chegar aqui significa URL
    montada à mão, e aí a resposta certa é dizer não — não devolver a tela como
    se o clique tivesse sido um engano.
    """
    def decorador(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            if not verificador(request.user):
                raise PermissionDenied(mensagem)
            return view_func(request, *args, **kwargs)
        return _wrapped
    return decorador


exige_gestao_contratacao = _barra(
    pode_gerenciar_contratacao,
    "Seu acesso permite abrir e acompanhar contratações, não atendê-las.",
)
exige_gestao_quiosque = _barra(
    pode_gerenciar_quiosque,
    "Seu acesso ao Quiosque é somente de consulta.",
)
exige_solicitar_estrutura = _barra(
    pode_solicitar_estrutura,
    "Seu acesso não permite solicitar a estrutura de uma vaga.",
)
exige_preencher_vaga = _barra(
    pode_preencher_vaga,
    "Seu acesso não permite alterar o preenchimento de uma vaga.",
)
exige_concluir_contratacao = _barra(
    pode_concluir_contratacao,
    "Seu acesso não permite confirmar a integração nem concluir a contratação.",
)
exige_acompanhar_chamados = _barra(
    pode_acompanhar_chamados,
    "Seu acesso não permite acompanhar os chamados de uma vaga.",
)


# ─── Contexto de template ────────────────────────────────────────────────────

def contexto_restrito(request) -> dict:
    """
    O que o `base.html` precisa saber para se encolher, e o que as telas dos
    dois módulos precisam para esconder o que a pessoa não pode acionar.

    Roda em TODA requisição (é context processor), então sai na primeira linha
    para usuário interno — que é o caso de quase toda requisição do sistema.

    O menu sai em SEÇÕES, uma por perfil que a pessoa tem: quem acumula
    Contratações e Quiosque vê as duas áreas, cada uma com o rótulo próprio, em
    vez de um menu único que misturaria as duas.
    """
    user = getattr(request, "user", None)
    chaves = perfis_de(user) if user is not None else ()
    if not chaves:
        return {
            "acesso_restrito": None,
            "pode_gerenciar_contratacao": True,
            "pode_gerenciar_quiosque": True,
            "pode_solicitar_estrutura": True,
            "pode_preencher_vaga": True,
            "pode_concluir_contratacao": True,
            "pode_acompanhar_chamados": True,
        }

    atual = getattr(request, "path", "") or ""
    secoes = []
    for chave in chaves:
        perfil = PERFIS[chave]
        menu = []
        for item in perfil["nav"]:
            destino = reverse(item["url"])
            menu.append({
                "url": destino,
                "rotulo": item["rotulo"],
                "icone": item["icone"],
                # "Painel" não pode ficar aceso em cima de "Abrir nova vaga": o
                # painel é prefixo de todo caminho do módulo, então a comparação
                # é exata — e o `if` abaixo faz o detalhe (`/contratacoes/7/`)
                # pendurar no primeiro item em vez de deixar o menu todo apagado.
                "ativo": atual == destino,
            })
        # O fallback vale POR SEÇÃO e só dentro da área dela: com dois perfis,
        # acender o primeiro item de cada seção ao mesmo tempo mostraria duas
        # telas abertas ao mesmo tempo.
        if menu and atual.startswith(perfil["prefixos"]) and not any(m["ativo"] for m in menu):
            menu[0]["ativo"] = True
        secoes.append({"chave": chave, "rotulo": perfil["rotulo"], "menu": menu})

    return {
        "acesso_restrito": chaves[0],
        "acesso_restrito_chaves": chaves,
        "acesso_restrito_secoes": secoes,
        "pode_gerenciar_contratacao": "contratacao" not in chaves,
        "pode_gerenciar_quiosque": "quiosque" not in chaves,
        "pode_solicitar_estrutura": pode_solicitar_estrutura(user),
        "pode_preencher_vaga": pode_preencher_vaga(user),
        "pode_concluir_contratacao": pode_concluir_contratacao(user),
        "pode_acompanhar_chamados": pode_acompanhar_chamados(user),
    }


# ─── Provisionamento (usado pela tela de gestão) ─────────────────────────────
#
# Conceder acesso é criar/vincular um login Django e colocá-lo no grupo. Não há
# tabela de perfil para preencher (ver o cabeçalho): o grupo É a autorização.

_SENHA_MIN = 6


def _esquecer_cache(user_id: int) -> None:
    """
    Derruba o cache de grupos e o do rótulo do chip para este usuário.

    As duas chaves pertencem a OUTROS módulos (`middleware._grupos_do_usuario` e
    `perfil_acesso_service.perfil_de_acesso`), e os dois cacheiam por 60s. Sem
    esta linha, quem acabou de receber ou perder o acesso continua com o perfil
    antigo por até um minuto — e "removi e a pessoa continuou entrando" é
    exatamente o tipo de defeito que ninguém reproduz depois.
    """
    from django.core.cache import cache

    cache.delete(f"grupos_usuario_{user_id}")
    cache.delete(f"perfil_acesso_{user_id}")


def grupo_de(chave: str):
    """O `Group` do perfil, criado na hora se a migration 0181 não rodou."""
    from django.contrib.auth.models import Group

    grupo, _ = Group.objects.get_or_create(name=PERFIS[chave]["grupo"])
    return grupo


def usuarios_do_perfil(chave: str):
    return list(
        grupo_de(chave).user_set.filter(is_active=True)
        .order_by("first_name", "last_name", "username")
    )


def candidatos(chave: str):
    """
    Logins que podem receber o acesso restrito `chave`: ativos, não-staff e que
    ainda NÃO estão no grupo DESTE perfil.

    A exclusão é só do grupo pedido, não de todos: quem já tem Consulta Quiosque
    continua sendo candidato ao Portal de Contratações — os perfis se somam.

    Staff fica fora porque o acesso restrito **não teria efeito nenhum** nele —
    os middlewares, este service inclusive, deixam staff passar por tudo. Mostrar
    um administrador na lista seria oferecer uma restrição que o sistema não vai
    aplicar.
    """
    from django.contrib.auth import get_user_model

    return (
        get_user_model().objects
        .filter(is_active=True, is_staff=False, is_superuser=False)
        .exclude(groups__name=PERFIS[chave]["grupo"])
        .order_by("first_name", "last_name", "username")
    )


def conceder(*, chave, username, email="", senha="", user=None):
    """
    Dá a `username` o acesso restrito `chave`. Cria o login se ele não existir.

    ADITIVO: o login que já tem outro perfil restrito o mantém — Contratações e
    Quiosque se somam. Conceder nunca substitui um grupo por outro, e conceder o
    mesmo grupo de novo é um no-op.

    Retorna `(usuario, criado_bool)`. Lança `ValidationError` com texto pronto
    para a tela.
    """
    from django.contrib.auth import get_user_model
    from django.core.exceptions import ValidationError
    from django.db import transaction

    if chave not in PERFIS:
        raise ValidationError("Perfil de acesso desconhecido.")

    username = (username or "").strip()
    email = (email or "").strip()
    senha = (senha or "").strip()

    if not username:
        raise ValidationError("Informe o nome de usuário.")
    if senha and len(senha) < _SENHA_MIN:
        raise ValidationError(f"A senha deve ter ao menos {_SENHA_MIN} caracteres.")

    AuthUser = get_user_model()
    with transaction.atomic():
        existente = AuthUser.objects.filter(username=username).first()
        grupo = grupo_de(chave)

        if existente:
            # Recusar em vez de criar um acesso que não restringe nada. O
            # middleware libera staff/superusuário em qualquer caminho, então
            # colocar um administrador no grupo daria uma linha na tela e
            # nenhuma restrição no sistema — a pior combinação possível.
            if existente.is_staff or existente.is_superuser:
                raise ValidationError(
                    f"“{username}” é administrador do sistema. O acesso restrito não "
                    "se aplica a staff/superusuário — ele continuaria alcançando tudo."
                )
            existente.groups.add(grupo)
            if email:
                existente.email = email
            if senha:
                existente.set_password(senha)
            existente.save()
            _esquecer_cache(existente.pk)
            return existente, False

        if not senha:
            raise ValidationError("Informe uma senha para o novo usuário.")

        novo = AuthUser.objects.create_user(
            username=username, email=email, password=senha,
            is_staff=False, is_superuser=False,
        )
        novo.groups.add(grupo)
        _esquecer_cache(novo.pk)
        return novo, True


def revogar(*, chave, usuario):
    """
    Tira o acesso restrito `chave` e SÓ ele — o outro perfil, se houver, segue
    intacto. **Não** desativa nem apaga o login.

    De propósito: o mesmo login pode ser o de um colaborador que amanhã recebe
    outro acesso, e `is_active=False` o tiraria de tudo — inclusive do histórico
    de quem criou o quê, que é `criado_por` em meia tabela do sistema.
    """
    from django.core.exceptions import ValidationError

    if chave not in PERFIS:
        raise ValidationError("Perfil de acesso desconhecido.")
    usuario.groups.remove(grupo_de(chave))
    _esquecer_cache(usuario.pk)


def painel_de_acessos() -> list:
    """
    Os dois perfis com seus usuários e candidatos — o que a tela de gestão
    desenha. Cada usuário ganha `tambem_em`: os rótulos dos OUTROS perfis que a
    mesma pessoa tem, para a tela mostrar que o login acumula as duas áreas.
    """
    perfis = [
        {
            "chave": chave,
            "rotulo": perfil["rotulo"],
            "detalhe": perfil["detalhe"],
            "inicio": url_inicial(chave),
            "usuarios": usuarios_do_perfil(chave),
            "candidatos": list(candidatos(chave)),
        }
        for chave, perfil in PERFIS.items()
    ]

    rotulos_por_usuario = {}
    for perfil in perfis:
        for usuario in perfil["usuarios"]:
            rotulos_por_usuario.setdefault(usuario.pk, []).append(perfil["rotulo"])
    for perfil in perfis:
        for usuario in perfil["usuarios"]:
            usuario.tambem_em = [
                r for r in rotulos_por_usuario[usuario.pk] if r != perfil["rotulo"]
            ]
    return perfis
