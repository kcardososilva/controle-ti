"""
Estoque tecnológico do Zelo Agro: equipamentos INDIVIDUAIS (um por número de série) e materiais
por QUANTIDADE (saldo por local), mais o razão (`TecMovimentacao`) que conta a história dos dois.

Camadas: views -> este service -> models. Nada aqui toca o estoque de TI (`Item`,
`MovimentacaoItem`, `Fornecedor`): o Agro é independente dele.

Decisões que moldam este módulo — e não são óbvias olhando o código:

  • DOIS TIPOS DE CONTROLE QUE NUNCA SE MISTURAM. Quem decide é `TecModelo.controle`. Um rádio é
    um `TecEquipamento` (série, situação, histórico próprio); um rolo de cabo é um `TecSaldo`
    (modelo × local). Registrar entrada de material num modelo individual, ou cadastrar
    "equipamento" de um modelo por quantidade, é RECUSADO com a mensagem do que fazer.
  • A GARANTIA É O BANCO, NÃO O CÓDIGO. SQLite não tem `select_for_update`, então a integridade vem
    de três coisas: transação (`atomic`), restrição (`tec_saldo_nao_negativo`, `tec_equip_*`, a
    parcial de instalação ativa) e UPDATE CONDICIONAL — o débito de saldo é
    `filter(quantidade__gte=q).update(F-q)` e a mudança de situação é
    `filter(situacao=esperada).update(...)`; se nenhuma linha muda, outra pessoa chegou antes e a
    operação é recusada com a situação ATUAL na mensagem. Nunca se lê o saldo para decidir e
    gravar depois.
  • O RAZÃO É UNIFORME. O saldo de um (modelo, local) é SEMPRE
    `Σ quantidade onde destino=(estoque, local) − Σ quantidade onde origem=(estoque, local)`,
    seja material, seja equipamento (que conta 1). Entrada, saída, instalação, retirada,
    envio à manutenção, transferência e ajuste são todos a mesma conta com origem e destino
    diferentes — por isso `conferir_consistencia` confere o cache de material E a contagem de
    equipamentos disponíveis com UMA régua. `quantidade` é sempre POSITIVA: o sentido está em
    origem/destino (um ajuste para baixo sai do estoque; um para cima entra).
  • NENHUMA TELA GRAVA SALDO NEM SITUAÇÃO. Quantidade inicial é uma ENTRADA. `situacao` e `local`
    do equipamento só mudam por `transicao`, dentro de uma operação que grava o razão na mesma
    transação.
  • IDEMPOTÊNCIA POR TOKEN. O formulário manda um UUID; um segundo envio com o mesmo token
    (duplo clique, reenvio do navegador) devolve o resultado do primeiro com `repetida=True` em vez
    de registrar de novo. `token` é único no banco, então até a corrida entre dois envios simultâneos
    é resolvida (o que perde a corrida acha o registro do vencedor).
  • MOTIVO É OBRIGATÓRIO em tudo, menos na entrada; o autor também. O razão guarda o login no
    momento (`usuario_nome`) para sobreviver à exclusão do usuário.
  • NÃO SE EXCLUI NADA: equipamento sai de circulação por INATIVAÇÃO (uma baixa registrada), e o
    razão é imutável.

Contratos de leitura para as telas: `listar_equipamentos`, `listar_materiais`, `saldos_do_modelo`,
`resumo_estoque`, `abaixo_do_minimo`, `linha_do_tempo_equipamento`, `historico_movimentacoes`,
`conferir_consistencia`. Cada docstring diz o que devolve.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, DecimalField, Exists, F, OuterRef, Prefetch, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from ProjetoEstoque.models import (
    AgroFornecedor,
    AgroLocal,
    TecControle,
    TecEquipamento,
    TecEvento,
    TecInstalacao,
    TecModelo,
    TecMovimentacao,
    TecSaldo,
)
from services import tec_base as base

logger = logging.getLogger(__name__)

Situacao = TecEquipamento.Situacao
Tipo = TecMovimentacao.Tipo
Ponto = TecMovimentacao.Ponto
ZERO = Decimal("0")


@dataclass
class Resultado:
    """O que uma operação de estoque devolve. `repetida` = o mesmo token já tinha sido registrado e
    nada novo foi gravado (a tela redireciona como se tivesse acabado de acontecer)."""

    equipamento: TecEquipamento | None = None
    movimentacoes: list = field(default_factory=list)
    saldo: TecSaldo | None = None
    repetida: bool = False


# ════════════════════════════════════════════════════════
# Primitivas (também usadas pelo motor de instalação)
# ════════════════════════════════════════════════════════

def token_de(valor):
    """UUID a partir de UUID ou texto; vazio ou inválido = `None` (sem idempotência)."""
    if not valor:
        return None
    if isinstance(valor, uuid.UUID):
        return valor
    try:
        return uuid.UUID(str(valor))
    except (ValueError, AttributeError):
        return None


def movimentacao_do_token(token):
    """A movimentação que já usou este token (o formulário foi enviado antes), ou `None`."""
    if token is None:
        return None
    return TecMovimentacao.objects.select_related("equipamento", "modelo").filter(token=token).first()


def _repetida(token) -> Resultado | None:
    ja = movimentacao_do_token(token)
    if ja is None:
        return None
    return Resultado(equipamento=ja.equipamento, movimentacoes=[ja], repetida=True)


def motivo_de(valor, *, obrigatorio: bool = True) -> str:
    """Motivo aparado (até 255). Obrigatório em tudo, menos na entrada."""
    return base.texto(valor, 255, "motivo", "o motivo", obrigatorio=obrigatorio)


def gravar_movimentacao(
    tipo: str, modelo: TecModelo, usuario, *, equipamento=None, quantidade=Decimal("1"), origem=None, destino=None,
    motivo: str = "", observacoes: str = "", documento: str = "", fornecedor=None, saldo_depois=None,
    operacao=None, token=None, em=None,
) -> TecMovimentacao:
    """Grava UMA linha do razão. `origem` e `destino` são `tec_base.ponto(...)` (ou `None`). A
    `quantidade` é sempre positiva; o sentido está em origem/destino."""
    o_tipo, o_id, o_rotulo = origem or ("", None, "")
    d_tipo, d_id, d_rotulo = destino or ("", None, "")
    return TecMovimentacao.objects.create(
        tipo=tipo, modelo=modelo, equipamento=equipamento, quantidade=quantidade, saldo_depois=saldo_depois,
        origem_tipo=o_tipo, origem_id=o_id, origem_rotulo=o_rotulo,
        destino_tipo=d_tipo, destino_id=d_id, destino_rotulo=d_rotulo,
        motivo=motivo, observacoes=observacoes, documento=documento, fornecedor=fornecedor,
        usuario=base.usuario_ou_none(usuario), usuario_nome=base.nome_do_usuario(usuario),
        em=em or timezone.now(), operacao=operacao or uuid.uuid4(), token=token,
    )


def transicao(equipamento: TecEquipamento, de, para: str, *, local=None, usuario=None, local_atual=False) -> None:
    """Muda a situação (e o local de guarda) do equipamento por UPDATE CONDICIONAL: só grava se ele
    AINDA está em `de` (uma situação ou uma tupla delas) — e, com `local_atual=<AgroLocal>`, ainda
    guardado nesse local. Se nenhuma linha muda, outra pessoa chegou antes: levanta `ValidationError`
    com a situação ATUAL (relida do banco). Chamar sempre dentro de `transaction.atomic()`, junto
    com a gravação do razão."""
    estados = (de,) if isinstance(de, str) else tuple(de)
    filtro = Q(pk=equipamento.pk, situacao__in=estados)
    if local_atual is not False:
        filtro &= Q(local=local_atual)
    mudadas = TecEquipamento.objects.filter(filtro).update(
        situacao=para, local=local, atualizado_por=base.usuario_ou_none(usuario), updated_at=timezone.now(),
    )
    if mudadas != 1:
        atual = TecEquipamento.objects.select_related("local").get(pk=equipamento.pk)
        onde = f" em {atual.local}" if atual.local_id else ""
        raise ValidationError(
            f"O equipamento {atual.codigo} mudou enquanto você olhava: agora está "
            f"{atual.get_situacao_display().lower()}{onde}. Atualize a tela e confira antes de repetir."
        )
    equipamento.situacao = para
    equipamento.local = local


def quantidade_valida(valor, modelo: TecModelo, *, campo: str = "quantidade", rotulo: str = "a quantidade") -> Decimal:
    """Quantidade positiva, com até 3 casas, inteira se a unidade do modelo não for fracionável."""
    qtd = base.decimal(valor, campo, rotulo, casas=3)
    if qtd <= 0:
        raise ValidationError({campo: f"{rotulo.capitalize()} precisa ser maior que zero."})
    unidade = modelo.unidade
    if unidade is not None and not unidade.fracionavel and qtd != qtd.to_integral_value():
        raise ValidationError({campo: f"{rotulo.capitalize()} de {modelo} é em {unidade.nome.lower()}s inteiras."})
    return qtd


def creditar(modelo: TecModelo, local: AgroLocal, quantidade: Decimal) -> Decimal:
    """Soma `quantidade` ao saldo (modelo, local), criando a linha se preciso. Devolve o saldo novo.
    Atômico no banco (`F`), não lê-e-grava."""
    TecSaldo.objects.get_or_create(modelo=modelo, local=local, defaults={"quantidade": ZERO})
    TecSaldo.objects.filter(modelo=modelo, local=local).update(quantidade=F("quantidade") + quantidade)
    return TecSaldo.objects.get(modelo=modelo, local=local).quantidade


def debitar(modelo: TecModelo, local: AgroLocal, quantidade: Decimal) -> Decimal:
    """Tira `quantidade` do saldo (modelo, local) por UPDATE CONDICIONAL (`quantidade >= q`): se não
    cobre, nada muda e levanta `ValidationError` com o saldo que há. Devolve o saldo novo. O saldo
    nunca fica negativo — nem aqui, nem pelo `CheckConstraint` do banco, que é a segunda trava."""
    tirou = TecSaldo.objects.filter(modelo=modelo, local=local, quantidade__gte=quantidade).update(
        quantidade=F("quantidade") - quantidade,
    )
    if not tirou:
        atual = TecSaldo.objects.filter(modelo=modelo, local=local).values_list("quantidade", flat=True).first() or ZERO
        un = modelo.unidade.sigla if modelo.unidade_id else "un"
        raise ValidationError({"quantidade": f"Saldo insuficiente em {local}: há {atual:f} {un} e a operação pede {quantidade:f} {un}."})
    return TecSaldo.objects.get(modelo=modelo, local=local).quantidade


def _exigir_modelo(valor, *, individual: bool, ativo: bool = True) -> TecModelo:
    modelo = base.entidade(valor, TecModelo, "modelo", "o modelo")
    if ativo and not modelo.ativo:
        raise ValidationError({"modelo": "Este modelo está inativo."})
    if individual and modelo.controle != TecControle.INDIVIDUAL:
        raise ValidationError({"modelo": f"{modelo} é controlado por quantidade: use a entrada de material, não o cadastro de equipamento."})
    if not individual and modelo.controle != TecControle.QUANTIDADE:
        raise ValidationError({"modelo": f"{modelo} é controlado individualmente: cadastre cada equipamento com o número de série."})
    return modelo


def _exigir_local(valor, campo: str = "local", rotulo: str = "o local de armazenamento") -> AgroLocal:
    local = base.entidade(valor, AgroLocal, campo, rotulo)
    if not local.ativo:
        raise ValidationError({campo: "Este local está inativo."})
    return local


# O motor de instalação (`tec_instalacao_service`) valida modelo e local com as mesmas regras.
exigir_modelo = _exigir_modelo
exigir_local = _exigir_local


# ════════════════════════════════════════════════════════
# Equipamentos individuais
# ════════════════════════════════════════════════════════

_CAMPOS_EDITAVEIS = ("modelo", "numero_serie", "patrimonio", "data_aquisicao", "fornecedor", "observacoes")


def _limpar_equipamento(dados: dict, atual: TecEquipamento | None) -> dict:
    """Valida e normaliza os campos de cadastro. Só as chaves presentes em `dados` entram (na criação,
    todas). Devolve `{campo: valor}`; levanta `ValidationError({campo: msg})` com TODOS os erros."""
    criando = atual is None
    erros: dict = {}
    valores: dict = {}

    def tentar(campo, passo):
        try:
            valores[campo] = passo()
        except ValidationError as exc:
            erros.update(exc.message_dict if hasattr(exc, "error_dict") else {campo: exc.messages[0]})

    if criando or "modelo" in dados:
        def _modelo():
            modelo = _exigir_modelo(dados.get("modelo"), individual=True, ativo=False)
            # modelo inativo não recebe equipamento novo; mas o equipamento de um modelo que foi
            # inativado depois continua editável (a edição não troca o modelo)
            if (criando or modelo.pk != atual.modelo_id) and not modelo.ativo:
                raise ValidationError({"modelo": "Este modelo está inativo."})
            return modelo
        tentar("modelo", _modelo)
    modelo_ref = valores.get("modelo") or (atual.modelo if atual else None)
    if criando or "numero_serie" in dados:
        tentar("numero_serie", lambda: base.texto(dados.get("numero_serie"), 80, "numero_serie", "o número de série"))
    if criando or "patrimonio" in dados:
        tentar("patrimonio", lambda: base.texto(dados.get("patrimonio"), 40, "patrimonio", "o patrimônio").upper())
    if criando or "data_aquisicao" in dados:
        tentar("data_aquisicao", lambda: base.data(dados.get("data_aquisicao"), "data_aquisicao", "a data de aquisição"))
    if criando or "fornecedor" in dados:
        tentar("fornecedor", lambda: base.entidade(dados.get("fornecedor"), AgroFornecedor, "fornecedor", "o fornecedor", obrigatorio=False))
    if criando or "observacoes" in dados:
        tentar("observacoes", lambda: base.texto_livre(dados.get("observacoes"), "observacoes", "as observações técnicas"))

    serie = valores.get("numero_serie", atual.numero_serie if atual else "")
    patrimonio = valores.get("patrimonio", atual.patrimonio if atual else "")
    if not erros.get("numero_serie") and not erros.get("patrimonio") and not serie and not patrimonio:
        erros["numero_serie"] = "Informe o número de série ou o patrimônio: sem um dos dois o equipamento não é identificável."
    excluir = atual.pk if atual else None
    if serie and modelo_ref is not None and "numero_serie" not in erros:
        igual = TecEquipamento.objects.filter(modelo__fabricante=modelo_ref.fabricante, numero_serie__iexact=serie).exclude(pk=excluir).first()
        if igual is not None:
            erros["numero_serie"] = f"Já existe um equipamento {modelo_ref.fabricante.nome} com este número de série ({igual.codigo})."
    if patrimonio and "patrimonio" not in erros:
        igual = TecEquipamento.objects.filter(patrimonio=patrimonio).exclude(pk=excluir).first()
        if igual is not None:
            erros["patrimonio"] = f"Já existe um equipamento com este patrimônio ({igual.codigo})."
    if erros:
        raise ValidationError(erros)
    return valores


def _valores_de_auditoria(equipamento: TecEquipamento, campos) -> dict:
    saida = {}
    for campo in campos:
        valor = getattr(equipamento, campo)
        saida[campo] = str(valor) if valor is not None and not isinstance(valor, (str, int, bool)) else valor
    return saida


def criar_equipamento(dados: dict, usuario, *, token=None) -> Resultado:
    """Cadastra UM equipamento individual e o coloca no estoque: é uma ENTRADA no razão. `dados`:
    `modelo` (de controle individual, ativo), `local` (onde fica guardado — obrigatório), `numero_serie`
    e/ou `patrimonio` (ao menos um), `data_aquisicao` (não futura), `fornecedor`, `observacoes`,
    `documento` (NF), `motivo` (opcional na entrada). O código `EQ-00001` é gerado aqui. Grava
    `TecEvento(cadastro)` e `TecMovimentacao(entrada)` na mesma transação. Com `token` já usado,
    devolve o resultado anterior (`repetida=True`). `ValidationError({campo: msg})`."""
    usuario = base.exigir_usuario(usuario)
    token = token_de(token)
    anterior = _repetida(token)
    if anterior is not None:
        return anterior
    erros: dict = {}
    try:
        valores = _limpar_equipamento(dados, None)
    except ValidationError as exc:
        erros.update(exc.message_dict)
        valores = {}
    local = None
    try:
        local = _exigir_local(dados.get("local"))
    except ValidationError as exc:
        erros.update(exc.message_dict)
    documento = ""
    try:
        documento = base.texto(dados.get("documento"), 60, "documento", "o documento (nota fiscal)")
        motivo = motivo_de(dados.get("motivo"), obrigatorio=False)
    except ValidationError as exc:
        erros.update(exc.message_dict)
        motivo = ""
    if erros:
        raise ValidationError(erros)
    agora = timezone.now()
    try:
        with transaction.atomic():
            equipamento = TecEquipamento(
                **valores, situacao=Situacao.DISPONIVEL, local=local,
                criado_por=usuario, atualizado_por=usuario,
            )
            equipamento.save()
            equipamento.codigo = f"EQ-{equipamento.pk:05d}"
            TecEquipamento.objects.filter(pk=equipamento.pk).update(codigo=equipamento.codigo)
            base.evento(
                TecEvento.Alvo.EQUIPAMENTO, equipamento.pk, TecEvento.Tipo.CADASTRO, usuario,
                {"codigo": equipamento.codigo, "modelo": str(valores["modelo"]), "numero_serie": equipamento.numero_serie, "patrimonio": equipamento.patrimonio},
            )
            mov = gravar_movimentacao(
                Tipo.ENTRADA, valores["modelo"], usuario, equipamento=equipamento,
                origem=base.ponto_externo("Entrada de fornecedor" if valores.get("fornecedor") else "Entrada"), destino=base.ponto_estoque(local),
                motivo=motivo, documento=documento, fornecedor=valores.get("fornecedor"), token=token, em=agora,
            )
    except IntegrityError:
        repetida = _repetida(token)
        if repetida is not None:
            return repetida            # o outro envio simultâneo ganhou a corrida
        raise ValidationError("Já existe um equipamento com este número de série ou patrimônio.") from None
    return Resultado(equipamento=equipamento, movimentacoes=[mov])


def editar_equipamento(equipamento: TecEquipamento, dados: dict, usuario) -> TecEquipamento:
    """Altera o CADASTRO (`modelo`, `numero_serie`, `patrimonio`, `data_aquisicao`, `fornecedor`,
    `observacoes`). Situação e local NÃO se editam aqui: mudam só por operação (movimentação). Só as
    chaves presentes mudam; se nada mudou, não grava. Trocar o `modelo` é correção de cadastro: o
    razão antigo mantém o modelo de então, e a troca fica na linha do tempo (de/para). Equipamento
    inativo também pode ser corrigido (é cadastro, não operação)."""
    usuario = base.exigir_usuario(usuario)
    chaves = {k: v for k, v in dados.items() if k in _CAMPOS_EDITAVEIS}
    atual = TecEquipamento.objects.select_related("modelo__fabricante", "fornecedor").get(pk=equipamento.pk)
    valores = _limpar_equipamento(chaves, atual)
    antes = _valores_de_auditoria(atual, valores)
    depois = {c: (str(v) if v is not None and not isinstance(v, (str, int, bool)) else v) for c, v in valores.items()}
    alteracoes = base.de_para(antes, depois)
    if not alteracoes:
        return equipamento
    try:
        with transaction.atomic():
            for campo, valor in valores.items():
                setattr(equipamento, campo, valor)
            equipamento.atualizado_por = usuario
            equipamento.save()
            base.evento(TecEvento.Alvo.EQUIPAMENTO, equipamento.pk, TecEvento.Tipo.EDICAO, usuario, {"alteracoes": alteracoes})
    except IntegrityError:
        raise ValidationError("Já existe um equipamento com este número de série ou patrimônio.") from None
    return equipamento


def mover_equipamento_de_local(equipamento: TecEquipamento, local_destino, usuario, motivo: str, *, observacoes: str = "", token=None) -> Resultado:
    """Muda um equipamento DISPONÍVEL de um local de armazenamento para outro (entre depósitos ou
    entre fazendas). É a `transferencia` do razão com origem e destino em estoque. Recusa se ele não
    está mais disponível ou se não está mais no local que a tela mostrava."""
    usuario = base.exigir_usuario(usuario)
    token = token_de(token)
    anterior = _repetida(token)
    if anterior is not None:
        return anterior
    motivo = motivo_de(motivo)
    destino = _exigir_local(local_destino, "local", "o local de destino")
    atual = TecEquipamento.objects.select_related("local", "modelo").get(pk=equipamento.pk)
    if atual.situacao != Situacao.DISPONIVEL:
        raise ValidationError(f"{atual.codigo} está {atual.get_situacao_display().lower()}: só equipamento disponível muda de local de armazenamento.")
    if atual.local_id == destino.pk:
        raise ValidationError({"local": "O equipamento já está neste local."})
    origem = atual.local
    try:
        with transaction.atomic():
            transicao(equipamento, Situacao.DISPONIVEL, Situacao.DISPONIVEL, local=destino, usuario=usuario, local_atual=origem)
            mov = gravar_movimentacao(
                Tipo.TRANSFERENCIA, atual.modelo, usuario, equipamento=equipamento,
                origem=base.ponto_estoque(origem), destino=base.ponto_estoque(destino),
                motivo=motivo, observacoes=base.texto_livre(observacoes, "observacoes", "as observações"), token=token,
            )
    except IntegrityError:
        repetida = _repetida(token)
        if repetida is not None:
            return repetida
        raise
    return Resultado(equipamento=equipamento, movimentacoes=[mov])


def inativar_equipamento(equipamento: TecEquipamento, usuario, motivo: str, *, observacoes: str = "", token=None) -> Resultado:
    """Tira o equipamento de circulação (baixa: perdido, descartado, irrecuperável). Só de
    `disponível` ou `em manutenção`: um equipamento INSTALADO precisa ser retirado antes — senão
    ficaria "instalado" numa máquina enquanto o cadastro diz que não existe mais. É definitivo na
    prática: o histórico fica, o equipamento não volta a ser escolhido."""
    usuario = base.exigir_usuario(usuario)
    token = token_de(token)
    anterior = _repetida(token)
    if anterior is not None:
        return anterior
    motivo = motivo_de(motivo)
    atual = TecEquipamento.objects.select_related("local", "modelo").get(pk=equipamento.pk)
    if atual.situacao == Situacao.INSTALADO:
        raise ValidationError(f"{atual.codigo} está instalado: retire-o antes de inativar.")
    if atual.situacao == Situacao.INATIVO:
        raise ValidationError(f"{atual.codigo} já está inativo.")
    origem = base.ponto_estoque(atual.local) if atual.situacao == Situacao.DISPONIVEL else base.ponto(Ponto.MANUTENCAO, None, "Manutenção")
    try:
        with transaction.atomic():
            transicao(equipamento, (Situacao.DISPONIVEL, Situacao.EM_MANUTENCAO), Situacao.INATIVO, local=None, usuario=usuario)
            mov = gravar_movimentacao(
                Tipo.INATIVACAO, atual.modelo, usuario, equipamento=equipamento, origem=origem,
                destino=base.ponto_externo("Baixa (inativo)"), motivo=motivo,
                observacoes=base.texto_livre(observacoes, "observacoes", "as observações"), token=token,
            )
            base.evento(TecEvento.Alvo.EQUIPAMENTO, equipamento.pk, TecEvento.Tipo.SITUACAO, usuario,
                        {"de": atual.situacao, "para": Situacao.INATIVO, "motivo": motivo})
    except IntegrityError:
        repetida = _repetida(token)
        if repetida is not None:
            return repetida
        raise
    return Resultado(equipamento=equipamento, movimentacoes=[mov])


# ════════════════════════════════════════════════════════
# Materiais por quantidade
# ════════════════════════════════════════════════════════

def registrar_entrada_material(
    modelo, local, quantidade, usuario, *, fornecedor=None, documento: str = "", motivo: str = "", observacoes: str = "", token=None,
) -> Resultado:
    """Entrada de material por quantidade num local (compra, doação, quantidade inicial). Soma ao saldo
    e grava `entrada`. `modelo` precisa ser de controle por quantidade. Não exige motivo."""
    usuario = base.exigir_usuario(usuario)
    token = token_de(token)
    anterior = _repetida(token)
    if anterior is not None:
        return anterior
    modelo = _exigir_modelo(modelo, individual=False)
    local = _exigir_local(local)
    qtd = quantidade_valida(quantidade, modelo)
    forn = base.entidade(fornecedor, AgroFornecedor, "fornecedor", "o fornecedor", obrigatorio=False)
    documento = base.texto(documento, 60, "documento", "o documento (nota fiscal)")
    motivo = motivo_de(motivo, obrigatorio=False)
    try:
        with transaction.atomic():
            saldo = creditar(modelo, local, qtd)
            mov = gravar_movimentacao(
                Tipo.ENTRADA, modelo, usuario, quantidade=qtd, saldo_depois=saldo,
                origem=base.ponto_externo("Entrada de fornecedor" if forn else "Entrada"), destino=base.ponto_estoque(local),
                motivo=motivo, observacoes=base.texto_livre(observacoes, "observacoes", "as observações"), documento=documento, fornecedor=forn, token=token,
            )
    except IntegrityError:
        repetida = _repetida(token)
        if repetida is not None:
            return repetida
        raise
    return Resultado(movimentacoes=[mov], saldo=TecSaldo.objects.get(modelo=modelo, local=local))


def registrar_saida_material(modelo, local, quantidade, usuario, motivo: str, *, destino: str = "", observacoes: str = "", token=None) -> Resultado:
    """Saída de material por quantidade (consumo, perda, doação…). Debita por UPDATE condicional: sem
    saldo suficiente, recusa. `destino` é um rótulo livre ("Consumo na oficina"); instalar o material
    numa máquina ou torre é a operação de instalação, que grava o destino de verdade."""
    usuario = base.exigir_usuario(usuario)
    token = token_de(token)
    anterior = _repetida(token)
    if anterior is not None:
        return anterior
    modelo = _exigir_modelo(modelo, individual=False)
    local = _exigir_local(local)
    qtd = quantidade_valida(quantidade, modelo)
    motivo = motivo_de(motivo)
    rotulo = base.texto(destino, 120, "destino", "o destino") or "Saída / baixa"
    try:
        with transaction.atomic():
            saldo = debitar(modelo, local, qtd)
            mov = gravar_movimentacao(
                Tipo.SAIDA, modelo, usuario, quantidade=qtd, saldo_depois=saldo,
                origem=base.ponto_estoque(local), destino=base.ponto_externo(rotulo),
                motivo=motivo, observacoes=base.texto_livre(observacoes, "observacoes", "as observações"), token=token,
            )
    except IntegrityError:
        repetida = _repetida(token)
        if repetida is not None:
            return repetida
        raise
    return Resultado(movimentacoes=[mov], saldo=TecSaldo.objects.get(modelo=modelo, local=local))


def ajustar_saldo(modelo, local, quantidade_contada, usuario, motivo: str, *, observacoes: str = "", token=None) -> Resultado:
    """Acerta o saldo de (modelo, local) para a quantidade CONTADA (inventário, correção). A diferença
    vira um `ajuste` no razão — para cima entra no estoque, para baixo sai —, nunca uma edição do
    saldo. `quantidade_contada` pode ser zero. Sem diferença, recusa (não há o que ajustar)."""
    usuario = base.exigir_usuario(usuario)
    token = token_de(token)
    anterior = _repetida(token)
    if anterior is not None:
        return anterior
    modelo = _exigir_modelo(modelo, individual=False)
    local = _exigir_local(local)
    motivo = motivo_de(motivo)
    contada = base.decimal(quantidade_contada, "quantidade", "a quantidade contada", casas=3, minimo=ZERO)
    if modelo.unidade is not None and not modelo.unidade.fracionavel and contada != contada.to_integral_value():
        raise ValidationError({"quantidade": f"A quantidade de {modelo} é em {modelo.unidade.nome.lower()}s inteiras."})
    try:
        with transaction.atomic():
            TecSaldo.objects.get_or_create(modelo=modelo, local=local, defaults={"quantidade": ZERO})
            atual = TecSaldo.objects.get(modelo=modelo, local=local).quantidade
            diferenca = contada - atual
            if diferenca == 0:
                raise ValidationError({"quantidade": f"O saldo de {modelo} em {local} já é {atual:f}."})
            if diferenca > 0:
                saldo = creditar(modelo, local, diferenca)
                origem, destino = base.ponto_externo("Ajuste (sobra)"), base.ponto_estoque(local)
            else:
                saldo = debitar(modelo, local, -diferenca)
                origem, destino = base.ponto_estoque(local), base.ponto_externo("Ajuste (falta)")
            mov = gravar_movimentacao(
                Tipo.AJUSTE, modelo, usuario, quantidade=abs(diferenca), saldo_depois=saldo, origem=origem, destino=destino,
                motivo=motivo, observacoes=base.texto_livre(observacoes, "observacoes", "as observações"), token=token,
            )
    except IntegrityError:
        repetida = _repetida(token)
        if repetida is not None:
            return repetida
        raise
    return Resultado(movimentacoes=[mov], saldo=TecSaldo.objects.get(modelo=modelo, local=local))


def transferir_material_entre_locais(modelo, local_origem, local_destino, quantidade, usuario, motivo: str, *, observacoes: str = "", token=None) -> Resultado:
    """Leva material por quantidade de um local de armazenamento a outro: debita a origem (condicional)
    e credita o destino na mesma transação, e grava UMA `transferencia` (origem e destino em estoque).
    `saldo_depois` é o do local de DESTINO."""
    usuario = base.exigir_usuario(usuario)
    token = token_de(token)
    anterior = _repetida(token)
    if anterior is not None:
        return anterior
    modelo = _exigir_modelo(modelo, individual=False)
    origem = _exigir_local(local_origem, "local_origem", "o local de origem")
    destino = _exigir_local(local_destino, "local_destino", "o local de destino")
    if origem.pk == destino.pk:
        raise ValidationError({"local_destino": "A origem e o destino são o mesmo local."})
    qtd = quantidade_valida(quantidade, modelo)
    motivo = motivo_de(motivo)
    try:
        with transaction.atomic():
            debitar(modelo, origem, qtd)
            saldo_destino = creditar(modelo, destino, qtd)
            mov = gravar_movimentacao(
                Tipo.TRANSFERENCIA, modelo, usuario, quantidade=qtd, saldo_depois=saldo_destino,
                origem=base.ponto_estoque(origem), destino=base.ponto_estoque(destino),
                motivo=motivo, observacoes=base.texto_livre(observacoes, "observacoes", "as observações"), token=token,
            )
    except IntegrityError:
        repetida = _repetida(token)
        if repetida is not None:
            return repetida
        raise
    return Resultado(movimentacoes=[mov], saldo=TecSaldo.objects.get(modelo=modelo, local=destino))


# ════════════════════════════════════════════════════════
# Leitura
# ════════════════════════════════════════════════════════

def _inteiro_ou_none(valor):
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None


def listar_equipamentos(filtros: dict | None = None):
    """QuerySet de `TecEquipamento` (modelo, fabricante, categoria, local, fazenda e fornecedor já
    carregados). `filtros` (todos opcionais; vazio é ignorado): `q` (código, série, patrimônio, modelo
    ou fabricante), `fabricante`, `categoria`, `grupo`, `modelo`, `situacao`, `local`, `fazenda`.
    Valor de id que não é número devolve lista vazia (nunca estoura)."""
    filtros = {k: v for k, v in (filtros or {}).items() if v not in (None, "")}
    qs = TecEquipamento.objects.select_related("modelo__fabricante", "modelo__categoria", "local__fazenda", "fornecedor").prefetch_related(
        Prefetch(
            "instalacoes", queryset=TecInstalacao.objects.filter(fim_em__isnull=True).select_related("veiculo", "operador"),
            to_attr="ativas_prefetch",
        )
    )
    if filtros.get("q"):
        termo = str(filtros["q"]).strip()
        qs = qs.filter(
            Q(codigo__icontains=termo) | Q(numero_serie__icontains=termo) | Q(patrimonio__icontains=termo)
            | Q(modelo__nome__icontains=termo) | Q(modelo__fabricante__nome__icontains=termo)
        )
    for chave, caminho in (
        ("fabricante", "modelo__fabricante_id"), ("categoria", "modelo__categoria_id"), ("modelo", "modelo_id"),
        ("local", "local_id"), ("fazenda", "local__fazenda_id"),
    ):
        if chave in filtros:
            numero = _inteiro_ou_none(filtros[chave])
            qs = qs.filter(**{caminho: numero}) if numero is not None else qs.none()
    if filtros.get("grupo"):
        qs = qs.filter(modelo__categoria__grupo=filtros["grupo"])
    if filtros.get("situacao") in Situacao.values:
        qs = qs.filter(situacao=filtros["situacao"])
    return qs.order_by("-pk")


def listar_materiais(filtros: dict | None = None):
    """QuerySet de `TecModelo` de controle por quantidade, com `total` (soma dos saldos) e `n_locais`
    (locais com saldo > 0) anotados. `filtros`: `q`, `fabricante`, `categoria`, `grupo`, `local`
    (só modelos com saldo > 0 ali), `abaixo_minimo` ('1': total < mínimo), `ativo` ('1'/'0')."""
    filtros = {k: v for k, v in (filtros or {}).items() if v not in (None, "")}
    qs = TecModelo.objects.filter(controle=TecControle.QUANTIDADE).select_related("fabricante", "categoria", "unidade").annotate(
        total=Coalesce(Sum("saldos__quantidade"), Value(ZERO), output_field=DecimalField(max_digits=14, decimal_places=3)),
        n_locais=Count("saldos", filter=Q(saldos__quantidade__gt=0), distinct=True),
    )
    if filtros.get("q"):
        termo = str(filtros["q"]).strip()
        qs = qs.filter(Q(nome__icontains=termo) | Q(fabricante__nome__icontains=termo) | Q(categoria__nome__icontains=termo))
    for chave, caminho in (("fabricante", "fabricante_id"), ("categoria", "categoria_id")):
        if chave in filtros:
            numero = _inteiro_ou_none(filtros[chave])
            qs = qs.filter(**{caminho: numero}) if numero is not None else qs.none()
    if filtros.get("grupo"):
        qs = qs.filter(categoria__grupo=filtros["grupo"])
    if "local" in filtros:
        # Exists, não join: um filter() em `saldos` DEPOIS do annotate abriria outro join e
        # multiplicaria o `total` pelo número de linhas.
        numero = _inteiro_ou_none(filtros["local"])
        qs = qs.filter(Exists(TecSaldo.objects.filter(modelo=OuterRef("pk"), local_id=numero, quantidade__gt=0))) if numero is not None else qs.none()
    if filtros.get("abaixo_minimo") == "1":
        qs = qs.filter(estoque_minimo__gt=0, total__lt=F("estoque_minimo"))
    if filtros.get("ativo") in ("1", "0"):
        qs = qs.filter(ativo=filtros["ativo"] == "1")
    return qs.order_by("fabricante__nome", "nome")


def saldos_do_modelo(modelo: TecModelo):
    """QuerySet de `TecSaldo` do modelo com saldo > 0, por local (com o local e a fazenda carregados)."""
    return TecSaldo.objects.filter(modelo=modelo, quantidade__gt=0).select_related("local__fazenda").order_by("local__fazenda__local", "local__nome")


def total_disponivel(modelo: TecModelo) -> Decimal:
    """Quanto há DISPONÍVEL do modelo: soma dos saldos (por quantidade) ou nº de equipamentos
    disponíveis (individual)."""
    if modelo.controle == TecControle.QUANTIDADE:
        return TecSaldo.objects.filter(modelo=modelo).aggregate(t=Coalesce(Sum("quantidade"), Value(ZERO)))["t"]
    return Decimal(TecEquipamento.objects.filter(modelo=modelo, situacao=Situacao.DISPONIVEL).count())


def abaixo_do_minimo(limite: int | None = None) -> list[dict]:
    """Modelos ATIVOS com `estoque_minimo > 0` cujo disponível está abaixo do mínimo. Devolve uma lista
    de `{"modelo", "disponivel", "minimo", "falta"}` pelos mais faltantes. Só aparece em tela: não há
    e-mail de estoque mínimo nesta entrega."""
    individuais = dict(
        TecEquipamento.objects.filter(situacao=Situacao.DISPONIVEL).values_list("modelo_id").annotate(n=Count("pk")).values_list("modelo_id", "n")
    )
    saldos = dict(TecSaldo.objects.values_list("modelo_id").annotate(t=Sum("quantidade")).values_list("modelo_id", "t"))
    saida = []
    for modelo in TecModelo.objects.filter(ativo=True, estoque_minimo__gt=0).select_related("fabricante", "unidade"):
        if modelo.controle == TecControle.QUANTIDADE:
            disponivel = saldos.get(modelo.pk) or ZERO
        else:
            disponivel = Decimal(individuais.get(modelo.pk, 0))
        if disponivel < modelo.estoque_minimo:
            saida.append({"modelo": modelo, "disponivel": disponivel, "minimo": modelo.estoque_minimo, "falta": modelo.estoque_minimo - disponivel})
    saida.sort(key=lambda linha: linha["falta"], reverse=True)
    return saida[:limite] if limite else saida


def resumo_estoque() -> dict:
    """Números da visão geral do estoque: equipamentos por situação (`por_situacao`, `total`),
    `materiais` (modelos por quantidade ativos), `abaixo_do_minimo` e `movimentos_7_dias`."""
    por_situacao = {s: 0 for s in Situacao.values}
    for situacao, n in TecEquipamento.objects.values_list("situacao").annotate(n=Count("pk")).values_list("situacao", "n"):
        por_situacao[situacao] = n
    desde = timezone.now() - timedelta(days=7)
    return {
        "por_situacao": por_situacao,
        "total": sum(por_situacao.values()),
        "materiais": TecModelo.objects.filter(controle=TecControle.QUANTIDADE, ativo=True).count(),
        "abaixo_do_minimo": len(abaixo_do_minimo()),
        "movimentos_7_dias": TecMovimentacao.objects.filter(em__gte=desde).count(),
    }


def historico_movimentacoes(filtros: dict | None = None):
    """QuerySet do razão (`TecMovimentacao`) com modelo, fabricante e equipamento carregados, do mais
    novo para o mais antigo. `filtros`: `tipo`, `modelo`, `fabricante`, `categoria`, `equipamento`,
    `ponto` + `ponto_id` (qualquer movimentação que tenha esse destino OU origem — ex.: tudo o que
    passou por uma máquina), `usuario` (login), `de` / `ate` (datas), `q` (código, série, modelo,
    fabricante ou motivo)."""
    filtros = {k: v for k, v in (filtros or {}).items() if v not in (None, "")}
    qs = TecMovimentacao.objects.select_related("modelo__fabricante", "equipamento")
    if filtros.get("tipo") in Tipo.values:
        qs = qs.filter(tipo=filtros["tipo"])
    for chave, caminho in (("modelo", "modelo_id"), ("fabricante", "modelo__fabricante_id"), ("categoria", "modelo__categoria_id"), ("equipamento", "equipamento_id")):
        if chave in filtros:
            numero = _inteiro_ou_none(filtros[chave])
            qs = qs.filter(**{caminho: numero}) if numero is not None else qs.none()
    if filtros.get("ponto") in Ponto.values and "ponto_id" in filtros:
        numero = _inteiro_ou_none(filtros["ponto_id"])
        ponto_ = filtros["ponto"]
        qs = qs.filter(Q(destino_tipo=ponto_, destino_id=numero) | Q(origem_tipo=ponto_, origem_id=numero)) if numero is not None else qs.none()
    if filtros.get("usuario"):
        qs = qs.filter(usuario_nome__iexact=str(filtros["usuario"]).strip())
    try:
        if filtros.get("de"):
            qs = qs.filter(em__date__gte=base.data(filtros["de"], "de", "a data inicial", permitir_futuro=True))
        if filtros.get("ate"):
            qs = qs.filter(em__date__lte=base.data(filtros["ate"], "ate", "a data final", permitir_futuro=True))
    except ValidationError:
        qs = qs.none()
    if filtros.get("q"):
        termo = str(filtros["q"]).strip()
        qs = qs.filter(
            Q(equipamento__codigo__icontains=termo) | Q(equipamento__numero_serie__icontains=termo) | Q(equipamento__patrimonio__icontains=termo)
            | Q(modelo__nome__icontains=termo) | Q(modelo__fabricante__nome__icontains=termo) | Q(motivo__icontains=termo)
        )
    return qs.order_by("-em", "-pk")


def _descrever_movimentacao(m: TecMovimentacao) -> str:
    de = m.origem_rotulo or "—"
    para = m.destino_rotulo or "—"
    return f"{de} → {para}"


def linha_do_tempo_equipamento(equipamento: TecEquipamento, limite: int = 100) -> list[dict]:
    """A história de UM equipamento, do mais novo para o mais antigo: o cadastro (`TecEvento`) e o
    razão (`TecMovimentacao`) juntos. Cada item: `{"em", "origem" ('evento'|'movimentacao'), "titulo",
    "detalhe", "usuario", "motivo"}`. As movimentações não se repetem no `TecEvento`: entram por leitura."""
    itens = []
    for e in TecEvento.objects.filter(alvo=TecEvento.Alvo.EQUIPAMENTO, alvo_id=equipamento.pk).select_related("usuario")[:limite]:
        detalhe = ""
        if e.tipo == TecEvento.Tipo.EDICAO:
            detalhe = "; ".join(f"{c}: {v.get('de') or '—'} → {v.get('para') or '—'}" for c, v in (e.dados.get("alteracoes") or {}).items())
        elif e.tipo == TecEvento.Tipo.SITUACAO:
            detalhe = e.dados.get("motivo", "")
        itens.append({
            "em": e.em, "origem": "evento", "titulo": e.get_tipo_display(), "detalhe": detalhe,
            "usuario": e.usuario.username if e.usuario_id else "", "motivo": "",
        })
    for m in TecMovimentacao.objects.filter(equipamento=equipamento).select_related("usuario")[:limite]:
        itens.append({
            "em": m.em, "origem": "movimentacao", "titulo": m.get_tipo_display(), "detalhe": _descrever_movimentacao(m),
            "usuario": m.usuario_nome or "", "motivo": m.motivo,
        })
    itens.sort(key=lambda i: i["em"], reverse=True)
    return itens[:limite]


# ════════════════════════════════════════════════════════
# Consistência: o cache e o estado contra o razão
# ════════════════════════════════════════════════════════

def razao_por_modelo_local() -> dict:
    """O saldo de cada (modelo_id, local_id) segundo o RAZÃO: `Σ destino=(estoque, local) −
    Σ origem=(estoque, local)`. Vale para material e equipamento (que conta 1). É a fonte da
    verdade que o cache (`TecSaldo`) e a situação (`TecEquipamento`) são conferidos contra."""
    saldos: dict = {}
    entradas = TecMovimentacao.objects.filter(destino_tipo=Ponto.ESTOQUE).values("modelo_id", "destino_id").annotate(t=Sum("quantidade"))
    for linha in entradas:
        saldos[(linha["modelo_id"], linha["destino_id"])] = saldos.get((linha["modelo_id"], linha["destino_id"]), ZERO) + linha["t"]
    saidas = TecMovimentacao.objects.filter(origem_tipo=Ponto.ESTOQUE).values("modelo_id", "origem_id").annotate(t=Sum("quantidade"))
    for linha in saidas:
        saldos[(linha["modelo_id"], linha["origem_id"])] = saldos.get((linha["modelo_id"], linha["origem_id"]), ZERO) - linha["t"]
    return saldos


def conferir_consistencia() -> dict:
    """Compara o estado com o razão, SEM CORRIGIR nada (leitura não escreve): devolve
    `{"materiais": [...], "equipamentos": [...], "ok": bool}`. Cada divergência é um dict com
    `modelo`, `local`, `cache` (ou `disponiveis`) e `razao`. Para alinhar o cache de material ao
    razão, `reconciliar`; divergência de equipamento NÃO se reconcilia (indicaria um defeito)."""
    razao = razao_por_modelo_local()
    modelos = {m.pk: m for m in TecModelo.objects.select_related("fabricante")}
    locais = {l.pk: l for l in AgroLocal.objects.select_related("fazenda")}
    cache = {(s.modelo_id, s.local_id): s.quantidade for s in TecSaldo.objects.all()}
    disponiveis = {}
    for modelo_id, local_id, n in TecEquipamento.objects.filter(situacao=Situacao.DISPONIVEL).values_list("modelo_id", "local_id").annotate(n=Count("pk")).values_list("modelo_id", "local_id", "n"):
        disponiveis[(modelo_id, local_id)] = Decimal(n)
    divergencias_material, divergencias_equipamento = [], []
    for chave in set(razao) | set(cache) | set(disponiveis):
        modelo = modelos.get(chave[0])
        if modelo is None:
            continue
        esperado = razao.get(chave, ZERO)
        if modelo.controle == TecControle.QUANTIDADE:
            atual = cache.get(chave, ZERO)
            if atual != esperado:
                divergencias_material.append({"modelo": modelo, "local": locais.get(chave[1]), "cache": atual, "razao": esperado})
        else:
            atual = disponiveis.get(chave, ZERO)
            if atual != esperado:
                divergencias_equipamento.append({"modelo": modelo, "local": locais.get(chave[1]), "disponiveis": atual, "razao": esperado})
    return {"materiais": divergencias_material, "equipamentos": divergencias_equipamento, "ok": not divergencias_material and not divergencias_equipamento}


def reconciliar() -> int:
    """Alinha o cache de MATERIAL (`TecSaldo`) ao razão. Não toca no razão nem nos equipamentos.
    Devolve quantos saldos corrigiu. Explícito e logado: abrir uma tela nunca chama isto."""
    corrigidos = 0
    for d in conferir_consistencia()["materiais"]:
        if d["local"] is None:
            continue
        if d["razao"] < 0:
            # o razão dizendo "menos que zero" é um defeito a investigar, não um saldo a gravar
            logger.error("Razão negativo para %s em %s (%s): saldo NÃO reconciliado.", d["modelo"], d["local"], d["razao"])
            continue
        with transaction.atomic():
            TecSaldo.objects.get_or_create(modelo=d["modelo"], local=d["local"], defaults={"quantidade": ZERO})
            TecSaldo.objects.filter(modelo=d["modelo"], local=d["local"]).update(quantidade=d["razao"])
        logger.warning("Saldo de %s em %s reconciliado: %s → %s", d["modelo"], d["local"], d["cache"], d["razao"])
        corrigidos += 1
    return corrigidos
