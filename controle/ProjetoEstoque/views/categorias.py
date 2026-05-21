from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from ..models import Categoria
from ..forms import CategoriaForm


def categorias_list(request):
    categorias = Categoria.objects.all()
    return render(request, 'front/categorias/categoria_list.html', {'categorias': categorias})


def categoria_create(request):
    form = CategoriaForm(request.POST or None)
    if form.is_valid():
        categoria = form.save(commit=False)
        categoria.criado_por = request.user
        categoria.atualizado_por = request.user
        categoria.save()
        return redirect('categorias_list')
    return render(request, 'front/categorias/categoria_form.html', {'form': form})


def categoria_update(request, pk):
    categoria = get_object_or_404(Categoria, pk=pk)
    form = CategoriaForm(request.POST or None, instance=categoria)
    if form.is_valid():
        categoria = form.save(commit=False)
        categoria.atualizado_por = request.user
        categoria.save()
        return redirect('categorias_list')
    return render(request, 'front/categorias/categoria_form.html', {'form': form})


def categoria_delete(request, pk):
    categoria = get_object_or_404(Categoria, pk=pk)
    if request.method == 'POST':
        categoria.delete()
        return redirect('categorias_list')
    return render(request, 'front/categorias/categoria_confirm_delete.html', {'obj': categoria})
