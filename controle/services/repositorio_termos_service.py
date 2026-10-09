# -*- coding: utf-8 -*-
"""
Repositório de Termos — a visão de ARQUIVO dos termos de responsabilidade.

Duas fontes, um acervo
----------------------
Um termo de responsabilidade chega ao sistema por dois caminhos, e até aqui a
tela só conhecia um deles:

  • **anexo** — o TI colheu a assinatura no papel, escaneou e anexou em
    `MovimentacaoItem.termo_pdf` no fluxo normal de transferência;
  • **digital** — o colaborador assinou no tablet e o sistema emitiu o PDF
    (`TermoAssinatura`, com código de verificação e SHA-256).

O termo digital JÁ aparecia na tela, mas disfarçado: `registrar_assinatura`
grava o PDF no `termo_pdf` de cada movimentação vinculada, então ele entrava
como uma linha de movimentação igual a qualquer outra — sem dizer que era
assinatura eletrônica, sem o código de verificação, sem a conferência de
integridade e sem link para o ato. E o que NÃO tinha movimentação vinculada
(assinatura cuja movimentação falhou ao ser registrada — caminho que
`_registrar_movimentacoes` loga e segue) não aparecia de forma alguma: ficava
num acervo que o repositório não indexava.

Aqui as duas fontes viram uma lista única de `LinhaTermo`, e a de-duplicação é
a parte que importa: toda movimentação coberta por um `TermoAssinatura` é
REMOVIDA da fonte "anexo", porque o documento é o mesmo arquivo. Sem isso, um
termo digital de 3 equipamentos apareceria 4 vezes (uma como ato, três como
movimentações). O ato é a linha autoritativa — ele é um documento só, assinado
uma vez, cobrindo os 3 itens.

Pastas
------
O acervo é apresentado como pastas porque a pergunta real do TI é de arquivo
("me dá o termo do João"), não de relatório. O eixo PADRÃO é o colaborador: o
termo de responsabilidade é um acordo entre a empresa e a PESSOA, e é por ela
que alguém procura. Fornecedor existe como eixo (foi pedido), mas é quem
VENDEU o equipamento — raramente o caminho de busca de um termo, e por isso
não é o padrão.

Um termo pode pertencer a MAIS DE UMA pasta no eixo fornecedor (termo digital
com equipamentos de fornecedores diferentes), então `chaves_de_pasta` devolve
lista, não valor único. A soma das pastas pode passar o total do acervo nesse
eixo — é a verdade do agrupamento, e a tela diz isso em vez de esconder.

Escala
------
O merge e o agrupamento acontecem em Python, sobre o acervo filtrado inteiro
(hoje ~670 linhas). É deliberado: a de-duplicação entre as duas fontes e a
pertinência a múltiplas pastas não se expressam numa `GROUP BY` sem virar duas
consultas que precisam concordar entre si — e discordância silenciosa entre
elas é pior do que o custo de materializar algumas centenas de dicionários. Se
o acervo passar de alguns milhares, o lugar de mexer é `_linhas`, movendo a
contagem das pastas para agregação no banco e mantendo o merge só para o
conteúdo da pasta aberta.
"""

from dataclasses import dataclass, field
from datetime import datetime

from django.db.models import Q
from django.urls import reverse


# ---------------------------------------------------------------------------
# Eixos de organização
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Eixo:
    slug: str
    rotulo: str
    icone: str
    descricao: str
    # Pasta pode conter o mesmo termo em mais de um lugar? (ver docstring)
    multiplo: bool = False


EIXOS = (
    Eixo("colaborador", "Colaborador", "fa-user",
         "Uma pasta por pessoa — como o termo é procurado na prática."),
    Eixo("centro_custo", "Centro de custo", "fa-sitemap",
         "Uma pasta por departamento do colaborador."),
    Eixo("periodo", "Ano e mês", "fa-calendar-days",
         "Arquivo cronológico, do mais recente para o mais antigo."),
    Eixo("fornecedor", "Fornecedor", "fa-truck-field",
         "Uma pasta por fornecedor do equipamento entregue.", multiplo=True),
)

EIXO_PADRAO = "colaborador"

_EIXOS_POR_SLUG = {eixo.slug: eixo for eixo in EIXOS}

_MESES = (
    "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
)


def eixo_valido(slug: str) -> Eixo:
    """O eixo pedido, ou o padrão. Nunca levanta: o slug vem da URL, e um
    `?agrupar=qualquer-coisa` tem de cair numa tela que funciona, não num 500."""
    return _EIXOS_POR_SLUG.get((slug or "").strip(), _EIXOS_POR_SLUG[EIXO_PADRAO])


# ---------------------------------------------------------------------------
# Linha do acervo
# ---------------------------------------------------------------------------

@dataclass
class LinhaTermo:
    """
    Um DOCUMENTO do acervo — não uma movimentação.

    A distinção é o que faz a contagem da tela bater com a realidade: um termo
    digital de 3 equipamentos é UMA linha (um papel, uma assinatura), e não
    três.
    """
    origem: str                 # "digital" | "anexo"
    chave: str                  # identificador estável para a UI ("d-12", "a-345")
    data: datetime
    tipo: str                   # "entrega" | "devolucao"
    tipo_label: str
    pdf_url: str
    detalhe_url: str

    colaborador_id: int | None = None
    colaborador_nome: str = "Colaborador não informado"
    centro_custo_id: int | None = None
    centro_custo_nome: str = "Sem centro de custo"

    equipamentos: list = field(default_factory=list)   # [{nome, serie}]
    fornecedores: list = field(default_factory=list)   # [{id, nome}]

    # Só em termo digital:
    codigo_verificacao: str = ""
    desligamento: bool = False
    compartilhado: bool = False
    movimentacoes_cobertas: int = 0

    @property
    def digital(self) -> bool:
        return self.origem == "digital"

    @property
    def equipamento_resumo(self) -> str:
        nomes = [e["nome"] for e in self.equipamentos if e.get("nome")]
        if not nomes:
            return "—"
        if len(nomes) == 1:
            return nomes[0]
        return f"{nomes[0]} + {len(nomes) - 1}"

    @property
    def fornecedor_resumo(self) -> str:
        nomes = [f["nome"] for f in self.fornecedores if f.get("nome")]
        return " · ".join(nomes) if nomes else "Sem fornecedor"

    def chaves_de_pasta(self, eixo: Eixo) -> list[tuple[str, str]]:
        """(chave, rótulo) de cada pasta a que esta linha pertence."""
        if eixo.slug == "colaborador":
            return [(str(self.colaborador_id or 0), self.colaborador_nome)]

        if eixo.slug == "centro_custo":
            return [(str(self.centro_custo_id or 0), self.centro_custo_nome)]

        if eixo.slug == "periodo":
            return [(
                f"{self.data:%Y-%m}",
                f"{_MESES[self.data.month - 1].capitalize()} de {self.data:%Y}",
            )]

        # fornecedor: pode ser mais de um no mesmo termo digital.
        if not self.fornecedores:
            return [("0", "Sem fornecedor")]
        return [(str(f["id"] or 0), f["nome"]) for f in self.fornecedores]


# ---------------------------------------------------------------------------
# Filtros
# ---------------------------------------------------------------------------

@dataclass
class Filtros:
    q: str = ""
    tipo: str = ""
    origem: str = ""          # "digital" | "anexo" | ""
    fornecedor: str = ""
    data_inicio: str = ""
    data_fim: str = ""

    @classmethod
    def do_request(cls, request):
        g = request.GET.get
        return cls(
            q=(g("q") or "").strip(),
            tipo=(g("tipo") or "").strip(),
            origem=(g("origem") or "").strip(),
            fornecedor=(g("fornecedor") or "").strip(),
            data_inicio=(g("data_inicio") or "").strip(),
            data_fim=(g("data_fim") or "").strip(),
        )

    @property
    def algum(self) -> bool:
        return any([self.q, self.tipo, self.origem, self.fornecedor,
                    self.data_inicio, self.data_fim])


# ---------------------------------------------------------------------------
# Montagem do acervo
# ---------------------------------------------------------------------------

def _fornecedor_de(item) -> list[dict]:
    forn = getattr(item, "fornecedor", None)
    if forn is None:
        return []
    return [{"id": forn.pk, "nome": forn.nome}]


def _linha_de_assinatura(termo) -> LinhaTermo:
    """
    Linha a partir do SNAPSHOT do termo assinado, não das FKs atuais.

    `dados["equipamentos"]` é o que o colaborador viu e assinou; renomear o
    item depois não pode reescrever o conteúdo de um documento firmado (mesmo
    princípio de `TermoAssinatura.equipamentos_resumo`). As FKs servem só para
    o que é navegação — fornecedor, para a pasta.
    """
    dados = termo.dados or {}
    equipamentos = [
        {"nome": (e.get("nome") or "—"), "serie": (e.get("numero_serie") or "")}
        for e in (dados.get("equipamentos") or [])
    ]

    fornecedores = {}
    compartilhado = False
    for item in termo.itens.all():
        for forn in _fornecedor_de(item):
            fornecedores[forn["id"]] = forn
        compartilhado = compartilhado or bool(getattr(item, "compartilhado", False))
    # O snapshot também marca compartilhado (e é ele que valia no ato).
    compartilhado = compartilhado or any(
        e.get("compartilhado") for e in (dados.get("equipamentos") or [])
    )

    colaborador = termo.colaborador
    centro_custo = getattr(colaborador, "centro_custo", None)

    return LinhaTermo(
        origem="digital",
        chave=f"d-{termo.pk}",
        data=termo.assinado_em or termo.created_at,
        tipo=termo.tipo,
        tipo_label="Entrega" if termo.tipo == "entrega" else "Devolução",
        pdf_url=reverse("termo_assinatura_pdf", args=[termo.pk]),
        detalhe_url=reverse("termo_assinatura_detalhe", args=[termo.pk]),
        colaborador_id=colaborador.pk,
        colaborador_nome=colaborador.nome or "Colaborador não informado",
        centro_custo_id=getattr(centro_custo, "pk", None),
        centro_custo_nome=getattr(centro_custo, "departamento", None) or "Sem centro de custo",
        equipamentos=equipamentos,
        fornecedores=list(fornecedores.values()),
        codigo_verificacao=termo.codigo_verificacao,
        desligamento=bool(termo.desligamento),
        compartilhado=compartilhado,
        # `len(...all())` e não `.count()`: o `.count()` vai ao banco mesmo com
        # `prefetch_related` em vigor, uma consulta por termo.
        movimentacoes_cobertas=len(termo.movimentacoes.all()),
    )


def _linha_de_movimentacao(mov) -> LinhaTermo:
    item = mov.item
    colaborador = mov.usuario
    centro_custo = getattr(colaborador, "centro_custo", None)

    return LinhaTermo(
        origem="anexo",
        chave=f"a-{mov.pk}",
        data=mov.created_at,
        tipo=mov.tipo_transferencia or "",
        tipo_label="Entrega" if mov.tipo_transferencia == "entrega" else "Devolução",
        pdf_url=mov.termo_pdf.url,
        detalhe_url=reverse("movimentacao_detail", args=[mov.pk]),
        colaborador_id=getattr(colaborador, "pk", None),
        colaborador_nome=getattr(colaborador, "nome", None) or "Colaborador não informado",
        centro_custo_id=getattr(centro_custo, "pk", None),
        centro_custo_nome=getattr(centro_custo, "departamento", None) or "Sem centro de custo",
        equipamentos=[{
            "nome": getattr(item, "nome", "—"),
            "serie": getattr(item, "numero_serie", "") or "",
        }],
        fornecedores=_fornecedor_de(item),
        compartilhado=bool(getattr(item, "compartilhado", False)),
    )


def _casa_com_busca(linha: LinhaTermo, termo_busca: str) -> bool:
    """Busca sobre a linha já montada.

    Em Python, e não no banco, por um motivo de correção: o termo digital
    guarda o nome do equipamento no SNAPSHOT (`dados`), não na FK — filtrar no
    banco por `itens__nome` acharia o nome ATUAL do item e não o que está
    escrito no documento assinado. O acervo tem centenas de linhas, não
    milhões; a diferença de custo é irrelevante e a de resultado não é.
    """
    alvo = termo_busca.lower()
    if alvo in linha.colaborador_nome.lower():
        return True
    if alvo in (linha.codigo_verificacao or "").lower():
        return True
    for equipamento in linha.equipamentos:
        if alvo in (equipamento.get("nome") or "").lower():
            return True
        if alvo in (equipamento.get("serie") or "").lower():
            return True
    return False


def _linhas(filtros: Filtros) -> list[LinhaTermo]:
    """O acervo inteiro, já de-duplicado, ordenado do mais recente ao mais antigo."""
    from ProjetoEstoque.models import (
        MovimentacaoItem,
        StatusAssinaturaTermoChoices,
        TermoAssinatura,
        TipoMovimentacaoChoices,
    )

    assinaturas = (
        TermoAssinatura.objects
        .filter(status=StatusAssinaturaTermoChoices.ASSINADO)
        .exclude(pdf="")
        .exclude(pdf__isnull=True)
        .select_related("colaborador", "colaborador__centro_custo")
        .prefetch_related("itens__fornecedor", "movimentacoes")
    )

    movimentacoes = (
        MovimentacaoItem.objects
        .filter(tipo_movimentacao=TipoMovimentacaoChoices.TRANSFERENCIA)
        .exclude(termo_pdf__isnull=True)
        .exclude(termo_pdf="")
        .select_related("item", "item__fornecedor", "usuario", "usuario__centro_custo")
    )

    if filtros.tipo:
        assinaturas = assinaturas.filter(tipo=filtros.tipo)
        movimentacoes = movimentacoes.filter(tipo_transferencia=filtros.tipo)
    if filtros.data_inicio:
        assinaturas = assinaturas.filter(assinado_em__date__gte=filtros.data_inicio)
        movimentacoes = movimentacoes.filter(created_at__date__gte=filtros.data_inicio)
    if filtros.data_fim:
        assinaturas = assinaturas.filter(assinado_em__date__lte=filtros.data_fim)
        movimentacoes = movimentacoes.filter(created_at__date__lte=filtros.data_fim)
    if filtros.fornecedor:
        assinaturas = assinaturas.filter(itens__fornecedor_id=filtros.fornecedor).distinct()
        movimentacoes = movimentacoes.filter(item__fornecedor_id=filtros.fornecedor)

    linhas = []
    cobertas = set()

    if filtros.origem != "anexo":
        for termo in assinaturas:
            linhas.append(_linha_de_assinatura(termo))
            cobertas.update(termo.movimentacoes.values_list("pk", flat=True))
    else:
        # Mesmo listando só anexos, as movimentações cobertas por assinatura
        # têm de sair: o PDF delas É o documento digital, e mostrá-las aqui
        # faria o filtro "anexo" exibir termos que foram assinados no tablet.
        cobertas.update(
            TermoAssinatura.objects
            .filter(status=StatusAssinaturaTermoChoices.ASSINADO)
            .values_list("movimentacoes__pk", flat=True)
        )
        cobertas.discard(None)

    if filtros.origem != "digital":
        for mov in movimentacoes.exclude(pk__in=cobertas):
            linhas.append(_linha_de_movimentacao(mov))

    if filtros.q:
        linhas = [linha for linha in linhas if _casa_com_busca(linha, filtros.q)]

    linhas.sort(key=lambda linha: linha.data, reverse=True)
    return linhas


# ---------------------------------------------------------------------------
# API usada pela view
# ---------------------------------------------------------------------------

@dataclass
class Pasta:
    chave: str
    rotulo: str
    total: int
    entregas: int
    devolucoes: int
    digitais: int
    ultima_data: datetime

    @property
    def inicial(self) -> str:
        return (self.rotulo or "?")[:1].upper()


def pastas(eixo: Eixo, linhas: list[LinhaTermo]) -> list[Pasta]:
    """Agrupa o acervo no eixo pedido."""
    acumulado: dict[str, dict] = {}

    for linha in linhas:
        for chave, rotulo in linha.chaves_de_pasta(eixo):
            alvo = acumulado.setdefault(chave, {
                "chave": chave, "rotulo": rotulo, "total": 0,
                "entregas": 0, "devolucoes": 0, "digitais": 0,
                "ultima_data": linha.data,
            })
            alvo["total"] += 1
            if linha.tipo == "entrega":
                alvo["entregas"] += 1
            else:
                alvo["devolucoes"] += 1
            if linha.digital:
                alvo["digitais"] += 1
            if linha.data > alvo["ultima_data"]:
                alvo["ultima_data"] = linha.data

    resultado = [Pasta(**dados) for dados in acumulado.values()]

    # Período ordena por data (arquivo cronológico); os outros eixos, por
    # nome — é como alguém procura uma pasta de pessoa ou de setor.
    if eixo.slug == "periodo":
        resultado.sort(key=lambda p: p.chave, reverse=True)
    else:
        resultado.sort(key=lambda p: p.rotulo.casefold())
    return resultado


def conteudo(eixo: Eixo, chave: str, linhas: list[LinhaTermo]) -> list[LinhaTermo]:
    """As linhas de UMA pasta."""
    return [
        linha for linha in linhas
        if any(c == chave for c, _ in linha.chaves_de_pasta(eixo))
    ]


def resumo(linhas: list[LinhaTermo]) -> dict:
    return {
        "total": len(linhas),
        "digitais": sum(1 for linha in linhas if linha.digital),
        "anexos": sum(1 for linha in linhas if not linha.digital),
        "entregas": sum(1 for linha in linhas if linha.tipo == "entrega"),
        "devolucoes": sum(1 for linha in linhas if linha.tipo != "entrega"),
        "colaboradores": len({linha.colaborador_id for linha in linhas if linha.colaborador_id}),
    }


def pendencias(filtros: Filtros, limite=None):
    """
    Transferências sem termo nenhum — nem anexo, nem assinatura.

    Movimentação coberta por `TermoAssinatura` nunca é pendência, mesmo que o
    `termo_pdf` dela esteja vazio: o documento existe e está assinado. Isso
    acontece de verdade quando `_anexar_pdf_nas_movimentacoes` encontra o campo
    já preenchido ou quando a gravação do arquivo falha — e cobrar o termo de
    novo mandaria o TI colher uma segunda assinatura do mesmo ato.
    """
    from ProjetoEstoque.models import (
        MovimentacaoItem,
        StatusAssinaturaTermoChoices,
        TermoAssinatura,
        TipoMovimentacaoChoices,
    )

    cobertas = set(
        TermoAssinatura.objects
        .filter(status=StatusAssinaturaTermoChoices.ASSINADO)
        .values_list("movimentacoes__pk", flat=True)
    )
    cobertas.discard(None)

    qs = (
        MovimentacaoItem.objects
        .filter(tipo_movimentacao=TipoMovimentacaoChoices.TRANSFERENCIA)
        .filter(Q(termo_pdf__isnull=True) | Q(termo_pdf=""))
        .exclude(pk__in=cobertas)
        .select_related("item", "usuario")
        .order_by("-created_at")
    )

    if filtros.tipo:
        qs = qs.filter(tipo_transferencia=filtros.tipo)
    if filtros.fornecedor:
        qs = qs.filter(item__fornecedor_id=filtros.fornecedor)
    if filtros.data_inicio:
        qs = qs.filter(created_at__date__gte=filtros.data_inicio)
    if filtros.data_fim:
        qs = qs.filter(created_at__date__lte=filtros.data_fim)

    total = qs.count()
    return (list(qs[:limite]) if limite else list(qs)), total


def acervo(filtros: Filtros) -> list[LinhaTermo]:
    """Ponto de entrada da view."""
    return _linhas(filtros)


def opcoes_fornecedor() -> list[dict]:
    """
    Fornecedores que têm ao menos um termo no acervo — não o cadastro inteiro,
    para o filtro não oferecer opção que devolve zero.

    Vai ao banco em vez de varrer as linhas já montadas: o combo é o mesmo
    independentemente dos filtros em vigor (senão o filtro apagaria as próprias
    opções ao ser aplicado), e remontar o acervo inteiro só para isso dobraria
    o trabalho de cada carregamento da tela.
    """
    from ProjetoEstoque.models import (
        MovimentacaoItem,
        StatusAssinaturaTermoChoices,
        TermoAssinatura,
        TipoMovimentacaoChoices,
    )

    vistos = {}

    anexos = (
        MovimentacaoItem.objects
        .filter(tipo_movimentacao=TipoMovimentacaoChoices.TRANSFERENCIA)
        .exclude(termo_pdf__isnull=True).exclude(termo_pdf="")
        .exclude(item__fornecedor__isnull=True)
        .values_list("item__fornecedor_id", "item__fornecedor__nome")
        .distinct()
    )
    digitais = (
        TermoAssinatura.objects
        .filter(status=StatusAssinaturaTermoChoices.ASSINADO)
        .exclude(itens__fornecedor__isnull=True)
        .values_list("itens__fornecedor_id", "itens__fornecedor__nome")
        .distinct()
    )

    for pk, nome in list(anexos) + list(digitais):
        if pk is not None:
            vistos[pk] = {"id": pk, "nome": nome or "—"}

    return sorted(vistos.values(), key=lambda f: (f["nome"] or "").casefold())
