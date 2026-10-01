import logging

from django.db import transaction
from django.utils import timezone

from ProjetoEstoque.models import (
    Item,
    ItemColaborador,
    MovimentacaoItem,
    MovimentacaoLicenca,
    StatusItemChoices,
    StatusUsuarioChoices,
    TipoMovLicencaChoices,
)
from services.movimentacao_service import MovimentacaoEstoqueService as MovService

logger = logging.getLogger(__name__)


class DesligamentoService:
    """
    Cascata de desligamento de colaborador, disparada pelo checkbox "O
    colaborador está sendo desligado?" na devolução de equipamento (tela de
    Movimentações): devolve automaticamente TODOS os equipamentos e licenças
    ativas do colaborador (não só o item que originou a devolução) e marca o
    cadastro como desligado.

    Reaproveita a lógica de negócio já existente para cada devolução
    individual (`MovimentacaoEstoqueService` para equipamento,
    `MovimentacaoLicencaForm` para licença) em vez de duplicá-la, para não
    divergir do comportamento de uma devolução manual normal.
    """

    @classmethod
    @transaction.atomic
    def desligar_e_liberar_ativos(cls, *, usuario, executado_por, termo_pdf_bytes=None, termo_pdf_nome=None):
        n_itens = cls._devolver_itens_ativos(
            usuario=usuario, executado_por=executado_por,
            termo_pdf_bytes=termo_pdf_bytes, termo_pdf_nome=termo_pdf_nome,
        )
        n_licencas = cls._devolver_licencas_ativas(usuario=usuario, executado_por=executado_por)
        cls._marcar_desligado(usuario=usuario, executado_por=executado_por)
        return {"itens": n_itens, "licencas": n_licencas}

    @classmethod
    def ativos_do_usuario(cls, usuario):
        """Só leitura — equipamentos e licenças ativas do colaborador agora,
        como OBJETOS completos (não só a contagem). Usado para montar o termo
        de devolução consolidado (ver `services/termos.gerar_termo_desligamento_docx`)."""
        return {
            "itens": cls._itens_ativos_do_usuario(usuario),
            "licencas": cls._licencas_ativas_do_usuario(usuario),
        }

    @classmethod
    def resumo_ativos(cls, usuario):
        """Só leitura — quantos equipamentos e licenças ativas o colaborador
        tem agora. Usado no preview do formulário de devolução, antes de
        confirmar o desligamento."""
        ativos = cls.ativos_do_usuario(usuario)
        return {"itens": len(ativos["itens"]), "licencas": len(ativos["licencas"])}

    # ── Leitura (sem efeito colateral) ──────────────────────────────────

    @classmethod
    def _itens_ativos_do_usuario(cls, usuario):
        """Mesmo critério de `_itens_ativos_do_usuario` (views/usuarios.py):
        compartilhado → vínculo ativo em ItemColaborador; detentor único →
        última movimentação de transferência do item."""
        itens = []
        vistos = set()

        vinculos = (
            ItemColaborador.objects
            .filter(
                colaborador=usuario,
                ativo=True,
                item__isnull=False,
                item__compartilhado=True,
            )
            .select_related("item")
        )
        for vinculo in vinculos:
            if vinculo.item_id in vistos:
                continue
            vistos.add(vinculo.item_id)
            itens.append(vinculo.item)

        movs = (
            MovimentacaoItem.objects
            .filter(item__isnull=False, item__excluido=False)
            .select_related("item")
            .order_by("item_id", "-created_at", "-id")
        )
        for mov in movs:
            if mov.item_id in vistos:
                continue
            vistos.add(mov.item_id)

            item = mov.item
            if item.compartilhado:
                continue
            if mov.usuario_id != usuario.pk:
                continue
            if mov.tipo_movimentacao == MovService.BAIXA:
                continue
            if mov.tipo_movimentacao == MovService.TRANSFERENCIA and mov.tipo_transferencia == "devolucao":
                continue

            itens.append(item)

        return itens

    @classmethod
    def _licencas_ativas_do_usuario(cls, usuario):
        ativas = []
        vistas = set()

        movs = (
            MovimentacaoLicenca.objects
            .filter(usuario=usuario)
            .select_related("licenca")
            .order_by("licenca_id", "-created_at", "-id")
        )
        for mov in movs:
            if mov.licenca_id in vistas:
                continue
            vistas.add(mov.licenca_id)
            if mov.tipo == TipoMovLicencaChoices.ATRIBUICAO:
                ativas.append(mov.licenca)

        return ativas

    # ── Escrita (devolve de fato) ────────────────────────────────────────

    @classmethod
    def _devolver_itens_ativos(cls, *, usuario, executado_por, termo_pdf_bytes=None, termo_pdf_nome=None):
        itens = cls._itens_ativos_do_usuario(usuario)
        for item in itens:
            cls._registrar_devolucao_item(
                item=item, usuario=usuario, executado_por=executado_por,
                termo_pdf_bytes=termo_pdf_bytes, termo_pdf_nome=termo_pdf_nome,
            )
        return len(itens)

    @classmethod
    def _registrar_devolucao_item(cls, *, item, usuario, executado_por, termo_pdf_bytes=None, termo_pdf_nome=None):
        item = Item.objects.select_for_update().get(pk=item.pk)

        mov = MovimentacaoItem(
            tipo_movimentacao=MovService.TRANSFERENCIA,
            tipo_transferencia="devolucao",
            item=item,
            usuario=usuario,
            quantidade=1,
            localidade_origem=item.localidade,
            centro_custo_origem=item.centro_custo,
            observacao=(
                f"Devolução automática — desligamento do colaborador {usuario.nome}, "
                f"registrado por {executado_por}."
            ),
        )

        restaurar_cc = False

        if not item.compartilhado:
            ultima_entrega = (
                MovimentacaoItem.objects
                .filter(item=item, tipo_movimentacao=MovService.TRANSFERENCIA, tipo_transferencia="entrega")
                .order_by("-created_at", "-id")
                .first()
            )
            if ultima_entrega is not None:
                restaurar_cc = True
                mov.centro_custo_destino = ultima_entrega.centro_custo_origem

        MovService.preencher_auditoria(mov, executado_por, criando=True)
        mov.full_clean()
        mov.save()

        # Anexa cópia do termo assinado na devolução principal (ou do termo
        # consolidado gerado para o desligamento, se foi esse o usado) — sem
        # isso, cada devolução automática da cascata ficaria sem nenhum
        # comprovante de responsabilidade. Ver DesligamentoService.desligar_e_liberar_ativos.
        if termo_pdf_bytes:
            from django.core.files.base import ContentFile
            nome = termo_pdf_nome or f"termo_desligamento_{item.pk}.pdf"
            mov.termo_pdf.save(nome, ContentFile(termo_pdf_bytes), save=True)

        update_fields = []

        if item.compartilhado:
            MovService._sync_vinculo_compartilhado(mov=mov, item=item, user=executado_por)

            existe_vinculo_ativo = ItemColaborador.objects.filter(item=item, ativo=True).exists()
            if not existe_vinculo_ativo and item.status == StatusItemChoices.ATIVO:
                item.status = StatusItemChoices.BACKUP
                update_fields.append("status")

            cc_anterior_id = item.centro_custo_id
            if MovService.aplicar_centro_custo_compartilhado(item=item, cc_anterior_id=cc_anterior_id):
                update_fields.append("centro_custo")
        else:
            if item.status == StatusItemChoices.ATIVO:
                item.status = StatusItemChoices.BACKUP
                update_fields.append("status")

            if restaurar_cc:
                # Restaura o CC original do item (pode ser None se ele não
                # tinha CC antes da entrega) — mesmo comportamento do fluxo
                # manual de devolução (`_registrar_movimentacao_padrao`).
                item.centro_custo = mov.centro_custo_destino
                update_fields.append("centro_custo")

        if "centro_custo" in update_fields:
            novo_pmb = MovService.pmb_por_centro_custo(item.centro_custo)
            if item.pmb != novo_pmb:
                item.pmb = novo_pmb
                update_fields.append("pmb")

        if update_fields:
            MovService.preencher_auditoria(item, executado_por, criando=False)
            if hasattr(item, "atualizado_por"):
                update_fields.append("atualizado_por")
            item.save(update_fields=list(set(update_fields)))

        return mov

    @classmethod
    def _devolver_licencas_ativas(cls, *, usuario, executado_por):
        from ProjetoEstoque.forms import MovimentacaoLicencaForm

        licencas = cls._licencas_ativas_do_usuario(usuario)
        total = 0

        for licenca in licencas:
            form = MovimentacaoLicencaForm(data={
                "tipo": TipoMovLicencaChoices.DEVOLUCAO,
                "licenca": licenca.pk,
                "usuario": usuario.pk,
                "observacao": (
                    f"Devolução automática — desligamento do colaborador {usuario.nome}, "
                    f"registrado por {executado_por}."
                ),
            })

            if form.is_valid():
                form.save(user=executado_por)
                total += 1
            else:
                logger.warning(
                    "Desligamento: falha ao devolver licença %s do colaborador %s: %s",
                    licenca.pk, usuario.pk, form.errors,
                )

        return total

    @classmethod
    def _marcar_desligado(cls, *, usuario, executado_por):
        usuario.status = StatusUsuarioChoices.DESLIGADO
        if not usuario.data_termino:
            usuario.data_termino = timezone.localdate()

        update_fields = ["status", "data_termino"]
        if hasattr(usuario, "atualizado_por"):
            usuario.atualizado_por = executado_por
            update_fields.append("atualizado_por")

        usuario.save(update_fields=update_fields)
