from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.views.decorators.http import require_POST
from ..models import Funcao, Usuario
from ..forms import FuncaoForm


@login_required
def funcao_list(request):
    q = (request.GET.get("q") or "").strip()
    ordenar = request.GET.get("ord") or "nome"

    base = Funcao.objects.annotate(
        num_ativos=Count("usuario", filter=~Q(usuario__status="desligado")),
        num_desligados=Count("usuario", filter=Q(usuario__status="desligado")),
        num_total=Count("usuario"),
    )

    kpi_total = base.count()
    kpi_em_uso = base.filter(num_total__gt=0).count()
    kpi = {
        "total": kpi_total,
        "em_uso": kpi_em_uso,
        "sem_uso": kpi_total - kpi_em_uso,
        "colaboradores": Usuario.objects.exclude(status="desligado")
                                        .filter(funcao__isnull=False).count(),
    }

    qs = base
    if q:
        qs = qs.filter(nome__icontains=q)
    if ordenar == "uso":
        qs = qs.order_by("-num_ativos", "nome")
    else:
        ordenar = "nome"
        qs = qs.order_by("nome")
    total = qs.count()

    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page", 1))
    get_copy = request.GET.copy()
    if "page" in get_copy:
        del get_copy["page"]

    return render(request, "front/funcoes/funcao_list.html", {
        "funcoes": page_obj.object_list,
        "page_obj": page_obj,
        "total": total,
        "kpi": kpi,
        "ordenar": ordenar,
        "qs_keep": get_copy.urlencode(),
        "q": q,
    })


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
@permission_required("ProjetoEstoque.delete_funcao", raise_exception=True)
@require_POST
def funcao_delete(request, pk):
    obj = get_object_or_404(Funcao, pk=pk)
    usuarios_count = Usuario.objects.filter(funcao=obj).count()
    if usuarios_count:
        messages.error(
            request,
            f"Não é possível excluir a função '{obj.nome}': há {usuarios_count} "
            "colaborador(es) vinculado(s) a ela. Altere a função deles antes de excluir."
        )
        return redirect("funcoes_list")
    obj.delete()
    messages.success(request, "Função removida.")
    return redirect("funcoes_list")
