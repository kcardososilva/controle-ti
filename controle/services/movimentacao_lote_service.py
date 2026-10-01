from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.utils.datastructures import MultiValueDict

from ProjetoEstoque.forms import MovimentacaoItemForm
from ProjetoEstoque.models import Item, Usuario
from services.movimentacao_service import MovimentacaoEstoqueService


class MovimentacaoLoteService:
    """
    Registra a MESMA ação (entrega ou devolução) para VÁRIOS equipamentos de
    uma vez, para um único colaborador — "Movimentação em Lote".

    Reaproveita o mesmo `MovimentacaoItemForm` + `MovimentacaoEstoqueService`
    já usados na tela de Movimentação individual, um item por vez dentro de
    uma ÚNICA transação (tudo ou nada: se um equipamento falhar a validação,
    nenhuma movimentação do lote é gravada — evita ficar com o lote "meio
    processado"). Isso garante que o lote segue exatamente as mesmas regras
    de negócio de uma movimentação manual (item em manutenção, equipamento
    sem centro de custo, etc.), sem duplicar essa lógica aqui.

    O termo de responsabilidade pode ser UM único arquivo (reaproveitado em
    todos os itens do lote, mesmo mecanismo da cascata de desligamento — ver
    `DesligamentoService`) ou UM arquivo por equipamento.
    """

    @classmethod
    @transaction.atomic
    def processar(cls, request):
        acao = request.POST.get("tipo_transferencia")
        usuario_id = request.POST.get("usuario")
        item_ids = [i for i in request.POST.getlist("itens") if i]
        modo_termo = request.POST.get("modo_termo", "unico")
        localidade_destino = request.POST.get("localidade_destino") or ""
        centro_custo_destino = request.POST.get("centro_custo_destino") or ""
        observacao = request.POST.get("observacao", "")

        if acao not in ("entrega", "devolucao"):
            raise ValidationError("Selecione a ação (entrega ou devolução).")

        if not usuario_id:
            raise ValidationError("Selecione o colaborador.")

        usuario = Usuario.objects.filter(pk=usuario_id).first()
        if not usuario:
            raise ValidationError("Colaborador inválido.")

        if not item_ids:
            raise ValidationError("Selecione ao menos um equipamento.")

        if acao == "entrega" and not localidade_destino:
            raise ValidationError("Informe a localidade de destino.")

        termo_unico_bytes = None
        termo_unico_nome = None
        termo_unico_content_type = "application/pdf"

        if modo_termo == "unico":
            arquivo = request.FILES.get("termo_pdf_unico")
            if not arquivo:
                raise ValidationError("Envie o termo de responsabilidade (PDF) — obrigatório.")
            termo_unico_bytes = arquivo.read()
            termo_unico_nome = arquivo.name
            termo_unico_content_type = arquivo.content_type or termo_unico_content_type
        elif modo_termo != "por_item":
            raise ValidationError("Modo de termo inválido.")

        movimentos = []

        for item_id in item_ids:
            item = Item.objects.filter(pk=item_id).first()
            nome_item = item.nome if item else f"#{item_id}"

            if modo_termo == "por_item":
                termo_file = request.FILES.get(f"termo_item_{item_id}")
                if not termo_file:
                    raise ValidationError(
                        f'Falta o termo de responsabilidade do equipamento "{nome_item}".'
                    )
            else:
                termo_file = SimpleUploadedFile(
                    termo_unico_nome, termo_unico_bytes, content_type=termo_unico_content_type,
                )

            data = {
                "tipo_movimentacao": "transferencia",
                "tipo_transferencia": acao,
                "item": item_id,
                "usuario": usuario_id,
                "quantidade": 1,
                "observacao": observacao,
            }

            if acao == "entrega":
                data["localidade_destino"] = localidade_destino
                if centro_custo_destino:
                    data["centro_custo_destino"] = centro_custo_destino
            elif localidade_destino:
                data["localidade_destino"] = localidade_destino

            files = MultiValueDict({"termo_pdf": [termo_file]})
            form = MovimentacaoItemForm(data=data, files=files)

            if not form.is_valid():
                primeiro_erro = next(iter(form.errors.values()))[0]
                raise ValidationError(f'Equipamento "{nome_item}": {primeiro_erro}')

            mov = MovimentacaoEstoqueService.registrar(form=form, user=request.user)
            movimentos.append(mov)

        return {
            "sucesso": len(movimentos),
            "tipo_label": "entrega(s)" if acao == "entrega" else "devolução(ões)",
            "movimentos": movimentos,
        }
