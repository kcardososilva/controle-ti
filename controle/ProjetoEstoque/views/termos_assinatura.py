# -*- coding: utf-8 -*-
"""
Assinatura digital dos termos de responsabilidade — views.

Três escopos de acesso convivem aqui, e a diferença entre eles é a parte
importante deste arquivo:

  • TI, autenticado (`@login_required`) — prepara a sessão, acompanha o
    status, baixa o PDF, cancela, verifica autenticidade.

  • Colaborador, ANÔNIMO, no tablet (`termo_assinar` / `termo_assinar_enviar`)
    — abre pelo token e assina. Sem `@login_required` de propósito: o
    colaborador não tem login no sistema (ver `services/
    termo_assinatura_service`, cabeçalho). A autorização é o token opaco de
    uso único, com prazo curto, resolvido em tempo constante.

  • Ninguém — o PDF nunca é servido direto de `/media/` por URL adivinhada;
    `termo_assinatura_pdf` é `@login_required` e `MEDIA_ROOT` já passa por
    `views/media_protegida.py` (regra 23 do CLAUDE.md).

`termo_assinar_enviar` é `@csrf_exempt`? NÃO. A tela anônima recebe `csrf_token`
normalmente (o Django emite cookie CSRF para sessão anônima também) e o fetch
do canvas manda o cabeçalho. Dispensar CSRF num endpoint que grava arquivo e
emite documento assinado seria trocar uma dor de cabeça de 5 minutos por um
buraco permanente.
"""

import json
import logging
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from services import termo_assinatura_service as tas

from ..forms import TermoAssinaturaSessaoForm, TermoDesligamentoSessaoForm
from ..models import (
    Item,
    MovimentacaoItem,
    StatusAssinaturaTermoChoices,
    StatusItemChoices,
    TermoAssinatura,
    Usuario,
)

# Equipamentos que o termo de ENTREGA pode oferecer: o que não está com
# ninguém. "Ativo" é o status de item já em uso por um colaborador, então fica
# fora — oferecê-lo geraria um termo de entrega para algo que já foi entregue.
# Trilha de segurança (vai para `logs/seguranca.log` — ver settings.LOGGING):
# token recusado na porta anônima e leitura de documento assinado. O termo
# assinado é prova e contém o documento pessoal digitado no ato, então "quem
# abriu este PDF e quando" é pergunta que precisa ter resposta.
_log_seguranca = logging.getLogger("seguranca")

_STATUS_ENTREGAVEIS = (
    StatusItemChoices.ESTOQUE,
    StatusItemChoices.BACKUP,
    StatusItemChoices.PAUSADO,
)

# ...MENOS para equipamento COMPARTILHADO, onde "Ativo" não quer dizer "já está
# com alguém, logo indisponível" — quer dizer "tem pelo menos um detentor". É a
# situação NORMAL dele: os tablets de campo ficam com 11 colaboradores ao mesmo
# tempo, e o item fica Ativo a partir do primeiro. Com o filtro único, esses
# equipamentos desapareciam da tela e o TI não conseguia colher a assinatura do
# próximo colaborador — justamente o caso que o compartilhamento existe para
# atender. Quem barra entrega repetida ao MESMO colaborador é
# `validar_pre_condicoes` (pelo vínculo em `ItemColaborador`), que é a pergunta
# certa; o status do item nunca foi capaz de responder isso.
_STATUS_ENTREGAVEIS_COMPARTILHADO = _STATUS_ENTREGAVEIS + (StatusItemChoices.ATIVO,)


def _itens_entregaveis():
    # `Item.objects` já exclui os itens apagados (manager padrão do model);
    # `item_consumo="nao"` tira material de consumo, que sai por baixa de
    # estoque e não por termo de responsabilidade nominal.
    #
    # Manutenção/defeito continuam fora nos dois casos: `MovimentacaoItemForm.
    # clean` recusa transferir item preso em manutenção, e descobrir isso
    # DEPOIS da assinatura é o que a validação na preparação existe para evitar.
    return (
        Item.objects
        .filter(item_consumo="nao")
        .filter(
            Q(status__in=_STATUS_ENTREGAVEIS)
            | Q(compartilhado=True, status__in=_STATUS_ENTREGAVEIS_COMPARTILHADO)
        )
        .select_related("subtipo", "centro_custo", "localidade")
        .order_by("nome")
    )


def _itens_do_colaborador(colaborador):
    """Queryset (não lista) dos equipamentos em posse do colaborador — o campo
    do formulário precisa de queryset para validar o POST."""
    if colaborador is None:
        return Item.objects.none()
    ids = [item.pk for item in tas.itens_em_posse(colaborador)]
    return (
        Item.objects
        .filter(pk__in=ids)
        .select_related("subtipo", "centro_custo", "localidade")
        .order_by("nome")
    )


# ---------------------------------------------------------------------------
# Busca remota (Select2) — colaborador e equipamento
# ---------------------------------------------------------------------------
# Por que AJAX e não um `<select>` com tudo dentro: são 1.376 colaboradores
# ativos e 751 equipamentos entregáveis. Renderizar isso como `<option>` são
# centenas de KB de HTML em cada abertura da tela, que o navegador ainda tem de
# montar antes de pintar — e a busca do Select2 local ficaria filtrando um DOM
# gigante a cada tecla. Mesmo padrão já usado na Movimentação em Lote
# (`api_itens_disponiveis_lote`).

_LIMITE_BUSCA = 30


@login_required
def termo_assinatura_colaboradores_api(request):
    """Busca remota de colaboradores ativos, por nome ou matrícula."""
    termo = (request.GET.get("q") or "").strip()

    qs = (
        Usuario.objects
        .filter(status="ativo")
        .select_related("centro_custo", "funcao", "localidade")
    )
    if termo:
        qs = qs.filter(Q(nome__icontains=termo) | Q(matricula__icontains=termo))

    # `incluir` garante que o colaborador já escolhido apareça mesmo sem busca,
    # para o Select2 conseguir rotular a opção ao reabrir a tela.
    incluir = request.GET.get("incluir")
    if incluir:
        qs = Usuario.objects.filter(
            Q(pk__in=qs.values("pk")[: _LIMITE_BUSCA]) | Q(pk=incluir)
        ).select_related("centro_custo", "funcao", "localidade")

    resultados = [{
        "id": colaborador.pk,
        "text": colaborador.nome,
        "matricula": colaborador.matricula or "",
        "centro_custo": getattr(colaborador.centro_custo, "departamento", "") or "",
        "funcao": getattr(colaborador.funcao, "nome", "") or "",
        "localidade": getattr(colaborador.localidade, "local", "") or "",
    } for colaborador in qs.order_by("nome")[: _LIMITE_BUSCA]]

    return JsonResponse({"results": resultados})


@login_required
def termo_assinatura_equipamentos_api(request):
    """
    Busca remota dos equipamentos elegíveis para o termo.

    O universo é o MESMO que a view de preparação usa (`_itens_entregaveis` /
    `_itens_do_colaborador` + `_com_item_indicado`) — não há uma segunda regra
    aqui, senão a lista do Select2 e a validação do formulário poderiam
    discordar sobre o que é elegível, e o operador veria uma opção que o POST
    recusa.
    """
    tipo = request.GET.get("tipo")
    termo = (request.GET.get("q") or "").strip()
    colaborador = _colaborador_do_request(request)

    if tipo == "devolucao":
        if colaborador is None:
            return JsonResponse({"results": [], "aviso": "Selecione o colaborador primeiro."})
        qs = _itens_do_colaborador(colaborador)
    else:
        qs = _itens_entregaveis()

    qs = _com_item_indicado(qs, request.GET.get("incluir"))

    if termo:
        qs = qs.filter(
            Q(nome__icontains=termo)
            | Q(numero_serie__icontains=termo)
            | Q(modelo__icontains=termo)
            | Q(marca__icontains=termo)
        )

    itens = list(qs[: _LIMITE_BUSCA])
    posse = _posse_de_compartilhados(itens, colaborador)

    resultados = [{
        "id": item.pk,
        "text": item.nome,
        "serie": item.numero_serie or "",
        "marca_modelo": " ".join(p for p in (item.marca, item.modelo) if p),
        "centro_custo": getattr(item.centro_custo, "departamento", "") or "",
        "status": item.get_status_display(),
        # O operador precisa saber ANTES de montar o termo: equipamento sem
        # centro de custo não passa na validação da entrega (ver
        # `MovimentacaoItemForm.clean`), porque a devolução não teria para onde
        # devolver o item.
        "sem_centro_custo": not item.centro_custo_id,
        # Compartilhado aparece mesmo estando "Ativo" (é o normal dele). Sem
        # estes dois a lista ficaria confusa justamente onde ela ficou mais
        # permissiva: o operador veria um item Ativo e não saberia se pode
        # entregar. `codetentores` diz com quantos está; `ja_com_colaborador`
        # diz que ESTE colaborador já é um deles — o caso que
        # `validar_pre_condicoes` recusa, avisado antes de montar o termo.
        "compartilhado": bool(item.compartilhado),
        "codetentores": posse["contagem"].get(item.pk, 0),
        "ja_com_colaborador": item.pk in posse["do_colaborador"],
    } for item in itens]

    return JsonResponse({"results": resultados})


def _posse_de_compartilhados(itens, colaborador):
    """
    Quantos detentores ativos cada equipamento compartilhado tem, e quais deles
    já são deste colaborador.

    Uma consulta para a lista toda (não uma por item): a busca devolve até 30
    resultados e o Select2 refaz a chamada a cada tecla digitada.
    """
    from ..models import ItemColaborador

    ids = [item.pk for item in itens if item.compartilhado]
    if not ids:
        return {"contagem": {}, "do_colaborador": set()}

    contagem = {}
    do_colaborador = set()
    for item_id, colaborador_id in (
        ItemColaborador.objects
        .filter(item_id__in=ids, ativo=True)
        .values_list("item_id", "colaborador_id")
    ):
        contagem[item_id] = contagem.get(item_id, 0) + 1
        if colaborador is not None and colaborador_id == colaborador.pk:
            do_colaborador.add(item_id)

    return {"contagem": contagem, "do_colaborador": do_colaborador}


def _colaborador_do_request(request, campo="colaborador"):
    valor = request.POST.get(campo) or request.GET.get(campo)
    if not valor:
        return None
    return Usuario.objects.filter(pk=valor).select_related("centro_custo", "funcao", "localidade").first()


def _com_item_indicado(queryset, item_id):
    """
    Acrescenta ao universo o equipamento apontado por `?item=`, mesmo que ele
    esteja fora do filtro padrão.

    Isso NÃO é afrouxar a validação — é o caso de uso principal da entrada
    pelo Repositório de Termos: a movimentação já foi registrada (então o item
    saiu do estoque, ou saiu da posse do colaborador) e o que falta é o termo
    daquele ato que já aconteceu. Sem isto, a pendência que a própria tela
    aponta seria impossível de resolver pelo botão que ela oferece — o
    checkbox simplesmente não apareceria, sem explicar por quê.

    O que continua travado é o POST ARBITRÁRIO: o universo é a lista que a
    tela mostrou, e o formulário valida contra ela (ver
    `TermoAssinaturaSessaoForm.__init__`). O `?item=` é uma indicação
    explícita de quem abriu a tela, não um id solto vindo do envio.
    """
    if not item_id:
        return queryset
    try:
        item_id = int(item_id)
    except (TypeError, ValueError):
        return queryset

    return (
        Item.objects
        .filter(Q(pk__in=queryset.values("pk")) | Q(pk=item_id))
        .select_related("subtipo", "centro_custo", "localidade")
        .order_by("nome")
    )


# ---------------------------------------------------------------------------
# TI — preparar a sessão de assinatura
# ---------------------------------------------------------------------------

@login_required
def termo_assinatura_preparar(request, tipo):
    """
    Monta a sessão de assinatura e redireciona para o painel com o QR Code.

    `tipo` vem da URL (`entrega` / `devolucao`) porque é o que decide qual
    universo de equipamentos a tela oferece e qual modelo base de texto legal
    será assinado — não é um campo que o usuário deva poder trocar depois de
    escolher os itens.
    """
    if tipo not in ("entrega", "devolucao"):
        raise Http404("Tipo de termo inválido.")

    # `?item=` sobrevive ao POST (campo oculto no formulário): sem isso, um
    # erro de validação reexibiria a tela sem o equipamento que a originou.
    item_inicial = request.POST.get("item_indicado") or request.GET.get("item")
    colaborador_selecionado = _colaborador_do_request(request)

    # Na devolução, o universo depende do colaborador: só o que está com ele.
    # Na entrega, é o estoque livre. Nos dois casos, o equipamento apontado
    # por `?item=` entra junto — ver `_com_item_indicado`.
    if tipo == "devolucao":
        itens_disponiveis = _itens_do_colaborador(colaborador_selecionado)
    else:
        itens_disponiveis = _itens_entregaveis()

    itens_disponiveis = _com_item_indicado(itens_disponiveis, item_inicial)

    if request.method == "POST":
        form = TermoAssinaturaSessaoForm(request.POST, itens_disponiveis=itens_disponiveis)
        if form.is_valid():
            dados = form.cleaned_data
            try:
                sessao = tas.criar_sessao(
                    tipo=tipo,
                    colaborador=dados["colaborador"],
                    itens=list(dados["itens"]),
                    form_data=dados,
                    user=request.user,
                    request=request,
                    validade_minutos=dados.get("validade_minutos"),
                    movimentacoes=_movimentacoes_sugeridas(tipo, dados["colaborador"], dados["itens"]),
                )
            except (ValueError, FileNotFoundError) as erro:
                form.add_error(None, str(erro))
            else:
                # O token puro existe só nesta resposta. Ele vai pela sessão do
                # TI (não pela URL) para não ficar no histórico do navegador,
                # em log de servidor nem no cabeçalho `Referer` de qualquer
                # recurso que o painel carregue.
                request.session[f"termo_token_{sessao['termo'].pk}"] = sessao["token"]
                messages.success(
                    request,
                    "Sessão de assinatura criada. Abra o link no tablet e colha a assinatura.",
                )
                return redirect("termo_assinatura_painel", pk=sessao["termo"].pk)
    else:
        inicial = {
            "estabelecimento": "karitel",
            "responsavel_ti_nome": request.user.get_full_name() or request.user.username,
            "validade_minutos": tas.VALIDADE_MINUTOS_PADRAO,
            "observacoes": (
                "" if tipo == "entrega"
                else "Devolução de equipamento conferida pelo setor de TI no ato do recebimento."
            ),
        }
        if colaborador_selecionado is not None:
            inicial["colaborador"] = colaborador_selecionado.pk
        if item_inicial:
            inicial["itens"] = [item_inicial]
            # Entrada pela ficha do equipamento: na devolução o colaborador é o
            # detentor atual, então o universo de itens é dele — resolvido aqui
            # para o campo já vir marcado ao abrir a tela.
            if tipo == "devolucao" and colaborador_selecionado is None:
                from services.termos import get_usuario_atual_item

                item = Item.objects.filter(pk=item_inicial).first()
                detentor = get_usuario_atual_item(item) if item is not None else None
                if detentor is not None:
                    colaborador_selecionado = detentor
                    inicial["colaborador"] = detentor.pk
                    itens_disponiveis = _com_item_indicado(
                        _itens_do_colaborador(detentor), item_inicial
                    )

        form = TermoAssinaturaSessaoForm(initial=inicial, itens_disponiveis=itens_disponiveis)

    pendentes = (
        tas.pendencias_do_colaborador(colaborador_selecionado)
        if colaborador_selecionado is not None else []
    )

    # PKs marcados como lista de INTEIROS: o template compara com `item.pk`
    # (também int). Comparar strings ali daria falso positivo por substring
    # ("1" dentro de "11") e marcaria equipamento que o usuário não escolheu.
    bruto = form.data.getlist("itens") if form.is_bound else (form.initial.get("itens") or [])
    itens_marcados = []
    for valor in bruto:
        try:
            itens_marcados.append(int(valor))
        except (TypeError, ValueError):
            continue

    # Só os itens JÁ escolhidos viram `<option>` no HTML — o resto do catálogo
    # o Select2 busca por AJAX conforme o operador digita. É o que mantém a
    # página leve com 751 equipamentos elegíveis, sem perder o rótulo do que
    # já está selecionado (um `<select>` vazio mostraria só o número do id).
    itens_pre_selecionados = (
        Item.objects.filter(pk__in=itens_marcados).select_related("centro_custo")
        if itens_marcados else Item.objects.none()
    )

    return render(request, "front/termos/termo_assinatura_preparar.html", {
        "form": form,
        "tipo": tipo,
        "itens_disponiveis": itens_disponiveis,
        "itens_marcados": itens_marcados,
        "itens_pre_selecionados": itens_pre_selecionados,
        "item_indicado": item_inicial or "",
        "colaborador_selecionado": colaborador_selecionado,
        "pendentes": pendentes,
        "config_busca": {
            "tipo": tipo,
            "urlColaboradores": reverse("termo_assinatura_colaboradores_api"),
            "urlEquipamentos": reverse("termo_assinatura_equipamentos_api"),
            "itemIndicado": item_inicial or "",
        },
        "titulo": "Assinatura digital — Termo de Entrega" if tipo == "entrega"
                  else "Assinatura digital — Termo de Devolução",
        "subtitulo": (
            "Selecione o colaborador e os equipamentos; o link do tablet é gerado na etapa seguinte."
            if tipo == "entrega" else
            "Escolha o colaborador para listar o que está em posse dele e montar o termo de devolução."
        ),
    })



@login_required
def termo_assinatura_desligamento(request):
    """
    Sessão de assinatura de DESLIGAMENTO.

    Tela própria, e não mais um `tipo` na view de preparação, por uma razão de
    fluxo: aqui o TI **não escolhe equipamento**. O termo cobre tudo o que o
    colaborador tem — equipamentos e licenças — apurado na hora. Deixar
    selecionar seria convidar ao erro que o desligamento não perdoa:
    equipamento esquecido fora do termo continua no nome de quem não trabalha
    mais aqui.

    A tela existe, então, para **mostrar o que vai acontecer** e confirmar: a
    lista completa do que será devolvido e removido, e o aviso de que assinar
    encerra o vínculo. O efeito roda quando o colaborador assina, não aqui.
    """
    from services.desligamento_service import DesligamentoService

    colaborador = _colaborador_do_request(request)
    ativos = {"itens": [], "licencas": []}
    if colaborador is not None:
        ativos = DesligamentoService.ativos_do_usuario(colaborador)

    if request.method == "POST":
        form = TermoDesligamentoSessaoForm(request.POST)
        if form.is_valid():
            dados = form.cleaned_data
            try:
                sessao = tas.criar_sessao_desligamento(
                    colaborador=dados["colaborador"],
                    form_data=dados,
                    user=request.user,
                    request=request,
                    validade_minutos=dados.get("validade_minutos"),
                )
            except (ValueError, FileNotFoundError) as erro:
                form.add_error(None, str(erro))
            else:
                request.session[f"termo_token_{sessao['termo'].pk}"] = sessao["token"]
                messages.success(
                    request,
                    "Sessão de desligamento criada. Ao assinar, o colaborador devolve "
                    "todos os equipamentos, perde as licenças e é marcado como desligado.",
                )
                return redirect("termo_assinatura_painel", pk=sessao["termo"].pk)
    else:
        inicial = {
            "validade_minutos": tas.VALIDADE_MINUTOS_PADRAO,
            "responsavel_ti_nome": request.user.get_full_name() or request.user.username,
            "estabelecimento": "karitel",
        }
        if colaborador is not None:
            inicial["colaborador"] = colaborador.pk
        form = TermoDesligamentoSessaoForm(initial=inicial)

    pendentes = (
        tas.pendencias_do_colaborador(colaborador)
        if colaborador is not None else []
    )

    return render(request, "front/termos/termo_assinatura_desligamento.html", {
        "form": form,
        "tipo": "desligamento",
        "colaborador_selecionado": colaborador,
        "itens": ativos["itens"],
        "licencas": ativos["licencas"],
        "pendentes": pendentes,
        "config_busca": {
            "tipo": "desligamento",
            "urlColaboradores": reverse("termo_assinatura_colaboradores_api"),
        },
        "titulo": "Assinatura digital — Desligamento",
        "subtitulo": (
            "Um único termo cobre tudo o que o colaborador tem. Ao assinar, o sistema "
            "devolve os equipamentos, remove as licenças e desliga o cadastro."
        ),
    })


def _movimentacoes_sugeridas(tipo, colaborador, itens):
    """
    Movimentações de transferência destes itens para este colaborador que ainda
    não têm termo anexado — o PDF assinado vai preencher o `termo_pdf` delas.

    Janela de 7 dias e `revertida=False`: a assinatura costuma ser colhida no
    mesmo momento do registro da movimentação, mas às vezes o TI registra a
    entrega e colhe a assinatura no dia seguinte. Sem o recorte de tempo, uma
    entrega feita no ano passado e nunca documentada receberia o termo de hoje,
    o que seria datar prova errado.
    """
    limite = timezone.now() - timedelta(days=7)

    return list(
        MovimentacaoItem.objects.filter(
            tipo_movimentacao="transferencia",
            tipo_transferencia=tipo,
            usuario=colaborador,
            item__in=itens,
            revertida=False,
            created_at__gte=limite,
        ).filter(Q(termo_pdf__isnull=True) | Q(termo_pdf=""))
    )


# ---------------------------------------------------------------------------
# TI — painel da sessão (QR Code + acompanhamento)
# ---------------------------------------------------------------------------

@login_required
def termo_assinatura_painel(request, pk):
    """
    Tela que o TI deixa aberta enquanto o colaborador assina no tablet: QR
    Code, link copiável, contagem do prazo e status atualizado por polling
    (`termo_assinatura_status`).

    O token puro só aparece se esta for a mesma sessão de navegador que criou a
    assinatura (guardado em `request.session` pela view anterior). Reabrir o
    painel de outro computador mostra a sessão e o status, mas não o link — o
    banco só tem o hash. Para esse caso existe o botão "Gerar novo link".
    """
    termo = get_object_or_404(
        TermoAssinatura.objects
        .select_related("colaborador", "colaborador__centro_custo", "colaborador__funcao",
                        "criado_por")
        .prefetch_related("itens", "movimentacoes__item"),
        pk=pk,
    )

    chave = f"termo_token_{termo.pk}"
    token = request.session.get(chave)
    link = tas.link_de_assinatura(termo, token, request) if (token and termo.pode_assinar) else None

    # O link é montado com o Host que o TI usou para abrir esta tela. Se ele
    # entrou por `127.0.0.1:8000`, o QR Code aponta para o loopback DA MÁQUINA
    # DELE e o tablet recebe "não foi possível conectar" — sem nada que explique
    # por quê, porque o QR é válido e a sessão está certa. Avisar aqui é o que
    # evita a caçada ao problema no Wi-Fi.
    host_local = bool(link) and any(
        marca in link["url"] for marca in ("127.0.0.1", "localhost", "[::1]")
    )

    return render(request, "front/termos/termo_assinatura_painel.html", {
        "termo": termo,
        "link": link,
        "host_local": host_local,
        "titulo": f"Assinatura do termo de {termo.get_tipo_display().lower()}",
        "subtitulo": f"{termo.colaborador.nome} · {termo.equipamentos_resumo}",
    })


@login_required
def termo_assinatura_status(request, pk):
    """Status da sessão em JSON, para o painel saber que a assinatura chegou
    sem o TI precisar recarregar a página com o colaborador esperando."""
    termo = get_object_or_404(TermoAssinatura.objects.only(
        "id", "status", "expira_em", "assinado_em", "signatario_nome", "codigo_verificacao",
    ), pk=pk)

    return JsonResponse({
        "situacao": termo.situacao_slug,
        "situacao_label": termo.situacao_label,
        "assinado": termo.status == StatusAssinaturaTermoChoices.ASSINADO,
        "assinado_em": timezone.localtime(termo.assinado_em).strftime("%d/%m/%Y %H:%M:%S")
                       if termo.assinado_em else None,
        "signatario": termo.signatario_nome,
        "codigo_verificacao": termo.codigo_verificacao,
        "expira_em_segundos": max(
            0, int((termo.expira_em - timezone.now()).total_seconds())
        ) if termo.expira_em else 0,
    })


@login_required
@require_POST
def termo_assinatura_novo_link(request, pk):
    termo = get_object_or_404(TermoAssinatura, pk=pk)
    try:
        sessao = tas.regerar_token(termo, user=request.user, request=request)
    except ValueError as erro:
        messages.error(request, str(erro))
    else:
        request.session[f"termo_token_{termo.pk}"] = sessao["token"]
        messages.success(
            request,
            f"Novo link gerado, válido por {sessao['validade_minutos']} minutos. "
            "O link anterior deixou de funcionar.",
        )
    return redirect("termo_assinatura_painel", pk=termo.pk)


@login_required
@require_POST
def termo_assinatura_reenviar_via(request, pk):
    """
    Reenvia a via do termo ao e-mail do colaborador, a pedido do TI.

    O único reenvio que existia era o botão do tablet, válido pelos 15 minutos
    da sessão de quem assinou (ver `CHAVE_RECIBO`). Isso cobre "o colaborador
    percebeu na hora" e não cobre o caso real: alguém nota dias depois que a via
    não chegou, e aí não havia caminho nenhum — a alternativa era emitir um
    termo novo, criando uma segunda assinatura para o mesmo ato.
    """
    termo = get_object_or_404(
        TermoAssinatura.objects.select_related("colaborador"), pk=pk,
    )

    enviado, mensagem = tas.reenviar_via(termo, user=request.user)
    if enviado:
        messages.success(request, f"Via reenviada ao colaborador. {mensagem}")
    else:
        messages.error(request, f"A via não foi enviada. {mensagem}")

    return redirect("termo_assinatura_detalhe", pk=termo.pk)


@login_required
@require_POST
def termo_assinatura_cancelar(request, pk):
    termo = get_object_or_404(TermoAssinatura, pk=pk)
    try:
        tas.cancelar_sessao(termo=termo, user=request.user, motivo=request.POST.get("motivo", ""))
    except ValueError as erro:
        messages.error(request, str(erro))
    else:
        messages.success(request, "Sessão de assinatura cancelada.")
        request.session.pop(f"termo_token_{termo.pk}", None)
    return redirect("termo_assinatura_list")


# ---------------------------------------------------------------------------
# Colaborador — tablet (anônimo, protegido por token)
# ---------------------------------------------------------------------------

def _resposta_token_invalido(request, motivo):
    """
    Tela de recusa do tablet. Sempre a MESMA mensagem genérica para token
    inexistente, expirado, já usado e cancelado, pelo mesmo motivo da tela de
    recuperação de senha (regra 27): a rota é anônima e alcançável de fora, e
    distinguir "não existe" de "existe mas expirou" transformaria a URL num
    verificador de tokens válidos. O detalhe real fica com o TI, no painel.
    """
    return render(request, "front/termos/termo_assinar_indisponivel.html", {
        "motivo": motivo,
    }, status=404)


def termo_assinar(request, token):
    """Tela de assinatura no tablet: termo na íntegra + quadro de assinatura."""
    if not tas.acesso_token_dentro_do_limite(request):
        return _resposta_token_invalido(
            request,
            "Este link de assinatura não está mais disponível.",
        )

    termo = tas.resolver_token(token)
    if termo is None:
        _log_seguranca.info(
            "Assinatura de termo: token recusado na abertura (ip=%s).",
            tas.ip_do_request(request),
        )
        return _resposta_token_invalido(
            request,
            "Este link de assinatura não está mais disponível.",
        )

    dados = termo.dados or {}
    texto_legal = dados.get("texto_legal") or {}

    return render(request, "front/termos/termo_assinar.html", {
        "termo": termo,
        "token": token,
        "dados": dados,
        "texto_legal": texto_legal,
        "clausulas": texto_legal.get("clausulas") or [],
        "equipamentos": dados.get("equipamentos") or [],
        "texto_aceite": dados.get("texto_aceite") or "",
        # Config em dict e entregue por `json_script` no template: o JS do
        # canvas é servido de `static/` e não passa pelo motor de template, então
        # `{% url %}` lá viraria texto literal e o script quebraria em silêncio
        # (CLAUDE.md, "Telas grandes"). Dado novo para a tela de assinatura
        # entra AQUI, não interpolado no .js.
        "config": {
            "enviarUrl": reverse("termo_assinar_enviar", args=[token]),
            "concluidoUrl": reverse("termo_assinar_concluido", args=[token]),
            "nomeSugerido": termo.colaborador.nome or "",
            "expiraEmSegundos": max(0, int((termo.expira_em - timezone.now()).total_seconds())),
        },
    })


@require_POST
def termo_assinar_enviar(request, token):
    """
    Recebe o traço do canvas e fecha a sessão. Responde JSON — o tablet mostra
    o resultado sem recarregar, para não perder o traço se algo falhar.
    """
    if not tas.acesso_token_dentro_do_limite(request):
        return JsonResponse(
            {"ok": False, "erro": "Este link de assinatura não está mais disponível."},
            status=404,
        )

    termo = tas.resolver_token(token)
    if termo is None:
        _log_seguranca.info(
            "Assinatura de termo: token recusado no envio (ip=%s).",
            tas.ip_do_request(request),
        )
        return JsonResponse(
            {"ok": False, "erro": "Este link de assinatura não está mais disponível."},
            status=404,
        )

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "erro": "Não foi possível ler os dados enviados."}, status=400)

    try:
        assinado = tas.registrar_assinatura(
            termo=termo,
            assinatura_colaborador=payload.get("assinatura"),
            assinatura_responsavel=payload.get("assinatura_responsavel"),
            signatario_nome=payload.get("nome"),
            signatario_cpf=payload.get("cpf", ""),
            signatario_rg=payload.get("rg", ""),
            aceite=bool(payload.get("aceite")),
            request=request,
        )
    except ValueError as erro:
        return JsonResponse({"ok": False, "erro": str(erro)}, status=400)

    # Libera a via (download/e-mail) para ESTE navegador, por tempo limitado.
    _marcar_recibo_disponivel(request, assinado)

    return JsonResponse({
        "ok": True,
        "codigo_verificacao": assinado.codigo_verificacao,
        "assinado_em": timezone.localtime(assinado.assinado_em).strftime("%d/%m/%Y às %H:%M"),
        "mensagem": "Termo assinado com sucesso. Você pode devolver o tablet ao responsável.",
    })


# ---------------------------------------------------------------------------
# Recibo do colaborador — baixar a via ou receber por e-mail
# ---------------------------------------------------------------------------
# O token é de uso único e já foi consumido quando estas rotas são chamadas, então
# a autorização aqui é outra: a SESSÃO DO NAVEGADOR que acabou de assinar, com
# prazo curto. Ninguém mais tem como pedir a via — nem quem pegar o tablet
# depois, passada a janela, nem quem souber o id do termo, que não entra na URL.
#
# Por que não um segundo token: ele viajaria na URL, ficaria no histórico do
# tablet (que é aparelho compartilhado, passando de mão em mão) e precisaria de
# mais uma coluna no banco para uma permissão que dura dez minutos.

CHAVE_RECIBO = "termo_recibo"
RECIBO_JANELA_SEGUNDOS = 15 * 60


def _marcar_recibo_disponivel(request, termo):
    """Guarda na sessão do tablet qual termo acabou de ser assinado. Só UM por
    vez: o tablet atende um colaborador de cada vez, e manter uma lista faria o
    aparelho carregar o rastro de quem assinou antes."""
    request.session[CHAVE_RECIBO] = {
        "pk": termo.pk,
        "em": timezone.now().isoformat(),
    }


def _termo_do_recibo(request):
    """Termo que esta sessão de navegador pode baixar/enviar, ou None."""
    marca = request.session.get(CHAVE_RECIBO) or {}
    pk = marca.get("pk")
    carimbo = marca.get("em")
    if not pk or not carimbo:
        return None

    try:
        assinado_em = timezone.datetime.fromisoformat(carimbo)
    except (TypeError, ValueError):
        return None

    if (timezone.now() - assinado_em).total_seconds() > RECIBO_JANELA_SEGUNDOS:
        # Expirou: limpa para o tablet não ficar guardando o rastro.
        request.session.pop(CHAVE_RECIBO, None)
        return None

    return (
        TermoAssinatura.objects
        .select_related("colaborador")
        .filter(pk=pk, status=StatusAssinaturaTermoChoices.ASSINADO)
        .first()
    )


def termo_recibo_pdf(request):
    """Baixa a via do termo que acabou de ser assinado neste aparelho."""
    termo = _termo_do_recibo(request)
    if termo is None or not termo.pdf:
        raise Http404("Documento não disponível.")

    from services.termo_pdf_service import nome_arquivo_pdf

    termo.pdf.open("rb")
    try:
        conteudo = termo.pdf.read()
    finally:
        termo.pdf.close()

    resposta = HttpResponse(conteudo, content_type="application/pdf")
    # `attachment` aqui, diferente da tela do TI: o colaborador quer a CÓPIA no
    # aparelho dele, não uma visualização que some ao fechar a aba.
    resposta["Content-Disposition"] = f'attachment; filename="{nome_arquivo_pdf(termo)}"'
    return resposta


@require_POST
def termo_recibo_email(request):
    """Envia a via por e-mail para o endereço do cadastro do colaborador."""
    termo = _termo_do_recibo(request)
    if termo is None:
        return JsonResponse(
            {"ok": False, "erro": "A sessão expirou. Peça a via ao responsável do setor de TI."},
            status=404,
        )

    # Pelo service, não direto pelo `email_alertas`: é o que grava o resultado
    # em `via_email_*`. O pedido do colaborador no tablet é tentativa de envio
    # como qualquer outra, e deixá-lo fora do registro recriaria o ponto cego
    # que o rastreamento existe para fechar.
    enviado, mensagem = tas.reenviar_via(termo, user=None)
    return JsonResponse({"ok": enviado, "mensagem": mensagem}, status=200 if enviado else 400)


def termo_assinar_concluido(request, token):
    """
    Confirmação exibida no tablet depois de assinar, com as duas opções de via:
    baixar o PDF ou receber por e-mail.

    O token da URL já foi consumido e **não** é usado para autorizar nada aqui
    (`resolver_token` devolveria None) — ele fica só para a URL continuar
    legível. Quem autoriza é a marca de sessão, com prazo (ver `_termo_do_recibo`).
    """
    termo = _termo_do_recibo(request)

    return render(request, "front/termos/termo_assinar_concluido.html", {
        "termo": termo,
        # Sem e-mail no cadastro, o botão não aparece: oferecer e falhar depois
        # é pior do que dizer antes que não há para onde mandar.
        "tem_email": bool(termo and (termo.colaborador.email or "").strip()),
    })


# ---------------------------------------------------------------------------
# TI — repositório das assinaturas digitais
# ---------------------------------------------------------------------------

@login_required
def termo_assinatura_list(request):
    """Lista filtrável das sessões de assinatura, com os contadores por
    situação. Complementa o Repositório de Termos (que indexa o PDF anexado às
    movimentações, inclusive o escaneado em papel) mostrando o ATO: quem
    assinou, quando, de onde, e o que ainda está pendente."""
    q = (request.GET.get("q") or "").strip()
    f_situacao = (request.GET.get("situacao") or "").strip()
    f_tipo = (request.GET.get("tipo") or "").strip()
    f_ini = (request.GET.get("data_inicio") or "").strip()
    f_fim = (request.GET.get("data_fim") or "").strip()

    base = (
        TermoAssinatura.objects
        .select_related("colaborador", "colaborador__centro_custo", "criado_por")
        .prefetch_related("itens")
    )

    if q:
        base = base.filter(
            Q(colaborador__nome__icontains=q)
            | Q(numero_termo__icontains=q)
            | Q(codigo_verificacao__icontains=q)
            | Q(signatario_nome__icontains=q)
            | Q(itens__nome__icontains=q)
            | Q(itens__numero_serie__icontains=q)
        ).distinct()

    if f_tipo in ("entrega", "devolucao"):
        base = base.filter(tipo=f_tipo)

    agora = timezone.now()
    if f_situacao == "assinado":
        base = base.filter(status=StatusAssinaturaTermoChoices.ASSINADO)
    elif f_situacao == "pendente":
        base = base.filter(status=StatusAssinaturaTermoChoices.PENDENTE, expira_em__gt=agora)
    elif f_situacao == "expirado":
        base = base.filter(status=StatusAssinaturaTermoChoices.PENDENTE, expira_em__lte=agora)
    elif f_situacao == "cancelado":
        base = base.filter(status=StatusAssinaturaTermoChoices.CANCELADO)

    if f_ini:
        base = base.filter(created_at__date__gte=f_ini)
    if f_fim:
        base = base.filter(created_at__date__lte=f_fim)

    # Contadores sobre TODAS as sessões (não sobre o filtro): são a visão de
    # pendência do setor, e filtrá-los junto faria o painel mostrar zero
    # pendente só porque o usuário está olhando um colaborador específico.
    totais = TermoAssinatura.objects.aggregate(
        total=Count("id"),
        assinados=Count("id", filter=Q(status=StatusAssinaturaTermoChoices.ASSINADO)),
        pendentes=Count("id", filter=Q(status=StatusAssinaturaTermoChoices.PENDENTE,
                                       expira_em__gt=agora)),
        expirados=Count("id", filter=Q(status=StatusAssinaturaTermoChoices.PENDENTE,
                                       expira_em__lte=agora)),
        cancelados=Count("id", filter=Q(status=StatusAssinaturaTermoChoices.CANCELADO)),
    )

    paginator = Paginator(base, 20)
    pagina = paginator.get_page(request.GET.get("page"))

    return render(request, "front/termos/termo_assinatura_list.html", {
        "pagina": pagina,
        "totais": totais,
        "q": q,
        "f_situacao": f_situacao,
        "f_tipo": f_tipo,
        "f_ini": f_ini,
        "f_fim": f_fim,
        "titulo": "Termos assinados digitalmente",
        "subtitulo": "Assinaturas coletadas em tablet, com comprovante de autoria e integridade.",
    })


@login_required
def termo_assinatura_detalhe(request, pk):
    termo = get_object_or_404(
        TermoAssinatura.objects
        .select_related("colaborador", "colaborador__centro_custo", "colaborador__funcao",
                        "colaborador__localidade", "criado_por", "atualizado_por")
        .prefetch_related("itens", "movimentacoes__item"),
        pk=pk,
    )

    integridade = None
    if termo.status == StatusAssinaturaTermoChoices.ASSINADO:
        resultado = tas.verificar_autenticidade(termo.codigo_verificacao)
        integridade = resultado if resultado else None

    return render(request, "front/termos/termo_assinatura_detalhe.html", {
        "termo": termo,
        "integridade": integridade,
        "dados": termo.dados or {},
        "equipamentos": (termo.dados or {}).get("equipamentos") or [],
        "titulo": f"Termo de {termo.get_tipo_display().lower()} · {termo.colaborador.nome}",
        "subtitulo": termo.numero_termo or termo.codigo_verificacao,
    })


@login_required
def termo_assinatura_pdf(request, pk):
    """
    Entrega o PDF assinado. Autenticado e lido pelo campo do model — nunca por
    caminho montado a partir da URL, que é o que abre path traversal.
    `inline` para o navegador abrir no visualizador em vez de baixar: a conferência
    costuma ser uma olhada rápida.
    """
    termo = get_object_or_404(TermoAssinatura.objects.select_related("colaborador"), pk=pk)

    if not termo.pdf:
        raise Http404("Este termo ainda não tem documento assinado.")

    from services.termo_pdf_service import nome_arquivo_pdf

    termo.pdf.open("rb")
    try:
        conteudo = termo.pdf.read()
    finally:
        termo.pdf.close()

    # O PDF traz a assinatura e o documento pessoal que o colaborador digitou
    # no tablet. Quem o abriu fica registrado: é o que permite responder
    # "quem teve acesso a isso?" sem depender da memória de ninguém.
    _log_seguranca.info(
        "Termo assinado lido: termo=%s codigo=%s colaborador=%s por=%s ip=%s",
        termo.pk, termo.codigo_verificacao, termo.colaborador_id,
        request.user.username, tas.ip_do_request(request),
    )

    resposta = HttpResponse(conteudo, content_type="application/pdf")
    resposta["Content-Disposition"] = f'inline; filename="{nome_arquivo_pdf(termo)}"'
    return resposta


@login_required
def termo_assinatura_verificar(request):
    """
    Confere a autenticidade de um termo pelo código impresso no PDF.

    Recalcula o SHA-256 do arquivo armazenado e compara com o hash gravado no
    ato da assinatura — é a resposta para "este PDF que me mandaram é o mesmo
    que o sistema emitiu?".
    """
    codigo = (request.GET.get("codigo") or request.POST.get("codigo") or "").strip().upper()
    resultado = tas.verificar_autenticidade(codigo) if codigo else None

    return render(request, "front/termos/termo_assinatura_verificar.html", {
        "codigo": codigo,
        "resultado": resultado,
        "consultou": bool(codigo),
        "titulo": "Verificar autenticidade de termo",
        "subtitulo": "Informe o código de verificação impresso no rodapé do documento.",
    })
