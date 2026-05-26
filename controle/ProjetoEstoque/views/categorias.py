from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.paginator import Paginator
from ..models import Categoria
from ..forms import CategoriaForm


@login_required
def categorias_list(request):
    q = (request.GET.get("q") or "").strip()
    qs = Categoria.objects.all().order_by("nome")
    if q:
        qs = qs.filter(nome__icontains=q)
    total = qs.count()

    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page", 1))
    get_copy = request.GET.copy()
    if "page" in get_copy:
        del get_copy["page"]

    return render(request, 'front/categorias/categoria_list.html', {
        'categorias': page_obj.object_list,
        'page_obj': page_obj,
        'total': total,
        'qs_keep': get_copy.urlencode(),
        'q': q,
    })


@login_required
def categoria_create(request):
    form = CategoriaForm(request.POST or None)
    if form.is_valid():
        categoria = form.save(commit=False)
        categoria.criado_por = request.user
        categoria.atualizado_por = request.user
        categoria.save()
        messages.success(request, "Categoria criada com sucesso!")
        return redirect('categorias_list')
    return render(request, 'front/categorias/categoria_form.html', {'form': form})


@login_required
def categoria_update(request, pk):
    categoria = get_object_or_404(Categoria, pk=pk)
    form = CategoriaForm(request.POST or None, instance=categoria)
    if form.is_valid():
        obj = form.save(commit=False)
        obj.atualizado_por = request.user
        obj.save()
        messages.success(request, "Categoria atualizada com sucesso!")
        return redirect('categorias_list')
    return render(request, 'front/categorias/categoria_form.html', {'form': form})


@login_required
@permission_required("ProjetoEstoque.delete_categoria", raise_exception=True)
def categoria_delete(request, pk):
    categoria = get_object_or_404(Categoria, pk=pk)
    if request.method == 'POST':
        categoria.delete()
        messages.success(request, "Categoria excluída com sucesso!")
        return redirect('categorias_list')
    return render(request, 'front/categorias/categoria_confirm_delete.html', {'obj': categoria})
