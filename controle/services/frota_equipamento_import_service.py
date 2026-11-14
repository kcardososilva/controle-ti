"""
Importação da planilha de EQUIPAMENTOS MOTORIZADOS (a do ERP) para Frotas e Maquinários.

Molde: `frota_import_service.py` (a de chips) — mesma disciplina de prévia → confirmação → relatório,
mesma leitura de planilha (`planilha_leitura.py`, que também lê o Excel "Strict" em que esta planilha
chega) — com as diferenças que são o ponto desta:

  • O ASSISTENTE TEM SEIS PASSOS e o serviço entrega um por função: Envio (`enviar`), Mapeamento
    (`painel_do_mapeamento` + `aplicar_mapeamento`), Pré-visualização e Validação (`resumo`,
    `linhas_da_importacao`, `conferencias_agrupadas`), Confirmação (`confirmar`) e Resultado
    (`relatorio`, `exportar_relatorio_xlsx`).
  • A PRÉVIA GUARDA AS LINHAS BRUTAS. Cada linha leva, em `dados["celulas"]`, o que veio nas células;
    o mapeamento (qual coluna é qual campo, o que "Em Uso" vira, de que natureza é cada empresa, em que
    categoria cada classe cai) é aplicado SOBRE elas, e `aplicar_mapeamento` reanalisa. Por isso "voltar
    e corrigir o mapeamento" não pede para enviar o arquivo de novo — e a decisão de quem conferiu é
    o que a confirmação aplica, nunca o texto da planilha.
  • NADA É GRAVADO ANTES DA CONFIRMAÇÃO — nem cadastro de apoio. A prévia só DESCREVE a empresa, a
    classe e o centro de custo de cada linha; criar os que faltam é parte da confirmação.
  • IDEMPOTENTE. O equipamento é reconhecido pelo CÓDIGO DO ERP (`codigo_legado`): reenviar a mesma
    planilha encontra os 395 e não cria nenhum. O código interno do Zelo é outro e é gerado.
  • IMPORTAR NÃO CORRIGE NEM RECUSA POR SUSPEITA. Chassi repetido, data de 1905, ano-modelo à frente,
    placa interna: a linha entra como veio e a suspeita vira CONFERÊNCIA (`frota_qualidade_service`).
    Só é recusado o que não dá para gravar: sem código, data que não é data, texto que não cabe, placa
    de outro equipamento, status ou natureza que ninguém decidiu.
  • NÃO PRESUME QUE "EM USO" É VALIDADO. Todo equipamento importado nasce com a validação pendente; o
    status vem do mapeamento que o responsável conferiu, e quem confirma a situação é o responsável
    pelo inventário (`frota_service.validar_cadastro`).
  • ATUALIZAR É AUTORIZAÇÃO SEPARADA. Sem ela, equipamento já cadastrado fica como está. Com ela, só
    mudam os campos que O ERP SABE (`CAMPOS_DO_ERP`): o que o Zelo gerencia (nome, status, categoria,
    fazenda, responsável) nunca é tocado, e célula vazia nunca apaga dado.

Planilha é entrada hostil (limites de 5 MB e 5.000 linhas, nada é executado). Contratos de leitura para
as telas: `COLUNAS`, `modelo_xlsx`, `modelo_csv`, `enviar`, `painel_do_mapeamento`, `aplicar_mapeamento`,
`resumo`, `conferencias_agrupadas`, `confirmar`, `cancelar`, `relatorio`, `exportar_relatorio_xlsx`,
`listar_importacoes`, `linhas_da_importacao`, `arquivo_ja_importado`.
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from ProjetoEstoque.models import (
    FrotaCentroCusto,
    FrotaClasseMecanica,
    FrotaClasseOperacional,
    FrotaEmpresa,
    FrotaImportacaoEquipamentoLinha,
    FrotaImportacaoEquipamentos,
    FrotaNatureza,
    FrotaTipo,
    FrotaVeiculo,
)
from services import busca_service as busca
from services import frota_classificacao_service as classificacao
from services import frota_qualidade_service as qualidade
from services import frota_service as frota
from services import planilha_leitura as leitura

MAX_BYTES = leitura.MAX_BYTES
MAX_LINHAS = 5000
MAX_DESCOMPACTADO = leitura.MAX_DESCOMPACTADO
LOTE_IN = 400                              # `__in` em pedaços, longe do limite de variáveis do SQLite
LINHAS_PARA_ACHAR_O_CABECALHO = 10
LIMITE_DE_DATA_EM_LOTE = 20                # tantos equipamentos com a MESMA data viram um aviso da planilha

Estado = FrotaImportacaoEquipamentos.Estado
Situacao = FrotaImportacaoEquipamentoLinha.Situacao
Status = FrotaVeiculo.Status

# O que significa "sem setor" no ERP: ele escreve "Não Informado" (código 0). Isso é a AUSÊNCIA do dado —
# o texto bruto fica na linha da importação, e o equipamento entra sem setor (e com a conferência).
SETOR_SEM_INFORMACAO = frozenset({"nao informado", "n/i", "ni", "sem setor", "-"})

# Situação do ERP -> status do Zelo, só o que é SEGURO sugerir. "Em Uso" é o único valor que a planilha traz
# hoje; qualquer outro (vendido, baixado…) pede decisão de quem confere, nunca um palpite.
STATUS_SUGERIDO = {"em uso": Status.ATIVO}


# ═════════════════════════════════════════════════════════════
# As colunas da planilha e o campo de cada uma
# ═════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Coluna:
    campo: str
    rotulo: str                  # o cabeçalho do arquivo-modelo
    aliases: tuple               # cabeçalhos aceitos, sem acento nem caixa
    tipo: str                    # texto | número | data | código
    obrigatoria: bool
    destino: str                 # onde o dado mora no Zelo (mostrado no mapeamento)
    justificativa: str = ""      # por que NÃO vira campo do equipamento, quando é o caso


COLUNAS = (
    Coluna("empresa_codigo", "Cód.Empresa", ("cod.empresa", "cod. empresa", "cod empresa", "codigo da empresa", "codigo empresa"),
           "código", False, "Cadastro de empresas · código do ERP"),
    Coluna("empresa", "Empresa", ("empresa",), "texto", False, "Empresa e natureza do ativo (próprio ou terceiro)"),
    Coluna("codigo_legado", "Equipamento", ("equipamento", "codigo do equipamento", "codigo equipamento", "cod. equipamento"),
           "código", True, "Código original (ERP) — preservado como identificador legado"),
    Coluna("modelo", "Modelo", ("modelo", "descricao", "nome"), "texto", True, "Nome e modelo do equipamento"),
    Coluna("placa", "Placa", ("placa",), "texto", False, "Placa / identificação"),
    Coluna("classe_operacional_codigo", "Cód. Operacional", ("cod. operacional", "cod operacional", "codigo operacional"),
           "código", False, "Cadastro de classes operacionais · código do ERP"),
    Coluna("classe_operacional", "Classe Operacional", ("classe operacional",), "texto", False, "Classe operacional (e a categoria que a agrupa)"),
    Coluna("setor_codigo", "Cód.", ("cod.", "cod", "codigo", "cod. setor", "codigo do setor"), "código", False,
           "Registrado só na linha da importação",
           "O setor é identificado pelo nome no cadastro; o código do ERP não tem outro uso no Zelo e fica guardado na linha da importação e no relatório."),
    Coluna("setor", "Setor", ("setor",), "texto", False, "Setor"),
    Coluna("centro_custo_codigo", "Cód.Custo", ("cod.custo", "cod. custo", "cod custo", "codigo do custo", "codigo custo"),
           "código", False, "Cadastro de centros de custo · código do ERP"),
    Coluna("centro_custo", "Cent. Custo", ("cent. custo", "cent custo", "centro de custo", "centro custo"), "texto", False, "Centro de custo"),
    Coluna("chassi_serie", "Chassi", ("chassi", "chassi / numero de serie", "numero de serie"), "texto", False, "Chassi / nº de série"),
    Coluna("ano_modelo", "Ano Modelo", ("ano modelo", "ano-modelo"), "número", False, "Ano-modelo"),
    Coluna("ano_fabricacao", "Ano Fabr.", ("ano fabr.", "ano fabr", "ano de fabricacao", "ano fabricacao"), "número", False, "Ano de fabricação"),
    Coluna("data_compra", "Data Compra", ("data compra", "data de compra"), "data", False, "Data de compra"),
    Coluna("situacao", "Situação", ("situacao",), "texto", False, "Status do equipamento (conferido antes de importar)"),
    Coluna("classe_mecanica_codigo", "Cód.2", ("cod.2", "cod. 2", "cod 2", "cod. mecanica", "codigo mecanica"),
           "código", False, "Cadastro de classes mecânicas · código do ERP"),
    Coluna("classe_mecanica", "Classe Mecânica", ("classe mecanica",), "texto", False, "Classe mecânica"),
    Coluna("data_inicio", "Data de Início", ("data de inicio", "data inicio"), "data", False, "Data de início"),
)
COLUNA = {c.campo: c for c in COLUNAS}
ROTULOS = [c.rotulo for c in COLUNAS]
CABECALHO_ESPERADO = ", ".join(ROTULOS)
_CAMPO_POR_CABECALHO = {leitura.normalizar(alias): c.campo for c in COLUNAS for alias in (*c.aliases, c.rotulo)}

# O que o ERP SABE e, por isso, o que uma atualização autorizada pode mudar. O que o Zelo gerencia
# (nome, status, categoria, fazenda, responsável, observações) nunca é tocado pela planilha.
CAMPOS_DO_ERP = (
    "modelo", "placa", "chassi_serie", "ano_modelo", "ano_fabricacao", "data_compra", "data_inicio", "setor", "natureza",
    "empresa", "classe_operacional", "classe_mecanica", "centro_custo",
)
_CATALOGO_POR_CAMPO = {
    "empresa": ("empresa", FrotaEmpresa), "classe_operacional": ("classe_operacional", FrotaClasseOperacional),
    "classe_mecanica": ("classe_mecanica", FrotaClasseMecanica), "centro_custo": ("centro_custo", FrotaCentroCusto),
}


# ═════════════════════════════════════════════════════════════
# Arquivos-modelo
# ═════════════════════════════════════════════════════════════

INSTRUCOES = (
    "Como preencher",
    "",
    "1. Preencha a aba \"Equipamentos\", uma linha por equipamento. Não apague nem renomeie a linha de cabeçalho.",
    "2. Só duas colunas são obrigatórias: Equipamento (o código do ERP) e Modelo. As demais enriquecem o cadastro.",
    "3. Formate Equipamento, Placa e Chassi como TEXTO (já vem assim neste modelo): o Excel remove zeros à esquerda",
    "   e arredonda número comprido.",
    "4. Empresa: SANTA COLOMBA vale como equipamento próprio e TERCEIROS como de terceiro. Outro nome pede que você",
    "   escolha a natureza no passo de mapeamento — o sistema não adivinha de quem é o ativo.",
    "5. Situação: \"Em Uso\" vira Em uso. Qualquer outro valor pede que você escolha o status no mapeamento.",
    "6. Datas no formato dia/mês/ano. Ano com 4 dígitos.",
    "7. Placa e chassi são opcionais: gerador, trator e implemento nem sempre têm.",
    "",
    "O que a importação NÃO faz",
    "",
    "• Não corrige nada: chassi repetido, data improvável e ano estranho entram como vieram, e aparecem nas conferências.",
    "• Não altera equipamento já cadastrado, a menos que você autorize na confirmação — e mesmo assim só os dados",
    "  que o ERP conhece (nunca nome, status, categoria, fazenda ou responsável).",
    "• Não considera o equipamento validado: o responsável pelo inventário confirma a situação depois.",
    "• Não executa fórmulas: texto que começa com =, +, - ou @ é tratado como texto.",
    "",
    "Limites: arquivo de até 5 MB e 5.000 linhas, em .xlsx ou .csv. Antes de gravar, o sistema mostra uma prévia",
    "com o que será importado, o que já existe e o que foi rejeitado (e por quê).",
)


def modelo_xlsx() -> bytes:
    """O arquivo-modelo em Excel: aba "Equipamentos" (só o cabeçalho do ERP, com as colunas de código, placa
    e chassi como TEXTO) e a aba "Instruções". Devolve os bytes do .xlsx."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "Equipamentos"
    ws.append(ROTULOS)
    for indice, coluna in enumerate(COLUNAS, start=1):
        celula = ws.cell(row=1, column=indice)
        celula.font = Font(bold=True, color="FFFFFF")
        celula.fill = PatternFill("solid", fgColor="1F6B47")
        celula.alignment = Alignment(vertical="center")
        ws.column_dimensions[celula.column_letter].width = max(14, len(coluna.rotulo) + 4)
        if coluna.tipo == "código" or coluna.campo in ("placa", "chassi_serie"):
            for linha in range(2, 502):
                ws.cell(row=linha, column=indice).number_format = "@"
    ws.freeze_panes = "A2"
    ajuda = wb.create_sheet("Instruções")
    for linha in INSTRUCOES:
        ajuda.append([linha])
    ajuda.column_dimensions["A"].width = 120
    ajuda["A1"].font = Font(bold=True, size=13)
    saida = io.BytesIO()
    wb.save(saida)
    return saida.getvalue()


def modelo_csv() -> bytes:
    """O modelo em CSV: UTF-8 com BOM (o Excel brasileiro abre a acentuação certa) e separador `;`."""
    saida = io.StringIO(newline="")
    csv.writer(saida, delimiter=";", lineterminator="\r\n").writerow(ROTULOS)
    return saida.getvalue().encode("utf-8-sig")


# ═════════════════════════════════════════════════════════════
# Leitura do arquivo
# ═════════════════════════════════════════════════════════════

def _bruto_da_celula(valor) -> str:
    """O que veio na célula, como texto estável para guardar: data em ISO, inteiro sem `.0`, texto aparado."""
    if valor is None:
        return ""
    if isinstance(valor, datetime):
        return valor.date().isoformat()
    if isinstance(valor, date):
        return valor.isoformat()
    if isinstance(valor, bool):
        return str(valor)
    if isinstance(valor, float):
        return str(int(valor)) if valor == int(valor) and abs(valor) < 1e15 else repr(valor)
    return " ".join(str(valor).split()) if "\n" not in str(valor) else str(valor).strip()


def _localizar_cabecalho(linhas: list) -> tuple[int, list]:
    """A linha do cabeçalho: entre as primeiras não vazias, a que mais cabeçalhos conhecidos reconhece
    (planilha exportada por ERP costuma ter título e data antes do cabeçalho). Devolve `(posição na
    lista, cabeçalhos como texto)` ou levanta `ValidationError` com as colunas esperadas."""
    melhor, melhor_pontos = None, 0
    for posicao, (_, celulas) in enumerate(linhas[:LINHAS_PARA_ACHAR_O_CABECALHO]):
        reconhecidos = {_CAMPO_POR_CABECALHO.get(leitura.normalizar(c)) for c in celulas if c is not None} - {None}
        if "codigo_legado" in reconhecidos and len(reconhecidos) > melhor_pontos:
            melhor, melhor_pontos = posicao, len(reconhecidos)
    if melhor is None:
        raise ValidationError(
            "Cabeçalho não reconhecido: a planilha precisa ter, na primeira linha, pelo menos a coluna Equipamento "
            f"(o código do ERP). Colunas esperadas: {CABECALHO_ESPERADO}. Use o arquivo-modelo."
        )
    return melhor, [_bruto_da_celula(c) for c in linhas[melhor][1]]


def _colunas_reconhecidas(cabecalho: list) -> dict:
    """`{campo: índice}` pelos cabeçalhos conhecidos; a primeira coluna que casa com o campo vale."""
    colunas: dict[str, int] = {}
    for indice, texto in enumerate(cabecalho):
        campo = _CAMPO_POR_CABECALHO.get(leitura.normalizar(texto))
        if campo and campo not in colunas:
            colunas[campo] = indice
    return colunas


def _ler_arquivo(arquivo):
    nome = leitura.nome_do_arquivo(arquivo)
    extensao = os.path.splitext(nome)[1].lower()
    if extensao not in (".xlsx", ".csv"):
        raise ValidationError("Envie uma planilha .xlsx ou um arquivo .csv.")
    dados = leitura.ler_bytes(arquivo, MAX_BYTES)
    if extensao == ".xlsx":
        linhas = leitura.linhas_do_xlsx(dados, max_descompactado=MAX_DESCOMPACTADO, aba_preferida="Equipamentos")
    else:
        linhas = leitura.linhas_do_csv(dados)
    linhas = [(n, v) for n, v in linhas if not leitura.linha_vazia(v)]
    if not linhas:
        raise ValidationError("A planilha está vazia.")
    posicao, cabecalho = _localizar_cabecalho(linhas)
    corpo = linhas[posicao + 1:]
    if not corpo:
        raise ValidationError("A planilha só tem o cabeçalho: nenhuma linha de dados.")
    if len(corpo) > MAX_LINHAS:
        raise ValidationError(f"A planilha tem mais de {MAX_LINHAS:,} linhas de dados.".replace(",", "."))
    return {
        "nome": nome, "formato": "xlsx" if extensao == ".xlsx" else "csv", "sha256": hashlib.sha256(dados).hexdigest(),
        "cabecalho": cabecalho, "numero_do_cabecalho": linhas[posicao][0], "corpo": corpo,
    }


# ═════════════════════════════════════════════════════════════
# Mapeamento
# ═════════════════════════════════════════════════════════════

def _chave(valor) -> str:
    return busca.normalizar_busca(valor)


def _mapeamento_inicial(cabecalho: list, numero_do_cabecalho: int) -> dict:
    return {
        "cabecalho": cabecalho, "numero_do_cabecalho": numero_do_cabecalho, "colunas": _colunas_reconhecidas(cabecalho),
        "valores": {"situacao": {}, "empresa": {}, "classe_operacional": {}},
        # `conferido`: o responsável passou pelo passo de mapeamento. Antes disso a prévia existe (as sugestões
        # já foram aplicadas, para a tela mostrar o quadro), mas não se avança nem se confirma.
        "conferido": False,
    }


def _celulas_das_linhas(importacao):
    return importacao.itens.order_by("numero").values_list("numero", "dados")


def valores_distintos(importacao: FrotaImportacaoEquipamentos) -> dict:
    """Os valores que PEDEM DECISÃO, com quantos equipamentos cada um tem, lidos das linhas brutas pelo
    mapeamento de colunas vigente: `{"situacao": [(valor, n)], "empresa": [...], "classe_operacional": [...]}`.
    O vazio entra como `""` (empresa e situação em branco também precisam de destino)."""
    colunas = importacao.mapeamento.get("colunas", {})
    contagem: dict[str, Counter] = {k: Counter() for k in ("situacao", "empresa", "classe_operacional")}
    for _, dados in _celulas_das_linhas(importacao):
        celulas = dados.get("celulas", [])
        for campo in contagem:
            indice = colunas.get(campo)
            if indice is not None:
                contagem[campo][(celulas[indice] if indice < len(celulas) else "").strip()] += 1
    return {campo: sorted(c.items(), key=lambda par: (par[0] == "", par[0].casefold())) for campo, c in contagem.items()}


def _decisao_de_valor(mapeamento: dict, campo: str, valor: str):
    """O que foi decidido para `valor` do `campo` (`""` = nada decidido ainda)."""
    return (mapeamento.get("valores", {}).get(campo, {}) or {}).get(_chave(valor), "")


def _com_sugestoes(mapeamento: dict, distintos: dict) -> dict:
    """O mapeamento de valores com a SUGESTÃO nos lugares ainda sem decisão. Sugerir não decide: a tela
    mostra a sugestão preenchida, quem confere muda, e só o que foi confirmado vale na análise."""
    valores = {campo: dict(mapa) for campo, mapa in mapeamento.get("valores", {}).items()}
    for valor, _ in distintos["situacao"]:
        valores["situacao"].setdefault(_chave(valor), STATUS_SUGERIDO.get(_chave(valor), ""))
    for valor, _ in distintos["empresa"]:
        valores["empresa"].setdefault(_chave(valor), classificacao.sugerir_natureza(valor))
    for valor, _ in distintos["classe_operacional"]:
        valores["classe_operacional"].setdefault(_chave(valor), (classificacao.sugerir_categoria(valor) or classificacao.CATEGORIA_DE_RESERVA) if valor else "")
    return valores


def painel_do_mapeamento(importacao: FrotaImportacaoEquipamentos) -> dict:
    """Tudo o que o passo "Mapeamento" mostra:

        {"colunas": [{"campo", "rotulo", "tipo", "obrigatoria", "destino", "justificativa", "indice",
                      "origem" (o cabeçalho do arquivo), "situacao": "mapeada"|"nao_mapeada"|"faltando"}],
         "cabecalho": [textos do arquivo], "nao_usadas": [cabeçalhos do arquivo sem destino],
         "valores": {"situacao": [{"valor", "n", "decisao", "sugerido"}], "empresa": [...], "classe_operacional": [...]},
         "opcoes": {"situacao": [(valor, rótulo)], "empresa": [...], "classe_operacional": [categorias]}}

    Coluna obrigatória sem origem é "faltando" (a importação não segue); opcional sem origem, "não mapeada"."""
    mapeamento = importacao.mapeamento
    colunas = mapeamento.get("colunas", {})
    cabecalho = mapeamento.get("cabecalho", [])
    linhas = []
    for c in COLUNAS:
        indice = colunas.get(c.campo)
        origem = cabecalho[indice] if indice is not None and indice < len(cabecalho) else ""
        situacao = "mapeada" if origem or indice is not None else ("faltando" if c.obrigatoria else "nao_mapeada")
        linhas.append({
            "campo": c.campo, "rotulo": c.rotulo, "tipo": c.tipo, "obrigatoria": c.obrigatoria, "destino": c.destino,
            "justificativa": c.justificativa, "indice": indice, "origem": origem, "situacao": situacao,
        })
    usados = set(colunas.values())
    nao_usadas = [texto for i, texto in enumerate(cabecalho) if i not in usados and texto]
    distintos = valores_distintos(importacao)
    sugeridos = _com_sugestoes({"valores": {"situacao": {}, "empresa": {}, "classe_operacional": {}}}, distintos)
    decididos = _com_sugestoes(mapeamento, distintos)
    valores = {
        campo: [
            {"valor": valor, "n": n, "decisao": decididos[campo].get(_chave(valor), ""), "sugerido": sugeridos[campo].get(_chave(valor), "")}
            for valor, n in distintos[campo]
        ]
        for campo in distintos
    }
    categorias = sorted({*FrotaTipo.objects.filter(ativo=True).values_list("nome", flat=True), *(c for _, c in classificacao.CATEGORIA_SUGERIDA), classificacao.CATEGORIA_DE_RESERVA})
    return {
        "colunas": linhas, "cabecalho": cabecalho, "nao_usadas": nao_usadas, "valores": valores,
        "opcoes": {"situacao": list(Status.choices), "empresa": list(FrotaNatureza.choices), "classe_operacional": categorias},
    }


def _exigir_previa_aberta(importacao: FrotaImportacaoEquipamentos) -> FrotaImportacaoEquipamentos:
    atual = FrotaImportacaoEquipamentos.objects.get(pk=importacao.pk)
    if atual.estado != Estado.PREVIA:
        raise ValidationError(f"Esta importação já foi {atual.get_estado_display().lower()}.")
    if atual.expirada:
        raise ValidationError("A prévia expirou (vale por 24 h). Envie a planilha de novo para gerar outra.")
    return atual


def aplicar_mapeamento(importacao: FrotaImportacaoEquipamentos, colunas: dict, valores: dict) -> FrotaImportacaoEquipamentos:
    """Grava o que quem conferiu decidiu e REANALISA as linhas (as células brutas ficam como estão).

    `colunas`: `{campo: índice do cabeçalho do arquivo, ou ""/None para ignorar}`; só os campos de `COLUNAS`.
    `valores`: `{"situacao": {valor: status}, "empresa": {valor: natureza}, "classe_operacional": {valor:
    categoria}}`. O índice precisa existir no arquivo; o campo obrigatório precisa de uma coluna. Só vale
    com a prévia aberta. Levanta `ValidationError`."""
    atual = _exigir_previa_aberta(importacao)
    cabecalho = atual.mapeamento.get("cabecalho", [])
    novas: dict[str, int] = {}
    for campo, indice in (colunas or {}).items():
        if campo not in COLUNA:
            raise ValidationError(f"Campo desconhecido no mapeamento: {campo}.")
        if indice in (None, ""):
            continue
        try:
            indice = int(indice)
        except (TypeError, ValueError):
            raise ValidationError(f"A coluna escolhida para {COLUNA[campo].rotulo} não é válida.") from None
        if not 0 <= indice < len(cabecalho):
            raise ValidationError(f"A coluna escolhida para {COLUNA[campo].rotulo} não existe no arquivo.")
        novas[campo] = indice
    faltando = [c.rotulo for c in COLUNAS if c.obrigatoria and c.campo not in novas]
    if faltando:
        raise ValidationError(f"Escolha a coluna de {', '.join(faltando)}: sem ela a importação não identifica o equipamento.")
    limpos: dict[str, dict] = {}
    validos = {"situacao": set(Status.values), "empresa": set(FrotaNatureza.values)}
    for campo in ("situacao", "empresa", "classe_operacional"):
        limpos[campo] = {}
        for valor, decisao in ((valores or {}).get(campo) or {}).items():
            decisao = str(decisao or "").strip()
            if not decisao:
                continue
            if campo in validos and decisao not in validos[campo]:
                raise ValidationError(f"Valor inválido no mapeamento de {campo}: {decisao}.")
            limpos[campo][_chave(valor)] = decisao
    mapeamento = dict(atual.mapeamento)
    mapeamento["colunas"], mapeamento["valores"], mapeamento["conferido"] = novas, limpos, True
    atual.mapeamento = mapeamento
    atual.save(update_fields=["mapeamento"])
    _reanalisar(atual)
    importacao.refresh_from_db()
    return importacao


# ═════════════════════════════════════════════════════════════
# Análise de uma linha
# ═════════════════════════════════════════════════════════════

def _data_da_celula(texto: str, rotulo: str, motivos: list):
    if not texto:
        return None
    try:
        return frota.ler_data(texto, rotulo=rotulo)
    except ValidationError as exc:
        motivos.append(f"{rotulo} “{texto}” não é uma data válida: {exc.messages[0]}")
        return None


def _ano_da_celula(texto: str, rotulo: str, motivos: list):
    if not texto:
        return None
    try:
        return frota.ler_ano(texto, rotulo, importando=True)
    except ValidationError as exc:
        motivos.append(f"{rotulo} “{texto}” não é um ano válido: {exc.messages[0]}")
        return None


def _entidade_da_linha(celulas: list, colunas: dict, campo_codigo: str, campo_nome: str) -> dict:
    def valor(campo):
        indice = colunas.get(campo)
        return (celulas[indice] if indice is not None and indice < len(celulas) else "").strip()

    return {"codigo": valor(campo_codigo), "nome": valor(campo_nome)}


def _normalizar_linha(celulas: list, mapeamento: dict) -> tuple[dict, list, list]:
    """Passo A (só a linha): lê as células pelo mapeamento e devolve `(dados, motivos, avisos)` — `motivos`
    rejeitam a linha, `avisos` só informam. Nada de banco aqui."""
    colunas = mapeamento.get("colunas", {})
    motivos: list[str] = []
    avisos: list[str] = []

    def valor(campo) -> str:
        indice = colunas.get(campo)
        return (celulas[indice] if indice is not None and indice < len(celulas) else "").strip()

    codigo = valor("codigo_legado")
    if not codigo:
        motivos.append("O código do equipamento (coluna Equipamento) está vazio.")
    elif len(codigo) > 30:
        motivos.append("O código do equipamento passa de 30 caracteres.")
    modelo = valor("modelo")
    if not modelo:
        motivos.append("O modelo está vazio: sem ele o equipamento fica sem nome.")
    elif len(modelo) > 80:
        motivos.append("O modelo passa de 80 caracteres.")

    placa = valor("placa")
    try:
        placa = frota.normalizar_identificacao(placa)
    except ValidationError as exc:
        motivos.append(f"Placa “{placa}”: {exc.messages[0]}")
        placa = ""
    try:
        chassi = frota.normalizar_chassi(valor("chassi_serie"))
    except ValidationError as exc:
        motivos.append(exc.messages[0])
        chassi = ""
    setor_bruto = valor("setor")
    setor = "" if _chave(setor_bruto) in SETOR_SEM_INFORMACAO else setor_bruto
    if len(setor) > 100:
        motivos.append("O setor passa de 100 caracteres.")

    empresa = _entidade_da_linha(celulas, colunas, "empresa_codigo", "empresa")
    classe_operacional = _entidade_da_linha(celulas, colunas, "classe_operacional_codigo", "classe_operacional")
    classe_mecanica = _entidade_da_linha(celulas, colunas, "classe_mecanica_codigo", "classe_mecanica")
    centro_custo = _entidade_da_linha(celulas, colunas, "centro_custo_codigo", "centro_custo")
    for rotulo, entidade in (("A empresa", empresa), ("A classe operacional", classe_operacional), ("A classe mecânica", classe_mecanica), ("O centro de custo", centro_custo)):
        if len(entidade["nome"]) > 120:
            motivos.append(f"{rotulo} passa de 120 caracteres.")
        if len(entidade["codigo"]) > 20:
            motivos.append(f"O código de {rotulo.lower()} passa de 20 caracteres.")

    situacao_origem = valor("situacao")
    if len(situacao_origem) > 40:
        motivos.append("A situação passa de 40 caracteres.")

    # decisões de quem conferiu o mapeamento
    status = _decisao_de_valor(mapeamento, "situacao", situacao_origem)
    if "situacao" in colunas and not status:
        status = ""
        motivos.append(f"Situação “{situacao_origem or 'vazia'}” sem destino: escolha o status no mapeamento.")
    elif "situacao" not in colunas:
        status = Status.ATIVO                                             # sem a coluna não há o que mapear
    natureza = _decisao_de_valor(mapeamento, "empresa", empresa["nome"])
    if not natureza:
        motivos.append(f"Empresa “{empresa['nome'] or 'vazia'}” sem natureza: escolha Próprio ou Terceiro no mapeamento.")
    categoria = _decisao_de_valor(mapeamento, "classe_operacional", classe_operacional["nome"]) if classe_operacional["nome"] else ""

    dados = {
        "codigo_legado": codigo, "nome": modelo, "modelo": modelo, "placa": placa, "chassi_serie": chassi,
        "ano_modelo": _ano_da_celula(valor("ano_modelo"), "O ano-modelo", motivos),
        "ano_fabricacao": _ano_da_celula(valor("ano_fabricacao"), "O ano de fabricação", motivos),
        "data_compra": None, "data_inicio": None,
        "setor": setor, "setor_codigo": valor("setor_codigo"), "setor_original": setor_bruto,
        "empresa": empresa, "natureza": natureza, "classe_operacional": classe_operacional,
        "classe_mecanica": classe_mecanica, "centro_custo": centro_custo, "categoria": categoria,
        "situacao_origem": situacao_origem, "status": status,
    }
    compra = _data_da_celula(valor("data_compra"), "A data de compra", motivos)
    inicio = _data_da_celula(valor("data_inicio"), "A data de início", motivos)
    for campo, data, rotulo in (("data_compra", compra, "A data de compra"), ("data_inicio", inicio, "A data de início")):
        if data is not None and data.year < frota.ANO_DA_IMPORTACAO[0]:
            motivos.append(f"{rotulo} ({data:%d/%m/%Y}) está antes de {frota.ANO_DA_IMPORTACAO[0]}: não é uma data de cadastro.")
        dados[campo] = data.isoformat() if data is not None else None
    for texto in (codigo, modelo, placa, chassi, setor_bruto):
        if texto[:1] in leitura.PREFIXOS_DE_FORMULA:
            avisos.append(f"“{texto[:20]}” começa com “{texto[:1]}”: tratado como texto, nada é executado.")
    return dados, motivos, avisos


# ═════════════════════════════════════════════════════════════
# Análise do conjunto
# ═════════════════════════════════════════════════════════════

def _em_pedacos(valores, tamanho=LOTE_IN):
    valores = list(valores)
    for i in range(0, len(valores), tamanho):
        yield valores[i:i + tamanho]


def _espera_do_tipo(nome_da_categoria: str, tipos_por_nome: dict) -> tuple[bool, bool]:
    existente = tipos_por_nome.get(_chave(nome_da_categoria))
    if existente is not None:
        return existente.espera_placa, existente.espera_chassi
    padrao = classificacao.ESPERA_DA_CATEGORIA_NOVA.get(nome_da_categoria, {})
    return bool(padrao.get("espera_placa")), bool(padrao.get("espera_chassi"))


def _diferencas(dados: dict, veiculo: FrotaVeiculo) -> dict:
    """Em que a planilha difere do equipamento JÁ cadastrado, só nos `CAMPOS_DO_ERP` e só onde a planilha
    TEM valor (célula vazia nunca apaga dado). `{campo: {"de", "para"}}` com textos para a tela."""
    achadas = {}

    def comparar(campo, atual, novo, exibir_atual=None, exibir_novo=None):
        if novo in (None, "") or atual == novo:
            return
        achadas[campo] = {"de": str(exibir_atual if exibir_atual is not None else atual or "—"), "para": str(exibir_novo if exibir_novo is not None else novo)}

    for campo in ("modelo", "placa", "chassi_serie", "setor"):
        comparar(campo, getattr(veiculo, campo), dados[campo])
    for campo in ("ano_modelo", "ano_fabricacao"):
        comparar(campo, getattr(veiculo, campo), dados[campo])
    for campo in ("data_compra", "data_inicio"):
        atual = getattr(veiculo, campo)
        comparar(campo, atual.isoformat() if atual else None, dados[campo], atual.strftime("%d/%m/%Y") if atual else "—",
                 datetime.fromisoformat(dados[campo]).strftime("%d/%m/%Y") if dados[campo] else "")
    comparar("natureza", veiculo.natureza, dados["natureza"], veiculo.get_natureza_display(), dict(FrotaNatureza.choices).get(dados["natureza"], ""))
    for campo, (chave, _) in _CATALOGO_POR_CAMPO.items():
        entidade = dados[campo]
        atual = getattr(veiculo, campo)
        if not (entidade["codigo"] or entidade["nome"]):
            continue
        mesmo = atual is not None and (
            (entidade["codigo"] and atual.codigo_erp == entidade["codigo"])
            or (not entidade["codigo"] and _chave(atual.nome) == _chave(entidade["nome"]))
        )
        if not mesmo:
            achadas[campo] = {"de": atual.nome if atual else "—", "para": entidade["nome"] or entidade["codigo"]}
    return achadas


def _analisar_linhas(importacao: FrotaImportacaoEquipamentos, linhas: list) -> list:
    """Passo B: a análise do CONJUNTO (duplicidade no arquivo e no cadastro, chassi repetido, placa de
    outro equipamento, diferenças com o que já existe). Devolve as linhas com `dados`, `situacao`,
    `motivos`, `conferencias` e `veiculo` preenchidos — o chamador grava."""
    mapeamento = importacao.mapeamento
    primeiras = []
    for linha in linhas:
        dados, motivos, avisos = _normalizar_linha(linha.dados.get("celulas", []), mapeamento)
        dados["celulas"] = linha.dados.get("celulas", [])
        primeiras.append((linha, dados, motivos, avisos))

    codigos = {d["codigo_legado"] for _, d, m, _ in primeiras if d["codigo_legado"]}
    existentes: dict[str, FrotaVeiculo] = {}
    for pedaco in _em_pedacos(sorted(codigos)):
        for v in FrotaVeiculo.objects.filter(codigo_legado__in=pedaco).select_related("empresa", "classe_operacional", "classe_mecanica", "centro_custo"):
            existentes[v.codigo_legado] = v
    placas = sorted({d["placa"] for _, d, _, _ in primeiras if d["placa"]})
    placas_no_cadastro: dict[str, FrotaVeiculo] = {}
    for pedaco in _em_pedacos(placas):
        for v in FrotaVeiculo.objects.filter(placa__in=pedaco):
            placas_no_cadastro[v.placa] = v
    chassis = sorted({d["chassi_serie"] for _, d, _, _ in primeiras if d["chassi_serie"]})
    chassis_no_cadastro: dict[str, list] = defaultdict(list)
    for pedaco in _em_pedacos(chassis):
        for v in FrotaVeiculo.objects.filter(chassi_serie__in=pedaco):
            chassis_no_cadastro[v.chassi_serie].append(v)
    chassis_no_arquivo: dict[str, list] = defaultdict(list)
    for _, d, m, _ in primeiras:
        if d["chassi_serie"] and d["codigo_legado"]:
            chassis_no_arquivo[d["chassi_serie"]].append(d)
    tipos_por_nome = {_chave(t.nome): t for t in FrotaTipo.objects.all()}
    hoje = timezone.localdate()

    vistos_codigo: dict[str, int] = {}
    vistas_placas: dict[str, int] = {}
    resultado = []
    for linha, dados, motivos, avisos in primeiras:
        situacao, veiculo = Situacao.REJEITADA, None
        codigo = dados["codigo_legado"]
        if not motivos:
            if codigo in vistos_codigo:
                situacao, motivos = Situacao.EXISTENTE, [f"Equipamento repetido: o código {codigo} já aparece na linha {vistos_codigo[codigo]} desta planilha."]
            elif codigo in existentes:
                veiculo = existentes[codigo]
                situacao, motivos = Situacao.EXISTENTE, ["Equipamento já cadastrado — não é alterado pela importação."]
                dados["diferencas"] = _diferencas(dados, veiculo)
            else:
                situacao = Situacao.VALIDA
                placa = dados["placa"]
                if placa and placa in vistas_placas:
                    situacao, motivos = Situacao.REJEITADA, [f"A placa {placa} já está na linha {vistas_placas[placa]} desta planilha."]
                elif placa and placa in placas_no_cadastro:
                    dono = placas_no_cadastro[placa]
                    situacao, motivos = Situacao.REJEITADA, [f"A placa {placa} já pertence ao equipamento {dono.codigo_legado or dono.codigo} · {dono.nome}."]
        if codigo and codigo not in vistos_codigo:
            vistos_codigo[codigo] = linha.numero
        if situacao == Situacao.VALIDA and dados["placa"]:
            vistas_placas.setdefault(dados["placa"], linha.numero)

        conferencias = []
        if situacao in (Situacao.VALIDA, Situacao.EXISTENTE) and codigo:
            espera_placa, espera_chassi = _espera_do_tipo(dados["categoria"] or classificacao.CATEGORIA_DE_RESERVA, tipos_por_nome)
            outros: dict[str, str] = {}              # por código do ERP: quem está no arquivo E no cadastro conta uma vez
            for o in chassis_no_arquivo.get(dados["chassi_serie"], []):
                if o["codigo_legado"] != codigo:
                    outros[o["codigo_legado"]] = f"{o['codigo_legado']} · {o['nome']}"
            for v in chassis_no_cadastro.get(dados["chassi_serie"], []):
                if v.codigo_legado != codigo:
                    outros[v.codigo_legado or v.codigo] = f"{v.codigo_legado or v.codigo} · {v.nome}"
            outros = list(outros.values())
            conferencias = qualidade.conferir({
                "placa": dados["placa"], "chassi_serie": dados["chassi_serie"], "ano_fabricacao": dados["ano_fabricacao"],
                "ano_modelo": dados["ano_modelo"], "data_compra": date.fromisoformat(dados["data_compra"]) if dados["data_compra"] else None,
                "data_inicio": date.fromisoformat(dados["data_inicio"]) if dados["data_inicio"] else None,
                "setor": dados["setor"], "tem_classe_operacional": bool(dados["classe_operacional"]["nome"]),
                "espera_placa": espera_placa, "espera_chassi": espera_chassi, "validacao_pendente": situacao == Situacao.VALIDA,
            }, hoje=hoje, chassi_em_outros=outros)
        for aviso in avisos:
            conferencias.append({"codigo": "texto_com_formula", "gravidade": qualidade.INFO, "titulo": "Texto com prefixo de fórmula", "detalhe": aviso})
        resultado.append((linha, dados, situacao, motivos, conferencias, veiculo))
    return resultado


def _datas_em_lote(linhas: list) -> list[dict]:
    """As datas repetidas num número suspeito de equipamentos ("69 equipamentos comprados em 01/01/2024"):
    não é erro de linha, é a assinatura de uma data provisória na carga do ERP — avisa UMA vez."""
    achados = []
    for campo, rotulo in (("data_compra", "de compra"), ("data_inicio", "de início")):
        contagem = Counter(d[campo] for _, d, *_ in linhas if d[campo])
        for dia, n in contagem.most_common():
            if n < LIMITE_DE_DATA_EM_LOTE:
                break
            achados.append({
                "campo": campo, "data": dia, "n": n,
                "texto": f"{n} equipamentos têm a mesma data {rotulo}, {date.fromisoformat(dia):%d/%m/%Y}. Em carga de ERP isso costuma ser data provisória: confira.",
                # a frase curta, para a tela juntar várias datas num aviso só sem repetir o conselho em cada uma
                "curto": f"{n} equipamentos com a mesma data {rotulo}: {date.fromisoformat(dia):%d/%m/%Y}",
            })
    return achados


def _reanalisar(importacao: FrotaImportacaoEquipamentos) -> None:
    linhas = list(importacao.itens.order_by("numero"))
    analisadas = _analisar_linhas(importacao, linhas)
    for linha, dados, situacao, motivos, conferencias, veiculo in analisadas:
        linha.dados, linha.situacao, linha.motivos, linha.conferencias, linha.veiculo = dados, situacao, motivos, conferencias, veiculo
        linha.codigo_legado = dados["codigo_legado"][:30]
    mapeamento = dict(importacao.mapeamento)
    mapeamento["datas_em_lote"] = _datas_em_lote(analisadas)
    importacao.mapeamento = mapeamento
    with transaction.atomic():
        FrotaImportacaoEquipamentoLinha.objects.bulk_update(
            [a[0] for a in analisadas], ["dados", "situacao", "motivos", "conferencias", "veiculo", "codigo_legado"], batch_size=200,
        )
        importacao.save(update_fields=["mapeamento"])
        _gravar_totais(importacao)


def _gravar_totais(importacao: FrotaImportacaoEquipamentos) -> FrotaImportacaoEquipamentos:
    """Recalcula os totais A PARTIR DAS LINHAS (a fonte da verdade) e grava."""
    contagem = {s: 0 for s in Situacao.values}
    avisos = 0
    for situacao, conferencias in importacao.itens.values_list("situacao", "conferencias"):
        contagem[situacao] += 1
        avisos += bool(conferencias)
    importacao.linhas = sum(contagem.values())
    importacao.validas = contagem[Situacao.VALIDA]
    importacao.importados = contagem[Situacao.IMPORTADA]
    importacao.atualizados = contagem[Situacao.ATUALIZADA]
    importacao.existentes = contagem[Situacao.EXISTENTE]
    importacao.rejeitados = contagem[Situacao.REJEITADA]
    importacao.avisos = avisos
    importacao.save(update_fields=["linhas", "validas", "importados", "atualizados", "existentes", "rejeitados", "avisos"])
    return importacao


# ═════════════════════════════════════════════════════════════
# Passo 1: envio
# ═════════════════════════════════════════════════════════════

def enviar(arquivo, usuario) -> FrotaImportacaoEquipamentos:
    """Lê a planilha (.xlsx — inclusive o "Strict" do Excel — ou .csv de até 5 MB e 5.000 linhas), reconhece
    o cabeçalho (mesmo com título acima dele) e cria a importação em estado `previa` com UMA linha por
    equipamento, já analisada pelo mapeamento automático. Nada é gravado na frota. Levanta
    `ValidationError` (formato, tamanho, vazia, sem a coluna Equipamento).

    O mapeamento automático só LIGA colunas pelo cabeçalho; os valores que pedem decisão (situação, empresa,
    classe) nascem sem decisão — o passo seguinte as mostra com a sugestão para quem confere."""
    lido = _ler_arquivo(arquivo)
    mapeamento = _mapeamento_inicial(lido["cabecalho"], lido["numero_do_cabecalho"])
    with transaction.atomic():
        importacao = FrotaImportacaoEquipamentos.objects.create(
            arquivo_nome=lido["nome"], sha256=lido["sha256"], formato=lido["formato"], estado=Estado.PREVIA,
            mapeamento=mapeamento, criada_por=usuario if getattr(usuario, "pk", None) else None,
        )
        FrotaImportacaoEquipamentoLinha.objects.bulk_create([
            FrotaImportacaoEquipamentoLinha(
                importacao=importacao, numero=numero, situacao=Situacao.REJEITADA,
                dados={"celulas": [_bruto_da_celula(c) for c in celulas]},
            )
            for numero, celulas in lido["corpo"]
        ])
        # as SUGESTÕES entram como ponto de partida (o que a planilha parece pedir); o que nenhuma regra
        # reconhece fica sem decisão e a linha, sem destino, até o passo de mapeamento
        mapeamento["valores"] = _com_sugestoes(mapeamento, valores_distintos(importacao))
        importacao.mapeamento = mapeamento
        importacao.save(update_fields=["mapeamento"])
        _reanalisar(importacao)
    importacao.refresh_from_db()
    return importacao


# ═════════════════════════════════════════════════════════════
# Passos 3 e 4: pré-visualização e validação
# ═════════════════════════════════════════════════════════════

def linhas_da_importacao(importacao: FrotaImportacaoEquipamentos, situacao: str | None = None, regra: str | None = None, busca_texto: str = ""):
    """As linhas por número, com o equipamento carregado. `situacao` filtra por uma delas, `regra` pelas que
    têm aquela conferência (o JSON não se consulta no SQLite: os números saem de uma passada em Python) e
    `busca_texto` pelo código do ERP ou pelo modelo, sem acento nem caixa."""
    qs = importacao.itens.select_related("veiculo").order_by("numero")
    if situacao in Situacao.values:
        qs = qs.filter(situacao=situacao)
    if regra:
        numeros = [n for n, c in importacao.itens.values_list("numero", "conferencias") if any(x["codigo"] == regra for x in c or [])]
        qs = qs.filter(numero__in=numeros)
    termo = busca.normalizar_busca(busca_texto)
    if termo:
        ids = [
            pk for pk, dados in importacao.itens.values_list("pk", "dados")
            if termo in busca.normalizar_busca(f"{dados.get('codigo_legado', '')} {dados.get('modelo', '')}")
        ]
        qs = qs.filter(pk__in=ids)
    return qs


def linha_do_relatorio(linha: FrotaImportacaoEquipamentoLinha) -> dict:
    """Uma linha como a tela e o Excel a descrevem (a mesma descrição nos dois)."""
    d = linha.dados or {}
    nome = lambda chave: (d.get(chave) or {}).get("nome", "")           # noqa: E731
    return {
        "numero": linha.numero, "situacao": linha.situacao, "situacao_rotulo": linha.get_situacao_display(),
        "codigo_legado": d.get("codigo_legado", ""), "modelo": d.get("modelo", ""), "placa": d.get("placa", ""),
        "chassi_serie": d.get("chassi_serie", ""), "ano_fabricacao": d.get("ano_fabricacao"), "ano_modelo": d.get("ano_modelo"),
        "data_compra": d.get("data_compra"), "data_inicio": d.get("data_inicio"), "setor": d.get("setor", ""),
        "empresa": nome("empresa"), "natureza": d.get("natureza", ""),
        "natureza_rotulo": dict(FrotaNatureza.choices).get(d.get("natureza", ""), ""),
        "classe_operacional": nome("classe_operacional"), "classe_mecanica": nome("classe_mecanica"),
        "centro_custo": nome("centro_custo"), "categoria": d.get("categoria", ""),
        "situacao_origem": d.get("situacao_origem", ""), "status": d.get("status", ""),
        "motivos": list(linha.motivos or []), "conferencias": list(linha.conferencias or []),
        "diferencas": d.get("diferencas") or {}, "aplicadas": d.get("aplicadas") or {},
        "veiculo_id": linha.veiculo_id,
        "veiculo_codigo": linha.veiculo.codigo if linha.veiculo_id else "",
    }


def resumo(importacao: FrotaImportacaoEquipamentos) -> dict:
    """O quadro dos passos de pré-visualização, validação e confirmação, de uma passada nas linhas:

        {"totais": {...}, "novos": n (serão importados), "ja_cadastrados": n, "repetidos_na_planilha": n,
         "rejeitados": n, "com_diferencas": n (já cadastrados que a planilha mudaria, se autorizado),
         "por_regra": {código da conferência: n}, "por_natureza": {"proprio": n, "terceiro": n},
         "sem_placa": n, "sem_chassi": n   (FATO, não erro: determinados equipamentos não têm),
         "datas_em_lote": [...], "avisos_de_cadastro": [...], "conferido": bool, "pronta": bool}

    `pronta`: o mapeamento foi conferido e a prévia está aberta (a confirmação só vale assim)."""
    por_regra: Counter = Counter()
    por_natureza: Counter = Counter()
    repetidos = com_diferencas = ja = sem_placa = sem_chassi = 0
    for linha in importacao.itens.all():
        d = linha.dados or {}
        for c in linha.conferencias or []:
            por_regra[c["codigo"]] += 1
        if linha.situacao == Situacao.EXISTENTE:
            if linha.veiculo_id:
                ja += 1
                com_diferencas += bool(d.get("diferencas"))
            else:
                repetidos += 1
        if linha.situacao in (Situacao.VALIDA, Situacao.IMPORTADA):
            por_natureza[d.get("natureza", "")] += 1
        if linha.situacao != Situacao.REJEITADA:
            sem_placa += not d.get("placa")
            sem_chassi += not d.get("chassi_serie")
    mapeamento = importacao.mapeamento
    return {
        "totais": {
            "linhas": importacao.linhas, "validas": importacao.validas, "importados": importacao.importados,
            "atualizados": importacao.atualizados, "existentes": importacao.existentes, "rejeitados": importacao.rejeitados,
            "avisos": importacao.avisos,
        },
        "novos": importacao.validas, "ja_cadastrados": ja, "repetidos_na_planilha": repetidos,
        "rejeitados": importacao.rejeitados, "com_diferencas": com_diferencas, "por_regra": dict(por_regra),
        "por_natureza": {"proprio": por_natureza.get("proprio", 0), "terceiro": por_natureza.get("terceiro", 0)},
        "sem_placa": sem_placa, "sem_chassi": sem_chassi,
        "datas_em_lote": mapeamento.get("datas_em_lote", []), "avisos_de_cadastro": mapeamento.get("avisos_de_cadastro", []),
        "conferido": bool(mapeamento.get("conferido")),
        "pronta": bool(mapeamento.get("conferido")) and importacao.estado == Estado.PREVIA and not importacao.expirada,
    }


def conferencias_agrupadas(importacao: FrotaImportacaoEquipamentos) -> list[dict]:
    """O passo "Validação dos dados": as conferências por regra, com os equipamentos de cada uma — as de
    `aviso` primeiro, e dentro de cada gravidade na ordem das regras. Nada aqui é corrigido: é o que fica
    para o responsável pelo inventário conferir.

        [{"codigo", "titulo", "gravidade", "n", "equipamentos": [{"numero", "codigo_legado", "modelo", "detalhe"}]}]"""
    grupos: dict[str, dict] = {}
    for linha in importacao.itens.exclude(situacao=Situacao.REJEITADA).order_by("numero"):
        d = linha.dados or {}
        for c in linha.conferencias or []:
            grupo = grupos.setdefault(c["codigo"], {"codigo": c["codigo"], "titulo": c["titulo"], "gravidade": c["gravidade"], "equipamentos": []})
            grupo["equipamentos"].append({"numero": linha.numero, "codigo_legado": d.get("codigo_legado", ""), "modelo": d.get("modelo", ""), "detalhe": c["detalhe"]})
    ordem = list(qualidade.REGRAS)
    achados = sorted(grupos.values(), key=lambda g: (g["gravidade"] != qualidade.AVISO, ordem.index(g["codigo"]) if g["codigo"] in ordem else 99))
    for grupo in achados:
        grupo["n"] = len(grupo["equipamentos"])
    return achados


# ═════════════════════════════════════════════════════════════
# Passo 5: confirmação
# ═════════════════════════════════════════════════════════════

def _categoria_da_linha(dados: dict, usuario, cache: dict) -> FrotaTipo:
    nome = dados.get("categoria") or classificacao.CATEGORIA_DE_RESERVA
    chave = _chave(nome)
    if chave not in cache:
        cache[chave], _ = classificacao.obter_ou_criar_categoria(nome, usuario)
    return cache[chave]


def _preparar_cadastros(linhas: list, usuario) -> dict:
    """Cria (uma vez cada) os cadastros de apoio que as linhas a gravar citam e ainda não existem — empresa,
    classe operacional, classe mecânica, centro de custo e categoria — e devolve o que as linhas precisam:
    `{"tipos": {...}, "entidades": {(chave, id_da_entidade): objeto}, "avisos": [textos]}`.

    Roda ANTES de reservar a importação e FORA da transação de cada linha, de propósito: se uma linha
    desfizesse a sua transação depois de criar uma classe, a classe sumiria e o cache das outras linhas
    apontaria para um registro que não existe. É idempotente (reconhece pelo código do ERP): repetir não duplica."""
    tipos: dict = {}
    entidades: dict = {}
    avisos: list[str] = []
    grafias_avisadas: set = set()
    with transaction.atomic():
        for linha in linhas:
            d = linha.dados
            categoria = _categoria_da_linha(d, usuario, tipos)
            for campo, (chave, _) in _CATALOGO_POR_CAMPO.items():
                entidade = d[campo]
                if not (entidade["codigo"] or entidade["nome"]):
                    continue
                identidade = (campo, entidade["codigo"] or _chave(entidade["nome"]))
                if identidade in entidades:
                    # o mesmo código com outra grafia (o ERP escreve "CAMINHAO - GRAOS" e "CAMINHÕES GRÃOS" para o
                    # centro 32119): fica o primeiro nome e a divergência é avisada UMA vez — nunca renomeia em silêncio
                    grafia = (identidade, _chave(entidade["nome"]))
                    if entidade["nome"] and _chave(entidade["nome"]) != _chave(entidades[identidade].nome) and grafia not in grafias_avisadas:
                        grafias_avisadas.add(grafia)
                        avisos.append(
                            f"{classificacao.CATALOGOS[chave].rotulo.capitalize()} {entidade['codigo']}: a planilha também traz "
                            f"“{entidade['nome']}”, o cadastro tem “{entidades[identidade].nome}” (mantido)."
                        )
                    continue
                extras = {}
                if campo == "empresa":
                    extras["natureza_padrao"] = d["natureza"]
                elif campo == "classe_operacional":
                    extras["categoria"] = categoria.pk
                objeto, _, aviso = classificacao.obter_ou_criar(chave, entidade["codigo"], entidade["nome"], usuario, **extras)
                entidades[identidade] = objeto
                if aviso:
                    avisos.append(aviso)
    return {"tipos": tipos, "entidades": entidades, "avisos": avisos}


def _entidade_resolvida(preparados: dict, dados: dict, campo: str):
    entidade = dados[campo]
    if not (entidade["codigo"] or entidade["nome"]):
        return None
    return preparados["entidades"].get((campo, entidade["codigo"] or _chave(entidade["nome"])))


def _mensagens(exc: ValidationError) -> str:
    if hasattr(exc, "error_dict"):
        return " ".join(m for mensagens in exc.message_dict.values() for m in mensagens)
    return " ".join(exc.messages)


def _dados_do_veiculo(linha, preparados: dict) -> dict:
    d = linha.dados
    return {
        "nome": d["nome"], "tipo": _categoria_da_linha(d, None, preparados["tipos"]).pk, "codigo_legado": d["codigo_legado"],
        "modelo": d["modelo"], "placa": d["placa"], "chassi_serie": d["chassi_serie"], "ano_modelo": d["ano_modelo"],
        "ano_fabricacao": d["ano_fabricacao"], "data_compra": d["data_compra"], "data_inicio": d["data_inicio"],
        "setor": d["setor"], "natureza": d["natureza"], "status": d["status"] or Status.ATIVO,
        "situacao_origem": d["situacao_origem"], "validacao_pendente": True,
        **{campo: getattr(_entidade_resolvida(preparados, d, campo), "pk", None) for campo in _CATALOGO_POR_CAMPO},
    }


def _importar_linha(linha: FrotaImportacaoEquipamentoLinha, importacao, usuario, preparados: dict) -> None:
    """Grava UMA linha válida no seu próprio `atomic()`: a que não passa vira `existente` (alguém cadastrou
    o código no meio-tempo) ou `rejeitada` (com o motivo da revalidação) e as demais seguem."""
    codigo = linha.dados["codigo_legado"]
    dono = FrotaVeiculo.objects.filter(codigo_legado=codigo).first()
    if dono is not None:
        linha.situacao, linha.veiculo = Situacao.EXISTENTE, dono
        linha.motivos = ["Revalidação ao confirmar: o equipamento foi cadastrado depois da prévia — não é alterado."]
        linha.save(update_fields=["situacao", "veiculo", "motivos"])
        return
    try:
        with transaction.atomic():
            veiculo = frota.criar_veiculo(_dados_do_veiculo(linha, preparados), usuario, importacao=importacao)
    except ValidationError as exc:
        linha.situacao, linha.motivos = Situacao.REJEITADA, [f"Revalidação ao confirmar: {_mensagens(exc)}"]
        linha.save(update_fields=["situacao", "motivos"])
        return
    linha.situacao, linha.veiculo = Situacao.IMPORTADA, veiculo
    linha.save(update_fields=["situacao", "veiculo"])


def _atualizar_linha(linha: FrotaImportacaoEquipamentoLinha, importacao, usuario, preparados: dict) -> None:
    """Atualização AUTORIZADA de um equipamento já cadastrado: só os `CAMPOS_DO_ERP` que a planilha traz e
    que mudam (`_diferencas`, recalculadas contra o cadastro de AGORA). Sem diferença, a linha segue como
    `existente`; célula vazia nunca apaga dado."""
    veiculo = FrotaVeiculo.objects.select_related("empresa", "classe_operacional", "classe_mecanica", "centro_custo").filter(pk=linha.veiculo_id).first()
    if veiculo is None:
        return
    diferencas = _diferencas(linha.dados, veiculo)
    if not diferencas:
        return
    novos = {}
    for campo in diferencas:
        if campo in _CATALOGO_POR_CAMPO:
            novos[campo] = getattr(_entidade_resolvida(preparados, linha.dados, campo), "pk", None)
        else:
            novos[campo] = linha.dados[campo]
    try:
        with transaction.atomic():
            frota.editar_veiculo(veiculo, novos, usuario, importacao=importacao)
    except ValidationError as exc:
        linha.motivos = [*linha.motivos, f"Atualização recusada: {_mensagens(exc)}"]
        linha.save(update_fields=["motivos"])
        return
    linha.situacao = Situacao.ATUALIZADA
    linha.dados = {**linha.dados, "aplicadas": diferencas}
    linha.save(update_fields=["situacao", "dados"])


def confirmar(importacao: FrotaImportacaoEquipamentos, usuario, *, atualizar_existentes: bool = False) -> FrotaImportacaoEquipamentos:
    """Grava os equipamentos da prévia. Recusa (`ValidationError`) importação já confirmada ou cancelada,
    prévia EXPIRADA (24 h) e mapeamento ainda não conferido. `usuario` é obrigatório.

    Ordem: (1) cria os cadastros de apoio que faltam (idempotente, fora da transação por linha); (2) RESERVA
    a importação por atualização condicional `estado=previa` — dois cliques simultâneos não importam duas
    vezes; (3) REVALIDA e grava cada linha `valida` no seu `atomic()` (outra pessoa pode ter cadastrado o
    código no meio-tempo: a linha vira `existente`, e a que não passa vira `rejeitada` com o motivo, sem
    derrubar as outras). Cada equipamento nasce com a validação PENDENTE.

    `atualizar_existentes=True` é a autorização de mexer em quem já está cadastrado: só os campos do ERP, só
    onde a planilha tem valor, cada mudança com o seu evento. Sem ela, equipamento existente fica como está."""
    if usuario is None or not getattr(usuario, "pk", None):
        raise ValidationError("Informe quem está confirmando a importação.")
    atual = _exigir_previa_aberta(importacao)
    if not atual.mapeamento.get("conferido"):
        raise ValidationError("Confira o mapeamento antes de confirmar a importação.")
    validas = list(atual.itens.filter(situacao=Situacao.VALIDA).order_by("numero"))
    a_atualizar = list(atual.itens.filter(situacao=Situacao.EXISTENTE, veiculo__isnull=False).order_by("numero")) if atualizar_existentes else []
    preparados = _preparar_cadastros([*validas, *a_atualizar], usuario)

    reservou = FrotaImportacaoEquipamentos.objects.filter(pk=atual.pk, estado=Estado.PREVIA).update(
        estado=Estado.CONFIRMADA, confirmada_por=usuario, confirmada_em=timezone.now(), atualizar_existentes=atualizar_existentes,
    )
    if not reservou:
        raise ValidationError("Esta importação acabou de ser confirmada por outra pessoa.")
    atual.refresh_from_db()
    try:
        for linha in validas:
            _importar_linha(linha, atual, usuario, preparados)
        for linha in a_atualizar:
            _atualizar_linha(linha, atual, usuario, preparados)
    finally:
        mapeamento = dict(atual.mapeamento)
        mapeamento["avisos_de_cadastro"] = preparados["avisos"]
        atual.mapeamento = mapeamento
        atual.save(update_fields=["mapeamento"])
        _gravar_totais(atual)
    atual.refresh_from_db()
    return atual


def cancelar(importacao: FrotaImportacaoEquipamentos, usuario=None) -> FrotaImportacaoEquipamentos:
    """Descarta a prévia (estado `cancelada`; as linhas ficam como registro). Só vale em prévia."""
    mudou = FrotaImportacaoEquipamentos.objects.filter(pk=importacao.pk, estado=Estado.PREVIA).update(estado=Estado.CANCELADA)
    if not mudou:
        atual = FrotaImportacaoEquipamentos.objects.get(pk=importacao.pk)
        raise ValidationError(f"Esta importação já foi {atual.get_estado_display().lower()}.")
    importacao.refresh_from_db()
    return importacao


# ═════════════════════════════════════════════════════════════
# Leituras e relatório
# ═════════════════════════════════════════════════════════════

def listar_importacoes(limite: int = 50):
    """As importações mais recentes (QuerySet, com `criada_por` e `confirmada_por` carregados)."""
    return FrotaImportacaoEquipamentos.objects.select_related("criada_por", "confirmada_por").order_by("-criada_em", "-pk")[: max(1, limite)]


def arquivo_ja_importado(importacao: FrotaImportacaoEquipamentos) -> FrotaImportacaoEquipamentos | None:
    """Outra importação CONFIRMADA com o mesmo arquivo (mesmo SHA-256) — para avisar "este arquivo já foi
    importado em 12/11" antes de confirmar. `None` se não houver."""
    return (
        FrotaImportacaoEquipamentos.objects.filter(sha256=importacao.sha256, estado=Estado.CONFIRMADA)
        .exclude(pk=importacao.pk).order_by("-confirmada_em").first()
    )


def relatorio(importacao: FrotaImportacaoEquipamentos) -> dict:
    """O relatório auditado, em dados (a tela e o Excel usam o mesmo): a importação, quem e quando, os
    totais (das LINHAS), cada linha (`linha_do_relatorio`) e o mapeamento usado."""
    importacao = FrotaImportacaoEquipamentos.objects.select_related("criada_por", "confirmada_por").get(pk=importacao.pk)
    linhas = [linha_do_relatorio(l) for l in linhas_da_importacao(importacao)]
    return {
        "importacao": importacao, "arquivo": importacao.arquivo_nome, "sha256": importacao.sha256, "formato": importacao.formato,
        "estado": importacao.estado, "estado_rotulo": importacao.get_estado_display(),
        "criada_por": importacao.criada_por.get_username() if importacao.criada_por_id else "", "criada_em": importacao.criada_em,
        "confirmada_por": importacao.confirmada_por.get_username() if importacao.confirmada_por_id else "",
        "confirmada_em": importacao.confirmada_em, "atualizar_existentes": importacao.atualizar_existentes,
        "totais": {
            "linhas": len(linhas), "validas": sum(l["situacao"] == Situacao.VALIDA for l in linhas),
            "importados": sum(l["situacao"] == Situacao.IMPORTADA for l in linhas),
            "atualizados": sum(l["situacao"] == Situacao.ATUALIZADA for l in linhas),
            "existentes": sum(l["situacao"] == Situacao.EXISTENTE for l in linhas),
            "rejeitados": sum(l["situacao"] == Situacao.REJEITADA for l in linhas),
            "avisos": sum(bool(l["conferencias"]) for l in linhas),
        },
        "linhas": linhas, "mapeamento": importacao.mapeamento,
    }


def motivos_de_rejeicao(importacao: FrotaImportacaoEquipamentos) -> list[tuple[str, int]]:
    """Os motivos das linhas rejeitadas, do mais frequente ao menos, sem o trecho que varia (a linha, o
    código) — é a resposta a "por que rejeitou?" no resultado."""
    contagem: Counter = Counter()
    for motivos in importacao.itens.filter(situacao=Situacao.REJEITADA).values_list("motivos", flat=True):
        for motivo in motivos or []:
            contagem[motivo] += 1
    return contagem.most_common()


def exportar_relatorio_xlsx(importacao: FrotaImportacaoEquipamentos, *, so_inconsistencias: bool = False) -> bytes:
    """O relatório em Excel: "Resumo" (arquivo, SHA-256, estado, quem e quando, totais), "Linhas" (linha a
    linha: situação, dados, motivos, conferências, equipamento criado), "Inconsistências" (uma linha por
    conferência ou motivo de rejeição) e "Mapeamento" (o que foi decidido). `so_inconsistencias=True` leva
    só Resumo e Inconsistências — é o "exportar relatório de inconsistências" do passo de confirmação. TODA
    célula de texto passa por `texto_seguro` e é gravada como TEXTO: valor que começa com `=`, `+`, `-` ou
    `@` não vira fórmula."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    dados = relatorio(importacao)
    wb = Workbook()
    resumo_ws = wb.active
    resumo_ws.title = "Resumo"

    def texto(planilha, linha, coluna, valor):
        celula = planilha.cell(row=linha, column=coluna)
        celula.value = leitura.texto_seguro(valor)
        celula.data_type = "s"
        return celula

    def quando(momento):
        return timezone.localtime(momento).strftime("%d/%m/%Y %H:%M") if momento else ""

    itens = [
        ("Arquivo", dados["arquivo"]), ("SHA-256", dados["sha256"]), ("Formato", dados["formato"]),
        ("Estado", dados["estado_rotulo"]), ("Enviada por", dados["criada_por"]), ("Enviada em", quando(dados["criada_em"])),
        ("Confirmada por", dados["confirmada_por"]), ("Confirmada em", quando(dados["confirmada_em"])),
        ("Atualização de cadastrados autorizada", "sim" if dados["atualizar_existentes"] else "não"), ("Gerado em", quando(timezone.now())),
        ("", ""), ("Linhas analisadas", dados["totais"]["linhas"]), ("Importados", dados["totais"]["importados"]),
        ("Atualizados", dados["totais"]["atualizados"]), ("Ignorados por duplicidade", dados["totais"]["existentes"]),
        ("Rejeitados", dados["totais"]["rejeitados"]), ("Válidos (ainda não importados)", dados["totais"]["validas"]),
        ("Linhas com conferência", dados["totais"]["avisos"]),
    ]
    for i, (rotulo, valor) in enumerate(itens, start=1):
        texto(resumo_ws, i, 1, rotulo).font = Font(bold=True)
        if isinstance(valor, int):
            resumo_ws.cell(row=i, column=2, value=valor)
        else:
            texto(resumo_ws, i, 2, valor)
    resumo_ws.column_dimensions["A"].width = 42
    resumo_ws.column_dimensions["B"].width = 70

    if not so_inconsistencias:
        ws = wb.create_sheet("Linhas")
        cabecalho = [
            "Linha", "Situação", "Equipamento (ERP)", "Modelo", "Classe operacional", "Empresa", "Natureza", "Setor", "Placa", "Chassi",
            "Ano-modelo", "Ano de fabricação", "Data de compra", "Situação na planilha", "Status", "Motivos", "Conferências", "Cadastro no Zelo",
        ]
        for coluna, titulo in enumerate(cabecalho, start=1):
            texto(ws, 1, coluna, titulo).font = Font(bold=True)
        for i, l in enumerate(dados["linhas"], start=2):
            ws.cell(row=i, column=1, value=l["numero"])
            for coluna, valor in enumerate((
                l["situacao_rotulo"], l["codigo_legado"], l["modelo"], l["classe_operacional"], l["empresa"], l["natureza_rotulo"], l["setor"],
                l["placa"], l["chassi_serie"], l["ano_modelo"] or "", l["ano_fabricacao"] or "", l["data_compra"] or "",
                l["situacao_origem"], dict(Status.choices).get(l["status"], ""), " | ".join(l["motivos"]),
                " | ".join(f"{c['titulo']}: {c['detalhe']}" for c in l["conferencias"]), l["veiculo_codigo"],
            ), start=2):
                texto(ws, i, coluna, valor)
        for coluna, largura in zip("ABCDEFGHIJKLMNOPQR", (7, 14, 18, 26, 26, 18, 10, 16, 12, 22, 11, 12, 14, 16, 12, 60, 80, 14)):
            ws.column_dimensions[coluna].width = largura
        ws.freeze_panes = "A2"

    ws = wb.create_sheet("Inconsistências")
    for coluna, titulo in enumerate(("Linha", "Equipamento (ERP)", "Modelo", "Tipo", "Gravidade", "Regra", "Detalhe"), start=1):
        texto(ws, 1, coluna, titulo).font = Font(bold=True)
    linha_atual = 2
    for l in dados["linhas"]:
        achados = [("Rejeição", "erro", "Linha rejeitada", m) for m in l["motivos"] if l["situacao"] == Situacao.REJEITADA]
        achados += [("Conferência", c["gravidade"], c["titulo"], c["detalhe"]) for c in l["conferencias"]]
        for tipo, gravidade, regra, detalhe in achados:
            ws.cell(row=linha_atual, column=1, value=l["numero"])
            for coluna, valor in enumerate((l["codigo_legado"], l["modelo"], tipo, gravidade, regra, detalhe), start=2):
                texto(ws, linha_atual, coluna, valor)
            linha_atual += 1
    for coluna, largura in zip("ABCDEFG", (7, 18, 28, 13, 11, 34, 110)):
        ws.column_dimensions[coluna].width = largura
    ws.freeze_panes = "A2"

    if not so_inconsistencias:
        ws = wb.create_sheet("Mapeamento")
        for coluna, titulo in enumerate(("Campo do Zelo", "Coluna da planilha", "Tipo", "Obrigatório", "Destino"), start=1):
            texto(ws, 1, coluna, titulo).font = Font(bold=True)
        cabecalho_arquivo = dados["mapeamento"].get("cabecalho", [])
        indices = dados["mapeamento"].get("colunas", {})
        for i, c in enumerate(COLUNAS, start=2):
            origem = cabecalho_arquivo[indices[c.campo]] if c.campo in indices and indices[c.campo] < len(cabecalho_arquivo) else "(não mapeada)"
            for coluna, valor in enumerate((c.rotulo, origem, c.tipo, "sim" if c.obrigatoria else "não", c.destino), start=1):
                texto(ws, i, coluna, valor)
        deslocamento = len(COLUNAS) + 3
        texto(ws, deslocamento, 1, "Decisões sobre os valores").font = Font(bold=True)
        rotulos = {"situacao": "Situação → status", "empresa": "Empresa → natureza", "classe_operacional": "Classe operacional → categoria"}
        rotulo_do_destino = {**dict(Status.choices), **dict(FrotaNatureza.choices)}
        for campo, mapa in (dados["mapeamento"].get("valores") or {}).items():
            for valor, decisao in sorted(mapa.items()):
                deslocamento += 1
                texto(ws, deslocamento, 1, rotulos.get(campo, campo))
                texto(ws, deslocamento, 2, valor or "(vazio)")
                texto(ws, deslocamento, 3, rotulo_do_destino.get(decisao, decisao))
        for coluna, largura in zip("ABCDE", (34, 34, 14, 12, 70)):
            ws.column_dimensions[coluna].width = largura

    saida = io.BytesIO()
    wb.save(saida)
    return saida.getvalue()
