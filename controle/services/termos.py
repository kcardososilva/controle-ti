from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.utils import timezone
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from ProjetoEstoque.models import MovimentacaoItem


TERMOS_DIR = Path(settings.BASE_DIR) / "docs_templates" / "termos"
TEMPLATE_ENTREGA = TERMOS_DIR / "TEMPLATE BRANCO - Termo_de_Entrega.docx"
TEMPLATE_DEVOLUCAO = TERMOS_DIR / "TEMPLATE BRANCO - Termo_de_Devolução.docx"


def _safe(value, default="-"):
    if value is None:
        return default
    value = str(value).strip()
    return value if value else default


def _clear_paragraph(paragraph):
    p = paragraph._element
    for child in list(p):
        p.remove(child)


def _replace_paragraph_text(paragraph, text):
    style = paragraph.style
    _clear_paragraph(paragraph)
    run = paragraph.add_run(text)
    paragraph.style = style

    fmt = paragraph.paragraph_format
    fmt.space_before = Pt(0)
    fmt.space_after = Pt(4)
    fmt.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
    fmt.line_spacing = 1.15

    return run


def _find_paragraph_contains(doc, needle):
    for p in doc.paragraphs:
        if needle in (p.text or ""):
            return p
    return None


def _set_cell(cell, text):
    cell.text = ""
    p = cell.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    p.add_run(text)


def _usuario_display(usuario):
    if not usuario:
        return "-"

    return _safe(getattr(usuario, "nome", None), "-")


def get_usuario_atual_item(item):
    ultima_mov = (
        MovimentacaoItem.objects
        .filter(item=item, usuario__isnull=False)
        .select_related("usuario")
        .order_by("-created_at")
        .first()
    )
    return ultima_mov.usuario if ultima_mov and ultima_mov.usuario else None


def _build_estabelecimento_line(estabelecimento):
    checks = {
        "rio_do_meio": " ",
        "karitel": " ",
        "sao_paulo": " ",
        "sta_edwiges": " ",
    }
    if estabelecimento in checks:
        checks[estabelecimento] = "X"

    return (
        f"({checks['rio_do_meio']}) Rio do Meio   "
        f"({checks['karitel']}) Karitel   "
        f"({checks['sao_paulo']}) São Paulo   "
        f"({checks['sta_edwiges']}) Sta.Edwiges"
    )


def _build_dados(item, tipo, form_data):
    hoje = timezone.localdate()
    colaborador = form_data.get("colaborador")
    nome_colaborador = _usuario_display(colaborador)
    email_colaborador = _safe(getattr(colaborador, "email", None), "")
    centro_custo_colaborador = _safe(getattr(getattr(colaborador, "centro_custo", None), "departamento", None), "")
    funcao_colaborador = _safe(getattr(getattr(colaborador, "funcao", None), "nome", None), "")
    localidade_colaborador = _safe(getattr(getattr(colaborador, "localidade", None), "local", None), "")

    descricao = (
        f"{_safe(item.nome)} | "
        f"Modelo: {_safe(item.modelo)} | "
        f"Marca: {_safe(item.marca)} | "
        f"Centro de Custo: {_safe(getattr(item.centro_custo, 'departamento', None))}"
    )

    return {
        "tipo": tipo,
        "data_hoje": hoje.strftime("%d/%m/%Y"),
        "numero_termo": _safe(form_data.get("numero_termo"), ""),
        "numero_chamado": _safe(form_data.get("numero_chamado"), ""),
        "nome_colaborador": nome_colaborador,
        "email_colaborador": email_colaborador,
        "centro_custo_colaborador": centro_custo_colaborador,
        "funcao_colaborador": funcao_colaborador,
        "localidade_colaborador": localidade_colaborador,
        "descricao_equipamento": descricao,
        "serie": _safe(item.numero_serie),
        "acessorios": _safe(form_data.get("acessorios"), ""),
        "plaqueta": _safe(getattr(item, "patrimonio", None)),
        "estabelecimento": _build_estabelecimento_line(form_data.get("estabelecimento")),
        "observacoes": _safe(form_data.get("observacoes"), ""),
        "responsavel_ti_nome": _safe(form_data.get("responsavel_ti_nome"), ""),
    }


def _fill_intro(doc, dados):
    p_intro = _find_paragraph_contains(doc, "Eu, __")
    if not p_intro:
        return

    if dados["tipo"] == "entrega":
        texto = (
            f"Eu, {dados['nome_colaborador']}, portador(a) de cédula de identidade RG nº "
            f"_______________________________________________ e inscrito no CPF/MF nº "
            f"_____________________________ de agora em diante chamado de USUÁRIO, "
            f"declaro que recebi, li e entendi as obrigações desse Termo de Responsabilidade "
            f"pela Guarda e Uso de Equipamentos Corporativos da SANTA COLOMBA (Termo) e me "
            f"comprometo, de forma irretratável, com as regras e obrigações abaixo:"
        )
    else:
        texto = (
            f"Eu, {dados['nome_colaborador']}, portador(a) de cédula de identidade RG nº "
            f"_______________________________________________ e inscrito no CPF/MF nº "
            f"_____________________________ declaro que devolvi o(s) equipamento(s) descrito(s) "
            f"nesse documento, pertencente(s) à SANTA COLOMBA, nas condições especificadas. "
            f"Declaro estar ciente de que, caso haja danos não reportados ou perda de algum item, "
            f"poderei ser responsabilizado conforme as normas internas da empresa."
        )

    _replace_paragraph_text(p_intro, texto)


def _fill_signatures(doc, dados):
    nomes_encontrados = 0
    datas_encontradas = 0

    for p in doc.paragraphs:
        txt = (p.text or "").strip()

        if txt == "Colaborador:":
            _replace_paragraph_text(p, f"Colaborador: {dados['nome_colaborador']}")

        elif txt == "Nome:":
            nomes_encontrados += 1
            if nomes_encontrados == 1:
                _replace_paragraph_text(p, f"Nome: {dados['nome_colaborador']}")
            else:
                _replace_paragraph_text(p, f"Nome: {dados['responsavel_ti_nome']}")

        elif txt == "Data:":
            datas_encontradas += 1
            _replace_paragraph_text(p, f"Data: {dados['data_hoje']}")


def _fill_main_table(doc, dados):
    if not doc.tables:
        return

    table = doc.tables[0]

    # A estrutura dos templates enviados segue o padrão:
    # linha 0 -> termo
    # linha 1 -> chamado
    # linha 3 -> descrição equipamento
    # linha 4 -> série
    # linha 6 -> acessórios
    # linha 10 -> número da plaqueta
    # linha 12 -> estabelecimento
    # linha 13 -> observações
    mapping = {
        0: dados["numero_termo"],
        1: dados["numero_chamado"],
        3: dados["descricao_equipamento"],
        4: dados["serie"],
        6: dados["acessorios"],
        10: dados["plaqueta"],
        12: dados["estabelecimento"],
        13: dados["observacoes"],
    }

    for row_idx, value in mapping.items():
        if row_idx < len(table.rows) and len(table.rows[row_idx].cells) > 1:
            _set_cell(table.rows[row_idx].cells[1], value)


def _ajustar_layout_documento(doc):
    """
    Ajustes de layout do documento para padrão formal:
    - margens A4 adequadas
    - texto justificado
    - espaçamento entre linhas padronizado
    - remoção de espaços excessivos antes/depois
    """
    for section in doc.sections:
        section.top_margin = 720000      # ~2 cm
        section.bottom_margin = 720000   # ~2 cm
        section.left_margin = 720000     # ~2 cm
        section.right_margin = 720000    # ~2 cm

    for p in doc.paragraphs:
        texto = (p.text or "").strip()

        # mantém centralizado apenas o que realmente deve ficar centralizado
        if texto.startswith("TERMO DE RESPONSABILIDADE") or texto.startswith("POLÍTICA"):
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        elif texto.startswith("Colaborador:") or texto.startswith("Assinatura:") or texto.startswith("Nome:") or texto.startswith("Data:"):
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
        elif texto.startswith("Responsável pela Entrega") or texto.startswith("Responsável pelo Recebimento"):
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
        else:
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY

        fmt = p.paragraph_format
        fmt.space_before = Pt(0)
        fmt.space_after = Pt(4)
        fmt.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
        fmt.line_spacing = 1.15
        fmt.first_line_indent = Pt(0)

        # Ajusta fonte dos runs sem destruir o estilo do template
        for run in p.runs:
            if run.font.size is None:
                run.font.size = Pt(10.5)


def gerar_termo_docx(item, tipo, form_data):
    if tipo not in ("entrega", "devolucao"):
        raise ValueError("Tipo inválido para termo.")

    template_path = TEMPLATE_ENTREGA if tipo == "entrega" else TEMPLATE_DEVOLUCAO
    if not template_path.exists():
        raise FileNotFoundError(f"Template não encontrado: {template_path}")

    doc = Document(str(template_path))
    dados = _build_dados(item, tipo, form_data)

    _fill_intro(doc, dados)
    _fill_main_table(doc, dados)
    _fill_signatures(doc, dados)
    _ajustar_layout_documento(doc)

    output = BytesIO()
    doc.save(output)
    output.seek(0)

    nome_item = _safe(item.nome, "equipamento").replace(" ", "_")
    nome_arquivo = f"termo_{tipo}_{item.pk}_{nome_item}.docx"

    return output, nome_arquivo