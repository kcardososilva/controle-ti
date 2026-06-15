import re
import unicodedata
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


def _normalizar(texto):
    """Minúsculas sem acentos — para comparar rótulos do template de forma robusta."""
    texto = (texto or "").strip().lower()
    return "".join(
        c for c in unicodedata.normalize("NFD", texto)
        if unicodedata.category(c) != "Mn"
    )


def _slug_termo(value, maxlen=28):
    """Converte um texto em um trecho seguro para a numeração do termo (sem acento, MAIÚSCULO)."""
    s = unicodedata.normalize("NFD", str(value or ""))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").upper()
    return s[:maxlen].strip("_")


def _numero_termo_auto(tipo, hoje, nome_colaborador, colaborador):
    """
    Monta a numeração do termo no mesmo padrão de antes, porém SEM o ID do item:
    {PREFIXO}-{AAAAMMDD}-{NOME_COLABORADOR}-{CENTRO_DE_CUSTO}.
    """
    prefixo = "ENT" if tipo == "entrega" else "DEV"
    partes = [prefixo, hoje.strftime("%Y%m%d")]

    nome_slug = _slug_termo(nome_colaborador) if nome_colaborador and nome_colaborador != "-" else ""
    if nome_slug:
        partes.append(nome_slug)

    cc_obj = getattr(colaborador, "centro_custo", None)
    cc_codigo = _safe(getattr(cc_obj, "numero", None), "")
    if not cc_codigo or cc_codigo == "-":
        cc_codigo = _slug_termo(getattr(cc_obj, "departamento", None))
    if cc_codigo and cc_codigo != "-":
        partes.append(str(cc_codigo))

    return "-".join(partes)


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
    """
    Retorna o usuário atualmente com o item: última entrega (transferencia + tipo_transferencia=entrega)
    sem uma devolução posterior. Garante que devolução não seja confundida com posse atual.
    """
    from ProjetoEstoque.models import TipoMovimentacaoChoices, TipoTransferenciaChoices

    ultima_entrega = (
        MovimentacaoItem.objects
        .filter(
            item=item,
            tipo_movimentacao=TipoMovimentacaoChoices.TRANSFERENCIA,
            tipo_transferencia=TipoTransferenciaChoices.ENTREGA,
            usuario__isnull=False,
        )
        .select_related("usuario")
        .order_by("-created_at")
        .first()
    )

    if not ultima_entrega:
        return None

    # Verifica se houve devolução posterior
    devolucao_posterior = (
        MovimentacaoItem.objects
        .filter(
            item=item,
            tipo_movimentacao=TipoMovimentacaoChoices.TRANSFERENCIA,
            tipo_transferencia=TipoTransferenciaChoices.DEVOLUCAO,
            created_at__gt=ultima_entrega.created_at,
        )
        .exists()
    )

    if devolucao_posterior:
        return None

    return ultima_entrega.usuario


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

    # Numeração: sem ID — usa nome do colaborador + centro de custo.
    # O campo do formulário continua válido como sobrescrita manual.
    numero_termo = _safe(form_data.get("numero_termo"), "")
    if not numero_termo:
        numero_termo = _numero_termo_auto(tipo, hoje, nome_colaborador, colaborador)

    return {
        "tipo": tipo,
        "data_hoje": hoje.strftime("%d/%m/%Y"),
        "numero_termo": numero_termo,
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
    """
    Preenche a tabela do termo localizando cada linha pelo RÓTULO da 1ª célula,
    em vez de índices fixos. Assim, editar o template (inserir/remover/reordenar
    linhas, alterar rótulos ou adicionar páginas) não quebra a geração.

    Quando há mais de uma linha com o mesmo rótulo (o template usa linhas de
    cabeçalho + linha de digitação), preenche a linha de digitação — preferindo
    a de mais colunas e, em empate, a primeira ainda não usada.
    """
    if not doc.tables:
        return

    table = doc.tables[0]
    rows = table.rows

    # (predicado sobre o rótulo normalizado, valor a inserir)
    campos = [
        (lambda t: "termo" in t and "chamado" not in t
                   and ("uso do ti" in t or "responsab" in t or "entrega" in t or "devoluc" in t or " n" in t),
         dados["numero_termo"]),
        (lambda t: "chamado" in t, dados["numero_chamado"]),
        (lambda t: "descric" in t and "equipamento" in t, dados["descricao_equipamento"]),
        (lambda t: t == "serie" or t.startswith("serie"), dados["serie"]),
        (lambda t: "acessorio" in t, dados["acessorios"]),
        (lambda t: "plaqueta" in t, dados["plaqueta"]),
        (lambda t: "estabelecimento" in t, dados["estabelecimento"]),
        (lambda t: "observac" in t, dados["observacoes"]),
    ]

    usados = set()
    for predicado, valor in campos:
        candidatos = []
        for ri, row in enumerate(rows):
            if ri in usados or len(row.cells) < 2:
                continue
            label = _normalizar(row.cells[0].text)
            if label and predicado(label):
                candidatos.append((ri, row))

        if not candidatos:
            continue

        # linha de digitação = mais colunas; empate = primeira (menor índice)
        ri, row = max(candidatos, key=lambda c: (len(c[1].cells), -c[0]))
        _set_cell(row.cells[1], valor)
        usados.add(ri)


def _ajustar_layout_documento(doc):
    """
    Ajuste de layout NÃO destrutivo.

    Antes, este método forçava alinhamento, espaçamento e recuo em TODOS os
    parágrafos — o que sobrescrevia a formatação do template. Por isso, edições
    feitas no modelo base (novas linhas, espaçamentos, quebras de página) não
    apareciam no termo gerado. Agora preservamos a formatação do template e só
    aplicamos ajustes seguros:
      - margens A4;
      - centraliza apenas o título;
      - garante um tamanho de fonte mínimo onde o run não define nenhum.
    """
    for section in doc.sections:
        section.top_margin = 720000      # ~2 cm
        section.bottom_margin = 720000   # ~2 cm
        section.left_margin = 720000     # ~2 cm
        section.right_margin = 720000    # ~2 cm

    for p in doc.paragraphs:
        texto = (p.text or "").strip()

        # Mantém centralizado somente o título — restante segue o template.
        if texto.startswith("TERMO DE RESPONSABILIDADE") or texto.startswith("POLÍTICA"):
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER

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