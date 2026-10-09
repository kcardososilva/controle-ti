# -*- coding: utf-8 -*-
"""
Licenças — regra de negócio dos assentos (pool por nome de software).

Este módulo é a FONTE ÚNICA de atribuição e devolução de assento de licença.
Antes dele havia QUATRO caminhos que faziam a mesma coisa de formas
diferentes:

  1. `MovimentacaoLicencaForm.save()` (tela de movimentação);
  2. `views/usuarios.licenca_devolver_rapido` (botão na ficha do colaborador);
  3. `views/usuarios.usuario_remover_todas_licencas` (remoção em massa, com
     os helpers `_criar_mov_licenca_devolucao` + `_incrementar_saldo_lote`);
  4. `DesligamentoService._devolver_licencas_ativas` (cascata do desligamento,
     que por sua vez chamava o caminho 1).

Quatro implementações da mesma regra divergem — e divergiram. Os dois defeitos
abaixo foram REPRODUZIDOS antes desta refatoração:

**Devolução dupla criava assento do nada.** A validação de devolução procurava
a última movimentação `tipo=ATRIBUICAO` do par (licença, colaborador). Como o
filtro só olhava atribuições, ela continuava achando a atribuição ANTIGA mesmo
depois da devolução — então uma segunda devolução passava na validação e
somava +1 no saldo do lote. Resultado medido: lote com `quantidade_total=10`
ficava com `quantidade_disponivel=11`. Assento fantasma, que o sistema depois
ofereceria para atribuir a alguém. Alcançável por duplo clique, por rodar o
desligamento duas vezes, ou por combinar o botão da ficha com a cascata.

**Nenhum caminho limitava o contador.** `quantidade_disponivel += 1` sem teto
em `quantidade_total`.

Razão é a verdade; o contador é cache
-----------------------------------------
`MovimentacaoLicenca` é um livro-razão: cada linha é um fato datado. Quem
detém o quê é DERIVADO dele (`alocacao_de`), nunca lido de um campo mutável.
`LicencaLote.quantidade_disponivel` continua existindo e sendo mantido — é o
que a listagem e os filtros do catálogo usam, e remover a coluna quebraria
tudo que já lê dela — mas aqui ele é tratado como **cache**: pode divergir, a
divergência é detectável (`conferir_consistencia`) e o reparo é uma ação
EXPLÍCITA e auditada (`reconciliar_lote`), nunca um efeito colateral
silencioso de uma leitura de tela.

Essa é a razão de `conferir_consistencia` não corrigir nada: uma função de
leitura que conserta dados "de passagem" transforma abrir um relatório num
evento de escrita — e aí a próxima divergência é indistinguível de um reparo
automático que já aconteceu.

Ordenação do razão
------------------
Sempre `("created_at", "id")`. Sem o `id` como desempate, duas movimentações
gravadas no mesmo instante — o que acontece de verdade numa remoção em massa
ou na cascata de desligamento — ficam em ordem indefinida, e "atribuiu e
devolveu" pode ser lido como "devolveu e atribuiu", invertendo o estado.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction

logger = logging.getLogger(__name__)

_CENTAVO = Decimal("0.01")

# Divisores do ciclo de cobrança para chegar ao custo MENSAL.
#
# **Esta tabela e `PeriodicidadeChoices` (models.py) têm de andar juntas — e não
# andaram.** A tabela mapeava `trimestral`, que NÃO era opção do enum, e não
# tinha `trienal` nem `contrato`, que eram. Resultado: lote Trienal caía no
# fallback de 1 mês e publicava custo mensal **36× maior** que o real; lote
# "Contrato/Outro", 12× maior. Nenhum lote da base estava nesses ciclos, então
# o defeito nunca apareceu em tela — mas o maior contrato do acervo passa de
# R$ 1 mi por ciclo, e ali o divisor errado não é centavo, é ordem de grandeza.
#
# `contrato` ("Contrato/Outro") é ciclo DECLARADAMENTE indefinido, e por isso é
# o único caso em que o número exibido é uma estimativa. Tratá-lo como anual é
# a aposta certa (contrato corporativo é anual na esmagadora maioria), mas
# estimar em silêncio é o que transforma relatório em armadilha: `CustoLote`
# carrega `ciclo_estimado=True` e as telas marcam o valor. Antes a estimativa
# era "mensal", que não só estimava errado como estimava para o lado que
# infla a despesa.
_MESES_POR_CICLO = {
    "mensal": Decimal("1"),
    "trimestral": Decimal("3"),
    "semestral": Decimal("6"),
    "anual": Decimal("12"),
    "trienal": Decimal("36"),
    "contrato": Decimal("12"),
}

# Ciclos cujo divisor é uma suposição, não um dado.
_CICLOS_ESTIMADOS = {"contrato", ""}

# Quantos meses o ciclo avança para chegar ao próximo vencimento. Mesma tabela
# acima, mas em int — `relativedelta` não aceita Decimal.
_MESES_INT = {k: int(v) for k, v in _MESES_POR_CICLO.items()}


class LicencaError(Exception):
    """Erro de regra de negócio, com mensagem pronta para a tela."""


# ---------------------------------------------------------------------------
# Custo — uma única função de rateio
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CustoLote:
    """Rateio do custo de um lote. Todos os valores já arredondados a centavo."""
    unitario_ciclo: Decimal   # custo de 1 assento no ciclo contratado
    mensal_unit: Decimal      # custo de 1 assento por mês
    anual_unit: Decimal       # custo de 1 assento por ano
    mensal_total: Decimal     # custo do lote inteiro por mês
    anual_total: Decimal      # custo do lote inteiro por ano
    # Ciclo "Contrato/Outro" não declara periodicidade: o mensal é uma
    # ESTIMATIVA (tratado como anual). A tela marca o número em vez de
    # apresentá-lo como medida — estimativa silenciosa em relatório de custo é
    # o que faz alguém decidir compra com base em número que ninguém apurou.
    ciclo_estimado: bool = False

    @classmethod
    def zero(cls):
        z = Decimal("0.00")
        return cls(z, z, z, z, z, False)


def custos_do_lote(lote) -> CustoLote:
    """
    Rateia o custo do lote em assento/mês/ano.

    **Uma função só, e isto importa.** Antes o cálculo existia em dois lugares
    com ORDENS DE ARREDONDAMENTO diferentes: a tela de detalhe arredondava o
    custo mensal do lote e só depois dividia pelos assentos, enquanto o
    formulário dividia pelos assentos e só depois convertia para mensal. Duas
    contas sobre o mesmo lote podiam fechar com um centavo de diferença, e o
    valor gravado em `MovimentacaoLicenca.valor_unitario` não batia com o
    custo mensal mostrado na mesma linha da tela.

    A política de arredondamento aqui é: **divide tudo em precisão cheia e
    arredonda só no fim**, uma vez por valor exibido. É a ordem que não
    acumula erro de um passo no passo seguinte.
    """
    qtd = int(getattr(lote, "quantidade_total", 0) or 0)
    if qtd <= 0:
        return CustoLote.zero()

    ciclo = Decimal(getattr(lote, "custo_ciclo", 0) or 0)
    periodicidade = str(getattr(lote, "periodicidade", "") or "").lower()
    # Ciclo desconhecido é tratado como ANUAL, não como mensal. O fallback
    # antigo (1 mês) não dividia nada: um lote trienal de R$ 1 mi aparecia
    # custando R$ 1 mi/mês. Errar para o lado que infla a despesa é o pior dos
    # dois lados — é o número que vai para a reunião de custos.
    meses = _MESES_POR_CICLO.get(periodicidade, Decimal("12"))

    unit_ciclo_exato = ciclo / Decimal(qtd)
    mensal_unit_exato = unit_ciclo_exato / meses

    return CustoLote(
        unitario_ciclo=unit_ciclo_exato.quantize(_CENTAVO, rounding=ROUND_HALF_UP),
        mensal_unit=mensal_unit_exato.quantize(_CENTAVO, rounding=ROUND_HALF_UP),
        anual_unit=(mensal_unit_exato * Decimal("12")).quantize(_CENTAVO, rounding=ROUND_HALF_UP),
        mensal_total=(mensal_unit_exato * Decimal(qtd)).quantize(_CENTAVO, rounding=ROUND_HALF_UP),
        anual_total=(mensal_unit_exato * Decimal(qtd) * Decimal("12")).quantize(
            _CENTAVO, rounding=ROUND_HALF_UP
        ),
        ciclo_estimado=periodicidade in _CICLOS_ESTIMADOS
        or periodicidade not in _MESES_POR_CICLO,
    )


# ---------------------------------------------------------------------------
# Estado derivado do razão
# ---------------------------------------------------------------------------

@dataclass
class Alocacao:
    """
    Quem detém assentos desta licença AGORA, apurado pelo razão.

    `por_usuario` guarda a movimentação de atribuição vigente de cada
    colaborador — é ela que carrega o lote de origem e o custo cobrado, os dois
    dados necessários para devolver certo.
    """
    por_usuario: dict = field(default_factory=dict)   # {usuario_id: mov}
    por_lote: dict = field(default_factory=lambda: defaultdict(int))  # {lote_id: n}
    sem_lote: int = 0

    @property
    def em_uso(self) -> int:
        return len(self.por_usuario)


def alocacao_de(licenca) -> Alocacao:
    """
    Reconstrói do razão quem está com a licença.

    Lê o razão inteiro da licença em ordem e aplica cada fato: atribuição
    coloca o colaborador, devolução tira. O estado final é a resposta — não há
    campo "está ativo" para ficar desatualizado.
    """
    from ProjetoEstoque.models import MovimentacaoLicenca, TipoMovLicencaChoices

    movs = (
        MovimentacaoLicenca.objects
        .filter(licenca=licenca)
        .select_related("usuario", "lote", "centro_custo_destino")
        .order_by("created_at", "id")
    )

    alocacao = Alocacao()
    for mov in movs:
        if mov.tipo == TipoMovLicencaChoices.ATRIBUICAO:
            alocacao.por_usuario[mov.usuario_id] = mov
        else:
            alocacao.por_usuario.pop(mov.usuario_id, None)

    for mov in alocacao.por_usuario.values():
        if mov.lote_id:
            alocacao.por_lote[mov.lote_id] += 1
        else:
            alocacao.sem_lote += 1

    return alocacao


def atribuicao_vigente(licenca, usuario):
    """A atribuição ativa deste par, ou None.

    É a pergunta que a validação de devolução precisa fazer — e a que o código
    antigo NÃO fazia: ele procurava a última atribuição (filtrando por tipo),
    que continua existindo depois de devolvida.
    """
    return alocacao_de(licenca).por_usuario.get(
        usuario.pk if hasattr(usuario, "pk") else usuario
    )


def licencas_do_usuario(usuario) -> list:
    """
    Licenças que este colaborador detém agora, com lote e custo.

    Devolve `[{licenca, lote, movimentacao, custo}]`. Fonte única para a ficha
    do colaborador, para a cascata de desligamento e para o painel de
    assentos órfãos — três telas que antes respondiam isso com três consultas
    diferentes.
    """
    from ProjetoEstoque.models import MovimentacaoLicenca, TipoMovLicencaChoices

    movs = (
        MovimentacaoLicenca.objects
        .filter(usuario=usuario)
        .select_related("licenca", "licenca__fornecedor", "lote")
        .order_by("created_at", "id")
    )

    vigente = {}
    for mov in movs:
        if mov.tipo == TipoMovLicencaChoices.ATRIBUICAO:
            vigente[mov.licenca_id] = mov
        else:
            vigente.pop(mov.licenca_id, None)

    resultado = []
    for mov in vigente.values():
        custo = custos_do_lote(mov.lote) if mov.lote else CustoLote.zero()
        resultado.append({
            "licenca": mov.licenca,
            "lote": mov.lote,
            "movimentacao": mov,
            "custo": custo,
            # Custo COBRADO na época, que é o que o estorno devolve. Pode
            # diferir do custo atual do lote (renovação mudou o preço) — a
            # tela mostra os dois em vez de escolher um e mentir sobre o outro.
            "valor_cobrado": Decimal(mov.valor_unitario or 0),
        })

    resultado.sort(key=lambda r: (r["licenca"].nome or "").casefold())
    return resultado


# ---------------------------------------------------------------------------
# Saldo e consistência
# ---------------------------------------------------------------------------

def saldo_calculado(lote, alocacao=None) -> int:
    """Assentos livres do lote SEGUNDO O RAZÃO (não o contador gravado)."""
    total = int(getattr(lote, "quantidade_total", 0) or 0)
    if alocacao is None:
        alocacao = alocacao_de(lote.licenca)
    return max(0, total - alocacao.por_lote.get(lote.pk, 0))


def conferir_consistencia(licenca=None) -> list[dict]:
    """
    Compara o contador gravado com o razão, para cada lote.

    **Só lê.** Ver o cabeçalho do módulo: uma função de leitura que conserta de
    passagem faz de abrir um relatório um evento de escrita, e some com a
    distinção entre "nunca divergiu" e "divergiu e alguém consertou sem
    registrar". O reparo é `reconciliar_lote`, explícito e auditado.

    Devolve uma linha por lote DIVERGENTE.
    """
    from ProjetoEstoque.models import LicencaLote

    lotes = LicencaLote.objects.select_related("licenca")
    if licenca is not None:
        lotes = lotes.filter(licenca=licenca)

    por_licenca = {}
    problemas = []

    for lote in lotes.order_by("licenca__nome", "pk"):
        if lote.licenca_id not in por_licenca:
            por_licenca[lote.licenca_id] = alocacao_de(lote.licenca)
        alocacao = por_licenca[lote.licenca_id]

        total = int(lote.quantidade_total or 0)
        gravado = int(lote.quantidade_disponivel or 0)
        esperado = saldo_calculado(lote, alocacao)

        motivos = []
        if gravado > total:
            motivos.append(
                f"saldo gravado ({gravado}) é MAIOR que o total comprado ({total}) — "
                "assento que não existe sendo oferecido para atribuição"
            )
        if gravado != esperado:
            motivos.append(
                f"saldo gravado ({gravado}) diverge do razão ({esperado})"
            )

        if motivos:
            problemas.append({
                "lote": lote,
                "licenca": lote.licenca,
                "total": total,
                "gravado": gravado,
                "esperado": esperado,
                "em_uso_razao": alocacao.por_lote.get(lote.pk, 0),
                "motivos": motivos,
            })

    return problemas


@transaction.atomic
def reconciliar_lote(lote, *, user=None, observacao="") -> dict:
    """
    Alinha o contador do lote ao razão. Ação EXPLÍCITA e registrada.

    Não mexe em nenhuma movimentação: o razão é a verdade e permanece
    intocado — o que muda é só o cache que divergiu dele. Por isso é seguro
    rodar quantas vezes quiser (idempotente).
    """
    from ProjetoEstoque.models import LicencaLote

    travado = LicencaLote.objects.select_for_update().get(pk=lote.pk)
    antes = int(travado.quantidade_disponivel or 0)
    depois = saldo_calculado(travado)

    if antes == depois:
        return {"lote": travado, "antes": antes, "depois": depois, "mudou": False}

    travado.quantidade_disponivel = depois
    if user is not None and hasattr(travado, "atualizado_por"):
        travado.atualizado_por = user
    travado.save(update_fields=["quantidade_disponivel", "atualizado_por", "updated_at"])

    logger.warning(
        "Licença: saldo do lote %s reconciliado de %s para %s (razão) por %s. %s",
        travado.pk, antes, depois, getattr(user, "username", "?"), observacao,
    )
    return {"lote": travado, "antes": antes, "depois": depois, "mudou": True}


# ---------------------------------------------------------------------------
# Atribuir e devolver — os dois únicos caminhos
# ---------------------------------------------------------------------------

def _escolher_lote(licenca, lote=None):
    """
    Lote de onde o assento sai, travado para escrita.

    Sem lote indicado, usa FIFO pela data de compra: consome primeiro o que foi
    comprado antes, que é o que vence antes. O `id` entra como desempate para
    lotes sem `data_compra` não embaralharem entre requisições.

    O saldo conferido é o do RAZÃO, não o do contador: um contador inflado por
    divergência ofereceria um assento que não existe.
    """
    from ProjetoEstoque.models import LicencaLote

    alocacao = alocacao_de(licenca)

    if lote is not None:
        travado = LicencaLote.objects.select_for_update().get(pk=lote.pk)
        if travado.licenca_id != (licenca.pk if hasattr(licenca, "pk") else licenca):
            raise LicencaError("O lote escolhido não pertence a esta licença.")
        if saldo_calculado(travado, alocacao) < 1:
            raise LicencaError(
                f"O lote #{travado.pk} não tem assento livre "
                f"({travado.quantidade_total or 0} comprado(s), todos em uso)."
            )
        return travado

    candidatos = (
        LicencaLote.objects
        .select_for_update()
        .filter(licenca=licenca)
        .order_by("data_compra", "id")
    )
    for candidato in candidatos:
        if saldo_calculado(candidato, alocacao) >= 1:
            return candidato

    raise LicencaError(
        "Não há assento livre nesta licença. Cadastre um lote novo ou devolva "
        "um assento antes de atribuir."
    )


def conferir_pode_atribuir(*, licenca, lote=None) -> int:
    """
    Há assento livre? Devolve quantos; levanta `LicencaError` se não houver.

    Existe separado de `atribuir` para a VALIDAÇÃO do formulário poder usar a
    mesma conta sem abrir transação nem travar linha — o formulário precisa
    responder "dá para atribuir?" antes de gravar, e duplicar a conta ali era
    como a tela e o service passavam a discordar sobre o que está esgotado.
    """
    from ProjetoEstoque.models import LicencaLote

    alocacao = alocacao_de(licenca)

    if lote is not None:
        livre = saldo_calculado(lote, alocacao)
        if livre < 1:
            raise LicencaError(
                f"O lote #{lote.pk} não tem assento livre "
                f"({lote.quantidade_total or 0} comprado(s), todos em uso)."
            )
        return livre

    lotes = LicencaLote.objects.filter(licenca=licenca)
    if not lotes.exists():
        raise LicencaError(
            "Esta licença não tem nenhum lote cadastrado — registre a compra "
            "antes de atribuir assentos."
        )

    livre = sum(saldo_calculado(l, alocacao) for l in lotes)
    if livre < 1:
        raise LicencaError(
            "Não há assento livre nesta licença. Cadastre um lote novo ou "
            "devolva um assento antes de atribuir."
        )
    return livre


def _sincronizar_saldo(lote):
    """Realinha o contador do lote ao razão depois de uma escrita.

    Deriva em vez de somar/subtrair (`+= 1`), e é isso que impede o assento
    fantasma: um `+= 1` repetido inflava o contador para sempre, enquanto
    recalcular do razão dá o mesmo resultado quantas vezes rodar.
    """
    novo = saldo_calculado(lote)
    if int(lote.quantidade_disponivel or 0) != novo:
        lote.quantidade_disponivel = novo
        lote.save(update_fields=["quantidade_disponivel", "updated_at"])
    return novo


@transaction.atomic
def atribuir(*, licenca, usuario, lote=None, user=None, observacao="") -> object:
    """
    Dá um assento desta licença ao colaborador.

    Levanta `LicencaError` com mensagem pronta quando não pode: colaborador já
    tem a licença, sem assento livre, ou lote de outra licença.
    """
    from ProjetoEstoque.models import MovimentacaoLicenca, TipoMovLicencaChoices

    if usuario is None:
        raise LicencaError("Informe o colaborador que vai receber a licença.")

    if atribuicao_vigente(licenca, usuario) is not None:
        raise LicencaError(
            f"{usuario.nome} já tem a licença “{licenca.nome}”. "
            "Para trocar o lote de origem, devolva primeiro."
        )

    lote_alvo = _escolher_lote(licenca, lote)
    custo = custos_do_lote(lote_alvo)

    mov = MovimentacaoLicenca.objects.create(
        tipo=TipoMovLicencaChoices.ATRIBUICAO,
        licenca=licenca,
        usuario=usuario,
        lote=lote_alvo,
        # O custo segue o colaborador: o assento é despesa do centro de custo
        # de quem usa, não de quem comprou. Na devolução ele volta para o dono
        # do lote (ver `devolver`).
        centro_custo_destino=getattr(usuario, "centro_custo", None),
        valor_unitario=custo.mensal_unit,
        observacao=observacao or "",
        criado_por=user,
        atualizado_por=user,
    )

    _sincronizar_saldo(lote_alvo)

    logger.info(
        "Licença atribuída: licenca=%s usuario=%s lote=%s mensal=%s por=%s",
        licenca.pk, usuario.pk, lote_alvo.pk, custo.mensal_unit,
        getattr(user, "username", "?"),
    )
    return mov


@transaction.atomic
def devolver(*, licenca, usuario, user=None, observacao="", silencioso=False):
    """
    Devolve ao estoque o assento que o colaborador detém.

    `silencioso=True` devolve `None` em vez de levantar quando não há assento
    ativo. É o modo das operações EM MASSA (desligamento, remoção total): ali
    "este colaborador não tem esta licença" não é erro do operador, é só um
    item que já estava resolvido — e abortar a cascata inteira por causa dele
    deixaria o resto das licenças presas, que é exatamente a perda que o
    desligamento tem de evitar.

    Idempotente: devolver duas vezes não cria assento. O contador é
    RECALCULADO do razão, nunca incrementado.
    """
    from ProjetoEstoque.models import MovimentacaoLicenca, TipoMovLicencaChoices

    if usuario is None:
        raise LicencaError("Informe o colaborador que está devolvendo a licença.")

    vigente = atribuicao_vigente(licenca, usuario)
    if vigente is None:
        if silencioso:
            return None
        raise LicencaError(
            f"{usuario.nome} não tem a licença “{licenca.nome}” ativa — "
            "não há o que devolver."
        )

    from ProjetoEstoque.models import LicencaLote

    lote_retorno = None
    if vigente.lote_id:
        lote_retorno = (
            LicencaLote.objects.select_for_update().filter(pk=vigente.lote_id).first()
        )
    if lote_retorno is None:
        # Lote da atribuição apagado: devolve ao mais recente da licença para o
        # assento não se perder. Sem isto a devolução sumiria com o assento.
        lote_retorno = (
            LicencaLote.objects.select_for_update()
            .filter(licenca=licenca).order_by("-data_compra", "-id").first()
        )

    mov = MovimentacaoLicenca.objects.create(
        tipo=TipoMovLicencaChoices.DEVOLUCAO,
        licenca=licenca,
        usuario=usuario,
        lote=lote_retorno,
        # O custo volta para o dono do lote (quem pagou), não fica no CC de
        # quem devolveu.
        centro_custo_destino=(
            getattr(lote_retorno, "centro_custo", None) if lote_retorno
            else getattr(licenca, "centro_custo", None)
        ),
        # Estorna exatamente o que foi cobrado na saída.
        valor_unitario=vigente.valor_unitario,
        observacao=observacao or "",
        criado_por=user,
        atualizado_por=user,
    )

    if lote_retorno is not None:
        _sincronizar_saldo(lote_retorno)

    logger.info(
        "Licença devolvida: licenca=%s usuario=%s lote=%s por=%s",
        licenca.pk, usuario.pk, getattr(lote_retorno, "pk", None),
        getattr(user, "username", "?"),
    )
    return mov


@transaction.atomic
def devolver_todas_do_usuario(*, usuario, user=None, observacao="") -> dict:
    """
    Devolve TODAS as licenças do colaborador. Usado pelo desligamento e pela
    remoção em massa.

    Nenhuma licença derruba as outras: cada devolução é tentada em modo
    silencioso, e o que falhar por regra entra em `falhas` em vez de abortar.
    Numa cascata de desligamento, abortar na 3ª licença deixaria as outras
    presas no nome de quem não trabalha mais aqui — a perda que o fluxo existe
    para evitar.
    """
    devolvidas = []
    falhas = []

    for registro in licencas_do_usuario(usuario):
        licenca = registro["licenca"]
        try:
            mov = devolver(
                licenca=licenca, usuario=usuario, user=user,
                observacao=observacao, silencioso=True,
            )
            if mov is not None:
                devolvidas.append(mov)
        except LicencaError as erro:
            falhas.append({"licenca": licenca, "motivo": str(erro)})
            logger.error(
                "Licença %s não devolvida no desligamento de %s: %s",
                licenca.pk, usuario.pk, erro,
            )

    return {"devolvidas": devolvidas, "falhas": falhas, "total": len(devolvidas)}


# ---------------------------------------------------------------------------
# Saúde do acervo — o que o TI precisa ver sem procurar
# ---------------------------------------------------------------------------

def assentos_orfaos() -> list[dict]:
    """
    Assentos ativos no nome de quem NÃO está mais ativo no cadastro.

    É dinheiro saindo todo mês por licença de quem já saiu da empresa. O
    sistema tem a cascata de desligamento, mas ela só cobre quem foi desligado
    PELO fluxo — colaborador marcado como desligado direto na ficha, ou
    desligado antes da cascata existir, deixa o assento preso e invisível: não
    aparece em lugar nenhum porque nenhuma tela cruzava "assento ativo" com
    "status do colaborador".

    Ordenado pelo custo mensal, do mais caro para o mais barato — é a ordem em
    que vale a pena resolver.
    """
    from ProjetoEstoque.models import MovimentacaoLicenca, TipoMovLicencaChoices

    movs = (
        MovimentacaoLicenca.objects
        .select_related("licenca", "usuario", "lote")
        .order_by("created_at", "id")
    )

    vigente = {}
    for mov in movs:
        chave = (mov.licenca_id, mov.usuario_id)
        if mov.tipo == TipoMovLicencaChoices.ATRIBUICAO:
            vigente[chave] = mov
        else:
            vigente.pop(chave, None)

    orfaos = []
    for mov in vigente.values():
        usuario = mov.usuario
        if usuario is None or (usuario.status or "").lower() == "ativo":
            continue
        custo = custos_do_lote(mov.lote) if mov.lote else CustoLote.zero()
        orfaos.append({
            "movimentacao": mov,
            "licenca": mov.licenca,
            "usuario": usuario,
            "lote": mov.lote,
            "mensal": custo.mensal_unit,
            "anual": custo.anual_unit,
            "desde": mov.created_at,
        })

    orfaos.sort(key=lambda o: (-o["mensal"], (o["usuario"].nome or "").casefold()))
    return orfaos


def resumo_saude() -> dict:
    """
    Painel de saúde do módulo, numa chamada.

    Junta o que o TI precisa saber antes de qualquer outra coisa ao abrir
    licenças: assentos presos em desligado (dinheiro a recuperar), pools
    esgotados (próxima atribuição vai falhar) e divergências de contador
    (números da tela não confiáveis).
    """
    from ProjetoEstoque.models import Licenca, LicencaLote

    orfaos = assentos_orfaos()
    divergencias = conferir_consistencia()

    esgotados = []
    for licenca in Licenca.objects.prefetch_related("lotes").order_by("nome"):
        lotes = list(licenca.lotes.all())
        if not lotes:
            continue
        alocacao = alocacao_de(licenca)
        total = sum(int(l.quantidade_total or 0) for l in lotes)
        livre = sum(saldo_calculado(l, alocacao) for l in lotes)
        if total > 0 and livre == 0:
            esgotados.append({"licenca": licenca, "total": total, "em_uso": alocacao.em_uso})

    return {
        "orfaos": orfaos,
        "orfaos_mensal": sum((o["mensal"] for o in orfaos), Decimal("0.00")),
        "orfaos_anual": sum((o["anual"] for o in orfaos), Decimal("0.00")),
        "divergencias": divergencias,
        "esgotados": esgotados,
        # Licença cadastrada sem nenhum lote: não tem assento para atribuir e
        # não tem custo para ratear. Fica no painel porque é cadastro pela
        # metade — alguém criou a licença e paramos antes de registrar a compra.
        "sem_lote": list(
            Licenca.objects.filter(lotes__isnull=True).order_by("nome")
        ),
    }


# ---------------------------------------------------------------------------
# Renovação — a pergunta que o módulo não sabia responder
# ---------------------------------------------------------------------------
#
# Até 2026-10-07 o acervo guardava `data_compra` e `periodicidade` e NÃO
# respondia "o que vence nos próximos 60 dias". São ~R$ 3,5 mi/ano em contrato,
# e a renovação chegava pela cobrança do fornecedor — ou seja, a empresa
# descobria o vencimento DEPOIS de renovar, quando já não havia decisão a
# tomar. Renovação só é uma decisão enquanto há prazo.
#
# `data_renovacao` vazia significa "derive", não "não vence": é o que faz os
# lotes já cadastrados entrarem na tela sem ninguém digitar nada.

# Janelas de urgência, em dias. A ordem importa: a primeira faixa que couber é
# a do lote.
JANELAS_RENOVACAO = (30, 60, 90)

_TOM_POR_JANELA = {
    "vencido": "erro",
    30: "erro",
    60: "alerta",
    90: "info",
}


def vencimento_de(lote):
    """
    Próxima renovação do lote: o campo gravado, ou derivado do ciclo.

    Derivar não é "estimar uma data qualquer": avança a `data_compra` em passos
    do ciclo até passar de hoje. Um lote anual comprado em 2023 vence no
    aniversário DESTE ano, não em 2024 — devolver a primeira data já vencida
    encheria a tela de pendência falsa de três anos atrás, e a tela perderia a
    utilidade no primeiro dia.

    Devolve `None` quando não há como saber (sem data de compra e sem campo).
    """
    from datetime import date

    gravado = getattr(lote, "data_renovacao", None)
    if gravado:
        return gravado

    compra = getattr(lote, "data_compra", None)
    if not compra:
        return None

    meses = _MESES_INT.get(str(getattr(lote, "periodicidade", "") or "").lower(), 12)
    if meses <= 0:
        return None

    from dateutil.relativedelta import relativedelta

    hoje = date.today()
    vencimento = compra
    # Teto de segurança: lote com ano digitado errado (1923) faria o laço rodar
    # mil vezes. 200 passos cobrem 16 anos de ciclo mensal — além disso é dado
    # ruim, não contrato.
    for _ in range(200):
        if vencimento > hoje:
            return vencimento
        vencimento = vencimento + relativedelta(months=meses)
    return vencimento


def _faixa_de(dias):
    """Em que janela de urgência cai um vencimento a `dias` de hoje."""
    if dias is None:
        return None
    if dias < 0:
        return "vencido"
    for janela in JANELAS_RENOVACAO:
        if dias <= janela:
            return janela
    return None


def lotes_sem_vencimento() -> list:
    """
    Lotes de que não se consegue derivar vencimento: sem data de compra e sem
    data de renovação.

    Ficam LISTADOS em vez de silenciosamente fora da tela — um lote sem
    vencimento não é um lote que não vence, é um lote que o sistema não sabe
    vigiar, e a diferença importa para quem confia na tela.
    """
    from ProjetoEstoque.models import LicencaLote

    return list(
        LicencaLote.objects
        .filter(data_renovacao__isnull=True, data_compra__isnull=True)
        .select_related("licenca")
        .order_by("licenca__nome", "id")
    )


def renovacoes(*, dias=90, incluir_vencidos=True) -> list[dict]:
    """
    Lotes que vencem dentro da janela, do mais urgente para o menos.

    Cada linha traz o custo do CICLO que vai ser cobrado na renovação — é esse
    o número que decide se renova, reduz assentos ou cancela. Vem junto o uso
    real apurado no razão, porque renovar 225 assentos para usar 168 é a forma
    mais cara de não decidir nada: nesse lote são 57 assentos pagos e parados.
    """
    from datetime import date

    from ProjetoEstoque.models import LicencaLote

    hoje = date.today()
    lotes = (
        LicencaLote.objects
        .select_related("licenca", "licenca__fornecedor", "fornecedor", "centro_custo")
        .order_by("licenca__nome", "id")
    )

    alocacoes = {}
    linhas = []

    for lote in lotes:
        vencimento = vencimento_de(lote)
        if vencimento is None:
            continue

        restam = (vencimento - hoje).days
        if restam < 0 and not incluir_vencidos:
            continue
        if restam > dias:
            continue

        if lote.licenca_id not in alocacoes:
            alocacoes[lote.licenca_id] = alocacao_de(lote.licenca)
        alocacao = alocacoes[lote.licenca_id]

        custo = custos_do_lote(lote)
        total = int(lote.quantidade_total or 0)
        em_uso = alocacao.por_lote.get(lote.pk, 0)
        faixa = _faixa_de(restam)

        linhas.append({
            "lote": lote,
            "licenca": lote.licenca,
            "vencimento": vencimento,
            "dias": restam,
            "vencido": restam < 0,
            "faixa": faixa,
            "tom": _TOM_POR_JANELA.get(faixa, "neutro"),
            "derivado": not getattr(lote, "data_renovacao", None),
            "automatica": bool(getattr(lote, "renovacao_automatica", True)),
            # Valor absoluto: o template precisa dizer "vencido há 12 dias",
            # e virar o sinal com filtro de template (`|cut:"-"`) é o tipo de
            # malabarismo que quebra calado quando o número muda de forma.
            "dias_abs": abs(restam),
            "total": total,
            "em_uso": em_uso,
            "ocioso": max(0, total - em_uso),
            "pct_uso": int(round((em_uso / total) * 100)) if total else 0,
            # Tom do USO (distinto do tom do PRAZO): metade ou mais do lote
            # parado é o caso em que renovar a mesma quantidade é desperdício
            # declarado. A conta mora aqui porque template não calcula — e
            # porque assim a mesma régua vale na tela, no e-mail e na auditoria.
            "tom_uso": (
                "vazio" if total == 0
                else "ok" if em_uso == total
                else "erro" if em_uso * 2 <= total
                else "alerta"
            ),
            "custo": custo,
            # Valor da fatura que chega na renovação — o ciclo inteiro, não o
            # mensal. É o que o fornecedor vai cobrar.
            "custo_ciclo": Decimal(lote.custo_ciclo or 0),
            # Quanto do ciclo está sendo pago por assento parado: economia
            # disponível sem negociar preço nenhum, bastando comprar menos.
            "desperdicio_ciclo": (
                custo.unitario_ciclo * Decimal(max(0, total - em_uso))
            ).quantize(_CENTAVO, rounding=ROUND_HALF_UP),
        })

    linhas.sort(key=lambda r: (r["dias"], -r["custo_ciclo"]))
    return linhas


def resumo_renovacoes(*, dias=90) -> dict:
    """
    Painel de renovação: quanto vence em cada janela e quanto disso é assento parado.

    Uma chamada, porque a tela, o alerta por e-mail e a auditoria agendada
    fazem exatamente a mesma pergunta — e fariam três contas diferentes se cada
    uma montasse a sua.
    """
    linhas = renovacoes(dias=dias)

    def _soma(seq, chave):
        return sum((item[chave] for item in seq), Decimal("0.00"))

    vencidos = [l for l in linhas if l["vencido"]]
    por_janela = []
    for janela in JANELAS_RENOVACAO:
        faixa = [l for l in linhas if l["faixa"] == janela]
        por_janela.append({
            "dias": janela,
            "tom": _TOM_POR_JANELA[janela],
            "linhas": faixa,
            "qtd": len(faixa),
            "valor": _soma(faixa, "custo_ciclo"),
        })

    # Contrato que NÃO renova sozinho e está vencendo é o caso que realmente
    # interrompe serviço: ninguém cobra, a licença simplesmente para.
    manuais = [l for l in linhas if not l["automatica"] and l["dias"] <= 60]

    return {
        "linhas": linhas,
        "janela_dias": dias,
        "vencidos": vencidos,
        "vencidos_valor": _soma(vencidos, "custo_ciclo"),
        "por_janela": por_janela,
        "manuais": manuais,
        "total_valor": _soma(linhas, "custo_ciclo"),
        "total_desperdicio": _soma(linhas, "desperdicio_ciclo"),
        "sem_data": lotes_sem_vencimento(),
    }


# ---------------------------------------------------------------------------
# Catálogo — os números do razão para TODA tela de lista
# ---------------------------------------------------------------------------

@dataclass
class LinhaCatalogo:
    """Uma licença com seus números já apurados — o que as listas mostram."""
    licenca: object
    lotes: list = field(default_factory=list)
    total: int = 0
    em_uso: int = 0
    livre: int = 0
    saldo_contador: int = 0
    mensal: Decimal = Decimal("0.00")
    anual: Decimal = Decimal("0.00")
    mensal_em_uso: Decimal = Decimal("0.00")
    investido_ciclo: Decimal = Decimal("0.00")
    vencimento: object = None
    dias_vencimento: object = None
    ciclo_estimado: bool = False

    @property
    def pct_uso(self) -> int:
        return int(round((self.em_uso / self.total) * 100)) if self.total else 0

    @property
    def divergente(self) -> bool:
        """O contador gravado discorda do razão nesta licença."""
        return self.saldo_contador != self.livre

    @property
    def esgotada(self) -> bool:
        return self.total > 0 and self.livre == 0

    @property
    def sem_lote(self) -> bool:
        return not self.lotes

    @property
    def desperdicio_mensal(self) -> Decimal:
        """Custo mensal dos assentos pagos e parados."""
        return (self.mensal - self.mensal_em_uso).quantize(
            _CENTAVO, rounding=ROUND_HALF_UP
        )

    @property
    def faixa_vencimento(self):
        return _faixa_de(self.dias_vencimento)


def catalogo(licencas=None) -> list[LinhaCatalogo]:
    """
    Números de cada licença apurados pelo RAZÃO, numa passada.

    **Por que isto existe.** Catálogo, galeria, lista de lotes, aba de lotes do
    detalhe e o seletor de lote da movimentação liam `quantidade_disponivel` —
    o CACHE —, enquanto o cabeçalho do detalhe e a atribuição rápida liam o
    razão. Duas telas a um clique de distância mostravam "assentos livres"
    diferentes para a mesma licença, e nenhuma das duas parecia errada para
    quem olhava. O painel de saúde existe justamente porque o cache pode
    divergir; não faz sentido o resto do módulo propagar a divergência.

    O contador continua sendo LIDO — em `saldo_contador` — para a tela poder
    APONTAR a divergência. O que não acontece mais é ele ser apresentado como
    se fosse a verdade.

    Anota os números também em cada lote (`lote.saldo_real`, `lote.custo`,
    `lote.vencimento`), porque as telas mostram linha por lote e template não
    calcula.
    """
    from datetime import date

    from ProjetoEstoque.models import Licenca

    hoje = date.today()

    if licencas is None:
        licencas = Licenca.objects.all()

    licencas = licencas.select_related("fornecedor", "centro_custo").prefetch_related(
        "lotes", "lotes__fornecedor", "lotes__centro_custo"
    )

    linhas = []
    for licenca in licencas:
        lotes = list(licenca.lotes.all())
        alocacao = alocacao_de(licenca)

        linha = LinhaCatalogo(licenca=licenca, lotes=lotes)

        for lote in lotes:
            custo = custos_do_lote(lote)
            total = int(lote.quantidade_total or 0)
            em_uso = alocacao.por_lote.get(lote.pk, 0)
            livre = max(0, total - em_uso)

            lote.custo = custo
            lote.em_uso_real = em_uso
            lote.saldo_real = livre
            lote.vencimento = vencimento_de(lote)
            lote.dias_vencimento = (
                (lote.vencimento - hoje).days if lote.vencimento else None
            )
            lote.faixa_vencimento = _faixa_de(lote.dias_vencimento)
            lote.divergente = int(lote.quantidade_disponivel or 0) != livre

            linha.total += total
            linha.em_uso += em_uso
            linha.livre += livre
            linha.saldo_contador += int(lote.quantidade_disponivel or 0)
            linha.mensal += custo.mensal_total
            linha.anual += custo.anual_total
            linha.mensal_em_uso += (custo.mensal_unit * Decimal(em_uso)).quantize(
                _CENTAVO, rounding=ROUND_HALF_UP
            )
            linha.investido_ciclo += Decimal(lote.custo_ciclo or 0)
            linha.ciclo_estimado = linha.ciclo_estimado or custo.ciclo_estimado

            # O vencimento da LICENÇA é o mais próximo entre os lotes dela: é a
            # primeira data em que alguém precisa decidir algo.
            if lote.vencimento and (
                linha.vencimento is None or lote.vencimento < linha.vencimento
            ):
                linha.vencimento = lote.vencimento
                linha.dias_vencimento = lote.dias_vencimento

        lotes.sort(
            key=lambda l: (l.data_compra is not None, l.data_compra or hoje),
            reverse=True,
        )
        linhas.append(linha)

    linhas.sort(key=lambda l: (l.licenca.nome or "").casefold())
    return linhas


def resumo_acervo(linhas=None) -> dict:
    """
    Totais do acervo para a faixa de KPI das listas.

    Recebe as linhas já apuradas para não varrer o razão duas vezes na mesma
    requisição — a lista precisa dos dois (linha e total) e `catalogo()` é a
    parte cara.
    """
    if linhas is None:
        linhas = catalogo()

    total = sum(l.total for l in linhas)
    em_uso = sum(l.em_uso for l in linhas)
    mensal = sum((l.mensal for l in linhas), Decimal("0.00"))
    mensal_em_uso = sum((l.mensal_em_uso for l in linhas), Decimal("0.00"))

    return {
        "licencas": len(linhas),
        "total": total,
        "em_uso": em_uso,
        "livre": sum(l.livre for l in linhas),
        "pct_uso": int(round((em_uso / total) * 100)) if total else 0,
        "mensal": mensal,
        "anual": sum((l.anual for l in linhas), Decimal("0.00")),
        "mensal_em_uso": mensal_em_uso,
        # Dinheiro que já sai todo mês por assento comprado e não usado. É o
        # único número desta faixa em que dá para agir hoje.
        "desperdicio_mensal": (mensal - mensal_em_uso).quantize(
            _CENTAVO, rounding=ROUND_HALF_UP
        ),
        "divergentes": sum(1 for l in linhas if l.divergente),
        "esgotadas": sum(1 for l in linhas if l.esgotada),
        "sem_lote": sum(1 for l in linhas if l.sem_lote),
    }
