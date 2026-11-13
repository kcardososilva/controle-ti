"""
Importação de chips por planilha (.xlsx ou .csv): modelo, prévia, confirmação e relatório.

Molde: `licenca_office_import_service.py` (openpyxl, sem pandas) — com três diferenças que
são o ponto desta importação:

  • NUNCA ATUALIZA. Chip que já existe é contado como `existente` e fica como está: reimportar
    a mesma planilha não pode sobrescrever ajuste que o TI fez à mão (o upsert da planilha de
    licenças faz isso de propósito; aqui seria destrutivo).
  • DUAS ETAPAS, COM REVALIDAÇÃO. `analisar` só lê e valida (nada é gravado nos chips);
    `confirmar` revalida CADA linha antes de gravar — outra pessoa pode ter cadastrado o chip
    no meio-tempo — e grava cada uma num `atomic()` próprio: uma linha com erro não derruba as
    outras. A prévia vale 24 h e só confirma uma vez.
  • PLANILHA É ENTRADA HOSTIL. Limites de 5 MB e 5.000 linhas, recusa de zip inflado, só a
    primeira aba de dados, e NADA é executado: valores que começam com `=`, `+`, `-` ou `@`
    são tratados como TEXTO (e avisados), e o Excel de relatório os escapa (apóstrofo na
    frente, célula de tipo texto) para que abrir o relatório não rode fórmula nenhuma.

Contratos de leitura para as telas (Fase E): `modelo_xlsx`, `modelo_csv`, `analisar`,
`confirmar`, `cancelar`, `relatorio`, `exportar_relatorio_xlsx`, `listar_importacoes`,
`linhas_da_importacao`, `linha_do_relatorio`, `arquivo_ja_importado`.
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models.functions import Lower
from django.utils import timezone

from ProjetoEstoque.models import (
    ConectividadeOperadora,
    FrotaImportacaoChips,
    FrotaImportacaoLinha,
    Localidade,
    M2MChip,
)
from services import m2m_monitoramento_service as m2m
from services import tim_m2m_service as tim

MAX_BYTES = 5 * 1024 * 1024
MAX_LINHAS = 5000
MAX_DESCOMPACTADO = 60 * 1024 * 1024      # teto do .xlsx já aberto (zip inflado)
LOTE_IN = 400                              # `__in` em pedaços, longe do limite de variáveis do SQLite
PREFIXOS_DE_FORMULA = ("=", "+", "-", "@")

Estado = FrotaImportacaoChips.Estado
Situacao = FrotaImportacaoLinha.Situacao

# (campo, rótulo do modelo, aliases normalizados do cabeçalho)
COLUNAS = (
    ("iccid", "ICCID", ("iccid",)),
    ("imsi", "IMSI", ("imsi",)),
    ("numero_linha", "Número da linha", ("numero da linha", "numero linha", "numero", "linha", "telefone", "msisdn")),
    ("operadora", "Operadora", ("operadora",)),
    ("identificacao", "Identificação", ("identificacao", "nome", "nome do chip", "chip")),
    ("descricao", "Descrição", ("descricao",)),
    ("unidade", "Unidade", ("unidade", "fazenda", "unidade fazenda", "localidade")),
    ("observacoes", "Observações", ("observacoes", "observacao", "obs")),
)
ROTULOS = [rotulo for _, rotulo, _ in COLUNAS]
_CAMPO_POR_CABECALHO = {alias: campo for campo, _, aliases in COLUNAS for alias in aliases}
CABECALHO_ESPERADO = ", ".join(ROTULOS)


def _norm(texto) -> str:
    """minúsculas, sem acento, espaços colapsados — casa cabeçalhos, operadoras e unidades
    sem depender de digitação idêntica."""
    t = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode()
    return " ".join(t.strip().lower().split())


def _texto_seguro(valor) -> str:
    """Texto para o relatório Excel: o que começa com `=`, `+`, `-`, `@` (ou tab/CR, que
    algumas versões também interpretam) ganha um apóstrofo na frente — injeção de fórmula."""
    texto = "" if valor is None else str(valor)
    if texto[:1] in PREFIXOS_DE_FORMULA or texto[:1] in ("\t", "\r"):
        return "'" + texto
    return texto


# ═════════════════════════════════════════════════════════════
# Arquivos-modelo
# ═════════════════════════════════════════════════════════════

INSTRUCOES = (
    "Como preencher",
    "",
    "1. Preencha a aba \"Chips\", uma linha por chip. Não apague nem renomeie a linha de cabeçalho.",
    "2. Pelo menos UM dos dois, ICCID ou IMSI, é obrigatório em cada linha.",
    "3. ICCID: 19 ou 20 dígitos, começando por 89. IMSI: 15 dígitos. Formate as colunas ICCID, IMSI e",
    "   Número da linha como TEXTO (já vem assim neste modelo): o Excel arredonda número comprido.",
    "4. Número da linha: com DDD, de 10 a 15 dígitos. Não é único — número repetido gera só um aviso.",
    "5. Operadora: escolha na lista. Em branco vale TIM. Operadora que não existe ou está inativa rejeita a linha.",
    "6. Identificação: o nome do chip. Em branco vira \"Chip\" + os 6 últimos dígitos do ICCID (ou IMSI).",
    "7. Unidade: o nome da unidade/fazenda como cadastrado (veja a aba \"Listas\"). Unidade que não",
    "   existe rejeita a linha — o sistema não inventa unidade.",
    "8. Descrição e Observações são livres.",
    "",
    "O que a importação NÃO faz",
    "",
    "• Não atualiza chip que já existe: ele é contado como \"já cadastrado\" e fica como está.",
    "• Não vincula o chip a veículo: isso é feito depois, no cadastro do veículo.",
    "• Não executa fórmulas: texto que começa com =, +, - ou @ é tratado como texto.",
    "",
    "Limites: arquivo de até 5 MB e 5.000 linhas. Antes de gravar, o sistema mostra uma prévia",
    "com o que será importado, o que já existe e o que foi rejeitado (e por quê).",
)


def modelo_xlsx() -> bytes:
    """O arquivo-modelo em Excel: aba "Chips" (só o cabeçalho, com as colunas ICCID, IMSI e
    Número da linha formatadas como TEXTO e a lista de operadoras e de unidades como validação),
    aba "Instruções" e aba "Listas" (operadoras ativas e unidades). Devolve os bytes do .xlsx."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation

    wb = Workbook()
    ws = wb.active
    ws.title = "Chips"
    ws.append(ROTULOS)
    for coluna, largura in zip("ABCDEFGH", (24, 18, 18, 16, 28, 30, 24, 36)):
        ws.column_dimensions[coluna].width = largura
    for celula in ws[1]:
        celula.font = Font(bold=True, color="FFFFFF")
        celula.fill = PatternFill("solid", fgColor="1F4E78")
        celula.alignment = Alignment(vertical="center")
    for linha in range(2, 502):                         # as 3 colunas numéricas como TEXTO
        for coluna in "ABC":
            ws[f"{coluna}{linha}"].number_format = "@"
    ws.freeze_panes = "A2"

    listas = wb.create_sheet("Listas")
    listas.append(["Operadoras (ativas)", "Unidades"])
    operadoras = list(ConectividadeOperadora.objects.filter(ativo=True).order_by("nome").values_list("nome", flat=True))
    unidades = list(Localidade.objects.order_by("local").values_list("local", flat=True))
    for i in range(max(len(operadoras), len(unidades))):
        listas.append([operadoras[i] if i < len(operadoras) else None, unidades[i] if i < len(unidades) else None])
    for linha in listas.iter_rows(min_row=2):                     # nome é texto, nunca fórmula
        for celula in linha:
            if celula.value is not None:
                celula.data_type = "s"
    listas.column_dimensions["A"].width = 28
    listas.column_dimensions["B"].width = 36
    if operadoras:
        validacao = DataValidation(type="list", formula1=f"Listas!$A$2:$A${len(operadoras) + 1}", allow_blank=True)
        validacao.error, validacao.errorTitle = "Escolha uma operadora da lista.", "Operadora"
        ws.add_data_validation(validacao)
        validacao.add("D2:D501")
    if unidades:
        validacao = DataValidation(type="list", formula1=f"Listas!$B$2:$B${len(unidades) + 1}", allow_blank=True)
        validacao.error, validacao.errorTitle = "Escolha uma unidade da lista.", "Unidade"
        ws.add_data_validation(validacao)
        validacao.add("G2:G501")

    ajuda = wb.create_sheet("Instruções")
    for linha in INSTRUCOES:
        ajuda.append([linha])
    ajuda.column_dimensions["A"].width = 110
    ajuda["A1"].font = Font(bold=True, size=13)

    wb.move_sheet("Instruções", offset=-1)              # Chips, Instruções, Listas
    wb.active = 0
    saida = io.BytesIO()
    wb.save(saida)
    return saida.getvalue()


def modelo_csv() -> bytes:
    """O modelo em CSV: UTF-8 com BOM (o Excel brasileiro abre acentuação certa) e
    separador `;` (o do Excel em português). Só o cabeçalho."""
    saida = io.StringIO(newline="")
    csv.writer(saida, delimiter=";", lineterminator="\r\n").writerow(ROTULOS)
    return saida.getvalue().encode("utf-8-sig")


# ═════════════════════════════════════════════════════════════
# Leitura do arquivo
# ═════════════════════════════════════════════════════════════

@dataclass
class _Arquivo:
    nome: str
    formato: str
    sha256: str
    linhas: list                    # [(numero_da_linha, [valores...])], na ordem, sem as vazias
    colunas: dict = field(default_factory=dict)     # campo -> índice


def _nome_do_arquivo(arquivo) -> str:
    nome = os.path.basename(str(getattr(arquivo, "name", "") or "")).strip()
    nome = re.sub(r"[\x00-\x1f]", "", nome)
    return nome[:255] or "planilha"


def _ler_bytes(arquivo) -> bytes:
    if hasattr(arquivo, "seek"):
        arquivo.seek(0)
    dados = arquivo.read(MAX_BYTES + 1)
    if isinstance(dados, str):
        dados = dados.encode("utf-8")
    if len(dados) > MAX_BYTES:
        raise ValidationError(f"O arquivo passa do limite de {MAX_BYTES // (1024 * 1024)} MB.")
    if not dados:
        raise ValidationError("O arquivo está vazio.")
    return dados


def _linhas_do_xlsx(dados: bytes) -> list:
    from openpyxl import load_workbook

    if dados[:2] != b"PK":
        raise ValidationError("Não foi possível ler o arquivo — envie uma planilha .xlsx válida.")
    try:
        with zipfile.ZipFile(io.BytesIO(dados)) as zipado:
            if sum(i.file_size for i in zipado.infolist()) > MAX_DESCOMPACTADO:
                raise ValidationError("O arquivo é grande demais depois de aberto. Envie uma planilha menor.")
        wb = load_workbook(io.BytesIO(dados), read_only=True, data_only=True)
    except ValidationError:
        raise
    except Exception:
        raise ValidationError("Não foi possível ler o arquivo — envie uma planilha .xlsx válida.") from None
    try:
        ws = wb["Chips"] if "Chips" in wb.sheetnames else wb.worksheets[0]
        ws.reset_dimensions()
        return [(i, list(valores)) for i, valores in enumerate(ws.iter_rows(values_only=True), start=1)]
    except Exception:
        raise ValidationError("Não foi possível ler o arquivo — envie uma planilha .xlsx válida.") from None
    finally:
        wb.close()


def _decodificar(dados: bytes) -> str:
    for codificacao in ("utf-8-sig", "cp1252"):
        try:
            return dados.decode(codificacao)
        except UnicodeDecodeError:
            continue
    raise ValidationError("Não foi possível ler o texto do arquivo. Salve-o como CSV (UTF-8) e envie de novo.")


def _separador(texto: str) -> str:
    primeira = next((linha for linha in texto.splitlines() if linha.strip()), "")
    contagem = {sep: primeira.count(sep) for sep in (";", ",", "\t")}
    melhor = max(contagem, key=contagem.get)
    return melhor if contagem[melhor] else ";"


def _linhas_do_csv(dados: bytes) -> list:
    if dados[:2] == b"PK":
        raise ValidationError("Este arquivo é uma planilha do Excel com extensão .csv. Envie-o como .xlsx.")
    texto = _decodificar(dados)
    try:
        leitor = csv.reader(io.StringIO(texto, newline=""), delimiter=_separador(texto))
        return [(i, list(valores)) for i, valores in enumerate(leitor, start=1)]
    except csv.Error:
        raise ValidationError("Não foi possível ler o CSV: confira as aspas e os separadores.") from None


def _vazia(valores) -> bool:
    return all(v is None or str(v).strip() == "" for v in valores)


def _carregar(arquivo) -> _Arquivo:
    nome = _nome_do_arquivo(arquivo)
    extensao = os.path.splitext(nome)[1].lower()
    if extensao not in (".xlsx", ".csv"):
        raise ValidationError("Envie uma planilha .xlsx ou um arquivo .csv.")
    dados = _ler_bytes(arquivo)
    linhas = _linhas_do_xlsx(dados) if extensao == ".xlsx" else _linhas_do_csv(dados)
    linhas = [(n, v) for n, v in linhas if not _vazia(v)]
    if not linhas:
        raise ValidationError("A planilha está vazia.")

    numero_cabecalho, cabecalho = linhas[0]
    colunas: dict[str, int] = {}
    for indice, celula in enumerate(cabecalho):
        campo = _CAMPO_POR_CABECALHO.get(_norm(celula))
        if campo and campo not in colunas:
            colunas[campo] = indice
    if "iccid" not in colunas and "imsi" not in colunas:
        raise ValidationError(
            "Cabeçalho não reconhecido: a planilha precisa ter pelo menos a coluna ICCID ou IMSI na primeira "
            f"linha. Colunas esperadas: {CABECALHO_ESPERADO}. Use o arquivo-modelo."
        )
    dados_das_linhas = linhas[1:]
    if len(dados_das_linhas) > MAX_LINHAS:
        raise ValidationError(f"A planilha tem mais de {MAX_LINHAS:,} linhas de dados.".replace(",", "."))
    if not dados_das_linhas:
        raise ValidationError("A planilha só tem o cabeçalho: nenhuma linha de dados.")
    return _Arquivo(
        nome=nome, formato="xlsx" if extensao == ".xlsx" else "csv", sha256=hashlib.sha256(dados).hexdigest(),
        linhas=dados_das_linhas, colunas=colunas,
    )


# ═════════════════════════════════════════════════════════════
# Validação (uma linha)
# ═════════════════════════════════════════════════════════════

def _celula(valor):
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


def _bruto(valores, colunas, campo) -> str | None:
    indice = colunas.get(campo)
    if indice is None or indice >= len(valores):
        return ""
    return _celula(valores[indice])


def _perdeu_digitos(rotulo: str) -> str:
    return (
        f"O {rotulo} veio como número do Excel e perdeu dígitos. Formate a coluna como Texto e digite de novo "
        "(o arquivo-modelo já vem assim)."
    )


@dataclass
class _Contexto:
    """O que as linhas precisam do cadastro: operadoras e unidades por nome (uma vez só)."""
    operadoras: dict
    unidades: dict
    operadora_padrao: ConectividadeOperadora | None

    @classmethod
    def carregar(cls) -> "_Contexto":
        operadoras = {_norm(o.nome): o for o in ConectividadeOperadora.objects.all()}
        unidades: dict = {}
        for u in Localidade.objects.all():
            unidades.setdefault(_norm(u.local), []).append(u)
        return cls(operadoras, unidades, m2m.operadora_padrao())


def _normalizar_linha(valores, colunas, contexto: _Contexto) -> tuple[dict, list, list]:
    """Etapa A: lê e normaliza as células. Devolve `(dados, motivos, avisos)`; `dados` leva os
    valores normalizados mais `bruto` (o que veio na planilha, para o relatório)."""
    motivos: list[str] = []
    avisos: list[str] = []
    bruto = {campo: _bruto(valores, colunas, campo) for campo, _, _ in COLUNAS}
    dados: dict = {
        "iccid": "", "imsi": "", "numero_linha": "", "operadora": "", "operadora_id": None, "identificacao": "",
        "identificacao_gerada": False, "descricao": "", "unidade": "", "unidade_id": None, "observacoes": "",
        "bruto": {c: ("" if v is None else v) for c, v in bruto.items() if c in ("iccid", "imsi", "numero_linha")},
    }

    for campo, rotulo, normalizar in (
        ("iccid", "ICCID", tim.normalizar_iccid), ("imsi", "IMSI", tim.normalizar_imsi),
        ("numero_linha", "número da linha", tim.normalizar_numero_linha),
    ):
        valor = bruto[campo]
        if valor is None:
            motivos.append(_perdeu_digitos(rotulo))
        elif valor:
            try:
                dados[campo] = normalizar(valor)
            except ValidationError as exc:
                motivos.append(exc.messages[0])
    if not bruto["iccid"] and not bruto["imsi"]:                 # `None` = veio número do Excel, já tem motivo
        motivos.append("Informe o ICCID ou o IMSI.")

    for campo, rotulo, maximo in (
        ("identificacao", "Identificação", 120), ("descricao", "Descrição", 255), ("observacoes", "Observações", 2000),
    ):
        texto = bruto[campo] or ""
        if len(texto) > maximo:
            motivos.append(f"{rotulo} aceita até {maximo} caracteres.")
        dados[campo] = texto
    for campo, rotulo in (("identificacao", "Identificação"), ("descricao", "Descrição"), ("observacoes", "Observações"),
                          ("unidade", "Unidade"), ("operadora", "Operadora")):
        texto = bruto[campo] or ""
        if texto[:1] in PREFIXOS_DE_FORMULA:
            avisos.append(f"{rotulo} começa com \"{texto[:1]}\": foi tratado como texto, nada é executado.")

    # operadora
    nome_operadora = bruto["operadora"] or ""
    if nome_operadora:
        operadora = contexto.operadoras.get(_norm(nome_operadora))
        if operadora is None:
            motivos.append(f"Operadora \"{nome_operadora}\" não está cadastrada.")
        elif not operadora.ativo:
            motivos.append(f"A operadora \"{operadora.nome}\" está inativa.")
        else:
            dados["operadora"], dados["operadora_id"] = operadora.nome, operadora.pk
    else:
        padrao = contexto.operadora_padrao
        if padrao is None:
            motivos.append("Não há operadora ativa cadastrada.")
        else:
            dados["operadora"], dados["operadora_id"] = padrao.nome, padrao.pk

    # unidade: se foi preenchida e não existe, a linha é rejeitada (nada é inventado)
    nome_unidade = bruto["unidade"] or ""
    if nome_unidade:
        achadas = contexto.unidades.get(_norm(nome_unidade), [])
        if not achadas:
            motivos.append(f"Unidade \"{nome_unidade}\" não está cadastrada.")
        elif len(achadas) > 1:
            motivos.append(f"Há mais de uma unidade chamada \"{nome_unidade}\" no cadastro; não dá para escolher uma.")
        else:
            dados["unidade"], dados["unidade_id"] = achadas[0].local, achadas[0].pk

    # identificação vazia vira "Chip <6 últimos dígitos>"
    if not dados["identificacao"]:
        base = dados["iccid"] or dados["imsi"]
        if base:
            dados["identificacao"], dados["identificacao_gerada"] = f"Chip {base[-6:]}", True
    return dados, motivos, avisos


class _Banco:
    """Etapa B: o que o cadastro de chips já tem, carregado em poucas consultas para o
    conjunto de linhas dado."""

    def __init__(self, por_iccid, por_imsi, nomes, linhas_em_uso):
        self.por_iccid, self.por_imsi, self.nomes, self.linhas_em_uso = por_iccid, por_imsi, nomes, linhas_em_uso

    @classmethod
    def carregar(cls, lista_de_dados: list) -> "_Banco":
        iccids = sorted({d["iccid"] for d in lista_de_dados if d["iccid"]})
        imsis = sorted({d["imsi"] for d in lista_de_dados if d["imsi"]})
        nomes = sorted({d["identificacao"] for d in lista_de_dados if d["identificacao"]})
        numeros = sorted({d["numero_linha"] for d in lista_de_dados if d["numero_linha"]})
        por_iccid: dict = {}
        por_imsi: dict = {}
        nomes_em_uso: dict = {}
        linhas_em_uso: dict = {}

        def pedacos(valores):
            for i in range(0, len(valores), LOTE_IN):
                yield valores[i:i + LOTE_IN]

        for p in pedacos(iccids):
            for chip in M2MChip.objects.filter(iccid__in=p):
                por_iccid[chip.iccid] = chip
        for p in pedacos(imsis):
            for chip in M2MChip.objects.filter(imsi__in=p):
                por_imsi[chip.imsi] = chip
        minusculos = sorted({n.lower() for n in nomes})
        for p in pedacos(minusculos):
            for chip in M2MChip.objects.annotate(nome_minusculo=Lower("nome")).filter(nome_minusculo__in=p):
                nomes_em_uso[chip.nome.lower()] = chip
        for p in pedacos(numeros):
            for chip in M2MChip.objects.filter(numero_linha__in=p):
                linhas_em_uso.setdefault(chip.numero_linha, []).append(chip)
        return cls(por_iccid, por_imsi, nomes_em_uso, linhas_em_uso)

    def checar(self, dados: dict) -> tuple[str, list, list, M2MChip | None]:
        """`(situacao, motivos, avisos, chip_existente)`: `existente` (já cadastrado, ignorado),
        `rejeitada` (conflito ou nome repetido) ou `valida`."""
        motivos: list[str] = []
        avisos: list[str] = []
        iccid, imsi = dados["iccid"], dados["imsi"]
        a = self.por_iccid.get(iccid) if iccid else None
        b = self.por_imsi.get(imsi) if imsi else None
        existente = None
        if a is not None and b is not None and a.pk != b.pk:
            motivos.append(
                f"Conflito: o ICCID pertence ao chip \"{a.nome}\" e o IMSI ao chip \"{b.nome}\". "
                "Corrija a planilha ou o cadastro; nada foi alterado."
            )
        elif a is not None or b is not None:
            existente = a or b
            if a is not None and imsi and a.imsi and a.imsi != imsi:
                motivos.append(
                    f"Conflito: o chip \"{a.nome}\" já está cadastrado com outro IMSI (cadastrado: {a.imsi}; planilha: {imsi})."
                )
            if b is not None and iccid and b.iccid and b.iccid != iccid:
                motivos.append(
                    f"Conflito: o chip \"{b.nome}\" já está cadastrado com outro ICCID (cadastrado: {b.iccid}; planilha: {iccid})."
                )
        elif dados["identificacao"] and dados["identificacao"].lower() in self.nomes:
            motivos.append(f"Já existe um chip com a identificação \"{dados['identificacao']}\".")
        if dados["numero_linha"]:
            outros = [c for c in self.linhas_em_uso.get(dados["numero_linha"], []) if existente is None or c.pk != existente.pk]
            if outros:
                avisos.append(
                    f"O número da linha {dados['numero_linha']} já está em uso pelo chip \"{outros[0].nome}\""
                    + (f" (e mais {len(outros) - 1})" if len(outros) > 1 else "") + "."
                )
        if motivos:
            return Situacao.REJEITADA, motivos, avisos, None
        if existente is not None:
            return Situacao.EXISTENTE, ["Chip já cadastrado — não é alterado pela importação."], avisos, existente
        return Situacao.VALIDA, [], avisos, None


# ═════════════════════════════════════════════════════════════
# Prévia
# ═════════════════════════════════════════════════════════════

def analisar(arquivo, usuario) -> FrotaImportacaoChips:
    """Lê e VALIDA a planilha — nada é gravado nos chips. Devolve a `FrotaImportacaoChips` em
    estado `previa` (válida por 24 h) com uma `FrotaImportacaoLinha` por linha de dados.

    `arquivo`: o upload (`.name` e `.read()`), .xlsx ou .csv de até 5 MB e 5.000 linhas; o CSV
    aceita UTF-8 (com ou sem BOM) ou cp1252 e separador `;`, `,` ou tab. O cabeçalho é
    reconhecido sem distinção de acento e caixa; sem a coluna ICCID ou IMSI a leitura é recusada
    com a lista das colunas esperadas.

    Por linha (situação, `motivos` que rejeitam, `avisos` que só informam):
      • rejeitada — nem ICCID nem IMSI; ICCID/IMSI/número inválido; operadora inexistente ou
        inativa; unidade preenchida que não existe; identificação repetida (no arquivo ou no
        cadastro); ICCID ou IMSI repetido no arquivo; CONFLITO (o ICCID é de um chip e o IMSI de
        outro, ou o chip existe com IMSI/ICCID diferente) — os dois chips são citados;
      • existente — o chip já está cadastrado (nunca é atualizado);
      • valida — será importada;
      • aviso — número da linha repetido (no arquivo ou no cadastro); texto que começa com
        `=`, `+`, `-` ou `@`.
    Identificação vazia vira "Chip" + os 6 últimos dígitos do ICCID (ou IMSI); operadora vazia
    vale a padrão (TIM). Levanta `ValidationError` (arquivo ilegível, grande demais, cabeçalho
    inválido, vazio)."""
    carregado = _carregar(arquivo)
    contexto = _Contexto.carregar()
    analisadas = []
    for numero, valores in carregado.linhas:
        dados, motivos, avisos = _normalizar_linha(valores, carregado.colunas, contexto)
        analisadas.append([numero, dados, motivos, avisos])
    banco = _Banco.carregar([d for _, d, _, _ in analisadas])

    vistos = {"iccid": {}, "imsi": {}, "nome": {}, "linha": {}}
    resultado = []
    for numero, dados, motivos, avisos in analisadas:
        situacao, chip = Situacao.REJEITADA, None
        if not motivos:
            situacao, m, a, chip = banco.checar(dados)
            motivos, avisos = motivos + m, avisos + a
        # repetição dentro do próprio arquivo (só contra linhas anteriores que não foram rejeitadas)
        if situacao != Situacao.REJEITADA:
            repetidos = []
            for campo, rotulo in (("iccid", "ICCID"), ("imsi", "IMSI")):
                if dados[campo] and dados[campo] in vistos[campo]:
                    repetidos.append(f"{rotulo} repetido: já aparece na linha {vistos[campo][dados[campo]]}.")
            if situacao == Situacao.VALIDA and dados["identificacao"].lower() in vistos["nome"]:
                repetidos.append(
                    f"Identificação \"{dados['identificacao']}\" repetida: já aparece na linha "
                    f"{vistos['nome'][dados['identificacao'].lower()]}."
                )
            if repetidos:
                motivos, situacao = motivos + repetidos, Situacao.REJEITADA
        if situacao != Situacao.REJEITADA:
            for campo in ("iccid", "imsi"):
                if dados[campo]:
                    vistos[campo].setdefault(dados[campo], numero)
            if dados["identificacao"] and situacao == Situacao.VALIDA:
                vistos["nome"].setdefault(dados["identificacao"].lower(), numero)
        if dados["numero_linha"]:
            if dados["numero_linha"] in vistos["linha"] and situacao != Situacao.REJEITADA:
                avisos.append(
                    f"O número da linha {dados['numero_linha']} se repete: já aparece na linha {vistos['linha'][dados['numero_linha']]}."
                )
            if situacao != Situacao.REJEITADA:
                vistos["linha"].setdefault(dados["numero_linha"], numero)
        resultado.append((numero, dados, situacao, motivos, avisos, chip))

    with transaction.atomic():
        importacao = FrotaImportacaoChips.objects.create(
            arquivo_nome=carregado.nome, sha256=carregado.sha256, formato=carregado.formato,
            estado=Estado.PREVIA, criada_por=usuario if getattr(usuario, "pk", None) else None,
        )
        FrotaImportacaoLinha.objects.bulk_create([
            FrotaImportacaoLinha(
                importacao=importacao, numero=numero, dados=dados, situacao=situacao,
                motivos=motivos, avisos=avisos, chip=chip,
            )
            for numero, dados, situacao, motivos, avisos, chip in resultado
        ])
        _gravar_totais(importacao)
    return importacao


def _gravar_totais(importacao: FrotaImportacaoChips) -> FrotaImportacaoChips:
    """Recalcula os totais A PARTIR DAS LINHAS (a fonte da verdade) e grava."""
    linhas = importacao.itens.all()
    contagem = {s: 0 for s in Situacao.values}
    avisos = 0
    for situacao, tem_aviso in linhas.values_list("situacao", "avisos"):
        contagem[situacao] += 1
        avisos += bool(tem_aviso)
    importacao.linhas = sum(contagem.values())
    importacao.validas = contagem[Situacao.VALIDA]
    importacao.importados = contagem[Situacao.IMPORTADA]
    importacao.existentes = contagem[Situacao.EXISTENTE]
    importacao.rejeitados = contagem[Situacao.REJEITADA]
    importacao.avisos = avisos
    importacao.save(update_fields=["linhas", "validas", "importados", "existentes", "rejeitados", "avisos"])
    return importacao


# ═════════════════════════════════════════════════════════════
# Confirmação
# ═════════════════════════════════════════════════════════════

def confirmar(importacao: FrotaImportacaoChips, usuario) -> FrotaImportacaoChips:
    """Grava os chips da prévia. Recusa (`ValidationError`) prévia já confirmada ou cancelada
    e prévia EXPIRADA (24 h) — ninguém confirma duas vezes, nem uma prévia velha.

    REVALIDA cada linha `valida` contra o cadastro de agora (outra pessoa pode ter cadastrado o
    chip, o nome ou a operadora mudar) e grava cada uma num `atomic()` próprio: a linha que não
    passa vira `existente` ou `rejeitada` (com o motivo da revalidação) e as demais seguem. NUNCA
    atualiza chip existente. Cada chip entra por `m2m.criar_chip` (que registra a ENTRADA e o
    evento). Ao fim recalcula os totais das linhas e devolve a importação (`confirmada`).

    `usuario` é obrigatório. A importação é reservada antes de começar (atualização condicional
    `estado=previa`): dois cliques simultâneos não importam duas vezes."""
    if usuario is None or not getattr(usuario, "pk", None):
        raise ValidationError("Informe quem está confirmando a importação.")
    atual = FrotaImportacaoChips.objects.get(pk=importacao.pk)
    if atual.estado != Estado.PREVIA:
        raise ValidationError(f"Esta importação já foi {atual.get_estado_display().lower()}.")
    if atual.expirada:
        raise ValidationError("A prévia expirou (vale por 24 h). Envie a planilha de novo para gerar outra.")
    agora = timezone.now()
    reservou = FrotaImportacaoChips.objects.filter(pk=atual.pk, estado=Estado.PREVIA).update(
        estado=Estado.CONFIRMADA, confirmada_por=usuario, confirmada_em=agora,
    )
    if not reservou:
        raise ValidationError("Esta importação acabou de ser confirmada por outra pessoa.")

    try:
        contexto = _Contexto.carregar()
        for linha in atual.itens.filter(situacao=Situacao.VALIDA).order_by("numero"):
            _importar_linha(linha, atual, usuario, contexto)
    finally:
        _gravar_totais(atual)
    atual.refresh_from_db()
    return atual


def _importar_linha(linha: FrotaImportacaoLinha, importacao, usuario, contexto: _Contexto) -> None:
    dados = linha.dados
    banco = _Banco.carregar([dados])
    situacao, motivos, avisos, chip = banco.checar(dados)
    operadora = ConectividadeOperadora.objects.filter(pk=dados.get("operadora_id")).first()
    if situacao == Situacao.VALIDA and (operadora is None or not operadora.ativo):
        situacao, motivos = Situacao.REJEITADA, ["A operadora deixou de existir ou foi inativada desde a prévia."]
    if situacao == Situacao.VALIDA and dados.get("unidade_id") and not Localidade.objects.filter(pk=dados["unidade_id"]).exists():
        situacao, motivos = Situacao.REJEITADA, ["A unidade deixou de existir desde a prévia."]
    if situacao != Situacao.VALIDA:
        linha.situacao, linha.chip = situacao, chip
        linha.motivos = ["Revalidação ao confirmar: " + m for m in motivos]
        linha.save(update_fields=["situacao", "motivos", "chip"])
        return
    identificador = "iccid" if dados["iccid"] else "imsi"
    try:
        with transaction.atomic():
            novo = m2m.criar_chip(
                {
                    "nome": dados["identificacao"], "iccid": dados["iccid"], "imsi": dados["imsi"],
                    "identificador_consulta": identificador, "descricao": dados["descricao"],
                    "observacoes": dados["observacoes"], "operadora": dados["operadora_id"],
                    "numero_linha": dados["numero_linha"], "localidade": dados.get("unidade_id"),
                },
                usuario, importacao=importacao,
            )
    except ValidationError as exc:
        mensagens = (
            [f"{m}" for mensagens in exc.message_dict.values() for m in mensagens]
            if hasattr(exc, "error_dict") else exc.messages
        )
        linha.situacao, linha.motivos = Situacao.REJEITADA, ["Revalidação ao confirmar: " + " ".join(mensagens)]
        linha.save(update_fields=["situacao", "motivos"])
        return
    linha.situacao, linha.chip = Situacao.IMPORTADA, novo
    linha.save(update_fields=["situacao", "chip"])


def cancelar(importacao: FrotaImportacaoChips, usuario=None) -> FrotaImportacaoChips:
    """Descarta a prévia (estado `cancelada`; as linhas ficam como registro). Só vale em prévia."""
    mudou = FrotaImportacaoChips.objects.filter(pk=importacao.pk, estado=Estado.PREVIA).update(estado=Estado.CANCELADA)
    if not mudou:
        atual = FrotaImportacaoChips.objects.get(pk=importacao.pk)
        raise ValidationError(f"Esta importação já foi {atual.get_estado_display().lower()}.")
    importacao.refresh_from_db()
    return importacao


# ═════════════════════════════════════════════════════════════
# Leituras e relatório
# ═════════════════════════════════════════════════════════════

def listar_importacoes(limite: int = 50):
    """As importações mais recentes (QuerySet, com `criada_por` e `confirmada_por` carregados)."""
    return FrotaImportacaoChips.objects.select_related("criada_por", "confirmada_por").order_by("-criada_em", "-pk")[: max(1, limite)]


def linhas_da_importacao(importacao: FrotaImportacaoChips, situacao: str | None = None):
    """As linhas da importação por número, com o chip carregado; `situacao` filtra por uma delas."""
    qs = importacao.itens.select_related("chip").order_by("numero")
    return qs.filter(situacao=situacao) if situacao in Situacao.values else qs


def arquivo_ja_importado(importacao: FrotaImportacaoChips) -> FrotaImportacaoChips | None:
    """Outra importação CONFIRMADA com o mesmo arquivo (mesmo SHA-256) — para avisar
    "este arquivo já foi importado em 12/11" antes de confirmar. `None` se não houver."""
    return (
        FrotaImportacaoChips.objects.filter(sha256=importacao.sha256, estado=Estado.CONFIRMADA)
        .exclude(pk=importacao.pk).order_by("-confirmada_em").first()
    )


def _linha_do_relatorio(linha: FrotaImportacaoLinha) -> dict:
    d = linha.dados or {}
    bruto = d.get("bruto") or {}
    return {
        "numero": linha.numero, "situacao": linha.situacao, "situacao_rotulo": linha.get_situacao_display(),
        "iccid": d.get("iccid") or bruto.get("iccid", ""), "imsi": d.get("imsi") or bruto.get("imsi", ""),
        "numero_linha": d.get("numero_linha") or bruto.get("numero_linha", ""),
        "identificacao": d.get("identificacao", ""), "operadora": d.get("operadora", ""), "unidade": d.get("unidade", ""),
        "motivos": list(linha.motivos or []), "avisos": list(linha.avisos or []),
        "chip_id": linha.chip_id, "chip_nome": linha.chip.nome if linha.chip_id else "",
    }


linha_do_relatorio = _linha_do_relatorio       # público: a tela PAGINA as linhas e usa a mesma descrição do relatório


def relatorio(importacao: FrotaImportacaoChips) -> dict:
    """O relatório auditado, em dados (a tela e o Excel usam o mesmo):

        {"importacao": obj, "arquivo", "sha256", "formato", "estado", "estado_rotulo",
         "criada_por" (login), "criada_em", "confirmada_por", "confirmada_em",
         "totais": {"linhas", "validas", "importados", "existentes", "rejeitados", "avisos"},
         "linhas": [{"numero", "situacao", "situacao_rotulo", "iccid", "imsi", "numero_linha",
                     "identificacao", "operadora", "unidade", "motivos": [...], "avisos": [...],
                     "chip_id", "chip_nome"}]}

    Os totais saem das LINHAS, não de um contador que possa ter ficado para trás."""
    importacao = FrotaImportacaoChips.objects.select_related("criada_por", "confirmada_por").get(pk=importacao.pk)
    linhas = [_linha_do_relatorio(l) for l in linhas_da_importacao(importacao)]
    contagem = {s: sum(1 for l in linhas if l["situacao"] == s) for s in Situacao.values}
    return {
        "importacao": importacao, "arquivo": importacao.arquivo_nome, "sha256": importacao.sha256,
        "formato": importacao.formato, "estado": importacao.estado, "estado_rotulo": importacao.get_estado_display(),
        "criada_por": importacao.criada_por.get_username() if importacao.criada_por_id else "",
        "criada_em": importacao.criada_em,
        "confirmada_por": importacao.confirmada_por.get_username() if importacao.confirmada_por_id else "",
        "confirmada_em": importacao.confirmada_em,
        "totais": {
            "linhas": len(linhas), "validas": contagem[Situacao.VALIDA], "importados": contagem[Situacao.IMPORTADA],
            "existentes": contagem[Situacao.EXISTENTE], "rejeitados": contagem[Situacao.REJEITADA],
            "avisos": sum(1 for l in linhas if l["avisos"]),
        },
        "linhas": linhas,
    }


def exportar_relatorio_xlsx(importacao: FrotaImportacaoChips) -> bytes:
    """O relatório em Excel: aba "Resumo" (arquivo, SHA-256, estado, quem e quando criou e
    confirmou, totais) e aba "Linhas" (linha a linha: situação, dados, motivos e avisos, chip
    criado). TODA célula de texto passa por `_texto_seguro` e é gravada como TEXTO: valor que
    começa com `=`, `+`, `-` ou `@` não vira fórmula (injeção). Devolve os bytes do .xlsx."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    dados = relatorio(importacao)
    wb = Workbook()
    resumo = wb.active
    resumo.title = "Resumo"

    def texto(planilha, linha, coluna, valor):
        celula = planilha.cell(row=linha, column=coluna)
        celula.value = _texto_seguro(valor)
        celula.data_type = "s"
        return celula

    def quando(momento):
        return timezone.localtime(momento).strftime("%d/%m/%Y %H:%M") if momento else ""

    itens = [
        ("Arquivo", dados["arquivo"]), ("SHA-256", dados["sha256"]), ("Formato", dados["formato"]),
        ("Estado", dados["estado_rotulo"]), ("Prévia criada por", dados["criada_por"]),
        ("Prévia criada em", quando(dados["criada_em"])), ("Confirmada por", dados["confirmada_por"]),
        ("Confirmada em", quando(dados["confirmada_em"])), ("Gerado em", quando(timezone.now())),
        ("", ""), ("Linhas", dados["totais"]["linhas"]), ("Importadas", dados["totais"]["importados"]),
        ("Já cadastradas", dados["totais"]["existentes"]), ("Rejeitadas", dados["totais"]["rejeitados"]),
        ("Válidas (ainda não importadas)", dados["totais"]["validas"]), ("Linhas com aviso", dados["totais"]["avisos"]),
    ]
    for i, (rotulo, valor) in enumerate(itens, start=1):
        texto(resumo, i, 1, rotulo).font = Font(bold=True)
        if isinstance(valor, int):
            resumo.cell(row=i, column=2, value=valor)
        else:
            texto(resumo, i, 2, valor)
    resumo.column_dimensions["A"].width = 30
    resumo.column_dimensions["B"].width = 70

    ws = wb.create_sheet("Linhas")
    cabecalho = ["Linha", "Situação", "ICCID", "IMSI", "Número da linha", "Identificação", "Operadora", "Unidade",
                 "Motivos", "Avisos", "Chip"]
    for coluna, titulo in enumerate(cabecalho, start=1):
        texto(ws, 1, coluna, titulo).font = Font(bold=True)
    for i, l in enumerate(dados["linhas"], start=2):
        ws.cell(row=i, column=1, value=l["numero"])
        for coluna, valor in enumerate((
            l["situacao_rotulo"], l["iccid"], l["imsi"], l["numero_linha"], l["identificacao"], l["operadora"],
            l["unidade"], " | ".join(l["motivos"]), " | ".join(l["avisos"]), l["chip_nome"],
        ), start=2):
            texto(ws, i, coluna, valor)
    for coluna, largura in zip("ABCDEFGHIJK", (8, 14, 24, 18, 18, 28, 14, 22, 60, 50, 28)):
        ws.column_dimensions[coluna].width = largura
    ws.freeze_panes = "A2"
    saida = io.BytesIO()
    wb.save(saida)
    return saida.getvalue()
