"""
Kanban de Solicitações de Compra/Estoque — rastreio interno do fluxo
Datasul (requisição → aprovação do gestor) → Paradigma (compra), sem nenhuma
integração automática com esses sistemas externos. Ver `services.requisicao_service`
para toda a lógica de transição de status (nunca feita aqui na view).

Cada card é um `RequisicaoItem`; a coluna em que ele aparece é sempre
recalculada a partir do status do item + o status da `Requisicao` a que
pertence (`coluna_kanban`), nunca armazenada.
"""
import json
from collections import Counter
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Max, Q, Sum
from django.db.models.functions import TruncMonth
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST

from ..forms import (
    ComentarioRequisicaoItemForm,
    ItemPadraoDatasulForm,
    ItemPadraoImportForm,
    RequisicaoItemForm,
    RequisicaoNumerosForm,
    RequisicaoReceberCompraForm,
)
from ..models import (
    Categoria,
    ComentarioRequisicaoItem,
    Item,
    ItemPadraoDatasul,
    Requisicao,
    RequisicaoItem,
    StatusItemSolicitacaoChoices,
    StatusRequisicaoChoices,
    TipoRequisicaoChoices,
)
from services.requisicao_service import (
    COLUNA_APROVADOS,
    COLUNA_LABELS,
    COLUNA_ORDEM,
    COLUNA_PAUSADOS,
    COLUNA_RECEBIDOS,
    RequisicaoService,
    coluna_kanban,
    estagio_kanban,
    item_status_badge_class,
    status_badge_class,
)

# Itens nas colunas "Recebidos" e "Pausados/Cancelados" há mais desse limiar
# somem do board por padrão (continuam visíveis em `requisicao_itens_list`/
# export) — só pra não deixar o board crescendo pra sempre com histórico morto.
_DIAS_OCULTAR_ENCERRADOS = 90
_COLUNAS_HISTORICO = (COLUNA_RECEBIDOS, COLUNA_PAUSADOS)


def _pode_editar_requisicao_item(item, user) -> bool:
    """Só quem criou o item pode editar/excluir o conteúdo — qualquer usuário
    logado pode visualizar (módulo é público de leitura dentro do TI)."""
    if not user or not user.is_authenticated:
        return False
    return item.criado_por_id == user.id


def _pode_editar_comentario(comentario, user) -> bool:
    if not user or not user.is_authenticated:
        return False
    return comentario.criado_por_id == user.id


def _pode_gerenciar_requisicao(requisicao, user) -> bool:
    """Pausar/retomar/cancelar/marcar erro/enviar p/ aprovação/editar números
    — restrito a quem criou a requisição. Aprovar/Reprovar NÃO usa isso (ver
    `requisicao_acao`) — é aberto a qualquer usuário logado por decisão de
    produto, já que ainda não existe um grupo "Gestor" formal."""
    if not user or not user.is_authenticated:
        return False
    return requisicao.criado_por_id == user.id


# ── Board ────────────────────────────────────────────────────────────────

@login_required
def requisicoes_kanban(request):
    f_tipo = (request.GET.get("tipo") or "").strip()
    f_categoria = (request.GET.get("categoria") or "").strip()
    f_requisitante = (request.GET.get("requisitante") or "").strip()
    q = (request.GET.get("q") or "").strip()
    mostrar_antigos = request.GET.get("mostrar_antigos") == "1"

    qs = (
        RequisicaoItem.objects
        .select_related("requisicao", "categoria", "item_vinculado", "criado_por")
        .annotate(comentarios_count=Count("comentarios"))
        .order_by("-created_at")
    )
    if f_tipo:
        qs = qs.filter(tipo=f_tipo)
    if f_categoria:
        qs = qs.filter(categoria_id=f_categoria)
    if f_requisitante:
        qs = qs.filter(criado_por_id=f_requisitante)
    if q:
        qs = qs.filter(Q(descricao__icontains=q) | Q(codigo__icontains=q))

    itens = list(qs)

    if not mostrar_antigos:
        limite = timezone.now() - timedelta(days=_DIAS_OCULTAR_ENCERRADOS)
        itens = [
            item for item in itens
            if not (coluna_kanban(item) in _COLUNAS_HISTORICO and item.updated_at < limite)
        ]

    for item in itens:
        item.pode_editar = _pode_editar_requisicao_item(item, request.user)
        item.elegivel_agrupar = (
            item.requisicao_id is None and item.status == StatusItemSolicitacaoChoices.NAO_SOLICITADO
        )
        item.estagio_rotulo, item.estagio_data = estagio_kanban(item)

    buckets = RequisicaoService.agrupar_itens_por_coluna(itens)
    colunas = [
        {"chave": chave, "titulo": COLUNA_LABELS[chave], "itens": buckets[chave], "total": len(buckets[chave])}
        for chave in COLUNA_ORDEM
    ]

    requisitantes = User.objects.filter(
        pk__in=RequisicaoItem.objects.exclude(criado_por_id__isnull=True).values_list("criado_por_id", flat=True).distinct()
    ).order_by("first_name", "last_name", "username")

    return render(request, "front/requisicoes/kanban.html", {
        "colunas": colunas,
        "categorias": Categoria.objects.order_by("nome"),
        "requisitantes": requisitantes,
        "f_tipo": f_tipo,
        "f_categoria": f_categoria,
        "f_requisitante": f_requisitante,
        "q": q,
        "mostrar_antigos": mostrar_antigos,
        "tipo_choices": TipoRequisicaoChoices.choices,
        "total_itens": len(itens),
    })


@login_required
@require_POST
def requisicao_item_mover(request, pk):
    item = get_object_or_404(RequisicaoItem, pk=pk)
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "erro": "Corpo da requisição inválido."}, status=400)

    coluna_destino = payload.get("coluna_destino")
    coluna_conhecida = payload.get("coluna_conhecida")
    if coluna_destino not in COLUNA_ORDEM:
        return JsonResponse({"ok": False, "erro": "Coluna de destino inválida."}, status=400)

    try:
        RequisicaoService.mover_item_kanban(
            item=item,
            coluna_destino=coluna_destino,
            coluna_conhecida=coluna_conhecida,
            user=request.user,
        )
    except ValidationError as e:
        return JsonResponse({"ok": False, "erro": "; ".join(e.messages)}, status=400)

    return JsonResponse({"ok": True})


# ── Item de Requisição (CRUD + comentários) ─────────────────────────────

def _mapa_itens_vinculaveis_por_numero_serie():
    """`numero_serie` normalizado (strip+upper) -> pk do Item, restrito ao
    mesmo universo elegível pro campo `item_vinculado` (`tem_lote=True`) —
    usado no form pra autovincular quando o código digitado/escolhido bate
    com o número de série de um item já cadastrado. Sem `unique=True` no
    model, número de série duplicado faz o último da consulta prevalecer."""
    return {
        (ns or "").strip().upper(): pk
        for pk, ns in Item.objects.filter(tem_lote=True)
            .exclude(numero_serie__isnull=True).exclude(numero_serie="")
            .values_list("id", "numero_serie")
    }


@login_required
def requisicao_item_create(request):
    form = RequisicaoItemForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        item = RequisicaoService.criar_item(form=form, user=request.user)
        messages.success(request, "Item adicionado ao quadro com sucesso!")
        return redirect("requisicao_item_detail", pk=item.pk)
    return render(request, "front/requisicoes/requisicao_item_form.html", {
        "form": form, "modo": "criar",
        "mapa_ns": _mapa_itens_vinculaveis_por_numero_serie(),
    })


@login_required
def requisicao_item_update(request, pk):
    item = get_object_or_404(RequisicaoItem.objects.select_related("requisicao"), pk=pk)
    if not _pode_editar_requisicao_item(item, request.user):
        messages.error(request, "Apenas quem criou este item pode editá-lo.")
        return redirect("requisicao_item_detail", pk=item.pk)

    form = RequisicaoItemForm(request.POST or None, instance=item)
    if request.method == "POST" and form.is_valid():
        try:
            RequisicaoService.atualizar_item(form=form, user=request.user)
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
            return redirect("requisicao_item_detail", pk=item.pk)
        messages.success(request, "Item atualizado com sucesso!")
        return redirect("requisicao_item_detail", pk=item.pk)
    return render(request, "front/requisicoes/requisicao_item_form.html", {
        "form": form, "modo": "editar", "item": item,
        "mapa_ns": _mapa_itens_vinculaveis_por_numero_serie(),
    })


@login_required
def requisicao_item_delete(request, pk):
    item = get_object_or_404(RequisicaoItem, pk=pk)
    if not _pode_editar_requisicao_item(item, request.user):
        messages.error(request, "Apenas quem criou este item pode excluí-lo.")
        return redirect("requisicao_item_detail", pk=item.pk)

    if request.method == "POST":
        item.delete()
        messages.success(request, "Item excluído com sucesso!")
        return redirect("requisicoes_kanban")
    return render(request, "front/requisicoes/requisicao_item_confirm_delete.html", {"obj": item})


@login_required
def requisicao_item_detail(request, pk):
    item = get_object_or_404(
        RequisicaoItem.objects.select_related("requisicao", "categoria", "item_vinculado", "criado_por"),
        pk=pk,
    )
    comentarios = list(item.comentarios.select_related("criado_por").order_by("-created_at"))
    for c in comentarios:
        c.pode_excluir = _pode_editar_comentario(c, request.user)

    coluna = coluna_kanban(item)
    estagio_rotulo, estagio_data = estagio_kanban(item)
    return render(request, "front/requisicoes/requisicao_item_detail.html", {
        "item": item,
        "comentarios": comentarios,
        "comentario_form": ComentarioRequisicaoItemForm(),
        "pode_editar": _pode_editar_requisicao_item(item, request.user),
        "coluna": coluna,
        "coluna_titulo": COLUNA_LABELS[coluna],
        "pode_retirar": coluna == COLUNA_APROVADOS,
        "pode_desfazer_retirada": coluna == COLUNA_RECEBIDOS,
        "estagio_rotulo": estagio_rotulo,
        "estagio_data": estagio_data,
        "item_badge_class": item_status_badge_class(item.status),
    })


@login_required
@require_POST
def requisicao_item_acao(request, pk):
    """Ações rápidas sobre um item individual, fora do CRUD de conteúdo —
    abertas a qualquer usuário logado (mesma lógica de Aprovar/Reprovar: quem
    retira fisicamente no almoxarifado não precisa ser quem criou o item)."""
    item = get_object_or_404(RequisicaoItem, pk=pk)
    acao = request.POST.get("acao", "")
    try:
        if acao == "retirar":
            RequisicaoService.marcar_item_retirado(item=item, user=request.user)
            messages.success(request, "Item marcado como retirado no almoxarifado.")
        elif acao == "desfazer_retirada":
            RequisicaoService.desfazer_retirada_item(item=item, user=request.user)
            messages.success(request, "Retirada desfeita — item voltou para Aprovados.")
        else:
            messages.error(request, "Ação desconhecida.")
    except ValidationError as e:
        messages.error(request, "; ".join(e.messages))
    return redirect("requisicao_item_detail", pk=pk)


@login_required
def requisicao_item_receber_compra(request, pk):
    """Recebimento de uma compra vinculada a um item de estoque: dá entrada
    real (NF, custo, CC, localidade) e marca o item da requisição como
    retirado — ver `RequisicaoService.finalizar_compra_estoque`. Só se aplica
    a itens tipo Compra com `item_vinculado` preenchido; os demais continuam
    usando a ação simples "Marcar como Retirado" (`requisicao_item_acao`)."""
    item = get_object_or_404(
        RequisicaoItem.objects.select_related("requisicao", "item_vinculado"),
        pk=pk,
    )
    if coluna_kanban(item) != COLUNA_APROVADOS:
        messages.error(request, 'Só é possível receber um item que esteja em "Aprovados".')
        return redirect("requisicao_item_detail", pk=item.pk)
    if item.tipo != TipoRequisicaoChoices.COMPRA or not item.item_vinculado_id:
        messages.error(request, "Esta ação só está disponível para itens de Compra vinculados a um item de estoque.")
        return redirect("requisicao_item_detail", pk=item.pk)

    form = RequisicaoReceberCompraForm(request.POST or None, initial={
        "fornecedor": item.item_vinculado.fornecedor_id,
        "localidade_destino": item.item_vinculado.localidade_id,
        "centro_custo_destino": item.item_vinculado.centro_custo_id,
    })
    if request.method == "POST" and form.is_valid():
        try:
            RequisicaoService.finalizar_compra_estoque(
                item=item,
                fornecedor=form.cleaned_data["fornecedor"],
                numero_nf=form.cleaned_data["numero_nf"],
                custo_unitario=form.cleaned_data["custo_unitario"],
                localidade_destino=form.cleaned_data["localidade_destino"],
                centro_custo_destino=form.cleaned_data["centro_custo_destino"],
                observacao=form.cleaned_data.get("observacao"),
                user=request.user,
            )
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
            return redirect("requisicao_item_detail", pk=item.pk)
        messages.success(
            request,
            f'Entrada de {item.quantidade} unidade(s) registrada em "{item.item_vinculado.nome}" '
            "— item marcado como retirado.",
        )
        return redirect("requisicao_item_detail", pk=item.pk)

    return render(request, "front/requisicoes/requisicao_item_receber_compra.html", {
        "item": item,
        "form": form,
    })


@login_required
def requisicao_item_comentar(request, pk):
    item = get_object_or_404(RequisicaoItem, pk=pk)
    if request.method == "POST":
        form = ComentarioRequisicaoItemForm(request.POST)
        if form.is_valid():
            comentario = form.save(commit=False)
            comentario.requisicao_item = item
            comentario.criado_por = request.user
            comentario.atualizado_por = request.user
            comentario.save()
            messages.success(request, "Comentário adicionado.")
        else:
            messages.error(request, "Não foi possível salvar o comentário.")
    return redirect("requisicao_item_detail", pk=item.pk)


@login_required
def requisicao_comentario_excluir(request, pk):
    comentario = get_object_or_404(ComentarioRequisicaoItem, pk=pk)
    item_pk = comentario.requisicao_item_id
    if not _pode_editar_comentario(comentario, request.user):
        messages.error(request, "Apenas quem escreveu este comentário pode excluí-lo.")
        return redirect("requisicao_item_detail", pk=item_pk)

    if request.method == "POST":
        comentario.delete()
        messages.success(request, "Comentário excluído.")
    return redirect("requisicao_item_detail", pk=item_pk)


# ── Requisição (agrupamento + ciclo de vida) ────────────────────────────

@login_required
def requisicao_create_from_itens(request):
    if request.method != "POST":
        return redirect("requisicoes_kanban")

    item_ids = request.POST.getlist("item_ids")
    tipo = request.POST.get("tipo")
    if not item_ids:
        messages.error(request, "Selecione ao menos um item para agrupar em uma requisição.")
        return redirect("requisicoes_kanban")

    try:
        requisicao = RequisicaoService.criar_requisicao_de_itens(item_ids=item_ids, tipo=tipo, user=request.user)
    except ValidationError as e:
        messages.error(request, "; ".join(e.messages))
        return redirect("requisicoes_kanban")

    messages.success(
        request,
        "Requisição criada em rascunho — adicione o número do Datasul e envie para aprovação quando estiver pronta.",
    )
    return redirect("requisicao_detail", pk=requisicao.pk)


#: Status de onde a requisição ainda pode ser pausada/retomada/cancelada —
#: usado tanto pra decidir o card de Ações quanto o grupo "Encerrar / pausar".
_STATUS_ENCERRAVEIS = {
    StatusRequisicaoChoices.RASCUNHO, StatusRequisicaoChoices.SOLICITADA,
    StatusRequisicaoChoices.ENVIADA_APROVACAO, StatusRequisicaoChoices.APROVADA,
    StatusRequisicaoChoices.PAUSADA, StatusRequisicaoChoices.COM_ERRO,
}


@login_required
def requisicao_detail(request, pk):
    requisicao = get_object_or_404(Requisicao, pk=pk)
    itens = list(requisicao.itens.select_related("categoria", "item_vinculado", "criado_por").order_by("-created_at"))
    for item in itens:
        item.requisicao = requisicao  # evita reconsultar a mesma requisição por item em estagio_kanban()
        item.estagio_rotulo, item.estagio_data = estagio_kanban(item)
        item.badge_class = item_status_badge_class(item.status)

    pode_gerenciar = _pode_gerenciar_requisicao(requisicao, request.user)
    status = requisicao.status
    itens_pendentes_retirada = any(i.status == StatusItemSolicitacaoChoices.SOLICITADO for i in itens)

    # Flags de visibilidade calculadas aqui (não no template) — cada uma
    # corresponde a um botão/grupo de ação específico da tela de detalhe.
    # Aprovar/Reprovar/Retirar continuam abertos a qualquer usuário logado
    # (ver `_pode_gerenciar_requisicao`); as demais exigem ser o criador.
    mostrar_solicitar = pode_gerenciar and status == StatusRequisicaoChoices.RASCUNHO
    mostrar_enviar_aprovacao = pode_gerenciar and status in (
        StatusRequisicaoChoices.RASCUNHO, StatusRequisicaoChoices.SOLICITADA,
    )
    mostrar_aprovar_reprovar = status == StatusRequisicaoChoices.ENVIADA_APROVACAO
    mostrar_retirar_todos = status == StatusRequisicaoChoices.APROVADA and itens_pendentes_retirada
    mostrar_avancar = (
        mostrar_solicitar or mostrar_enviar_aprovacao or mostrar_aprovar_reprovar or mostrar_retirar_todos
    )

    mostrar_encerrar = pode_gerenciar and status in _STATUS_ENCERRAVEIS
    mostrar_excluir = pode_gerenciar and status == StatusRequisicaoChoices.RASCUNHO
    mostrar_pausar_erro = pode_gerenciar and status in (
        StatusRequisicaoChoices.SOLICITADA, StatusRequisicaoChoices.ENVIADA_APROVACAO, StatusRequisicaoChoices.APROVADA,
    )
    mostrar_retomar = pode_gerenciar and status in (StatusRequisicaoChoices.PAUSADA, StatusRequisicaoChoices.COM_ERRO)

    mostrar_card_acoes = pode_gerenciar or status in (
        StatusRequisicaoChoices.ENVIADA_APROVACAO, StatusRequisicaoChoices.APROVADA,
    )

    return render(request, "front/requisicoes/requisicao_detail.html", {
        "requisicao": requisicao,
        "itens": itens,
        "pode_gerenciar": pode_gerenciar,
        "numeros_form": RequisicaoNumerosForm(instance=requisicao),
        "itens_pendentes_retirada": itens_pendentes_retirada,
        "badge_class": status_badge_class(requisicao.status),
        "mostrar_card_acoes": mostrar_card_acoes,
        "mostrar_avancar": mostrar_avancar,
        "mostrar_solicitar": mostrar_solicitar,
        "mostrar_enviar_aprovacao": mostrar_enviar_aprovacao,
        "mostrar_aprovar_reprovar": mostrar_aprovar_reprovar,
        "mostrar_retirar_todos": mostrar_retirar_todos,
        "mostrar_encerrar": mostrar_encerrar,
        "mostrar_excluir": mostrar_excluir,
        "mostrar_pausar_erro": mostrar_pausar_erro,
        "mostrar_retomar": mostrar_retomar,
    })


@login_required
def requisicao_acao(request, pk):
    requisicao = get_object_or_404(Requisicao, pk=pk)
    if request.method != "POST":
        return redirect("requisicao_detail", pk=pk)

    acao = request.POST.get("acao", "")

    # Aprovar/Reprovar/Retirar: qualquer usuário logado pode — decisão de
    # produto (não há grupo "Gestor" formal ainda, e quem retira fisicamente
    # no almoxarifado não precisa ser quem criou a requisição), diferente das
    # demais ações abaixo que exigem ser o criador da requisição.
    if acao in ("aprovar", "reprovar"):
        try:
            RequisicaoService.mudar_status_requisicao(requisicao=requisicao, acao=acao, user=request.user)
            messages.success(request, "Requisição atualizada com sucesso!")
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
        return redirect("requisicao_detail", pk=pk)

    if acao == "retirar":
        try:
            _, puladas = RequisicaoService.marcar_requisicao_retirada(requisicao=requisicao, user=request.user)
            if puladas:
                nomes = ", ".join(i.descricao for i in puladas)
                messages.warning(
                    request,
                    f"Requisição marcada como retirada — exceto {len(puladas)} item(ns) de Compra "
                    f'vinculados a estoque ({nomes}), que precisam ser recebidos individualmente em '
                    '"Receber Compra e Dar Entrada no Estoque".',
                )
            else:
                messages.success(request, "Requisição inteira marcada como retirada no almoxarifado.")
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
        return redirect("requisicao_detail", pk=pk)

    if not _pode_gerenciar_requisicao(requisicao, request.user):
        messages.error(request, "Apenas quem criou esta requisição pode executar essa ação.")
        return redirect("requisicao_detail", pk=pk)

    if acao == "solicitar":
        try:
            RequisicaoService.mudar_status_requisicao(requisicao=requisicao, acao="solicitar", user=request.user)
            messages.success(request, "Requisição marcada como Solicitada.")
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
        return redirect("requisicao_detail", pk=pk)

    if acao == "enviar_aprovacao":
        try:
            RequisicaoService.enviar_para_aprovacao(
                requisicao=requisicao,
                numero_datasul=request.POST.get("numero_datasul", ""),
                user=request.user,
            )
            messages.success(request, "Requisição enviada para aprovação!")
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
        return redirect("requisicao_detail", pk=pk)

    if acao in ("pausar", "retomar", "cancelar", "marcar_erro"):
        try:
            RequisicaoService.mudar_status_requisicao(requisicao=requisicao, acao=acao, user=request.user)
            messages.success(request, "Requisição atualizada com sucesso!")
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
        return redirect("requisicao_detail", pk=pk)

    if acao == "definir_numeros":
        form = RequisicaoNumerosForm(request.POST, instance=requisicao)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.atualizado_por = request.user
            obj.save()
            messages.success(request, "Números atualizados.")
        else:
            messages.error(request, "Não foi possível salvar os números informados.")
        return redirect("requisicao_detail", pk=pk)

    if acao == "excluir":
        try:
            RequisicaoService.excluir_requisicao_rascunho(requisicao=requisicao, user=request.user)
            messages.success(request, "Requisição excluída — os itens voltaram ao quadro.")
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
            return redirect("requisicao_detail", pk=pk)
        return redirect("requisicoes_kanban")

    messages.error(request, "Ação desconhecida.")
    return redirect("requisicao_detail", pk=pk)


# ── Listas / consulta ────────────────────────────────────────────────────

@login_required
def requisicoes_list(request):
    f_status = (request.GET.get("status") or "").strip()
    f_tipo = (request.GET.get("tipo") or "").strip()
    q = (request.GET.get("q") or "").strip()

    qs = (
        Requisicao.objects
        .select_related("criado_por", "decidida_por")
        .annotate(
            itens_count=Count("itens", distinct=True),
            itens_retirados_count=Count("itens", filter=Q(itens__status="retirado"), distinct=True),
            finalizada_em=Max("itens__retirado_em"),
        )
        .order_by("-created_at")
    )
    if f_status:
        qs = qs.filter(status=f_status)
    if f_tipo:
        qs = qs.filter(tipo=f_tipo)
    if q:
        qs = qs.filter(numero_datasul__icontains=q)

    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page", 1))
    get_copy = request.GET.copy()
    if "page" in get_copy:
        del get_copy["page"]

    requisicoes = list(page_obj.object_list)
    for req in requisicoes:
        req.esta_finalizada = req.itens_count > 0 and req.itens_count == req.itens_retirados_count
        req.badge_class = status_badge_class(req.status)

    return render(request, "front/requisicoes/requisicoes_list.html", {
        "page_obj": page_obj,
        "requisicoes": requisicoes,
        "f_status": f_status,
        "f_tipo": f_tipo,
        "q": q,
        "qs_keep": get_copy.urlencode(),
        "status_choices": StatusRequisicaoChoices.choices,
        "tipo_choices": TipoRequisicaoChoices.choices,
    })


@login_required
def requisicao_itens_list(request):
    f_status = (request.GET.get("status") or "").strip()
    f_tipo = (request.GET.get("tipo") or "").strip()
    f_categoria = (request.GET.get("categoria") or "").strip()
    q = (request.GET.get("q") or "").strip()

    qs = (
        RequisicaoItem.objects
        .select_related("requisicao", "categoria", "criado_por")
        .order_by("-created_at")
    )
    if f_status:
        qs = qs.filter(status=f_status)
    if f_tipo:
        qs = qs.filter(tipo=f_tipo)
    if f_categoria:
        qs = qs.filter(categoria_id=f_categoria)
    if q:
        qs = qs.filter(Q(descricao__icontains=q) | Q(codigo__icontains=q))

    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page", 1))
    get_copy = request.GET.copy()
    if "page" in get_copy:
        del get_copy["page"]

    itens = page_obj.object_list
    for item in itens:
        item.coluna_titulo = COLUNA_LABELS[coluna_kanban(item)]
        item.badge_class = item_status_badge_class(item.status)

    return render(request, "front/requisicoes/requisicao_itens_list.html", {
        "page_obj": page_obj,
        "itens": itens,
        "f_status": f_status,
        "f_tipo": f_tipo,
        "f_categoria": f_categoria,
        "q": q,
        "qs_keep": get_copy.urlencode(),
        "status_choices": StatusItemSolicitacaoChoices.choices,
        "tipo_choices": TipoRequisicaoChoices.choices,
        "categorias": Categoria.objects.order_by("nome"),
    })


# ── Dashboard (itens/tipos mais solicitados) ────────────────────────────

@login_required
def requisicoes_dashboard(request):
    """KPIs + rankings de demanda do módulo de Requisições: itens mais
    pedidos (geral e por tipo Compra/Estoque), status das requisições no
    período e requisitantes mais ativos. Agregação em Python (não em SQL)
    porque o agrupamento é por descrição normalizada (case/espaço
    insensível) — os itens são texto livre, sem FK pra um catálogo único
    (`ItemPadraoDatasul` é só atalho de preenchimento, não vínculo)."""
    hoje = timezone.localdate()
    dt_ini = parse_date(request.GET.get("inicio") or "") or (hoje - timedelta(days=90))
    dt_fim = parse_date(request.GET.get("fim") or "") or hoje
    if dt_ini > dt_fim:
        dt_ini, dt_fim = dt_fim, dt_ini

    f_tipo = (request.GET.get("tipo") or "").strip()
    f_categoria = (request.GET.get("categoria") or "").strip()

    itens_qs = RequisicaoItem.objects.filter(
        created_at__date__gte=dt_ini, created_at__date__lte=dt_fim,
    )
    if f_tipo:
        itens_qs = itens_qs.filter(tipo=f_tipo)
    if f_categoria:
        itens_qs = itens_qs.filter(categoria_id=f_categoria)

    req_qs = Requisicao.objects.filter(created_at__date__gte=dt_ini, created_at__date__lte=dt_fim)
    if f_tipo:
        req_qs = req_qs.filter(tipo=f_tipo)

    itens = list(itens_qs.select_related("categoria", "criado_por"))

    total_itens = len(itens)
    total_requisicoes = req_qs.count()
    quantidade_total = sum(i.quantidade for i in itens)
    ticket_medio = round(total_itens / total_requisicoes, 1) if total_requisicoes else 0
    aprovadas = req_qs.filter(status=StatusRequisicaoChoices.APROVADA).count()
    taxa_aprovacao = round(aprovadas / total_requisicoes * 100, 1) if total_requisicoes else 0

    def _top_itens(lista, limit=10):
        agregados = {}
        for item in lista:
            chave = (item.descricao or "").strip().lower()
            if not chave:
                continue
            reg = agregados.setdefault(chave, {
                "descricao": item.descricao, "codigo": item.codigo, "qtd_total": 0, "pedidos": 0,
            })
            reg["qtd_total"] += item.quantidade
            reg["pedidos"] += 1
            if not reg["codigo"] and item.codigo:
                reg["codigo"] = item.codigo
        return sorted(agregados.values(), key=lambda r: (-r["qtd_total"], -r["pedidos"]))[:limit]

    top_geral = _top_itens(itens)
    top_estoque = _top_itens([i for i in itens if i.tipo == TipoRequisicaoChoices.ESTOQUE])
    top_compra = _top_itens([i for i in itens if i.tipo == TipoRequisicaoChoices.COMPRA])

    # Requisitantes mais ativos no período (soma da quantidade pedida)
    contagem_requisitantes = Counter()
    nomes_requisitantes = {}
    for item in itens:
        if not item.criado_por_id:
            continue
        contagem_requisitantes[item.criado_por_id] += item.quantidade
        nomes_requisitantes[item.criado_por_id] = item.criado_por.get_full_name() or item.criado_por.username
    top_requisitantes = [
        {"nome": nomes_requisitantes[uid], "qtd": qtd}
        for uid, qtd in contagem_requisitantes.most_common(8)
    ]

    # Categorias mais solicitadas
    contagem_categorias = Counter()
    for item in itens:
        contagem_categorias[str(item.categoria)] += item.quantidade
    top_categorias = contagem_categorias.most_common(8)

    # Status das requisições no período — lista com barra proporcional (não
    # donut: contagem categórica lê melhor em lista do que em gráfico de
    # pizza). Tom reaproveita a mesma semântica dos badges do quadro
    # (status_badge_class), só traduzido pra sufixo de classe CSS local.
    _TONE_MAP = {
        "kan-badge-success": "success", "kan-badge-warning": "warning",
        "kan-badge-danger": "danger", "kan-badge-primary": "primary",
    }
    status_labels_map = dict(StatusRequisicaoChoices.choices)
    status_rows = list(req_qs.values("status").annotate(n=Count("id")).order_by("-n"))
    status_dist = [
        {
            "label": status_labels_map.get(r["status"], r["status"]),
            "n": r["n"],
            "pct": round(r["n"] / total_requisicoes * 100) if total_requisicoes else 0,
            "tone": _TONE_MAP.get(status_badge_class(r["status"]), "neutral"),
        }
        for r in status_rows
    ]

    # Evolução mensal da quantidade solicitada — série contínua (sem meses
    # "buracos"), mesma técnica usada no dashboard de Toner.
    mensal_map = {
        (row["mes"].year, row["mes"].month): row["qtd"] or 0
        for row in itens_qs.annotate(mes=TruncMonth("created_at")).values("mes").annotate(qtd=Sum("quantidade"))
        if row["mes"]
    }
    _MESES_PT = ["", "Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]
    mes_labels, mes_qtd = [], []
    cursor = date(dt_ini.year, dt_ini.month, 1)
    limite = date(dt_fim.year, dt_fim.month, 1)
    guard = 0
    while cursor <= limite and guard < 60:
        mes_labels.append(f"{_MESES_PT[cursor.month]}/{str(cursor.year)[2:]}")
        mes_qtd.append(mensal_map.get((cursor.year, cursor.month), 0))
        cursor = date(cursor.year + 1, 1, 1) if cursor.month == 12 else date(cursor.year, cursor.month + 1, 1)
        guard += 1

    return render(request, "front/requisicoes/requisicoes_dashboard.html", {
        "dt_ini": dt_ini, "dt_fim": dt_fim,
        "f_tipo": f_tipo, "f_categoria": f_categoria,
        "tipo_choices": TipoRequisicaoChoices.choices,
        "categorias": Categoria.objects.order_by("nome"),

        "total_itens": total_itens,
        "total_requisicoes": total_requisicoes,
        "quantidade_total": quantidade_total,
        "ticket_medio": ticket_medio,
        "taxa_aprovacao": taxa_aprovacao,

        "top_geral": top_geral,
        "top_estoque": top_estoque,
        "top_compra": top_compra,
        "top_requisitantes": top_requisitantes,
        "top_categorias": top_categorias,
        "status_dist": status_dist,

        "mes_labels": mes_labels,
        "mes_qtd": mes_qtd,
    })


# ── Catálogo de itens padrão (código Datasul) ───────────────────────────
# Atalho de preenchimento pro formulário de item — não é um cadastro de
# equipamento (isso é o `Item`), é só "código X = descrição Y" pra não
# digitar a mesma coisa toda vez que um item recorrente é solicitado.

@login_required
def itens_padrao_list(request):
    q = (request.GET.get("q") or "").strip()
    f_categoria = (request.GET.get("categoria") or "").strip()
    f_ativo = request.GET.get("ativo", "1")

    qs = ItemPadraoDatasul.objects.select_related("categoria").order_by("descricao")
    if q:
        qs = qs.filter(Q(descricao__icontains=q) | Q(codigo__icontains=q))
    if f_categoria:
        qs = qs.filter(categoria_id=f_categoria)
    if f_ativo == "1":
        qs = qs.filter(ativo=True)
    elif f_ativo == "0":
        qs = qs.filter(ativo=False)

    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page", 1))
    get_copy = request.GET.copy()
    if "page" in get_copy:
        del get_copy["page"]

    return render(request, "front/requisicoes/itens_padrao_list.html", {
        "page_obj": page_obj,
        "itens": page_obj.object_list,
        "q": q,
        "f_categoria": f_categoria,
        "f_ativo": f_ativo,
        "qs_keep": get_copy.urlencode(),
        "categorias": Categoria.objects.order_by("nome"),
    })


@login_required
def itens_padrao_create(request):
    form = ItemPadraoDatasulForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.criado_por = request.user
        obj.atualizado_por = request.user
        obj.save()
        messages.success(request, "Item padrão cadastrado com sucesso!")
        return redirect("itens_padrao_list")
    return render(request, "front/requisicoes/itens_padrao_form.html", {"form": form, "modo": "criar"})


@login_required
def itens_padrao_update(request, pk):
    obj = get_object_or_404(ItemPadraoDatasul, pk=pk)
    form = ItemPadraoDatasulForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        item = form.save(commit=False)
        item.atualizado_por = request.user
        item.save()
        messages.success(request, "Item padrão atualizado com sucesso!")
        return redirect("itens_padrao_list")
    return render(request, "front/requisicoes/itens_padrao_form.html", {"form": form, "modo": "editar", "obj": obj})


@login_required
def itens_padrao_delete(request, pk):
    obj = get_object_or_404(ItemPadraoDatasul, pk=pk)
    if request.method == "POST":
        obj.delete()
        messages.success(request, "Item padrão excluído com sucesso!")
        return redirect("itens_padrao_list")
    return render(request, "front/requisicoes/itens_padrao_confirm_delete.html", {"obj": obj})


@login_required
def itens_padrao_importar(request):
    form = ItemPadraoImportForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            criados, atualizados, erros = RequisicaoService.importar_catalogo_datasul(
                arquivo=form.cleaned_data["arquivo"], user=request.user,
            )
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
            return redirect("itens_padrao_importar")

        if criados or atualizados:
            messages.success(request, f"Importação concluída: {criados} criado(s), {atualizados} atualizado(s).")
        if erros:
            messages.warning(
                request,
                f"{len(erros)} linha(s) não importada(s): " + " | ".join(erros[:10])
                + (f" (+{len(erros) - 10} outras)" if len(erros) > 10 else ""),
            )
        if not criados and not atualizados and not erros:
            messages.info(request, "Nenhuma linha encontrada na planilha.")
        return redirect("itens_padrao_list")
    return render(request, "front/requisicoes/itens_padrao_importar.html", {"form": form})
