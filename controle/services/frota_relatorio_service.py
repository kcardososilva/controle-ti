"""
Relatórios e exportações de Frotas e Maquinários.

Um relatório é UMA leitura da base, exportável: nada aqui grava, e nenhum número é calculado de novo — a lista
vem de `frota_service.listar_veiculos` (a mesma da tela, com os mesmos filtros), as conferências de
`frota_qualidade_service` e os indicadores de `frota_service.indicadores_equipamentos`. Exportar o que a tela
mostra com outra regra faria o Excel contradizer a lista.

Todo `.xlsx` sai com o texto GRAVADO COMO TEXTO (`leitura.texto_seguro` + tipo "string"): valor que começa com
`=`, `+`, `-` ou `@` não vira fórmula ao abrir o arquivo.
"""

from __future__ import annotations

import io
from datetime import date

from django.db.models import Count, F, Q
from django.utils import timezone

from ProjetoEstoque.models import FrotaNatureza, FrotaVeiculo
from services import frota_qualidade_service as qualidade
from services import frota_service as frota
from services import planilha_leitura as leitura

Status = FrotaVeiculo.Status


# ═════════════════════════════════════════════════════════════
# Quadros da página de Relatórios
# ═════════════════════════════════════════════════════════════

def _contagens() -> dict:
    """As contagens que todo quadro repete, sobre a MESMA base da lista (todos os equipamentos, desativados inclusive: o
    número que se clica é o número que a lista mostra)."""
    return {
        "total": Count("pk"), "proprios": Count("pk", filter=Q(natureza=FrotaNatureza.PROPRIO)),
        "terceiros": Count("pk", filter=Q(natureza=FrotaNatureza.TERCEIRO)),
        "em_uso": Count("pk", filter=Q(status=Status.ATIVO)), "em_manutencao": Count("pk", filter=Q(status=Status.MANUTENCAO)),
        "a_validar": Count("pk", filter=Q(validacao_pendente=True)),
    }


def quadro_por_categoria() -> list[dict]:
    """O inventário por CATEGORIA (o agrupamento superior), da mais numerosa à menos:
    `[{"tipo_id", "categoria", "total", "proprios", "terceiros", "em_uso", "em_manutencao", "a_validar"}]`. Categoria sem
    equipamento não aparece: é leitura do que existe, não lista de cadastro. (A chave é `categoria` e não `nome`:
    `nome` é campo do equipamento e o Django recusa uma anotação com o nome de um campo.)"""
    return list(FrotaVeiculo.objects.values("tipo_id", categoria=F("tipo__nome")).annotate(**_contagens()).order_by("-total", "categoria"))


def quadro_por_classe() -> list[dict]:
    """O inventário por CLASSE OPERACIONAL do ERP (com o nome original) e a categoria que a agrupa, da mais numerosa à
    menos: `[{"classe_id", "classe", "categoria", "total", "proprios", "terceiros", "em_uso", "em_manutencao", "a_validar"}]`.
    `classe_id` nulo é "sem classe operacional" (o equipamento cadastrado à mão)."""
    return list(
        FrotaVeiculo.objects.values(classe_id=F("classe_operacional_id"), classe=F("classe_operacional__nome"), categoria=F("tipo__nome"))
        .annotate(**_contagens()).order_by("-total", "classe", "categoria")
    )


def conferencias_do_cadastro() -> list[dict]:
    """As pendências cadastrais com quantos equipamentos cada uma alcança, para a página de Relatórios apontar para a lista
    filtrada: `[{"chave", "rotulo", "n"}]`, só as que têm algum. A contagem é a da lista (`frota_service.listar_veiculos`)."""
    contagem = qualidade.contar_pendencias(FrotaVeiculo.objects.all())
    return [{"chave": chave, "rotulo": rotulo, "n": contagem[chave]} for chave, (rotulo, _) in qualidade.PENDENCIAS.items() if contagem[chave]]

# (título da coluna, largura, função que tira o valor do equipamento)
COLUNAS_DO_INVENTARIO = (
    ("Código original (ERP)", 16, lambda v: v.codigo_legado),
    ("Identificador interno", 16, lambda v: v.codigo),
    ("Equipamento", 30, lambda v: v.nome),
    ("Categoria", 18, lambda v: v.tipo.nome),
    ("Classe operacional", 26, lambda v: v.classe_operacional.nome if v.classe_operacional_id else ""),
    ("Classe mecânica", 22, lambda v: v.classe_mecanica.nome if v.classe_mecanica_id else ""),
    ("Fabricante", 16, lambda v: v.fabricante),
    ("Modelo", 26, lambda v: v.modelo),
    ("Empresa", 18, lambda v: v.empresa.nome if v.empresa_id else ""),
    ("Natureza", 11, lambda v: v.get_natureza_display()),
    ("Setor", 14, lambda v: v.setor),
    ("Centro de custo (código)", 12, lambda v: v.centro_custo.codigo_erp if v.centro_custo_id else ""),
    ("Centro de custo", 30, lambda v: v.centro_custo.nome if v.centro_custo_id else ""),
    ("Fazenda", 18, lambda v: v.localidade.local if v.localidade_id else ""),
    ("Responsável", 24, lambda v: v.responsavel.nome if v.responsavel_id else ""),
    ("Placa", 12, lambda v: v.placa),
    ("Chassi / nº de série", 22, lambda v: v.chassi_serie),
    ("RENAVAM", 14, lambda v: v.renavam),
    ("Ano de fabricação", 11, lambda v: v.ano_fabricacao),
    ("Ano-modelo", 11, lambda v: v.ano_modelo),
    ("Data de compra", 13, lambda v: v.data_compra),
    ("Data de início", 13, lambda v: v.data_inicio),
    ("Situação", 14, lambda v: v.get_status_display()),
    ("Validação do inventário", 14, lambda v: "A validar" if v.validacao_pendente else "Validado"),
    ("Situação na planilha", 14, lambda v: v.situacao_origem),
    ("Observações", 40, lambda v: v.observacoes),
)


def _gravar_celula(planilha, linha: int, coluna: int, valor):
    celula = planilha.cell(row=linha, column=coluna)
    if isinstance(valor, (int, date)) and not isinstance(valor, bool):
        celula.value = valor
        if isinstance(valor, date):
            celula.number_format = "DD/MM/YYYY"
    else:
        celula.value = leitura.texto_seguro(valor)
        celula.data_type = "s"
    return celula


def _cabecalho(planilha, titulos_e_larguras):
    from openpyxl.styles import Font

    for coluna, (titulo, largura) in enumerate(titulos_e_larguras, start=1):
        celula = planilha.cell(row=1, column=coluna)
        celula.value = titulo
        celula.font = Font(bold=True)
        planilha.column_dimensions[celula.column_letter].width = largura
    planilha.freeze_panes = "A2"


def exportar_equipamentos_xlsx(filtros: dict | None = None) -> bytes:
    """O inventário em Excel, no recorte da tela: `filtros` são os mesmos de `frota_service.listar_veiculos`.
    Uma linha por equipamento, com TODAS as informações do cadastro (a tela só mostra as principais). Aba
    "Inventário" e aba "Resumo" (filtros usados, quando e quantos)."""
    from openpyxl import Workbook

    filtros = dict(filtros or {})
    filtros.setdefault("ordem", "legado")
    veiculos = list(frota.listar_veiculos(filtros).select_related("tipo", "empresa", "localidade", "responsavel"))
    wb = Workbook()
    ws = wb.active
    ws.title = "Inventário"
    _cabecalho(ws, [(t, l) for t, l, _ in COLUNAS_DO_INVENTARIO])
    for linha, v in enumerate(veiculos, start=2):
        for coluna, (_, _, extrair) in enumerate(COLUNAS_DO_INVENTARIO, start=1):
            _gravar_celula(ws, linha, coluna, extrair(v))

    resumo = wb.create_sheet("Resumo")
    usados = {k: v for k, v in filtros.items() if v and k not in ("ordem", "dir")}
    itens = [
        ("Gerado em", timezone.localtime().strftime("%d/%m/%Y %H:%M")),
        ("Equipamentos neste arquivo", len(veiculos)),
        ("Filtros aplicados", ", ".join(f"{k}={v}" for k, v in sorted(usados.items())) or "nenhum"),
    ]
    for i, (rotulo, valor) in enumerate(itens, start=1):
        _gravar_celula(resumo, i, 1, rotulo)
        _gravar_celula(resumo, i, 2, valor)
    resumo.column_dimensions["A"].width = 30
    resumo.column_dimensions["B"].width = 80
    saida = io.BytesIO()
    wb.save(saida)
    return saida.getvalue()


def nome_do_arquivo(prefixo: str, extensao: str = "xlsx") -> str:
    return f"{prefixo}_{timezone.localdate():%Y%m%d}.{extensao}"


def exportar_eventos_xlsx(filtros: dict | None = None, *, so_movimentacoes: bool) -> bytes:
    """As movimentações (`so_movimentacoes=True`) ou o histórico da frota em Excel, no recorte da tela. Uma linha por
    MUDANÇA (de/para por campo); evento sem de/para (cadastro, validação) sai numa linha só, com o resumo."""
    from openpyxl import Workbook

    from services import frota_historico_service as historico

    wb = Workbook()
    ws = wb.active
    ws.title = "Movimentações" if so_movimentacoes else "Histórico"
    colunas = (
        ("Quando", 17), ("Código original (ERP)", 16), ("Identificador interno", 16), ("Equipamento", 30), ("Natureza", 11),
        ("Evento", 28), ("Campo", 20), ("De", 26), ("Para", 26), ("Motivo / detalhe", 50), ("Registrado por", 16),
    )
    _cabecalho(ws, colunas)
    linha = 2
    for evento in historico.eventos_da_frota(filtros, so_movimentacoes=so_movimentacoes):
        d = historico.descrever(evento)
        v = evento.veiculo
        base = (
            timezone.localtime(d["quando"]).strftime("%d/%m/%Y %H:%M"), v.codigo_legado, v.codigo, v.nome, v.get_natureza_display(),
            d["titulo"],
        )
        itens = d["mudancas"] or [{"rotulo": "", "de": "", "para": ""}]
        for m in itens:
            detalhe = d["motivo"] or ("" if d["mudancas"] else d["resumo"])
            for coluna, valor in enumerate((*base, m["rotulo"], m["de"] if d["mudancas"] else "", m["para"] if d["mudancas"] else "", detalhe, d["por"]), start=1):
                _gravar_celula(ws, linha, coluna, valor)
            linha += 1
    saida = io.BytesIO()
    wb.save(saida)
    return saida.getvalue()
