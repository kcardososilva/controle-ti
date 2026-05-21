from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from ..models import Funcao
from ..forms import FuncaoForm


@login_required
def funcao_list(request):
    q = (request.GET.get("q") or "").strip()
    funcoes = Funcao.objects.all().order_by("nome")
    if q:
        funcoes = funcoes.filter(nome__icontains=q)
    ctx = {
        "funcoes": funcoes,
        "q": q,
        "total": funcoes.count(),
    }
    return render(request, "front/funcoes/funcao_list.html", ctx)


@login_required
def funcao_form(request, pk=None):
    instance = get_object_or_404(Funcao, pk=pk) if pk else None
    if request.method == "POST":
        form = FuncaoForm(request.POST, instance=instance)
        if form.is_valid():
            obj = form.save(commit=False)
            if instance is None:
                obj.criado_por = request.user
            obj.atualizado_por = request.user
            obj.save()
            messages.success(request, "Função salva com sucesso.")
            return redirect("funcoes_list")
    else:
        form = FuncaoForm(instance=instance)
    return render(
        request,
        "front/funcoes/funcao_form.html",
        {"form": form, "instance": instance},
    )


@login_required
@require_POST
def funcao_delete(request, pk):
    obj = get_object_or_404(Funcao, pk=pk)
    obj.delete()
    messages.success(request, "Função removida.")
    return redirect("funcoes_list")
