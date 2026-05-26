from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.paginator import Paginator
from ..models import Comentario
from ..forms import ComentarioForm


@login_required
def comentarios_list(request):
    q = (request.GET.get("q") or "").strip()
    qs = (
        Comentario.objects
        .select_related("item")
        .order_by("-created_at")
    )
    if q:
        qs = qs.filter(item__nome__icontains=q)
    total = qs.count()

    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page", 1))
    get_copy = request.GET.copy()
    if "page" in get_copy:
        del get_copy["page"]

    return render(request, 'front/comentarios/comentario_list.html', {
        'comentarios': page_obj.object_list,
        'page_obj': page_obj,
        'total': total,
        'qs_keep': get_copy.urlencode(),
        'q': q,
    })


@login_required
def comentario_create(request):
    form = ComentarioForm(request.POST or None)
    if form.is_valid():
        comentario = form.save(commit=False)
        comentario.criado_por = request.user
        comentario.atualizado_por = request.user
        comentario.save()
        messages.success(request, "Comentário criado com sucesso!")
        return redirect('comentarios_list')
    return render(request, 'front/comentarios/comentario_form.html', {'form': form})


@login_required
def comentario_update(request, pk):
    comentario = get_object_or_404(Comentario, pk=pk)
    form = ComentarioForm(request.POST or None, instance=comentario)
    if form.is_valid():
        obj = form.save(commit=False)
        obj.atualizado_por = request.user
        obj.save()
        messages.success(request, "Comentário atualizado com sucesso!")
        return redirect('comentarios_list')
    return render(request, 'front/comentarios/comentario_form.html', {'form': form})


@login_required
@permission_required("ProjetoEstoque.delete_comentario", raise_exception=True)
def comentario_delete(request, pk):
    comentario = get_object_or_404(Comentario, pk=pk)
    if request.method == 'POST':
        comentario.delete()
        messages.success(request, "Comentário excluído com sucesso!")
        return redirect('comentarios_list')
    return render(request, 'front/comentarios/comentario_confirm_delete.html', {'obj': comentario})
