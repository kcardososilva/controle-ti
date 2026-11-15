"""
Regras de negócio do monitoramento TIM M2M — cadastro, atualização, ocorrências e ciclo.

Camadas (cada uma só fala com a de baixo):

    views / commands  ->  frota_service / frota_vinculo_service / frota_import_service
                      ->  m2m_monitoramento_service  ->  tim_m2m_service (rede, token)
                                                     ->  m2m_interpretacao (mapeamento)

(Este módulo NÃO importa os services de frota no topo: `criar_chip` com `veiculo` chama
`frota_vinculo_service` por import tardio, para não fechar um ciclo.)

Decisões que moldam este módulo — e não são óbvias olhando o código:

  • SITUAÇÃO ATUAL ≠ HISTÓRICO. `atualizar` grava SEMPRE uma `M2MConsulta`
    (imutável). Só uma consulta VÁLIDA move os ponteiros do chip
    (`sessao_consulta`, `consumo_consulta`, estado, bytes). Falha só incrementa
    `falhas_consecutivas` e `ultima_falha_*`: o último dado bom continua lá, com a
    hora da consulta que o trouxe.
  • FALHA ≠ CHIP INDISPONÍVEL. 404, timeout, 5xx, credencial recusada: tudo isso é
    falha de CONSULTA. "Sem sessão" só existe quando o mapeamento confirmou.
  • NÃO MARTELAR. O ciclo para no primeiro 401/403 ou 429, não volta a consultar
    com credencial recusada e respeita `bloqueado_ate`. A pausa entre as chamadas é
    a configurada, mas nunca menor que o que `limite_req_minuto` permite.
  • O HISTÓRICO NUNCA É APAGADO. Chip não se exclui: desativa-se (com motivo).
  • TUDO O QUE MUDA É AUDITADO em `M2MEvento` (com de/para; nunca o token).
  • SÓ SE CONSULTA QUEM TEM INTEGRAÇÃO. O chip tem uma operadora; o ciclo e as
    estatísticas de conectividade só enxergam chips cuja operadora tem
    `integracao="tim_m2m"` (`Q_CONSULTAVEL`). Os demais são cadastrados e controlados,
    e a tela diz "sem integração — não consultado pela API".
  • ALERTA É REGRA. Cada tipo de ocorrência tem uma `FrotaRegraAlerta` (gera ou não,
    nível, e-mail e destinatários). `sem_sessao` só vale para chip VINCULADO: chip
    disponível sem sessão é o esperado. O texto de um alerta nunca afirma "fora de
    cobertura" nem "defeito" — o monitoramento só sabe o que a API respondeu.

Contratos de leitura para as telas (Fase B): `indicadores`, `contar_alertas_abertos`,
`estado_integracao`, `fita_consultas`, `linha_do_tempo`, `serie_consumo`,
`ultima_resposta_valida`, `frescor`, `validar_frequencia` e, na última seção do
arquivo, as leituras que as telas acrescentaram (`listar_chips`, `anotar_chips`,
`resumo_conectividade`, `resumo_do_chip`, `comparar_periodos`, `listar_ocorrencias`,
`previa_do_mapeamento`...). Cada docstring diz o que devolve.
"""

from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, time as hora_do_dia, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import (
    Case, Count, Exists, F, IntegerField, Max, Min, OuterRef, Prefetch, Q, Subquery, Sum, Value, When,
)
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.dateparse import parse_date

from ProjetoEstoque.models import (
    REGRAS_DE_ALERTA_PADRAO,
    ConectividadeOperadora,
    FrotaRegraAlerta,
    FrotaVeiculo,
    FrotaVinculo,
    Localidade,
    M2MChip,
    M2MConsulta,
    M2MEvento,
    M2MExecucao,
    M2MIntegracao,
    M2MOcorrencia,
    M2MOcorrenciaTratamento,
    Usuario,
)
from services import icone_service
from services import m2m_dados_service as dados_svc
from services import m2m_interpretacao as interp
from services import tim_m2m_service as tim

logger = logging.getLogger(__name__)

TIPOS_DE_CONSULTA = tim.TIPOS                       # ("sessao", "consumo")
INTERVALO_MINIMO_MINUTOS = 1
INTERVALO_MANUAL_S = 60                             # mesma atualização manual do mesmo chip
LIMIAR_LOTE = 2                                     # a partir de N chips vencidos, UMA leitura da lista completa da API
TRAVA_EXPIRA = timedelta(minutes=30)                # teto da trava: execução "presa" deixa de bloquear a próxima
TRAVA_MINIMA = timedelta(minutes=3)
TIQUE_PADRAO_MINUTOS = 1                            # intervalo da tarefa do Agendador (o pedido é 1 min)
MARGEM_DO_TIQUE_S = 10                              # o ciclo termina antes do próximo tique (que senão pularia)
ORCAMENTO_MINIMO_S = 20
ORCAMENTO_MAXIMO_S = 20 * 60
RESERVA_PARA_O_MANUAL = 0.9                         # 10% do limite de requisições fica para o "Atualizar" da tela
BACKOFF_MAXIMO_MINUTOS = 30                         # chip que falha é consultado de novo em intervalos crescentes, até aqui
CICLO_DO_LOTE_MINUTOS = 25                          # o "Atualizar todos" dura bem mais que um ciclo: dá a ele o tempo e a trava de um lote
ORIGEM_DO_LOTE = "manual"                           # `M2MExecucao.origem` e `M2MConsulta.origem` do "Atualizar todos"
PASSO_DO_PROGRESSO_DO_LOTE = 5                      # de quantos em quantos chips o lote grava "X de N" (um UPDATE barato)
RETENCAO_DAS_EXECUCOES = timedelta(days=7)
JANELA_DA_SINCRONIZACAO_MIN = 3                     # "atualizada" = ciclo bem-sucedido há menos de N tiques
ABERTAS = M2MOcorrencia.ESTADOS_ABERTOS
INTEGRACAO_TIM = ConectividadeOperadora.Integracao.TIM_M2M      # "tim_m2m"
# Chip que a API consulta: monitorado E de operadora com integração. Todo número que
# depende da API (ciclo, KPIs de sessão e consumo, "desatualizado") parte daqui.
Q_CONSULTAVEL = Q(monitorado=True, operadora__integracao=INTEGRACAO_TIM)
_ORDEM_DE_NIVEL = {"info": 0, "atencao": 1, "critico": 2}

ROTULO_SITUACAO = {
    "normal": "Normal",
    "pendente": "Pendente (ainda não consultado)",
    "erro_consulta": "Falha de consulta",
    "desatualizado": "Desatualizado",
    "desativado": "Monitoramento desativado",
    "sem_integracao": "Sem integração (não consultado pela API)",
}


class AtualizacaoRecusada(ValidationError):
    """A consulta nem foi tentada (sem token, API bloqueada por limite, chip
    desativado, operadora sem integração, atualização manual repetida cedo demais).
    `codigo`: `configuracao` | `bloqueada` | `desativado` | `sem_integracao` | `aguardar`.
    `aguardar_s` só no último."""

    def __init__(self, mensagem: str, codigo: str = "recusada", aguardar_s: int | None = None):
        super().__init__(mensagem, code=codigo)
        self.codigo = codigo
        self.aguardar_s = aguardar_s


def _usuario_ou_none(usuario):
    return usuario if getattr(usuario, "pk", None) else None


def _hhmm(momento) -> str:
    return timezone.localtime(momento).strftime("%H:%M")


def _data_hora(momento) -> str:
    return timezone.localtime(momento).strftime("%d/%m/%Y às %H:%M") if momento else "—"


# ═════════════════════════════════════════════════════════════
# Cadastro de chips
# ═════════════════════════════════════════════════════════════

def _entidade(valor, modelo, rotulo: str, campo: str, erros: dict):
    """Instância, id ou vazio -> instância ou None (erro registrado se o id não existe)."""
    if valor in (None, "", 0, "0"):
        return None
    if isinstance(valor, modelo):
        return valor
    try:
        return modelo.objects.get(pk=int(valor))
    except (ValueError, TypeError, modelo.DoesNotExist):
        erros[campo] = f"{rotulo} não encontrado(a)."
        return None


def _texto_limitado(valor, maximo: int, rotulo: str, campo: str, erros: dict, obrigatorio: bool = False) -> str:
    texto = (valor or "").strip() if isinstance(valor, str) or valor is None else str(valor).strip()
    if obrigatorio and not texto:
        erros[campo] = f"Informe {rotulo}."
    elif len(texto) > maximo:
        erros[campo] = f"{rotulo[:1].upper() + rotulo[1:]} aceita até {maximo} caracteres."
    return texto


def _limpar_dados_chip(dados: dict, atual: dict) -> dict:
    """Valida e normaliza os campos do cadastro. `atual` traz os valores vigentes
    (na criação, os padrões); só as chaves presentes em `dados` mudam.
    `ValidationError({campo: mensagem})` com tudo o que estiver errado."""
    erros: dict[str, str] = {}
    v = dict(atual)
    if "nome" in dados:
        v["nome"] = _texto_limitado(dados["nome"], 120, "o nome do chip", "nome", erros, obrigatorio=True)
    for campo, rotulo, normalizar in (
        ("iccid", "ICCID", tim.normalizar_iccid), ("imsi", "IMSI", tim.normalizar_imsi),
    ):
        if campo in dados:
            bruto = (dados[campo] or "").strip() if isinstance(dados[campo], str) or dados[campo] is None else str(dados[campo])
            if not bruto:
                v[campo] = ""
            else:
                try:
                    v[campo] = normalizar(bruto)
                except ValidationError as exc:
                    erros[campo] = exc.messages[0]
    if "identificador_consulta" in dados and dados["identificador_consulta"]:
        if dados["identificador_consulta"] not in M2MChip.Identificador.values:
            erros["identificador_consulta"] = "Escolha ICCID ou IMSI como identificador de consulta."
        else:
            v["identificador_consulta"] = dados["identificador_consulta"]
    elif "identificador_consulta" not in v or not v["identificador_consulta"]:
        v["identificador_consulta"] = "iccid" if v.get("iccid") else "imsi"
    if v.get("identificador_consulta") == "iccid" and not v.get("iccid") and "iccid" not in erros:
        erros["iccid"] = "Informe o ICCID, ou escolha o IMSI como identificador de consulta."
    if v.get("identificador_consulta") == "imsi" and not v.get("imsi") and "imsi" not in erros:
        erros["imsi"] = "Informe o IMSI, ou escolha o ICCID como identificador de consulta."
    if "descricao" in dados:
        v["descricao"] = _texto_limitado(dados["descricao"], 255, "a descrição", "descricao", erros)
    if "setor" in dados:
        v["setor"] = _texto_limitado(dados["setor"], 100, "o setor", "setor", erros)
    if "observacoes" in dados:
        v["observacoes"] = (dados["observacoes"] or "").strip()
    if "numero_linha" in dados:
        try:
            v["numero_linha"] = tim.normalizar_numero_linha(dados["numero_linha"])
        except ValidationError as exc:
            erros["numero_linha"] = exc.messages[0]
    if "operadora" in dados:
        operadora = _entidade(dados["operadora"], ConectividadeOperadora, "Operadora", "operadora", erros)
        if operadora is None:
            if "operadora" not in erros:
                erros["operadora"] = "Escolha a operadora."
        elif not operadora.ativo and getattr(atual.get("operadora"), "pk", None) != operadora.pk:
            erros["operadora"] = f"A operadora {operadora.nome} está inativa."
        else:
            v["operadora"] = operadora
    if v.get("operadora") is None and "operadora" not in erros:
        v["operadora"] = operadora_padrao()
        if v["operadora"] is None:
            erros["operadora"] = "Cadastre e ative uma operadora antes de cadastrar chips."
    if "localidade" in dados:
        v["localidade"] = _entidade(dados["localidade"], Localidade, "Unidade", "localidade", erros)
    if "responsavel" in dados:
        v["responsavel"] = _entidade(dados["responsavel"], Usuario, "Responsável", "responsavel", erros)
    if "limite_mensal_bytes" in dados:
        bruto = dados["limite_mensal_bytes"]
        if bruto in (None, ""):
            v["limite_mensal_bytes"] = None
        else:
            try:
                numero = int(str(bruto).strip())
                if numero <= 0 or numero > 2 ** 62:
                    raise ValueError
                v["limite_mensal_bytes"] = numero
            except ValueError:
                erros["limite_mensal_bytes"] = "O limite mensal deve ser um número inteiro de bytes maior que zero."
    if erros:
        raise ValidationError(erros)
    return v


def _conferir_unicidade(valores: dict, excluir_pk=None) -> None:
    erros: dict[str, str] = {}
    outros = M2MChip.objects.exclude(pk=excluir_pk) if excluir_pk else M2MChip.objects.all()
    if outros.filter(nome__iexact=valores["nome"]).exists():
        erros["nome"] = "Já existe um chip com este nome."
    if valores.get("iccid") and outros.filter(iccid=valores["iccid"]).exists():
        erros["iccid"] = "Já existe um chip cadastrado com este ICCID."
    if valores.get("imsi") and outros.filter(imsi=valores["imsi"]).exists():
        erros["imsi"] = "Já existe um chip cadastrado com este IMSI."
    if erros:
        raise ValidationError(erros)


_PADROES_CHIP = dict(
    nome="", iccid="", imsi="", identificador_consulta="", descricao="", setor="", observacoes="",
    operadora=None, numero_linha="", localidade=None, responsavel=None, limite_mensal_bytes=None,
)
CAMPOS_CADASTRO = tuple(_PADROES_CHIP)


def operadora_padrao() -> ConectividadeOperadora | None:
    """A operadora que um chip recebe quando o cadastro não diz qual: a ATIVA com
    integração TIM M2M (a que o módulo atendia antes) ou, na falta dela, a primeira
    ativa por nome. `None` se não há operadora ativa nenhuma."""
    ativas = ConectividadeOperadora.objects.filter(ativo=True)
    return ativas.filter(integracao=INTEGRACAO_TIM).order_by("pk").first() or ativas.order_by("nome", "pk").first()


def _json_do_valor(valor):
    if valor is None or isinstance(valor, (str, int, float, bool)):
        return valor
    return str(valor)


def _resolver_veiculo(valor, erros: dict):
    """`veiculo` do cadastro: instância, id ou vazio -> `FrotaVeiculo` ou `None`."""
    return _entidade(valor, FrotaVeiculo, "Veículo", "veiculo", erros)


def criar_chip(dados: dict, usuario=None, *, importacao=None, item_ti=None, item_ti_origem: str = "") -> M2MChip:
    """Cadastra um chip. `dados`: nome, iccid, imsi, identificador_consulta,
    descricao, operadora, numero_linha, localidade, setor, responsavel, observacoes,
    limite_mensal_bytes e, opcional, `veiculo` (`operadora`/`localidade`/`responsavel`/
    `veiculo` aceitam instância ou id; sem `operadora`, vale `operadora_padrao()`). O
    ICCID/IMSI é normalizado e validado; nome, ICCID e IMSI são únicos; o número da
    linha é só dígitos e NÃO é único.

    Tudo numa transação: grava o chip (nasce monitorado, `situacao_uso=disponivel`),
    `M2MEvento(cadastro)` e a movimentação de ENTRADA
    (`frota_vinculo_service.registrar_entrada`) — é por isso que todo chip tem uma
    entrada, venha do cadastro individual ou da planilha (`importacao`). Com `veiculo`,
    vincula em seguida (`vincular`, motivo "Vinculado no cadastro do chip.") e, se o
    veículo não puder receber o chip, NADA é gravado e o erro vem em `{"veiculo": ...}`.

    O chip nasce INTEGRADO ao estoque de TI (regra 66): com `item_ti`, liga a esse item (o caminho "criar o chip a partir
    do item"; falhando, nada é gravado); sem ele, liga ao item de chip LIVRE de mesmo ICCID se houver um só — em qualquer
    dúvida não liga, e a conciliação resolve depois.

    `usuario` é obrigatório (a movimentação precisa de um responsável). Levanta
    `ValidationError({campo: mensagem})`. Devolve o chip (recarregado: já vinculado, se
    foi o caso)."""
    from services import chip_estoque_ti_service as estoque_ti
    from services import frota_vinculo_service as vinculos

    dados = dict(dados)
    erros_veiculo: dict[str, str] = {}
    veiculo = _resolver_veiculo(dados.pop("veiculo", None), erros_veiculo)
    erros_chip: dict = {}
    try:
        valores = _limpar_dados_chip(dados, _PADROES_CHIP)
        _conferir_unicidade(valores)
    except ValidationError as exc:
        erros_chip = dict(exc.message_dict) if hasattr(exc, "error_dict") else {"__all__": exc.messages}
    if erros_chip or erros_veiculo:
        raise ValidationError({**erros_chip, **erros_veiculo})
    try:
        with transaction.atomic():
            chip = M2MChip(
                **valores, monitorado=True, situacao_uso_em=timezone.now(),
                criado_por=_usuario_ou_none(usuario), atualizado_por=_usuario_ou_none(usuario),
            )
            chip.full_clean(exclude=["nome", "iccid", "imsi"], validate_unique=False, validate_constraints=False)
            chip.save()
            M2MEvento.objects.create(
                chip=chip, tipo=M2MEvento.Tipo.CADASTRO, usuario=_usuario_ou_none(usuario),
                dados={
                    "nome": chip.nome, "iccid": chip.iccid, "imsi": chip.imsi,
                    "identificador_consulta": chip.identificador_consulta,
                    "operadora": chip.operadora.nome, "numero_linha": chip.numero_linha,
                },
            )
            vinculos.registrar_entrada(chip, usuario, importacao=importacao)
            if veiculo is not None:
                try:
                    vinculos.vincular(chip, veiculo, usuario, "Vinculado no cadastro do chip.")
                except ValidationError as exc:
                    raise ValidationError({"veiculo": " ".join(exc.messages)}) from None
            if item_ti is not None:
                estoque_ti.vincular(chip, item_ti, usuario, origem=item_ti_origem or "manual")
            else:
                estoque_ti.vincular_se_unico(chip, usuario)
    except IntegrityError:
        raise ValidationError("Já existe um chip com este nome, ICCID ou IMSI.") from None
    chip.refresh_from_db()
    return chip


def editar_chip(chip: M2MChip, dados: dict, usuario=None) -> M2MChip:
    """Altera o cadastro. Só as chaves presentes em `dados` mudam; se nada mudou de
    fato, não grava nada. Grava `M2MEvento(edicao)` com o de/para dos campos
    alterados (inclui `operadora` e `numero_linha`). Trocar o identificador de consulta,
    o ICCID ou o IMSI marca o chip para ser consultado na próxima rodada. O estado de
    monitoramento (sessão, consumo, falhas) e a situação de uso NÃO são editáveis aqui —
    a ligação com o veículo só muda por `vincular`/`transferir`/`desvincular`
    (`frota_vinculo_service`); um `veiculo` diferente do atual em `dados` é recusado."""
    dados = dict(dados)
    if "veiculo" in dados:
        pedido = dados.pop("veiculo")
        atual_id = (
            FrotaVinculo.objects.filter(chip=chip, fim_em__isnull=True).values_list("veiculo_id", flat=True).first()
        )
        pedido_id = getattr(pedido, "pk", pedido)
        if str(pedido_id or "") != str(atual_id or ""):
            raise ValidationError({
                "veiculo": "O veículo do chip não se altera na edição: use vincular, transferir ou desvincular.",
            })
    atuais = {campo: getattr(chip, campo) for campo in CAMPOS_CADASTRO}
    valores = _limpar_dados_chip(dados, atuais)
    _conferir_unicidade(valores, excluir_pk=chip.pk)

    alteracoes = {
        campo: {"de": _json_do_valor(atuais[campo]), "para": _json_do_valor(valores[campo])}
        for campo in CAMPOS_CADASTRO if valores[campo] != atuais[campo]
    }
    if not alteracoes:
        return chip
    for campo in alteracoes:
        setattr(chip, campo, valores[campo])
    campos = list(alteracoes) + ["atualizado_por", "updated_at"]
    chip.atualizado_por = _usuario_ou_none(usuario)
    if {"iccid", "imsi", "identificador_consulta"} & set(alteracoes):
        chip.proxima_consulta_em = None
        campos.append("proxima_consulta_em")
    try:
        with transaction.atomic():
            chip.full_clean(exclude=["nome", "iccid", "imsi"], validate_unique=False, validate_constraints=False)
            chip.save(update_fields=campos)
            M2MEvento.objects.create(
                chip=chip, tipo=M2MEvento.Tipo.EDICAO, usuario=_usuario_ou_none(usuario),
                dados={"alteracoes": alteracoes},
            )
    except IntegrityError:
        raise ValidationError("Já existe um chip com este nome, ICCID ou IMSI.") from None
    return chip


def desativar(chip: M2MChip, usuario=None, motivo: str = "") -> M2MChip:
    """Para de monitorar o chip. O MOTIVO é obrigatório. O histórico (consultas,
    eventos, ocorrências) fica intacto; as ocorrências em aberto são encerradas
    como resolvidas automaticamente (não há mais o que vigiar)."""
    motivo = (motivo or "").strip()
    if not motivo:
        raise ValidationError({"motivo": "Informe o motivo para desativar o monitoramento."})
    if len(motivo) > 255:
        raise ValidationError({"motivo": "O motivo aceita até 255 caracteres."})
    if not chip.monitorado:
        raise ValidationError("Este chip já está com o monitoramento desativado.")
    agora = timezone.now()
    with transaction.atomic():
        chip.monitorado = False
        chip.desativado_em, chip.desativado_motivo = agora, motivo
        chip.desativado_por = _usuario_ou_none(usuario)
        chip.atualizado_por = _usuario_ou_none(usuario)
        chip.save(update_fields=[
            "monitorado", "desativado_em", "desativado_por", "desativado_motivo", "atualizado_por", "updated_at",
        ])
        M2MEvento.objects.create(
            chip=chip, tipo=M2MEvento.Tipo.DESATIVADO, usuario=_usuario_ou_none(usuario), dados={"motivo": motivo},
        )
        for ocorrencia in M2MOcorrencia.objects.filter(chip=chip, estado__in=ABERTAS):
            _resolver_automatica(ocorrencia, agora, "O monitoramento do chip foi desativado.")
    return chip


def reativar(chip: M2MChip, usuario=None, motivo: str = "") -> M2MChip:
    """Volta a monitorar o chip (motivo opcional, mas registrado). Zera o contador
    de falhas — falhas de antes da pausa não devem disparar alerta na hora — e
    marca o chip para a próxima rodada. As falhas antigas seguem no histórico."""
    if chip.monitorado:
        raise ValidationError("Este chip já está sendo monitorado.")
    motivo = (motivo or "").strip()[:255]
    with transaction.atomic():
        chip.monitorado = True
        chip.desativado_em, chip.desativado_motivo, chip.desativado_por = None, "", None
        chip.falhas_consecutivas = 0
        chip.proxima_consulta_em = None
        chip.atualizado_por = _usuario_ou_none(usuario)
        chip.save(update_fields=[
            "monitorado", "desativado_em", "desativado_por", "desativado_motivo",
            "falhas_consecutivas", "proxima_consulta_em", "atualizado_por", "updated_at",
        ])
        M2MEvento.objects.create(
            chip=chip, tipo=M2MEvento.Tipo.REATIVADO, usuario=_usuario_ou_none(usuario), dados={"motivo": motivo},
        )
    return chip


# ═════════════════════════════════════════════════════════════
# Atualização (uma consulta ou mais, de um chip)
# ═════════════════════════════════════════════════════════════

@dataclass
class ResultadoAtualizacao:
    """O que `atualizar` fez. `consultas`: as `M2MConsulta` gravadas, na ordem.
    `interrompida_por`: "" ou o resultado (`falha_autenticacao`/`limite_excedido`)
    que fez parar antes de consultar todos os tipos pedidos."""
    consultas: list = field(default_factory=list)
    interrompida_por: str = ""
    sessoes_novas: int = 0           # sessões de dados que apareceram nesta atualização
    sessoes_atualizadas: int = 0     # sessões já conhecidas que a plataforma alterou

    @property
    def houve_falha(self) -> bool:
        return any(not c.valida for c in self.consultas)

    @property
    def ok(self) -> bool:
        return bool(self.consultas) and not self.houve_falha

    def resumo(self) -> str:
        """Frase pronta para a tela: "Sessão: sucesso. Consumo: Erro de rede."."""
        partes = [f"{c.get_tipo_display()}: {c.get_resultado_display().lower()}" for c in self.consultas]
        return ". ".join(partes) + "." if partes else "Nenhuma consulta feita."


def _tipos_validos(tipos) -> tuple[str, ...]:
    if isinstance(tipos, str):
        tipos = (tipos,)
    saida = []
    for tipo in tipos or TIPOS_DE_CONSULTA:
        if tipo not in TIPOS_DE_CONSULTA:
            raise ValueError(f"Tipo de consulta inválido: {tipo!r}.")
        if tipo not in saida:
            saida.append(tipo)
    return tuple(saida)


def pausa_efetiva_s(cfg: M2MIntegracao) -> float:
    """Pausa entre duas chamadas, em segundos: a configurada, mas nunca menor que
    60 / `limite_req_minuto` (assim o teto declarado vale também em rajada)."""
    return max(cfg.pausa_entre_chamadas_ms / 1000.0, 60.0 / max(1, cfg.limite_req_minuto))


def _conferir_pode_consultar(chip: M2MChip, origem: str, cfg: M2MIntegracao, agora) -> None:
    if origem != "teste" and not chip.monitorado:
        raise AtualizacaoRecusada(
            "Este chip está com o monitoramento desativado. Reative-o para consultar.", "desativado",
        )
    if not chip.consultavel_pela_api:
        raise AtualizacaoRecusada(
            f"A operadora {chip.operadora.nome} não tem integração: este chip não é consultado pela API.", "sem_integracao",
        )
    motivo = tim.motivo_nao_configurada()
    if motivo:
        raise AtualizacaoRecusada(motivo, "configuracao")
    if cfg.bloqueado_ate and cfg.bloqueado_ate > agora:
        raise AtualizacaoRecusada(
            f"A API limitou as requisições; as consultas estão pausadas até {_hhmm(cfg.bloqueado_ate)}.",
            "bloqueada", aguardar_s=int((cfg.bloqueado_ate - agora).total_seconds()),
        )
    if origem == "manual":
        # Reserva a vaga de forma atômica: dois cliques seguidos não geram duas consultas.
        reservado = M2MChip.objects.filter(pk=chip.pk).filter(
            Q(ultima_tentativa_em__isnull=True) | Q(ultima_tentativa_em__lt=agora - timedelta(seconds=INTERVALO_MANUAL_S))
        ).update(ultima_tentativa_em=agora)
        if not reservado:
            ultima = M2MChip.objects.filter(pk=chip.pk).values_list("ultima_tentativa_em", flat=True).first()
            espera = max(1, INTERVALO_MANUAL_S - int((agora - ultima).total_seconds())) if ultima else INTERVALO_MANUAL_S
            raise AtualizacaoRecusada(
                f"Este chip foi consultado há instantes. Aguarde {espera} s para atualizar de novo.",
                "aguardar", aguardar_s=espera,
            )


def atualizar(
    chip: M2MChip, tipos=TIPOS_DE_CONSULTA, origem: str = "manual", usuario=None, *,
    cliente=None, pausa_s: float = 0.0, avaliar: bool = True, cfg: M2MIntegracao | None = None,
    conferir: bool = True,
) -> ResultadoAtualizacao:
    """Consulta a API para o chip (`tipos`: "sessao", "consumo" ou ambos) e grava
    uma `M2MConsulta` por tipo, SEMPRE — sucesso ou falha.

    • Sucesso: move os ponteiros da situação atual (consulta, estado, bytes), zera
      `falhas_consecutivas` e grava `M2MEvento(estado_sessao_mudou)` se o estado
      mudou.
    • Falha: só `falhas_consecutivas` (+1 por atualização, não por consulta) e
      `ultima_falha_*`. NUNCA toca nos últimos dados válidos.
    • 401/403 e 429 interrompem na hora (não consulta o tipo seguinte); o 429
      grava `bloqueado_ate`.
    • Com `cfg.dados_por_eventos`, a atualização também lê as sessões de dados do chip
      (`m2m_dados_service`) e a falha dessa leitura é falha da consulta.
    • `origem="manual"`: recusa repetir o mesmo chip em menos de 60 s.
    • `avaliar=True` roda `avaliar_ocorrencias(chip)` no fim (o ciclo passa False
      e avalia todos de uma vez).

    Levanta `AtualizacaoRecusada` (a consulta nem foi tentada). Devolve
    `ResultadoAtualizacao`."""
    tipos = _tipos_validos(tipos)
    cfg = cfg or M2MIntegracao.get()
    agora = timezone.now()
    if conferir:
        _conferir_pode_consultar(chip, origem, cfg, agora)
    if cliente is None:
        try:
            cliente = tim.ClienteTIM()
        except tim.TimM2MErro as exc:
            raise AtualizacaoRecusada(str(exc), "configuracao") from exc

    resultado = ResultadoAtualizacao()
    anterior = None
    # As sessões de dados da plataforma (o que o portal mostra) são lidas UMA vez por atualização e
    # entram na resposta de cada tipo: o resumo do SIM Card demora a refletir o uso (regra 61).
    ler_dados = dados_svc.usar_eventos(cfg, cliente)
    dados = None
    if ler_dados:
        cliente.ritmo_eventos_s = pausa_efetiva_s(cfg)
    for i, tipo in enumerate(tipos):
        # Sessão e consumo são o MESMO registro da API: depois de uma resposta válida a
        # segunda consulta sai da memória (sem chamada), então só se espera quando a
        # anterior falhou e a próxima vai de fato à rede.
        if i and pausa_s > 0 and anterior is not None and not anterior.valida and anterior.tentativas > 0:
            time.sleep(pausa_s)
        try:
            resposta = cliente.consultar(chip, tipo, origem)
        except tim.TimM2MErro as exc:
            raise AtualizacaoRecusada(tim.sanear(str(exc)), "configuracao") from exc
        anterior = resposta
        if ler_dados and resposta.valida:
            if dados is None:
                dados = dados_svc.sincronizar(chip, cliente, origem)
                resultado.sessoes_novas, resultado.sessoes_atualizadas = dados.novas, dados.atualizadas
            resposta = dados_svc.enriquecer(resposta, tipo, dados)
        resultado.consultas.append(_registrar(chip, tipo, resposta, origem, usuario, cfg.mapeamento))
        if resposta.interrompe_ciclo:
            resultado.interrompida_por = resposta.resultado
            break
    _fechar_atualizacao(chip, resultado, cfg)
    if avaliar:
        avaliar_ocorrencias(chip, cfg=cfg)
    return resultado


def _registrar(chip, tipo, resposta, origem, usuario, mapeamento) -> M2MConsulta:
    """Grava a consulta (e, se válida, a situação atual) numa transação curta —
    a chamada HTTP já terminou, então não há rede dentro dela."""
    agora = timezone.now()
    normalizados = interp.campos_normalizados(tipo, resposta.payload, mapeamento, valida=resposta.valida)
    with transaction.atomic():
        consulta = M2MConsulta.objects.create(
            chip=chip, tipo=tipo, origem=origem, usuario=_usuario_ou_none(usuario),
            iniciada_em=agora - timedelta(milliseconds=resposta.duracao_ms),
            duracao_ms=resposta.duracao_ms, tentativas=max(resposta.tentativas, 0),
            http_status=resposta.http_status,
            identificador_tipo=resposta.identificador_tipo or chip.identificador_consulta,
            resultado=resposta.resultado, payload=resposta.payload,
            mensagem_erro=tim.sanear(resposta.mensagem_erro)[:2000],
            **normalizados,
        )
        _atualizar_saude_da_integracao(resposta, agora)
        if resposta.valida:
            _aplicar_dado_valido(chip, consulta, tipo, normalizados)
    return consulta


def _atualizar_saude_da_integracao(resposta, agora) -> None:
    campos = {}
    if resposta.http_status is not None:
        campos["ultimo_status_http"] = resposta.http_status
        if 200 <= resposta.http_status < 300:
            campos["autenticada_em"] = agora
    if resposta.resultado == "falha_autenticacao":
        campos["falha_autenticacao_em"] = agora
    if resposta.resultado == "limite_excedido":
        campos["bloqueado_ate"] = agora + timedelta(seconds=resposta.retry_after_s or tim.BLOQUEIO_PADRAO_S)
    if campos:
        M2MIntegracao.get()
        M2MIntegracao.objects.filter(pk=1).update(**campos)


def _aplicar_dado_valido(chip: M2MChip, consulta: M2MConsulta, tipo: str, normalizados: dict) -> None:
    ultima_valida = consulta.iniciada_em
    if chip.ultima_valida_em and chip.ultima_valida_em > ultima_valida:
        ultima_valida = chip.ultima_valida_em
    chip.ultima_valida_em = ultima_valida
    campos = ["ultima_valida_em"]
    if tipo == "sessao":
        anterior = (
            M2MChip.objects.filter(pk=chip.pk).values_list("sessao_estado", flat=True).first()
            or M2MChip.SessaoEstado.NAO_CONSULTADO
        )
        novo = normalizados["estado_interpretado"] or M2MChip.SessaoEstado.NAO_INTERPRETADO
        chip.sessao_consulta, chip.sessao_estado = consulta, novo
        campos += ["sessao_consulta", "sessao_estado"]
        if anterior != novo:
            M2MEvento.objects.create(
                chip=chip, tipo=M2MEvento.Tipo.ESTADO_SESSAO_MUDOU,
                dados={
                    "de": anterior, "para": novo, "estado_bruto": normalizados["estado_bruto"],
                    "consulta_id": consulta.pk,
                },
            )
    else:
        chip.consumo_consulta, chip.consumo_bytes = consulta, normalizados["volume_bytes"]
        campos += ["consumo_consulta", "consumo_bytes"]
    chip.save(update_fields=campos)


def intervalo_do_chip(chip: M2MChip, cfg: M2MIntegracao) -> int:
    """De quanto em quanto tempo ESTE chip deve ser consultado, em minutos: o ritmo curto para quem tem sessão
    ativa (é onde o consumo e a sessão andam), o de repouso para os demais."""
    ativo = chip.sessao_estado == M2MChip.SessaoEstado.ATIVA
    return max(1, cfg.intervalo_minutos if ativo else cfg.intervalo_repouso_minutos)


def espera_apos_falha(intervalo_min: int, falhas_seguidas: int) -> int:
    """Backoff: a cada falha seguida o chip espera o DOBRO (1, 2, 4, 8, 16 min no ritmo de 1 min), até
    `BACKOFF_MAXIMO_MINUTOS`. Insistir num chip que a API recusa só gasta o limite de requisições dos outros."""
    n = max(1, falhas_seguidas)
    return max(intervalo_min, min(intervalo_min * (2 ** (n - 1)), BACKOFF_MAXIMO_MINUTOS))


def _fechar_atualizacao(chip: M2MChip, resultado: ResultadoAtualizacao, cfg: M2MIntegracao) -> None:
    if not resultado.consultas:
        return
    agora = timezone.now()
    intervalo = intervalo_do_chip(chip, cfg)
    if resultado.houve_falha:
        intervalo = espera_apos_falha(intervalo, (chip.falhas_consecutivas or 0) + 1)
    campos = {
        "ultima_tentativa_em": agora,
        "proxima_consulta_em": agora + timedelta(minutes=intervalo),
    }
    if resultado.houve_falha:
        ultima = [c for c in resultado.consultas if not c.valida][-1]
        campos.update(
            ultima_falha_em=agora, ultima_falha_tipo=ultima.resultado,
            falhas_consecutivas=F("falhas_consecutivas") + 1,
        )
    else:
        campos["falhas_consecutivas"] = 0
    M2MChip.objects.filter(pk=chip.pk).update(**campos)
    chip.refresh_from_db(fields=[
        "ultima_tentativa_em", "proxima_consulta_em", "ultima_falha_em", "ultima_falha_tipo", "falhas_consecutivas",
    ])


def testar_conexao(chip: M2MChip, usuario=None, *, cliente=None) -> M2MConsulta:
    """Teste de conexão da tela de Integração: UMA consulta de sessão do chip
    escolhido, com `origem="teste"` (vale para qualquer chip, até desativado).
    Atualiza a saúde da integração como qualquer consulta; se der certo, os dados
    do chip também (é uma resposta válida, não há por que descartá-la). Guarda o
    resumo em `M2MIntegracao.ultimo_teste` e grava `M2MEvento(teste_conexao)`.
    Levanta `AtualizacaoRecusada`; devolve a `M2MConsulta` gravada."""
    resultado = atualizar(chip, ("sessao",), "teste", usuario, cliente=cliente, avaliar=False)
    consulta = resultado.consultas[0]
    resumo = {
        "em": timezone.now().isoformat(), "chip_id": chip.pk, "chip": chip.nome,
        "resultado": consulta.resultado, "http_status": consulta.http_status,
        "duracao_ms": consulta.duracao_ms, "mensagem": consulta.mensagem_erro,
        "consulta_id": consulta.pk,
    }
    M2MIntegracao.objects.filter(pk=1).update(ultimo_teste=resumo)
    M2MEvento.objects.create(
        chip=chip, tipo=M2MEvento.Tipo.TESTE_CONEXAO, usuario=_usuario_ou_none(usuario),
        dados={k: resumo[k] for k in ("resultado", "http_status", "duracao_ms", "consulta_id")},
    )
    return consulta


# ═════════════════════════════════════════════════════════════
# Ocorrências
# ═════════════════════════════════════════════════════════════

def _rotulo_resultado(codigo: str) -> str:
    try:
        return M2MConsulta.Resultado(codigo).label
    except ValueError:
        return codigo or "falha"


def _desde_sem_sessao(chip: M2MChip):
    """Desde quando as consultas VÁLIDAS dizem "sem sessão" (a primeira da sequência atual)."""
    base = M2MConsulta.objects.filter(
        chip=chip, tipo="sessao", resultado__in=M2MConsulta.RESULTADOS_VALIDOS,
    )
    ultima_diferente = base.exclude(estado_interpretado="sem_sessao").order_by("-iniciada_em").values_list(
        "iniciada_em", flat=True,
    ).first()
    sequencia = base.filter(estado_interpretado="sem_sessao")
    if ultima_diferente:
        sequencia = sequencia.filter(iniciada_em__gt=ultima_diferente)
    return sequencia.order_by("iniciada_em").values_list("iniciada_em", flat=True).first()


TIPOS_DE_CHIP = (
    "falha_recorrente", "sem_sessao", "desatualizado", "consumo_elevado", "identificador_divergente",
)

# O que a pessoa precisa FAZER, por tipo. Aparece no e-mail e nas telas; nenhuma frase
# afirma "fora de cobertura" nem "defeito": o monitoramento só sabe o que a API respondeu.
ACAO_NECESSARIA = {
    "falha_recorrente": (
        "Conferir a credencial e a conexão com a API da operadora; se a falha continuar, conferir o "
        "identificador (ICCID/IMSI) cadastrado."
    ),
    "sem_sessao": (
        "Verificar se o equipamento do veículo está ligado e com o chip bem encaixado; se a situação "
        "continuar, acionar a operadora."
    ),
    "desatualizado": "Atualizar a consulta do chip; se não funcionar, conferir a integração (token e rede).",
    "consumo_elevado": "Avaliar o consumo do chip e, se for o caso, ajustar o plano ou o limite cadastrado.",
    "identificador_divergente": "Conferir o ICCID/IMSI cadastrado com o do chip físico e corrigir o cadastro.",
    "veiculo_sem_chip": (
        "Vincular um chip disponível ao veículo — ou desmarcar \"exige conectividade\" no cadastro, se ele "
        "não precisa de chip."
    ),
    "chip_desvinculado": "Conferir se a desvinculação foi intencional; o chip continua cadastrado.",
    "alteracao_vinculo": "Conferir se a alteração de vínculo foi intencional.",
    "veiculo_sem_atualizacao": "Atualizar a consulta dos chips do veículo; se não funcionar, conferir a integração.",
}


def acao_necessaria(tipo: str) -> str:
    """A frase de "ação necessária" do tipo de ocorrência (vazio se o tipo é desconhecido)."""
    return ACAO_NECESSARIA.get(tipo, "")


def _condicoes(chip: M2MChip, cfg: M2MIntegracao, agora) -> dict:
    """{tipo_da_ocorrencia: (nivel, descricao)} para cada condição de CHIP que está
    VERDADEIRA agora. `nivel` é `None` ("use o nível da regra") salvo no consumo acima
    de 100% do limite, que sempre sobe para crítico.

    Só chip CONSULTÁVEL (monitorado e de operadora com integração) tem condição. E
    `sem_sessao` só vale para chip VINCULADO a um veículo: chip disponível, em manutenção
    ou inativo sem sessão é o esperado, não um problema."""
    cond: dict = {}
    if not chip.monitorado or not chip.consultavel_pela_api:
        return cond
    mapa = cfg.mapeamento or {}
    base_consumo = (mapa.get("consumo") or {}).get("base") or 1000

    # falha de consulta recorrente — texto sempre diz que é a CONSULTA que falha
    if chip.falhas_consecutivas >= max(1, cfg.falhas_para_alerta):
        cond["falha_recorrente"] = (None, (
            f"A consulta à API da {chip.operadora.nome} falhou {chip.falhas_consecutivas} vezes seguidas "
            f"(última falha: {_rotulo_resultado(chip.ultima_falha_tipo).lower()}, em {_data_hora(chip.ultima_falha_em)}). "
            "É uma falha de CONSULTA — rede, credencial, API ou identificador —, não indica que o chip "
            "esteja indisponível. "
            + (f"O último dado válido é de {_data_hora(chip.ultima_valida_em)} e continua guardado."
               if chip.ultima_valida_em else "Nenhuma consulta deste chip teve sucesso até agora.")
        ))

    # sem sessão, confirmado pelo mapeamento, há X horas — só de chip vinculado
    if chip.situacao_uso == M2MChip.SituacaoUso.VINCULADO and chip.sessao_estado == M2MChip.SessaoEstado.SEM_SESSAO:
        desde = _desde_sem_sessao(chip) or (chip.sessao_consulta.iniciada_em if chip.sessao_consulta_id else None)
        if desde and agora - desde >= timedelta(hours=cfg.horas_sem_sessao_alerta):
            horas = int((agora - desde).total_seconds() // 3600)
            cond["sem_sessao"] = (None, (
                f"O mapeamento configurado confirma que o chip está sem sessão desde {_data_hora(desde)} "
                f"({horas} h; o alerta dispara a partir de {cfg.horas_sem_sessao_alerta} h)."
            ))

    # último dado válido velho demais (chip já consultado, ou cadastrado há tempo sem nunca ter dado certo)
    if chip.ultima_tentativa_em is not None:
        referencia = chip.ultima_valida_em or chip.created_at
        if referencia and agora - referencia > timedelta(hours=cfg.horas_desatualizado):
            horas = int((agora - referencia).total_seconds() // 3600)
            cond["desatualizado"] = (None, (
                (f"O último dado válido é de {_data_hora(chip.ultima_valida_em)}" if chip.ultima_valida_em
                 else f"O chip foi cadastrado em {_data_hora(chip.created_at)} e nenhuma consulta teve sucesso")
                + f" — há {horas} h; o alerta dispara a partir de {cfg.horas_desatualizado} h sem dado novo."
            ))

    # consumo elevado: só com limite mensal cadastrado no chip
    if chip.limite_mensal_bytes and chip.consumo_bytes is not None:
        pct = chip.consumo_bytes * 100 / chip.limite_mensal_bytes
        if pct >= cfg.pct_consumo_alerta:
            cond["consumo_elevado"] = ("critico" if pct >= 100 else None, (
                f"O último volume informado pela operadora é {interp.formatar_bytes(chip.consumo_bytes, base_consumo)}, "
                f"{pct:.0f}% do limite mensal cadastrado ({interp.formatar_bytes(chip.limite_mensal_bytes, base_consumo)}); "
                f"o alerta dispara a partir de {cfg.pct_consumo_alerta}%."
            ))

    # identificador diferente do que a API informa (só se o mapeamento de identidade existe)
    if mapa.get("identidade"):
        diferencas = []
        for consulta in (chip.sessao_consulta, chip.consumo_consulta):
            if consulta is None or not consulta.valida:
                continue
            achado = interp.identidade_do_payload(consulta.payload, mapa)
            for campo, rotulo in (("iccid", "ICCID"), ("imsi", "IMSI")):
                cadastrado = getattr(chip, campo)
                if achado[campo] and cadastrado and achado[campo] != cadastrado:
                    diferencas.append(f"{rotulo} informado pela API: {achado[campo]}; cadastrado: {cadastrado}")
        if diferencas:
            cond["identificador_divergente"] = (None, (
                "O identificador devolvido pela API não bate com o cadastro — pode ser um chip trocado ou "
                "um cadastro errado. " + "; ".join(sorted(set(diferencas))) + "."
            ))
    return cond


# ── regras de alerta ─────────────────────────────────────────

def regras_de_alerta() -> dict:
    """`{tipo: FrotaRegraAlerta}` para TODOS os tipos de ocorrência, com os destinatários
    já carregados. Uma regra apagada é recriada com o padrão (`REGRAS_DE_ALERTA_PADRAO`):
    nenhum tipo fica sem regra. Em laço (o ciclo), carregue UMA vez e repasse."""
    existentes = {r.tipo: r for r in FrotaRegraAlerta.objects.prefetch_related("destinatarios_usuarios")}
    for tipo in M2MOcorrencia.Tipo.values:
        if tipo not in existentes:
            existentes[tipo], _ = FrotaRegraAlerta.objects.get_or_create(
                tipo=tipo, defaults=REGRAS_DE_ALERTA_PADRAO[tipo],
            )
    return existentes


def regra_do_tipo(tipo: str) -> FrotaRegraAlerta:
    """A regra de UM tipo (criada com o padrão se faltar)."""
    return regras_de_alerta()[tipo]


def destinatarios_da_regra(regra: FrotaRegraAlerta) -> list[str]:
    """Os e-mails da regra, sem repetição e na ordem em que foram dados: os digitados em
    `destinatarios_emails` (separados por vírgula, ponto e vírgula, espaço ou quebra de
    linha; os inválidos são ignorados) e os dos usuários ATIVOS escolhidos. Lista vazia =
    "sem destinatário próprio": o envio cai na lista padrão do sistema (`.env`)."""
    vistos: dict[str, str] = {}
    for bruto in re.split(r"[,;\s]+", regra.destinatarios_emails or ""):
        email = bruto.strip()
        if not email:
            continue
        try:
            validate_email(email)
        except ValidationError:
            continue
        vistos.setdefault(email.lower(), email)
    for usuario in regra.destinatarios_usuarios.all():
        if usuario.is_active and usuario.email:
            vistos.setdefault(usuario.email.strip().lower(), usuario.email.strip())
    return list(vistos.values())


# ── abrir / resolver ─────────────────────────────────────────

def _resolver_automatica(ocorrencia: M2MOcorrencia, agora, motivo: str = "A condição que gerou o alerta deixou de existir.") -> None:
    mudou = M2MOcorrencia.objects.filter(pk=ocorrencia.pk, estado__in=ABERTAS).update(
        estado=M2MOcorrencia.Estado.RESOLVIDA, resolvida_em=agora, resolvida_por=None,
    )
    if mudou:
        M2MOcorrenciaTratamento.objects.create(
            ocorrencia=ocorrencia, usuario=None, acao=M2MOcorrenciaTratamento.Acao.RESOLVIDA_AUTOMATICA,
            comentario=motivo, em=agora,
        )


def _do_alvo(tipo: str, chip: M2MChip | None, veiculo: FrotaVeiculo | None) -> dict:
    """O filtro que identifica a ocorrência do (chip, tipo) ou, sem chip, do (veículo, tipo)
    — o mesmo critério das duas restrições de deduplicação do banco."""
    if chip is not None:
        return {"chip": chip, "tipo": tipo}
    return {"chip__isnull": True, "veiculo": veiculo, "tipo": tipo}


def _veiculo_atual_do_chip(chip: M2MChip) -> FrotaVeiculo | None:
    vinculo = FrotaVinculo.objects.filter(chip=chip, fim_em__isnull=True).select_related("veiculo").first()
    return vinculo.veiculo if vinculo else None


def _abrir(
    tipo: str, nivel: str, descricao: str, agora, *, chip: M2MChip | None = None, veiculo: FrotaVeiculo | None = None,
) -> M2MOcorrencia | None:
    """Abre a ocorrência do chip (ou, sem `chip`, do veículo) — ou não: se o TI
    DESCARTOU a anterior e a condição não chegou a sumir desde então, o alerta não volta
    (descartar é "já sei disso"). Numa ocorrência de chip, o veículo a que ele está
    ligado agora entra como contexto (não participa da deduplicação)."""
    descartada = (
        M2MOcorrencia.objects.filter(**_do_alvo(tipo, chip, veiculo), estado=M2MOcorrencia.Estado.DESCARTADA)
        .exclude(tratamentos__acao=M2MOcorrenciaTratamento.Acao.CONDICAO_ENCERRADA)
        .order_by("-detectada_em", "-pk").first()
    )
    if descartada is not None:
        M2MOcorrencia.objects.filter(pk=descartada.pk).update(ultima_deteccao_em=agora)
        return None
    if chip is not None and veiculo is None:
        veiculo = _veiculo_atual_do_chip(chip)
    try:
        with transaction.atomic():
            ocorrencia = M2MOcorrencia.objects.create(
                chip=chip, veiculo=veiculo, tipo=tipo, nivel=nivel, descricao=descricao,
                detectada_em=agora, ultima_deteccao_em=agora,
            )
            M2MOcorrenciaTratamento.objects.create(
                ocorrencia=ocorrencia, usuario=None, acao=M2MOcorrenciaTratamento.Acao.DETECTADA,
                comentario="Detectada automaticamente pelo monitoramento.", em=agora,
            )
    except IntegrityError:
        return None  # outra execução abriu a mesma ocorrência um instante antes
    return ocorrencia


def _marcar_condicao_encerrada(descartadas, agora) -> None:
    """Descartada + condição que sumiu = pode voltar a alertar quando reaparecer."""
    for descartada in descartadas.exclude(tratamentos__acao=M2MOcorrenciaTratamento.Acao.CONDICAO_ENCERRADA):
        M2MOcorrenciaTratamento.objects.create(
            ocorrencia=descartada, usuario=None, acao=M2MOcorrenciaTratamento.Acao.CONDICAO_ENCERRADA,
            comentario="A condição deixou de existir; um novo alerta pode ser aberto se ela voltar.", em=agora,
        )


def avaliar_ocorrencias(
    chip: M2MChip, *, agora=None, cfg: M2MIntegracao | None = None, notificar: bool = True, regras: dict | None = None,
) -> list:
    """Compara as condições de alerta do CHIP com as ocorrências em aberto:

    • condição verdadeira e nada aberto  -> abre, SE a regra do tipo gera ocorrência (e
      avisa por e-mail se a regra manda);
    • condição verdadeira e já aberta    -> só atualiza `ultima_deteccao_em`/`deteccoes`
      (deduplicação: no máximo uma ocorrência em aberto por chip e tipo);
    • condição falsa e há aberta         -> resolve sozinha, com tratamento automático
      (mesmo que a regra tenha sido desligada depois).

    Tipos de chip: `falha_recorrente`, `sem_sessao` (só chip VINCULADO), `desatualizado`,
    `consumo_elevado` (só com limite mensal cadastrado), `identificador_divergente`. Os de
    veículo estão em `avaliar_ocorrencias_de_veiculos`. O nível é o da regra (o consumo
    acima de 100% do limite sobe para crítico). Devolve a lista de ocorrências NOVAS.
    `regras`: o dict de `regras_de_alerta()` (o ciclo carrega uma vez); `notificar=False`
    deixa o e-mail para quem chamou (o ciclo manda um só, consolidado). Chip sem
    integração não tem condição nenhuma."""
    cfg = cfg or M2MIntegracao.get()
    agora = agora or timezone.now()
    regras = regras or regras_de_alerta()
    condicoes = _condicoes(chip, cfg, agora)
    abertas = {o.tipo: o for o in M2MOcorrencia.objects.filter(chip=chip, estado__in=ABERTAS)}
    novas: list[M2MOcorrencia] = []
    encerradas_sem_condicao: set[str] = set()

    for tipo in TIPOS_DE_CHIP:
        condicao = condicoes.get(tipo)
        existente = abertas.get(tipo)
        regra = regras[tipo]
        if condicao:
            nivel_da_condicao, descricao = condicao
            nivel = nivel_da_condicao or regra.nivel
            if existente is not None:
                M2MOcorrencia.objects.filter(pk=existente.pk).update(
                    ultima_deteccao_em=agora, deteccoes=F("deteccoes") + 1, descricao=descricao, nivel=nivel,
                )
            elif regra.gera_ocorrencia:
                nova = _abrir(tipo, nivel, descricao, agora, chip=chip)
                if nova is not None:
                    novas.append(nova)
        else:
            encerradas_sem_condicao.add(tipo)
            if existente is not None:
                _resolver_automatica(existente, agora)

    if encerradas_sem_condicao:
        _marcar_condicao_encerrada(
            M2MOcorrencia.objects.filter(chip=chip, estado=M2MOcorrencia.Estado.DESCARTADA, tipo__in=encerradas_sem_condicao),
            agora,
        )

    if notificar:
        notificar_ocorrencias(novas, regras=regras)
    return novas


def _veiculos_que_exigem_e_nao_tem_chip():
    """Veículo ATIVO, que exige conectividade (o dele, ou o padrão do tipo) e não tem
    nenhum vínculo ativo."""
    com_chip = FrotaVinculo.objects.filter(veiculo=OuterRef("pk"), fim_em__isnull=True)
    return (
        FrotaVeiculo.objects.filter(status=FrotaVeiculo.Status.ATIVO)
        .filter(Q(exige_conectividade=True) | Q(exige_conectividade__isnull=True, tipo__exige_conectividade=True))
        .annotate(tem_chip=Exists(com_chip)).filter(tem_chip=False)
        .select_related("tipo")
    )


veiculos_sem_chip_exigido = _veiculos_que_exigem_e_nao_tem_chip   # nome público, para as leituras da frota


def _veiculos_sem_atualizacao(agora, cfg: M2MIntegracao) -> dict:
    """`{veiculo: descricao}` dos veículos ATIVOS que têm chip consultável vinculado mas
    NENHUM deles com consulta válida dentro de `horas_desatualizado`. Um chip vinculado há
    menos que o limite ainda está no prazo (acabou de entrar; não se cobra dado de antes
    do vínculo). Chip sem integração ou com o monitoramento desligado não conta: não há
    consulta a esperar dele."""
    limite = agora - timedelta(hours=cfg.horas_desatualizado)
    vinculos = (
        FrotaVinculo.objects.filter(fim_em__isnull=True, veiculo__status=FrotaVeiculo.Status.ATIVO)
        .filter(chip__monitorado=True, chip__operadora__integracao=INTEGRACAO_TIM)
        .select_related("chip", "veiculo__tipo")
        .order_by("veiculo__codigo", "pk")
    )
    por_veiculo: dict = {}
    for vinculo in vinculos:
        por_veiculo.setdefault(vinculo.veiculo, []).append(vinculo)
    saida = {}
    for veiculo, lista in por_veiculo.items():
        no_prazo = any(
            (v.chip.ultima_valida_em is not None and v.chip.ultima_valida_em >= limite) or v.inicio_em >= limite
            for v in lista
        )
        if no_prazo:
            continue
        validas = [v.chip.ultima_valida_em for v in lista if v.chip.ultima_valida_em]
        ultimo = f"A consulta mais recente com dado válido é de {_data_hora(max(validas))}." if validas else (
            "Nenhuma consulta dos chips teve sucesso desde que foram vinculados."
        )
        nomes = ", ".join(v.chip.nome for v in lista)
        saida[veiculo] = (
            f"Nenhum dos {len(lista)} chip(s) vinculado(s) ao veículo ({nomes}) tem consulta válida nas últimas "
            f"{cfg.horas_desatualizado} h. {ultimo}"
        )
    return saida


def avaliar_ocorrencias_de_veiculos(
    *, agora=None, cfg: M2MIntegracao | None = None, notificar: bool = True, regras: dict | None = None,
) -> list:
    """O mesmo ciclo abrir/atualizar/resolver de `avaliar_ocorrencias`, agora para as
    condições de VEÍCULO (sem chip na ocorrência):

    • `veiculo_sem_chip` — veículo `ativo` + exige conectividade (o dele, ou o padrão do
      tipo) + nenhum vínculo ativo;
    • `veiculo_sem_atualizacao` — veículo `ativo` cujos chips vinculados e consultáveis
      não têm, nenhum, consulta válida dentro de `horas_desatualizado`.

    No máximo uma em aberto por (veículo, tipo). Respeita `gera_ocorrencia` da regra. Veículo
    em manutenção, inativo ou desativado não gera nenhuma das duas (não se espera conexão
    dele). Devolve as ocorrências NOVAS."""
    cfg = cfg or M2MIntegracao.get()
    agora = agora or timezone.now()
    regras = regras or regras_de_alerta()
    novas: list[M2MOcorrencia] = []

    verdadeiras = {
        "veiculo_sem_chip": {
            v: (
                f"O veículo {v.codigo} · {v.nome} está ativo, exige conectividade e não tem nenhum chip vinculado."
            )
            for v in _veiculos_que_exigem_e_nao_tem_chip()
        },
        "veiculo_sem_atualizacao": _veiculos_sem_atualizacao(agora, cfg),
    }
    for tipo, condicoes in verdadeiras.items():
        regra = regras[tipo]
        ids = {v.pk for v in condicoes}
        abertas = {
            o.veiculo_id: o
            for o in M2MOcorrencia.objects.filter(tipo=tipo, chip__isnull=True, veiculo__isnull=False, estado__in=ABERTAS)
        }
        for veiculo, descricao in condicoes.items():
            existente = abertas.get(veiculo.pk)
            if existente is not None:
                M2MOcorrencia.objects.filter(pk=existente.pk).update(
                    ultima_deteccao_em=agora, deteccoes=F("deteccoes") + 1, descricao=descricao, nivel=regra.nivel,
                )
            elif regra.gera_ocorrencia:
                nova = _abrir(tipo, regra.nivel, descricao, agora, veiculo=veiculo)
                if nova is not None:
                    novas.append(nova)
        for veiculo_id, existente in abertas.items():
            if veiculo_id not in ids:
                _resolver_automatica(existente, agora)
        _marcar_condicao_encerrada(
            M2MOcorrencia.objects.filter(
                tipo=tipo, chip__isnull=True, veiculo__isnull=False, estado=M2MOcorrencia.Estado.DESCARTADA,
            ).exclude(veiculo_id__in=ids),
            agora,
        )

    if notificar:
        notificar_ocorrencias(novas, regras=regras)
    return novas


def abrir_ocorrencia_de_evento(
    tipo: str, chip: M2MChip, veiculo: FrotaVeiculo | None, descricao: str, *, usuario=None, agora=None,
) -> M2MOcorrencia | None:
    """Ocorrência INFORMATIVA de um evento do vínculo (`chip_desvinculado` ou
    `alteracao_vinculo`), chamada por `frota_vinculo_service` na mesma transação da
    movimentação. Só abre se a regra do tipo gera ocorrência; não tem "condição que some":
    fica aberta até alguém tratá-la. Se já há uma aberta do mesmo (chip, tipo), só a
    atualiza (nova detecção, descrição mais recente) e NÃO manda outro e-mail. Devolve a
    ocorrência NOVA, ou `None` (regra desligada, ou atualização da que já existia). O
    e-mail, quando a regra manda, sai por `transaction.on_commit`."""
    if tipo not in M2MOcorrencia.TIPOS_DE_EVENTO:
        raise ValueError(f"Tipo de ocorrência não é de evento: {tipo!r}.")
    regras = regras_de_alerta()
    regra = regras[tipo]
    if not regra.gera_ocorrencia:
        return None
    agora = agora or timezone.now()
    existente = M2MOcorrencia.objects.filter(chip=chip, tipo=tipo, estado__in=ABERTAS).first()
    if existente is not None:
        M2MOcorrencia.objects.filter(pk=existente.pk).update(
            ultima_deteccao_em=agora, deteccoes=F("deteccoes") + 1, descricao=descricao, veiculo=veiculo,
        )
        return None
    try:
        with transaction.atomic():
            ocorrencia = M2MOcorrencia.objects.create(
                chip=chip, veiculo=veiculo, tipo=tipo, nivel=regra.nivel, descricao=descricao,
                detectada_em=agora, ultima_deteccao_em=agora,
            )
            M2MOcorrenciaTratamento.objects.create(
                ocorrencia=ocorrencia, usuario=_usuario_ou_none(usuario), acao=M2MOcorrenciaTratamento.Acao.DETECTADA,
                comentario="Registrada pela movimentação do vínculo.", em=agora,
            )
    except IntegrityError:
        return None
    notificar_ocorrencias([ocorrencia], regras=regras)
    return ocorrencia


def notificar_ocorrencias(ocorrencias, regras: dict | None = None) -> None:
    """E-mail (canal `m2m_ocorrencias`) para as ocorrências NOVAS cuja REGRA manda
    avisar (`envia_email`), e para os destinatários DA REGRA: ocorrências de regras com
    destinatários diferentes saem em e-mails separados; cada e-mail é consolidado (uma
    linha por ocorrência). Regra sem destinatário próprio cai na lista padrão do sistema.
    O canal da Central de Alertas continua sendo a chave liga/desliga (junto da
    chave-mestra de e-mails) — o envio respeita as duas. Fire-and-forget por
    `transaction.on_commit` (regra 3): nunca bloqueia a requisição nem o ciclo no SMTP, e
    uma falha de envio não desfaz nada."""
    ocorrencias = list(ocorrencias or [])
    if not ocorrencias:
        return
    regras = regras or regras_de_alerta()
    grupos: dict[tuple, list[int]] = {}
    for ocorrencia in ocorrencias:
        regra = regras.get(ocorrencia.tipo)
        if regra is None or not regra.envia_email:
            continue
        grupos.setdefault(tuple(destinatarios_da_regra(regra)), []).append(ocorrencia.pk)
    if not grupos:
        return

    def enviar():
        try:
            from services import email_alertas
        except Exception:  # pragma: no cover — import do próprio projeto
            logger.exception("Frotas: o módulo de e-mail não pôde ser carregado.")
            return
        for destinatarios, ids in grupos.items():
            try:
                lista = list(
                    M2MOcorrencia.objects.filter(pk__in=ids)
                    .select_related("chip", "chip__operadora", "veiculo").order_by("veiculo__codigo", "chip__nome", "tipo")
                )
                email_alertas.alerta_m2m_ocorrencias(lista, destinatarios=list(destinatarios) or None)
            except Exception:  # e-mail nunca derruba o monitoramento
                logger.exception("Frotas: o e-mail de ocorrências não foi enviado.")

    transaction.on_commit(enviar)


ACOES_DE_TRATAMENTO = ("assumir", "resolver", "descartar", "comentar")


def tratar_ocorrencia(ocorrencia: M2MOcorrencia, acao: str, usuario=None, comentario: str = "") -> M2MOcorrencia:
    """Andamento manual: `assumir` (aberta -> em tratamento), `resolver` e
    `descartar` (exigem comentário e encerram) e `comentar` (só registra). Todas
    viram uma linha em `M2MOcorrenciaTratamento`. Levanta `ValidationError`."""
    if acao not in ACOES_DE_TRATAMENTO:
        raise ValidationError("Ação desconhecida.")
    comentario = (comentario or "").strip()
    agora = timezone.now()
    with transaction.atomic():
        oc = M2MOcorrencia.objects.select_for_update().get(pk=ocorrencia.pk)
        if acao != "comentar" and not oc.aberta_ou_em_tratamento:
            raise ValidationError("Esta ocorrência já foi encerrada.")
        if acao == "comentar" and not comentario:
            raise ValidationError("Escreva o comentário.")
        if acao in ("resolver", "descartar") and not comentario:
            raise ValidationError(
                f"Explique, no comentário, por que a ocorrência está sendo {'resolvida' if acao == 'resolver' else 'descartada'}."
            )
        if acao == "assumir":
            if oc.estado == M2MOcorrencia.Estado.EM_TRATAMENTO:
                raise ValidationError("Esta ocorrência já está em tratamento.")
            oc.estado = M2MOcorrencia.Estado.EM_TRATAMENTO
            oc.save(update_fields=["estado"])
        elif acao in ("resolver", "descartar"):
            oc.estado = M2MOcorrencia.Estado.RESOLVIDA if acao == "resolver" else M2MOcorrencia.Estado.DESCARTADA
            oc.resolvida_em, oc.resolvida_por = agora, _usuario_ou_none(usuario)
            oc.save(update_fields=["estado", "resolvida_em", "resolvida_por"])
        M2MOcorrenciaTratamento.objects.create(
            ocorrencia=oc, usuario=_usuario_ou_none(usuario), acao=acao, comentario=comentario, em=agora,
        )
    return oc


# ═════════════════════════════════════════════════════════════
# Ciclo (o que o Agendador dispara)
# ═════════════════════════════════════════════════════════════

@dataclass
class ResumoCiclo:
    """O que `executar_ciclo` fez. `executou` False = pulou, e `motivo_pulo` diz
    por quê: `sem_configuracao` | `credencial_recusada` | `bloqueada` | `em_andamento`.
    `consultados`: chips consultados; `sucessos`: chips em que todas as consultas
    deram certo; `falhas`: chips com pelo menos uma consulta com falha.
    `interrompido_por`: "" | `falha_autenticacao` | `limite_excedido`."""
    executou: bool = False
    motivo_pulo: str = ""
    mensagem: str = ""
    consultados: int = 0
    sucessos: int = 0
    falhas: int = 0
    interrompido_por: str = ""
    novas_ocorrencias: int = 0
    erro: str = ""
    vencidos: int = 0                # chips vencidos quando o ciclo começou
    pendentes: int = 0               # vencidos que ficaram para o próximo ciclo
    chamadas: int = 0                # requisições HTTP que o ciclo fez
    execucao_id: int | None = None   # a `M2MExecucao` gravada
    mudancas: dict = field(default_factory=dict)

    @property
    def exige_atencao(self) -> bool:
        """Deve virar código de saída ≠ 0 no Agendador: configuração faltando,
        credencial recusada ou ciclo que terminou em erro. Pausa por limite (429)
        e "já em andamento" são normais e saem com 0."""
        return (
            self.motivo_pulo in ("sem_configuracao", "credencial_recusada")
            or self.interrompido_por == "falha_autenticacao"
            or bool(self.erro and self.interrompido_por != "limite_excedido")
        )

    def texto(self) -> str:
        """Resumo em uma ou duas linhas, em pt-BR."""
        if not self.executou:
            return f"Ciclo não executado: {self.mensagem}"
        linhas = [
            f"Chips consultados: {self.consultados} (sem falha: {self.sucessos}, com falha: {self.falhas}). "
            f"Ocorrências novas: {self.novas_ocorrencias}."
        ]
        if self.chamadas:
            linhas.append(f"Requisições à API: {self.chamadas}.")
        if self.pendentes:
            linhas.append(f"{self.pendentes} chip(s) vencido(s) ficam para o próximo ciclo.")
        if self.erro:
            linhas.append(self.erro)
        return " ".join(linhas)


def _proxima_prevista(agora):
    """Quando o próximo chip fica "vencido": `None` sem chips monitorados; `agora` se
    algum já está vencido ou nunca foi consultado."""
    dados = M2MChip.objects.filter(Q_CONSULTAVEL).aggregate(
        n=Count("pk"), sem_data=Count("pk", filter=Q(proxima_consulta_em__isnull=True)), menor=Min("proxima_consulta_em"),
    )
    if not dados["n"]:
        return None
    if dados["sem_data"] or dados["menor"] is None:
        return agora
    return max(dados["menor"], agora)


def _registrar_pulo(campos: dict) -> None:
    """Um ciclo que pulou também prova que a tarefa do Agendador está viva."""
    M2MIntegracao.objects.filter(pk=1).update(**campos)


def _logar_erro_inesperado(exc: Exception) -> None:
    """O traceback vai para o log (é o que permite diagnosticar), mas passa pelo
    saneamento: nenhuma exceção pode levar o token junto para `logs/erros.log`."""
    import traceback

    try:
        segredo = tim.token()
    except Exception:
        segredo = ""
    logger.error(
        "TIM M2M: o ciclo terminou com erro inesperado.\n%s",
        tim.sanear("".join(traceback.format_exception(exc)), segredo),
    )


def _fechar_execucao(execucao, resumo: ResumoCiclo, inicio_monotonico: float) -> None:
    """Grava como o ciclo terminou. Nunca levanta: o registro é observação, e uma falha ao gravá-lo não pode
    esconder o resultado do ciclo nem derrubar a tarefa do Agendador."""
    if execucao is None:
        return
    try:
        if resumo.interrompido_por:                  # 401/403 ou 429: o ciclo parou antes de acabar
            estado = M2MExecucao.Estado.INTERROMPIDA
        elif resumo.erro:                            # nada deu certo, ou erro inesperado
            estado = M2MExecucao.Estado.ERRO
        elif resumo.falhas:
            estado = M2MExecucao.Estado.COM_FALHAS
        else:
            estado = M2MExecucao.Estado.CONCLUIDA
        M2MExecucao.objects.filter(pk=execucao.pk).update(
            estado=estado, concluida_em=timezone.now(),
            duracao_ms=int((time.monotonic() - inicio_monotonico) * 1000),
            consultados=resumo.consultados, sucessos=resumo.sucessos, falhas=resumo.falhas,
            chamadas=resumo.chamadas, pendentes=resumo.pendentes, interrompido_por=resumo.interrompido_por,
            erro=tim.sanear(resumo.erro)[:2000], mudancas=resumo.mudancas or {},
        )
        if execucao.pk % 100 == 0:          # a poda é barata e não precisa rodar a cada minuto
            podar_execucoes()
    except Exception:  # noqa: BLE001
        logger.warning("TIM M2M: não foi possível gravar o registro da execução %s.", execucao.pk, exc_info=True)


def trava_expira(tique_minutos: int | None) -> timedelta:
    """Quanto tempo uma execução marcada como "em andamento" bloqueia a próxima. É proporcional ao tique da
    tarefa (3 tiques), com piso de 3 min e teto de 30: com a tarefa de 1 em 1 min, uma execução que MORREU no
    meio (queda de energia, processo morto) não pode deixar o monitoramento parado meia hora."""
    tique = max(1, int(tique_minutos or TIQUE_PADRAO_MINUTOS))
    return min(TRAVA_EXPIRA, max(TRAVA_MINIMA, timedelta(minutes=3 * tique)))


def lote_em_curso(agora=None) -> M2MExecucao | None:
    """A execução do "Atualizar todos" que está rodando agora, ou `None`. É a que mantém a trava de 30 min mesmo quando
    quem olha usa o prazo curto do ciclo de 1 minuto (3 min): sem isto o ciclo agendado julgaria "morta" uma execução
    de lote que dura 8 min ou mais, tomaria a trava e as duas consultariam ao mesmo tempo — estourando juntas o limite de
    requisições da API. Execução que MORREU deixa de contar em `TRAVA_EXPIRA`, como qualquer outra."""
    agora = agora or timezone.now()
    return M2MExecucao.objects.filter(
        origem=ORIGEM_DO_LOTE, estado=M2MExecucao.Estado.EM_ANDAMENTO, iniciada_em__gt=agora - TRAVA_EXPIRA,
    ).order_by("-iniciada_em").first()


def orcamento_do_ciclo_s(tique_minutos: int | None) -> int:
    """Quanto tempo (s) o ciclo pode gastar consultando chips: o tique menos uma margem, para a execução
    terminar ANTES do próximo tique (que, senão, pularia por "já em andamento") — o que sobrar vencido
    é o primeiro da fila no ciclo seguinte. Piso de 20 s; teto de 20 min."""
    tique = max(1, int(tique_minutos or TIQUE_PADRAO_MINUTOS))
    return max(ORCAMENTO_MINIMO_S, min(tique * 60 - MARGEM_DO_TIQUE_S, ORCAMENTO_MAXIMO_S))


def _mudancas_do_ciclo(antes: dict, depois: dict, novas: int, atualizadas: int) -> dict:
    """O que o ciclo ATUALIZOU, para a tela dizer "quais informações mudaram": quantos chips trocaram de estado
    de sessão, quantos tiveram o consumo alterado e quantas sessões de dados entraram ou mudaram."""
    estado = sum(1 for pk, v in antes.items() if pk in depois and depois[pk][0] != v[0])
    consumo = sum(1 for pk, v in antes.items() if pk in depois and depois[pk][1] != v[1])
    return {"estado_sessao": estado, "consumo": consumo, "sessoes_novas": novas, "sessoes_atualizadas": atualizadas}


def podar_execucoes(agora=None) -> int:
    """Apaga o registro de execuções com mais de `RETENCAO_DAS_EXECUCOES`. Só ele: consultas, eventos e sessões
    de dados nunca são apagados. Devolve quantas linhas saíram."""
    agora = agora or timezone.now()
    apagadas, _ = M2MExecucao.objects.filter(iniciada_em__lt=agora - RETENCAO_DAS_EXECUCOES).delete()
    return apagadas


def executar_ciclo(
    origem: str = "agendada", *, cliente=None, tique_minutos: int | None = None, ciclo_minutos: int | None = None,
    todos: bool = False, usuario=None,
) -> ResumoCiclo:
    """Um ciclo de monitoramento — o que `monitorar_m2m` roda a cada tique do Agendador.

    1. PULA (sem consultar) se não há token/URL válidos, se a credencial foi
       recusada e nada a reabilitou (`falha_autenticacao_em` > `autenticada_em`),
       se a API pediu pausa (`bloqueado_ate`) ou se outra execução está em andamento
       (trava `executando_desde` por update condicional atômico; expira em 3 tiques da
       tarefa, no mínimo 3 e no máximo 30 min — `trava_expira`).
    2. Escolhe os chips CONSULTÁVEIS (monitorados e de operadora com integração TIM
       M2M) "vencidos" (sem `proxima_consulta_em` ou já passada): primeiro os que têm SESSÃO
       ATIVA (é onde o dado muda e o ritmo curto vale), depois os mais atrasados, até
       `max_chips_execucao`. Chip de operadora sem integração nunca entra.
    3. Consulta UM por vez (sessão e consumo), com a pausa entre chamadas, e PARA no
       primeiro 401/403 ou 429 — ou quando o tempo do ciclo acaba (`orcamento_do_ciclo_s`:
       o tique menos uma margem). O que ficou vencido é o primeiro da fila no próximo ciclo.
       Cada chip leva o intervalo do SEU estado (`intervalo_do_chip`) e, se falhou, o backoff
       (`espera_apos_falha`). Cada execução fica registrada em `M2MExecucao`.
    4. Avalia as ocorrências de TODOS os consultáveis (é aqui que "desatualizado"
       aparece) e as de VEÍCULO (`veiculo_sem_chip`, `veiculo_sem_atualizacao`), manda
       os e-mails das novas (um por conjunto de destinatários das regras) e grava a saúde
       (`ultima_execucao`, `ultimo_sucesso`, `ultimo_erro`, `proxima_execucao`).

    `tique_minutos`: intervalo da tarefa do Agendador, só para a tela saber se ela
    parou; se omitido, o valor gravado não muda. `ciclo_minutos`: de quanto em quanto tempo este
    ciclo se repete DE VERDADE (`executar_varios_ciclos` roda um por minuto dentro de um tique
    maior): é ele, e não o tique, que dá o tempo do ciclo e o prazo da trava. Devolve `ResumoCiclo`.

    `todos=True` é o "Atualizar todos" (`atualizar_todos`): no lugar dos vencidos entram TODOS os chips consultáveis, sem o
    corte `max_chips_execucao`, e as consultas ficam gravadas como `manual` e com o `usuario` que pediu — não como
    agendadas e sem autor. O resto é o mesmo ciclo: a mesma trava, o mesmo limite de requisições por minuto, a lista
    completa lida UMA vez para todos e depois um chip por vez. O que não couber no tempo do ciclo fica `pendente`."""
    agora = timezone.now()
    cfg = M2MIntegracao.get()
    resumo = ResumoCiclo()
    extra_tique = {"tique_minutos": tique_minutos} if tique_minutos else {}

    motivo = tim.motivo_nao_configurada()
    if motivo:
        resumo.motivo_pulo, resumo.mensagem = "sem_configuracao", motivo
        _registrar_pulo({"ultima_execucao": agora, "ultimo_erro": motivo, **extra_tique})
        return resumo
    if cfg.falha_autenticacao_em and (not cfg.autenticada_em or cfg.falha_autenticacao_em > cfg.autenticada_em):
        resumo.motivo_pulo = "credencial_recusada"
        resumo.mensagem = (
            f"a API recusou a credencial em {_data_hora(cfg.falha_autenticacao_em)} e ela não foi trocada nem "
            "testada com sucesso desde então. Corrija o token (ou faça um teste de conexão) para retomar."
        )
        _registrar_pulo({"ultima_execucao": agora, "ultimo_erro": "Credencial recusada pela API.", **extra_tique})
        return resumo
    if cfg.bloqueado_ate and cfg.bloqueado_ate > agora:
        resumo.motivo_pulo = "bloqueada"
        resumo.mensagem = f"a API limitou as requisições; as consultas ficam pausadas até {_hhmm(cfg.bloqueado_ate)}."
        _registrar_pulo({"ultima_execucao": agora, **extra_tique})
        return resumo

    tique_efetivo = ciclo_minutos or tique_minutos or cfg.tique_minutos
    expira = trava_expira(tique_efetivo)
    if todos or lote_em_curso(agora) is not None:
        expira = TRAVA_EXPIRA            # o lote dura mais que o ciclo de 1 min: ninguém toma a trava dele pelo prazo curto
    travou = M2MIntegracao.objects.filter(pk=1).filter(
        Q(executando_desde__isnull=True) | Q(executando_desde__lt=agora - expira)
    ).update(executando_desde=agora)
    if not travou:
        resumo.motivo_pulo, resumo.mensagem = "em_andamento", "outra execução do monitoramento ainda está em andamento."
        return resumo

    novas: list[M2MOcorrencia] = []
    execucao = None
    inicio_monotonico = time.monotonic()
    try:
        cfg = M2MIntegracao.get()
        cliente = cliente or tim.ClienteTIM()
        pausa = pausa_efetiva_s(cfg)
        resumo.executou = True
        orcamento_s = orcamento_do_ciclo_s(tique_efetivo)

        quem_esta_vencido = Q_CONSULTAVEL & (Q(proxima_consulta_em__isnull=True) | Q(proxima_consulta_em__lte=timezone.now()))
        # `vencidos` é, no lote, o TOTAL a atualizar: é o "N" do "X de N" da tela.
        alvo = Q_CONSULTAVEL if todos else quem_esta_vencido
        origem_da_consulta = ORIGEM_DO_LOTE if todos else "agendada"
        autor = usuario if todos else None
        resumo.vencidos = M2MChip.objects.filter(alvo).count()
        execucao = M2MExecucao.objects.create(origem=origem, iniciada_em=agora, vencidos=resumo.vencidos)
        resumo.execucao_id = execucao.pk
        vencidos = (
            M2MChip.objects.filter(alvo).select_related("operadora")
            .order_by(
                Case(When(sessao_estado=M2MChip.SessaoEstado.ATIVA, then=0), default=1, output_field=IntegerField()),
                F("proxima_consulta_em").asc(nulls_first=True), "pk",
            )
        )
        a_consultar = list(vencidos if todos else vencidos[: max(1, cfg.max_chips_execucao)])
        antes = {c.pk: (c.sessao_estado, c.consumo_bytes) for c in a_consultar}
        sessoes_novas = sessoes_atualizadas = 0
        # Com vários chips vencidos, UMA leitura da lista completa de SIM Cards da conta
        # atualiza todos (a API devolve a conta inteira num GET): a doc fala em limite de
        # requisições, e uma chamada por chip, para centenas de chips, não cabe nele. Se a
        # leitura falhar, cada chip herda a falha e a registra — não há chamada nem pausa.
        usa_lote = False
        carregar = getattr(cliente, "carregar_lote", None)
        if carregar is not None and len(a_consultar) >= LIMIAR_LOTE:
            try:
                carregar(origem_da_consulta)
                usa_lote = True
            except tim.TimM2MErro as exc:
                resumo.erro = tim.sanear(str(exc))
                a_consultar = []
        pausa_chip = 0.0 if usa_lote else pausa
        primeiro = True
        for chip in a_consultar:
            if not primeiro and time.monotonic() - inicio_monotonico >= orcamento_s:
                break           # o tempo do ciclo acabou: quem sobrou é o primeiro da fila no próximo
            if not primeiro and pausa_chip > 0:
                time.sleep(pausa_chip)
            primeiro = False
            try:
                r = atualizar(
                    chip, TIPOS_DE_CONSULTA, origem_da_consulta, autor,
                    cliente=cliente, pausa_s=pausa_chip, avaliar=False, cfg=cfg, conferir=False,
                )
            except AtualizacaoRecusada as exc:
                resumo.erro = tim.sanear(" ".join(exc.messages))
                break
            resumo.consultados += 1
            sessoes_novas += r.sessoes_novas
            sessoes_atualizadas += r.sessoes_atualizadas
            if r.houve_falha:
                resumo.falhas += 1
            else:
                resumo.sucessos += 1
            if todos and resumo.consultados % PASSO_DO_PROGRESSO_DO_LOTE == 0:
                # O "X de N" da tela: o ciclo comum só grava os números ao terminar, mas o lote dura minutos e quem
                # clicou fica olhando. Um UPDATE a cada poucos chips, na própria linha da execução.
                M2MExecucao.objects.filter(pk=execucao.pk).update(
                    consultados=resumo.consultados, sucessos=resumo.sucessos, falhas=resumo.falhas,
                )
            if r.interrompida_por:
                resumo.interrompido_por = r.interrompida_por
                break

        if resumo.interrompido_por == "falha_autenticacao":
            resumo.erro = "A API recusou a credencial. O monitoramento fica parado até o token ser corrigido."
        elif resumo.interrompido_por == "limite_excedido":
            bloqueio = M2MIntegracao.get().bloqueado_ate
            resumo.erro = "A API limitou as requisições (HTTP 429)." + (
                f" As consultas ficam pausadas até {_hhmm(bloqueio)}." if bloqueio else ""
            )
        elif resumo.consultados and not resumo.sucessos:
            resumo.erro = f"Todas as {resumo.consultados} consultas falharam; confira rede, credencial e a API."

        resumo.pendentes = max(0, resumo.vencidos - resumo.consultados)
        resumo.chamadas = getattr(cliente, "chamadas", 0)
        depois = {pk: (estado, consumo) for pk, estado, consumo in M2MChip.objects.filter(pk__in=antes).values_list(
            "pk", "sessao_estado", "consumo_bytes",
        )}
        resumo.mudancas = _mudancas_do_ciclo(antes, depois, sessoes_novas, sessoes_atualizadas)
        agora_fim = timezone.now()
        regras = regras_de_alerta()
        for chip in (
            M2MChip.objects.filter(Q_CONSULTAVEL)
            .select_related("operadora", "sessao_consulta", "consumo_consulta")
            .defer("sessao_consulta__payload", "consumo_consulta__payload")
        ):
            novas.extend(avaliar_ocorrencias(chip, agora=agora_fim, cfg=cfg, notificar=False, regras=regras))
        novas.extend(avaliar_ocorrencias_de_veiculos(agora=agora_fim, cfg=cfg, notificar=False, regras=regras))
        resumo.novas_ocorrencias = len(novas)

        saude = {"ultima_execucao": agora_fim, "ultimo_erro": resumo.erro, "proxima_execucao": _proxima_prevista(agora_fim), **extra_tique}
        if not resumo.erro:
            saude["ultimo_sucesso"] = agora_fim
        M2MIntegracao.objects.filter(pk=1).update(**saude)
    except Exception as exc:  # o Agendador precisa ver um erro, não um traceback solto
        _logar_erro_inesperado(exc)
        resumo.erro = f"Erro inesperado no ciclo ({exc.__class__.__name__}). Veja logs/erros.log."
        M2MIntegracao.objects.filter(pk=1).update(ultima_execucao=timezone.now(), ultimo_erro=resumo.erro)
    finally:
        M2MIntegracao.objects.filter(pk=1).update(executando_desde=None)
        _fechar_execucao(execucao, resumo, inicio_monotonico)

    notificar_ocorrencias(novas)  # fora da trava: SMTP lento não segura a próxima execução
    return resumo


def conferir_atualizar_todos(agora=None) -> int:
    """Pode começar um "Atualizar todos" AGORA? Devolve quantos chips ele vai atualizar, ou levanta `AtualizacaoRecusada`
    com a razão (`configuracao` | `credencial_recusada` | `bloqueada` | `em_andamento` | `sem_chips`). É a conferência
    que a TELA faz antes de abrir o processo do lote, para responder com a razão em vez de iniciar uma execução que o
    próprio ciclo pularia. O ciclo confere de novo (a trava é dele): esta leitura nunca autoriza nada."""
    agora = agora or timezone.now()
    cfg = M2MIntegracao.get()
    motivo = tim.motivo_nao_configurada()
    if motivo:
        raise AtualizacaoRecusada(motivo, "configuracao")
    if cfg.falha_autenticacao_em and (not cfg.autenticada_em or cfg.falha_autenticacao_em > cfg.autenticada_em):
        raise AtualizacaoRecusada(
            "A API recusou a credencial e ela não foi trocada nem testada com sucesso desde então. Corrija o token.",
            "credencial_recusada",
        )
    if cfg.bloqueado_ate and cfg.bloqueado_ate > agora:
        raise AtualizacaoRecusada(
            f"A API limitou as requisições; as consultas ficam pausadas até {_hhmm(cfg.bloqueado_ate)}.",
            "bloqueada", aguardar_s=int((cfg.bloqueado_ate - agora).total_seconds()),
        )
    if estado_integracao(agora)["executando"]:
        raise AtualizacaoRecusada(
            "Já há uma atualização dos chips em andamento. Acompanhe pela faixa de sincronização.", "em_andamento",
        )
    total = M2MChip.objects.filter(Q_CONSULTAVEL).count()
    if not total:
        raise AtualizacaoRecusada("Nenhum chip está sendo consultado pela API.", "sem_chips")
    return total


def atualizar_todos(usuario=None, *, cliente=None) -> ResumoCiclo:
    """O "Atualizar todos" da tela: UMA execução que atualiza todos os chips consultáveis, no limite de requisições da API.

    "Todos ao mesmo tempo" não existe nesse limite (30 requisições por minuto): 205 chips levam uns 8 minutos, mais na
    primeira passada, que baixa 3 dias de sessões por chip. O que a execução faz de uma vez só é a leitura da lista
    completa da conta (sessão e consumo de TODOS os chips, numa chamada); em seguida lê as sessões de dados de cada chip,
    no ritmo permitido. Quem estourar o tempo do ciclo fica `pendente` e entra na fila normal do monitoramento.

    É o que o comando `monitorar_m2m --todos` roda — a tela só o dispara em outro processo e acompanha o progresso lendo
    a `M2MExecucao` (`estado_sincronizacao["lote"]`). Pula, como todo ciclo, quando outra execução está em andamento."""
    return executar_ciclo(
        ORIGEM_DO_LOTE, cliente=cliente, ciclo_minutos=CICLO_DO_LOTE_MINUTOS, todos=True, usuario=usuario,
    )


def cadencia_dos_ciclos(cfg: M2MIntegracao, tique_minutos: int | None = None) -> int:
    """De quanto em quanto tempo um ciclo roda, em minutos: o intervalo DESEJADO para quem tem sessão ativa
    (`intervalo_minutos`), mas nunca mais espaçado que o tique da tarefa — a tarefa é quem garante que o ciclo
    acontece; o intervalo, quem pede que aconteça mais vezes dentro do tique."""
    tique = max(1, int(tique_minutos or cfg.tique_minutos or TIQUE_PADRAO_MINUTOS))
    return max(1, min(int(cfg.intervalo_minutos), tique))


def executar_varios_ciclos(
    origem: str = "agendada", *, tique_minutos: int | None = None, cliente=None,
    relogio=None, dormir=None,
) -> list[ResumoCiclo]:
    """O que o comando `monitorar_m2m` roda: um ciclo — ou VÁRIOS, um por minuto, até o fim do tique.

    O Agendador do Windows dispara a tarefa de `tique_minutos` em `tique_minutos`. Se o intervalo desejado
    (`intervalo_minutos`, 1 min) é menor que o tique, a execução NÃO termina no primeiro ciclo: repete um ciclo a
    cada `cadencia_dos_ciclos` minutos até uns segundos antes do próximo disparo da tarefa, e o Agendador só
    precisa estar de pé. É assim que a sincronização de 1 em 1 minuto funciona com a tarefa já registrada de 15
    em 15, e com a de 1 em 1 (aí é um ciclo só, sem laço). O ciclo seguinte espera o MINUTO inteiro desde o
    início do anterior (nunca mais requisições por minuto que o limite).

    Para na hora quando o ciclo diz que insistir não adianta (sem configuração, credencial recusada, API
    limitando) — o próximo disparo da tarefa tenta de novo. `relogio` e `dormir` existem para o teste."""
    relogio = relogio or time.monotonic            # resolvidos AQUI (e não como valor padrão): o teste troca `time.sleep`
    dormir = dormir or time.sleep
    cfg = M2MIntegracao.get()
    tique = max(1, int(tique_minutos or cfg.tique_minutos or TIQUE_PADRAO_MINUTOS))
    cadencia = cadencia_dos_ciclos(cfg, tique)
    if cadencia >= tique:
        return [executar_ciclo(origem, cliente=cliente, tique_minutos=tique_minutos)]
    inicio_da_execucao = relogio()
    fim = inicio_da_execucao + tique * 60 - 2 * MARGEM_DO_TIQUE_S      # termina antes do próximo disparo
    resumos: list[ResumoCiclo] = []
    maximo = tique // cadencia + 1             # trava: nunca mais ciclos que o tique comporta, qualquer que seja o relógio
    while len(resumos) < maximo:
        comeco = relogio()
        resumo = executar_ciclo(origem, cliente=cliente, tique_minutos=tique_minutos, ciclo_minutos=cadencia)
        resumos.append(resumo)
        if resumo.exige_atencao or resumo.motivo_pulo in ("sem_configuracao", "credencial_recusada", "bloqueada"):
            break
        proximo = comeco + cadencia * 60
        if proximo + orcamento_do_ciclo_s(cadencia) > fim:             # não cabe mais um ciclo INTEIRO antes do próximo disparo
            break
        dormir(max(1.0, proximo - relogio()))
    return resumos


# ═════════════════════════════════════════════════════════════
# Configuração (telas de Integração)
# ═════════════════════════════════════════════════════════════

# campo -> (mínimo, máximo, rótulo)
LIMITES_CONFIG = {
    "intervalo_minutos": (INTERVALO_MINIMO_MINUTOS, 7 * 24 * 60, "Intervalo entre consultas (chip com sessão ativa)"),
    "intervalo_repouso_minutos": (INTERVALO_MINIMO_MINUTOS, 7 * 24 * 60, "Intervalo entre consultas (chip sem sessão)"),
    "pausa_entre_chamadas_ms": (0, 60_000, "Pausa entre chamadas"),
    "limite_req_minuto": (1, 600, "Limite de requisições por minuto"),
    "max_chips_execucao": (1, 1000, "Máximo de chips por execução"),
    "falhas_para_alerta": (1, 100, "Falhas seguidas para alertar"),
    "horas_sem_sessao_alerta": (1, 24 * 30, "Horas sem sessão para alertar"),
    "horas_desatualizado": (1, 24 * 30, "Horas sem dado novo para alertar"),
    "pct_consumo_alerta": (1, 100, "Percentual do limite para alertar consumo"),
}


def contar_monitorados() -> int:
    """Chips que a API consulta (monitorados E de operadora com integração) — é o que
    entra na conta de requisições por minuto."""
    return M2MChip.objects.filter(Q_CONSULTAVEL).count()


def capacidade_por_minuto(limite_req_minuto: int) -> float:
    """Quantos chips o ciclo consegue atualizar por minuto sem passar do limite de requisições: o limite,
    menos 10% que ficam para o "Atualizar" da tela, menos a leitura da lista completa que cada ciclo faz.
    Em regime cada chip custa UMA requisição (as sessões de dados de hoje); o resumo do SIM Card sai da
    lista, que é uma chamada só para a conta inteira."""
    return max(0.0, int(limite_req_minuto) * RESERVA_PARA_O_MANUAL - 1)


def _arredondar_min(valor) -> float | None:
    return None if valor is None else round(float(valor), 1)


def plano_de_frequencia(
    intervalo_ativo, intervalo_repouso, n_ativos: int, n_repouso: int, limite_req_minuto: int,
) -> dict:
    """A conta que a tela mostra, em dois ritmos: chips com sessão ativa (`intervalo_ativo`) e sem sessão
    (`intervalo_repouso`). O intervalo configurado é o DESEJADO; se a carga passa do que o limite de
    requisições comporta (`capacidade_por_minuto`), o ciclo atende primeiro quem tem sessão ativa e o resto
    espera — e é essa a frequência EFETIVA que o plano informa. Nunca recusa: o limite é cumprido pelo ciclo.

        {"limitado": bool, "carga": float, "capacidade": float,         # consultas por minuto
         "efetivo_ativo_min": float|None, "efetivo_repouso_min": float|None,   # None = não cabe
         "texto": str}"""
    ia, ir = max(1, int(intervalo_ativo)), max(1, int(intervalo_repouso))
    n_ativos, n_repouso = max(0, int(n_ativos)), max(0, int(n_repouso))
    capacidade = capacidade_por_minuto(limite_req_minuto)
    carga_a, carga_r = n_ativos / ia, n_repouso / ir
    carga = carga_a + carga_r
    limitado = carga > capacidade
    if not limitado:
        efetivo_a, efetivo_r = float(ia), float(ir)
    elif carga_a >= capacidade:
        efetivo_a = (n_ativos / capacidade) if capacidade > 0 else None
        efetivo_r = None
    else:
        efetivo_a = float(ia)
        sobra = capacidade - carga_a
        efetivo_r = (n_repouso / sobra) if sobra > 0 else None

    def virgula(n):
        return str(round(n, 1)).replace(".", ",")

    texto = (
        f"{n_ativos} chip(s) com sessão ativa a cada {ia} min + {n_repouso} sem sessão a cada {ir} min = "
        f"{virgula(carga)} consultas/min; com o limite de {limite_req_minuto} req/min cabem {virgula(capacidade)}."
    )
    if limitado:
        partes = []
        if efetivo_a is not None:
            partes.append(f"chips com sessão ativa a cada {virgula(efetivo_a)} min")
        if efetivo_r is not None:
            partes.append(f"chips sem sessão a cada {virgula(efetivo_r)} min")
        texto += " Na prática, o ciclo atualiza " + (" e ".join(partes) if partes else "só parte dos chips") + "."
    return {
        "limitado": limitado, "carga": round(carga, 2), "capacidade": round(capacidade, 2),
        "efetivo_ativo_min": _arredondar_min(efetivo_a), "efetivo_repouso_min": _arredondar_min(efetivo_r),
        "chips_ativos": n_ativos, "chips_repouso": n_repouso, "texto": texto,
    }


def plano_atual(cfg: M2MIntegracao | None = None, *, intervalo_ativo=None, intervalo_repouso=None, limite=None) -> dict:
    """`plano_de_frequencia` com os chips de agora: ATIVA e o resto dos consultáveis. Qualquer parâmetro
    omitido vem da configuração gravada (a tela passa os que o usuário está digitando)."""
    cfg = cfg or M2MIntegracao.get()
    dados = M2MChip.objects.filter(Q_CONSULTAVEL).aggregate(
        total=Count("pk"), ativos=Count("pk", filter=Q(sessao_estado=M2MChip.SessaoEstado.ATIVA)),
    )
    return plano_de_frequencia(
        intervalo_ativo if intervalo_ativo is not None else cfg.intervalo_minutos,
        intervalo_repouso if intervalo_repouso is not None else cfg.intervalo_repouso_minutos,
        dados["ativos"], dados["total"] - dados["ativos"],
        limite if limite is not None else cfg.limite_req_minuto,
    )


def validar_frequencia(intervalo_minutos, n_chips: int | None = None, limite_req_minuto: int | None = None) -> dict:
    """A conta de UM ritmo só (todos os chips no mesmo intervalo) — o que o campo "intervalo" mostra ao vivo.
    Sem `n_chips` usa os chips consultáveis; sem `limite_req_minuto`, o configurado. NÃO recusa um intervalo
    que não cabe no limite: o ciclo cumpre o limite sozinho e a conta diz qual é o intervalo efetivo. Só
    recusa o que não faz sentido (abaixo de `INTERVALO_MINIMO_MINUTOS`). Devolve:

        {"ok": bool, "motivo": str,           # motivo só quando ok=False
         "limitado": bool,                    # o desejado não cabe no limite
         "req_por_minuto": float, "limite": int, "chips": int, "intervalo": int,
         "efetivo_minutos": float,            # intervalo que o ciclo consegue de fato
         "intervalo_minimo_sugerido": int,    # o menor intervalo que cabe no limite sem alongar
         "conta": "205 chips a cada 1 min = 205 consultas/min; com o limite de 30 req/min cabem 26"}"""
    intervalo = int(intervalo_minutos)
    n = contar_monitorados() if n_chips is None else int(n_chips)
    limite = int(limite_req_minuto if limite_req_minuto is not None else M2MIntegracao.get().limite_req_minuto)
    capacidade = capacidade_por_minuto(limite)
    req = (n / intervalo) if intervalo > 0 else float("inf")
    sugerido = max(INTERVALO_MINIMO_MINUTOS, math.ceil(n / capacidade)) if capacidade > 0 else INTERVALO_MINIMO_MINUTOS
    limitado = math.isfinite(req) and req > capacidade
    efetivo = (n / capacidade) if (limitado and capacidade > 0) else float(max(intervalo, 1))
    motivo = f"O intervalo mínimo é de {INTERVALO_MINIMO_MINUTOS} minuto(s)." if intervalo < INTERVALO_MINIMO_MINUTOS else ""

    def virgula(n_):
        return str(round(n_, 1)).replace(".", ",")

    conta = f"{n} chips a cada {intervalo} min = {virgula(req) if math.isfinite(req) else 'infinito'} consultas/min; com o limite de {limite} req/min cabem {virgula(capacidade)}."
    if limitado:
        conta += f" Na prática, cada chip é consultado a cada {virgula(efetivo)} min."
    return {
        "ok": not motivo, "motivo": motivo, "limitado": limitado,
        "req_por_minuto": req, "limite": limite, "chips": n, "intervalo": intervalo,
        "efetivo_minutos": round(efetivo, 1), "intervalo_minimo_sugerido": sugerido, "conta": conta,
    }


def salvar_configuracao(dados: dict, usuario=None) -> M2MIntegracao:
    """Grava frequência e limites de alerta. `dados`: qualquer subconjunto de
    `intervalo_minutos`, `pausa_entre_chamadas_ms`, `limite_req_minuto`,
    `max_chips_execucao`, `falhas_para_alerta`, `horas_sem_sessao_alerta`,
    `horas_desatualizado`, `pct_consumo_alerta`, `intervalo_repouso_minutos` (inteiros, dentro
    de `LIMITES_CONFIG`). Recusa (`ValidationError({campo: mensagem})`) valor fora dos limites;
    frequência que não cabe no limite de requisições NÃO é recusada (o ciclo a alonga e a tela
    informa o efetivo — `plano_atual`). Grava `M2MEvento(config_alterada)` com o de/para."""
    cfg = M2MIntegracao.get()
    erros: dict[str, str] = {}
    novos: dict[str, int] = {}
    for campo, (minimo, maximo, rotulo) in LIMITES_CONFIG.items():
        if campo not in dados:
            continue
        try:
            valor = int(str(dados[campo]).strip())
        except (TypeError, ValueError):
            erros[campo] = f"{rotulo}: informe um número inteiro."
            continue
        if not minimo <= valor <= maximo:
            erros[campo] = f"{rotulo}: informe um valor entre {minimo} e {maximo}."
            continue
        novos[campo] = valor
    if erros:
        raise ValidationError(erros)

    # O intervalo desejado nunca é recusado por "não caber": o ciclo cumpre o limite de requisições sozinho e
    # a tela mostra o intervalo efetivo (`plano_atual`). Recusar aqui obrigava a configurar 15 min para 205 chips.

    alteracoes = {c: {"de": getattr(cfg, c), "para": v} for c, v in novos.items() if getattr(cfg, c) != v}
    if not alteracoes:
        return cfg
    with transaction.atomic():
        M2MIntegracao.objects.filter(pk=1).update(**{c: v["para"] for c, v in alteracoes.items()})
        M2MEvento.objects.create(
            tipo=M2MEvento.Tipo.CONFIG_ALTERADA, usuario=_usuario_ou_none(usuario), dados={"alteracoes": alteracoes},
        )
    cfg.refresh_from_db()
    return cfg


def salvar_mapeamento(mapeamento, usuario=None) -> dict:
    """Valida (`m2m_interpretacao.validar_mapeamento`) e grava o mapeamento da
    resposta. NÃO reescreve consultas antigas — para isso existe
    `reinterpretar_historico`. Grava `M2MEvento(mapeamento_alterado)` com o de/para.
    Devolve o mapeamento normalizado. `ValidationError` com todas as mensagens."""
    novo = interp.validar_mapeamento(mapeamento)
    cfg = M2MIntegracao.get()
    if novo == (cfg.mapeamento or {}):
        return novo
    with transaction.atomic():
        M2MIntegracao.objects.filter(pk=1).update(mapeamento=novo)
        M2MEvento.objects.create(
            tipo=M2MEvento.Tipo.MAPEAMENTO_ALTERADO, usuario=_usuario_ou_none(usuario),
            dados={"de": cfg.mapeamento or {}, "para": novo},
        )
    return novo


def reinterpretar_historico(usuario=None) -> dict:
    """"Reinterpretar histórico": recalcula os campos NORMALIZADOS de todas as
    consultas válidas com o mapeamento atual — e a situação atual dos chips, que
    deriva delas. O `payload` nunca é tocado. Devolve
    `{"consultas": lidas, "alteradas": regravadas, "chips": chips cujo estado/consumo mudou}`."""
    cfg = M2MIntegracao.get()
    mapa = cfg.mapeamento or {}
    lidas = alteradas = 0
    lote: list[M2MConsulta] = []
    with transaction.atomic():
        for consulta in M2MConsulta.objects.filter(resultado__in=M2MConsulta.RESULTADOS_VALIDOS).iterator(chunk_size=500):
            lidas += 1
            novos = interp.campos_normalizados(consulta.tipo, consulta.payload, mapa, valida=True)
            if any(getattr(consulta, campo) != valor for campo, valor in novos.items()):
                for campo, valor in novos.items():
                    setattr(consulta, campo, valor)
                lote.append(consulta)
                if len(lote) >= 500:
                    M2MConsulta.objects.bulk_update(lote, M2MConsulta.CAMPOS_NORMALIZADOS)
                    alteradas += len(lote)
                    lote = []
        if lote:
            M2MConsulta.objects.bulk_update(lote, M2MConsulta.CAMPOS_NORMALIZADOS)
            alteradas += len(lote)

        chips_mudados = 0
        for chip in M2MChip.objects.filter(Q(sessao_consulta__isnull=False) | Q(consumo_consulta__isnull=False)).select_related(
            "sessao_consulta", "consumo_consulta",
        ).defer("sessao_consulta__payload", "consumo_consulta__payload"):
            campos = []
            if chip.sessao_consulta_id and chip.sessao_consulta.valida:
                novo = chip.sessao_consulta.estado_interpretado or M2MChip.SessaoEstado.NAO_INTERPRETADO
                if novo != chip.sessao_estado:
                    M2MEvento.objects.create(
                        chip=chip, tipo=M2MEvento.Tipo.ESTADO_SESSAO_MUDOU, usuario=_usuario_ou_none(usuario),
                        dados={"de": chip.sessao_estado, "para": novo, "motivo": "reinterpretacao_do_historico"},
                    )
                    chip.sessao_estado = novo
                    campos.append("sessao_estado")
            if chip.consumo_consulta_id and chip.consumo_consulta.valida:
                if chip.consumo_consulta.volume_bytes != chip.consumo_bytes:
                    chip.consumo_bytes = chip.consumo_consulta.volume_bytes
                    campos.append("consumo_bytes")
            if campos:
                chip.save(update_fields=campos)
                chips_mudados += 1
        M2MEvento.objects.create(
            tipo=M2MEvento.Tipo.MAPEAMENTO_ALTERADO, usuario=_usuario_ou_none(usuario),
            dados={"acao": "reinterpretar_historico", "consultas": lidas, "alteradas": alteradas, "chips": chips_mudados},
        )
    return {"consultas": lidas, "alteradas": alteradas, "chips": chips_mudados}


# ═════════════════════════════════════════════════════════════
# Leituras para as telas
# ═════════════════════════════════════════════════════════════

def contar_alertas_abertos() -> int:
    """Ocorrências abertas ou em tratamento (o contador da sub-navegação)."""
    return M2MOcorrencia.objects.filter(estado__in=ABERTAS).count()


def indicadores(agora=None) -> dict:
    """KPIs da Visão geral, calculados do banco com UMA consulta agregada sobre os
    chips (mais a configuração e a contagem de alertas — sem N+1). As contagens de
    sessão/consumo/falha valem só para chips MONITORADOS. Chaves:

        total, monitorados, desativados, sem_integracao,
        sessao_ativa, sem_sessao, nao_interpretado, nao_consultado,   # soma = monitorados
        com_consumo,        # monitorados com consumo normalizado
        falha_consulta,     # monitorados cuja última tentativa falhou
        desatualizados,     # monitorados com último dado válido além do limite
        precisam_atencao,   # monitorados com ocorrência aberta/em tratamento
        alertas_abertos     # ocorrências abertas/em tratamento (total)

    "Monitorados" são os CONSULTÁVEIS (monitorado e de operadora com integração);
    `sem_integracao` conta os monitorados de operadora sem API (não entram nas demais).
    "Sessão ativa" só conta o que o mapeamento confirmou; sem mapeamento todo chip
    consultado cai em `nao_interpretado`."""
    agora = agora or timezone.now()
    cfg = M2MIntegracao.get()
    limite = agora - timedelta(hours=cfg.horas_desatualizado)
    monitorado = Q_CONSULTAVEL
    abertas = M2MOcorrencia.objects.filter(chip=OuterRef("pk"), estado__in=ABERTAS)
    dados = M2MChip.objects.aggregate(
        total=Count("pk"),
        monitorados=Count("pk", filter=monitorado),
        desativados=Count("pk", filter=Q(monitorado=False)),
        sem_integracao=Count("pk", filter=Q(monitorado=True) & ~Q(operadora__integracao=INTEGRACAO_TIM)),
        sessao_ativa=Count("pk", filter=monitorado & Q(sessao_estado="ativa")),
        sem_sessao=Count("pk", filter=monitorado & Q(sessao_estado="sem_sessao")),
        nao_interpretado=Count("pk", filter=monitorado & Q(sessao_estado="nao_interpretado")),
        nao_consultado=Count("pk", filter=monitorado & Q(sessao_estado="nao_consultado")),
        com_consumo=Count("pk", filter=monitorado & Q(consumo_bytes__isnull=False)),
        falha_consulta=Count("pk", filter=monitorado & Q(falhas_consecutivas__gt=0)),
        desatualizados=Count("pk", filter=monitorado & (
            Q(ultima_valida_em__lt=limite)
            | Q(ultima_valida_em__isnull=True, ultima_tentativa_em__isnull=False, created_at__lt=limite)
        )),
        precisam_atencao=Count("pk", filter=monitorado & Exists(abertas)),
    )
    dados["alertas_abertos"] = contar_alertas_abertos()
    return dados


def frescor(momento, horas_limite: int | None = None, agora=None) -> dict:
    """O rótulo de frescor que acompanha todo dado vindo da API:
    `{"rotulo": "há 3 h", "desatualizado": bool, "momento": datetime|None}`.
    `horas_limite` omitido = o configurado. Sem momento: "nunca consultado"."""
    if momento is None:
        return {"rotulo": "nunca consultado", "desatualizado": False, "momento": None}
    agora = agora or timezone.now()
    horas_limite = horas_limite if horas_limite is not None else M2MIntegracao.get().horas_desatualizado
    minutos = max(0, int((agora - momento).total_seconds() // 60))
    if minutos < 1:
        rotulo = "agora há pouco"
    elif minutos < 60:
        rotulo = f"há {minutos} min"
    elif minutos < 48 * 60:
        rotulo = f"há {minutos // 60} h"
    else:
        rotulo = f"há {minutos // 1440} dias"
    return {"rotulo": rotulo, "desatualizado": (agora - momento) > timedelta(hours=horas_limite), "momento": momento}


ROTULO_ESTADO_INTEGRACAO = {
    "url_invalida": "URL da API inválida",
    "nao_configurada": "Não configurada",
    "token_ilegivel": "Token salvo ilegível",
    "bloqueada": "Bloqueada por limite",
    "credencial_recusada": "Credencial recusada",
    "autenticada": "Autenticada",
    "nao_testada": "Configurada, não testada",
}


def estado_integracao(agora=None) -> dict:
    """O que a faixa de saúde da integração mostra. Chaves:

        estado   : url_invalida | nao_configurada | token_ilegivel | bloqueada |
                   credencial_recusada | autenticada | nao_testada
        rotulo   : texto pronto ("Autenticada às 14:05", "Bloqueada por limite até 14:40", ...)
        ultima_sincronizacao : datetime|None  (última execução do ciclo, mesmo que tenha pulado)
        proxima  : datetime|None  (quando o próximo chip fica vencido; `agora` se já há vencido)
        tarefa_parada : bool  (última execução há mais de 2 tiques; False se nunca rodou)
        token_origem : "env" | "banco" | "nenhum"
        credencial_modo : "oauth2" (usuario e senha da API, no .env) | "token" | "nenhum"
        token_sufixo : últimos 4 caracteres do token ("" se curto ou ausente) — NUNCA o token
        + extras: autenticada_em, bloqueado_ate, falha_autenticacao_em, ultimo_erro,
          ultimo_teste (dict), tique_minutos, executando (bool), token_legivel,
          token_env_definido, token_no_banco, chave_cifra_configurada, base_url."""
    agora = agora or timezone.now()
    cfg = M2MIntegracao.get()
    token = tim.estado_token()
    try:
        base, url_ok = tim.base_url(), True
    except tim.ConfiguracaoInvalida:
        base, url_ok = "", False

    def quando(momento):
        local = timezone.localtime(momento)
        return local.strftime("%H:%M") if local.date() == timezone.localdate() else local.strftime("%d/%m %H:%M")

    if not url_ok:
        estado, rotulo = "url_invalida", ROTULO_ESTADO_INTEGRACAO["url_invalida"]
    elif token["origem"] == "nenhum":
        estado, rotulo = "nao_configurada", ROTULO_ESTADO_INTEGRACAO["nao_configurada"]
    elif not token["legivel"]:
        estado, rotulo = "token_ilegivel", ROTULO_ESTADO_INTEGRACAO["token_ilegivel"]
    elif cfg.bloqueado_ate and cfg.bloqueado_ate > agora:
        estado, rotulo = "bloqueada", f"Bloqueada por limite até {quando(cfg.bloqueado_ate)}"
    elif cfg.falha_autenticacao_em and (not cfg.autenticada_em or cfg.falha_autenticacao_em > cfg.autenticada_em):
        estado, rotulo = "credencial_recusada", ROTULO_ESTADO_INTEGRACAO["credencial_recusada"]
    elif cfg.autenticada_em:
        estado, rotulo = "autenticada", f"Autenticada às {quando(cfg.autenticada_em)}"
    else:
        estado, rotulo = "nao_testada", ROTULO_ESTADO_INTEGRACAO["nao_testada"]

    tarefa_parada = bool(
        cfg.ultima_execucao and agora - cfg.ultima_execucao > timedelta(minutes=2 * max(1, cfg.tique_minutos))
    )
    return {
        "estado": estado, "rotulo": rotulo,
        "ultima_sincronizacao": cfg.ultima_execucao, "proxima": _proxima_prevista(agora),
        "tarefa_parada": tarefa_parada,
        "token_origem": token["origem"], "token_sufixo": token["sufixo"],
        "credencial_modo": token["modo"],
        "autenticada_em": cfg.autenticada_em, "bloqueado_ate": cfg.bloqueado_ate,
        "falha_autenticacao_em": cfg.falha_autenticacao_em, "ultimo_erro": cfg.ultimo_erro,
        "ultimo_teste": cfg.ultimo_teste or {}, "tique_minutos": cfg.tique_minutos,
        "executando": bool(
            cfg.executando_desde
            and (agora - cfg.executando_desde < trava_expira(cfg.tique_minutos) or lote_em_curso(agora) is not None)
        ),
        "token_legivel": token["legivel"], "token_env_definido": token["token_env_definido"],
        "token_no_banco": token["token_no_banco"], "chave_cifra_configurada": token["chave_cifra_configurada"],
        "base_url": base,
    }


ROTULO_SINCRONIZACAO = {
    "atualizada": "Sincronização atualizada",
    "em_andamento": "Sincronização em andamento",
    "aguardando": "Aguardando próxima atualização",
    "falha": "Falha na última sincronização",
    "desatualizada": "Dados desatualizados",
    "indisponivel": "Integração indisponível",
}
TOM_SINCRONIZACAO = {
    "atualizada": "ok", "em_andamento": "info", "aguardando": "info",
    "falha": "erro", "desatualizada": "atencao", "indisponivel": "erro",
}


# A chave de `m2m_ind` (a forma e a cor do marcador) de cada estado da sincronização.
INDICADOR_SINCRONIZACAO = {
    "atualizada": "normal", "em_andamento": "info", "aguardando": "pendente",
    "falha": "erro_consulta", "desatualizada": "desatualizado", "indisponivel": "critico",
}


def execucoes_recentes(limite: int = 8) -> list[M2MExecucao]:
    """As últimas execuções do ciclo (mais nova primeiro), para o painel."""
    return list(M2MExecucao.objects.order_by("-iniciada_em", "-id")[: max(1, limite)])


def estado_sincronizacao(agora=None) -> dict:
    """A faixa "a sincronização automática está funcionando?" — vem do BANCO (o que a tarefa do Agendador
    gravou), nunca do navegador: quem abre a tela não dispara nada. Chaves:

        estado, rotulo, tom (ok|info|erro|atencao)
        motivo            : frase que explica o estado ("A tarefa do Agendador não rodou há 12 min.")
        ultima_sincronizacao : o último ciclo que terminou SEM erro (`ultimo_sucesso`)
        ultima_tentativa     : o último ciclo, com ou sem erro (`ultima_execucao`)
        ultima_resposta_valida : a resposta válida mais recente de qualquer chip — NÃO é a hora do ciclo:
                                 o ciclo pode rodar e a API não responder
        proxima              : próxima atualização prevista
        vencidos             : chips que já passaram do intervalo e esperam a fila
        execucao             : a última `M2MExecucao` (chips, consultas, falhas, requisições, mudanças) ou None
        lote                 : o "Atualizar todos" em curso, `{"em_andamento", "feitos", "total"}`, ou None
        plano                : intervalo desejado × efetivo (`plano_atual`)
        tique_minutos, tarefa_parada

    Precedência: integração indisponível > em andamento > tarefa parada ("desatualizada") > falha > fila
    esperando ("aguardando") > atualizada."""
    agora = agora or timezone.now()
    cfg = M2MIntegracao.get()
    integ = estado_integracao(agora)
    ultima = M2MExecucao.objects.order_by("-iniciada_em", "-id").first()
    tique = max(1, cfg.tique_minutos)
    vencidos = M2MChip.objects.filter(Q_CONSULTAVEL).filter(
        Q(proxima_consulta_em__isnull=True) | Q(proxima_consulta_em__lte=agora)
    ).count()
    atrasados = M2MChip.objects.filter(
        Q_CONSULTAVEL, proxima_consulta_em__lt=agora - timedelta(minutes=max(3, 2 * cadencia_dos_ciclos(cfg))),
    ).count()
    ultima_resposta = M2MChip.objects.filter(Q_CONSULTAVEL).aggregate(m=Max("ultima_valida_em"))["m"]
    # O "Atualizar todos" em curso (só vale com a trava de pé: uma linha `em_andamento` de execução que morreu não conta).
    execucao_do_lote = lote_em_curso(agora) if integ["executando"] else None
    lote = (
        {"em_andamento": True, "feitos": execucao_do_lote.consultados, "total": execucao_do_lote.vencidos}
        if execucao_do_lote is not None else None
    )

    # A cadência dos ciclos (1 min com o intervalo desejado de 1 min, mesmo com a tarefa de 15 em 15): a tela só
    # diz "parada" quando passa bem de dois ciclos sem execução. Piso de 3 min — o ciclo leva até 50 s.
    cadencia = cadencia_dos_ciclos(cfg)
    parada = bool(
        not cfg.ultima_execucao or agora - cfg.ultima_execucao > timedelta(minutes=max(3, 2 * cadencia + 1))
    )
    falhou = bool(cfg.ultimo_erro) and (not cfg.ultimo_sucesso or (cfg.ultima_execucao and cfg.ultima_execucao > cfg.ultimo_sucesso))

    if integ["estado"] in ("url_invalida", "nao_configurada", "token_ilegivel", "bloqueada", "credencial_recusada"):
        estado, motivo = "indisponivel", integ["rotulo"] + "."
    elif integ["executando"]:
        estado, motivo = "em_andamento", "Um ciclo está consultando os chips agora."
        if lote is not None:
            motivo = f"Atualizando todos os chips: {lote['feitos']} de {lote['total']}."
    elif parada:
        estado = "desatualizada"
        motivo = (
            "A tarefa do Agendador nunca rodou: nada atualiza os chips sozinho." if not cfg.ultima_execucao
            else f"A tarefa do Agendador não roda desde {timezone.localtime(cfg.ultima_execucao):%d/%m %H:%M} "
                 f"(o esperado é a cada {tique} min)."
        )
    elif falhou:
        estado, motivo = "falha", cfg.ultimo_erro
    elif atrasados:
        estado = "aguardando"
        motivo = f"{atrasados} chip(s) estão na fila, esperando o próximo ciclo (limite de requisições por minuto)."
    else:
        estado, motivo = "atualizada", "Os chips estão sendo atualizados dentro do intervalo configurado."

    return {
        "estado": estado, "rotulo": ROTULO_SINCRONIZACAO[estado], "tom": TOM_SINCRONIZACAO[estado], "motivo": motivo,
        "ind": INDICADOR_SINCRONIZACAO[estado],
        "ultima_sincronizacao": cfg.ultimo_sucesso, "ultima_tentativa": cfg.ultima_execucao,
        "ultima_resposta_valida": ultima_resposta, "proxima": _proxima_prevista(agora),
        "vencidos": vencidos, "execucao": ultima, "plano": plano_atual(cfg), "lote": lote,
        "tique_minutos": cfg.tique_minutos, "tarefa_parada": parada, "integracao": integ,
    }


def _classe_da_consulta(c: M2MConsulta) -> str:
    if c.resultado == "sucesso_sem_corpo":
        return "sem_corpo"
    if c.resultado == "sucesso":
        interpretado = (
            c.estado_interpretado in ("ativa", "sem_sessao") if c.tipo == "sessao" else c.volume_bytes is not None
        )
        return "sucesso" if interpretado else "nao_interpretado"
    return "falha"


def fita_consultas(chip: M2MChip, n: int = 24) -> list[dict]:
    """As últimas `n` consultas do chip, da MAIS ANTIGA para a mais nova (a ordem
    em que a fita se lê, da esquerda para a direita). Cada marca:

        {"id", "tipo": "sessao"|"consumo", "tipo_rotulo", "em": datetime,
         "classe": "sucesso" | "sem_corpo" | "falha" | "nao_interpretado",
         "resultado": slug, "resultado_rotulo", "http_status", "origem",
         "rotulo": "texto do tooltip"}

    `classe` é o que a marca desenha: `nao_interpretado` é resposta válida que o
    mapeamento não soube ler (ou não existe) — diferente de falha. Uma consulta
    (sem o payload), nenhuma por marca."""
    consultas = list(chip.consultas.defer("payload").order_by("-iniciada_em", "-pk")[: max(1, n)])
    marcas = []
    for c in reversed(consultas):
        partes = [c.get_tipo_display(), _data_hora(c.iniciada_em), c.get_resultado_display()]
        if c.http_status:
            partes.append(f"HTTP {c.http_status}")
        marcas.append({
            "id": c.pk, "tipo": c.tipo, "tipo_rotulo": c.get_tipo_display(), "em": c.iniciada_em,
            "classe": _classe_da_consulta(c), "resultado": c.resultado,
            "resultado_rotulo": c.get_resultado_display(), "http_status": c.http_status,
            "origem": c.origem, "rotulo": " · ".join(partes),
        })
    return marcas


# Os eventos do chip que contam o que a API disse ou o que foi feito com a consulta a ela: ficam fora do histórico de quem
# não vê API (a sessão que mudou de estado e o monitoramento ligado/desligado). O resto — cadastro, edição, vínculo — é
# cadastro e movimentação.
EVENTOS_DE_API = (
    M2MEvento.Tipo.ESTADO_SESSAO_MUDOU, M2MEvento.Tipo.DESATIVADO, M2MEvento.Tipo.REATIVADO,
)


def linha_do_tempo(chip: M2MChip, tipos=None, limite: int = 100, com_api: bool = True) -> list[dict]:
    """Histórico do chip, do MAIS NOVO para o mais antigo, misturando consultas,
    eventos e ocorrências. `tipos`: subconjunto de {"consulta", "evento",
    "ocorrencia"} (None = todos). `com_api=False` (quem não vê API: Portal do Colaborador) tira tudo que a operadora
    disse — as consultas, as ocorrências e os eventos `EVENTOS_DE_API` — e deixa o que é cadastro e movimentação. Cada item:

        {"quando": datetime, "fonte": "consulta"|"evento"|"ocorrencia", "tipo": slug,
         "titulo": str, "detalhe": str, "nivel": "ok"|"info"|"aviso"|"erro", "ref_id": int}"""
    fontes = set(tipos) if tipos else {"consulta", "evento", "ocorrencia"}
    if not com_api:
        fontes -= {"consulta", "ocorrencia"}
    itens: list[dict] = []
    if "consulta" in fontes:
        for c in chip.consultas.defer("payload").order_by("-iniciada_em", "-pk")[:limite]:
            if c.valida:
                if c.tipo == "sessao":
                    detalhe = f"Estado: {c.estado_bruto or '—'} ({c.estado_interpretado or 'não interpretado'})."
                else:
                    detalhe = (
                        f"Volume: {c.valor_original} {c.unidade_original}".strip() if c.valor_original
                        else "Resposta guardada; volume não interpretado."
                    )
                nivel = "ok" if _classe_da_consulta(c) == "sucesso" else "info"
            else:
                detalhe, nivel = c.mensagem_erro or c.get_resultado_display(), "erro"
            itens.append({
                "quando": c.iniciada_em, "fonte": "consulta", "tipo": c.tipo,
                "titulo": f"Consulta de {c.get_tipo_display().lower()} ({c.get_origem_display().lower()}): {c.get_resultado_display().lower()}",
                "detalhe": detalhe, "nivel": nivel, "ref_id": c.pk,
            })
    if "evento" in fontes:
        eventos = chip.eventos.all() if com_api else chip.eventos.exclude(tipo__in=EVENTOS_DE_API)
        for e in eventos.select_related("usuario").order_by("-em", "-pk")[:limite]:
            itens.append({
                "quando": e.em, "fonte": "evento", "tipo": e.tipo, "titulo": e.get_tipo_display(),
                "detalhe": _resumo_do_evento(e), "nivel": "info", "ref_id": e.pk,
            })
    if "ocorrencia" in fontes:
        for o in chip.ocorrencias.order_by("-detectada_em", "-pk")[:limite]:
            nivel = "erro" if o.nivel == "critico" else "aviso"
            itens.append({
                "quando": o.detectada_em, "fonte": "ocorrencia", "tipo": o.tipo,
                "titulo": f"Ocorrência: {o.get_tipo_display()}", "detalhe": o.descricao, "nivel": nivel, "ref_id": o.pk,
            })
            if o.resolvida_em:
                itens.append({
                    "quando": o.resolvida_em, "fonte": "ocorrencia", "tipo": o.tipo,
                    "titulo": f"Ocorrência {o.get_estado_display().lower()}: {o.get_tipo_display()}",
                    "detalhe": "", "nivel": "ok", "ref_id": o.pk,
                })
    itens.sort(key=lambda i: i["quando"], reverse=True)
    return itens[:limite]


def _resumo_do_evento(e: M2MEvento) -> str:
    d = e.dados or {}
    if e.tipo == M2MEvento.Tipo.ESTADO_SESSAO_MUDOU:
        rotulos = dict(M2MChip.SessaoEstado.choices)
        return f"De {rotulos.get(d.get('de'), d.get('de') or '—')} para {rotulos.get(d.get('para'), d.get('para') or '—')}."
    if e.tipo in (M2MEvento.Tipo.DESATIVADO, M2MEvento.Tipo.REATIVADO):
        return d.get("motivo", "")
    if e.tipo == M2MEvento.Tipo.EDICAO:
        return "Campos alterados: " + ", ".join(sorted((d.get("alteracoes") or {}).keys())) + "."
    if e.tipo == M2MEvento.Tipo.CADASTRO:
        return f"Cadastrado como {d.get('nome', '')}."
    if e.tipo == M2MEvento.Tipo.SITUACAO_MUDOU:
        return _resumo_da_mudanca_de_situacao(e)
    if e.tipo in _EVENTOS_DE_MOVIMENTACAO:
        return _resumo_da_movimentacao(e.tipo, d)
    if e.tipo == M2MEvento.Tipo.ITEM_REMOVIDO:
        return (
            f"O vínculo direto com o equipamento \"{d.get('item_nome', '')}\" do estoque de TI foi removido; "
            "o veículo passou a ser o equipamento do chip."
        )
    return ""


_EVENTOS_DE_MOVIMENTACAO = (
    M2MEvento.Tipo.ENTRADA, M2MEvento.Tipo.VINCULADO, M2MEvento.Tipo.DESVINCULADO, M2MEvento.Tipo.TRANSFERIDO,
    M2MEvento.Tipo.SUBSTITUIDO, M2MEvento.Tipo.RETORNO_DISPONIBILIDADE, M2MEvento.Tipo.INATIVADO,
)


def _resumo_da_movimentacao(tipo: str, d: dict) -> str:
    """Frase curta de um `M2MEvento` de movimentação, a partir do que a movimentação gravou em `dados`.
    `origem_texto`/`destino_texto` (já com "veículo X" ou "equipamento Y") valem quando existem; o evento
    antigo, de antes do equipamento de TI, só tinha `veiculo_*` e continua lido do mesmo jeito."""
    origem = d.get("origem_texto") or (f"veículo {d['veiculo_origem']}" if d.get("veiculo_origem") else "")
    destino = d.get("destino_texto") or (f"veículo {d['veiculo_destino']}" if d.get("veiculo_destino") else "")
    motivo = d.get("motivo", "")
    if tipo == M2MEvento.Tipo.ENTRADA:
        texto = "Entrada do chip no controle (disponível)."
    elif tipo == M2MEvento.Tipo.VINCULADO:
        texto = f"Vinculado ao {destino}."
    elif tipo == M2MEvento.Tipo.DESVINCULADO:
        texto = f"Desvinculado do {origem}; ficou {d.get('situacao_uso_rotulo', 'disponível').lower()}."
    elif tipo == M2MEvento.Tipo.TRANSFERIDO:
        texto = f"Transferido do {origem} para o {destino}."
    elif tipo == M2MEvento.Tipo.SUBSTITUIDO:
        outro = d.get("chip_relacionado", "")
        texto = (
            f"Substituiu o chip {outro} no {destino}." if destino
            else f"Substituído pelo chip {outro} no {origem}."
        )
    elif tipo == M2MEvento.Tipo.RETORNO_DISPONIBILIDADE:
        anterior = d.get("situacao_anterior_rotulo")      # o evento de antes de 15/11/2026 não tem
        texto = f"Devolvido à disponibilidade (estava {anterior.lower()})." if anterior else "Devolvido à disponibilidade."
    else:
        texto = "Chip inativado." + (f" Estava no {origem}." if origem else "")
    return f"{texto} Motivo: {motivo}" if motivo else texto


def mudancas_de_situacao(chip: M2MChip, limite: int = 6) -> list[dict]:
    """O recorte CURTO da trilha que responde "por onde este chip passou": as últimas mudanças de situação e as trocas
    (vinculado, desvinculado, transferido, substituído, devolvido, inativado, situação mudada), do mais novo para o mais
    antigo. É a mesma trilha da `linha_do_tempo`, só os eventos que mexem na situação. Cada item:
    `{"quando", "titulo", "detalhe", "tipo"}`."""
    eventos = chip.eventos.filter(
        tipo__in=_EVENTOS_DE_MOVIMENTACAO + (M2MEvento.Tipo.SITUACAO_MUDOU,)
    ).select_related("usuario").order_by("-em", "-pk")[:limite]
    return [
        {"quando": e.em, "titulo": e.get_tipo_display(), "detalhe": _resumo_do_evento(e), "tipo": e.tipo} for e in eventos
    ]


def _resumo_da_mudanca_de_situacao(e: M2MEvento) -> str:
    """"De disponível para com defeito. Motivo: … Por fulano." — a única linha da trilha que diz QUEM fez, porque a
    situação é decisão de gente (o resto da trilha é a API ou o cadastro) e quem decidiu é o que se procura depois."""
    d = e.dados or {}
    texto = f"De {(d.get('situacao_anterior_rotulo') or '—').lower()} para {(d.get('situacao_uso_rotulo') or '—').lower()}."
    motivo = (d.get("motivo") or "").strip()
    if motivo:
        texto += f" Motivo: {motivo}" + ("" if motivo[-1] in ".!?" else ".")
    if e.usuario_id:
        texto += f" Por {e.usuario.get_username()}."
    return texto


def _unidade_para(maior_valor: int, base: int) -> tuple[str, int]:
    for rotulo, expoente in (("TB", 4), ("GB", 3), ("MB", 2), ("KB", 1)):
        if maior_valor >= base ** expoente:
            return rotulo, base ** expoente
    return "B", 1


def serie_consumo(chip: M2MChip, limite: int = 90) -> dict:
    """Os volumes de consumo informados pela TIM em cada consulta VÁLIDA e
    interpretada do chip, do mais antigo ao mais novo (até `limite`):

        {"pontos": [{"em": datetime, "bytes": int, "valor": float, "consulta_id": int}],
         "suficiente": bool,        # >= 3 pontos: só então vale desenhar o gráfico
         "unidade": "MB", "divisor": 1000000, "base": 1000}

    `valor` já está em `unidade` (o que o Chart.js plota). É o valor informado em
    cada consulta — não o consumo "entre" consultas."""
    mapa = M2MIntegracao.get().mapeamento or {}
    base = (mapa.get("consumo") or {}).get("base") or 1000
    consultas = list(
        chip.consultas.defer("payload")
        .filter(tipo="consumo", resultado__in=M2MConsulta.RESULTADOS_VALIDOS, volume_bytes__isnull=False)
        .order_by("-iniciada_em", "-pk")[: max(1, limite)]
    )
    consultas.reverse()
    maior = max((c.volume_bytes for c in consultas), default=0)
    unidade, divisor = _unidade_para(maior, base)
    return {
        "pontos": [
            {"em": c.iniciada_em, "bytes": c.volume_bytes, "valor": round(c.volume_bytes / divisor, 3), "consulta_id": c.pk}
            for c in consultas
        ],
        "suficiente": len(consultas) >= 3, "unidade": unidade, "divisor": divisor, "base": base,
    }


def ultima_resposta_valida(tipo: str | None = None, chip: M2MChip | None = None) -> M2MConsulta | None:
    """A consulta bem-sucedida MAIS RECENTE que trouxe corpo JSON (com payload) — a
    amostra que a tela de mapeamento oferece para escolher os campos
    (`m2m_interpretacao.caminhos(consulta.payload)`). Filtra por `tipo` e/ou `chip`."""
    qs = M2MConsulta.objects.filter(resultado="sucesso", payload__isnull=False)
    if tipo:
        qs = qs.filter(tipo=tipo)
    if chip is not None:
        qs = qs.filter(chip=chip)
    return qs.select_related("chip").order_by("-iniciada_em", "-pk").first()


# ═════════════════════════════════════════════════════════════
# Leituras das telas (Fase B)
#
# Tudo daqui é LEITURA: nenhuma função grava, e nenhuma tela refaz a conta por
# conta própria. As views só validam a entrada, chamam estas funções e renderizam.
# ═════════════════════════════════════════════════════════════

def base_de_consumo(mapeamento) -> int:
    """1000 ou 1024, conforme o mapeamento (1000 sem mapeamento)."""
    return ((mapeamento or {}).get("consumo") or {}).get("base") or 1000


def mapeamento_configurado(mapeamento) -> dict:
    """O que o mapeamento já permite interpretar: `{"sessao": bool, "consumo": bool,
    "identidade": bool}`. Sessão só conta com o campo do estado E ao menos um valor
    de "ativa" ou "sem sessão" — sem isso toda resposta cai em "não interpretado".
    É o que as telas usam para dizer "sem mapeamento" em vez de mostrar zero."""
    mapa = mapeamento or {}
    s, c = mapa.get("sessao") or {}, mapa.get("consumo") or {}
    return {
        "sessao": bool(s.get("campo_estado") and (s.get("valores_ativa") or s.get("valores_sem_sessao"))),
        "consumo": bool(c.get("campo_volume")),
        "identidade": bool(mapa.get("identidade")),
    }


def configuracao_atual() -> dict:
    """A configuração para as telas, SEM nenhum campo da credencial (nem o token
    cifrado): os limites de `LIMITES_CONFIG`, o mapeamento, o tique, e quando/por
    quem o token foi trocado pela última vez."""
    cfg = M2MIntegracao.get()
    dados = {campo: getattr(cfg, campo) for campo in LIMITES_CONFIG}
    dados.update(
        mapeamento=cfg.mapeamento or {}, tique_minutos=cfg.tique_minutos,
        token_atualizado_em=cfg.token_atualizado_em,
        token_atualizado_por=(
            cfg.token_atualizado_por.get_username() if cfg.token_atualizado_por_id else ""
        ),
    )
    return dados


def interpretar_data(texto):
    """`AAAA-MM-DD` -> `date`, ou `None` (vazio ou inválido: o filtro é ignorado)."""
    try:
        return parse_date((texto or "").strip())
    except ValueError:
        return None


def _inicio_do_dia(dia):
    return timezone.make_aware(datetime.combine(dia, hora_do_dia.min))


def _intervalo_de_datas(de, ate) -> tuple:
    """`[inicio, fim)` em datetimes cientes de fuso; `ate` é INCLUSIVO (vai até o fim do dia)."""
    return (
        _inicio_do_dia(de) if de else None,
        _inicio_do_dia(ate + timedelta(days=1)) if ate else None,
    )


# ── lista de chips ───────────────────────────────────────────

SITUACOES_DE_MONITORAMENTO = tuple(ROTULO_SITUACAO)
ORDENS_DE_CHIPS = ("atencao", "nome", "consumo", "consulta")


def _q_situacao(situacao: str, limite):
    """O mesmo critério de `M2MChip.situacao_monitoramento_com`, em SQL (usa a
    anotação `referencia` = última consulta válida ou, na falta, o cadastro)."""
    ativo_sem_falha = Q_CONSULTAVEL & Q(falhas_consecutivas=0)
    if situacao == "desativado":
        return Q(monitorado=False)
    if situacao == "sem_integracao":
        return Q(monitorado=True) & ~Q(operadora__integracao=INTEGRACAO_TIM)
    if situacao == "erro_consulta":
        return Q_CONSULTAVEL & Q(falhas_consecutivas__gt=0)
    if situacao == "pendente":
        return ativo_sem_falha & Q(ultima_tentativa_em__isnull=True)
    if situacao == "desatualizado":
        return ativo_sem_falha & Q(ultima_tentativa_em__isnull=False, referencia__lt=limite)
    return ativo_sem_falha & Q(ultima_tentativa_em__isnull=False, referencia__gte=limite)  # normal


# ── Em uso × não utilizado (o gráfico de utilização e a lista dele usam ESTA definição) ─────────────────
# Em uso = vínculo ATIVO com um equipamento que está em operação. Nunca é deduzido de a API ter respondido, de
# ter sessão ou de ter consumo: chip cadastrado e consultado, sem vínculo, é "não utilizado". Os demais estados
# do cadastro ficam SEPARADOS (e fora do percentual), na ordem de precedência abaixo — cada chip em UM grupo só.
# "Em operação" é a MESMA régua para veículo e para equipamento do estoque: Ativo ou Em manutenção. Equipamento na
# oficina continua sendo o dono do chip (o vínculo não foi desfeito), e só o veículo contava a manutenção — um chip
# vinculado a um item em manutenção saía do gráfico de utilização e o "Em uso" mostrava um a menos que os vínculos.
STATUS_DE_VEICULO_EM_OPERACAO = (FrotaVeiculo.Status.ATIVO, FrotaVeiculo.Status.MANUTENCAO)
STATUS_DE_ITEM_EM_OPERACAO = ("ativo", "manutencao")
GRUPOS_DE_USO = (
    ("em_uso", "Em uso"),
    ("nao_utilizado", "Não utilizados"),
    ("vinculo_inativo", "Vinculados a equipamento fora de operação"),
    ("manutencao", "Chips em manutenção"),
    ("defeito", "Chips com defeito"),
    ("pausado", "Chips pausados"),
    ("reservado", "Chips reservados"),
    ("inativo", "Chips inativos"),
    ("desativado", "Monitoramento desativado"),
    ("sem_integracao", "Sem integração com a operadora"),
)
ROTULO_DE_USO = dict(GRUPOS_DE_USO)
# Os grupos que SÃO a própria `situacao_uso` do chip (decisão de cadastro, não leitura da API): cada situação que não é
# disponível nem vinculado tem o seu — sem isso o chip novo (pausado, defeito, reservado) cairia fora de todos os grupos e a
# soma do gráfico de utilização deixaria de fechar com o total.
GRUPOS_QUE_SAO_SITUACAO = ("manutencao", "defeito", "pausado", "reservado", "inativo")


def _vinculo_ativo_do_chip():
    return FrotaVinculo.objects.filter(chip=OuterRef("pk"), fim_em__isnull=True)


def q_uso(grupo: str) -> Q:
    """O critério SQL de cada grupo de `GRUPOS_DE_USO` (chave inválida: nenhum chip). Precedência: situação decidida no
    cadastro (inativo, em manutenção, com defeito, pausado, reservado) → monitoramento desativado → operadora sem
    integração → vínculo."""
    operando = Q(situacao_uso__in=(M2MChip.SituacaoUso.DISPONIVEL, M2MChip.SituacaoUso.VINCULADO))
    acompanhado = operando & Q_CONSULTAVEL
    vinculado = acompanhado & Q(situacao_uso=M2MChip.SituacaoUso.VINCULADO)
    destino_em_operacao = Exists(_vinculo_ativo_do_chip().filter(
        Q(veiculo__status__in=STATUS_DE_VEICULO_EM_OPERACAO) | Q(item__status__in=STATUS_DE_ITEM_EM_OPERACAO)
    ))
    if grupo in GRUPOS_QUE_SAO_SITUACAO:
        return Q(situacao_uso=grupo)
    if grupo == "desativado":
        return operando & Q(monitorado=False)
    if grupo == "sem_integracao":
        return operando & Q(monitorado=True) & ~Q(operadora__integracao=INTEGRACAO_TIM)
    if grupo == "em_uso":
        return vinculado & destino_em_operacao
    if grupo == "vinculo_inativo":
        return vinculado & ~destino_em_operacao
    if grupo == "nao_utilizado":
        return acompanhado & Q(situacao_uso=M2MChip.SituacaoUso.DISPONIVEL)
    return Q(pk__in=[])


def q_unidade(unidade_id: int) -> Q:
    """Chips de uma unidade/fazenda. A unidade de um chip vinculado é a do EQUIPAMENTO onde ele está (o cadastro do
    chip quase nunca preenche a unidade); sem vínculo, vale a unidade do próprio chip. Cada chip numa unidade só."""
    vinculo = _vinculo_ativo_do_chip()
    no_destino = Exists(vinculo.filter(Q(veiculo__localidade_id=unidade_id) | Q(item__localidade_id=unidade_id)))
    return (Q(Exists(vinculo)) & Q(no_destino)) | (~Q(Exists(vinculo)) & Q(localidade_id=unidade_id))


def _so_digitos(texto) -> str:
    return re.sub(r"\D", "", texto or "")


def listar_chips(filtros: dict | None = None, agora=None):
    """QuerySet da lista de chips, filtrado e ordenado. `filtros` (todos opcionais;
    valor vazio ou inválido é ignorado):

        q            busca rápida: nome, descrição, ICCID/IMSI (aceita espaços), número da
                     linha, operadora, veículo (código, nome ou placa do veículo ATUAL),
                     unidade, setor e responsável
        iccid, imsi  trecho do identificador (só dígitos)
        veiculo      id do veículo atual (só dígitos) OU trecho do código/nome/placa dele
        operadora    id da operadora
        situacao_uso `disponivel` | `vinculado` | `reservado` | `pausado` | `defeito` | `manutencao` | `inativo`
        estoque_ti   `com` | `sem`  (o chip tem ou não o item do estoque de TI ligado)
        sessao       `ativa` | `sem_sessao` | `nao_interpretado` | `nao_consultado`
        situacao     `normal` | `pendente` | `erro_consulta` | `desatualizado` | `desativado` |
                     `sem_integracao`
        conectividade  o segmento de `resumo_conectividade` (cada chip monitorado em UM só):
                     `ativa` | `sem_sessao` | `nao_interpretado` | `nao_consultado` | `falha_consulta`
        localidade   id da unidade;  setor  trecho do setor
        uso          um grupo de `GRUPOS_DE_USO` (em_uso, nao_utilizado, …) — a definição do gráfico de utilização
        unidade      id da unidade/fazenda do EQUIPAMENTO onde o chip está (`q_unidade`)
        inatividade  uma faixa de `m2m_analise_service.FAIXAS_DE_INATIVIDADE`;  consumo  uma categoria de
                     `CATEGORIAS_DE_CONSUMO` — os dois também leem `periodo` (`api`|7|30|90) e `escopo` (`uso`|`todos`)
        de, ate      `AAAA-MM-DD`: última CONSULTA (tentativa) dentro do período, ate inclusive
        ordem        `atencao` (padrão: quem precisa de atenção primeiro) | `nome` | `consumo` | `consulta`

    O `payload` das consultas nunca é carregado (até 64 KB por linha). Cada chip
    sai com `referencia` e `ocorrencia_aberta` anotados; para o rótulo da situação,
    o frescor e o consumo formatado use `anotar_chips`."""
    filtros = filtros or {}
    agora = agora or timezone.now()
    cfg = M2MIntegracao.get()
    limite = agora - timedelta(hours=cfg.horas_desatualizado)
    abertas = M2MOcorrencia.objects.filter(chip=OuterRef("pk"), estado__in=ABERTAS)
    qs = (
        M2MChip.objects.select_related("operadora", "localidade", "responsavel", "sessao_consulta", "consumo_consulta")
        .defer("sessao_consulta__payload", "consumo_consulta__payload")
        .annotate(referencia=Coalesce("ultima_valida_em", "created_at"), ocorrencia_aberta=Exists(abertas))
    )

    # O veículo ATUAL do chip (vínculo ativo): subconsulta, sem JOIN que multiplique linhas.
    vinculo_ativo = FrotaVinculo.objects.filter(chip=OuterRef("pk"), fim_em__isnull=True)

    def do_veiculo(**criterio):
        return Exists(vinculo_ativo.filter(**criterio))

    q = (filtros.get("q") or "").strip()
    if q:
        condicao = (
            Q(nome__icontains=q) | Q(descricao__icontains=q) | Q(setor__icontains=q)
            | Q(operadora__nome__icontains=q)
            | Q(localidade__local__icontains=q) | Q(responsavel__nome__icontains=q)
            | do_veiculo(veiculo__codigo__icontains=q) | do_veiculo(veiculo__nome__icontains=q)
            | do_veiculo(veiculo__placa__icontains=q.replace("-", "").replace(" ", ""))
            # o tablet/celular (estoque de TI) onde o chip está: pelo nome ou pelo número de série
            | do_veiculo(item__nome__icontains=q) | do_veiculo(item__numero_serie__icontains=q)
        )
        digitos = _so_digitos(q)
        if len(digitos) >= 3 and len(digitos) >= len(re.sub(r"[\s.\-]", "", q)):
            condicao |= Q(iccid__contains=digitos) | Q(imsi__contains=digitos) | Q(numero_linha__contains=digitos)
        qs = qs.filter(condicao)
    for campo in ("iccid", "imsi"):
        digitos = _so_digitos(filtros.get(campo))
        if digitos:
            qs = qs.filter(**{f"{campo}__contains": digitos})
    veiculo = str(filtros.get("veiculo") or "").strip()
    if veiculo:
        if veiculo.isdigit():
            qs = qs.filter(do_veiculo(veiculo_id=int(veiculo)))
        else:
            qs = qs.filter(
                do_veiculo(veiculo__codigo__icontains=veiculo) | do_veiculo(veiculo__nome__icontains=veiculo)
                | do_veiculo(veiculo__placa__icontains=veiculo.replace("-", "").replace(" ", ""))
            )
    if str(filtros.get("operadora") or "").isdigit():
        qs = qs.filter(operadora_id=int(filtros["operadora"]))
    if filtros.get("situacao_uso") in M2MChip.SituacaoUso.values:
        qs = qs.filter(situacao_uso=filtros["situacao_uso"])
    if filtros.get("estoque_ti") == "com":                    # patrimônio no estoque de TI (regra 66)
        qs = qs.filter(item_ti__isnull=False)
    elif filtros.get("estoque_ti") == "sem":
        qs = qs.filter(item_ti__isnull=True)
    if filtros.get("sessao") in M2MChip.SessaoEstado.values:
        qs = qs.filter(sessao_estado=filtros["sessao"])
    if filtros.get("situacao") in SITUACOES_DE_MONITORAMENTO:
        qs = qs.filter(_q_situacao(filtros["situacao"], limite))
    conectividade = filtros.get("conectividade")
    if conectividade == "falha_consulta":
        qs = qs.filter(Q_CONSULTAVEL, falhas_consecutivas__gt=0)
    elif conectividade in M2MChip.SessaoEstado.values:
        qs = qs.filter(Q_CONSULTAVEL, falhas_consecutivas=0, sessao_estado=conectividade)
    if str(filtros.get("localidade") or "").isdigit():
        qs = qs.filter(localidade_id=int(filtros["localidade"]))
    # Os filtros do dashboard de análise (`m2m_analise_service`): o gráfico abre a lista com o MESMO critério.
    if filtros.get("uso") in ROTULO_DE_USO:
        qs = qs.filter(q_uso(filtros["uso"]))
    if str(filtros.get("unidade") or "").isdigit():
        qs = qs.filter(q_unidade(int(filtros["unidade"])))
    if filtros.get("inatividade") or filtros.get("consumo"):
        from services import m2m_analise_service as analise     # a análise parte deste módulo: import tardio
        qs = qs.filter(pk__in=analise.pks_do_filtro(filtros, agora))
    setor = (filtros.get("setor") or "").strip()
    if setor:
        qs = qs.filter(setor__icontains=setor)
    inicio, fim = _intervalo_de_datas(interpretar_data(filtros.get("de")), interpretar_data(filtros.get("ate")))
    if inicio:
        qs = qs.filter(ultima_tentativa_em__gte=inicio)
    if fim:
        qs = qs.filter(ultima_tentativa_em__lt=fim)

    ordem = filtros.get("ordem") if filtros.get("ordem") in ORDENS_DE_CHIPS else "atencao"
    if ordem == "nome":
        return qs.order_by("nome", "pk")
    if ordem == "consumo":
        return qs.order_by(F("consumo_bytes").desc(nulls_last=True), "nome", "pk")
    if ordem == "consulta":
        return qs.order_by(F("ultima_tentativa_em").desc(nulls_last=True), "nome", "pk")
    prioridade = Case(
        When(monitorado=False, then=Value(5)),
        When(~Q(operadora__integracao=INTEGRACAO_TIM), then=Value(5)),   # sem integração: não há o que vigiar
        When(falhas_consecutivas__gt=0, then=Value(0)),
        When(ocorrencia_aberta=True, then=Value(1)),
        When(ultima_tentativa_em__isnull=True, then=Value(3)),   # pendente (nunca consultado)
        When(referencia__lt=limite, then=Value(2)),              # desatualizado
        default=Value(4),
        output_field=IntegerField(),
    )
    return qs.annotate(prioridade=prioridade).order_by("prioridade", "nome", "pk")


def anotar_chips(chips, cfg: M2MIntegracao | None = None, agora=None) -> list:
    """Acrescenta a cada chip da lista o que a tela precisa e que não é coluna —
    sem consultar o banco por linha (a configuração é lida uma vez):

        situacao, situacao_rotulo      do monitoramento (`ROTULO_SITUACAO`)
        frescor_sessao, frescor_consumo   `frescor()` da consulta que trouxe cada dado
        consumo_txt                    volume formatado ("1,5 GB") ou ""
        pct_limite                     % do limite mensal — só se o chip tem limite cadastrado
        ultima_falha_rotulo            "Erro de rede", ...
        estado_bruto                   o valor da sessão como a API mandou
        vinculo_atual, veiculo_atual   o `FrotaVinculo` ativo e o `FrotaVeiculo` (ou `None`),
                                       buscados juntos numa consulta só para a lista toda
        equipamento_atual              o `Item` do estoque de TI onde o chip está (tablet, celular) ou `None`
        destino_atual_rotulo/_tipo     o texto do lugar do chip e `"veiculo"`/`"item"` (`""` sem vínculo)

    Os chips devem vir com `select_related("operadora")` (`listar_chips` já faz).
    Devolve a lista (mesmos objetos)."""
    cfg = cfg or M2MIntegracao.get()
    agora = agora or timezone.now()
    horas = cfg.horas_desatualizado
    base = base_de_consumo(cfg.mapeamento)
    lista = list(chips)
    ativos = {
        v.chip_id: v
        # `tipo`/`classe_operacional` e `subtipo` vão junto só para a lista poder DESENHAR o tipo do destino (ícone),
        # na mesma consulta — sem uma por linha.
        for v in FrotaVinculo.objects.filter(chip__in=[c.pk for c in lista], fim_em__isnull=True).select_related(
            "veiculo", "veiculo__tipo", "veiculo__classe_operacional", "item", "item__subtipo", "item__subtipo__categoria",
        )
    }
    for chip in lista:
        chip.vinculo_atual = ativos.get(chip.pk)
        chip.veiculo_atual = chip.vinculo_atual.veiculo if chip.vinculo_atual else None
        chip.equipamento_atual = chip.vinculo_atual.item if chip.vinculo_atual else None
        chip.destino_atual_rotulo = chip.vinculo_atual.destino_rotulo if chip.vinculo_atual else ""
        chip.destino_atual_tipo = chip.vinculo_atual.destino_tipo if chip.vinculo_atual else ""
        chip.situacao = chip.situacao_monitoramento_com(horas, agora)
        chip.situacao_rotulo = ROTULO_SITUACAO[chip.situacao]
        chip.frescor_sessao = frescor(chip.sessao_consulta.iniciada_em if chip.sessao_consulta_id else None, horas, agora)
        chip.frescor_consumo = frescor(chip.consumo_consulta.iniciada_em if chip.consumo_consulta_id else None, horas, agora)
        chip.consumo_txt = interp.formatar_bytes(chip.consumo_bytes, base) if chip.consumo_bytes is not None else ""
        chip.pct_limite = (
            round(chip.consumo_bytes * 100 / chip.limite_mensal_bytes)
            if chip.limite_mensal_bytes and chip.consumo_bytes is not None else None
        )
        chip.ultima_falha_rotulo = _rotulo_resultado(chip.ultima_falha_tipo) if chip.ultima_falha_tipo else ""
        chip.estado_bruto = chip.sessao_consulta.estado_bruto if chip.sessao_consulta_id else ""
    return lista


def opcoes_de_filtro_de_chips() -> dict:
    """Valores que os filtros da lista oferecem: `localidades` (só as que têm chip,
    como `[(id, nome)]`), `setores` (distintos, em ordem alfabética), `operadoras`
    (as que têm chip, `[(id, nome)]`) e `situacoes_uso` (`M2MChip.SituacaoUso.choices`)."""
    return {
        "localidades": list(
            Localidade.objects.filter(chips_m2m__isnull=False).distinct().order_by("local").values_list("pk", "local")
        ),
        "setores": sorted(set(M2MChip.objects.exclude(setor="").values_list("setor", flat=True)), key=str.casefold),
        "operadoras": list(
            ConectividadeOperadora.objects.filter(chips__isnull=False).distinct().order_by("nome").values_list("pk", "nome")
        ),
        "situacoes_uso": list(M2MChip.SituacaoUso.choices),
    }


def buscar_veiculos(q: str, limite: int = 20, *, incluir_desativados: bool = False) -> list[dict]:
    """Veículos para os campos "Veículo" (cadastro de chip, vincular, transferir), no
    formato do Select2: `[{"id", "text"}]` com `text` = "COD · nome · modelo · placa".
    Mínimo de 2 caracteres; busca em código, nome, modelo, placa (sem hífen) e chassi/série.
    Veículo `desativado` não aparece (não recebe chip), salvo `incluir_desativados=True`."""
    q = (q or "").strip()
    if len(q) < 2:
        return []
    qs = FrotaVeiculo.objects.select_related("tipo", "classe_operacional")
    if not incluir_desativados:
        qs = qs.exclude(status=FrotaVeiculo.Status.DESATIVADO)
    sem_hifen = q.replace("-", "").replace(" ", "")
    qs = qs.filter(
        Q(codigo__icontains=q) | Q(nome__icontains=q) | Q(modelo__icontains=q)
        | Q(placa__icontains=sem_hifen) | Q(chassi_serie__icontains=q)
    ).order_by("codigo", "pk")[:limite]
    return [
        {
            "id": v.pk, "text": " · ".join(p for p in (v.codigo, v.nome, v.modelo, v.placa) if p),
            "icone": icone_service.icone_do_veiculo(v.classe_operacional, v.tipo),      # o desenho do tipo no resultado do Select2
        }
        for v in qs
    ]


def buscar_responsaveis(q: str, limite: int = 20) -> list[dict]:
    """Colaboradores ATIVOS para o campo "Responsável", no formato do Select2."""
    q = (q or "").strip()
    if len(q) < 2:
        return []
    achados = Usuario.objects.filter(status="ativo", nome__icontains=q).order_by("nome", "pk")[:limite]
    return [{"id": u.pk, "text": u.nome} for u in achados]


# ── Visão geral ──────────────────────────────────────────────

def resumo_conectividade() -> dict:
    """A barra empilhada da Visão geral: cada chip CONSULTÁVEL cai em UM segmento só
    (por isso a soma fecha com o total). Quem falhou na última consulta fica em
    "falha de consulta" — o estado dele é o da última resposta válida, ou seja, velho —
    e não em "ativa" ou "sem sessão". Devolve:

        {"total": N, "segmentos": [{"chave", "rotulo", "n", "pct"}...]}   # ordem fixa

    chaves: `ativa`, `sem_sessao`, `nao_interpretado`, `falha_consulta`, `nao_consultado`."""
    d = M2MChip.objects.filter(Q_CONSULTAVEL).aggregate(
        total=Count("pk"),
        falha_consulta=Count("pk", filter=Q(falhas_consecutivas__gt=0)),
        ativa=Count("pk", filter=Q(falhas_consecutivas=0, sessao_estado="ativa")),
        sem_sessao=Count("pk", filter=Q(falhas_consecutivas=0, sessao_estado="sem_sessao")),
        nao_interpretado=Count("pk", filter=Q(falhas_consecutivas=0, sessao_estado="nao_interpretado")),
        nao_consultado=Count("pk", filter=Q(falhas_consecutivas=0, sessao_estado="nao_consultado")),
    )
    total = d["total"]
    rotulos = (
        ("ativa", "Sessão ativa"), ("sem_sessao", "Sem sessão"), ("nao_interpretado", "Não interpretado"),
        ("falha_consulta", "Falha de consulta"), ("nao_consultado", "Não consultado"),
    )
    return {
        "total": total,
        "segmentos": [
            {"chave": c, "rotulo": r, "n": d[c], "pct": round(d[c] * 100 / total, 2) if total else 0}
            for c, r in rotulos
        ],
    }


def consumo_agregado(cfg: M2MIntegracao | None = None) -> dict:
    """A soma do ÚLTIMO volume informado pela TIM em cada chip monitorado — só dos
    chips com consumo INTERPRETADO. Não é consumo do mês nem saldo: cada chip
    informa o que a API devolveu na última consulta válida (e a data mais antiga e
    a mais nova entram para a tela dizer isso). Chaves: `chips_com_consumo`,
    `monitorados`, `total_bytes`, `total_txt` ("—" sem nenhum), `mais_antigo_em`,
    `mais_recente_em`, `mapeado` (o mapeamento do volume existe)."""
    cfg = cfg or M2MIntegracao.get()
    d = M2MChip.objects.filter(Q_CONSULTAVEL, consumo_bytes__isnull=False).aggregate(
        n=Count("pk"), total=Sum("consumo_bytes"),
        antigo=Min("consumo_consulta__iniciada_em"), recente=Max("consumo_consulta__iniciada_em"),
    )
    n = d["n"]
    return {
        "chips_com_consumo": n,
        "monitorados": M2MChip.objects.filter(Q_CONSULTAVEL).count(),
        "total_bytes": d["total"] or 0,
        "total_txt": interp.formatar_bytes(d["total"] or 0, base_de_consumo(cfg.mapeamento)) if n else "—",
        "mais_antigo_em": d["antigo"], "mais_recente_em": d["recente"],
        "mapeado": mapeamento_configurado(cfg.mapeamento)["consumo"],
    }


def ultimas_consultas(
    limite: int = 10, *, tipo: str | None = None, chip: M2MChip | None = None,
    so_falhas: bool = False, de=None, ate=None,
) -> list[M2MConsulta]:
    """As consultas mais recentes (sem o `payload`), com o chip já carregado.
    `so_falhas` deixa só as que NÃO foram válidas; `de`/`ate` são `date` (ate inclusive)."""
    qs = M2MConsulta.objects.select_related("chip").defer("payload")
    if tipo in TIPOS_DE_CONSULTA:
        qs = qs.filter(tipo=tipo)
    if chip is not None:
        qs = qs.filter(chip=chip)
    if so_falhas:
        qs = qs.exclude(resultado__in=M2MConsulta.RESULTADOS_VALIDOS)
    inicio, fim = _intervalo_de_datas(de, ate)
    if inicio:
        qs = qs.filter(iniciada_em__gte=inicio)
    if fim:
        qs = qs.filter(iniciada_em__lt=fim)
    return list(qs.order_by("-iniciada_em", "-pk")[: max(1, limite)])


def chips_com_falha(limite: int = 8, cfg: M2MIntegracao | None = None, agora=None) -> list[M2MChip]:
    """Chips monitorados cuja ÚLTIMA tentativa falhou, os que falham há mais tempo
    primeiro — já anotados (`anotar_chips`)."""
    qs = (
        M2MChip.objects.filter(Q_CONSULTAVEL, falhas_consecutivas__gt=0)
        .select_related("operadora", "localidade", "responsavel", "sessao_consulta", "consumo_consulta")
        .defer("sessao_consulta__payload", "consumo_consulta__payload")
        .order_by("-falhas_consecutivas", "nome", "pk")[: max(1, limite)]
    )
    return anotar_chips(qs, cfg, agora)


def descrever_evento(e: M2MEvento) -> str:
    """Uma frase curta para o evento, sem nunca citar o token. Estende o resumo da
    linha do tempo com os eventos da integração (que não têm chip)."""
    d = e.dados or {}
    texto = _resumo_do_evento(e)
    if texto:
        return texto
    if e.tipo == M2MEvento.Tipo.TOKEN_ALTERADO:
        return "Token removido do banco." if d.get("acao") == "removido" else "Token novo gravado (cifrado)."
    if e.tipo == M2MEvento.Tipo.CONFIG_ALTERADA:
        return "Campos alterados: " + ", ".join(sorted((d.get("alteracoes") or {}).keys())) + "."
    if e.tipo == M2MEvento.Tipo.MAPEAMENTO_ALTERADO:
        if d.get("acao") == "reinterpretar_historico":
            return f"Histórico reinterpretado: {d.get('alteradas', 0)} de {d.get('consultas', 0)} consultas."
        return "O mapeamento da resposta foi alterado."
    if e.tipo == M2MEvento.Tipo.TESTE_CONEXAO:
        return f"Resultado: {_rotulo_resultado(d.get('resultado', ''))}" + (
            f" (HTTP {d['http_status']})." if d.get("http_status") else "."
        )
    return ""


def eventos_recentes(limite: int = 10, *, so_da_integracao: bool = False) -> list[dict]:
    """Os eventos de auditoria mais novos: `[{"em", "titulo", "detalhe", "tipo", "chip", "usuario"}]`.
    `so_da_integracao` deixa só os que não são de um chip (token, configuração, mapeamento, teste)."""
    qs = M2MEvento.objects.select_related("chip", "usuario")
    if so_da_integracao:
        qs = qs.filter(chip__isnull=True)
    return [
        {
            "em": e.em, "titulo": e.get_tipo_display(), "detalhe": descrever_evento(e), "tipo": e.tipo,
            "chip": e.chip, "usuario": e.usuario.get_username() if e.usuario_id else "",
        }
        for e in qs.order_by("-em", "-pk")[: max(1, limite)]
    ]


# ── Detalhe do chip ──────────────────────────────────────────

def resumo_do_chip(chip: M2MChip, cfg: M2MIntegracao | None = None, agora=None) -> dict:
    """Sessão e consumo ATUAIS do chip, como o detalhe os mostra: sempre o último
    dado VÁLIDO, com a hora da consulta que o trouxe, mesmo que as consultas
    seguintes tenham falhado. Chaves:

        situacao, situacao_rotulo
        sessao  {consulta, estado, estado_rotulo, estado_bruto, motivo, campos, frescor, mapeada}
        consumo {consulta, valor_original, unidade_original, volume_bytes, volume_txt, periodo_inicio,
                 periodo_fim, motivo, frescor, mapeado, limite_txt, pct_limite}
        ultima_falha {em, tipo, rotulo} | None

    `motivo` diz por que algo não foi interpretado (sem mapeamento, campo ausente,
    unidade desconhecida...). `mapeada`/`mapeado` False = o mapeamento ainda não existe."""
    cfg = cfg or M2MIntegracao.get()
    agora = agora or timezone.now()
    mapa = cfg.mapeamento or {}
    configurado = mapeamento_configurado(mapa)
    base = base_de_consumo(mapa)
    horas = cfg.horas_desatualizado

    sc = chip.sessao_consulta if chip.sessao_consulta_id else None
    sessao = {
        "consulta": sc, "estado": chip.sessao_estado, "estado_rotulo": chip.get_sessao_estado_display(),
        "estado_bruto": sc.estado_bruto if sc else "", "motivo": "", "campos": [],
        "frescor": frescor(sc.iniciada_em if sc else None, horas, agora), "mapeada": configurado["sessao"],
    }
    if sc is not None:
        lido = interp.interpretar_sessao(sc.payload, mapa)
        sessao["campos"], sessao["motivo"] = lido.campos_exibir, lido.motivo

    cc = chip.consumo_consulta if chip.consumo_consulta_id else None
    consumo = {
        "consulta": cc, "valor_original": cc.valor_original if cc else "", "unidade_original": cc.unidade_original if cc else "",
        "volume_bytes": cc.volume_bytes if cc else None, "volume_txt": "", "periodo_inicio": cc.periodo_inicio if cc else None,
        "periodo_fim": cc.periodo_fim if cc else None, "motivo": "",
        "frescor": frescor(cc.iniciada_em if cc else None, horas, agora), "mapeado": configurado["consumo"],
        "limite_txt": interp.formatar_bytes(chip.limite_mensal_bytes, base) if chip.limite_mensal_bytes else "",
        "pct_limite": None, "fonte": "resumo", "completo": None, "dados_desde": None,
    }
    if cc is not None:
        periodo = cc.payload.get("_dados_periodo") if isinstance(cc.payload, dict) else None
        if isinstance(periodo, dict):
            # O volume é a soma das sessões de dados do mês (a mesma conta do portal), não o texto do resumo.
            consumo.update(fonte="sessoes_de_dados", completo=bool(periodo.get("completo")), dados_desde=interpretar_data(periodo.get("dados_desde")))
        if cc.volume_bytes is not None:
            consumo["volume_txt"] = interp.formatar_bytes(cc.volume_bytes, base)
            if chip.limite_mensal_bytes:
                consumo["pct_limite"] = round(cc.volume_bytes * 100 / chip.limite_mensal_bytes)
        else:
            consumo["motivo"] = interp.interpretar_consumo(cc.payload, mapa).motivo

    falha = None
    if chip.ultima_falha_em:
        falha = {
            "em": chip.ultima_falha_em, "tipo": chip.ultima_falha_tipo,
            "rotulo": _rotulo_resultado(chip.ultima_falha_tipo) if chip.ultima_falha_tipo else "Falha",
        }
    situacao = chip.situacao_monitoramento_com(horas, agora)
    return {
        "situacao": situacao, "situacao_rotulo": ROTULO_SITUACAO[situacao],
        "sessao": sessao, "consumo": consumo, "ultima_falha": falha,
    }


# ── Histórico de consumo ─────────────────────────────────────

def historico_consumo(chip: M2MChip | None = None, de=None, ate=None):
    """QuerySet das consultas de CONSUMO válidas (as que trouxeram resposta), da mais
    nova para a mais antiga, sem o `payload`. `de`/`ate`: `date` (ate inclusive).
    Resposta válida que o mapeamento não leu aparece com `volume_bytes` nulo —
    a tela diz "não interpretado", não esconde a linha."""
    qs = M2MConsulta.objects.filter(
        tipo="consumo", resultado__in=M2MConsulta.RESULTADOS_VALIDOS,
    ).select_related("chip").defer("payload")
    if chip is not None:
        qs = qs.filter(chip=chip)
    inicio, fim = _intervalo_de_datas(de, ate)
    if inicio:
        qs = qs.filter(iniciada_em__gte=inicio)
    if fim:
        qs = qs.filter(iniciada_em__lt=fim)
    return qs.order_by("-iniciada_em", "-pk")


def falhas_de_consumo(chip: M2MChip | None = None, de=None, ate=None) -> int:
    """Quantas consultas de consumo FALHARAM no recorte (não entram na tabela do histórico)."""
    qs = M2MConsulta.objects.filter(tipo="consumo").exclude(resultado__in=M2MConsulta.RESULTADOS_VALIDOS)
    if chip is not None:
        qs = qs.filter(chip=chip)
    inicio, fim = _intervalo_de_datas(de, ate)
    if inicio:
        qs = qs.filter(iniciada_em__gte=inicio)
    if fim:
        qs = qs.filter(iniciada_em__lt=fim)
    return qs.count()


def formatar_diferenca(valor, base: int = 1000) -> str:
    """`+1,5 GB`, `-300 MB`, `0 B` — a diferença entre dois volumes, com sinal."""
    if valor is None:
        return "—"
    if valor == 0:
        return "0 B"
    return ("+" if valor > 0 else "-") + interp.formatar_bytes(abs(valor), base)


def comparar_periodos(a_de, a_ate, b_de, b_ate, chip: M2MChip | None = None, limite: int = 300) -> dict:
    """Compara dois períodos: para cada chip, o ÚLTIMO volume válido e interpretado
    informado dentro de cada um (`a_*`/`b_*` são `date`, ate inclusive). NÃO calcula
    franquia nem consumo "no intervalo": a diferença é só entre os dois últimos
    valores informados pela TIM. Chip sem valor em um dos períodos fica sem diferença.

        {"linhas": [{"chip", "a_bytes", "a_em", "b_bytes", "b_em", "diferenca", "diferenca_txt"}],
         "total_a", "total_b", "total_a_txt", "total_b_txt", "truncado": bool, "base": int}

    Só entram chips com valor em pelo menos um dos períodos."""
    base = base_de_consumo(M2MIntegracao.get().mapeamento)

    def ultimo(de, ate, campo):
        inicio, fim = _intervalo_de_datas(de, ate)
        qs = M2MConsulta.objects.filter(
            chip=OuterRef("pk"), tipo="consumo", resultado__in=M2MConsulta.RESULTADOS_VALIDOS, volume_bytes__isnull=False,
        )
        if inicio:
            qs = qs.filter(iniciada_em__gte=inicio)
        if fim:
            qs = qs.filter(iniciada_em__lt=fim)
        return Subquery(qs.order_by("-iniciada_em", "-pk").values(campo)[:1])

    chips = M2MChip.objects.annotate(
        a_bytes=ultimo(a_de, a_ate, "volume_bytes"), a_em=ultimo(a_de, a_ate, "iniciada_em"),
        b_bytes=ultimo(b_de, b_ate, "volume_bytes"), b_em=ultimo(b_de, b_ate, "iniciada_em"),
    ).filter(Q(a_bytes__isnull=False) | Q(b_bytes__isnull=False))
    if chip is not None:
        chips = chips.filter(pk=chip.pk)
    chips = list(chips.order_by("nome", "pk")[: limite + 1])
    truncado = len(chips) > limite
    linhas, total_a, total_b = [], 0, 0
    com_a = com_b = 0
    for c in chips[:limite]:
        diferenca = c.b_bytes - c.a_bytes if c.a_bytes is not None and c.b_bytes is not None else None
        total_a += c.a_bytes or 0
        total_b += c.b_bytes or 0
        com_a += c.a_bytes is not None
        com_b += c.b_bytes is not None
        linhas.append({
            "chip": c, "a_bytes": c.a_bytes, "a_em": c.a_em, "b_bytes": c.b_bytes, "b_em": c.b_em,
            "diferenca": diferenca, "diferenca_txt": formatar_diferenca(diferenca, base),
        })
    return {
        "linhas": linhas, "total_a": total_a, "total_b": total_b, "truncado": truncado, "base": base,
        "total_a_txt": interp.formatar_bytes(total_a, base) if com_a else "—",
        "total_b_txt": interp.formatar_bytes(total_b, base) if com_b else "—",
    }


# ── Sessões ──────────────────────────────────────────────────

def mudancas_de_estado(limite: int = 30) -> list[dict]:
    """As últimas mudanças de estado da sessão (eventos `estado_sessao_mudou`):
    `[{"em", "chip", "de", "para", "de_chave", "para_chave", "estado_bruto", "motivo"}]`.
    `de`/`para` já são os rótulos ("Sessão ativa", "Sem sessão"...)."""
    rotulos = dict(M2MChip.SessaoEstado.choices)
    saida = []
    eventos = M2MEvento.objects.filter(tipo=M2MEvento.Tipo.ESTADO_SESSAO_MUDOU).select_related("chip")
    for e in eventos.order_by("-em", "-pk")[: max(1, limite)]:
        d = e.dados or {}
        saida.append({
            "em": e.em, "chip": e.chip, "de_chave": d.get("de", ""), "para_chave": d.get("para", ""),
            "de": rotulos.get(d.get("de"), d.get("de") or "—"), "para": rotulos.get(d.get("para"), d.get("para") or "—"),
            "estado_bruto": d.get("estado_bruto", ""),
            "motivo": "Mapeamento aplicado ao histórico." if d.get("motivo") == "reinterpretacao_do_historico" else "",
        })
    return saida


# ── Ocorrências ──────────────────────────────────────────────

ESTADOS_DE_FILTRO_DE_OCORRENCIA = ("abertas", "encerradas", "todas", "aberta", "em_tratamento", "resolvida", "descartada")


def listar_ocorrencias(filtros: dict | None = None):
    """QuerySet de ocorrências com o chip e os tratamentos já carregados. `filtros`:

        estado  `abertas` (padrão: aberta + em tratamento) | `encerradas` | `todas` |
                um estado específico
        nivel   `info` | `atencao` | `critico`;   tipo  um de `M2MOcorrencia.Tipo`
        q       nome do chip, trecho do ICCID/IMSI, ou código/nome/placa do veículo

    Críticas primeiro, depois as de detecção mais recente."""
    filtros = filtros or {}
    qs = M2MOcorrencia.objects.select_related("chip", "veiculo").prefetch_related(
        Prefetch("tratamentos", queryset=M2MOcorrenciaTratamento.objects.select_related("usuario").order_by("em", "pk"))
    )
    estado = filtros.get("estado") if filtros.get("estado") in ESTADOS_DE_FILTRO_DE_OCORRENCIA else "abertas"
    if estado == "abertas":
        qs = qs.filter(estado__in=ABERTAS)
    elif estado == "encerradas":
        qs = qs.exclude(estado__in=ABERTAS)
    elif estado != "todas":
        qs = qs.filter(estado=estado)
    if filtros.get("nivel") in M2MOcorrencia.Nivel.values:
        qs = qs.filter(nivel=filtros["nivel"])
    if filtros.get("tipo") in M2MOcorrencia.Tipo.values:
        qs = qs.filter(tipo=filtros["tipo"])
    q = (filtros.get("q") or "").strip()
    if q:
        condicao = (
            Q(chip__nome__icontains=q) | Q(veiculo__codigo__icontains=q) | Q(veiculo__nome__icontains=q)
            | Q(veiculo__placa__icontains=q.replace("-", "").replace(" ", ""))
        )
        digitos = _so_digitos(q)
        if len(digitos) >= 3:
            condicao |= Q(chip__iccid__contains=digitos) | Q(chip__imsi__contains=digitos)
        qs = qs.filter(condicao)
    gravidade = Case(
        When(nivel="critico", then=Value(0)), When(nivel="atencao", then=Value(1)),
        default=Value(2), output_field=IntegerField(),
    )
    return qs.annotate(gravidade=gravidade).order_by("gravidade", "-ultima_deteccao_em", "-pk")


def contar_ocorrencias_por_estado() -> dict:
    """`{"aberta": n, "em_tratamento": n, "resolvida": n, "descartada": n}` (zeros incluídos)."""
    contagem = dict.fromkeys(M2MOcorrencia.Estado.values, 0)
    for estado, n in M2MOcorrencia.objects.values_list("estado").annotate(n=Count("pk")):
        contagem[estado] = n
    return contagem


# ── Configuração: amostra real, prévia do mapeamento ─────────

def amostra_de_resposta(tipo: str) -> dict | None:
    """A última resposta REAL (JSON) do tipo, para a tela de mapeamento escolher
    campos: `{"consulta", "chip", "em", "payload", "caminhos": [{"caminho", "tipo", "exemplo"}]}`
    ou `None` enquanto nenhuma consulta trouxe corpo."""
    consulta = ultima_resposta_valida(tipo)
    if consulta is None:
        return None
    return {
        "consulta": consulta, "chip": consulta.chip, "em": consulta.iniciada_em, "payload": consulta.payload,
        "caminhos": interp.caminhos_com_exemplo(consulta.payload),
    }


def previa_do_mapeamento(mapeamento) -> dict:
    """Prévia AO VIVO (nada é gravado): o que o mapeamento digitado, ainda não
    salvo, interpretaria nas últimas respostas reais de sessão e de consumo.

        {"erros": [...],                              # mapeamento inválido: só isto
         "sessao": {...} | None, "consumo": {...} | None,   # `interp.previa`; None sem amostra
         "identidade": {...} | None,
         "amostras": {"sessao": {"chip", "em"} | None, "consumo": {...} | None}}

    Serializável em JSON (as datas vão como texto ISO)."""
    amostras = {tipo: amostra_de_resposta(tipo) for tipo in TIPOS_DE_CONSULTA}
    saida = {
        "erros": [], "sessao": None, "consumo": None, "identidade": None,
        "amostras": {
            tipo: ({"chip": a["chip"].nome, "em": a["em"].isoformat()} if a else None) for tipo, a in amostras.items()
        },
    }
    try:
        interp.validar_mapeamento(mapeamento)
    except ValidationError as exc:
        saida["erros"] = list(exc.messages)
        return saida
    if amostras["sessao"]:
        p = interp.previa(amostras["sessao"]["payload"], mapeamento)
        saida["sessao"], saida["identidade"] = p["sessao"], p["identidade"]
    if amostras["consumo"]:
        p = interp.previa(amostras["consumo"]["payload"], mapeamento)
        saida["consumo"] = p["consumo"]
        if saida["identidade"] is None:
            saida["identidade"] = p["identidade"]
    return saida


# ── Limite mensal: o campo do cadastro fala em MB/GB, o banco em bytes ───────

UNIDADES_DE_LIMITE = ("MB", "GB", "TB")


def volume_para_bytes(texto, unidade: str, mapeamento=None) -> int | None:
    """"5" + "GB" -> bytes, com a base (1000 ou 1024) do mapeamento. Aceita vírgula
    ou ponto decimal. Vazio = `None` (sem limite). `ValidationError` (em pt-BR)
    para texto que não é número, número <= 0 ou unidade fora de `UNIDADES_DE_LIMITE`."""
    bruto = str(texto or "").strip().replace(",", ".")
    if not bruto:
        return None
    try:
        valor = Decimal(bruto)
    except InvalidOperation:
        raise ValidationError("Informe um número, por exemplo 5 ou 1,5.") from None
    if not valor.is_finite() or valor <= 0:
        raise ValidationError("O limite deve ser maior que zero.")
    fator = interp.fator_unidade(unidade, base_de_consumo(mapeamento)) if unidade in UNIDADES_DE_LIMITE else None
    if fator is None:
        raise ValidationError("Escolha a unidade do limite (MB, GB ou TB).")
    em_bytes = int((valor * fator).to_integral_value(rounding=ROUND_HALF_UP))
    if em_bytes > 2 ** 62:
        raise ValidationError("O limite informado é grande demais.")
    return em_bytes


def volume_de_bytes(valor, mapeamento=None) -> tuple[str, str]:
    """O inverso de `volume_para_bytes`, para preencher o formulário: `("1,5", "GB")`.
    Usa a maior unidade em que o valor cabe com até 3 casas decimais exatas; se
    nenhuma serve, MB arredondado (até 6 casas: abaixo de 1 MB o número não vira "0",
    que o formulário recusaria na próxima edição). `("", "GB")` quando não há valor."""
    if valor in (None, ""):
        return "", "GB"
    base = base_de_consumo(mapeamento)
    n = int(valor)

    def texto(numero: Decimal, casas: str) -> str:
        return f"{numero.quantize(Decimal(casas), rounding=ROUND_HALF_UP):f}".rstrip("0").rstrip(".").replace(".", ",")

    for unidade in ("TB", "GB", "MB"):
        fator = interp.fator_unidade(unidade, base)
        if n >= fator and (n * 1000) % fator == 0:
            return texto(Decimal(n) / fator, "0.001"), unidade
    mb = interp.fator_unidade("MB", base)
    return (texto(Decimal(n) / mb, "0.001") if n >= mb else texto(Decimal(n) / mb, "0.000001")) or "0", "MB"
