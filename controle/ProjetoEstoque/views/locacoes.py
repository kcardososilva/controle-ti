from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.paginator import Paginator
from ..models import Locacao
from ..forms import LocacaoForm


@login_required
def locacoes_list(request):
    q = (request.GET.get("q") or "").strip()
    qs = (
        Locacao.objects
        .select_related("equipamento", "fornecedor")
        .order_by("-created_at")
    )
    if q:
        qs = qs.filter(equipamento__nome__icontains=q)
    total = qs.count()

    paginator = Paginator(qs, 20)
    page_obj = paginator.get_page(request.GET.get("page", 1))
    get_copy = request.GET.copy()
    if "page" in get_copy:
        del get_copy["page"]

    return render(request, 'front/locacoes/locacao_list.html', {
        'locacoes': page_obj.object_list,
        'page_obj': page_obj,
        'total': total,
        'qs_keep': get_copy.urlencode(),
        'q': q,
    })


@login_required
def locacao_create(request):
    form = LocacaoForm(request.POST or None)
    if form.is_valid():
        locacao = form.save(commit=False)
        locacao.criado_por = request.user
        locacao.atualizado_por = request.user
        locacao.save()
        messages.success(request, "Locação criada com sucesso!")
        return redirect('locacoes_list')
    return render(request, 'front/locacoes/locacao_form.html', {'form': form})


@login_required
def locacao_update(request, pk):
    locacao = get_object_or_404(Locacao, pk=pk)
    form = LocacaoForm(request.POST or None, instance=locacao)
    if form.is_valid():
        obj = form.save(commit=False)
        obj.atualizado_por = request.user
        obj.save()
        messages.success(request, "Locação atualizada com sucesso!")
        return redirect('locacoes_list')
    return render(request, 'front/locacoes/locacao_form.html', {'form': form})


@login_required
@permission_required("ProjetoEstoque.delete_locacao", raise_exception=True)
def locacao_delete(request, pk):
    locacao = get_object_or_404(Locacao, pk=pk)
    if request.method == 'POST':
        locacao.delete()
        messages.success(request, "Locação excluída com sucesso!")
        return redirect('locacoes_list')
    return render(request, 'front/locacoes/locacao_confirm_delete.html', {'obj': locacao})
