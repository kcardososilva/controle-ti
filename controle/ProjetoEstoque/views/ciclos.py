from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.paginator import Paginator
from ..models import CicloManutencao, Item
from ..forms import CicloManutencaoForm


@login_required
def ciclos_list(request):
    qs = CicloManutencao.objects.select_related("item").order_by("-created_at")
    total = qs.count()

    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page", 1))

    return render(request, "front/ciclos/ciclo_list.html", {
        "ciclos": page_obj.object_list,
        "page_obj": page_obj,
        "total": total,
        "qs_keep": "",
    })


@login_required
def ciclo_create(request, item_pk: int):
    item = get_object_or_404(Item, pk=item_pk)

    form = CicloManutencaoForm(request.POST or None)
    if form.is_valid():
        ciclo_aberto = CicloManutencao.objects.filter(item=item, data_fim__isnull=True).exists()
        if ciclo_aberto:
            messages.error(request, f"O item '{item.nome}' já possui um ciclo de manutenção em andamento.")
        else:
            ciclo = form.save(commit=False)
            ciclo.item = item
            ciclo.criado_por = request.user
            ciclo.atualizado_por = request.user
            item.status = "manutencao"
            item.atualizado_por = request.user
            item.save()
            ciclo.save()
            messages.success(request, f"Ciclo de manutenção iniciado para '{item.nome}'.")
            return redirect("ciclos_list")

    return render(request, "front/ciclos/ciclo_form.html", {"form": form, "item": item})


@login_required
def ciclo_update(request, pk):
    ciclo = get_object_or_404(CicloManutencao.objects.select_related("item"), pk=pk)
    form = CicloManutencaoForm(request.POST or None, instance=ciclo)
    if form.is_valid():
        ciclo = form.save(commit=False)
        ciclo.atualizado_por = request.user
        item = ciclo.item
        if ciclo.data_fim:
            item.status = "ativo"
            item.atualizado_por = request.user
            item.save()
            messages.success(request, f"Ciclo encerrado. '{item.nome}' voltou para operação.")
        ciclo.save()
        return redirect("ciclos_list")
    return render(request, "front/ciclos/ciclo_form.html", {"form": form, "item": ciclo.item})


@login_required
@permission_required("ProjetoEstoque.delete_ciclomanutencao", raise_exception=True)
def ciclo_delete(request, pk):
    ciclo = get_object_or_404(CicloManutencao, pk=pk)
    if request.method == "POST":
        ciclo.delete()
        return redirect("ciclos_list")
    return render(request, "front/ciclos/ciclo_confirm_delete.html", {"obj": ciclo})
