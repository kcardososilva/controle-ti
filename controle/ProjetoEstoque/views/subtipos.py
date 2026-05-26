from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.paginator import Paginator
from django.db.models import Q
from ..models import Subtipo, Categoria
from ..forms import SubtipoForm


@login_required
def subtipo_list(request):
    q = request.GET.get("q", "").strip()
    cat = request.GET.get("categoria", "").strip()
    alocado = request.GET.get("alocado", "").strip()

    qs = Subtipo.objects.select_related("categoria").order_by("categoria__nome", "nome")

    if q:
        qs = qs.filter(Q(nome__icontains=q) | Q(categoria__nome__icontains=q))
    if cat:
        qs = qs.filter(categoria__id=cat)
    if alocado:
        qs = qs.filter(alocado=alocado)

    total = qs.count()
    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page", 1))
    get_copy = request.GET.copy()
    if "page" in get_copy:
        del get_copy["page"]

    context = {
        "subtipos": page_obj.object_list,
        "page_obj": page_obj,
        "total": total,
        "qs_keep": get_copy.urlencode(),
        "categorias": Categoria.objects.order_by("nome"),
        "alocado_choices": (("sim", "Sim"), ("nao", "Não")),
        "q": q,
        "f_cat": cat,
        "f_alocado": alocado,
        "request": request,
    }
    return render(request, "front/subtipo/subtipo_list.html", context)


@login_required
def subtipo_create(request):
    if request.method == "POST":
        form = SubtipoForm(request.POST)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.criado_por = request.user
            obj.atualizado_por = request.user
            obj.save()
            messages.success(request, "Subtipo cadastrado com sucesso!")
            return redirect("subtipo_list")
        messages.error(request, "Verifique os campos destacados.")
    else:
        form = SubtipoForm()
    return render(request, "front/subtipo/subtipo_form.html", {"form": form, "editar": False})


@login_required
def subtipo_update(request, pk):
    obj = get_object_or_404(Subtipo, pk=pk)
    if request.method == "POST":
        form = SubtipoForm(request.POST, instance=obj)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.atualizado_por = request.user
            obj.save()
            messages.success(request, "Subtipo atualizado com sucesso!")
            return redirect("subtipo_list")
        messages.error(request, "Verifique os campos destacados.")
    else:
        form = SubtipoForm(instance=obj)
    return render(request, "front/subtipo/subtipo_form.html", {"form": form, "editar": True, "obj": obj})


@login_required
@permission_required("ProjetoEstoque.delete_subtipo", raise_exception=True)
def subtipo_delete(request, pk):
    obj = get_object_or_404(Subtipo, pk=pk)
    if request.method == "POST":
        obj.delete()
        messages.success(request, "Subtipo excluído com sucesso!")
        return redirect("subtipo_list")
    return render(request, "front/subtipo/subtipo_confirm_delete.html", {"obj": obj})


@login_required
def subtipo_detail(request, pk):
    obj = get_object_or_404(Subtipo.objects.select_related("categoria"), pk=pk)
    return render(request, "front/subtipo/subtipo_detail.html", {"obj": obj})
