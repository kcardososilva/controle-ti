"""
Instalação, retirada, transferência e substituição de equipamentos tecnológicos do Zelo Agro:
o fluxo central que leva um equipamento entre o estoque, as máquinas e as pessoas (e, nas
próximas etapas, torres e pontas de enlace).

Camadas: views -> este service -> `tec_estoque_service` (razão e `transicao`) -> models. Nada aqui
toca o estoque de TI (`Item`, `MovimentacaoItem`) nem o vínculo de chip (`frota_vinculo_service`,
que continua dono dos chips: a composição da máquina só LÊ `FrotaVinculo`).

Decisões que moldam este módulo — e não são óbvias olhando o código:

  • A GARANTIA É O BANCO. "Um equipamento, uma instalação ativa" é a `UniqueConstraint` parcial de
    `TecInstalacao` (equipamento, quando `fim_em` é nulo). Duas pessoas instalando o mesmo
    equipamento ao mesmo tempo: a primeira muda a situação por UPDATE CONDICIONAL
    (`tec_estoque_service.transicao`), a segunda não casa nenhuma linha e é recusada com a situação
    ATUAL na mensagem; se ainda assim algo escapasse, o INSERT esbarra na restrição. A instalação
    também é ENCERRADA por UPDATE condicional (`fim_em IS NULL`), nunca por ler-e-gravar.
    (`select_for_update` não faz nada no SQLite; não é ele que protege.)
  • TODA OPERAÇÃO É UMA TRANSAÇÃO que grava, juntas: a situação do equipamento, a linha do razão
    (`TecMovimentacao`, imutável) e a abertura/encerramento da instalação. Ou tudo, ou nada.
  • AS OPERAÇÕES SOBRE UMA INSTALAÇÃO EXISTENTE RECEBEM O ID DA INSTALAÇÃO ESPERADA, não o do
    equipamento: se a tela abriu com a instalação X e ela já foi encerrada (retirada, transferida,
    substituída), a operação é RECUSADA ("já foi retirado por fulano em 12/11 14:05") em vez de agir
    sobre a instalação nova que outra pessoa abriu.
  • O RAZÃO CONTINUA UNIFORME (ver `tec_estoque_service`): instalar tira 1 do estoque do local onde o
    equipamento estava; retirar devolve 1 ao estoque do local escolhido; transferir move de um
    destino a outro sem tocar no estoque. Por isso a origem do razão é SEMPRE o local lido na mesma
    transação em que a situação mudou (`transicao(..., local_atual=...)`): se ele mudou de local no
    meio, a operação é recusada em vez de gravar uma origem falsa.
  • EQUIPAMENTO EM MANUTENÇÃO NÃO SE INSTALA. Ele volta ao estoque pelo retorno da manutenção
    (`retornar_da_manutencao`) e só então fica disponível. Equipamento instalado precisa ser
    retirado (ou enviado à manutenção, que já o retira) antes de ser inativado.
  • RETIRAR × DESVINCULAR: é a MESMA operação (encerra a instalação e devolve o equipamento ao
    estoque, com o local de guarda obrigatório) com dois rótulos no razão. Sair de uma máquina,
    torre ou ponta é `retirada`; sair de uma PESSOA (rádio HT devolvido) é `desvinculacao`: o
    operador não é um lugar, é uma responsabilidade que acaba.
  • MATERIAL POR QUANTIDADE não tem instalação: aplicá-lo numa máquina é uma saída do estoque com
    destino nela (`instalacao` no razão), e quanto há aplicado ali é o razão lido do outro lado
    (`Σ destino − Σ origem`). Devolver confere esse saldo DEPOIS de gravar, ainda dentro da
    transação (o SQLite tem um escritor por vez, então quem chega depois lê o que o primeiro gravou).
  • IDEMPOTÊNCIA POR TOKEN (`TecMovimentacao.token` é único): reenviar o formulário devolve o
    resultado da primeira vez com `repetida=True`. Uma substituição grava duas linhas na mesma
    `operacao`; o token fica na primeira e a repetição devolve as duas.
  • MOTIVO E AUTOR SÃO OBRIGATÓRIOS em tudo. Máquina `desativada` e colaborador `desligado` não
    recebem equipamento (máquina em manutenção ou inativa recebe, com aviso).

Contratos de leitura para as telas: `composicao_do_veiculo`, `instalacoes_do_equipamento`,
`historico_do_veiculo`, `materiais_aplicados`, `aplicacoes_do_material`, `responsavel_do_equipamento`,
`opcoes_de_destino`, `separar_destino`, `conferir_instalacoes`.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F, Sum
from django.utils import timezone

from ProjetoEstoque.models import (
    FrotaVeiculo,
    StatusUsuarioChoices,
    TecEquipamento,
    TecInstalacao,
    TecModelo,
    TecMovimentacao,
    Usuario,
)
from services import tec_base as base
from services import tec_estoque_service as estoque

logger = logging.getLogger(__name__)

Situacao = TecEquipamento.Situacao
Tipo = TecMovimentacao.Tipo
Ponto = TecMovimentacao.Ponto
ZERO = Decimal("0")

# Quem pode receber um equipamento, e a coluna de `TecInstalacao` que o guarda. A infraestrutura
# (torre, estação, ponta de enlace) entra aqui, na etapa dela, com a própria coluna e a mesma regra.
CAMPO_DO_DESTINO = {Ponto.VEICULO: "veiculo", Ponto.OPERADOR: "operador"}
DESTINOS_QUE_RECEBEM = tuple(CAMPO_DO_DESTINO)

MANUTENCAO = base.ponto(Ponto.MANUTENCAO, None, "Manutenção / assistência")


@dataclass
class ResultadoInstalacao:
    """O que uma operação fez.

    `movimentacoes`: as linhas do razão gravadas, na ordem (a substituição grava duas, com o mesmo
    `operacao`). `instalacao`: a instalação ATIVA criada (instalar, transferir, substituir) ou
    `None`. `encerrada`: a que foi encerrada (retirar, transferir, substituir, enviar para
    manutenção) ou `None`. `equipamento`: o equipamento principal da operação, relido do banco.
    `avisos`: textos para mostrar junto da confirmação; o registro foi feito. `repetida`: o mesmo
    token já tinha sido registrado e nada novo foi gravado."""

    operacao: uuid.UUID | None = None
    movimentacoes: list = field(default_factory=list)
    equipamento: TecEquipamento | None = None
    instalacao: TecInstalacao | None = None
    encerrada: TecInstalacao | None = None
    avisos: list = field(default_factory=list)
    repetida: bool = False

    @property
    def movimentacao(self):
        return self.movimentacoes[0] if self.movimentacoes else None


# ════════════════════════════════════════════════════════
# Destinos
# ════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Destino:
    """Para onde o equipamento (ou o material) vai: o objeto, o rótulo do momento e os avisos."""

    tipo: str
    objeto: object
    rotulo: str
    avisos: tuple = ()

    @property
    def ponto(self):
        return base.ponto(self.tipo, self.objeto.pk, self.rotulo)

    @property
    def campo(self) -> str:
        return CAMPO_DO_DESTINO[self.tipo]


def _rotulo_do_veiculo(veiculo: FrotaVeiculo) -> str:
    return f"{veiculo.codigo} · {veiculo.nome}"


def resolver_destino(tipo, valor, *, campo: str = "destino", para_receber: bool = True) -> Destino:
    """O destino a partir do tipo (`veiculo` | `operador`) e da instância ou id. `para_receber`: é um
    destino NOVO (instalar, transferir, aplicar) e por isso precisa estar apto; sem ele só se lê o
    destino (retirar de uma máquina desativada precisa funcionar). Levanta `ValidationError({campo})`."""
    if tipo not in CAMPO_DO_DESTINO:
        raise ValidationError({campo: "Escolha para onde vai: uma máquina ou um operador."})
    if tipo == Ponto.VEICULO:
        veiculo = base.entidade(valor, FrotaVeiculo, campo, "a máquina")
        avisos = ()
        if para_receber and veiculo.status == FrotaVeiculo.Status.DESATIVADO:
            raise ValidationError({campo: f"A máquina {veiculo.codigo} está desativada e não recebe equipamento."})
        if para_receber and veiculo.status in (FrotaVeiculo.Status.MANUTENCAO, FrotaVeiculo.Status.INATIVO):
            avisos = (f"A máquina {veiculo.codigo} está {veiculo.get_status_display().lower()}; o equipamento foi registrado nela mesmo assim.",)
        return Destino(tipo, veiculo, _rotulo_do_veiculo(veiculo), avisos)
    operador = base.entidade(valor, Usuario, campo, "o operador")
    if para_receber and operador.status == StatusUsuarioChoices.DESLIGADO:
        raise ValidationError({campo: f"{operador.nome} está desligado(a) e não recebe equipamento."})
    return Destino(tipo, operador, operador.nome)


def _destino_da_instalacao(instalacao: TecInstalacao) -> Destino:
    """O destino ATUAL de uma instalação, só para ler (rótulo do momento no razão)."""
    if instalacao.veiculo_id:
        return Destino(Ponto.VEICULO, instalacao.veiculo, _rotulo_do_veiculo(instalacao.veiculo))
    return Destino(Ponto.OPERADOR, instalacao.operador, instalacao.operador.nome)


# ════════════════════════════════════════════════════════
# Peças internas
# ════════════════════════════════════════════════════════

def _quando(momento) -> str:
    return timezone.localtime(momento).strftime("%d/%m/%Y %H:%M") if momento else ""


def _resultado_repetida(token) -> ResultadoInstalacao | None:
    """Se o token já foi usado, o resultado da primeira vez (todas as linhas da mesma operação)."""
    ja = estoque.movimentacao_do_token(token)
    if ja is None:
        return None
    linhas = list(TecMovimentacao.objects.filter(operacao=ja.operacao).select_related("equipamento", "modelo").order_by("pk"))
    abertas = TecInstalacao.objects.filter(mov_inicio__in=linhas).select_related("veiculo", "operador").first()
    fechadas = TecInstalacao.objects.filter(mov_fim__in=linhas).select_related("veiculo", "operador").first()
    return ResultadoInstalacao(
        operacao=ja.operacao, movimentacoes=linhas, equipamento=ja.equipamento, instalacao=abertas, encerrada=fechadas, repetida=True,
    )


def _reler(equipamento: TecEquipamento) -> TecEquipamento:
    """O equipamento como está AGORA no banco (a tela pode estar velha)."""
    try:
        return TecEquipamento.objects.select_related("modelo__fabricante", "modelo__categoria", "local__fazenda").get(pk=equipamento.pk)
    except TecEquipamento.DoesNotExist:
        raise ValidationError("Equipamento não encontrado.") from None


def _instalacao_ativa(equipamento: TecEquipamento) -> TecInstalacao | None:
    return TecInstalacao.objects.filter(equipamento=equipamento, fim_em__isnull=True).select_related("veiculo", "operador").first()


def _mensagem_ja_encerrada(instalacao: TecInstalacao) -> str:
    """"Este equipamento já foi retirado por fulano em 12/11 14:05" — com o verbo certo para como a
    instalação acabou (retirada, desvinculação, transferência, substituição ou manutenção)."""
    verbos = {
        Tipo.RETIRADA: "retirado",
        Tipo.DESVINCULACAO: "desvinculado",
        Tipo.TRANSFERENCIA: "transferido para outro destino",
        Tipo.SUBSTITUICAO: "substituído",
        Tipo.ENVIO_MANUTENCAO: "enviado para manutenção",
    }
    tipo = instalacao.mov_fim.tipo if instalacao.mov_fim_id else Tipo.RETIRADA
    verbo = verbos.get(tipo, "retirado")
    quando = f" em {_quando(instalacao.fim_em)}" if instalacao.fim_em else ""
    quem = base.quem(instalacao.fim_por) if instalacao.fim_por_id else "alguém"
    return (
        f"Este equipamento já foi {verbo} por {quem}{quando} ({instalacao.destino_texto}). "
        "Atualize a página para ver a situação atual."
    )


mensagem_ja_encerrada = _mensagem_ja_encerrada          # as telas a mostram quando a instalação já acabou


def _instalacao_esperada(instalacao_id) -> TecInstalacao:
    """A instalação que a tela mostrava, se ela AINDA está ativa; senão `ValidationError` dizendo
    quem a encerrou e quando."""
    try:
        pk = int(instalacao_id)
    except (TypeError, ValueError):
        raise ValidationError("Instalação não encontrada.") from None
    instalacao = (
        TecInstalacao.objects.select_related("equipamento__modelo__fabricante", "veiculo", "operador", "fim_por", "mov_fim")
        .filter(pk=pk).first()
    )
    if instalacao is None:
        raise ValidationError("Instalação não encontrada.")
    if instalacao.fim_em is not None:
        raise ValidationError(_mensagem_ja_encerrada(instalacao))
    return instalacao


def _porque_nao_se_instala(atual: TecEquipamento) -> str:
    """A razão, em frase, de um equipamento que NÃO está disponível não poder ser instalado."""
    if atual.situacao == Situacao.INSTALADO:
        onde = _instalacao_ativa(atual)
        lugar = f" em {onde.destino_texto} desde {_quando(onde.inicio_em)}" if onde else ""
        return f"{atual.codigo} já está instalado{lugar}. Retire-o ou transfira-o em vez de instalar de novo."
    if atual.situacao == Situacao.EM_MANUTENCAO:
        return f"{atual.codigo} está em manutenção e não pode ser instalado. Registre o retorno da manutenção primeiro."
    if atual.situacao == Situacao.INATIVO:
        return f"{atual.codigo} está inativo e não pode ser instalado."
    return f"{atual.codigo} não está disponível."


def _abrir(equipamento: TecEquipamento, destino: Destino, usuario, mov: TecMovimentacao, agora) -> TecInstalacao:
    return TecInstalacao.objects.create(
        equipamento=equipamento, **{destino.campo: destino.objeto},
        inicio_em=agora, inicio_por=base.usuario_ou_none(usuario), mov_inicio=mov,
    )


def _encerrar(instalacao: TecInstalacao, usuario, mov: TecMovimentacao, motivo: str, agora) -> TecInstalacao:
    """Encerra o período por UPDATE CONDICIONAL (`fim_em IS NULL`): se outra pessoa encerrou entre a
    leitura e agora, nenhuma linha casa e a operação inteira é recusada (a transação desfaz o resto)."""
    mudadas = TecInstalacao.objects.filter(pk=instalacao.pk, fim_em__isnull=True).update(
        fim_em=agora, fim_por=base.usuario_ou_none(usuario), motivo_fim=motivo, mov_fim=mov,
    )
    if mudadas != 1:
        atual = TecInstalacao.objects.select_related("veiculo", "operador", "fim_por", "mov_fim").get(pk=instalacao.pk)
        raise ValidationError(_mensagem_ja_encerrada(atual))
    instalacao.fim_em, instalacao.motivo_fim, instalacao.mov_fim = agora, motivo, mov
    return instalacao


def _tipo_da_saida(instalacao: TecInstalacao) -> str:
    """Sair de uma pessoa é desvinculação; sair de uma máquina (e, depois, de torre ou ponta) é retirada."""
    return Tipo.DESVINCULACAO if instalacao.operador_id else Tipo.RETIRADA


def _se_a_corrida_foi_perdida(token, mensagem: str) -> ResultadoInstalacao:
    """Um `IntegrityError` é (a) o outro envio do MESMO formulário ganhou a corrida do token — devolve o
    resultado dele — ou (b) a restrição de instalação ativa: outra pessoa chegou antes."""
    repetida = _resultado_repetida(token)
    if repetida is not None:
        return repetida
    raise ValidationError(mensagem)


# ════════════════════════════════════════════════════════
# Instalar, retirar, transferir, substituir
# ════════════════════════════════════════════════════════

def instalar(equipamento: TecEquipamento, destino_tipo, destino, usuario, motivo: str, *, observacoes: str = "", token=None) -> ResultadoInstalacao:
    """Instala um equipamento DISPONÍVEL numa máquina (`destino_tipo='veiculo'`) ou num operador
    (`'operador'`). Tira o equipamento do estoque (`disponível` → `instalado`, sem local de guarda),
    grava `instalacao` no razão (origem = o local onde ele estava) e abre a instalação ativa. Recusa:
    equipamento em manutenção, já instalado (diz onde) ou inativo; máquina desativada; operador
    desligado. Devolve `ResultadoInstalacao` (`instalacao`, `avisos`)."""
    usuario = base.exigir_usuario(usuario)
    token = estoque.token_de(token)
    anterior = _resultado_repetida(token)
    if anterior is not None:
        return anterior
    motivo = estoque.motivo_de(motivo)
    obs = base.texto_livre(observacoes, "observacoes", "as observações")
    alvo = resolver_destino(destino_tipo, destino)
    agora = timezone.now()
    try:
        with transaction.atomic():
            atual = _reler(equipamento)
            if atual.situacao != Situacao.DISPONIVEL:
                raise ValidationError(_porque_nao_se_instala(atual))
            origem = base.ponto_estoque(atual.local)
            estoque.transicao(atual, Situacao.DISPONIVEL, Situacao.INSTALADO, local=None, usuario=usuario, local_atual=atual.local)
            mov = estoque.gravar_movimentacao(
                Tipo.INSTALACAO, atual.modelo, usuario, equipamento=atual, origem=origem, destino=alvo.ponto,
                motivo=motivo, observacoes=obs, token=token, em=agora,
            )
            nova = _abrir(atual, alvo, usuario, mov, agora)
    except IntegrityError:
        return _se_a_corrida_foi_perdida(token, f"{equipamento.codigo} acabou de ser instalado por outra pessoa. Atualize a tela e confira antes de repetir.")
    return ResultadoInstalacao(operacao=mov.operacao, movimentacoes=[mov], equipamento=atual, instalacao=nova, avisos=list(alvo.avisos))


def retirar(instalacao_id_esperada, local, usuario, motivo: str, *, observacoes: str = "", token=None) -> ResultadoInstalacao:
    """Tira o equipamento da instalação `instalacao_id_esperada` e o devolve ao estoque no `local`
    escolhido (obrigatório: "disponível" é um lugar). Encerra a instalação, `instalado` →
    `disponível` e grava `retirada` (ou `desvinculacao`, se o destino era uma pessoa). Recusa:
    instalação já encerrada (diz por quem e quando); local inativo. O histórico do período fica."""
    usuario = base.exigir_usuario(usuario)
    token = estoque.token_de(token)
    anterior = _resultado_repetida(token)
    if anterior is not None:
        return anterior
    motivo = estoque.motivo_de(motivo)
    obs = base.texto_livre(observacoes, "observacoes", "as observações")
    guarda = estoque.exigir_local(local, "local", "o local onde o equipamento fica guardado")
    agora = timezone.now()
    try:
        with transaction.atomic():
            instalacao = _instalacao_esperada(instalacao_id_esperada)
            equipamento = _reler(instalacao.equipamento)
            de = _destino_da_instalacao(instalacao)
            estoque.transicao(equipamento, Situacao.INSTALADO, Situacao.DISPONIVEL, local=guarda, usuario=usuario)
            mov = estoque.gravar_movimentacao(
                _tipo_da_saida(instalacao), equipamento.modelo, usuario, equipamento=equipamento,
                origem=de.ponto, destino=base.ponto_estoque(guarda), motivo=motivo, observacoes=obs, token=token, em=agora,
            )
            _encerrar(instalacao, usuario, mov, motivo, agora)
    except IntegrityError:
        return _se_a_corrida_foi_perdida(token, "Esta instalação acabou de ser alterada por outra pessoa. Atualize a tela.")
    return ResultadoInstalacao(operacao=mov.operacao, movimentacoes=[mov], equipamento=equipamento, encerrada=instalacao)


def transferir(instalacao_id_esperada, destino_tipo, destino, usuario, motivo: str, *, observacoes: str = "", token=None) -> ResultadoInstalacao:
    """Passa o equipamento da instalação `instalacao_id_esperada` para OUTRO destino (de uma máquina
    para outra, de uma pessoa para outra, de uma máquina para uma pessoa…) numa transação: encerra a
    instalação anterior e abre a nova, e grava UMA `transferencia` com origem e destino. Não mexe no
    estoque (o equipamento continua instalado) e os dois períodos ficam no histórico. Recusa:
    instalação já encerrada; destino igual ao atual; destino que não recebe."""
    usuario = base.exigir_usuario(usuario)
    token = estoque.token_de(token)
    anterior = _resultado_repetida(token)
    if anterior is not None:
        return anterior
    motivo = estoque.motivo_de(motivo)
    obs = base.texto_livre(observacoes, "observacoes", "as observações")
    alvo = resolver_destino(destino_tipo, destino)
    agora = timezone.now()
    try:
        with transaction.atomic():
            antiga = _instalacao_esperada(instalacao_id_esperada)
            if (antiga.destino_tipo, antiga.destino_id) == (alvo.tipo, alvo.objeto.pk):
                raise ValidationError({"destino": f"O equipamento já está em {antiga.destino_texto}."})
            equipamento = _reler(antiga.equipamento)
            de = _destino_da_instalacao(antiga)
            mov = estoque.gravar_movimentacao(
                Tipo.TRANSFERENCIA, equipamento.modelo, usuario, equipamento=equipamento, origem=de.ponto, destino=alvo.ponto,
                motivo=motivo, observacoes=obs, token=token, em=agora,
            )
            _encerrar(antiga, usuario, mov, motivo, agora)             # antes de abrir a nova: a restrição é por equipamento
            nova = _abrir(equipamento, alvo, usuario, mov, agora)
    except IntegrityError:
        return _se_a_corrida_foi_perdida(token, "Esta instalação acabou de ser alterada por outra pessoa. Atualize a tela.")
    return ResultadoInstalacao(
        operacao=mov.operacao, movimentacoes=[mov], equipamento=equipamento, instalacao=nova, encerrada=antiga, avisos=list(alvo.avisos),
    )


def substituir(
    instalacao_id_esperada, equipamento_novo: TecEquipamento, usuario, motivo: str, *, destino_do_antigo: str = "disponivel",
    local=None, observacoes: str = "", token=None,
) -> ResultadoInstalacao:
    """Troca o equipamento da instalação `instalacao_id_esperada` por `equipamento_novo` NO MESMO
    destino. O antigo sai para `destino_do_antigo`: `disponivel` (exige `local` de guarda) ou
    `manutencao`; o novo (que precisa estar DISPONÍVEL) entra. Grava DUAS linhas `substituicao` com o
    mesmo `operacao` — uma por equipamento — e os dois períodos ficam no histórico. Recusa: instalação
    já encerrada; novo igual ao antigo ou indisponível. Devolve `ResultadoInstalacao`
    (`movimentacoes` = [antigo, novo]; `instalacao` = a do novo; `encerrada` = a do antigo)."""
    usuario = base.exigir_usuario(usuario)
    token = estoque.token_de(token)
    anterior = _resultado_repetida(token)
    if anterior is not None:
        return anterior
    motivo = estoque.motivo_de(motivo)
    obs = base.texto_livre(observacoes, "observacoes", "as observações")
    if destino_do_antigo not in ("disponivel", "manutencao"):
        raise ValidationError({"destino_do_antigo": "O equipamento substituído volta ao estoque ou vai para a manutenção."})
    guarda = estoque.exigir_local(local, "local", "o local onde o equipamento substituído fica guardado") if destino_do_antigo == "disponivel" else None
    agora = timezone.now()
    operacao = uuid.uuid4()
    try:
        with transaction.atomic():
            antiga = _instalacao_esperada(instalacao_id_esperada)
            saindo = _reler(antiga.equipamento)
            if equipamento_novo is None or getattr(equipamento_novo, "pk", None) is None:
                raise ValidationError({"equipamento_novo": "Escolha o equipamento que entra."})
            if equipamento_novo.pk == saindo.pk:
                raise ValidationError({"equipamento_novo": "O equipamento novo é o mesmo que já está instalado."})
            entrando = _reler(equipamento_novo)
            if entrando.situacao != Situacao.DISPONIVEL:
                raise ValidationError({"equipamento_novo": _porque_nao_se_instala(entrando)})
            de = _destino_da_instalacao(antiga)
            origem_novo = base.ponto_estoque(entrando.local)
            # os dois equipamentos mudam de situação por UPDATE condicional; o que perder a corrida recusa tudo
            if destino_do_antigo == "disponivel":
                estoque.transicao(saindo, Situacao.INSTALADO, Situacao.DISPONIVEL, local=guarda, usuario=usuario)
                destino_antigo = base.ponto_estoque(guarda)
            else:
                estoque.transicao(saindo, Situacao.INSTALADO, Situacao.EM_MANUTENCAO, local=None, usuario=usuario)
                destino_antigo = MANUTENCAO
            estoque.transicao(entrando, Situacao.DISPONIVEL, Situacao.INSTALADO, local=None, usuario=usuario, local_atual=entrando.local)
            mov_saida = estoque.gravar_movimentacao(
                Tipo.SUBSTITUICAO, saindo.modelo, usuario, equipamento=saindo, origem=de.ponto, destino=destino_antigo,
                motivo=motivo, observacoes=obs, operacao=operacao, token=token, em=agora,
            )
            mov_entrada = estoque.gravar_movimentacao(
                Tipo.SUBSTITUICAO, entrando.modelo, usuario, equipamento=entrando, origem=origem_novo, destino=de.ponto,
                motivo=motivo, observacoes=obs, operacao=operacao, em=agora,
            )
            _encerrar(antiga, usuario, mov_saida, motivo, agora)
            nova = _abrir(entrando, de, usuario, mov_entrada, agora)
    except IntegrityError:
        return _se_a_corrida_foi_perdida(token, "Esta instalação acabou de ser alterada por outra pessoa. Atualize a tela.")
    return ResultadoInstalacao(
        operacao=operacao, movimentacoes=[mov_saida, mov_entrada], equipamento=entrando, instalacao=nova, encerrada=antiga,
    )


# ════════════════════════════════════════════════════════
# Manutenção (o que muda de estado e de lugar; o chamado em si é da etapa de manutenção)
# ════════════════════════════════════════════════════════

def enviar_para_manutencao(equipamento: TecEquipamento, usuario, motivo: str, *, observacoes: str = "", token=None) -> ResultadoInstalacao:
    """Manda o equipamento para a assistência. Vale para o DISPONÍVEL (sai do estoque) e para o INSTALADO
    (a instalação é encerrada na mesma transação: ele não fica "instalado" numa máquina enquanto está
    fora). Fica `em manutenção`, sem local de guarda, e não pode ser instalado até o retorno. Recusa
    o que já está em manutenção ou inativo."""
    usuario = base.exigir_usuario(usuario)
    token = estoque.token_de(token)
    anterior = _resultado_repetida(token)
    if anterior is not None:
        return anterior
    motivo = estoque.motivo_de(motivo)
    obs = base.texto_livre(observacoes, "observacoes", "as observações")
    agora = timezone.now()
    try:
        with transaction.atomic():
            atual = _reler(equipamento)
            instalacao = None
            if atual.situacao == Situacao.INSTALADO:
                instalacao = _instalacao_ativa(atual)
                if instalacao is None:
                    raise ValidationError(f"{atual.codigo} consta como instalado mas não tem instalação ativa: avise quem administra o sistema.")
                origem = _destino_da_instalacao(instalacao).ponto
                estoque.transicao(atual, Situacao.INSTALADO, Situacao.EM_MANUTENCAO, local=None, usuario=usuario)
            elif atual.situacao == Situacao.DISPONIVEL:
                origem = base.ponto_estoque(atual.local)
                estoque.transicao(atual, Situacao.DISPONIVEL, Situacao.EM_MANUTENCAO, local=None, usuario=usuario, local_atual=atual.local)
            elif atual.situacao == Situacao.EM_MANUTENCAO:
                raise ValidationError(f"{atual.codigo} já está em manutenção.")
            else:
                raise ValidationError(f"{atual.codigo} está inativo e não vai para manutenção.")
            mov = estoque.gravar_movimentacao(
                Tipo.ENVIO_MANUTENCAO, atual.modelo, usuario, equipamento=atual, origem=origem, destino=MANUTENCAO,
                motivo=motivo, observacoes=obs, token=token, em=agora,
            )
            if instalacao is not None:
                _encerrar(instalacao, usuario, mov, motivo, agora)
    except IntegrityError:
        return _se_a_corrida_foi_perdida(token, f"{equipamento.codigo} acabou de ser alterado por outra pessoa. Atualize a tela.")
    return ResultadoInstalacao(operacao=mov.operacao, movimentacoes=[mov], equipamento=atual, encerrada=instalacao)


def retornar_da_manutencao(equipamento: TecEquipamento, local, usuario, motivo: str, *, observacoes: str = "", token=None) -> ResultadoInstalacao:
    """Registra a volta da assistência: `em manutenção` → `disponível` no `local` escolhido (obrigatório).
    Só então ele pode ser instalado de novo. Recusa o que não está em manutenção."""
    usuario = base.exigir_usuario(usuario)
    token = estoque.token_de(token)
    anterior = _resultado_repetida(token)
    if anterior is not None:
        return anterior
    motivo = estoque.motivo_de(motivo)
    obs = base.texto_livre(observacoes, "observacoes", "as observações")
    guarda = estoque.exigir_local(local, "local", "o local onde o equipamento fica guardado")
    agora = timezone.now()
    try:
        with transaction.atomic():
            atual = _reler(equipamento)
            if atual.situacao != Situacao.EM_MANUTENCAO:
                raise ValidationError(f"{atual.codigo} está {atual.get_situacao_display().lower()}: só equipamento em manutenção tem retorno.")
            estoque.transicao(atual, Situacao.EM_MANUTENCAO, Situacao.DISPONIVEL, local=guarda, usuario=usuario)
            mov = estoque.gravar_movimentacao(
                Tipo.RETORNO_MANUTENCAO, atual.modelo, usuario, equipamento=atual, origem=MANUTENCAO, destino=base.ponto_estoque(guarda),
                motivo=motivo, observacoes=obs, token=token, em=agora,
            )
    except IntegrityError:
        return _se_a_corrida_foi_perdida(token, f"{equipamento.codigo} acabou de ser alterado por outra pessoa. Atualize a tela.")
    return ResultadoInstalacao(operacao=mov.operacao, movimentacoes=[mov], equipamento=atual)


# ════════════════════════════════════════════════════════
# Material por quantidade aplicado num destino
# ════════════════════════════════════════════════════════

def materiais_aplicados(destino_tipo, destino_id) -> list[dict]:
    """O que há de MATERIAL por quantidade aplicado num destino: `[{"modelo", "quantidade"}]` com
    quantidade > 0, do que mais tem para o que menos. É o razão lido pelo destino
    (`Σ onde destino=(tipo, id) − Σ onde origem=(tipo, id)`); equipamento individual não entra (ele
    tem instalação própria)."""
    base_qs = TecMovimentacao.objects.filter(equipamento__isnull=True)
    liquido: dict = {}
    for linha in base_qs.filter(destino_tipo=destino_tipo, destino_id=destino_id).values("modelo_id").annotate(t=Sum("quantidade")):
        liquido[linha["modelo_id"]] = liquido.get(linha["modelo_id"], ZERO) + linha["t"]
    for linha in base_qs.filter(origem_tipo=destino_tipo, origem_id=destino_id).values("modelo_id").annotate(t=Sum("quantidade")):
        liquido[linha["modelo_id"]] = liquido.get(linha["modelo_id"], ZERO) - linha["t"]
    modelos = TecModelo.objects.select_related("fabricante", "categoria", "unidade").in_bulk([m for m, q in liquido.items() if q > 0])
    saida = [{"modelo": modelos[m], "quantidade": q} for m, q in liquido.items() if q > 0 and m in modelos]
    saida.sort(key=lambda item: (-item["quantidade"], item["modelo"].nome))
    return saida


def aplicacoes_do_material(modelo: TecModelo) -> list[dict]:
    """Onde há um material por quantidade APLICADO: `[{"tipo", "id", "rotulo", "quantidade"}]` com
    quantidade > 0, do que mais tem para o que menos (o razão lido por modelo: `Σ destino − Σ origem` de
    cada máquina ou operador). O rótulo é o nome de HOJE do destino."""
    liquido: dict = {}
    linhas = TecMovimentacao.objects.filter(modelo=modelo, equipamento__isnull=True)
    for r in linhas.filter(destino_tipo__in=DESTINOS_QUE_RECEBEM).values("destino_tipo", "destino_id").annotate(t=Sum("quantidade")):
        liquido[(r["destino_tipo"], r["destino_id"])] = liquido.get((r["destino_tipo"], r["destino_id"]), ZERO) + r["t"]
    for r in linhas.filter(origem_tipo__in=DESTINOS_QUE_RECEBEM).values("origem_tipo", "origem_id").annotate(t=Sum("quantidade")):
        liquido[(r["origem_tipo"], r["origem_id"])] = liquido.get((r["origem_tipo"], r["origem_id"]), ZERO) - r["t"]
    veiculos = FrotaVeiculo.objects.in_bulk([i for (t, i), q in liquido.items() if t == Ponto.VEICULO and q > 0])
    operadores = Usuario.objects.in_bulk([i for (t, i), q in liquido.items() if t == Ponto.OPERADOR and q > 0])
    saida = []
    for (tipo, ident), quantidade in liquido.items():
        if quantidade <= 0:
            continue
        alvo = veiculos.get(ident) if tipo == Ponto.VEICULO else operadores.get(ident)
        if alvo is None:
            continue
        rotulo = _rotulo_do_veiculo(alvo) if tipo == Ponto.VEICULO else alvo.nome
        saida.append({"tipo": tipo, "id": ident, "rotulo": rotulo, "quantidade": quantidade})
    saida.sort(key=lambda item: (-item["quantidade"], item["rotulo"]))
    return saida


def _aplicado(modelo: TecModelo, destino_tipo, destino_id) -> Decimal:
    entrou = TecMovimentacao.objects.filter(modelo=modelo, destino_tipo=destino_tipo, destino_id=destino_id).aggregate(t=Sum("quantidade"))["t"] or ZERO
    saiu = TecMovimentacao.objects.filter(modelo=modelo, origem_tipo=destino_tipo, origem_id=destino_id).aggregate(t=Sum("quantidade"))["t"] or ZERO
    return entrou - saiu


def aplicar_material(modelo, local, quantidade, destino_tipo, destino, usuario, motivo: str, *, observacoes: str = "", token=None) -> ResultadoInstalacao:
    """Aplica material por quantidade (cabo, conector, fonte…) numa máquina ou operador: sai do estoque
    do `local` (débito condicional: sem saldo, recusa) e o razão guarda `instalacao` com o destino de
    verdade. `modelo` precisa ser de controle por quantidade."""
    usuario = base.exigir_usuario(usuario)
    token = estoque.token_de(token)
    anterior = _resultado_repetida(token)
    if anterior is not None:
        return anterior
    modelo = estoque.exigir_modelo(modelo, individual=False)
    origem = estoque.exigir_local(local, "local", "o local de onde sai o material")
    qtd = estoque.quantidade_valida(quantidade, modelo)
    motivo = estoque.motivo_de(motivo)
    obs = base.texto_livre(observacoes, "observacoes", "as observações")
    alvo = resolver_destino(destino_tipo, destino)
    try:
        with transaction.atomic():
            saldo = estoque.debitar(modelo, origem, qtd)
            mov = estoque.gravar_movimentacao(
                Tipo.INSTALACAO, modelo, usuario, quantidade=qtd, saldo_depois=saldo,
                origem=base.ponto_estoque(origem), destino=alvo.ponto, motivo=motivo, observacoes=obs, token=token,
            )
    except IntegrityError:
        return _se_a_corrida_foi_perdida(token, "Esta operação acabou de ser registrada. Atualize a tela.")
    return ResultadoInstalacao(operacao=mov.operacao, movimentacoes=[mov], avisos=list(alvo.avisos))


def devolver_material(modelo, destino_tipo, destino, local, quantidade, usuario, motivo: str, *, observacoes: str = "", token=None) -> ResultadoInstalacao:
    """Tira material por quantidade de um destino e o devolve ao estoque do `local`. Só devolve o que
    está aplicado ali: o saldo do destino é conferido DEPOIS de gravar, dentro da transação, e a
    operação inteira é desfeita se ficaria negativo."""
    usuario = base.exigir_usuario(usuario)
    token = estoque.token_de(token)
    anterior = _resultado_repetida(token)
    if anterior is not None:
        return anterior
    modelo = estoque.exigir_modelo(modelo, individual=False)
    guarda = estoque.exigir_local(local, "local", "o local onde o material fica guardado")
    qtd = estoque.quantidade_valida(quantidade, modelo)
    motivo = estoque.motivo_de(motivo)
    obs = base.texto_livre(observacoes, "observacoes", "as observações")
    de = resolver_destino(destino_tipo, destino, para_receber=False)
    try:
        with transaction.atomic():
            saldo = estoque.creditar(modelo, guarda, qtd)                 # primeira escrita: pega a vez do escritor único
            mov = estoque.gravar_movimentacao(
                Tipo.RETIRADA, modelo, usuario, quantidade=qtd, saldo_depois=saldo,
                origem=de.ponto, destino=base.ponto_estoque(guarda), motivo=motivo, observacoes=obs, token=token,
            )
            sobrou = _aplicado(modelo, de.tipo, de.objeto.pk)
            if sobrou < 0:
                aplicado = sobrou + qtd
                un = modelo.unidade.sigla if modelo.unidade_id else "un"
                raise ValidationError({"quantidade": f"Só há {aplicado:f} {un} de {modelo} aplicado em {de.rotulo}; a devolução pede {qtd:f} {un}."})
    except IntegrityError:
        return _se_a_corrida_foi_perdida(token, "Esta operação acabou de ser registrada. Atualize a tela.")
    return ResultadoInstalacao(operacao=mov.operacao, movimentacoes=[mov])


# ════════════════════════════════════════════════════════
# Leitura (telas)
# ════════════════════════════════════════════════════════

def separar_destino(valor, campo: str = "destino") -> tuple[str, int]:
    """`"veiculo:12"` → `("veiculo", 12)`: o valor que o seletor de destino das telas manda. Vazio ou
    desconhecido levanta `ValidationError({campo})`."""
    tipo, _, ident = str(valor or "").strip().partition(":")
    if tipo not in CAMPO_DO_DESTINO or not ident.isdigit():
        raise ValidationError({campo: "Escolha para onde vai: uma máquina ou um operador."})
    return tipo, int(ident)


def opcoes_de_destino(excluir: tuple | None = None) -> list:
    """Os destinos que RECEBEM, para o seletor das telas: `[(grupo, [("veiculo:12", "TR-025 · Trator 25")])]`
    com máquinas (menos as desativadas) e operadores (menos os desligados). `excluir=(tipo, id)` tira o
    destino de onde o equipamento já está (transferir para o mesmo lugar não faz sentido)."""
    maquinas = [
        (f"{Ponto.VEICULO}:{v.pk}", _rotulo_do_veiculo(v))
        for v in FrotaVeiculo.objects.exclude(status=FrotaVeiculo.Status.DESATIVADO).order_by("codigo")
        if excluir != (Ponto.VEICULO, v.pk)
    ]
    operadores = [
        (f"{Ponto.OPERADOR}:{u.pk}", u.nome)
        for u in Usuario.objects.filter(status=StatusUsuarioChoices.ATIVO).order_by("nome")
        if excluir != (Ponto.OPERADOR, u.pk)
    ]
    return [(rotulo, itens) for rotulo, itens in (("Máquinas", maquinas), ("Operadores", operadores)) if itens]


def responsavel_do_equipamento(equipamento: TecEquipamento) -> str:
    """Quem responde pelo equipamento AGORA, derivado (não há campo): o operador da instalação ativa; se
    ela é numa máquina, o responsável da máquina; senão vazio (disponível no estoque, em manutenção)."""
    instalacao = equipamento.instalacao_ativa
    if instalacao is None:
        return ""
    if instalacao.operador_id:
        return instalacao.operador.nome
    if instalacao.veiculo_id and instalacao.veiculo.responsavel_id:
        return instalacao.veiculo.responsavel.nome
    return ""


def instalacoes_do_equipamento(equipamento: TecEquipamento):
    """Todos os períodos do equipamento (a ativa primeiro, depois do mais novo para o mais antigo), com
    destino, quem instalou/retirou e a movimentação de entrada e saída."""
    return (
        TecInstalacao.objects.filter(equipamento=equipamento)
        .select_related("veiculo", "operador", "inicio_por", "fim_por", "mov_inicio", "mov_fim")
        .order_by(F("fim_em").desc(nulls_first=True), "-pk")      # a ativa no topo; depois a mais recente
    )


def _linha_da_instalacao(i: TecInstalacao) -> dict:
    eq = i.equipamento
    return {
        "instalacao": i, "equipamento": eq, "modelo": eq.modelo, "fabricante": eq.modelo.fabricante, "categoria": eq.modelo.categoria,
        "grupo": eq.modelo.categoria.grupo, "desde": i.inicio_em, "por": i.inicio_por.username if i.inicio_por_id else "",
        "ate": i.fim_em, "motivo_fim": i.motivo_fim,
    }


def composicao_do_veiculo(veiculo: FrotaVeiculo) -> dict:
    """A parte TECNOLÓGICA da composição de uma máquina: `{"equipamentos": [linha], "materiais":
    [{"modelo", "quantidade"}], "total": N}`. Cada linha: `instalacao`, `equipamento`, `modelo`,
    `fabricante`, `categoria`, `grupo`, `desde`, `por` (login). Os chips não vêm daqui: são do vínculo
    de chip (`FrotaVinculo`), que a tela já lê por `frota_service.conectividade_do_veiculo`."""
    ativas = (
        TecInstalacao.objects.filter(veiculo=veiculo, fim_em__isnull=True)
        .select_related("equipamento__modelo__fabricante", "equipamento__modelo__categoria", "inicio_por")
        .order_by("equipamento__modelo__categoria__nome", "inicio_em", "pk")
    )
    linhas = [_linha_da_instalacao(i) for i in ativas]
    materiais = materiais_aplicados(Ponto.VEICULO, veiculo.pk)
    return {"equipamentos": linhas, "materiais": materiais, "total": len(linhas) + len(materiais)}


def historico_do_veiculo(veiculo: FrotaVeiculo, limite: int = 30) -> list[dict]:
    """Os equipamentos que JÁ passaram pela máquina (períodos encerrados), do mais recente. Mesmas
    chaves de `composicao_do_veiculo`, com `ate` e `motivo_fim`."""
    encerradas = (
        TecInstalacao.objects.filter(veiculo=veiculo, fim_em__isnull=False)
        .select_related("equipamento__modelo__fabricante", "equipamento__modelo__categoria", "inicio_por", "fim_por")
        .order_by("-fim_em", "-pk")[:limite]
    )
    return [{**_linha_da_instalacao(i), "retirado_por": i.fim_por.username if i.fim_por_id else ""} for i in encerradas]


# ════════════════════════════════════════════════════════
# Consistência: a instalação contra o estado e contra o razão
# ════════════════════════════════════════════════════════

def conferir_instalacoes() -> dict:
    """Confere, SEM corrigir nada, que (a) todo equipamento `instalado` tem exatamente uma instalação ativa e
    toda instalação ativa é de um equipamento `instalado`, e (b) o razão concorda com as instalações:
    para cada (modelo, destino), `Σ destino − Σ origem` das linhas de equipamento é o número de
    instalações ativas ali. Devolve `{"instalado_sem_instalacao": [...], "instalacao_sem_instalado": [...],
    "razao": [...], "ok": bool}`; divergência é defeito a investigar, não algo que se reconcilia."""
    ativas = TecInstalacao.objects.filter(fim_em__isnull=True)
    ids_ativos = set(ativas.values_list("equipamento_id", flat=True))
    instalados = set(TecEquipamento.objects.filter(situacao=Situacao.INSTALADO).values_list("pk", flat=True))
    sem_instalacao = list(TecEquipamento.objects.filter(pk__in=instalados - ids_ativos).select_related("modelo"))
    sem_instalado = list(TecEquipamento.objects.filter(pk__in=ids_ativos - instalados).select_related("modelo"))

    esperado: dict = {}
    for i in ativas.select_related("equipamento"):
        chave = (i.equipamento.modelo_id, i.destino_tipo, i.destino_id)
        esperado[chave] = esperado.get(chave, 0) + 1
    razao: dict = {}
    individuais = TecMovimentacao.objects.filter(equipamento__isnull=False)
    for linha in individuais.filter(destino_tipo__in=DESTINOS_QUE_RECEBEM).values("modelo_id", "destino_tipo", "destino_id").annotate(t=Sum("quantidade")):
        chave = (linha["modelo_id"], linha["destino_tipo"], linha["destino_id"])
        razao[chave] = razao.get(chave, ZERO) + linha["t"]
    for linha in individuais.filter(origem_tipo__in=DESTINOS_QUE_RECEBEM).values("modelo_id", "origem_tipo", "origem_id").annotate(t=Sum("quantidade")):
        chave = (linha["modelo_id"], linha["origem_tipo"], linha["origem_id"])
        razao[chave] = razao.get(chave, ZERO) - linha["t"]
    modelos = {m.pk: m for m in TecModelo.objects.select_related("fabricante")}
    divergencias = []
    for chave in set(esperado) | set(razao):
        no_razao, nas_instalacoes = razao.get(chave, ZERO), Decimal(esperado.get(chave, 0))
        if no_razao != nas_instalacoes:
            divergencias.append({"modelo": modelos.get(chave[0]), "destino_tipo": chave[1], "destino_id": chave[2], "razao": no_razao, "instalacoes": nas_instalacoes})
    return {
        "instalado_sem_instalacao": sem_instalacao, "instalacao_sem_instalado": sem_instalado, "razao": divergencias,
        "ok": not sem_instalacao and not sem_instalado and not divergencias,
    }
