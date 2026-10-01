from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.utils import timezone
from ..models import Item, Usuario
from ProjetoEstoque.forms import TermoGeracaoForm
from services.termos import gerar_termo_docx, get_usuario_atual_item, gerar_termo_desligamento_docx


@login_required
def termo_entrega_form(request, pk):
    item = get_object_or_404(
        Item.objects.select_related("subtipo", "localidade", "centro_custo", "fornecedor"),
        pk=pk
    )

    initial = {
        # Numeração automática: {nº de série} - {solicitante} - {centro de custo}.
        # Campo opcional: se preenchido manualmente, sobrescreve a numeração.
        "numero_termo": "",
        "acessorios": "",
        "observacoes": "",
        "estabelecimento": "karitel",
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
        # Numeração automática: {nº de série} - {solicitante} - {centro de custo}.
        # Opcional: se preenchido, sobrescreve a numeração automática.
        "numero_termo": "",
        "acessorios": "",
        "observacoes": "",
        "estabelecimento": "karitel",
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


@login_required
def termo_desligamento_form(request, usuario_id):
    """Termo de devolução CONSOLIDADO — lista, num único documento, todos os
    equipamentos ativos do colaborador (não só um), para uma única assinatura
    cobrir toda a devolução do desligamento. Usado junto com o checkbox "O
    colaborador está sendo desligado?" na tela de Movimentações."""
    from services.desligamento_service import DesligamentoService

    usuario = get_object_or_404(Usuario, pk=usuario_id)
    itens = DesligamentoService.ativos_do_usuario(usuario)["itens"]

    initial = {
        "numero_termo": "",
        "acessorios": "",
        "observacoes": "Devolução consolidada de equipamentos por desligamento do colaborador.",
        "estabelecimento": "karitel",
        "responsavel_ti_nome": request.user.get_full_name() or request.user.username,
    }

    if request.method == "POST":
        form = TermoGeracaoForm(request.POST)
        if form.is_valid():
            if not itens:
                form.add_error(None, "Este colaborador não possui equipamentos ativos para devolução.")
            else:
                arquivo, nome_arquivo = gerar_termo_desligamento_docx(
                    usuario=usuario,
                    itens=itens,
                    form_data=form.cleaned_data,
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
        "front/usuarios/termo_desligamento_form.html",
        {
            "usuario": usuario,
            "itens": itens,
            "form": form,
            "titulo": "Gerar Termo de Devolução (Desligamento)",
            "subtitulo": f"Lista todos os equipamentos ativos de {usuario.nome} num único termo, para uma assinatura só.",
        }
    )
