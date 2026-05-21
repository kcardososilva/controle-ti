from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.utils import timezone
from ..models import Item
from ProjetoEstoque.forms import TermoGeracaoForm
from services.termos import gerar_termo_docx, get_usuario_atual_item


@login_required
def termo_entrega_form(request, pk):
    item = get_object_or_404(
        Item.objects.select_related("subtipo", "localidade", "centro_custo", "fornecedor"),
        pk=pk
    )

    initial = {
        "numero_termo": f"ENT-{timezone.localdate().strftime('%Y%m%d')}-{item.pk}",
        "acessorios": "",
        "observacoes": "",
        "responsavel_ti_nome": request.user.get_full_name() or request.user.username,
    }

    if request.method == "POST":
        form = TermoGeracaoForm(request.POST)
        if form.is_valid():
            if not form.cleaned_data.get("colaborador"):
                form.add_error("colaborador", "Selecione o colaborador que irá receber o equipamento.")
            else:
                arquivo, nome_arquivo = gerar_termo_docx(
                    item=item,
                    tipo="entrega",
                    form_data=form.cleaned_data
                )
                response = HttpResponse(
                    arquivo.getvalue(),
                    content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                )
                response["Content-Disposition"] = f'attachment; filename="{nome_arquivo}"'
                return response
    else:
        form = TermoGeracaoForm(initial=initial)

    return render(
        request,
        "front/equipamentos/termo_form.html",
        {
            "item": item,
            "form": form,
            "tipo_termo": "entrega",
            "titulo": "Gerar Termo de Entrega",
            "subtitulo": "Selecione o colaborador que irá receber o equipamento e preencha os dados complementares.",
        }
    )


@login_required
def termo_devolucao_form(request, pk):
    item = get_object_or_404(
        Item.objects.select_related("subtipo", "localidade", "centro_custo", "fornecedor"),
        pk=pk
    )

    usuario_atual = get_usuario_atual_item(item)

    initial = {
        "colaborador": usuario_atual.pk if usuario_atual else None,
        "numero_termo": f"DEV-{timezone.localdate().strftime('%Y%m%d')}-{item.pk}",
        "acessorios": "",
        "observacoes": "",
        "responsavel_ti_nome": request.user.get_full_name() or request.user.username,
    }

    if request.method == "POST":
        form = TermoGeracaoForm(request.POST)
        if form.is_valid():
            arquivo, nome_arquivo = gerar_termo_docx(
                item=item,
                tipo="devolucao",
                form_data=form.cleaned_data
            )
            response = HttpResponse(
                arquivo.getvalue(),
                content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            )
            response["Content-Disposition"] = f'attachment; filename="{nome_arquivo}"'
            return response
    else:
        form = TermoGeracaoForm(initial=initial)

    return render(
        request,
        "front/equipamentos/termo_form.html",
        {
            "item": item,
            "form": form,
            "tipo_termo": "devolucao",
            "titulo": "Gerar Termo de Devolução",
            "subtitulo": "Confira o colaborador vinculado e preencha os dados complementares antes de gerar o termo.",
        }
    )
