from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.paginator import Paginator
from django.db.models import Q
from ..models import Subtipo, Categoria, Item, CheckListModelo
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
    itens_count = Item.objects.filter(subtipo=obj).count()
    checklists_count = CheckListModelo.objects.filter(subtipo=obj).count()
    bloqueado = bool(itens_count or checklists_count)

    if request.method == "POST":
        if bloqueado:
            partes = []
            if itens_count:
                partes.append(f"{itens_count} equipamento(s)")
            if checklists_count:
                partes.append(f"{checklists_count} modelo(s) de checklist")
            messages.error(
                request,
                f"Não é possível excluir o subtipo '{obj.nome}': há "
                f"{', '.join(partes)} vinculados a ele. Reclassifique-os antes de excluir."
            )
            return redirect("subtipo_list")
        obj.delete()
        messages.success(request, "Subtipo excluído com sucesso!")
        return redirect("subtipo_list")

    return render(request, "front/subtipo/subtipo_confirm_delete.html", {
        "obj": obj,
        "itens_count": itens_count,
        "checklists_count": checklists_count,
        "bloqueado": bloqueado,
    })


@login_required
def subtipo_detail(request, pk):
    obj = get_object_or_404(Subtipo.objects.select_related("categoria"), pk=pk)
    itens_qs = (
        obj.item_set.select_related("localidade")
        .order_by("-created_at")
        if hasattr(obj, "item_set") else None
    )
    itens = list(itens_qs[:30]) if itens_qs is not None else []
    itens_count = itens_qs.count() if itens_qs is not None else 0
    return render(request, "front/subtipo/subtipo_detail.html", {
        "obj": obj,
        "itens": itens,
        "itens_count": itens_count,
    })
