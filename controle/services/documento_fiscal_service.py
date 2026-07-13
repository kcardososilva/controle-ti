"""
DocumentoFiscalService — gera o "Documento Fiscal de Remessa" (aviso interno de
controle, estilo nota fiscal) a partir de itens em Remessa (ver
ProjetoEstoque.models.SeparacaoItem/LoteSeparacao) e dispara o e-mail ao setor
Fiscal com o PDF anexado.

NÃO é a Nota Fiscal Eletrônica real do fornecedor — é um documento de controle
interno (sem CFOP/tributos) para agilizar o aviso ao Fiscal de que um
equipamento está indo (Envio) ou voltando (Devolução) ao fornecedor.

As colunas mostradas dependem do vínculo de Locação do item (`Item.locado`),
não do tipo do documento — um mesmo documento pode misturar itens próprios e
locados:
  · Locados (contrato de Locação em aberto/encerrando) → Fornecedor, Subtipo,
    Modelo, Nº de Série. Sem valor: não é uma venda, é devolução de um bem
    que já é do fornecedor.
  · Próprios (não locados) → Fornecedor, Subtipo, Modelo, Nº de Série, Valor
    do Equipamento (`Item.valor`, custo de aquisição — não confundir com
    `Locacao.valor_mensal`).

O PDF nunca é persistido em disco — é gerado sob demanda (para o e-mail e para
o botão de novo download), evitando expor o documento por /media/ (servido sem
autenticação).
"""
from __future__ import annotations

from io import BytesIO

from django.core.exceptions import ValidationError
from django.template.loader import get_template
from xhtml2pdf import pisa

from ProjetoEstoque.models import DocumentoFiscalRemessa, TipoSeparacaoChoices


class DocumentoFiscalService:

    @staticmethod
    def _fmt_valor(valor) -> str:
        if valor is None:
            return "—"
        try:
            return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        except (TypeError, ValueError):
            return str(valor)

    @classmethod
    def separar_itens(cls, itens):
        """Separa os itens do documento em (locados, próprios) — fonte única
        usada tanto pelo PDF quanto pelo e-mail (`email_alertas.
        alerta_documento_fiscal_remessa`), para as colunas exibidas nunca
        divergirem entre os dois canais."""
        locados, proprios = [], []
        for i in itens:
            if str(i.item.locado) == "sim":
                locados.append(i)
            else:
                i.valor_fmt = cls._fmt_valor(i.item.valor)
                proprios.append(i)
        return locados, proprios

    @classmethod
    def gerar_pdf_bytes(cls, documento: DocumentoFiscalRemessa) -> bytes:
        itens = list(
            documento.itens
            .select_related("item", "item__subtipo", "item__subtipo__categoria", "fornecedor")
            .order_by("item__nome")
        )
        itens_locados, itens_proprios = cls.separar_itens(itens)
        titulo = "Envio ao Fornecedor" if documento.tipo == TipoSeparacaoChoices.ENVIO else "Devolução ao Fornecedor"
        html = get_template("front/equipamentos/documento_fiscal_pdf.html").render({
            "documento": documento,
            "itens_locados": itens_locados,
            "itens_proprios": itens_proprios,
            "titulo": titulo,
        })
        buffer = BytesIO()
        pisa.CreatePDF(html, dest=buffer)
        return buffer.getvalue()

    @classmethod
    def gerar_e_enviar(cls, *, tipo, separacoes, user, lote=None) -> DocumentoFiscalRemessa:
        from services.email_alertas import alerta_documento_fiscal_remessa

        selecionados = [s for s in separacoes if s.tipo == tipo]
        if not selecionados:
            raise ValidationError("Selecione ao menos um item deste tipo de remessa para gerar o documento.")

        documento = DocumentoFiscalRemessa(tipo=tipo, lote=lote)
        documento.criado_por = user
        documento.atualizado_por = user
        documento.save()
        documento.itens.set(selecionados)

        pdf_bytes = cls.gerar_pdf_bytes(documento)
        enviado, destinatarios = alerta_documento_fiscal_remessa(documento, pdf_bytes)

        documento.email_enviado = enviado
        documento.destinatarios_envio = ", ".join(destinatarios) if enviado else ""
        documento.save(update_fields=["email_enviado", "destinatarios_envio"])
        return documento
