# -*- coding: utf-8 -*-
"""
Assinatura digital dos termos de responsabilidade — regra de negócio.

Fluxo completo (três atos, dois atores):

  1. PREPARAR  (TI, autenticado)   `criar_sessao`
     O TI escolhe o colaborador, o(s) equipamento(s) e os dados do termo.
     Nasce uma `TermoAssinatura` PENDENTE com um token opaco de uso único e
     validade curta. O token puro é devolvido UMA vez (QR Code + link).

  2. ASSINAR   (colaborador, anônimo, no tablet)   `registrar_assinatura`
     O tablet abre `/termos/assinar/<token>/`, mostra o termo na íntegra,
     coleta o traço no canvas e envia. O service valida, grava o traço,
     emite o PDF, calcula o SHA-256 e fecha a sessão como ASSINADO.

  3. ARQUIVAR  (automático)
     O PDF é anexado também ao `termo_pdf` de cada `MovimentacaoItem`
     vinculada, para o Repositório de Termos e a cascata de desligamento
     continuarem funcionando sem saber que a assinatura veio de um tablet.

Por que a tela de assinatura é anônima
--------------------------------------
Quem assina é o colaborador, que não tem login no sistema (o `Usuario` do
cadastro NÃO é um `User` do Django — ver CLAUDE.md, "Key Models"). A
alternativa seria o TI deixar a própria sessão aberta no tablet para o
colaborador usar, o que é pior por dois motivos: a assinatura passaria a ser
feita de dentro de uma sessão privilegiada, e a trilha de auditoria registraria
o TI como autor do acesso. A proteção é o token (só o hash persistido), o prazo
curto, o uso único e o registro de IP/aparelho de quem assinou.

Idempotência e concorrência
---------------------------
`registrar_assinatura` roda dentro de `atomic` com `select_for_update()` e
reconfere o estado PENDENTE com a linha travada. Sem isso, dois toques no botão
"Assinar" (comum em tela sensível ao toque, onde o primeiro toque não dá
retorno visual imediato) gerariam dois PDFs e duas gravações no mesmo
`termo_pdf`.
"""

import base64
import binascii
import logging
import secrets
from datetime import timedelta
from io import BytesIO

from django.core.files.base import ContentFile
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger(__name__)

# Validade do link de assinatura. A entrega acontece com o colaborador na
# frente do TI, então o prazo útil é de minutos, não de dias: quanto mais curto,
# menor a janela em que um link vazado ainda assina algo. O teto de 24h existe
# para a entrega combinada para "depois do almoço" não obrigar a refazer a
# sessão.
VALIDADE_MINUTOS_PADRAO = 30
VALIDADE_MINUTOS_MIN = 5
VALIDADE_MINUTOS_MAX = 1440

# Teto do PNG do traço recebido do tablet (base64 decodificado). Uma assinatura
# de canvas 1000x300 em PNG fica na casa de 20-60 KB; 2 MB já é folgado e
# impede que um cliente manipulado use o endpoint anônimo para empurrar
# arquivo grande para o disco do servidor.
TAMANHO_MAX_TRACO_BYTES = 2 * 1024 * 1024

# Dimensões mínimas do PNG recebido: abaixo disso não é assinatura, é um toque
# acidental na tela. O cliente também valida (contando os pontos do traço),
# mas o servidor não pode confiar no cliente.
LARGURA_MIN_TRACO = 120
ALTURA_MIN_TRACO = 40

_ALFABETO_CODIGO = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # sem 0/O e 1/I


# ---------------------------------------------------------------------------
# Token e código de verificação
# ---------------------------------------------------------------------------

def _hash_token(token: str) -> str:
    import hashlib
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _gerar_token() -> str:
    return secrets.token_urlsafe(32)


def _gerar_codigo_verificacao() -> str:
    """Código legível no formato ABCD-EFGH-JKLM, para ser ditado por telefone
    e digitado na tela de verificação sem ambiguidade de caractere."""
    from ProjetoEstoque.models import TermoAssinatura

    for _ in range(12):
        bruto = "".join(secrets.choice(_ALFABETO_CODIGO) for _ in range(12))
        codigo = f"{bruto[:4]}-{bruto[4:8]}-{bruto[8:]}"
        if not TermoAssinatura.objects.filter(codigo_verificacao=codigo).exists():
            return codigo
    # 32^12 combinações: chegar aqui significa um problema real de geração
    # aleatória, e inventar um fallback silencioso esconderia isso.
    raise RuntimeError("Não foi possível gerar um código de verificação único.")


def ip_do_request(request) -> str | None:
    """IP de origem, respeitando proxy reverso. Mesma leitura do
    `services/seguranca_service.py` — o acesso externo passa por túnel."""
    if request is None:
        return None
    encaminhado = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if encaminhado:
        return encaminhado.split(",")[0].strip()[:45] or None
    return request.META.get("REMOTE_ADDR") or None


# ---------------------------------------------------------------------------
# Snapshot do termo
# ---------------------------------------------------------------------------

def _texto(valor, default="") -> str:
    if valor is None:
        return default
    valor = str(valor).strip()
    return valor or default


def _snapshot_equipamentos(itens) -> list[dict]:
    """
    Congela o que será APRESENTADO como lista de equipamentos.

    `compartilhado` e `codetentores` entram aqui porque mudam o que a pessoa
    está assinando: num equipamento de uso compartilhado ela NÃO é a única
    responsável, e o termo tem de dizer isso na cara — assinar um texto de
    responsabilidade exclusiva por um tablet que outras dez pessoas usam seria
    pedir uma declaração falsa.

    `codetentores` é CONTAGEM, não lista de nomes, de propósito: a via vai para
    o e-mail pessoal do signatário, e quem mais usa o equipamento é informação
    da operação — não precisa virar um cadastro de colegas num documento que
    sai do sistema.
    """
    from ProjetoEstoque.models import ItemColaborador

    compartilhados = [item.pk for item in itens if getattr(item, "compartilhado", False)]
    detentores = {}
    if compartilhados:
        for vinculo in ItemColaborador.objects.filter(
            item_id__in=compartilhados, ativo=True,
        ).values_list("item_id", flat=True):
            detentores[vinculo] = detentores.get(vinculo, 0) + 1

    return [{
        "id": item.pk,
        "nome": _texto(item.nome, "—"),
        "marca": _texto(getattr(item, "marca", None)),
        "modelo": _texto(getattr(item, "modelo", None)),
        "numero_serie": _texto(getattr(item, "numero_serie", None)),
        "patrimonio": _texto(getattr(item, "patrimonio", None)),
        "centro_custo": _texto(getattr(getattr(item, "centro_custo", None), "departamento", None)),
        "compartilhado": bool(getattr(item, "compartilhado", False)),
        "codetentores": detentores.get(item.pk, 0),
    } for item in itens]


def _numero_termo_automatico(itens, colaborador) -> str:
    """
    Mesma regra de numeração do termo em `.docx`:
        {nº de série} - {nome do colaborador} - {centro de custo}

    Reaproveitada de propósito, para o mesmo termo ter o mesmo número nas duas
    vias (papel e digital) — se a TI gerar o .docx e depois colher a assinatura
    digital, os dois documentos têm de se referir ao mesmo ato. Com vários
    equipamentos, a série sai (não há uma só) e entra a contagem.
    """
    partes = []

    if len(itens) == 1:
        serie = _texto(getattr(itens[0], "numero_serie", None))
        if serie:
            partes.append(serie)
    elif len(itens) > 1:
        partes.append(f"{len(itens)} EQUIPAMENTOS")

    nome = _texto(getattr(colaborador, "nome", None))
    if nome:
        partes.append(nome)

    centro_custo = (
        _texto(getattr(getattr(colaborador, "centro_custo", None), "departamento", None))
        or (_texto(getattr(getattr(itens[0], "centro_custo", None), "departamento", None)) if itens else "")
    )
    if centro_custo:
        partes.append(centro_custo)

    return " - ".join(partes)


def _rotulo_estabelecimento(valor: str) -> str:
    from ProjetoEstoque.forms import ESTABELECIMENTO_CHOICES
    return dict(ESTABELECIMENTO_CHOICES).get(valor, "")


def _montar_dados(*, tipo, colaborador, itens, form_data, user) -> dict:
    """
    Snapshot do termo: tudo o que será APRESENTADO ao signatário, resolvido
    agora e congelado. Inclui o texto legal extraído do modelo base, porque o
    que vale é o texto que estava na tela quando ele assinou.
    """
    from services.termo_pdf_service import (
        TEXTO_ACEITE_ELETRONICO,
        carregar_texto_legal,
        sha256_bytes,
    )

    texto_legal = carregar_texto_legal(tipo)
    corpo = "\n".join([texto_legal["titulo"], *texto_legal["clausulas"], texto_legal.get("fecho", "")])

    return {
        "numero_chamado": _texto(form_data.get("numero_chamado")),
        "acessorios": _texto(form_data.get("acessorios")),
        "observacoes": _texto(form_data.get("observacoes")),
        "estabelecimento": _texto(form_data.get("estabelecimento")),
        "estabelecimento_label": _rotulo_estabelecimento(_texto(form_data.get("estabelecimento"))),
        "responsavel_ti_nome": _texto(form_data.get("responsavel_ti_nome")),
        "equipamentos": _snapshot_equipamentos(itens),
        "colaborador": {
            "id": colaborador.pk,
            "nome": _texto(colaborador.nome),
            "matricula": _texto(getattr(colaborador, "matricula", None)),
            "email": _texto(getattr(colaborador, "email", None)),
            "funcao": _texto(getattr(getattr(colaborador, "funcao", None), "nome", None)),
            "centro_custo": _texto(getattr(getattr(colaborador, "centro_custo", None), "departamento", None)),
            "localidade": _texto(getattr(getattr(colaborador, "localidade", None), "local", None)),
        },
        "texto_legal": texto_legal,
        "texto_legal_clausulas": len(texto_legal["clausulas"]),
        # Impressão digital do texto assinado: 12 hex bastam para distinguir
        # revisões do modelo base, e cabem numa linha do comprovante.
        "texto_legal_hash": sha256_bytes(corpo.encode("utf-8"))[:12],
        "texto_aceite": TEXTO_ACEITE_ELETRONICO,
        "criado_por": (user.get_full_name() or user.username) if user and user.is_authenticated else "—",
        "criado_em": timezone.localtime(timezone.now()).strftime("%d/%m/%Y %H:%M"),
    }


# ---------------------------------------------------------------------------
# 1. Preparar a sessão
# ---------------------------------------------------------------------------

def validar_pre_condicoes(*, tipo, colaborador, itens, movimentacoes=None) -> list[str]:
    """
    Impedimentos que fariam a movimentação falhar DEPOIS da assinatura.

    Esta checagem existe por uma razão de fluxo, não de rigor: quem assina é o
    colaborador, de pé, com o equipamento na mão. Descobrir só naquele momento
    que o equipamento está sem centro de custo significa o colaborador ter
    assinado e o sistema não conseguir concluir — a pior hora possível para
    aparecer um erro de cadastro. Então o bloqueio acontece na PREPARAÇÃO, com
    o TI na frente do computador e podendo corrigir.

    As regras espelham `MovimentacaoItemForm.clean` para `transferencia`; se
    aquele formulário ganhar uma exigência nova para entrega, ela precisa
    aparecer aqui também.

    `movimentacoes` são as transferências JÁ registradas que este termo vem
    documentar (ver `_movimentacoes_sugeridas`). Os itens que aparecem nelas
    ficam de fora da checagem de posse duplicada: ali a entrega já aconteceu
    e o que falta é o papel — barrar seria impedir justamente o caso de uso de
    regularizar uma pendência que a própria tela aponta.
    """
    problemas = []

    if tipo != "entrega":
        return problemas

    if colaborador is not None and not colaborador.localidade_id:
        problemas.append(
            f"O colaborador {colaborador.nome} está sem localidade no cadastro. "
            "A entrega precisa dela para registrar o destino do equipamento."
        )

    ja_documentados = {
        getattr(mov, "item_id", None) for mov in (movimentacoes or [])
    }

    for item in itens:
        compartilhado = bool(getattr(item, "compartilhado", False))

        # Item compartilhado é isento: ele não restaura centro de custo na
        # devolução (é ativo de vários setores) — mesma isenção do formulário.
        if not item.centro_custo_id and not compartilhado:
            problemas.append(
                f'O equipamento "{item.nome}" está sem centro de custo. Defina-o na '
                "ficha do equipamento antes de entregar — sem ele, a devolução não "
                "saberia para qual centro de custo retornar o item."
            )

        # Compartilhado pode ir para VÁRIOS colaboradores ao mesmo tempo — é
        # exatamente para isso que ele existe. O que não faz sentido é entregar
        # DE NOVO para quem já está com ele: o vínculo em `ItemColaborador` tem
        # unicidade por (item, colaborador, ativo=True), então o segundo termo
        # não abriria vínculo novo — produziria uma assinatura que não muda
        # nada, e a devolução depois encerraria um vínculo só, deixando o outro
        # termo pendurado sem par.
        if (
            compartilhado
            and colaborador is not None
            and item.pk not in ja_documentados
            and _ja_vinculado(item, colaborador)
        ):
            problemas.append(
                f'O equipamento compartilhado "{item.nome}" já está em posse de '
                f"{colaborador.nome} — não há o que entregar de novo. Para encerrar "
                "essa posse, monte um termo de DEVOLUÇÃO; para entregá-lo a outra "
                "pessoa, troque o colaborador do termo (o equipamento continua com "
                "quem já está com ele)."
            )

    return problemas


def _ja_vinculado(item, colaborador) -> bool:
    """Existe vínculo ATIVO deste equipamento compartilhado com este
    colaborador? (`ItemColaborador` é a fonte da posse de compartilhado — ver
    `MovimentacaoEstoqueService._sync_vinculo_compartilhado`.)"""
    from ProjetoEstoque.models import ItemColaborador

    return ItemColaborador.objects.filter(
        item=item, colaborador=colaborador, ativo=True
    ).exists()


@transaction.atomic
def criar_sessao(*, tipo, colaborador, itens, form_data, user, request=None,
                 validade_minutos=None, movimentacoes=None, desligamento=False) -> dict:
    """
    Cria a sessão de assinatura e devolve
    ``{"termo": TermoAssinatura, "token": str, "url": str, "qr_base64": str}``.

    O token puro só existe nesta resposta — a tela que a consome tem de exibir
    o QR e o link ali mesmo. Reabrir o painel depois NÃO recupera o token (só o
    hash está no banco); para isso existe `regerar_token`, que invalida o
    anterior e registra o motivo no histórico de auditoria da sessão.
    """
    from ProjetoEstoque.models import StatusAssinaturaTermoChoices, TermoAssinatura

    if tipo not in ("entrega", "devolucao"):
        raise ValueError(f"Tipo de termo inválido: {tipo!r}")
    if colaborador is None:
        raise ValueError("Selecione o colaborador que irá assinar o termo.")

    itens = list(itens or [])
    if not itens:
        raise ValueError("Selecione ao menos um equipamento para o termo.")

    problemas = validar_pre_condicoes(
        tipo=tipo, colaborador=colaborador, itens=itens, movimentacoes=movimentacoes,
    )
    if problemas:
        raise ValueError(" ".join(problemas))

    validade = min(max(int(validade_minutos or VALIDADE_MINUTOS_PADRAO),
                       VALIDADE_MINUTOS_MIN), VALIDADE_MINUTOS_MAX)

    numero_termo = _texto(form_data.get("numero_termo")) or _numero_termo_automatico(itens, colaborador)
    token = _gerar_token()

    termo = TermoAssinatura.objects.create(
        tipo=tipo,
        colaborador=colaborador,
        status=StatusAssinaturaTermoChoices.PENDENTE,
        token_hash=_hash_token(token),
        expira_em=timezone.now() + timedelta(minutes=validade),
        numero_termo=numero_termo[:200],
        dados=_montar_dados(tipo=tipo, colaborador=colaborador, itens=itens,
                            form_data=form_data, user=user),
        codigo_verificacao=_gerar_codigo_verificacao(),
        desligamento=bool(desligamento),
        criado_por=user if (user and user.is_authenticated) else None,
        atualizado_por=user if (user and user.is_authenticated) else None,
    )
    termo.itens.set(itens)
    if movimentacoes:
        termo.movimentacoes.set(movimentacoes)

    return {"termo": termo, "validade_minutos": validade, **link_de_assinatura(termo, token, request)}



def criar_sessao_desligamento(*, colaborador, form_data, user, request=None,
                              validade_minutos=None) -> dict:
    """
    Sessão de assinatura de DESLIGAMENTO.

    Diferente da devolução avulsa, aqui o TI **não escolhe** os equipamentos: o
    termo cobre tudo o que o colaborador tem em mãos, apurado na hora por
    `DesligamentoService.ativos_do_usuario` — a mesma fonte que a cascata da
    tela de movimentações usa. Deixar o operador selecionar seria convidar ao
    erro que o desligamento não perdoa: equipamento esquecido fora do termo é
    equipamento que continua no nome de alguém que não trabalha mais aqui.

    O resumo das licenças entra no snapshot para o colaborador VER no tablet o
    que está sendo encerrado junto — elas não são "equipamento", mas são parte
    do que ele está devolvendo, e assinar sem saber disso seria assinar no
    escuro.
    """
    from services.desligamento_service import DesligamentoService

    if colaborador is None:
        raise ValueError("Selecione o colaborador que está sendo desligado.")

    ativos = DesligamentoService.ativos_do_usuario(colaborador)
    itens = list(ativos["itens"])
    licencas = list(ativos["licencas"])

    if not itens and not licencas:
        raise ValueError(
            f"{colaborador.nome} não tem equipamento nem licença ativa — "
            "não há o que assinar. Faça o desligamento pela ficha do colaborador."
        )

    dados_extra = dict(form_data or {})
    dados_extra.setdefault(
        "observacoes",
        "Devolução consolidada por desligamento do colaborador. A assinatura "
        "encerra o vínculo: devolve todos os equipamentos e remove todas as "
        "licenças de software.",
    )

    sessao = criar_sessao(
        tipo="devolucao",
        colaborador=colaborador,
        itens=itens,
        form_data=dados_extra,
        user=user,
        request=request,
        validade_minutos=validade_minutos,
        desligamento=True,
    )

    # Licenças no snapshot: o tablet mostra, o PDF imprime, e o registro
    # guarda o que existia no momento da assinatura (depois elas somem).
    termo = sessao["termo"]
    termo.dados["licencas"] = [{
        "nome": str(getattr(lic, "licenca", None) or getattr(lic, "nome", "") or "—"),
        "detalhe": str(getattr(lic, "observacao", "") or ""),
    } for lic in licencas]
    termo.dados["desligamento"] = True
    termo.save(update_fields=["dados", "updated_at"])

    return sessao


def link_de_assinatura(termo, token: str, request=None) -> dict:
    """URL absoluta de assinatura + QR Code embutível. Sem `request` devolve a
    URL relativa (caso de uso em teste e em management command)."""
    from services.quiosque_service import gerar_qrcode_data_uri

    caminho = reverse("termo_assinar", args=[token])
    url = request.build_absolute_uri(caminho) if request is not None else caminho
    return {"token": token, "url": url, "qr_base64": gerar_qrcode_data_uri(url, tamanho_px=280)}


@transaction.atomic
def regerar_token(termo, *, user, request=None) -> dict:
    """
    Emite um token novo para uma sessão ainda pendente e invalida o anterior
    (o hash antigo é sobrescrito, então o link velho deixa de resolver).

    Existe porque o token puro não é recuperável: se o TI fechou a tela antes
    de o colaborador assinar, ou o link expirou, refazer a sessão inteira
    perderia o snapshot já conferido. O prazo é reiniciado a partir de agora.

    O estado é lido do BANCO com a linha travada, não da instância recebida: o
    objeto que chega pode estar velho (o colaborador pode ter acabado de
    assinar no tablet enquanto o TI tinha o painel aberto), e confiar no
    `status` em memória emitiria um link novo para um termo já assinado.
    """
    from ProjetoEstoque.models import StatusAssinaturaTermoChoices, TermoAssinatura

    travado = TermoAssinatura.objects.select_for_update().get(pk=termo.pk)

    if travado.status != StatusAssinaturaTermoChoices.PENDENTE:
        raise ValueError("Só é possível gerar um novo link para um termo pendente de assinatura.")

    token = _gerar_token()
    travado.token_hash = _hash_token(token)
    travado.expira_em = timezone.now() + timedelta(minutes=VALIDADE_MINUTOS_PADRAO)
    travado.atualizado_por = user if (user and user.is_authenticated) else None
    travado.save(update_fields=["token_hash", "expira_em", "atualizado_por", "updated_at"])

    return {"termo": travado, "validade_minutos": VALIDADE_MINUTOS_PADRAO,
            **link_de_assinatura(travado, token, request)}


# ---------------------------------------------------------------------------
# Proteção da porta anônima
# ---------------------------------------------------------------------------
# `/termos/assinar/<token>/` é a única rota do sistema que grava arquivo e emite
# documento assinado SEM login — por desenho (o colaborador não tem conta; ver
# o cabeçalho deste módulo). O token de 32 bytes não é adivinhável por força
# bruta, então o risco não é acertar o token: é o CUSTO de cada tentativa.
#
# `resolver_token` varre todas as sessões assináveis e compara uma a uma em
# tempo constante — a varredura é o que torna a comparação segura, e é também o
# que faz cada requisição custar uma consulta. Sem teto, um laço simples
# apontado para essa URL vira carga de banco contínua num SQLite que atende o
# sistema inteiro: não derruba a assinatura, derruba o Controle de Estoque.
#
# O teto é por IP e deliberadamente FROUXO, pelo mesmo motivo documentado em
# `recuperacao_senha_service`: o acesso externo passa por túnel e, se ele não
# repassar o `X-Forwarded-For`, toda a empresa chega com o mesmo `REMOTE_ADDR`.
# Um tablet assinando faz um punhado de requisições; 120 em 10 minutos nunca
# atrapalha o uso legítimo e corta o disparo automatizado.
_LIMITE_TOKEN_POR_IP = 120
_JANELA_TOKEN_SEGUNDOS = 600

_log_seguranca = logging.getLogger("seguranca")


def acesso_token_dentro_do_limite(request) -> bool:
    """
    `True` se esta tentativa de abrir/usar um link de assinatura pode seguir.

    Conta por IP numa janela curta. Quem estoura recebe a MESMA tela genérica
    de link indisponível — distinguir "bloqueado por excesso" de "token
    inválido" entregaria ao sondador a informação de que vale insistir de outro
    endereço.
    """
    from django.core.cache import cache

    ip = ip_do_request(request) or "desconhecido"
    chave = f"termo_assinar_token_{ip}"

    atual = cache.get(chave, 0)
    if atual >= _LIMITE_TOKEN_POR_IP:
        _log_seguranca.warning(
            "Assinatura de termo: teto de tentativas atingido para o IP %s "
            "(%s em %s s) — pedido recusado.",
            ip, atual, _JANELA_TOKEN_SEGUNDOS,
        )
        return False

    # `set` com o TTL inteiro a cada pedido faria a janela nunca fechar para
    # quem insiste; só o primeiro pedido define o prazo.
    if atual == 0:
        cache.set(chave, 1, _JANELA_TOKEN_SEGUNDOS)
    else:
        try:
            cache.incr(chave)
        except ValueError:
            # A chave expirou entre o get e o incr.
            cache.set(chave, 1, _JANELA_TOKEN_SEGUNDOS)
    return True


def resolver_token(token: str):
    """
    `TermoAssinatura` pendente e no prazo correspondente ao token puro, ou
    None.

    Varre só as sessões atualmente assináveis — são poucas (validade de
    minutos) — e compara com `compare_digest`, em tempo constante. Mesmo padrão
    do token de device e do link de instalador do Quiosque. Comparar por
    `filter(token_hash=...)` seria mais curto, mas abre uma diferença de tempo
    mensurável entre "hash existe" e "hash não existe" num endpoint anônimo.
    """
    from ProjetoEstoque.models import StatusAssinaturaTermoChoices, TermoAssinatura

    if not token:
        return None

    alvo = _hash_token(token)
    candidatos = (
        TermoAssinatura.objects
        .filter(status=StatusAssinaturaTermoChoices.PENDENTE, expira_em__gt=timezone.now())
        .select_related("colaborador", "colaborador__centro_custo", "colaborador__funcao",
                        "colaborador__localidade")
    )
    for termo in candidatos:
        if secrets.compare_digest(termo.token_hash, alvo):
            return termo
    return None


# ---------------------------------------------------------------------------
# 2. Registrar a assinatura
# ---------------------------------------------------------------------------

def _decodificar_traco(data_url: str) -> bytes:
    """
    Converte o `data:image/png;base64,...` do canvas em bytes de PNG válidos.

    Valida o formato DE VERDADE (abre com Pillow e confere as dimensões) em vez
    de confiar no prefixo do data URL: o endpoint é anônimo, e "começa com
    data:image/png" é trivial de falsificar. Levanta `ValueError` com mensagem
    em pt-BR, que a view devolve ao tablet.
    """
    if not data_url:
        raise ValueError("A assinatura não foi enviada.")

    cabecalho, _, corpo = str(data_url).partition(",")
    if "base64" not in cabecalho or not corpo:
        raise ValueError("Formato de assinatura não reconhecido.")

    try:
        dados = base64.b64decode(corpo, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("Formato de assinatura não reconhecido.")

    if not dados:
        raise ValueError("A assinatura chegou vazia.")
    if len(dados) > TAMANHO_MAX_TRACO_BYTES:
        raise ValueError("A imagem da assinatura é grande demais.")

    from PIL import Image as PILImage

    try:
        with PILImage.open(BytesIO(dados)) as img:
            formato = (img.format or "").upper()
            largura, altura = img.size
            img.verify()
    except Exception:
        raise ValueError("A assinatura enviada não é uma imagem válida.")

    if formato != "PNG":
        raise ValueError("A assinatura precisa ser enviada em PNG.")
    if largura < LARGURA_MIN_TRACO or altura < ALTURA_MIN_TRACO:
        raise ValueError("A assinatura ficou pequena demais. Assine novamente, ocupando o quadro.")

    return dados


def registrar_resultado_via(termo, *, enviado: bool, mensagem: str, tem_email: bool = True):
    """
    Grava na LINHA o resultado da tentativa de enviar a via por e-mail.

    Isto é coluna, e não só log, porque "não recebi o e-mail" tem duas causas
    de providência oposta — o sistema não mandou, ou mandou e a mensagem se
    perdeu no caminho — e antes disto era impossível distinguir as duas. O
    sucesso ia para `logger.info`, que o `settings.LOGGING` manda só para o
    CONSOLE (o `arquivo_erros` começa em WARNING): quando a janela do servidor
    fecha, o rastro vai com ela. A falha ia para arquivo, mas num log rotativo
    que ninguém abre ao olhar a tela do termo.

    Agora a tela do termo responde a pergunta, e cada reenvio pedido pelo TI
    soma em `via_email_tentativas`.

    Usa `update_fields` de propósito: `via_email_*` está fora de
    `CAMPOS_IMUTAVEIS_APOS_ASSINATURA` (ele descreve o que aconteceu DEPOIS da
    assinatura), e restringir os campos deixa isso explícito — além de não
    reescrever o resto da linha a partir de uma instância que pode estar velha.
    """
    from ProjetoEstoque.models import StatusViaEmailChoices

    if not tem_email:
        situacao = StatusViaEmailChoices.SEM_EMAIL
    elif enviado:
        situacao = StatusViaEmailChoices.ENVIADA
    else:
        situacao = StatusViaEmailChoices.FALHOU

    termo.via_email_status = situacao
    termo.via_email_em = timezone.now()
    termo.via_email_tentativas = (termo.via_email_tentativas or 0) + 1
    termo.via_email_detalhe = _texto(mensagem)[:300]
    termo.save(update_fields=[
        "via_email_status", "via_email_em", "via_email_tentativas",
        "via_email_detalhe", "updated_at",
    ])
    return termo


# Duas tentativas, com uma pausa curta entre elas. O que se vê na prática com
# Microsoft 365 é recusa momentânea (limite de submissão, conexão derrubada no
# meio do handshake) que passa na segunda — e uma tentativa só transformava isso
# em "a via nunca chegou". Mais do que duas não ajuda: a essa altura o problema
# não é momentâneo, e o TI tem o botão de reenviar.
_TENTATIVAS_VIA = 2
_ESPERA_ENTRE_TENTATIVAS_S = 3


def _enviar_via_em_segundo_plano(termo_pk: int) -> None:
    """
    Dispara a via do termo para o e-mail do colaborador, depois do commit.

    Nunca propaga exceção: isto roda num `on_commit`, já fora da transação e
    depois de a resposta ter sido montada. Uma falha de SMTP aqui não pode
    derrubar nada — a assinatura já está gravada e é o que importa. O resultado
    vai para a linha do termo (ver `registrar_resultado_via`) e para o log, e o
    colaborador continua com o download do PDF na tela de conclusão.
    """
    import time

    from ProjetoEstoque.models import TermoAssinatura
    from services.email_alertas import enviar_termo_assinado_ao_colaborador

    try:
        termo = (
            TermoAssinatura.objects
            .select_related("colaborador")
            .filter(pk=termo_pk)
            .first()
        )
        if termo is None:
            return

        mensagem = ""
        for tentativa in range(1, _TENTATIVAS_VIA + 1):
            try:
                enviado, mensagem = enviar_termo_assinado_ao_colaborador(termo)
            except Exception as erro:
                enviado, mensagem = False, f"{type(erro).__name__}: {erro}"

            if enviado:
                registrar_resultado_via(termo, enviado=True, mensagem=mensagem)
                logger.info(
                    "Termo %s: via enviada automaticamente ao colaborador (tentativa %s).",
                    termo_pk, tentativa,
                )
                return

            if tentativa < _TENTATIVAS_VIA:
                logger.warning(
                    "Termo %s: tentativa %s de enviar a via falhou (%s). Repetindo.",
                    termo_pk, tentativa, mensagem,
                )
                time.sleep(_ESPERA_ENTRE_TENTATIVAS_S)

        registrar_resultado_via(termo, enviado=False, mensagem=mensagem)
        # ERROR, não WARNING: a via é o comprovante do colaborador, e o
        # `arquivo_erros` do settings é onde o TI vai procurar. Com o status na
        # linha, a lista de termos também passa a mostrar a pendência.
        logger.error(
            "Termo %s: via NÃO enviada depois de %s tentativas — %s",
            termo_pk, _TENTATIVAS_VIA, mensagem,
        )
    except Exception:
        logger.exception("Termo %s: falha inesperada ao enviar a via por e-mail.", termo_pk)


def reenviar_via(termo, *, user=None) -> tuple[bool, str]:
    """
    Reenvia a via ao colaborador, a pedido do TI, pela tela do termo.

    Existe porque o único reenvio que havia era o botão do TABLET, e ele só
    funciona na janela de 15 minutos da sessão de quem assinou (ver
    `CHAVE_RECIBO` nas views). Passada essa janela — que é o caso sempre que o
    problema só é notado depois — não havia como mandar a via de novo sem
    inventar um termo novo, o que criaria uma segunda assinatura para o mesmo
    ato.

    Síncrono: quem está esperando é o TI, olhando a tela, e precisa da
    resposta de verdade para saber se resolveu ou se o problema é o cadastro.
    """
    from ProjetoEstoque.models import StatusAssinaturaTermoChoices
    from services.email_alertas import enviar_termo_assinado_ao_colaborador

    if termo.status != StatusAssinaturaTermoChoices.ASSINADO:
        return False, "Só é possível enviar a via de um termo já assinado."

    if not (getattr(termo.colaborador, "email", "") or "").strip():
        registrar_resultado_via(
            termo, enviado=False, tem_email=False,
            mensagem="Colaborador sem e-mail no cadastro.",
        )
        return False, (
            f"{termo.colaborador.nome} não tem e-mail no cadastro. "
            "Preencha o e-mail na ficha do colaborador e tente de novo."
        )

    try:
        enviado, mensagem = enviar_termo_assinado_ao_colaborador(termo)
    except Exception as erro:
        enviado, mensagem = False, f"{type(erro).__name__}: {erro}"
        logger.exception("Termo %s: reenvio da via falhou.", termo.pk)

    registrar_resultado_via(termo, enviado=enviado, mensagem=mensagem)
    logger.info(
        "Termo %s: reenvio da via pedido por %s — %s",
        termo.pk, getattr(user, "username", "?"), "enviado" if enviado else mensagem,
    )
    return enviado, mensagem


def _executar_desligamento(termo, conteudo_pdf: bytes, nome_arquivo: str, user) -> list:
    """
    Desligamento assinado: a assinatura encerra o vínculo inteiro.

    Devolve TODOS os equipamentos ativos do colaborador, remove TODAS as
    licenças alocadas a ele e marca o cadastro como desligado — tudo pelo
    `DesligamentoService`, que já é a fonte única desse fluxo no sistema (é o
    mesmo caminho da cascata disparada pelo checkbox "O colaborador está sendo
    desligado?" na tela de Movimentações).

    Reimplementar aqui significaria duas versões da regra de desligamento, e a
    divergência apareceria como colaborador desligado com licença ainda
    alocada — custo que só aparece na auditoria do contrato de software.

    O PDF assinado vai junto: cada devolução gerada pela cascata recebe o termo
    anexado, então o Repositório de Termos mostra o comprovante de todas elas,
    não só da primeira.
    """
    from services.desligamento_service import DesligamentoService

    resultado = DesligamentoService.desligar_e_liberar_ativos(
        usuario=termo.colaborador,
        executado_por=user,
        termo_pdf_bytes=conteudo_pdf,
        termo_pdf_nome=nome_arquivo,
    )

    # As movimentações que a cascata criou agora são as do termo: liga as duas
    # pontas para a tela de detalhe mostrar o que a assinatura de fato moveu.
    from ProjetoEstoque.models import MovimentacaoItem

    criadas = list(
        MovimentacaoItem.objects.filter(
            usuario=termo.colaborador,
            tipo_movimentacao="transferencia",
            tipo_transferencia="devolucao",
            created_at__gte=termo.assinado_em,
        )
    )
    if criadas:
        termo.movimentacoes.add(*criadas)

    logger.info(
        "Desligamento assinado: colaborador=%s itens_devolvidos=%s licencas_removidas=%s",
        termo.colaborador_id, resultado.get("itens"), resultado.get("licencas"),
    )
    return criadas


def _registrar_movimentacoes(termo, conteudo_pdf: bytes, nome_arquivo: str, user) -> list:
    """
    Cria a movimentação de estoque de cada equipamento do termo — é o que faz
    a assinatura ter efeito real no patrimônio.

    ENTREGA: o equipamento passa para o centro de custo e a localidade do
    colaborador, e o status vai para Ativo.
    DEVOLUÇÃO: o centro de custo original é restaurado e o status volta para
    Backup.

    **Nada disso é implementado aqui.** Tudo passa por
    `MovimentacaoEstoqueService.registrar`, que já é a fonte única dessas
    regras no sistema (`services/movimentacao_service.py`): ele resolve o CC do
    detentor, trata equipamento compartilhado pelo vínculo em `ItemColaborador`,
    restaura o CC da última entrega na devolução, recalcula PMB e dispara os
    e-mails de movimentação. Reescrever o efeito "na mão" aqui criaria um
    segundo caminho que divergiria do primeiro na próxima regra nova — e a
    divergência apareceria como equipamento no centro de custo errado, meses
    depois, sem ninguém saber de onde veio.

    O PDF assinado entra como o `termo_pdf` da movimentação, que o formulário
    exige para transferência. Um `SimpleUploadedFile` NOVO por movimentação: o
    arquivo é consumido na gravação, e reaproveitar a instância grava zero byte
    na segunda.
    """
    from django.core.files.uploadedfile import SimpleUploadedFile

    from ProjetoEstoque.forms import MovimentacaoItemForm
    from services.movimentacao_service import MovimentacaoEstoqueService

    dados = termo.dados or {}
    colaborador = termo.colaborador
    criadas = []

    for item in termo.itens.all():
        base = {
            "tipo_movimentacao": "transferencia",
            "tipo_transferencia": termo.tipo,
            "item": item.pk,
            "usuario": colaborador.pk,
            "quantidade": 1,
            "chamado": dados.get("numero_chamado") or "",
            "observacao": _observacao_da_movimentacao(termo, dados),
        }

        if termo.tipo == "entrega":
            # Localidade do colaborador é o destino pedido pela operação. Sem
            # ela, cai na localidade atual do equipamento em vez de falhar: a
            # pré-condição já barrou esse caso na criação da sessão, e aqui o
            # colaborador já assinou — travar agora seria o pior momento.
            destino = colaborador.localidade_id or item.localidade_id
            if destino:
                base["localidade_destino"] = destino
            if colaborador.centro_custo_id:
                base["centro_custo_destino"] = colaborador.centro_custo_id

        formulario = MovimentacaoItemForm(
            data=base,
            files={"termo_pdf": SimpleUploadedFile(
                nome_arquivo, conteudo_pdf, content_type="application/pdf"
            )},
        )

        if not formulario.is_valid():
            # Não derruba a assinatura: o documento já foi firmado e é prova.
            # O erro vai para o log e o termo fica sem movimentação vinculada,
            # visível como pendência no Repositório de Termos — bem melhor do
            # que perder a assinatura que o colaborador acabou de dar.
            logger.error(
                "Termo %s assinado, mas a movimentação do item %s não pôde ser "
                "registrada: %s",
                termo.pk, item.pk, formulario.errors.as_json(),
            )
            continue

        criadas.append(MovimentacaoEstoqueService.registrar(form=formulario, user=user))
        _garantir_status_final(item=item, tipo=termo.tipo, user=user)

    if criadas:
        termo.movimentacoes.add(*criadas)

    return criadas


def _garantir_status_final(*, item, tipo, user):
    """
    Fecha a lacuna entre o que o `MovimentacaoEstoqueService` promove sozinho e
    o estado que a operação pediu para o termo assinado.

    O service só faz a transição automática de **Backup → Ativo** na entrega e
    de **Ativo → Backup** na devolução. Equipamento que sai de "Estoque" (ou de
    "Pausado") — dois estados que o termo aceita entregar — ficava parado no
    status antigo: entregue ao colaborador, com o centro de custo já trocado, e
    ainda marcado como disponível em estoque. Na prática, aparecia na lista de
    "disponível para entrega" um aparelho que estava na mesa de alguém.

    Por que corrigir AQUI e não no service: mudar aquela condição alteraria o
    comportamento de todas as telas de movimentação de uma vez, incluindo
    fluxos que dependem do estado atual por motivos que não são deste módulo.
    A regra "ao assinar o termo, o equipamento fica Ativo (ou volta para
    Backup)" é do fluxo de assinatura, foi pedida para ele, e fica visível
    dentro dele.

    Equipamento COMPARTILHADO não segue "entrega → Ativo, devolução → Backup",
    porque ele tem vários detentores ao mesmo tempo: o status dele responde a
    outra pergunta — "ainda tem ALGUÉM com este equipamento?". Então o alvo sai
    da contagem de vínculos ativos em `ItemColaborador` (a fonte da posse de
    compartilhado), e não do tipo do termo: na entrega sempre sobra pelo menos
    o vínculo que acabou de abrir (Ativo); na devolução só cai para Backup
    quando o último detentor devolveu.
    """
    from ProjetoEstoque.models import Item, ItemColaborador, StatusItemChoices

    if getattr(item, "compartilhado", False):
        # Por que não deixar isto só com o `MovimentacaoEstoqueService`: lá a
        # promoção para Ativo só acontece saindo de **Backup**. Um tablet
        # compartilhado parado em "Estoque" (ou "Pausado") entregue a alguém
        # ficava com o vínculo aberto e o status ainda dizendo que estava
        # disponível no estoque — equipamento na mão de alguém e listado como
        # livre. Aqui o alvo é apurado pelo vínculo, então a origem não importa.
        resta_detentor = ItemColaborador.objects.filter(item=item, ativo=True).exists()
        alvo = StatusItemChoices.ATIVO if resta_detentor else StatusItemChoices.BACKUP
    else:
        alvo = StatusItemChoices.ATIVO if tipo == "entrega" else StatusItemChoices.BACKUP

    atual = Item.objects.filter(pk=item.pk).values_list("status", flat=True).first()
    if atual == alvo:
        return

    Item.objects.filter(pk=item.pk).update(
        status=alvo,
        atualizado_por=user if (user and getattr(user, "is_authenticated", False)) else None,
    )
    logger.info(
        "Termo assinado: status do item %s ajustado de %s para %s.", item.pk, atual, alvo,
    )


def _observacao_da_movimentacao(termo, dados) -> str:
    partes = [
        f"{'Entrega' if termo.tipo == 'entrega' else 'Devolução'} com assinatura digital "
        f"do colaborador (termo {termo.codigo_verificacao})."
    ]
    if (dados.get("acessorios") or "").strip():
        partes.append(f"Acessórios: {dados['acessorios'].strip()}")
    if (dados.get("observacoes") or "").strip():
        partes.append(dados["observacoes"].strip())
    return "\n".join(partes)


def _anexar_pdf_nas_movimentacoes(termo, conteudo: bytes, nome_arquivo: str) -> int:
    """
    Grava o PDF assinado no `termo_pdf` de cada movimentação vinculada.

    Um `ContentFile` por movimentação, de propósito: o `FileField.save()`
    consome o arquivo, e reaproveitar a mesma instância deixaria o segundo
    `save()` gravando zero byte — falha silenciosa, que só apareceria ao
    alguém abrir o PDF meses depois.

    Não sobrescreve `termo_pdf` já preenchido: se o TI anexou o escaneado do
    papel antes, aquele é o documento que a operação conhece, e trocá-lo por
    baixo seria perder prova.
    """
    anexados = 0
    for movimentacao in termo.movimentacoes.all():
        if movimentacao.termo_pdf:
            continue
        movimentacao.termo_pdf.save(nome_arquivo, ContentFile(conteudo), save=True)
        anexados += 1
    return anexados


def registrar_assinatura(*, termo, assinatura_colaborador, signatario_nome,
                         aceite=False, assinatura_responsavel=None,
                         signatario_cpf="", signatario_rg="", request=None):
    """
    Fecha a sessão: grava o traço, emite o PDF, calcula o hash e marca ASSINADO.

    Levanta `ValueError` (mensagem pronta para o usuário) quando a sessão não
    pode mais ser assinada ou a assinatura é inválida.

    CPF E RG NÃO SÃO GRAVADOS
    -------------------------
    Eles entram **apenas** no PDF emitido e são descartados quando esta função
    retorna: não há coluna para eles, não vão para o snapshot `dados` e não são
    logados. A via com os documentos fica com o colaborador (download/e-mail) e
    no `MovimentacaoItem.termo_pdf`, que é arquivo protegido por
    `media_acesso_service`.

    O motivo é de exposição, não de preferência: guardar CPF de 1.376
    colaboradores numa coluna cria um alvo que o sistema não precisa ter — o
    termo serve como prova com o documento impresso nele, e uma tabela
    consultável de CPFs não acrescenta nada ao fluxo e vaza inteira se o banco
    vazar. Ver `DOCUMENTOS_NAO_PERSISTEM` em `services/termo_pdf_service.py`.

    **Não crie campo para "facilitar a consulta depois".** Se um dia for
    preciso localizar termos por documento, o caminho é o colaborador
    (`TermoAssinatura.colaborador`), que já está no registro.
    """
    from ProjetoEstoque.models import StatusAssinaturaTermoChoices, TermoAssinatura
    from services.termo_pdf_service import gerar_pdf_assinado, nome_arquivo_pdf, sha256_bytes

    if not aceite:
        raise ValueError("É necessário marcar o aceite do termo antes de assinar.")

    nome = _texto(signatario_nome)
    if len(nome) < 3:
        raise ValueError("Informe o nome completo do signatário.")

    png_colaborador = _decodificar_traco(assinatura_colaborador)
    png_responsavel = _decodificar_traco(assinatura_responsavel) if assinatura_responsavel else None

    with transaction.atomic():
        # Trava a linha e reconfere: dois toques no botão não podem virar dois
        # PDFs. O `pode_assinar` fora do lock serve só para a mensagem amigável
        # da tela; a decisão real é esta.
        travado = (
            TermoAssinatura.objects
            .select_for_update()
            .select_related("colaborador")
            .get(pk=termo.pk)
        )

        if travado.status == StatusAssinaturaTermoChoices.ASSINADO:
            raise ValueError("Este termo já foi assinado.")
        if travado.status == StatusAssinaturaTermoChoices.CANCELADO:
            raise ValueError("Esta sessão de assinatura foi cancelada pelo setor de TI.")
        if travado.expira_em <= timezone.now():
            raise ValueError("O prazo deste link de assinatura expirou. Peça um novo ao setor de TI.")

        agora = timezone.now()

        # Autor da movimentação de estoque: quem PREPAROU a sessão. O
        # colaborador que assina é anônimo (não tem login) e não pode constar
        # como autor de uma operação de patrimônio; o responsável é o TI que
        # conduziu a entrega. A autoria da ASSINATURA é outra coisa e está nos
        # campos `signatario_*` + IP + aparelho.
        user_execucao = travado.criado_por

        travado.signatario_nome = nome[:150]
        travado.aceite_declarado = True
        travado.assinado_em = agora
        travado.assinado_ip = ip_do_request(request)
        travado.assinado_user_agent = (
            (request.META.get("HTTP_USER_AGENT", "") if request is not None else "") or ""
        )[:400]

        # `save=False`: o arquivo vai para o storage agora, mas a linha é
        # gravada uma única vez no fim, junto do PDF e do hash — evita deixar
        # um registro meio-assinado se a geração do PDF falhar.
        travado.assinatura_colaborador.save(
            "assinatura_colaborador.png", ContentFile(png_colaborador), save=False
        )
        if png_responsavel:
            travado.assinatura_responsavel.save(
                "assinatura_responsavel.png", ContentFile(png_responsavel), save=False
            )

        # Documentos em memória, só para esta emissão — ver o docstring.
        documentos = {
            "cpf": _texto(signatario_cpf)[:14],
            "rg": _texto(signatario_rg)[:30],
        }
        conteudo_pdf = gerar_pdf_assinado(travado, documentos=documentos)
        nome_pdf = nome_arquivo_pdf(travado)

        travado.pdf.save(nome_pdf, ContentFile(conteudo_pdf), save=False)
        travado.pdf_sha256 = sha256_bytes(conteudo_pdf)
        travado.status = StatusAssinaturaTermoChoices.ASSINADO
        travado.save()

        # Duas situações, e a diferença entre elas importa:
        #
        #  • A sessão JÁ tem movimentação vinculada — o TI registrou a
        #    transferência antes e só faltava o termo. O efeito no estoque já
        #    aconteceu; aqui é só anexar o documento.
        #
        #  • A sessão NÃO tem — a assinatura é o gatilho. Então ela registra a
        #    movimentação, e é ela que leva o equipamento para o centro de
        #    custo e a localidade do colaborador (entrega) ou de volta para
        #    Backup (devolução).
        if travado.desligamento:
            # Desligamento: a assinatura encerra o vínculo inteiro — devolve
            # TODOS os equipamentos, remove TODAS as licenças e marca o
            # colaborador como desligado. Fonte única: `DesligamentoService`,
            # a mesma que a cascata da tela de movimentações usa.
            criadas = _executar_desligamento(travado, conteudo_pdf, nome_pdf, user_execucao)
            anexados = 0
        elif travado.movimentacoes.exists():
            anexados = _anexar_pdf_nas_movimentacoes(travado, conteudo_pdf, nome_pdf)
            criadas = []
        else:
            criadas = _registrar_movimentacoes(travado, conteudo_pdf, nome_pdf, user_execucao)
            anexados = 0

        # ── Via do colaborador, por e-mail, automaticamente ────────────────
        # `on_commit` e NÃO envio direto: o colaborador está parado na frente
        # do tablet esperando a tela de confirmação, e pendurar a resposta num
        # SMTP lento (ou fora do ar) faria a assinatura PARECER que falhou —
        # quando ela já está gravada. É a regra 3 do CLAUDE.md, e aqui ela é
        # exatamente o que o fluxo precisa.
        #
        # Antes isto dependia de o colaborador clicar num botão na tela de
        # conclusão. Na prática ele assina e devolve o tablet: ninguém clicava,
        # e a via nunca chegava. O botão continua existindo, como reenvio.
        #
        # `robust=True`: o Django roda os callbacks de `on_commit` em fila, e
        # um que levante exceção IMPEDE os seguintes de rodar. Este é o último
        # da fila (o `MovimentacaoEstoqueService` registra os alertas de
        # movimentação antes), então qualquer callback novo que apareça na
        # frente e estoure levaria a via embora junto — silenciosamente, e só
        # na produção, onde a ordem depende do que mais a assinatura moveu.
        # Com `robust`, uma falha vizinha não cancela o envio da via.
        if (getattr(travado.colaborador, "email", "") or "").strip():
            transaction.on_commit(
                lambda pk=travado.pk: _enviar_via_em_segundo_plano(pk),
                robust=True,
            )
        else:
            # Sem e-mail no cadastro o sistema não tem para onde mandar. Isso
            # fica REGISTRADO em vez de simplesmente não acontecer: é a causa
            # mais comum de "não recebi", e a tela do termo passa a dizer que o
            # que falta é o cadastro — não um reenvio.
            registrar_resultado_via(
                travado, enviado=False, tem_email=False,
                mensagem="Colaborador sem e-mail no cadastro no momento da assinatura.",
            )

    logger.info(
        "Termo assinado eletronicamente: sessao=%s tipo=%s colaborador=%s codigo=%s "
        "movimentacoes_criadas=%s movimentacoes_anexadas=%s",
        travado.pk, travado.tipo, travado.colaborador_id, travado.codigo_verificacao,
        len(criadas), anexados,
    )
    return travado


# ---------------------------------------------------------------------------
# 3. Operações do TI sobre a sessão
# ---------------------------------------------------------------------------

@transaction.atomic
def cancelar_sessao(*, termo, user, motivo=""):
    """Cancela uma sessão pendente. Sessão assinada NUNCA é cancelada: o
    documento existe, foi firmado, e apagá-lo do fluxo seria destruir prova —
    o caminho para desfazer uma entrega é a reversão da movimentação."""
    from ProjetoEstoque.models import StatusAssinaturaTermoChoices, TermoAssinatura

    travado = TermoAssinatura.objects.select_for_update().get(pk=termo.pk)

    if travado.status == StatusAssinaturaTermoChoices.ASSINADO:
        raise ValueError("Este termo já foi assinado e não pode ser cancelado.")
    if travado.status == StatusAssinaturaTermoChoices.CANCELADO:
        return travado

    travado.status = StatusAssinaturaTermoChoices.CANCELADO
    travado.cancelado_em = timezone.now()
    travado.motivo_cancelamento = _texto(motivo)[:200]
    travado.atualizado_por = user if (user and user.is_authenticated) else None
    travado.save(update_fields=[
        "status", "cancelado_em", "motivo_cancelamento", "atualizado_por", "updated_at",
    ])
    return travado


def verificar_integridade_assinados(*, desde=None) -> dict:
    """
    Reconfere o SHA-256 de TODOS os termos assinados e classifica cada um em
    `ok` / `alterado` / `ausente`.

    `verificar_autenticidade` responde por um termo, quando alguém já
    desconfia. Isto responde pelo acervo, de forma agendável — a diferença
    importa porque arquivo perdido ou corrompido não avisa: ele é descoberto no
    dia em que o documento é pedido, que é o pior dia possível.

    Termo sem `pdf_sha256` (emitido antes de o campo existir, ou sessão
    assinada cujo PDF falhou) entra como `ausente`: não há com o que comparar,
    e tratá-lo como íntegro seria dar certificado de integridade a um arquivo
    que ninguém conferiu.
    """
    from ProjetoEstoque.models import StatusAssinaturaTermoChoices, TermoAssinatura
    from services.termo_pdf_service import sha256_arquivo

    qs = (
        TermoAssinatura.objects
        .select_related("colaborador")
        .filter(status=StatusAssinaturaTermoChoices.ASSINADO)
        .order_by("assinado_em", "pk")
    )
    if desde:
        qs = qs.filter(assinado_em__date__gte=desde)

    itens = []
    contagem = {"ok": 0, "alterado": 0, "ausente": 0}

    for termo in qs:
        if not termo.pdf or not termo.pdf_sha256:
            situacao = "ausente"
        else:
            try:
                atual = sha256_arquivo(termo.pdf)
            except (FileNotFoundError, OSError):
                situacao = "ausente"
            else:
                situacao = (
                    "ok" if secrets.compare_digest(atual, termo.pdf_sha256) else "alterado"
                )

        contagem[situacao] += 1
        itens.append({
            "pk": termo.pk,
            "codigo": termo.codigo_verificacao,
            "colaborador": termo.colaborador.nome,
            "assinado_em": (
                timezone.localtime(termo.assinado_em).strftime("%d/%m/%Y %H:%M")
                if termo.assinado_em else "—"
            ),
            "situacao": situacao,
        })

    return {
        "total": len(itens),
        "ok": contagem["ok"],
        "alterados": contagem["alterado"],
        "ausentes": contagem["ausente"],
        "itens": itens,
    }


def verificar_autenticidade(codigo: str) -> dict | None:
    """
    Confere um termo pelo código impresso no PDF.

    Recalcula o SHA-256 do arquivo armazenado e compara com o hash gravado no
    momento da assinatura. `integro=False` significa que o arquivo em
    `MEDIA_ROOT` mudou depois de emitido — o que, num diretório sincronizado
    por OneDrive, é mais provável acontecer por acidente do que por má-fé, e é
    justamente por isso que precisa ser detectável.
    """
    from ProjetoEstoque.models import TermoAssinatura
    from services.termo_pdf_service import sha256_arquivo

    codigo = (codigo or "").strip().upper()
    if not codigo:
        return None

    termo = (
        TermoAssinatura.objects
        .select_related("colaborador")
        .prefetch_related("itens", "movimentacoes")
        .filter(codigo_verificacao=codigo)
        .first()
    )
    if termo is None:
        return None

    integro = None
    hash_atual = ""
    if termo.pdf and termo.pdf_sha256:
        try:
            hash_atual = sha256_arquivo(termo.pdf)
            integro = secrets.compare_digest(hash_atual, termo.pdf_sha256)
        except (FileNotFoundError, OSError):
            integro = False

    return {
        "termo": termo,
        "integro": integro,
        "hash_gravado": termo.pdf_sha256,
        "hash_atual": hash_atual,
    }


# ---------------------------------------------------------------------------
# Consultas de apoio à UI
# ---------------------------------------------------------------------------

def itens_em_posse(colaborador):
    """
    Equipamentos hoje com o colaborador — a lista que o termo de DEVOLUÇÃO
    deve oferecer.

    Reaproveita `DesligamentoService.ativos_do_usuario`, que já é a fonte única
    dessa pergunta no sistema (usada pela cascata de desligamento e pelo termo
    consolidado). Duplicar a regra aqui faria a tela de assinatura discordar da
    tela de desligamento sobre o que o colaborador tem em mãos.
    """
    from services.desligamento_service import DesligamentoService
    return list(DesligamentoService.ativos_do_usuario(colaborador)["itens"])


def pendencias_do_colaborador(colaborador):
    """Sessões pendentes e ainda no prazo deste colaborador — para a tela
    avisar que já existe um link aberto em vez de criar um segundo."""
    from ProjetoEstoque.models import StatusAssinaturaTermoChoices, TermoAssinatura

    return list(
        TermoAssinatura.objects
        .filter(
            colaborador=colaborador,
            status=StatusAssinaturaTermoChoices.PENDENTE,
            expira_em__gt=timezone.now(),
        )
        .prefetch_related("itens")
    )
