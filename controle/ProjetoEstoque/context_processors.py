"""Context processors globais (injetados em todos os templates via settings)."""


# ─── Versão dos arquivos estáticos (cache-busting) ───────────────────────────
#
# O projeto usa CompressedStaticFilesStorage SEM manifesto (decisão registrada
# no settings.py: manifesto quebraria referências estáticas existentes). Sem
# manifesto não há hash no nome do arquivo, então o navegador decide sozinho
# quando revalidar o CSS — e um usuário com a folha antiga em cache vê a tela
# quebrada depois de um deploy.
#
# SUBIR ESTE NÚMERO a cada mudança em qualquer arquivo de static/css/. É a
# única coisa que precisa ser feita: todo template monta a URL como
# `{% static 'css/x.css' %}?v={{ assets_version }}`.
# v5 do design system (tokens.css + ui-kit.css + componentes do base.css
# reescritos). Sem subir isto, quem tem a folha v4 em cache recebe o HTML novo
# com o CSS antigo — e a tela sai quebrada de um jeito difícil de diagnosticar,
# porque no computador de quem fez o deploy está tudo certo.
# 2026-10-06: ds-termos.css + termo-assinar.js (assinatura digital de termos).
# 2026-10-06b: Select2 e tela de preparação da assinatura (ds-termos.css).
# 2026-10-06c: alinhamento da grade de campos do termo (ds-termos.css).
# 2026-10-07: telas de erro com pivô (erro-pivo.css) + desligamento assinado.
# 2026-10-07b: tokens.css — --surface/--surface-sunken/--border/--border-strong
# ficavam presos no valor claro em modo escuro (alias declarado só em :root,
# nunca redeclarado em body.theme-dark). Texto quase invisível no detalhe do
# termo assinado e em qualquer tela que usasse esses 4 aliases no escuro.
# 2026-10-07c: tokens.css — mesmo defeito achado em mais 5 aliases
# (--success/--warning/--danger/--info/--ring), que o teste por lista nomeada
# tinha deixado passar. Afetava o chip "Aguardando assinatura" do painel de
# assinatura. O teste agora VARRE todo alias de :root, não lista por nome.
# 2026-10-07d: ds-termos.css — selo de equipamento COMPARTILHADO (.ta-opt-tag e
# .ta-selo-compartilhado), usado na preparação, no painel, no detalhe e na tela
# do tablet agora que o termo passou a aceitar item compartilhado já Ativo.
# 2026-10-07e: ds-repositorio-termos.css (nova) — o Repositório de Termos saiu
# do <style> de 240 linhas com paleta Fluent escrita à mão e virou arquivo
# navegável por pastas, usando só tokens do sistema.
# 2026-10-07f: ds-repositorio-termos.css — .rt-linha usava UM grid-template-
# columns de 5 trilhas para dois tipos de linha (pendência=5 filhos, termo=6),
# desalinhando toda linha de conteúdo de pasta/busca. Modificadores --pendente/
# --termo + cabeçalho de coluna (.rt-linha-cab) + filtro virou <details>
# retrátil.
# 2026-10-07g: ds-licencas.css — o módulo de licenças saiu da paleta Fluent
# própria (rampa de cinza à mão + bloco body.theme-dark paralelo) e passou a
# derivar 100% de tokens.css; componentes novos do painel de saúde (.lic-saude-*).
# 2026-10-14: apresentacao.css + apresentacao.js (nova vitrine pública
# /apresentacao/, alcançada pela tela de acesso).
# 2026-10-14b: cinema.css nasce (a paleta e as camadas de atmosfera saem do
# apresentacao.css para serem divididas com a tela de acesso); auth.css
# reescrito na mesma linguagem, com claro = tokens do sistema e escuro = palco
# do filme; maquetes da vitrine refeitas sobre as telas reais de equipamentos,
# preventiva de rede e mapa do quiosque; carrossel das telas.
# 2026-10-14c: .ap-luz desligada (lia como mancha azul, não como
# continuidade); carrossel trocado de grid-auto-columns para flexbox
# (as porcentagens de grid colapsavam os 6 slides lado a lado).
# 2026-10-14d: auth.css — o reset `.au-page button { color: inherit }`
# vencia por especificidade a cor própria de `.btn-login` e deixava o
# texto do botão branco sobre fundo branco no tema escuro.
# 2026-10-29: módulo de Contratações — nova folha ds-contratacoes.css
# (.ctr-*, linguagem visual da Meta) e item "Contratações" no submenu Pessoas.
# 2026-10-29c: acesso restrito (Portal de Contratacoes / Consulta Quiosque)
# -- ds-contratacoes.css passou a estilizar input[type=email] e
# input[type=password], que a tela de Acessos restritos usa. Sem subir aqui,
# os dois campos abrem no desenho padrao do navegador ao lado dos vizinhos.
# 2026-10-30: licenças de Contratações — busca com Select2 em vez de
# <select multiple>, e linha de "usuário espelho" por licença marcada
# (.ctr-licencas-espelhos / .ctr-licenca-espelho, novas em ds-contratacoes.css).
# 2026-10-30b: Contratações trocou a linguagem visual da Meta pela do
# Microsoft Planner (violeta #5b5fc7, botão em pílula, coluna sem cartão,
# círculo de conclusão) e sistema+licença viraram UMA lista (.ctr-acesso*,
# .ctr-tag, .ctr-busca). Sem subir aqui, o navegador serve a folha azul
# antiga por cima do HTML novo e a tela abre com metade dos componentes
# sem estilo — HTTP 200, nada no console.
# 2026-11-09b: ds-contratacoes.css — fila "pedir estrutura" do painel (.ctr-fila*)
# e aviso com botão no detalhe da vaga (.ctr-aviso-acao). Sem subir aqui, o RH
# vê a fila como lista sem moldura e o botão do aviso colado ao texto.
# 2026-11-09c: ds-contratacoes.css — conversa dos chamados (.ctr-msg*, .ctr-anexo*,
# .ctr-resposta), lista de chamados (.ctr-grupo*, .ctr-chamado-linha*) e janelas
# do preenchimento da vaga. Sem subir aqui, a conversa abre como texto corrido
# sem balões nem lados (RH/TI), o que não dá erro — só some a leitura.
# 2026-11-11: Monitoramento TIM M2M — folha NOVA ds-m2m.css (.m2m-*) e script NOVO
# m2m.js. Sem subir aqui, quem já tem a página em cache não recebe nenhum dos
# dois e as sete telas abrem sem estilo e sem os botões de atualizar (HTTP 200,
# nenhum erro no servidor).
# 2026-11-13: Ambientes Zelo TI / Zelo Agro — tokens.css (bloco do Agro, verde),
# base.css (seletor de ambiente na barra superior) e ds-m2m.css (abas por seção).
# Sem subir aqui, o Agro abre com o violeta do TI e o seletor sem moldura.
# 2026-11-13b: base.css — menu lateral VERDE no Zelo Agro (todos os estados).
# 2026-11-13c: base.css — menu lateral AZUL no Zelo TI (todos os estados).
# 2026-11-13e: ds-agro.css + agro.js (estoque tecnologico do Agro: equipamentos, materiais, catalogo).
# 2026-11-13d: base.css — azul do menu do TI mais claro (#1e40af); equipamento-detalhe.css — Voltar/QR/Editar saem da barra superior para o cartão do ativo.
# 2026-11-13f: ds-agro.css — "Ja passaram por esta maquina" (.m2m-passado) na composicao tecnologica do veiculo.
# 2026-11-13g: ds-agro.css — grupo de acoes do cabecalho do painel (.m2m-painel-acoes: Instalar equipamento, Aplicar material).
ASSETS_VERSION = "20261113g"


def assets(request):
    """Expõe `assets_version` para o cache-busting dos arquivos estáticos."""
    return {"assets_version": ASSETS_VERSION}


def _prep(notificacoes, *, portal):
    """Anota cada notificação com `nao_lida` e `link` conforme o público (interno
    ou portal do fornecedor), para o template do sino ser agnóstico."""
    itens = list(notificacoes)
    for n in itens:
        if portal:
            n.nao_lida = not n.lida_fornecedor
            n.link = n.portal_url or n.url or "#"
        else:
            n.nao_lida = not n.lida
            n.link = n.url or "#"
    return itens


def notificacoes(request):
    """Alimenta o sino de notificações do topo com as mais recentes e a contagem
    de não lidas. Usuários internos (TI) veem tudo pelo `base.html`; fornecedores
    veem só as notificações da própria empresa pelo `portal_base.html`."""
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}
    try:
        from .models import Notificacao
        perfil = getattr(user, "perfil_fornecedor", None)
        if perfil is not None and getattr(perfil, "ativo", False):
            # Fornecedor: só as notificações da própria empresa.
            base = Notificacao.objects.filter(fornecedor=perfil.fornecedor)
            recentes = _prep(base[:12], portal=True)
            nao_lidas = base.filter(lida_fornecedor=False).count()
            return {
                "notificacoes_recentes": recentes,
                "notificacoes_nao_lidas": nao_lidas,
                "notif_marcar_url": "portal_notificacoes_marcar_lidas",
            }
        # Interno: as do ambiente da tela (Zelo TI ou Zelo Agro) — o sino não
        # mistura os dois. Ver `ambiente_service.notificacoes_do_ambiente`.
        from services.ambiente_service import notificacoes_do_ambiente
        chave = getattr(request, "ambiente", None) or "ti"
        base = notificacoes_do_ambiente(Notificacao.objects.all(), chave)
        recentes = _prep(base[:12], portal=False)
        nao_lidas = base.filter(lida=False).count()
        return {
            "notificacoes_recentes": recentes,
            "notificacoes_nao_lidas": nao_lidas,
            "notif_marcar_url": "notificacoes_marcar_lidas",
        }
    except Exception:
        return {}


def licencas_pendencias(request):
    """
    Contadores dos badges "Saúde" e "Renovações" no subnav do módulo.

    Assento preso em colaborador desligado é despesa recorrente, e informação
    que só aparece quando alguém decide procurar não é encontrada — por isso o
    número fica à vista em todas as telas do módulo.

    Duas contenções deliberadas, porque context processor roda em TODA
    requisição do sistema:

      • **sai imediatamente fora de `/licencas`** — o badge só existe no subnav
        daquele módulo, e apurar isso no dashboard, na lista de equipamentos ou
        no portal do fornecedor seria trabalho jogado fora em cada page-load;
      • **cache curto** — a apuração varre o razão de movimentações. Dois
        minutos é mais do que suficiente para um número que serve de alerta (o
        painel em si sempre calcula na hora) e evita repetir a varredura a cada
        clique dentro do módulo.

    Falha em silêncio: um badge nunca pode derrubar a navegação do módulo.
    """
    caminho = getattr(request, "path", "") or ""
    if not caminho.startswith("/licencas"):
        return {}

    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}

    try:
        from django.core.cache import cache

        chave = "licencas_badges_subnav"
        valor = cache.get(chave)
        if valor is None:
            from services import licenca_service as ls

            # As duas apurações saem na MESMA passada e no MESMO cache: são
            # dois badges vizinhos no mesmo trilho, e cachear separado faria
            # um deles varrer o razão de novo no mesmo page-load.
            valor = {
                "problemas": len(ls.assentos_orfaos()) + len(ls.conferir_consistencia()),
                # Só o que vence em até 30 dias OU já venceu entra no badge.
                # Com 90 dias o número vira paisagem — fica aceso sempre e
                # deixa de significar "olhe isto agora", que é a única coisa
                # que um badge sabe dizer.
                "renovacoes": len(ls.renovacoes(dias=30)),
            }
            cache.set(chave, valor, 120)
        return {
            "licencas_problemas": valor["problemas"],
            "licencas_renovacoes": valor["renovacoes"],
        }
    except Exception:
        return {}


def perfil_acesso(request):
    """
    Alimenta o chip de perfil do topo com o papel REAL do usuário.

    Era uma string fixa "Administrador" no base.html — todo mundo, inclusive
    fornecedor e visualizador de TV, se via como administrador. O cálculo fica
    em services/perfil_acesso_service.py (fonte de verdade: grupo Django +
    perfil externo, a mesma do middleware).
    """
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}
    try:
        from services.perfil_acesso_service import perfil_de_acesso
        return {"perfil_acesso": perfil_de_acesso(user)}
    except Exception:
        # O chip não pode derrubar a tela: sem o contexto, o template cai no
        # rótulo neutro do `{% if %}`.
        return {}


def acesso_restrito(request):
    """
    Perfil de acesso restrito do usuário (Portal de Contratações / Consulta
    Quiosque) + as capacidades que os templates consultam.

    A régua é `services/acesso_restrito_service.py` — fonte única das três
    camadas. Aqui é só o encanamento até o template.

    Falha em silêncio com as capacidades em `True`: o padrão tem de ser o do
    usuário interno, que é quem abre 99% das telas. Um erro aqui não pode
    esconder botão de quem tem direito a ele — e também não libera nada, porque
    quem autoriza de verdade é o middleware + o decorador.
    """
    try:
        from services.acesso_restrito_service import contexto_restrito
        return contexto_restrito(request)
    except Exception:
        return {
            "acesso_restrito": None,
            "pode_gerenciar_contratacao": True,
            "pode_gerenciar_quiosque": True,
            "pode_solicitar_estrutura": True,
            "pode_preencher_vaga": True,
            "pode_acompanhar_chamados": True,
        }


def ambiente(request):
    """
    Ambiente da vez (Zelo TI / Zelo Agro), os ambientes que o usuário alcança
    (seletor da barra superior) e o menu lateral do Agro.

    A régua é `services/ambiente_service.py`; aqui é só o encanamento. Falha em
    silêncio para o TI, que é o padrão de todo usuário interno: um erro aqui
    não pode derrubar a tela — e também não libera nada, porque quem autoriza
    é o `AmbienteMiddleware` + o decorador da view.
    """
    try:
        from services.ambiente_service import contexto
        return contexto(request)
    except Exception:
        return {"ambiente_atual": None, "ambientes_disponiveis": (), "nav_ambiente": ()}
