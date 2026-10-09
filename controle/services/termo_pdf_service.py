# -*- coding: utf-8 -*-
"""
Emissão do TERMO DE RESPONSABILIDADE JÁ ASSINADO, em PDF.

Diferença em relação a `services/termos.py` (que continua existindo e não foi
tocado): aquele produz o `.docx` em branco para imprimir e assinar à mão; este
produz o PDF **final**, com o traço da assinatura capturado no tablet embutido
e com o comprovante de autenticidade do ato.

Decisão central — de onde vem o texto legal
-------------------------------------------
O texto é lido dos MESMOS modelos base de `docs_templates/termos/*.docx` que o
`.docx` usa, via `python-docx`. Não há uma segunda cópia do texto aqui dentro.
Esse ponto não é estética: o termo é documento jurídico mantido pelo RH no
Word, e ter duas redações do mesmo termo — uma no Word, outra em código —
significa que uma hora as duas divergem, e aí metade dos colaboradores assinou
uma coisa e metade assinou outra, sem ninguém perceber. Mudar a cláusula no
.docx muda as duas vias no mesmo ato.

O texto extraído vai junto para o `dados["texto_legal"]` da
`TermoAssinatura` (snapshot), porque o que vale é o texto que estava na tela
no momento em que o colaborador assinou — uma revisão posterior do modelo base
não pode reescrever retroativamente o que ele aceitou.

Por que reportlab e não converter o .docx para PDF
--------------------------------------------------
Converter exigiria Word instalado no servidor (`docx2pdf`/COM) ou LibreOffice —
dependência pesada, que falha de formas difíceis de diagnosticar num serviço
rodando como tarefa do Windows, e que não existe hoje na máquina de produção.
O reportlab já é dependência do projeto (PDF do mapa de rota do Quiosque) e dá
controle exato sobre onde a imagem da assinatura entra na página.

CPF E RG NÃO SÃO PERSISTIDOS — leia antes de mexer
--------------------------------------------------
Os documentos que o signatário digita no tablet entram **só** neste PDF e em
mais lugar nenhum: não há coluna para eles no banco, não entram no snapshot
`dados` e não são logados. Eles chegam aqui pelo parâmetro `documentos`, em
memória, e morrem quando a função retorna. Ver `DOCUMENTOS_NAO_PERSISTEM`
abaixo e a regra 35 do CLAUDE.md antes de "só guardar para facilitar" —
guardar CPF de 1.376 colaboradores numa tabela é criar um alvo que o sistema
não precisa ter.

CLÁUSULA 13 DO MODELO BASE — leia antes de mexer
------------------------------------------------
O modelo de ENTREGA diz, hoje, que "a Assinatura do presente Termo será
realizada fisicamente". Este service **não reescreve essa cláusula** — alterar
redação de documento jurídico da empresa não é decisão de código. O que ele faz
é acrescentar, acima do campo de assinatura, um bloco de ACEITE ELETRÔNICO
expresso (`TEXTO_ACEITE_ELETRONICO`), que o signatário marca no tablet antes de
assinar: é o aceite da forma eletrônica no próprio ato, nos termos do art. 10,
§2º da MP 2.200-2/2001 (assinatura eletrônica vale entre as partes que a
admitem). Mesmo assim, a cláusula 13 deveria ser revista pelo RH/Jurídico para
prever expressamente a via eletrônica — enquanto ela disser "fisicamente", o
documento carrega uma contradição interna que um contraditório pode explorar.
"""

import hashlib
import unicodedata
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.utils import timezone
from docx import Document
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    Image,
    KeepTogether,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

TERMOS_DIR = Path(settings.BASE_DIR) / "docs_templates" / "termos"
TEMPLATE_POR_TIPO = {
    "entrega": TERMOS_DIR / "TEMPLATE BRANCO - Termo_de_Entrega.docx",
    "devolucao": TERMOS_DIR / "TEMPLATE BRANCO - Termo_de_Devolução.docx",
}

# Lembrete em código, não só em comentário: quem for acrescentar um campo de
# documento ao fluxo tropeça nisto antes de criar a coluna.
DOCUMENTOS_NAO_PERSISTEM = (
    "CPF e RG do signatário existem apenas dentro do PDF emitido. Não criar "
    "campo no model, não gravar em `dados`, não logar."
)

EMPRESA = "SANTA COLOMBA AGROPECUÁRIA LTDA"
SETOR = "Tecnologia da Informação"

# Texto do aceite que o signatário marca no tablet e que vai impresso no PDF.
# Fica aqui, e não no template .docx, porque é específico da via eletrônica:
# o .docx impresso é assinado à mão e não precisa dele.
TEXTO_ACEITE_ELETRONICO = (
    "Declaro que li e compreendi integralmente o termo acima, que confirmo a conferência "
    "do(s) equipamento(s) nele descrito(s) e que aceito firmá-lo por assinatura eletrônica, "
    "coletada de forma manuscrita em dispositivo eletrônico na presença do responsável do "
    "setor de Tecnologia da Informação. Reconheço esta assinatura eletrônica como válida e "
    "vinculante entre as partes, nos termos do art. 10, § 2º, da Medida Provisória nº "
    "2.200-2/2001."
)

# Rótulos do modelo base que NÃO entram no corpo do texto legal: são o bloco de
# assinatura manuscrita do .docx (linhas de "Assinatura: ____"), que na via
# eletrônica é substituído pelo bloco de assinatura capturada.
_ROTULOS_BLOCO_ASSINATURA = (
    "colaborador:",
    "assinatura:",
    "nome:",
    "data:",
    "responsavel pela entrega",
    "responsavel pelo recebimento",
    "responsavel",
)

_CORES = {
    "texto": colors.HexColor("#1f2430"),
    "suave": colors.HexColor("#5b6475"),
    "fraco": colors.HexColor("#8b93a3"),
    "marca": colors.HexColor("#1c4e80"),
    "linha": colors.HexColor("#d7dbe3"),
    "linha_forte": colors.HexColor("#b4bac6"),
    "fundo": colors.HexColor("#f4f6f9"),
    "fundo_aceite": colors.HexColor("#eef4fb"),
}


# ---------------------------------------------------------------------------
# Leitura do texto legal a partir do modelo base (.docx)
# ---------------------------------------------------------------------------

def _normalizar(texto: str) -> str:
    texto = (texto or "").strip().lower()
    return "".join(
        c for c in unicodedata.normalize("NFD", texto)
        if unicodedata.category(c) != "Mn"
    )


def _e_linha_de_assinatura(texto: str) -> bool:
    norm = _normalizar(texto)
    if not norm:
        return True
    if norm.startswith(_ROTULOS_BLOCO_ASSINATURA):
        return True
    # Linha composta só de underscores/traços (campo para preencher à mão).
    return set(texto.strip()) <= set("_- ")


def _e_rotulo_de_secao(texto: str) -> bool:
    """
    Rótulo de bloco do modelo impresso ("Condições da Devolução:", "Descrição
    do Equipamento Devolvido:") — não é texto do termo, é cabeçalho de uma
    seção que o PDF eletrônico já desenha com estrutura própria. Reimprimi-lo
    no corpo criaria um segundo título solto no meio das cláusulas.

    O critério é curto e termina em dois-pontos: nenhuma cláusula do modelo
    tem menos de 70 caracteres, e nenhuma delas termina em ":".
    """
    limpo = (texto or "").strip()
    return limpo.endswith(":") and len(limpo) <= 70


def _e_clausula(paragrafo) -> bool:
    """
    Um parágrafo é cláusula do termo quando é item de lista numerada.

    Dois jeitos, porque os DOIS modelos base fazem diferente — e descobrir isso
    só pelo de entrega foi o que o teste pegou:

      • Termo de ENTREGA   → estilo "List Paragraph" (13 cláusulas);
      • Termo de DEVOLUÇÃO → estilo "Normal" com `w:numPr` no `pPr`, que é a
        numeração aplicada direto no parágrafo (3 cláusulas).

    O `numPr` é o teste mais confiável dos dois (é o que o Word usa para
    numerar de verdade); o estilo entra como complemento, para o caso de uma
    revisão futura aplicar a lista pelo estilo sem numeração explícita.
    """
    estilo = (paragrafo.style.name or "") if paragrafo.style else ""
    if estilo == "List Paragraph":
        return True

    propriedades = paragrafo._p.pPr
    return propriedades is not None and propriedades.numPr is not None


@lru_cache(maxsize=4)
def carregar_texto_legal(tipo: str) -> dict:
    """
    Extrai do modelo base o texto que o colaborador assina.

    Retorna ``{"titulo": str, "intro": str, "clausulas": [str], "fecho": str}``.

    A separação é feita pela NUMERAÇÃO do parágrafo (ver `_e_clausula`), não
    por índice fixo: cláusula é item de lista numerada, o que vem antes é
    introdução e o que vem depois é o fecho. Assim, acrescentar ou remover uma
    cláusula no Word não exige mexer aqui — era o risco de usar posição fixa, e
    é o mesmo cuidado que `services/termos.py` já toma ao localizar as linhas
    da tabela pelo rótulo.

    Cacheado em memória (`lru_cache`): o arquivo só muda quando o RH publica
    uma revisão, e aí o processo é reiniciado no deploy. Em desenvolvimento,
    use `carregar_texto_legal.cache_clear()` depois de editar o .docx.
    """
    caminho = TEMPLATE_POR_TIPO.get(tipo)
    if caminho is None:
        raise ValueError(f"Tipo de termo inválido: {tipo!r}")
    if not caminho.exists():
        raise FileNotFoundError(f"Modelo base do termo não encontrado: {caminho}")

    doc = Document(str(caminho))

    titulo = ""
    intro = ""
    clausulas: list[str] = []
    fecho_partes: list[str] = []

    for paragrafo in doc.paragraphs:
        texto = (paragrafo.text or "").strip()
        if not texto:
            continue

        norm = _normalizar(texto)

        if not titulo and norm.startswith("termo de responsabilidade"):
            titulo = texto
            continue

        if _e_clausula(paragrafo):
            clausulas.append(texto)
            continue

        if _e_linha_de_assinatura(texto) or _e_rotulo_de_secao(texto):
            continue

        if not clausulas and not intro:
            # Primeiro parágrafo corrido depois do título: é o "Eu, ____" —
            # descartado, porque na via eletrônica a identificação do
            # signatário é montada com os dados reais (ver `_intro_assinada`).
            intro = texto
            continue

        if clausulas:
            fecho_partes.append(texto)

    return {
        "titulo": titulo or "TERMO DE RESPONSABILIDADE, GUARDA E USO DE EQUIPAMENTOS CORPORATIVOS",
        "intro": intro,
        "clausulas": clausulas,
        "fecho": " ".join(fecho_partes),
    }


def _intro_assinada(termo, texto_legal: dict, documentos: dict | None = None) -> str:
    """
    Reescreve o "Eu, ____, portador(a) de RG nº ____ e CPF/MF nº ____" do
    modelo com os dados REAIS do signatário, preservando o resto da frase.

    O modelo tem esses campos em branco porque é feito para caneta. Na via
    eletrônica eles estão preenchidos, e deixar a lacuna impressa num documento
    que não vai receber caneta nenhuma só gera dúvida sobre a validade.

    CPF e RG chegam por `documentos`, em memória, e **não** saem de nenhum
    campo do model — eles não são persistidos (ver `DOCUMENTOS_NAO_PERSISTEM`).
    """
    documentos = documentos or {}
    nome = (termo.signatario_nome or getattr(termo.colaborador, "nome", "") or "").strip()
    cpf = str(documentos.get("cpf") or "").strip()
    rg = str(documentos.get("rg") or "").strip()

    identificacao = [f"<b>{nome or 'Colaborador não identificado'}</b>"]
    if rg:
        identificacao.append(f"portador(a) de cédula de identidade RG nº <b>{rg}</b>")
    if cpf:
        identificacao.append(f"inscrito(a) no CPF/MF nº <b>{cpf}</b>")

    matricula = (getattr(termo.colaborador, "matricula", "") or "").strip()
    if matricula:
        identificacao.append(f"matrícula <b>{matricula}</b>")

    sujeito = ", ".join(identificacao)

    # "de agora em diante chamado de USUÁRIO" saiu a pedido da operação: o
    # jargão vinha do modelo impresso, onde servia para apresentar o termo
    # "Usuário" usado nas cláusulas. Na via eletrônica o nome real do
    # signatário já está em negrito logo acima, e a frase só atrapalhava a
    # leitura de quem assina em pé, no tablet. As cláusulas seguem falando em
    # "Usuário" — o texto delas vem do .docx e não é tocado aqui.
    if termo.tipo == "entrega":
        return (
            f"Eu, {sujeito}, declaro que recebi, li e entendi as obrigações deste Termo de "
            f"Responsabilidade pela Guarda e Uso de Equipamentos Corporativos da SANTA COLOMBA "
            f"e me comprometo, de forma irretratável, com as regras e obrigações abaixo:"
        )
    return (
        f"Eu, {sujeito}, declaro que devolvi o(s) equipamento(s) descrito(s) neste documento, "
        f"pertencente(s) à SANTA COLOMBA, nas condições especificadas. Declaro estar ciente de "
        f"que, caso haja danos não reportados ou perda de algum item, poderei ser "
        f"responsabilizado conforme as normas internas da empresa."
    )


# ---------------------------------------------------------------------------
# Estilos
# ---------------------------------------------------------------------------
# Família literal (Helvetica), e não token do design system: PDF é documento
# gerado, não carrega webfont e tem de ser autossuficiente — é a exceção que a
# regra 30 do CLAUDE.md prevê para `*_pdf.html` e vale igual aqui.

def _estilos() -> dict:
    base = ParagraphStyle(
        "zl-base",
        fontName="Helvetica",
        fontSize=8.6,
        leading=12.2,
        textColor=_CORES["texto"],
        alignment=TA_JUSTIFY,
    )
    return {
        "titulo": ParagraphStyle(
            "zl-titulo", parent=base, fontName="Helvetica-Bold", fontSize=12.5,
            leading=15.5, alignment=TA_CENTER, textColor=_CORES["marca"],
            spaceAfter=2,
        ),
        "subtitulo": ParagraphStyle(
            "zl-subtitulo", parent=base, fontSize=8, leading=10.5,
            alignment=TA_CENTER, textColor=_CORES["suave"], spaceAfter=8,
        ),
        "secao": ParagraphStyle(
            "zl-secao", parent=base, fontName="Helvetica-Bold", fontSize=8.2,
            leading=10.5, alignment=TA_LEFT, textColor=_CORES["marca"],
            spaceBefore=9, spaceAfter=4,
        ),
        "corpo": base,
        "clausula": ParagraphStyle(
            "zl-clausula", parent=base, leftIndent=11, firstLineIndent=-11,
            spaceAfter=3.6,
        ),
        "aceite": ParagraphStyle(
            "zl-aceite", parent=base, fontSize=8.2, leading=11.4,
            leftIndent=7, rightIndent=7, spaceBefore=5, spaceAfter=5,
        ),
        "rotulo": ParagraphStyle(
            "zl-rotulo", parent=base, fontName="Helvetica-Bold", fontSize=6.6,
            leading=8.4, alignment=TA_LEFT, textColor=_CORES["fraco"],
        ),
        "valor": ParagraphStyle(
            "zl-valor", parent=base, fontSize=8.4, leading=10.8, alignment=TA_LEFT,
        ),
        "assinatura_nome": ParagraphStyle(
            "zl-assin-nome", parent=base, fontName="Helvetica-Bold", fontSize=8.6,
            leading=11, alignment=TA_CENTER,
        ),
        "assinatura_meta": ParagraphStyle(
            "zl-assin-meta", parent=base, fontSize=6.8, leading=8.8,
            alignment=TA_CENTER, textColor=_CORES["suave"],
        ),
        "auditoria": ParagraphStyle(
            "zl-auditoria", parent=base, fontName="Courier", fontSize=6.9,
            leading=9.4, alignment=TA_LEFT, textColor=_CORES["suave"],
        ),
        "auditoria_rotulo": ParagraphStyle(
            "zl-auditoria-rotulo", parent=base, fontName="Helvetica-Bold",
            fontSize=6.9, leading=9.4, alignment=TA_LEFT, textColor=_CORES["texto"],
        ),
    }


# ---------------------------------------------------------------------------
# Cabeçalho / rodapé de cada página
# ---------------------------------------------------------------------------

class _TermoDocTemplate(BaseDocTemplate):
    """Moldura única do documento, com cabeçalho e rodapé desenhados em todas
    as páginas. O rodapé leva o código de verificação porque é o que permite
    conferir uma folha solta que tenha se separado do resto da impressão."""

    def __init__(self, buffer, *, termo, **kwargs):
        self._termo = termo
        super().__init__(
            buffer,
            pagesize=A4,
            leftMargin=17 * mm,
            rightMargin=17 * mm,
            topMargin=24 * mm,
            bottomMargin=20 * mm,
            title=f"Termo de {termo.get_tipo_display()} — {termo.colaborador}",
            author=EMPRESA,
            subject="Termo de Responsabilidade assinado eletronicamente",
            **kwargs,
        )
        quadro = Frame(
            self.leftMargin, self.bottomMargin,
            self.width, self.height,
            id="corpo", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
        )
        self.addPageTemplates([
            PageTemplate(id="padrao", frames=[quadro], onPage=self._moldura)
        ])

    def _moldura(self, canvas, doc):
        canvas.saveState()
        largura, altura = A4

        # Faixa de identidade no topo. Sangra até a borda de propósito: é o que
        # faz o documento ser reconhecido como "da empresa" a um metro de
        # distância, numa pilha de papel em cima da mesa do RH.
        canvas.setFillColor(_CORES["marca"])
        canvas.rect(0, altura - 6 * mm, largura, 6 * mm, stroke=0, fill=1)

        topo = altura - 14 * mm
        canvas.setFillColor(_CORES["marca"])
        canvas.setFont("Helvetica-Bold", 10.5)
        canvas.drawString(17 * mm, topo, EMPRESA)

        canvas.setFillColor(_CORES["suave"])
        canvas.setFont("Helvetica", 7.4)
        canvas.drawString(17 * mm, topo - 4.6 * mm, f"{SETOR} · Controle de Ativos")

        rotulo = f"TERMO DE {self._termo.get_tipo_display().upper()}"
        canvas.setFont("Helvetica-Bold", 7.8)
        canvas.setFillColor(_CORES["marca"])
        canvas.drawRightString(largura - 17 * mm, topo, rotulo)

        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(_CORES["fraco"])
        canvas.drawRightString(
            largura - 17 * mm, topo - 4.6 * mm,
            f"Verificação {self._termo.codigo_verificacao}",
        )

        canvas.setStrokeColor(_CORES["linha"])
        canvas.setLineWidth(0.6)
        canvas.line(17 * mm, topo - 7.6 * mm, largura - 17 * mm, topo - 7.6 * mm)

        base = 13 * mm
        canvas.line(17 * mm, base + 4 * mm, largura - 17 * mm, base + 4 * mm)
        canvas.setFont("Helvetica", 6.6)
        canvas.setFillColor(_CORES["fraco"])
        canvas.drawString(
            17 * mm, base,
            "Documento assinado eletronicamente — confira a autenticidade pelo código "
            f"{self._termo.codigo_verificacao} no sistema Zelo.",
        )
        # `doc.page` de `página X de Y`: o total só é conhecido depois de
        # paginar, e um segundo passe de build para saber o total dobraria o
        # tempo de geração com o colaborador esperando no tablet. O número da
        # página sozinho já responde "falta folha?" numa impressão grampeada.
        canvas.drawRightString(largura - 17 * mm, base, f"Página {doc.page}")
        canvas.restoreState()


# ---------------------------------------------------------------------------
# Blocos de conteúdo
# ---------------------------------------------------------------------------

def _tabela_dados(termo, estilos) -> Table:
    dados = termo.dados or {}

    def par(rotulo, valor):
        return [
            Paragraph(rotulo.upper(), estilos["rotulo"]),
            Paragraph(str(valor or "—"), estilos["valor"]),
        ]

    colaborador = termo.colaborador
    centro_custo = getattr(getattr(colaborador, "centro_custo", None), "departamento", "") or ""
    funcao = getattr(getattr(colaborador, "funcao", None), "nome", "") or ""
    localidade = getattr(getattr(colaborador, "localidade", None), "local", "") or ""

    linhas = [
        par("Nº do termo", termo.numero_termo) + par("Data", timezone.localtime(termo.assinado_em or timezone.now()).strftime("%d/%m/%Y %H:%M")),
        par("Colaborador", colaborador.nome) + par("Matrícula", getattr(colaborador, "matricula", "")),
        par("Função", funcao) + par("Centro de custo", centro_custo),
        par("Localidade", localidade) + par("Estabelecimento", dados.get("estabelecimento_label")),
        par("Nº do chamado", dados.get("numero_chamado")) + par("Responsável do TI", dados.get("responsavel_ti_nome")),
    ]

    # 31mm na coluna de rótulo: a 6,6pt em Helvetica-Bold, "ESTABELECIMENTO" e
    # "RESPONSÁVEL DO TI" ocupam ~24mm de texto e, com 26mm menos o padding,
    # quebravam em "ESTABELECIMENT / O". Rótulo cortado no meio da palavra num
    # documento que vai para pasta de RH é o tipo de detalhe que faz o
    # documento parecer improvisado.
    tabela = Table(
        linhas,
        colWidths=[31 * mm, 53 * mm, 31 * mm, 53 * mm],
        hAlign="LEFT",
    )
    tabela.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3.4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("BACKGROUND", (0, 0), (-1, -1), _CORES["fundo"]),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, colors.white),
        ("BOX", (0, 0), (-1, -1), 0.5, _CORES["linha"]),
    ]))
    return tabela


def _tabela_equipamentos(termo, estilos) -> Table:
    """
    Tabela dos equipamentos do termo.

    A coluna de PATRIMÔNIO é CONDICIONAL: `Item` não tem esse campo hoje (o
    termo em `.docx` preenche a linha "Número da plaqueta" com
    `getattr(item, "patrimonio", None)`, que sempre devolve None), e imprimir
    uma coluna de travessões num documento que vai para pasta de RH só gera a
    dúvida "por que isso está vazio?". Quando o campo existir, a coluna
    aparece sozinha — sem precisar mexer aqui.
    """
    equipamentos = (termo.dados or {}).get("equipamentos") or []
    tem_patrimonio = any((e.get("patrimonio") or "").strip() for e in equipamentos)

    cabecalho = [
        Paragraph("#", estilos["rotulo"]),
        Paragraph("EQUIPAMENTO", estilos["rotulo"]),
        Paragraph("MARCA / MODELO", estilos["rotulo"]),
        Paragraph("Nº DE SÉRIE", estilos["rotulo"]),
        Paragraph("PATRIMÔNIO" if tem_patrimonio else "CENTRO DE CUSTO", estilos["rotulo"]),
    ]

    linhas = [cabecalho]
    for i, equipamento in enumerate(equipamentos, start=1):
        marca_modelo = " / ".join(
            parte for parte in (equipamento.get("marca"), equipamento.get("modelo")) if parte
        )
        ultima = equipamento.get("patrimonio") if tem_patrimonio else equipamento.get("centro_custo")

        nome = str(equipamento.get("nome") or "—")
        if equipamento.get("compartilhado"):
            # Vai no DOCUMENTO, não só na tela: num equipamento de uso
            # compartilhado o signatário não é o único responsável, e um termo
            # de responsabilidade que não registra isso descreve um arranjo que
            # não é o real — é o próprio papel que ele assinou que precisa
            # dizer, não um aviso que passou na tela do tablet.
            nome += '<br/><font size="7" color="#475569">USO COMPARTILHADO — guarda conjunta</font>'

        linhas.append([
            Paragraph(str(i), estilos["valor"]),
            Paragraph(nome, estilos["valor"]),
            Paragraph(marca_modelo or "—", estilos["valor"]),
            Paragraph(str(equipamento.get("numero_serie") or "—"), estilos["valor"]),
            Paragraph(str(ultima or "—"), estilos["valor"]),
        ])

    if not equipamentos:
        linhas.append([
            Paragraph("—", estilos["valor"]),
            Paragraph("Nenhum equipamento informado", estilos["valor"]),
            Paragraph("—", estilos["valor"]),
            Paragraph("—", estilos["valor"]),
            Paragraph("—", estilos["valor"]),
        ])

    tabela = Table(
        linhas,
        colWidths=[8 * mm, 58 * mm, 38 * mm, 30 * mm, 34 * mm],
        hAlign="LEFT",
        repeatRows=1,
    )
    tabela.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3.4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("BACKGROUND", (0, 0), (-1, 0), _CORES["fundo"]),
        ("GRID", (0, 0), (-1, -1), 0.4, _CORES["linha"]),
        ("BOX", (0, 0), (-1, -1), 0.6, _CORES["linha_forte"]),
    ]))
    return tabela


def _imagem_assinatura(campo, largura_mm: float):
    """`Image` do traço, redimensionada preservando a proporção. Devolve None
    quando não há arquivo — o PDF segue sem a imagem em vez de estourar, porque
    a assinatura do responsável do TI é opcional."""
    if not campo:
        return None
    try:
        campo.open("rb")
        try:
            dados = campo.read()
        finally:
            campo.close()
    except (FileNotFoundError, ValueError, OSError):
        return None

    if not dados:
        return None

    from PIL import Image as PILImage

    try:
        with PILImage.open(BytesIO(dados)) as img:
            largura_px, altura_px = img.size
    except Exception:
        return None

    if not largura_px or not altura_px:
        return None

    largura = largura_mm * mm
    altura = largura * (altura_px / largura_px)
    altura_max = 20 * mm
    if altura > altura_max:
        altura = altura_max
        largura = altura * (largura_px / altura_px)

    return Image(BytesIO(dados), width=largura, height=altura)


def _bloco_assinaturas(termo, estilos) -> Table:
    """Duas colunas: colaborador (obrigatória) e responsável do TI (opcional).

    A linha de base é desenhada com `LINEBELOW` na célula do traço, e não com
    underscores num parágrafo: underscore não acompanha a largura da coluna e
    sai desalinhado do nome logo abaixo."""
    assinado_em = timezone.localtime(termo.assinado_em) if termo.assinado_em else None
    carimbo = assinado_em.strftime("%d/%m/%Y às %H:%M:%S") if assinado_em else "—"

    img_colaborador = _imagem_assinatura(termo.assinatura_colaborador, 62)
    img_responsavel = _imagem_assinatura(termo.assinatura_responsavel, 62)

    vazio = Spacer(1, 13 * mm)

    nome_colaborador = (termo.signatario_nome or termo.colaborador.nome or "—").strip()
    nome_responsavel = ((termo.dados or {}).get("responsavel_ti_nome") or "—").strip()

    rotulo_responsavel = (
        "Responsável pela Entrega (TI)" if termo.tipo == "entrega"
        else "Responsável pelo Recebimento (TI)"
    )

    linhas = [
        [img_colaborador or vazio, img_responsavel or vazio],
        [
            Paragraph(nome_colaborador, estilos["assinatura_nome"]),
            Paragraph(nome_responsavel, estilos["assinatura_nome"]),
        ],
        [
            Paragraph("Colaborador (USUÁRIO)", estilos["assinatura_meta"]),
            Paragraph(rotulo_responsavel, estilos["assinatura_meta"]),
        ],
        [
            Paragraph(
                f"Assinado eletronicamente em {carimbo}" if img_colaborador
                else "Assinatura não coletada",
                estilos["assinatura_meta"],
            ),
            Paragraph(
                f"Assinado eletronicamente em {carimbo}" if img_responsavel
                else "Conferido pelo setor de TI",
                estilos["assinatura_meta"],
            ),
        ],
    ]

    tabela = Table(linhas, colWidths=[84 * mm, 84 * mm], hAlign="CENTER")
    tabela.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, 0), "BOTTOM"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.7, _CORES["linha_forte"]),
        ("TOPPADDING", (0, 0), (-1, 0), 2),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 2),
        ("TOPPADDING", (0, 1), (-1, -1), 2.4),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 0.6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    return tabela


def _bloco_auditoria(termo, estilos, documentos: dict | None = None) -> Table:
    """
    Comprovante do ato. É o que diferencia um PDF com um rabisco colado de um
    documento que se sustenta: quem, quando, de onde, sobre qual texto.

    O SHA-256 do próprio arquivo NÃO é impresso aqui, e isso é deliberado —
    seria circular (o hash mudaria ao ser escrito dentro do arquivo que ele
    descreve). O hash é calculado sobre o PDF final e guardado em
    `TermoAssinatura.pdf_sha256`; a tela de verificação recalcula o hash do
    arquivo armazenado e compara. O que o papel leva é o `codigo_verificacao`,
    que localiza o registro.
    """
    dados = termo.dados or {}
    assinado_em = timezone.localtime(termo.assinado_em) if termo.assinado_em else None

    linhas_info = [
        ("Código de verificação", termo.codigo_verificacao),
        ("Signatário", f"{termo.signatario_nome or termo.colaborador.nome} (colaborador #{termo.colaborador_id})"),
        ("Documentos declarados", " · ".join(
            parte for parte in (
                f"CPF {(documentos or {}).get('cpf')}" if (documentos or {}).get("cpf") else "",
                f"RG {(documentos or {}).get('rg')}" if (documentos or {}).get("rg") else "",
            ) if parte
        ) or "não informados"),
        ("Data e hora da assinatura", assinado_em.strftime("%d/%m/%Y %H:%M:%S (%Z)") if assinado_em else "—"),
        ("Endereço IP de origem", termo.assinado_ip or "—"),
        ("Dispositivo", (termo.assinado_user_agent or "—")[:120]),
        ("Aceite eletrônico", "declarado pelo signatário" if termo.aceite_declarado else "não declarado"),
        ("Sessão de assinatura", f"#{termo.pk} · criada por {dados.get('criado_por') or '—'} em "
                                 f"{dados.get('criado_em') or '—'}"),
        ("Revisão do texto legal", f"{dados.get('texto_legal_clausulas', 0)} cláusulas · "
                                   f"impressão digital {dados.get('texto_legal_hash') or '—'}"),
    ]

    linhas = [[
        Paragraph(rotulo, estilos["auditoria_rotulo"]),
        Paragraph(str(valor), estilos["auditoria"]),
    ] for rotulo, valor in linhas_info]

    tabela = Table(linhas, colWidths=[44 * mm, 124 * mm], hAlign="LEFT")
    tabela.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("BACKGROUND", (0, 0), (-1, -1), _CORES["fundo"]),
        ("BOX", (0, 0), (-1, -1), 0.5, _CORES["linha"]),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.white),
    ]))
    return tabela


def _bloco_aceite(termo, estilos) -> Table:
    texto = (termo.dados or {}).get("texto_aceite") or TEXTO_ACEITE_ELETRONICO
    tabela = Table(
        [[Paragraph(f"<b>ACEITE ELETRÔNICO.</b> {texto}", estilos["aceite"])]],
        colWidths=[168 * mm],
        hAlign="LEFT",
    )
    tabela.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _CORES["fundo_aceite"]),
        ("BOX", (0, 0), (-1, -1), 0.6, _CORES["marca"]),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return tabela


# ---------------------------------------------------------------------------
# Montagem
# ---------------------------------------------------------------------------

class _Contador:
    """Numeração sequencial das seções, imune a bloco condicional."""

    def __init__(self):
        self.n = 0

    def titulo(self, texto: str) -> str:
        self.n += 1
        return f"{self.n}.&nbsp;&nbsp;{texto}"


def _regua(estilos) -> Table:
    """Fio sob o título. Uma tabela de uma célula com `LINEBELOW` em vez de um
    `HRFlowable`: o fio acompanha a largura útil do quadro e não precisa de
    cálculo de margem."""
    regua = Table([[""]], colWidths=[176 * mm], rowHeights=[1])
    regua.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, -1), 1.1, _CORES["marca"]),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return regua


def _bloco_texto(texto: str, estilos) -> Table:
    """Texto livre (acessórios, observações) numa caixa, não solto na página:
    o que o TI digitou precisa ficar visualmente separado do texto legal — são
    coisas de peso diferente, e misturadas dão a impressão de que a observação
    é mais uma cláusula."""
    conteudo = Paragraph(str(texto).strip().replace("\n", "<br/>"), estilos["corpo"])
    tabela = Table([[conteudo]], colWidths=[176 * mm], hAlign="LEFT")
    tabela.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _CORES["fundo"]),
        ("BOX", (0, 0), (-1, -1), 0.5, _CORES["linha"]),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return tabela


def nome_arquivo_pdf(termo) -> str:
    """Nome em ASCII puro: vai para o `Content-Disposition` da resposta HTTP e
    para o nome do arquivo no disco do Windows — acento e espaço quebram os
    dois de formas diferentes."""
    def slug(valor):
        valor = str(valor or "")
        sem_acento = "".join(
            c for c in unicodedata.normalize("NFD", valor)
            if unicodedata.category(c) != "Mn"
        )
        limpo = "".join(c if c.isalnum() else "_" for c in sem_acento)
        return "_".join(parte for parte in limpo.split("_") if parte)

    partes = [
        f"termo_{termo.tipo}",
        slug(termo.colaborador.nome)[:40],
        termo.codigo_verificacao,
    ]
    return "_".join(parte for parte in partes if parte) + ".pdf"


def gerar_pdf_assinado(termo, *, documentos: dict | None = None) -> bytes:
    """
    Monta o PDF final do termo assinado e devolve os bytes.

    `documentos` é um dict opcional `{"cpf": ..., "rg": ...}` com o que o
    signatário digitou no tablet. Vem por parâmetro, e não do model, porque
    esses dados **não são persistidos** (ver `DOCUMENTOS_NAO_PERSISTEM`):
    reemitir o PDF depois produz o mesmo documento sem as linhas de CPF/RG,
    o que é o comportamento desejado — a via original fica com o colaborador
    e no `MovimentacaoItem.termo_pdf`.

    Ordem das seções — o documento é lido de cima para baixo por quem for
    conferir, então a identificação vem antes do texto, o texto antes do
    aceite, e o comprovante técnico por último, para não competir com o que
    tem valor contratual.
    """
    estilos = _estilos()
    texto_legal = (termo.dados or {}).get("texto_legal") or carregar_texto_legal(termo.tipo)
    dados = termo.dados or {}

    historia = []

    historia.append(Paragraph(texto_legal["titulo"], estilos["titulo"]))
    historia.append(Paragraph(
        f"{EMPRESA} &nbsp;·&nbsp; via eletrônica &nbsp;·&nbsp; "
        f"{'entrega' if termo.tipo == 'entrega' else 'devolução'} de equipamento",
        estilos["subtitulo"],
    ))
    historia.append(_regua(estilos))

    # Contador de seção: as seções 3 e 4 são CONDICIONAIS (só existem com
    # acessórios/observações). Numerar com literal fazia o documento sair com
    # "1, 2, 4" quando havia observação mas não acessório — numeração que pula
    # num documento que vai para pasta de RH levanta a pergunta errada ("cadê
    # a 3?"). O contador resolve sozinho.
    secao = _Contador()

    historia.append(Paragraph(secao.titulo("Identificação"), estilos["secao"]))
    historia.append(_tabela_dados(termo, estilos))

    historia.append(Paragraph(secao.titulo("Equipamento(s)"), estilos["secao"]))
    historia.append(_tabela_equipamentos(termo, estilos))

    if (dados.get("acessorios") or "").strip():
        historia.append(Paragraph(secao.titulo("Acessórios entregues"), estilos["secao"]))
        historia.append(_bloco_texto(dados["acessorios"], estilos))

    if (dados.get("observacoes") or "").strip():
        historia.append(Paragraph(secao.titulo("Observações"), estilos["secao"]))
        historia.append(_bloco_texto(dados["observacoes"], estilos))

    historia.append(Paragraph(secao.titulo("Declaração do colaborador"), estilos["secao"]))
    historia.append(Paragraph(_intro_assinada(termo, texto_legal, documentos), estilos["corpo"]))
    historia.append(Spacer(1, 5))

    for i, clausula in enumerate(texto_legal["clausulas"], start=1):
        historia.append(Paragraph(f"<b>{i}.</b>&nbsp;&nbsp;{clausula}", estilos["clausula"]))

    if texto_legal.get("fecho"):
        historia.append(Spacer(1, 4))
        historia.append(Paragraph(f"<i>{texto_legal['fecho']}</i>", estilos["corpo"]))

    historia.append(Spacer(1, 8))
    historia.append(_bloco_aceite(termo, estilos))

    # O bloco de assinatura e o comprovante não podem ser partidos no meio por
    # uma quebra de página: meia assinatura numa folha e meio nome na seguinte
    # é exatamente o defeito que o `.docx` levou tempo para deixar de ter
    # (ver `_manter_bloco_assinatura_unido` em services/termos.py).
    historia.append(Spacer(1, 10))
    historia.append(KeepTogether([
        Paragraph("Assinaturas", estilos["secao"]),
        Spacer(1, 4),
        _bloco_assinaturas(termo, estilos),
    ]))

    historia.append(KeepTogether([
        Paragraph("Comprovante de assinatura eletrônica", estilos["secao"]),
        _bloco_auditoria(termo, estilos, documentos),
    ]))

    buffer = BytesIO()
    _TermoDocTemplate(buffer, termo=termo).build(historia)
    return buffer.getvalue()


def sha256_bytes(dados: bytes) -> str:
    return hashlib.sha256(dados).hexdigest()


def sha256_arquivo(campo) -> str:
    """SHA-256 de um `FileField` já armazenado, lido em blocos (o PDF é
    pequeno, mas ler em bloco é o que mantém a função utilizável se um dia o
    termo passar a embutir fotos do equipamento)."""
    if not campo:
        return ""
    digest = hashlib.sha256()
    campo.open("rb")
    try:
        for bloco in iter(lambda: campo.read(64 * 1024), b""):
            digest.update(bloco)
    finally:
        campo.close()
    return digest.hexdigest()
