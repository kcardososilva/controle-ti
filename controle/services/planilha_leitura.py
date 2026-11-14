"""
Leitura de planilhas de ENTRADA (.xlsx e .csv) para as importações do sistema.

Fonte única das primitivas que as importações de planilha compartilham — hoje a de chips
(`frota_import_service`) e a de equipamentos motorizados (`frota_equipamento_import_service`).
Cada importação decide o que quer da planilha (colunas, validação, gravação); aqui só se resolve o
que é comum a todas: ler o arquivo com segurança e devolver as linhas.

Planilha é entrada hostil e vem de gente, não de máquina:

  • LIMITES. O tamanho do arquivo e o do conteúdo já descompactado (um .xlsx é um zip: poucos KB
    podem virar centenas de MB) são recusados antes de abrir qualquer coisa. Quem chama passa os
    limites; os padrões abaixo são os do projeto.
  • NADA É EXECUTADO. Valor que começa com `=`, `+`, `-` ou `@` é texto (`texto_seguro` é o escape
    para quando o texto volta a uma planilha de relatório).
  • EXCEL "STRICT". O Excel oferece, em "Salvar como", a "Pasta de Trabalho Strict Open XML"
    (ISO/IEC 29500 Strict). Ela é um .xlsx legítimo, mas usa OUTROS endereços de namespace
    (`purl.oclc.org/ooxml/...` no lugar de `schemas.openxmlformats.org/...`), e o `openpyxl` não a
    reconhece: abre com ZERO abas, sem um único erro. A planilha de equipamentos motorizados do
    ERP chegou assim, e uma leitura que devolve "planilha vazia" para um arquivo cheio é a pior
    falha possível: silenciosa e com cara de erro do usuário. Por isso o arquivo Strict é
    reescrito em memória para os namespaces comuns (o conteúdo das células é o mesmo; as datas
    do Strict, `t="d"`, o `openpyxl` já lê) antes de abrir.
"""

from __future__ import annotations

import csv
import io
import os
import re
import unicodedata
import zipfile

from django.core.exceptions import ValidationError

MAX_BYTES = 5 * 1024 * 1024
MAX_DESCOMPACTADO = 60 * 1024 * 1024
PREFIXOS_DE_FORMULA = ("=", "+", "-", "@")

# Namespaces do formato Strict -> os do formato comum que o openpyxl conhece. Os prefixos das
# relações (`.../relationships/worksheet`, `.../sharedStrings`…) saem juntos, pela troca do prefixo.
_STRICT_PARA_COMUM = (
    (b"http://purl.oclc.org/ooxml/spreadsheetml/main", b"http://schemas.openxmlformats.org/spreadsheetml/2006/main"),
    (b"http://purl.oclc.org/ooxml/officeDocument/relationships", b"http://schemas.openxmlformats.org/officeDocument/2006/relationships"),
)
_MARCA_STRICT = _STRICT_PARA_COMUM[0][0]


# ═════════════════════════════════════════════════════════════
# Texto
# ═════════════════════════════════════════════════════════════

def normalizar(texto) -> str:
    """minúsculas, sem acento, espaços colapsados — casa cabeçalhos e nomes sem depender de
    digitação idêntica."""
    t = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode()
    return " ".join(t.strip().lower().split())


def texto_seguro(valor) -> str:
    """Texto para uma planilha de RELATÓRIO: o que começa com `=`, `+`, `-`, `@` (ou tab/CR, que
    algumas versões também interpretam) ganha um apóstrofo na frente — injeção de fórmula."""
    texto = "" if valor is None else str(valor)
    if texto[:1] in PREFIXOS_DE_FORMULA or texto[:1] in ("\t", "\r"):
        return "'" + texto
    return texto


def celula_como_texto(valor):
    """Valor da célula como texto aparado. Número do Excel: inteiro vira texto sem `.0`; número
    comprido demais (ponto flutuante) já perdeu dígitos — devolve `None` para a linha ser recusada."""
    if valor is None:
        return ""
    if isinstance(valor, bool):
        return str(valor)
    if isinstance(valor, int):
        return str(valor)
    if isinstance(valor, float):
        if valor != valor or abs(valor) >= 1e15 or valor != int(valor):
            return None
        return str(int(valor))
    return " ".join(str(valor).split()) if "\n" not in str(valor) else str(valor).strip()


def linha_vazia(valores) -> bool:
    return all(v is None or str(v).strip() == "" for v in valores)


# ═════════════════════════════════════════════════════════════
# Arquivo
# ═════════════════════════════════════════════════════════════

def nome_do_arquivo(arquivo) -> str:
    nome = os.path.basename(str(getattr(arquivo, "name", "") or "")).strip()
    nome = re.sub(r"[\x00-\x1f]", "", nome)
    return nome[:255] or "planilha"


def ler_bytes(arquivo, max_bytes: int = MAX_BYTES) -> bytes:
    """Os bytes do upload, recusando o vazio e o que passa de `max_bytes` (lê um a mais, para saber)."""
    if hasattr(arquivo, "seek"):
        arquivo.seek(0)
    dados = arquivo.read(max_bytes + 1)
    if isinstance(dados, str):
        dados = dados.encode("utf-8")
    if len(dados) > max_bytes:
        raise ValidationError(f"O arquivo passa do limite de {max_bytes // (1024 * 1024)} MB.")
    if not dados:
        raise ValidationError("O arquivo está vazio.")
    return dados


def _para_formato_comum(dados: bytes) -> bytes:
    """Reescreve um .xlsx Strict nos namespaces comuns; o que já é comum volta como veio."""
    with zipfile.ZipFile(io.BytesIO(dados)) as origem:
        try:
            principal = origem.read("xl/workbook.xml")
        except KeyError:
            return dados
        if _MARCA_STRICT not in principal:
            return dados
        saida = io.BytesIO()
        with zipfile.ZipFile(saida, "w", zipfile.ZIP_DEFLATED) as destino:
            for item in origem.infolist():
                conteudo = origem.read(item.filename)
                if item.filename.endswith((".xml", ".rels")):
                    for velho, novo in _STRICT_PARA_COMUM:
                        conteudo = conteudo.replace(velho, novo)
                destino.writestr(item.filename, conteudo)
    return saida.getvalue()


def linhas_do_xlsx(dados: bytes, *, max_descompactado: int = MAX_DESCOMPACTADO, aba_preferida: str | None = None) -> list:
    """`[(numero_da_linha, [valores…])]` da aba `aba_preferida` (se existir) ou da primeira, com o
    número da linha da PLANILHA. Os valores são os do `openpyxl`: texto, número, `date`/`datetime`
    ou `None`. Levanta `ValidationError` para o que não é um .xlsx legível, é grande demais depois
    de aberto ou não tem aba nenhuma."""
    from openpyxl import load_workbook

    ilegivel = "Não foi possível ler o arquivo — envie uma planilha .xlsx válida."
    if dados[:2] != b"PK":
        raise ValidationError(ilegivel)
    try:
        with zipfile.ZipFile(io.BytesIO(dados)) as zipado:
            if sum(i.file_size for i in zipado.infolist()) > max_descompactado:
                raise ValidationError("O arquivo é grande demais depois de aberto. Envie uma planilha menor.")
        wb = load_workbook(io.BytesIO(_para_formato_comum(dados)), read_only=True, data_only=True)
    except ValidationError:
        raise
    except Exception:
        raise ValidationError(ilegivel) from None
    try:
        if not wb.worksheets:
            raise ValidationError(ilegivel)
        ws = wb[aba_preferida] if aba_preferida and aba_preferida in wb.sheetnames else wb.worksheets[0]
        ws.reset_dimensions()
        return [(i, list(valores)) for i, valores in enumerate(ws.iter_rows(values_only=True), start=1)]
    except ValidationError:
        raise
    except Exception:
        raise ValidationError(ilegivel) from None
    finally:
        wb.close()


def decodificar(dados: bytes) -> str:
    for codificacao in ("utf-8-sig", "cp1252"):
        try:
            return dados.decode(codificacao)
        except UnicodeDecodeError:
            continue
    raise ValidationError("Não foi possível ler o texto do arquivo. Salve-o como CSV (UTF-8) e envie de novo.")


def separador(texto: str) -> str:
    primeira = next((linha for linha in texto.splitlines() if linha.strip()), "")
    contagem = {sep: primeira.count(sep) for sep in (";", ",", "\t")}
    melhor = max(contagem, key=contagem.get)
    return melhor if contagem[melhor] else ";"


def linhas_do_csv(dados: bytes) -> list:
    """`[(numero_da_linha, [valores…])]` de um CSV em UTF-8 (com ou sem BOM) ou cp1252, com
    separador `;`, `,` ou tab."""
    if dados[:2] == b"PK":
        raise ValidationError("Este arquivo é uma planilha do Excel com extensão .csv. Envie-o como .xlsx.")
    texto = decodificar(dados)
    try:
        leitor = csv.reader(io.StringIO(texto, newline=""), delimiter=separador(texto))
        return [(i, list(valores)) for i, valores in enumerate(leitor, start=1)]
    except csv.Error:
        raise ValidationError("Não foi possível ler o CSV: confira as aspas e os separadores.") from None
