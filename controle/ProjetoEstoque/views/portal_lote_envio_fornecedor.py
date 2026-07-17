"""
Portal do Fornecedor — Lotes de Envio (fornecedor → TI).

O fornecedor monta um "carrinho" (`LoteEnvioFornecedor`) com itens de troca
antecipada e/ou cadastro de equipamento novo, anexa NF, edita/exclui itens, e
envia o lote inteiro ou um item isolado ao TI. Toda a lógica de negócio vive em
`services/lote_envio_fornecedor_service.py` — esta view só lê POST e delega
(CLAUDE.md regra 2: forms/views nunca fazem lógica de negócio).
"""
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render

from .portal_fornecedor import _itens_elegiveis_troca, fornecedor_required
from ..models import (
    Categoria,
    CentroCusto,
    Localidade,
    LoteEnvioFornecedor,
    LoteEnvioFornecedorItem,
    OrdemManutencao,
    StatusLoteEnvioFornecedorChoices,
    StatusOrdemManutencaoChoices,
    Subtipo,
    TipoItemLoteEnvioChoices,
)


def _lote_do_fornecedor_or_404(request, pk):
    return get_object_or_404(
        LoteEnvioFornecedor.objects.select_related("fornecedor"),
        pk=pk, fornecedor=request.fornecedor,
    )


def _item_do_fornecedor_or_404(request, pk):
    return get_object_or_404(
        LoteEnvioFornecedorItem.objects.select_related(
            "lote", "item_defeituoso", "item_resultado", "ordem",
        ),
        pk=pk, lote__fornecedor=request.fornecedor,
    )


def _campos_equipamento_novo_do_post(request):
    return {
        "novo_nome": request.POST.get("novo_nome", ""),
        "novo_numero_serie": request.POST.get("novo_numero_serie", ""),
        "novo_marca": request.POST.get("novo_marca", ""),
        "novo_modelo": request.POST.get("novo_modelo", ""),
        "novo_categoria": Categoria.objects.filter(pk=request.POST.get("novo_categoria")).first(),
        "novo_subtipo": Subtipo.objects.filter(pk=request.POST.get("novo_subtipo")).first(),
        "novo_localidade": Localidade.objects.filter(pk=request.POST.get("novo_localidade")).first(),
        "novo_centro_custo": CentroCusto.objects.filter(pk=request.POST.get("novo_centro_custo")).first(),
        "novo_locado": request.POST.get("novo_locado", "nao"),
        "novo_pmb": request.POST.get("novo_pmb", "nao"),
        "novo_valor": request.POST.get("novo_valor", ""),
        "novo_contrato": request.POST.get("novo_contrato", ""),
        "novo_tempo_contrato_meses": request.POST.get("novo_tempo_contrato_meses", ""),
        "novo_cobranca_proximo_ano": request.POST.get("novo_cobranca_proximo_ano", "nao"),
    }


@fornecedor_required
def portal_lote_envio_list(request):
    from services.lote_envio_fornecedor_service import LoteEnvioFornecedorService

    if request.method == "POST" and request.POST.get("acao") == "criar_lote":
        lote = LoteEnvioFornecedorService.criar_lote(
            fornecedor=request.fornecedor, user=request.user,
            nome=request.POST.get("nome", ""),
        )
        messages.success(request, f'Lote "{lote.nome}" criado — adicione os equipamentos.')
        return redirect("portal_lote_envio_detail", pk=lote.pk)

    lotes = (
        LoteEnvioFornecedor.objects
        .filter(fornecedor=request.fornecedor)
        .prefetch_related("itens")
        .order_by("-created_at")
    )
    context = {
        "fornecedor": request.fornecedor,
        "lotes": lotes,
        "active_nav": "lotes_envio",
    }
    return render(request, "front/portal/portal_lote_envio_list.html", context)


@fornecedor_required
def portal_lote_envio_detail(request, pk: int):
    from services.lote_envio_fornecedor_service import LoteEnvioFornecedorService

    lote = _lote_do_fornecedor_or_404(request, pk)

    if request.method == "POST":
        acao = request.POST.get("acao", "")
        try:
            if acao == "renomear":
                LoteEnvioFornecedorService.renomear_lote(
                    lote=lote, user=request.user, nome=request.POST.get("nome", ""),
                )
                messages.success(request, "Lote renomeado.")
            elif acao == "anexar_nf":
                arquivos = request.FILES.getlist("nf")
                if not arquivos:
                    messages.error(request, "Selecione ao menos um arquivo de NF.")
                else:
                    LoteEnvioFornecedorService.anexar_nf(
                        lote=lote, arquivos=arquivos,
                        descricao=request.POST.get("descricao", ""), user=request.user,
                    )
                    messages.success(request, f"{len(arquivos)} nota(s) fiscal(is) anexada(s).")
            elif acao == "excluir_nf":
                anexo = lote.anexos.filter(pk=request.POST.get("anexo_id")).first()
                if not anexo:
                    messages.error(request, "Nota fiscal não encontrada.")
                else:
                    LoteEnvioFornecedorService.excluir_nf(anexo=anexo, user=request.user)
                    messages.success(request, "Nota fiscal excluída.")
            elif acao == "enviar_lote":
                LoteEnvioFornecedorService.enviar_lote(lote=lote, user=request.user)
                messages.success(request, f'Lote "{lote.nome}" enviado ao TI.')
            elif acao == "excluir_lote":
                LoteEnvioFornecedorService.excluir_lote(lote=lote, user=request.user)
                messages.success(request, "Lote excluído.")
                return redirect("portal_lote_envio_list")
            else:
                messages.error(request, "Ação inválida.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        return redirect("portal_lote_envio_detail", pk=lote.pk)

    itens = (
        lote.itens
        .select_related("item_defeituoso", "item_resultado", "ordem", "ordem__item", "localidade_devolucao")
        .order_by("created_at")
    )
    anexos = lote.anexos.select_related("criado_por").all()
    context = {
        "fornecedor": request.fornecedor,
        "lote": lote,
        "itens": itens,
        "anexos": anexos,
        "categorias": Categoria.objects.order_by("nome"),
        "subtipos": Subtipo.objects.select_related("categoria").order_by("nome"),
        "localidades": Localidade.objects.order_by("local"),
        "centros_custo": CentroCusto.objects.order_by("numero"),
        "active_nav": "lotes_envio",
    }
    return render(request, "front/portal/portal_lote_envio_detail.html", context)


@fornecedor_required
def portal_lote_envio_item_enviar(request, pk: int):
    from services.lote_envio_fornecedor_service import LoteEnvioFornecedorService
    item_lote = _item_do_fornecedor_or_404(request, pk)
    lote_id = item_lote.lote_id
    if request.method == "POST":
        try:
            LoteEnvioFornecedorService.enviar_item(item_lote=item_lote, user=request.user)
            messages.success(request, "Item enviado ao TI.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
    return redirect("portal_lote_envio_detail", pk=lote_id)


@fornecedor_required
def portal_lote_envio_item_excluir(request, pk: int):
    from services.lote_envio_fornecedor_service import LoteEnvioFornecedorService
    item_lote = _item_do_fornecedor_or_404(request, pk)
    lote_id = item_lote.lote_id
    if request.method == "POST":
        try:
            LoteEnvioFornecedorService.excluir_item(item_lote=item_lote, user=request.user)
            messages.success(request, "Item removido do lote.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
    return redirect("portal_lote_envio_detail", pk=lote_id)


@fornecedor_required
def portal_lote_envio_item_editar(request, pk: int):
    from services.lote_envio_fornecedor_service import LoteEnvioFornecedorService
    item_lote = _item_do_fornecedor_or_404(request, pk)
    if request.method == "POST":
        try:
            if item_lote.tipo == TipoItemLoteEnvioChoices.TROCA_ANTECIPADA:
                LoteEnvioFornecedorService.editar_item_troca_antecipada(
                    item_lote=item_lote, user=request.user,
                    sub_modelo=request.POST.get("sub_modelo", ""),
                    sub_serie=request.POST.get("sub_serie", ""),
                    sub_marca=request.POST.get("sub_marca", ""),
                    sub_data_contrato=request.POST.get("sub_data_contrato", ""),
                )
            else:
                LoteEnvioFornecedorService.editar_item_equipamento_novo(
                    item_lote=item_lote, user=request.user,
                    **_campos_equipamento_novo_do_post(request),
                )
            messages.success(request, "Item atualizado.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
    return redirect("portal_lote_envio_detail", pk=item_lote.lote_id)


@fornecedor_required
def portal_lote_envio_item_equipamento_novo(request):
    """Form de cadastro de equipamento novo — fica em rascunho no lote escolhido
    (ou criado ali mesmo) pelo fornecedor até ele enviar (ver `enviar_item`)."""
    from services.lote_envio_fornecedor_service import LoteEnvioFornecedorService

    if request.method == "POST":
        lote_id = (request.POST.get("lote_id") or "").strip()
        try:
            item_lote = LoteEnvioFornecedorService.adicionar_item_equipamento_novo(
                fornecedor=request.fornecedor, user=request.user,
                lote_id=(lote_id if lote_id.isdigit() else None),
                lote_nome_novo=request.POST.get("lote_nome_novo", ""),
                **_campos_equipamento_novo_do_post(request),
            )
            messages.success(request, "Equipamento adicionado ao lote de envio.")
            return redirect("portal_lote_envio_detail", pk=item_lote.lote_id)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))

    lote_preselect_id = (request.GET.get("lote") or "").strip()
    lote_preselect = None
    if lote_preselect_id.isdigit():
        lote_preselect = LoteEnvioFornecedor.objects.filter(
            pk=lote_preselect_id, fornecedor=request.fornecedor,
            status=StatusLoteEnvioFornecedorChoices.ABERTO,
        ).first()
    context = {
        "fornecedor": request.fornecedor,
        "categorias": Categoria.objects.order_by("nome"),
        "subtipos": Subtipo.objects.select_related("categoria").order_by("nome"),
        "localidades": Localidade.objects.order_by("local"),
        "centros_custo": CentroCusto.objects.order_by("numero"),
        "lotes_abertos": list(LoteEnvioFornecedorService.lotes_abertos(request.fornecedor)),
        "lote_preselect": lote_preselect,
        "active_nav": "lotes_envio",
    }
    return render(request, "front/portal/portal_lote_envio_item_equipamento_novo.html", context)


@fornecedor_required
def portal_lote_envio_item_reparo_concluido(request):
    """Separa uma Ordem de Manutenção já reparada (ou reprovada) para retorno
    físico ao TI através do Lote de Envio — mesmo mecanismo de rascunho/NF/
    envio dos outros 2 tipos. Reaproveita
    `LoteEnvioFornecedorService.adicionar_item_reparo_concluido`, que já é usada
    pelo fluxo "Separar para envio ao TI" da própria tela da OS; esta view só
    oferece um ponto de entrada direto pelo Lote de Envio, sem precisar navegar
    até a OS específica primeiro."""
    from services.lote_envio_fornecedor_service import LoteEnvioFornecedorService

    if request.method == "POST":
        lote_id = (request.POST.get("lote_id") or "").strip()
        ordem = OrdemManutencao.objects.filter(
            pk=request.POST.get("ordem_id"), fornecedor=request.fornecedor,
        ).first()
        if not ordem:
            messages.error(request, "Selecione uma ordem de manutenção válida.")
        else:
            try:
                item_lote = LoteEnvioFornecedorService.adicionar_item_reparo_concluido(
                    fornecedor=request.fornecedor, user=request.user, ordem=ordem,
                    localidade_devolucao_id=request.POST.get("localidade_devolucao"),
                    valor_avaliacao_tecnica=request.POST.get("valor_avaliacao_tecnica", ""),
                    lote_id=(lote_id if lote_id.isdigit() else None),
                    lote_nome_novo=request.POST.get("lote_nome_novo", ""),
                )
                messages.success(request, "Equipamento separado para retorno ao TI.")
                return redirect("portal_lote_envio_detail", pk=item_lote.lote_id)
            except ValidationError as exc:
                messages.error(request, "; ".join(exc.messages))

    ordens = [
        o for o in OrdemManutencao.objects.filter(
            fornecedor=request.fornecedor,
            status__in=[StatusOrdemManutencaoChoices.REPARADO, StatusOrdemManutencaoChoices.REPROVADO],
        ).select_related("item").order_by("-created_at")
        if LoteEnvioFornecedorService.ordem_elegivel_para_retorno(o)
    ]

    lote_preselect_id = (request.GET.get("lote") or "").strip()
    lote_preselect = None
    if lote_preselect_id.isdigit():
        lote_preselect = LoteEnvioFornecedor.objects.filter(
            pk=lote_preselect_id, fornecedor=request.fornecedor,
            status=StatusLoteEnvioFornecedorChoices.ABERTO,
        ).first()
    context = {
        "fornecedor": request.fornecedor,
        "ordens": ordens,
        "localidades": Localidade.objects.order_by("local"),
        "lotes_abertos": list(LoteEnvioFornecedorService.lotes_abertos(request.fornecedor)),
        "lote_preselect": lote_preselect,
        "active_nav": "lotes_envio",
    }
    return render(request, "front/portal/portal_lote_envio_item_reparo_concluido.html", context)
