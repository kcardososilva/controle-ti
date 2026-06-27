from decimal import Decimal

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.http import HttpResponse, JsonResponse
from django.db.models import Q, Count
from django.core.paginator import Paginator
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.template.loader import get_template, render_to_string
from django.utils import timezone
from xhtml2pdf import pisa

from ..models import (
    MovimentacaoItem, TipoMovimentacaoChoices, StatusItemChoices,
    ItemLote, Item, ItemColaborador,
)
from ..forms import MovimentacaoItemForm
from services.movimentacao_service import MovimentacaoEstoqueService


def _get_movimentacao_qs(request):
    """
    Helper: Aplica os filtros da requisição e retorna o QuerySet.
    Usado tanto na view de lista quanto na de exportação PDF.
    """
    q = (request.GET.get("q") or "").strip()
    tipo = (request.GET.get("tipo") or "").strip()
    usuario_q = (request.GET.get("usuario") or "").strip()
    numero_serie = (request.GET.get("numero_serie") or "").strip()
    centro_custo = (request.GET.get("centro_custo") or "").strip()
    data_inicio = request.GET.get("data_inicio")
    data_fim = request.GET.get("data_fim")

    qs = (
        MovimentacaoItem.objects
        .select_related(
            "item", "usuario",
            "localidade_origem", "localidade_destino",
            "centro_custo_origem", "centro_custo_destino",
            "fornecedor_manutencao",
        )
        .order_by("-created_at")
    )

    if q:
        qs = qs.filter(item__nome__icontains=q)
    if tipo:
        qs = qs.filter(tipo_movimentacao=tipo)
    if usuario_q:
        qs = qs.filter(usuario__nome__icontains=usuario_q)
    if numero_serie:
        qs = qs.filter(item__numero_serie__icontains=numero_serie)
    if centro_custo:
        qs = qs.filter(
            Q(centro_custo_origem__numero__icontains=centro_custo) |
            Q(centro_custo_origem__departamento__icontains=centro_custo) |
            Q(centro_custo_destino__numero__icontains=centro_custo) |
            Q(centro_custo_destino__departamento__icontains=centro_custo)
        )
    if data_inicio:
        qs = qs.filter(created_at__date__gte=data_inicio)
    if data_fim:
        qs = qs.filter(created_at__date__lte=data_fim)
        
    return qs

@login_required
def movimentacao_list(request):
    qs = (
        _get_movimentacao_qs(request)
        .select_related(
            "item",
            "usuario",
            "criado_por",
            "centro_custo_origem",
            "centro_custo_destino",
            "localidade_origem",
            "localidade_destino",
            "fornecedor_manutencao",
            "lote",
        )
        .order_by("-created_at")
    )

    total_filtrado = qs.count()

    stats = dict(qs.values_list("tipo_movimentacao").annotate(c=Count("id")).order_by())

    def get_count(key):
        return stats.get(key, 0)

    kpi_entrada = get_count("entrada")
    kpi_saida = get_count("baixa")
    kpi_transf = get_count("transferencia") + get_count("transferencia_equipamento")
    kpi_manut = get_count("envio_manutencao") + get_count("retorno_manutencao")

    hoje = timezone.now().date()
    kpi_hoje = qs.filter(created_at__date=hoje).count()

    top_mover_data = (
        qs.values("item__nome")
        .annotate(total=Count("id"))
        .order_by("-total")
        .first()
    )
    kpi_top_item_nome = top_mover_data["item__nome"] if top_mover_data else "-"
    kpi_top_item_qtd = top_mover_data["total"] if top_mover_data else 0

    # ranking dos usuários que mais realizam movimentações
    ranking_usuarios = (
        qs.filter(criado_por__isnull=False)
        .values(
            "criado_por__id",
            "criado_por__username",
            "criado_por__first_name",
            "criado_por__last_name",
        )
        .annotate(total=Count("id"))
        .order_by("-total", "criado_por__username")[:8]
    )

    try:
        per_page = int(request.GET.get("pp", 20))
    except ValueError:
        per_page = 20

    paginator = Paginator(qs, per_page)
    page_num = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_num)

    get_copy = request.GET.copy()
    if "page" in get_copy:
        del get_copy["page"]
    qs_keep = get_copy.urlencode()

    context = {
        "movimentacoes": page_obj.object_list,
        "page_obj": page_obj,
        "total": total_filtrado,
        "qs_keep": qs_keep,

        "f_q": request.GET.get("q", ""),
        "f_tipo": request.GET.get("tipo", ""),
        "f_user": request.GET.get("usuario", ""),
        "f_serie": request.GET.get("numero_serie", ""),
        "f_cc": request.GET.get("centro_custo", ""),
        "f_ini": request.GET.get("data_inicio", ""),
        "f_fim": request.GET.get("data_fim", ""),
        "tipos_choices": TipoMovimentacaoChoices.choices,

        "ranking_usuarios": ranking_usuarios,

        "kpi": {
            "hoje": kpi_hoje,
            "top_item": kpi_top_item_nome,
            "top_item_qtd": kpi_top_item_qtd,
            "entrada": kpi_entrada,
            "saida": kpi_saida,
            "transferencias": kpi_transf,
            "manutencao": kpi_manut,
        }
    }

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        view_mode = request.GET.get('view', 'list')
        data = {
            'count': total_filtrado,
            'pagination': render_to_string('front/movimentacao/_mov_pagination.html', context, request=request),
        }
        if view_mode == 'gallery':
            data['gallery'] = render_to_string('front/movimentacao/_mov_gallery.html', context, request=request)
        else:
            data['tbody'] = render_to_string('front/movimentacao/_mov_rows.html', context, request=request)
        return JsonResponse(data)

    return render(request, "front/movimentacao/movimentacao_list.html", context)

@login_required
def movimentacao_export_pdf(request):
    """
    Gera PDF usando xhtml2pdf (Pisa).
    Mostra o usuário que realmente realizou a movimentação
    com base no campo criado_por.
    """
    qs = (
        _get_movimentacao_qs(request)
        .select_related(
            "item",
            "criado_por",
            "centro_custo_origem",
            "centro_custo_destino",
            "localidade_origem",
            "localidade_destino",
            "fornecedor_manutencao",
        )
        .order_by("-created_at")
    )

    context = {
        "movimentacoes": qs,
        "usuario": request.user,
        "data_geracao": timezone.now(),
        "total": qs.count(),
        "filtros": {
            "inicio": request.GET.get("data_inicio"),
            "fim": request.GET.get("data_fim"),
            "tipo": request.GET.get("tipo"),
        }
    }

    template_path = "front/movimentacao_pdf.html"
    template = get_template(template_path)
    html = template.render(context)

    response = HttpResponse(content_type="application/pdf")
    filename = f'relatorio_movimentacoes_{timezone.now().strftime("%Y%m%d_%H%M")}.pdf'
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    pisa_status = pisa.CreatePDF(html, dest=response)

    if pisa_status.err:
        return HttpResponse("Erro ao gerar PDF <pre>" + html + "</pre>")

    return response




# views.py — movimentacao_create

@login_required
def movimentacao_create(request):
    if request.method == "POST":
        form = MovimentacaoItemForm(request.POST, request.FILES)

        if form.is_valid():
            try:
                MovimentacaoEstoqueService.registrar(
                    form=form,
                    user=request.user,
                )

                messages.success(request, "Movimentação realizada com sucesso.")
                return redirect("movimentacao_list")

            except ValidationError as e:
                form.add_error(None, e)
                messages.error(request, "Erro de validação na movimentação.")

            except IntegrityError:
                messages.error(
                    request,
                    "Erro de integridade na movimentação. Nenhuma alteração foi gravada."
                )

            except Exception as e:
                messages.error(
                    request,
                    f"Erro inesperado ao realizar movimentação: {str(e)}"
                )

        else:
            messages.error(request, "Verifique os erros no formulário.")

    else:
        form = MovimentacaoItemForm()

    return render(request, "front/movimentacao/movimentacao_form.html", {"form": form})


@login_required
def api_lotes_por_item(request):
    item_id = request.GET.get("item_id")

    if not item_id:
        return JsonResponse({"results": []})

    vinculos = (
        ItemLote.objects
        .filter(item_id=item_id, quantidade_disponivel__gt=0)
        .select_related(
            "lote",
            "item",
            "item__centro_custo",
            "item__localidade",
        )
        .order_by("-lote__data_entrada", "-created_at")
    )

    results = []

    for vinculo in vinculos:
        lote = vinculo.lote
        centro_custo = vinculo.item.centro_custo if vinculo.item and vinculo.item.centro_custo else "-"
        localidade = vinculo.item.localidade if vinculo.item and vinculo.item.localidade else "-"

        results.append({
            "id": lote.id,
            "text": (
                f"NF {lote.numero_nf} | Saldo: {vinculo.quantidade_disponivel} "
                f"| CC: {centro_custo} | Local: {localidade}"
            ),
            "saldo": vinculo.quantidade_disponivel,
            "centro_custo": str(centro_custo),
            "localidade": str(localidade),
        })

    return JsonResponse({"results": results})


@login_required
def api_item_devolucao_info(request):
    """Para a tela de devolução: informa quem está com o item (puxado da última
    entrega) e para qual centro de custo ele voltará. Usado só para exibir um aviso
    ao operador — a gravação real é feita pelo serviço de movimentação."""
    item_id = request.GET.get("item_id")
    if not item_id:
        return JsonResponse({"ok": False})

    item = Item.objects.filter(pk=item_id).only("id", "compartilhado").first()
    compartilhado = bool(item and item.compartilhado)

    # Item compartilhado: a devolução precisa que o operador escolha QUAL
    # colaborador está devolvendo. Devolve a lista de vínculos ativos.
    if compartilhado:
        vinculos = [
            {"id": v.colaborador_id, "nome": v.colaborador.nome}
            for v in (
                ItemColaborador.objects
                .filter(item_id=item_id, ativo=True)
                .select_related("colaborador")
                .order_by("colaborador__nome")
            )
        ]
        return JsonResponse({
            "ok": True,
            "compartilhado": True,
            "tem_entrega": bool(vinculos),
            "vinculos": vinculos,
        })

    ultima_entrega = (
        MovimentacaoItem.objects
        .filter(
            item_id=item_id,
            tipo_movimentacao=TipoMovimentacaoChoices.TRANSFERENCIA,
            tipo_transferencia="entrega",
        )
        .select_related("usuario", "centro_custo_origem")
        .order_by("-created_at")
        .first()
    )

    if ultima_entrega is None:
        return JsonResponse({"ok": True, "compartilhado": False, "tem_entrega": False})

    cc = ultima_entrega.centro_custo_origem
    return JsonResponse({
        "ok": True,
        "compartilhado": False,
        "tem_entrega": True,
        "usuario": str(ultima_entrega.usuario) if ultima_entrega.usuario_id else None,
        "centro_custo_origem": _centro_custo_label(cc) if cc else None,
    })




def _tem_valor(valor):
    if valor is None:
        return False

    if isinstance(valor, str) and not valor.strip():
        return False

    return True


def _safe_label(obj, *attrs):
    if not obj:
        return "—"

    for attr in attrs:
        valor = getattr(obj, attr, None)

        if _tem_valor(valor):
            return str(valor)

    return str(obj)


def _safe_decimal(valor):
    if valor is None:
        return Decimal("0.00")

    try:
        return Decimal(valor)
    except Exception:
        return Decimal("0.00")


def _centro_custo_label(cc):
    if not cc:
        return "—"

    numero = getattr(cc, "numero", None) or getattr(cc, "codigo", None)
    nome = getattr(cc, "departamento", None) or getattr(cc, "nome", None)

    if numero and nome:
        return f"{numero} - {nome}"

    if nome:
        return str(nome)

    if numero:
        return str(numero)

    return str(cc)


def _localidade_label(localidade):
    return _safe_label(localidade, "local", "nome", "descricao")


def _fornecedor_label(fornecedor):
    return _safe_label(fornecedor, "nome", "razao_social", "fantasia")


@login_required
def movimentacao_detail(request, pk):
    """
    Exibe os detalhes de uma movimentação com:
    - dados da operação
    - item vinculado
    - lote vinculado, quando existir
    - fluxo origem/destino
    - solicitante/usuário
    - contexto atual do item
    - auditoria
    """

    mov = get_object_or_404(
        MovimentacaoItem.objects.select_related(
            "item",
            "item__subtipo",
            "item__localidade",
            "item__centro_custo",
            "item__fornecedor",
            "usuario",
            "criado_por",
            "localidade_origem",
            "localidade_destino",
            "centro_custo_origem",
            "centro_custo_destino",
            "fornecedor_manutencao",
            "lote",
            "lote__fornecedor",
        ),
        pk=pk,
    )

    origem = {
        "loc": _localidade_label(mov.localidade_origem),
        "cc": _centro_custo_label(mov.centro_custo_origem),
    }

    destino = {
        "loc": _localidade_label(mov.localidade_destino),
        "cc": _centro_custo_label(mov.centro_custo_destino),
    }

    if mov.tipo_movimentacao in ("retorno", "retorno_manutencao"):
        status_final = "Backup"

    elif mov.tipo_movimentacao == "transferencia_equipamento" and mov.status_transferencia:
        status_final = dict(StatusItemChoices.choices).get(
            mov.status_transferencia,
            mov.status_transferencia,
        )

    elif mov.tipo_movimentacao == "envio_manutencao":
        status_final = "Em Manutenção"

    elif mov.tipo_movimentacao == "baixa":
        status_final = "Baixado"

    else:
        status_final = mov.item.get_status_display() if mov.item else "—"

    impacto_map = {
        "entrada": (f"+{mov.quantidade or 0} Entrada", "st-green"),
        "baixa": (f"-{mov.quantidade or 0} Baixa", "st-red"),
        "envio_manutencao": ("Saída Manutenção", "st-orange"),
        "retorno_manutencao": ("Retorno Manutenção", "st-blue"),
        "retorno": ("Retorno", "st-blue"),
        "transferencia": ("Transferência de Posse", "st-gray"),
        "transferencia_equipamento": ("Transferência de Setor", "st-gray"),
    }

    impacto_texto, impacto_class = impacto_map.get(
        mov.tipo_movimentacao,
        ("Apenas Registro", "st-gray"),
    )

    total_movs_item = (
        MovimentacaoItem.objects
        .filter(item=mov.item)
        .count()
        if mov.item_id else 0
    )

    ultima_mov = (
        MovimentacaoItem.objects
        .filter(item=mov.item)
        .exclude(pk=pk)
        .order_by("-created_at")
        .first()
        if mov.item_id else None
    )

    lote_info = None

    if mov.lote_id:
        item_lote = (
            ItemLote.objects
            .filter(item=mov.item, lote=mov.lote)
            .select_related("item", "lote", "lote__fornecedor")
            .first()
        )

        quantidade_entrada = (
            item_lote.quantidade_entrada
            if item_lote else getattr(mov.lote, "quantidade", None)
        ) or 0

        quantidade_disponivel = (
            item_lote.quantidade_disponivel
            if item_lote else 0
        ) or 0

        percentual_saldo = 0

        if quantidade_entrada > 0:
            percentual_saldo = int((quantidade_disponivel / quantidade_entrada) * 100)

        custo_unitario = (
            item_lote.custo_unitario
            if item_lote and item_lote.custo_unitario is not None
            else getattr(mov.lote, "custo_unitario", None)
        )

        lote_info = {
            "id": mov.lote.id,
            "numero_nf": getattr(mov.lote, "numero_nf", None),
            "data_entrada": getattr(mov.lote, "data_entrada", None),
            "fornecedor": _fornecedor_label(getattr(mov.lote, "fornecedor", None)),
            "quantidade": getattr(mov.lote, "quantidade", None),
            "custo_unitario": _safe_decimal(custo_unitario),
            "observacao_tecnica": getattr(mov.lote, "observacao_tecnica", None),
            "quantidade_entrada": quantidade_entrada,
            "quantidade_disponivel": quantidade_disponivel,
            "percentual_saldo": percentual_saldo,
        }

    item_info = {
        "nome": mov.item.nome if mov.item else "—",
        "numero_serie": mov.item.numero_serie if mov.item else "—",
        "status_atual": mov.item.get_status_display() if mov.item else "—",
        "quantidade_atual": mov.item.quantidade if mov.item else None,
        "localidade": _localidade_label(mov.item.localidade) if mov.item else None,
        "centro_custo": _centro_custo_label(mov.item.centro_custo) if mov.item else None,
        "fornecedor": _fornecedor_label(mov.item.fornecedor) if mov.item else None,
        "subtipo": _safe_label(mov.item.subtipo, "nome", "descricao") if mov.item else None,
    }

    # Integração com manutenção: OS aberta a partir desta movimentação (se houver)
    ordem_manutencao = (
        mov.ordens_manutencao
        .select_related("fornecedor", "item_substituto")
        .order_by("-created_at")
        .first()
    )

    context = {
        "mov": mov,
        "ordem_manutencao": ordem_manutencao,
        "origem": origem,
        "destino": destino,
        "status_final": status_final,
        "impacto": {
            "texto": impacto_texto,
            "class": impacto_class,
        },
        "stats": {
            "total_movs": total_movs_item,
            "ultima_data": ultima_mov.created_at if ultima_mov else None,
            "ultima_tipo": ultima_mov.get_tipo_movimentacao_display() if ultima_mov else None,
        },
        "lote_info": lote_info,
        "item_info": item_info,
    }

    return render(request, "front/movimentacao/movimentacao_detail.html", context)


def movimentacao_update(request, pk):
    mov = get_object_or_404(MovimentacaoItem, pk=pk)
    form = MovimentacaoItemForm(request.POST or None, request.FILES or None, instance=mov)
    if form.is_valid():
        mov = form.save(commit=False)
        mov.atualizado_por = request.user
        mov.save()
        return redirect('movimentacoes_list')
    return render(request, 'front/movimentacao/movimentacao_form.html', {'form': form})

@login_required
@permission_required("ProjetoEstoque.delete_movimentacaoitem", raise_exception=True)
def movimentacao_delete(request, pk):
    mov = get_object_or_404(MovimentacaoItem, pk=pk)
    if request.method == 'POST':
        mov.delete()
        return redirect('movimentacoes_list')
    return render(request, 'front/movimentacao/movimentacao_confirm_delete.html', {'obj': mov})
