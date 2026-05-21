from django.shortcuts import render, redirect, get_object_or_404
from ..models import Comentario
from ..forms import ComentarioForm


def comentarios_list(request):
    comentarios = Comentario.objects.all()
    return render(request, 'front/comentarios/comentario_list.html', {'comentarios': comentarios})


def comentario_create(request):
    form = ComentarioForm(request.POST or None)
    if form.is_valid():
        comentario = form.save(commit=False)
        comentario.criado_por = request.user
        comentario.save()
        return redirect('comentarios_list')
    return render(request, 'front/comentarios/comentario_form.html', {'form': form})


def comentario_update(request, pk):
    comentario = get_object_or_404(Comentario, pk=pk)
    form = ComentarioForm(request.POST or None, instance=comentario)
    if form.is_valid():
        form.save()
        return redirect('comentarios_list')
    return render(request, 'front/comentarios/comentario_form.html', {'form': form})


def comentario_delete(request, pk):
    comentario = get_object_or_404(Comentario, pk=pk)
    if request.method == 'POST':
        comentario.delete()
        return redirect('comentarios_list')
    return render(request, 'front/comentarios/comentario_confirm_delete.html', {'obj': comentario})
