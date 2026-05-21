from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from ..models import Localidade, LocalidadeChoices, Item, Usuario
from ..forms import LocalidadeForm


@login_required
def localidade_list(request):
    qs = Localidade.objects.all().order_by("local")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(local__icontains=q) | Q(codigo__icontains=q))
    context = {
        "localidades": qs,
        "LocalidadeChoices": LocalidadeChoices,
        "q": q,
    }
    return render(request, "front/localidade/localidade_list.html", context)


@login_required
def localidade_create(request):
    if request.method == "POST":
        form = LocalidadeForm(request.POST)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.criado_por = request.user
            obj.atualizado_por = request.user
            obj.save()
            messages.success(request, "Localidade criada com sucesso.")
            return redirect("localidade_list")
        messages.error(request, "Corrija os erros do formulário.")
    else:
        form = LocalidadeForm()
    return render(request, "front/localidade/localidade_form.html", {"form": form, "editar": False})


@login_required
def localidade_update(request, pk):
    obj = get_object_or_404(Localidade, pk=pk)
    if request.method == "POST":
        form = LocalidadeForm(request.POST, instance=obj)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.atualizado_por = request.user
            obj.save()
            messages.success(request, "Localidade atualizada com sucesso.")
            return redirect("localidade_list")
        messages.error(request, "Corrija os erros do formulário.")
    else:
        form = LocalidadeForm(instance=obj)
    return render(request, "front/localidade/localidade_form.html", {"form": form, "editar": True, "obj": obj})


@login_required
def localidade_delete(request, pk):
    obj = get_object_or_404(Localidade, pk=pk)
    if request.method == "POST":
        nome = obj.local
        obj.delete()
        messages.success(request, f"Localidade '{nome}' excluída.")
        return redirect("localidade_list")
    return redirect("localidade_list")


@login_required
def localidade_detail(request, pk):
    obj = get_object_or_404(Localidade, pk=pk)
    itens = (
        Item.objects
        .filter(localidade=obj)
        .select_related('subtipo')
        .order_by("nome")[:20]
    )
    itens_count = Item.objects.filter(localidade=obj).count()
    usuarios = (
        Usuario.objects
        .filter(localidade=obj)
        .select_related('funcao')
        .order_by("nome")[:20]
    )
    usuarios_count = Usuario.objects.filter(localidade=obj).count()
    context = {
        "obj": obj,
        "itens": itens,
        "itens_count": itens_count,
        "usuarios": usuarios,
        "usuarios_count": usuarios_count,
    }
    return render(request, "front/localidade/localidade_detail.html", context)
