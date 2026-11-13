"""
Veículos e equipamentos da frota: cadastro, status, leituras e cadastros de apoio.

Camadas: views -> este service -> `m2m_monitoramento_service` (leituras de chip, regras de
alerta) -> models. O vínculo chip <-> veículo e as movimentações estão em
`frota_vinculo_service`; a importação de chips, em `frota_import_service`.

Decisões que moldam este módulo:

  • INDEPENDENTE DO ESTOQUE DE TI. Nada aqui importa `Item`/`MovimentacaoItem`. Só
    `Localidade` (unidade/fazenda) e `Usuario` (responsável) são reaproveitados.
  • STATUS OPERACIONAL ≠ CONECTIVIDADE. `FrotaVeiculo.status` (ativo, manutenção, inativo,
    desativado) é decisão de quem opera a frota; a conectividade é o que a API respondeu
    sobre os chips. NENHUMA função daqui deriva uma da outra, nos dois sentidos: mudar o
    status não mexe em chip, e uma sessão caída não muda o status do veículo.
  • O PADRÃO DE "EXIGE CONECTIVIDADE" é do TIPO; o veículo pode sobrepor (sim/não). Sem
    sobreposição vale o do tipo (`exige_conectividade`).
  • TUDO O QUE MUDA É AUDITADO em `FrotaEvento` (cadastro, edição com de/para, status) — e
    os cadastros de apoio (tipos, operadoras, regras de alerta) em `M2MEvento(config_frota)`.
  • DESATIVAR VEÍCULO COM CHIP VINCULADO É RECUSADO: veículo `desativado` não recebe chip,
    então deixar um chip preso nele criaria um estado que o sistema não permite criar.

Contratos de leitura para as telas (Fase E): `listar_veiculos`, `anotar_veiculos`,
`conectividade_do_veiculo`, `linha_do_tempo_veiculo`, `indicadores_frota`,
`area_de_atencao`, `buscar_veiculos` (em `m2m_monitoramento_service`) e as listas de apoio
(`listar_tipos`, `listar_operadoras`, `listar_regras`). Cada docstring diz o que devolve.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.contrib.auth.models import User
from django.db.models import (
    BooleanField, Case, CharField, Count, Exists, F, IntegerField, OuterRef, Prefetch, Q, Subquery, Value, When,
)
from django.db.models.functions import Coalesce
from django.utils import timezone

from ProjetoEstoque.models import (
    REGRAS_DE_ALERTA_PADRAO,
    ConectividadeOperadora,
    FrotaEvento,
    FrotaMovimentacao,
    FrotaRegraAlerta,
    FrotaTipo,
    FrotaVeiculo,
    FrotaVinculo,
    Localidade,
    M2MChip,
    M2MEvento,
    M2MIntegracao,
    M2MOcorrencia,
    TecInstalacao,
    Usuario,
)
from services import m2m_monitoramento_service as m2m

logger = logging.getLogger(__name__)

Status = FrotaVeiculo.Status
ANO_MINIMO = 1950
INTEGRACAO_TIM = ConectividadeOperadora.Integracao.TIM_M2M

_PLACA_ANTIGA = re.compile(r"^[A-Z]{3}\d{4}$")
_PLACA_MERCOSUL = re.compile(r"^[A-Z]{3}\d[A-Z]\d{2}$")


# ═════════════════════════════════════════════════════════════
# Normalização
# ═════════════════════════════════════════════════════════════

def normalizar_codigo(valor) -> str:
    """Código interno do veículo: aparado, em CAIXA ALTA e com os espaços internos
    colapsados. `ValidationError` se vazio ou maior que 30."""
    codigo = " ".join(str(valor or "").split()).upper()
    if not codigo:
        raise ValidationError("Informe o código do veículo.")
    if len(codigo) > 30:
        raise ValidationError("O código aceita até 30 caracteres.")
    return codigo


def normalizar_placa(valor) -> str:
    """Placa em caixa alta, sem hífen nem espaços, no formato antigo (AAA9999) ou
    Mercosul (AAA9A99). Vazio devolve `""` (a placa é opcional: trator e implemento
    nem sempre têm). `ValidationError` para qualquer outro formato."""
    placa = re.sub(r"[\s\-]", "", str(valor or "")).upper()
    if not placa:
        return ""
    if not (_PLACA_ANTIGA.match(placa) or _PLACA_MERCOSUL.match(placa)):
        raise ValidationError("A placa deve estar no formato AAA9999 ou AAA9A99 (Mercosul).")
    return placa


def normalizar_renavam(valor) -> str:
    """RENAVAM só com dígitos, exatamente 11. Vazio devolve `""` (opcional)."""
    bruto = re.sub(r"[\s.\-]", "", str(valor or ""))
    if not bruto:
        return ""
    if not bruto.isdigit() or len(bruto) != 11:
        raise ValidationError("O RENAVAM deve ter 11 dígitos.")
    return bruto


def normalizar_chassi(valor) -> str:
    """Chassi ou número de série: caixa alta, sem espaços, até 30 caracteres. Vazio é válido."""
    chassi = re.sub(r"\s", "", str(valor or "")).upper()
    if len(chassi) > 30:
        raise ValidationError("O chassi / nº de série aceita até 30 caracteres.")
    return chassi


def _bool_ou_none(valor):
    """Sim/Não de formulário -> True/False; vazio -> None (herda do tipo)."""
    if valor is None or valor == "":
        return None
    if isinstance(valor, bool):
        return valor
    texto = str(valor).strip().lower()
    if texto in ("1", "true", "sim", "s", "on", "yes"):
        return True
    if texto in ("0", "false", "nao", "não", "n", "off", "no"):
        return False
    raise ValueError(valor)


def _bool(valor, padrao: bool = False) -> bool:
    resultado = _bool_ou_none(valor)
    return padrao if resultado is None else resultado


def _usuario_ou_none(usuario):
    return usuario if getattr(usuario, "pk", None) else None


def _json(valor):
    if valor is None or isinstance(valor, (str, int, float, bool)):
        return valor
    return str(valor)


# ═════════════════════════════════════════════════════════════
# Veículos: cadastro
# ═════════════════════════════════════════════════════════════

CAMPOS_DO_VEICULO = (
    "codigo", "nome", "tipo", "fabricante", "modelo", "ano_fabricacao", "placa", "renavam", "chassi_serie",
    "localidade", "setor", "area_operacao", "responsavel", "observacoes", "exige_conectividade",
)
_PADROES_VEICULO = dict(
    codigo="", nome="", tipo=None, fabricante="", modelo="", ano_fabricacao=None, placa="", renavam="",
    chassi_serie="", localidade=None, setor="", area_operacao="", responsavel=None, observacoes="",
    exige_conectividade=None,
)
_LIMITES_DE_TEXTO = {
    "nome": (120, "o nome", True), "fabricante": (80, "o fabricante", False), "modelo": (80, "o modelo", False),
    "setor": (100, "o setor", False), "area_operacao": (100, "a área de operação", False),
}


def _entidade(valor, modelo, rotulo: str, campo: str, erros: dict):
    if valor in (None, "", 0, "0"):
        return None
    if isinstance(valor, modelo):
        return valor
    try:
        return modelo.objects.get(pk=int(valor))
    except (ValueError, TypeError, modelo.DoesNotExist):
        erros[campo] = f"{rotulo} não encontrado(a)."
        return None


def _limpar_dados_veiculo(dados: dict, atual: dict) -> dict:
    """Valida e normaliza os campos do veículo. `atual` traz os valores vigentes (na
    criação, os padrões); só as chaves presentes em `dados` mudam. `ValidationError({campo: msg})`."""
    erros: dict[str, str] = {}
    v = dict(atual)
    if "codigo" in dados:
        try:
            v["codigo"] = normalizar_codigo(dados["codigo"])
        except ValidationError as exc:
            erros["codigo"] = exc.messages[0]
    for campo, (maximo, rotulo, obrigatorio) in _LIMITES_DE_TEXTO.items():
        if campo in dados:
            texto = " ".join(str(dados[campo] if dados[campo] is not None else "").split())
            if obrigatorio and not texto:
                erros[campo] = f"Informe {rotulo}."
            elif len(texto) > maximo:
                erros[campo] = f"{rotulo[:1].upper() + rotulo[1:]} aceita até {maximo} caracteres."
            v[campo] = texto
    for campo, funcao in (("placa", normalizar_placa), ("renavam", normalizar_renavam), ("chassi_serie", normalizar_chassi)):
        if campo in dados:
            try:
                v[campo] = funcao(dados[campo])
            except ValidationError as exc:
                erros[campo] = exc.messages[0]
    if "ano_fabricacao" in dados:
        bruto = dados["ano_fabricacao"]
        if bruto in (None, ""):
            v["ano_fabricacao"] = None
        else:
            maximo = timezone.localdate().year + 1
            try:
                ano = int(str(bruto).strip())
                if not ANO_MINIMO <= ano <= maximo:
                    raise ValueError
                v["ano_fabricacao"] = ano
            except ValueError:
                erros["ano_fabricacao"] = f"O ano de fabricação deve estar entre {ANO_MINIMO} e {maximo}."
    if "tipo" in dados:
        tipo = _entidade(dados["tipo"], FrotaTipo, "Tipo", "tipo", erros)
        if tipo is None:
            erros.setdefault("tipo", "Escolha o tipo do veículo.")
        elif not tipo.ativo and getattr(atual.get("tipo"), "pk", None) != tipo.pk:
            erros["tipo"] = f"O tipo {tipo.nome} está inativo."
        else:
            v["tipo"] = tipo
    elif v.get("tipo") is None:
        erros["tipo"] = "Escolha o tipo do veículo."
    if "localidade" in dados:
        v["localidade"] = _entidade(dados["localidade"], Localidade, "Unidade", "localidade", erros)
    if "responsavel" in dados:
        v["responsavel"] = _entidade(dados["responsavel"], Usuario, "Responsável", "responsavel", erros)
    if "observacoes" in dados:
        v["observacoes"] = (dados["observacoes"] or "").strip()
    if "exige_conectividade" in dados:
        try:
            v["exige_conectividade"] = _bool_ou_none(dados["exige_conectividade"])
        except ValueError:
            erros["exige_conectividade"] = "Escolha sim, não ou deixe vazio para herdar do tipo."
    if not v.get("codigo") and "codigo" not in erros:
        erros["codigo"] = "Informe o código do veículo."
    if not v.get("nome") and "nome" not in erros:
        erros["nome"] = "Informe o nome."
    if erros:
        raise ValidationError(erros)
    return v


def _conferir_unicidade_veiculo(valores: dict, excluir_pk=None) -> None:
    outros = FrotaVeiculo.objects.exclude(pk=excluir_pk) if excluir_pk else FrotaVeiculo.objects.all()
    erros: dict[str, str] = {}
    if outros.filter(codigo__iexact=valores["codigo"]).exists():
        erros["codigo"] = "Já existe um veículo com este código."
    if valores.get("placa") and outros.filter(placa=valores["placa"]).exists():
        erros["placa"] = "Já existe um veículo com esta placa."
    if valores.get("chassi_serie") and outros.filter(chassi_serie=valores["chassi_serie"]).exists():
        erros["chassi_serie"] = "Já existe um veículo com este chassi / nº de série."
    if erros:
        raise ValidationError(erros)


# As duas fazendas da operação, na ordem em que o formulário as oferece: `(código da sede em
# LocalidadeChoices, nome da unidade)`.
FAZENDAS = (("Karitel", "Fazenda Karitel"), ("Rio_do_Meio", "Fazenda Rio do Meio"))


def fazendas() -> list[Localidade]:
    """As unidades que o cadastro de VEÍCULO oferece: Fazenda Karitel e Fazenda Rio do Meio.

    O cadastro de `Localidade` é o do estoque de TI inteiro (setores, alojamentos, salas…) e não
    tem as fazendas como linhas próprias; sem elas, o veículo não teria o que escolher. Cada uma é
    procurada pelo nome (sem distinguir caixa) e, se ainda não existe, criada com o código da sede
    — é idempotente, e uma linha que já exista com esse nome é reaproveitada como está.

    Só a TELA de veículo restringe a escolha a estas duas (a view confere o valor recebido); este
    service continua aceitando qualquer `Localidade` em `criar_veiculo`/`editar_veiculo`, e é por
    isso que a importação de chips (que valida a unidade do CHIP pelo nome) não é afetada."""
    unidades = []
    for codigo, nome in FAZENDAS:
        unidade = Localidade.objects.filter(local__iexact=nome).order_by("pk").first()
        if unidade is None:
            unidade = Localidade.objects.create(codigo=codigo, local=nome)
        unidades.append(unidade)
    return unidades


def criar_veiculo(dados: dict, usuario=None) -> FrotaVeiculo:
    """Cadastra um veículo/equipamento. `dados`: codigo, nome, tipo, fabricante, modelo,
    ano_fabricacao, placa, renavam, chassi_serie, localidade, setor, area_operacao,
    responsavel, observacoes, exige_conectividade (`tipo`/`localidade`/`responsavel` aceitam
    instância ou id; `exige_conectividade` vazio = herda do tipo) e, opcional, `status`
    (padrão `ativo`). Normaliza (código e placa em caixa alta, placa sem hífen) e valida (placa
    AAA9999/AAA9A99, RENAVAM de 11 dígitos, ano entre 1950 e o ano que vem); código, placa
    e chassi/série são ÚNICOS (placa e chassi só quando preenchidos). Placa, RENAVAM e
    chassi são opcionais. Grava `FrotaEvento(cadastro)`. Levanta
    `ValidationError({campo: mensagem})`."""
    dados = dict(dados)
    status = dados.pop("status", Status.ATIVO) or Status.ATIVO
    erros_status = {}
    if status not in Status.values:
        erros_status["status"] = "Escolha um status válido."
    try:
        valores = _limpar_dados_veiculo(dados, _PADROES_VEICULO)
        _conferir_unicidade_veiculo(valores)
    except ValidationError as exc:
        erros = dict(exc.message_dict) if hasattr(exc, "error_dict") else {"__all__": exc.messages}
        raise ValidationError({**erros, **erros_status}) from None
    if erros_status:
        raise ValidationError(erros_status)
    try:
        with transaction.atomic():
            veiculo = FrotaVeiculo(
                **valores, status=status, criado_por=_usuario_ou_none(usuario), atualizado_por=_usuario_ou_none(usuario),
            )
            veiculo.save()
            FrotaEvento.objects.create(
                veiculo=veiculo, tipo=FrotaEvento.Tipo.CADASTRO, usuario=_usuario_ou_none(usuario),
                dados={
                    "codigo": veiculo.codigo, "nome": veiculo.nome, "tipo": veiculo.tipo.nome, "status": veiculo.status,
                },
            )
    except IntegrityError:
        raise ValidationError("Já existe um veículo com este código, placa ou chassi / nº de série.") from None
    return veiculo


def editar_veiculo(veiculo: FrotaVeiculo, dados: dict, usuario=None) -> FrotaVeiculo:
    """Altera o cadastro. Só as chaves presentes em `dados` mudam; se nada mudou de fato,
    não grava nada. Grava `FrotaEvento(edicao)` com o de/para dos campos alterados. O STATUS
    não se edita aqui: `mudar_status`. As mesmas validações e unicidades de `criar_veiculo`.
    Levanta `ValidationError({campo: mensagem})`."""
    dados = {k: v for k, v in dict(dados).items() if k != "status"}
    atuais = {campo: getattr(veiculo, campo) for campo in CAMPOS_DO_VEICULO}
    valores = _limpar_dados_veiculo(dados, atuais)
    _conferir_unicidade_veiculo(valores, excluir_pk=veiculo.pk)
    alteracoes = {
        campo: {"de": _json(atuais[campo]), "para": _json(valores[campo])}
        for campo in CAMPOS_DO_VEICULO if valores[campo] != atuais[campo]
    }
    if not alteracoes:
        return veiculo
    for campo in alteracoes:
        setattr(veiculo, campo, valores[campo])
    veiculo.atualizado_por = _usuario_ou_none(usuario)
    try:
        with transaction.atomic():
            veiculo.save(update_fields=[*alteracoes, "atualizado_por", "updated_at"])
            FrotaEvento.objects.create(
                veiculo=veiculo, tipo=FrotaEvento.Tipo.EDICAO, usuario=_usuario_ou_none(usuario),
                dados={"alteracoes": alteracoes},
            )
    except IntegrityError:
        raise ValidationError("Já existe um veículo com este código, placa ou chassi / nº de série.") from None
    return veiculo


def mudar_status(veiculo: FrotaVeiculo, novo_status: str, usuario=None, motivo: str = "") -> FrotaVeiculo:
    """Muda o status OPERACIONAL (`ativo`, `manutencao`, `inativo`, `desativado`) e grava
    `FrotaEvento(status)` com de/para e o motivo (opcional, até 255). NÃO toca em chip nem em
    conectividade — e a conectividade nunca muda o status. Recusa: status inválido; o mesmo
    status; `desativado` enquanto o veículo tem chip vinculado OU equipamento tecnológico instalado
    (desvincule, transfira ou retire antes — veículo desativado não recebe nenhum dos dois, e deixar
    um preso nele criaria um estado que o sistema não permite). Levanta `ValidationError`."""
    if novo_status not in Status.values:
        raise ValidationError({"status": "Escolha um status válido."})
    motivo = (motivo or "").strip()
    if len(motivo) > 255:
        raise ValidationError({"motivo": "O motivo aceita até 255 caracteres."})
    with transaction.atomic():
        atual = FrotaVeiculo.objects.select_for_update().get(pk=veiculo.pk)
        if atual.status == novo_status:
            raise ValidationError({"status": f"O veículo já está {atual.get_status_display().lower()}."})
        if novo_status == Status.DESATIVADO:
            n = FrotaVinculo.objects.filter(veiculo=atual, fim_em__isnull=True).count()
            if n:
                raise ValidationError({
                    "status": (
                        f"O veículo tem {n} chip{'s' if n != 1 else ''} vinculado{'s' if n != 1 else ''}: "
                        "desvincule ou transfira antes de desativá-lo."
                    ),
                })
            n = TecInstalacao.objects.filter(veiculo=atual, fim_em__isnull=True).count()
            if n:
                raise ValidationError({
                    "status": (
                        f"O veículo tem {n} equipamento{'s' if n != 1 else ''} tecnológico{'s' if n != 1 else ''} "
                        f"instalado{'s' if n != 1 else ''}: retire ou transfira antes de desativá-lo."
                    ),
                })
        anterior = atual.status
        atual.status = novo_status
        atual.atualizado_por = _usuario_ou_none(usuario)
        atual.save(update_fields=["status", "atualizado_por", "updated_at"])
        FrotaEvento.objects.create(
            veiculo=atual, tipo=FrotaEvento.Tipo.STATUS, usuario=_usuario_ou_none(usuario),
            dados={"de": anterior, "para": novo_status, "motivo": motivo},
        )
    veiculo.status = novo_status
    veiculo.atualizado_por = atual.atualizado_por
    veiculo.updated_at = atual.updated_at
    return veiculo


def exige_conectividade(veiculo: FrotaVeiculo) -> bool:
    """O veículo precisa de chip? O sim/não do PRÓPRIO veículo, se preenchido; senão o
    padrão do tipo (`FrotaTipo.exige_conectividade`)."""
    return veiculo.exige_conectividade_efetiva


# ═════════════════════════════════════════════════════════════
# Classificação de conectividade (chip e veículo)
#
# Um chip CONSULTÁVEL (monitorado + operadora com integração) cai em UM estado, na
# mesma ordem de `M2MChip.situacao_monitoramento_com`: falha (a última tentativa falhou)
# > pendente (nunca consultado) > desatualizado (dado velho demais) > o que a sessão
# diz (ativa / sem sessão / não interpretado). Chip não consultável é `sem_integracao`
# (operadora sem API) ou `desativado` (monitoramento desligado).
#
# O VEÍCULO resume os chips por precedência explícita: sem chip > sessão ativa
# confirmada (>= 1 chip) > sem sessão (TODOS os chips) > falha de consulta > pendente >
# desatualizado > não interpretado > sem integração > desativado.
# ═════════════════════════════════════════════════════════════

ROTULO_CHIP = {
    "ativa": "Sessão ativa", "sem_sessao": "Sem sessão", "falha": "Falha de consulta",
    "pendente": "Pendente (ainda não consultado)", "desatualizado": "Desatualizado",
    "nao_interpretado": "Não interpretado", "sem_integracao": "Sem integração (não consultado pela API)",
    "desativado": "Monitoramento desativado",
}
ROTULO_VEICULO = {"sem_chip": "Sem chip", **ROTULO_CHIP}
ESTADOS_DE_CONECTIVIDADE = tuple(ROTULO_VEICULO)
_PRECEDENCIA = (
    "sem_chip", "ativa", "sem_sessao", "falha", "pendente", "desatualizado", "nao_interpretado",
    "sem_integracao", "desativado",
)


def estado_de_conectividade_do_chip(chip: M2MChip, horas_desatualizado: int, agora=None) -> str:
    """O estado do chip, um de `ROTULO_CHIP`. Em lista, carregue `operadora` com
    `select_related`."""
    agora = agora or timezone.now()
    if not chip.monitorado:
        return "desativado"
    if not chip.consultavel_pela_api:
        return "sem_integracao"
    if chip.falhas_consecutivas > 0:
        return "falha"
    if chip.ultima_tentativa_em is None:
        return "pendente"
    referencia = chip.ultima_valida_em or chip.created_at
    if referencia is not None and agora - referencia > timedelta(hours=horas_desatualizado):
        return "desatualizado"
    if chip.sessao_estado == M2MChip.SessaoEstado.ATIVA:
        return "ativa"
    if chip.sessao_estado == M2MChip.SessaoEstado.SEM_SESSAO:
        return "sem_sessao"
    return "nao_interpretado"


def estado_do_veiculo(estados_dos_chips: list[str]) -> str:
    """Resume os estados dos chips ativos do veículo pela precedência acima."""
    if not estados_dos_chips:
        return "sem_chip"
    if "ativa" in estados_dos_chips:
        return "ativa"
    if all(e == "sem_sessao" for e in estados_dos_chips):
        return "sem_sessao"
    for estado in _PRECEDENCIA[3:]:
        if estado in estados_dos_chips:
            return estado
    return "desativado"  # pragma: no cover — os estados são fechados; nunca chega aqui


def _q_estado_do_chip(estado: str, limite, prefixo: str = "") -> Q:
    """O mesmo critério de `estado_de_conectividade_do_chip`, em SQL. `prefixo` é o
    caminho até o chip (`"chip__"` a partir de `FrotaVinculo`; vazio a partir de `M2MChip`).
    `limite` = agora - horas_desatualizado."""
    def c(**kw):
        return Q(**{prefixo + k: v for k, v in kw.items()})

    consultavel = c(monitorado=True, operadora__integracao=INTEGRACAO_TIM)
    sem_falha = consultavel & c(falhas_consecutivas=0)
    tentado = sem_falha & c(ultima_tentativa_em__isnull=False)
    velho = c(ultima_valida_em__lt=limite) | (c(ultima_valida_em__isnull=True) & c(created_at__lt=limite))
    fresco = c(ultima_valida_em__gte=limite) | (c(ultima_valida_em__isnull=True) & c(created_at__gte=limite))
    if estado == "desativado":
        return c(monitorado=False)
    if estado == "sem_integracao":
        return c(monitorado=True) & ~c(operadora__integracao=INTEGRACAO_TIM)
    if estado == "falha":
        return consultavel & c(falhas_consecutivas__gt=0)
    if estado == "pendente":
        return sem_falha & c(ultima_tentativa_em__isnull=True)
    if estado == "desatualizado":
        return tentado & velho
    if estado == "ativa":
        return tentado & fresco & c(sessao_estado="ativa")
    if estado == "sem_sessao":
        return tentado & fresco & c(sessao_estado="sem_sessao")
    if estado == "nao_interpretado":
        return tentado & fresco & c(sessao_estado__in=["nao_interpretado", "nao_consultado"])
    raise ValueError(estado)


def conectividade_do_veiculo(veiculo: FrotaVeiculo, cfg: M2MIntegracao | None = None, agora=None) -> dict:
    """A conectividade do veículo, SEM nunca derivar daí o status operacional. Devolve:

        {"estado": um de `ESTADOS_DE_CONECTIVIDADE`, "rotulo": "Sessão ativa" | "Sem chip" | ...,
         "chips": [{"chip", "vinculo", "estado", "rotulo", "desde" (início do vínculo),
                    "vinculado_por" (login), "sessao_estado", "sessao_rotulo",
                    "ultima_valida_em", "ultima_falha_em", "falhas_consecutivas", "frescor"}],
         "contagem": {"total": N, "por_estado": {estado: n}, "ativa": n_com_sessao_ativa,
                      "texto": "1 de 2 com sessão ativa" — vazio com 0 ou 1 chip}}

    Precedência (explícita): sem chip > sessão ativa confirmada (>= 1) > sem sessão (TODOS os
    chips interpretados como sem sessão) > falha de consulta > pendente > desatualizado > não
    interpretado > sem integração > monitoramento desativado. Chip com falha de consulta NÃO
    conta como "ativa" nem "sem sessão": o estado dele seria o do último dado válido, velho.

    Usa `veiculo.vinculos_ativos` quando a lista veio de `listar_veiculos`/`anotar_veiculos`
    (sem consulta por veículo); senão lê do banco."""
    cfg = cfg or M2MIntegracao.get()
    agora = agora or timezone.now()
    horas = cfg.horas_desatualizado
    vinculos = getattr(veiculo, "vinculos_ativos", None)
    if vinculos is None:
        vinculos = list(
            FrotaVinculo.objects.filter(veiculo=veiculo, fim_em__isnull=True)
            .select_related("chip", "chip__operadora", "chip__sessao_consulta", "inicio_por")
            .defer("chip__sessao_consulta__payload")
            .order_by("inicio_em", "pk")
        )
    itens = []
    for vinculo in vinculos:
        chip = vinculo.chip
        estado = estado_de_conectividade_do_chip(chip, horas, agora)
        itens.append({
            "chip": chip, "vinculo": vinculo, "estado": estado, "rotulo": ROTULO_CHIP[estado],
            "desde": vinculo.inicio_em,
            "vinculado_por": vinculo.inicio_por.get_username() if vinculo.inicio_por_id else "",
            "sessao_estado": chip.sessao_estado, "sessao_rotulo": chip.get_sessao_estado_display(),
            "ultima_valida_em": chip.ultima_valida_em, "ultima_falha_em": chip.ultima_falha_em,
            "falhas_consecutivas": chip.falhas_consecutivas,
            "frescor": m2m.frescor(
                chip.sessao_consulta.iniciada_em if chip.sessao_consulta_id else None, horas, agora,
            ),
        })
    por_estado: dict[str, int] = {}
    for item in itens:
        por_estado[item["estado"]] = por_estado.get(item["estado"], 0) + 1
    estado = estado_do_veiculo([i["estado"] for i in itens])
    ativa = por_estado.get("ativa", 0)
    return {
        "estado": estado, "rotulo": ROTULO_VEICULO[estado], "chips": itens,
        "contagem": {
            "total": len(itens), "por_estado": por_estado, "ativa": ativa,
            "texto": f"{ativa} de {len(itens)} com sessão ativa" if len(itens) > 1 else "",
        },
    }


# ═════════════════════════════════════════════════════════════
# Leituras
# ═════════════════════════════════════════════════════════════

ORDENS_DE_VEICULOS = ("atencao", "codigo", "nome", "tipo")


def _prefetch_de_vinculos_ativos() -> Prefetch:
    return Prefetch(
        "vinculos",
        queryset=(
            FrotaVinculo.objects.filter(fim_em__isnull=True)
            .select_related("chip", "chip__operadora", "chip__sessao_consulta", "inicio_por")
            .defer("chip__sessao_consulta__payload")
            .order_by("inicio_em", "pk")
        ),
        to_attr="vinculos_ativos",
    )


def listar_veiculos(filtros: dict | None = None, agora=None):
    """QuerySet de veículos, filtrado e ordenado, com `tipo`, `localidade` e `responsavel`
    já carregados e os VÍNCULOS ATIVOS (com chip, operadora e consulta de sessão, sem o
    payload) em `veiculo.vinculos_ativos` — sem N+1: a lista toda custa uma consulta para
    veículos e uma para os vínculos. `filtros` (todos opcionais; vazio ou inválido é ignorado):

        q            código, nome, modelo, fabricante, placa (sem hífen), chassi/série e, dos chips
                     ATIVOS do veículo, nome, ICCID, IMSI e número da linha
        tipo         id do tipo;   localidade  id da unidade;   status  um de `FrotaVeiculo.Status`
        chip         `com` | `sem`  (tem ou não chip vinculado)
        conectividade  um de `ESTADOS_DE_CONECTIVIDADE` (o resumo do veículo, ver `conectividade_do_veiculo`)
        operadora    id da operadora de algum chip ativo
        de, ate      `AAAA-MM-DD`: última CONSULTA (tentativa) de algum chip ativo dentro do período, ate inclusive
        ordem        `atencao` (padrão: quem precisa de atenção primeiro) | `codigo` | `nome` | `tipo`

    Cada veículo sai com `n_chips`, `estado_conectividade` e `exige_efetivo` anotados. Para
    o dict completo de conectividade use `anotar_veiculos`."""
    filtros = filtros or {}
    agora = agora or timezone.now()
    cfg = M2MIntegracao.get()
    limite = agora - timedelta(hours=cfg.horas_desatualizado)

    ativos = FrotaVinculo.objects.filter(veiculo=OuterRef("pk"), fim_em__isnull=True)

    def algum(q: Q):
        return Exists(ativos.filter(q))

    n_chips = Coalesce(
        Subquery(ativos.order_by().values("veiculo").annotate(n=Count("pk")).values("n")[:1]), 0,
    )
    nenhum_fora = ~Exists(ativos.exclude(_q_estado_do_chip("sem_sessao", limite, "chip__")))
    estado = Case(
        When(n_chips=0, then=Value("sem_chip")),
        When(algum_ativa=True, then=Value("ativa")),
        When(todas_sem_sessao=True, then=Value("sem_sessao")),
        When(algum_falha=True, then=Value("falha")),
        When(algum_pendente=True, then=Value("pendente")),
        When(algum_desatualizado=True, then=Value("desatualizado")),
        When(algum_nao_interpretado=True, then=Value("nao_interpretado")),
        When(algum_sem_integracao=True, then=Value("sem_integracao")),
        default=Value("desativado"),
        output_field=CharField(),
    )
    qs = (
        FrotaVeiculo.objects.select_related("tipo", "localidade", "responsavel")
        .prefetch_related(_prefetch_de_vinculos_ativos())
        .annotate(
            n_chips=n_chips,
            exige_efetivo=Case(
                When(exige_conectividade__isnull=False, then=F("exige_conectividade")),
                default=F("tipo__exige_conectividade"), output_field=BooleanField(),
            ),
            algum_ativa=algum(_q_estado_do_chip("ativa", limite, "chip__")),
            todas_sem_sessao=nenhum_fora,
            algum_falha=algum(_q_estado_do_chip("falha", limite, "chip__")),
            algum_pendente=algum(_q_estado_do_chip("pendente", limite, "chip__")),
            algum_desatualizado=algum(_q_estado_do_chip("desatualizado", limite, "chip__")),
            algum_nao_interpretado=algum(_q_estado_do_chip("nao_interpretado", limite, "chip__")),
            algum_sem_integracao=algum(_q_estado_do_chip("sem_integracao", limite, "chip__")),
        )
        .annotate(estado_conectividade=estado)
    )

    q = (filtros.get("q") or "").strip()
    if q:
        sem_hifen = q.replace("-", "").replace(" ", "")
        condicao = (
            Q(codigo__icontains=q) | Q(nome__icontains=q) | Q(modelo__icontains=q) | Q(fabricante__icontains=q)
            | Q(placa__icontains=sem_hifen) | Q(chassi_serie__icontains=q) | algum(Q(chip__nome__icontains=q))
        )
        digitos = re.sub(r"\D", "", q)
        if len(digitos) >= 3:
            condicao |= algum(
                Q(chip__iccid__contains=digitos) | Q(chip__imsi__contains=digitos) | Q(chip__numero_linha__contains=digitos)
            )
        qs = qs.filter(condicao)
    if str(filtros.get("tipo") or "").isdigit():
        qs = qs.filter(tipo_id=int(filtros["tipo"]))
    if str(filtros.get("localidade") or "").isdigit():
        qs = qs.filter(localidade_id=int(filtros["localidade"]))
    if filtros.get("status") in Status.values:
        qs = qs.filter(status=filtros["status"])
    if filtros.get("chip") == "com":
        qs = qs.filter(n_chips__gt=0)
    elif filtros.get("chip") == "sem":
        qs = qs.filter(n_chips=0)
    if filtros.get("conectividade") in ESTADOS_DE_CONECTIVIDADE:
        qs = qs.filter(estado_conectividade=filtros["conectividade"])
    if str(filtros.get("operadora") or "").isdigit():
        qs = qs.filter(algum(Q(chip__operadora_id=int(filtros["operadora"]))))
    inicio, fim = m2m._intervalo_de_datas(m2m.interpretar_data(filtros.get("de")), m2m.interpretar_data(filtros.get("ate")))
    if inicio:
        qs = qs.filter(algum(Q(chip__ultima_tentativa_em__gte=inicio)))
    if fim:
        qs = qs.filter(algum(Q(chip__ultima_tentativa_em__lt=fim)))

    ordem = filtros.get("ordem") if filtros.get("ordem") in ORDENS_DE_VEICULOS else "atencao"
    if ordem == "codigo":
        return qs.order_by("codigo", "pk")
    if ordem == "nome":
        return qs.order_by("nome", "pk")
    if ordem == "tipo":
        return qs.order_by("tipo__nome", "codigo", "pk")
    prioridade = Case(
        When(status=Status.DESATIVADO, then=Value(8)),
        When(status=Status.INATIVO, then=Value(7)),
        When(status=Status.ATIVO, exige_efetivo=True, n_chips=0, then=Value(0)),      # exige chip e não tem
        When(estado_conectividade="falha", then=Value(1)),
        When(estado_conectividade="desatualizado", then=Value(2)),
        When(estado_conectividade="pendente", then=Value(3)),
        When(estado_conectividade="sem_sessao", then=Value(4)),
        When(n_chips=0, then=Value(6)),
        default=Value(5),
        output_field=IntegerField(),
    )
    return qs.annotate(prioridade=prioridade).order_by("prioridade", "codigo", "pk")


def anotar_veiculos(veiculos, cfg: M2MIntegracao | None = None, agora=None) -> list:
    """Acrescenta a cada veículo da lista `veiculo.conectividade` (o dict de
    `conectividade_do_veiculo`) lendo a configuração UMA vez e usando os vínculos já
    carregados por `listar_veiculos` (nenhuma consulta por linha). Devolve a lista (mesmos
    objetos)."""
    cfg = cfg or M2MIntegracao.get()
    agora = agora or timezone.now()
    lista = list(veiculos)
    for veiculo in lista:
        veiculo.conectividade = conectividade_do_veiculo(veiculo, cfg, agora)
    return lista


def _resumo_do_evento_de_veiculo(e: FrotaEvento) -> str:
    d = e.dados or {}
    if e.tipo == FrotaEvento.Tipo.CADASTRO:
        return f"Cadastrado como {d.get('codigo', '')} · {d.get('nome', '')}."
    if e.tipo == FrotaEvento.Tipo.EDICAO:
        return "Campos alterados: " + ", ".join(sorted((d.get("alteracoes") or {}).keys())) + "."
    if e.tipo == FrotaEvento.Tipo.STATUS:
        rotulos = dict(Status.choices)
        texto = f"De {rotulos.get(d.get('de'), d.get('de') or '—')} para {rotulos.get(d.get('para'), d.get('para') or '—')}."
        return f"{texto} Motivo: {d['motivo']}" if d.get("motivo") else texto
    return ""


def linha_do_tempo_veiculo(veiculo: FrotaVeiculo, tipos=None, limite: int = 100) -> list[dict]:
    """Histórico do veículo, do MAIS NOVO para o mais antigo, misturando as quatro
    fontes. `tipos`: subconjunto de {"evento", "vinculo", "movimentacao", "ocorrencia"} (None =
    todas). Cada item:

        {"quando": datetime, "fonte": "evento"|"vinculo"|"movimentacao"|"ocorrencia", "tipo": slug,
         "titulo": str, "detalhe": str, "nivel": "ok"|"info"|"aviso"|"erro", "ref_id": int}

    `evento` é o cadastro do veículo (criação, edição, status); `vinculo` são os PERÍODOS
    (início e fim de cada chip, lidos de `FrotaVinculo`); `movimentacao` são as AÇÕES com
    motivo e responsável (lidas de `FrotaMovimentacao`, como origem ou destino); `ocorrencia`
    são os alertas do veículo. Vínculo e movimentação descrevem o mesmo fato por ângulos
    diferentes — a tela escolhe qual mostrar. Nada é duplicado no banco."""
    fontes = set(tipos) if tipos else {"evento", "vinculo", "movimentacao", "ocorrencia"}
    itens: list[dict] = []
    if "evento" in fontes:
        for e in veiculo.eventos.select_related("usuario").order_by("-em", "-pk")[:limite]:
            itens.append({
                "quando": e.em, "fonte": "evento", "tipo": e.tipo, "titulo": e.get_tipo_display(),
                "detalhe": _resumo_do_evento_de_veiculo(e), "nivel": "info", "ref_id": e.pk,
            })
    if "vinculo" in fontes:
        for v in veiculo.vinculos.select_related("chip", "inicio_por", "fim_por").order_by("-inicio_em", "-pk")[:limite]:
            itens.append({
                "quando": v.inicio_em, "fonte": "vinculo", "tipo": "inicio",
                "titulo": f"Chip {v.chip.nome} vinculado", "detalhe": f"Por {v.inicio_por.get_username()}." if v.inicio_por_id else "",
                "nivel": "ok", "ref_id": v.pk,
            })
            if v.fim_em:
                itens.append({
                    "quando": v.fim_em, "fonte": "vinculo", "tipo": "fim",
                    "titulo": f"Chip {v.chip.nome} desvinculado",
                    "detalhe": (f"Por {v.fim_por.get_username()}. " if v.fim_por_id else "") + (f"Motivo: {v.motivo_fim}" if v.motivo_fim else ""),
                    "nivel": "info", "ref_id": v.pk,
                })
    if "movimentacao" in fontes:
        movimentacoes = (
            FrotaMovimentacao.objects.filter(Q(veiculo_origem=veiculo) | Q(veiculo_destino=veiculo))
            .select_related("chip", "chip_relacionado", "veiculo_origem", "veiculo_destino")
            .order_by("-em", "-pk")[:limite]
        )
        for m in movimentacoes:
            itens.append({
                "quando": m.em, "fonte": "movimentacao", "tipo": m.tipo,
                "titulo": f"{m.get_tipo_display()}: chip {m.chip.nome}",
                "detalhe": f"Por {m.usuario_nome}. Motivo: {m.motivo}" if m.motivo else f"Por {m.usuario_nome}.",
                "nivel": "info", "ref_id": m.pk,
            })
    if "ocorrencia" in fontes:
        for o in veiculo.ocorrencias.order_by("-detectada_em", "-pk")[:limite]:
            nivel = "erro" if o.nivel == "critico" else ("info" if o.nivel == "info" else "aviso")
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


def indicadores_frota(agora=None) -> dict:
    """Os KPIs da Visão geral, em dois blocos SEPARADOS (frota e conectividade não se
    misturam), com poucas consultas agregadas. Chaves:

        {"frota": {"total",            # veículos NÃO desativados (ativos + manutenção + inativos)
                   "ativos", "em_manutencao", "inativos", "desativados",
                   "com_chip", "sem_chip"},   # com_chip + sem_chip == total
         "conectividade": {"chips_total", "chips_disponiveis", "chips_vinculados",
                           "chips_manutencao", "chips_inativos",
                           "com_sessao_ativa", "sem_sessao", "com_falha"}}

    `com_sessao_ativa`, `sem_sessao` e `com_falha` contam só chips VINCULADOS e consultáveis
    (chip disponível sem sessão é o esperado e não é problema), com o mesmo critério de
    `estado_de_conectividade_do_chip` — "ativa" exige dado fresco e sem falha."""
    agora = agora or timezone.now()
    cfg = M2MIntegracao.get()
    limite = agora - timedelta(hours=cfg.horas_desatualizado)
    com_chip_qs = FrotaVinculo.objects.filter(veiculo=OuterRef("pk"), fim_em__isnull=True)
    f = FrotaVeiculo.objects.aggregate(
        ativos=Count("pk", filter=Q(status=Status.ATIVO)),
        em_manutencao=Count("pk", filter=Q(status=Status.MANUTENCAO)),
        inativos=Count("pk", filter=Q(status=Status.INATIVO)),
        desativados=Count("pk", filter=Q(status=Status.DESATIVADO)),
    )
    total = f["ativos"] + f["em_manutencao"] + f["inativos"]
    com_chip = FrotaVeiculo.objects.exclude(status=Status.DESATIVADO).filter(Exists(com_chip_qs)).count()
    vinculado = Q(situacao_uso=M2MChip.SituacaoUso.VINCULADO)
    c = M2MChip.objects.aggregate(
        chips_total=Count("pk"),
        chips_disponiveis=Count("pk", filter=Q(situacao_uso=M2MChip.SituacaoUso.DISPONIVEL)),
        chips_vinculados=Count("pk", filter=vinculado),
        chips_manutencao=Count("pk", filter=Q(situacao_uso=M2MChip.SituacaoUso.MANUTENCAO)),
        chips_inativos=Count("pk", filter=Q(situacao_uso=M2MChip.SituacaoUso.INATIVO)),
        com_sessao_ativa=Count("pk", filter=vinculado & _q_estado_do_chip("ativa", limite)),
        sem_sessao=Count("pk", filter=vinculado & _q_estado_do_chip("sem_sessao", limite)),
        com_falha=Count("pk", filter=vinculado & _q_estado_do_chip("falha", limite)),
    )
    return {
        "frota": {
            "total": total, "ativos": f["ativos"], "em_manutencao": f["em_manutencao"], "inativos": f["inativos"],
            "desativados": f["desativados"], "com_chip": com_chip, "sem_chip": total - com_chip,
        },
        "conectividade": c,
    }


def area_de_atencao(limite: int = 8, agora=None) -> dict:
    """O que precisa de olho agora, cinco blocos `{"total": N, "itens": [...até `limite`]}`:

        veiculos_sem_chip        veículos ATIVOS que exigem conectividade e não têm chip (FrotaVeiculo)
        chips_sem_atualizacao    chips vinculados e consultáveis com o último dado válido velho (M2MChip)
        falhas_de_comunicacao    chips vinculados cuja última consulta falhou (M2MChip)
        conectividade_pendente   chips vinculados nunca consultados (M2MChip)
        movimentacoes_recentes   as últimas movimentações (FrotaMovimentacao; `total` = dos últimos 7 dias)

    Os chips saem anotados (`m2m.anotar_chips`: `veiculo_atual`, frescor, situação). Só chip
    VINCULADO entra: chip disponível sem sessão ou sem consulta é o esperado."""
    agora = agora or timezone.now()
    cfg = M2MIntegracao.get()
    limite_data = agora - timedelta(hours=cfg.horas_desatualizado)
    limite = max(1, limite)
    base = M2MChip.objects.filter(situacao_uso=M2MChip.SituacaoUso.VINCULADO).select_related(
        "operadora", "localidade", "responsavel", "sessao_consulta", "consumo_consulta",
    ).defer("sessao_consulta__payload", "consumo_consulta__payload")

    def bloco_de_chips(estado: str, ordem: tuple) -> dict:
        qs = base.filter(_q_estado_do_chip(estado, limite_data)).order_by(*ordem)
        return {"total": qs.count(), "itens": m2m.anotar_chips(qs[:limite], cfg, agora)}

    sem_chip = m2m.veiculos_sem_chip_exigido().order_by("codigo")
    recentes = FrotaMovimentacao.objects.select_related("chip", "veiculo_origem", "veiculo_destino").order_by("-em", "-pk")
    return {
        "veiculos_sem_chip": {"total": sem_chip.count(), "itens": list(sem_chip[:limite])},
        "chips_sem_atualizacao": bloco_de_chips("desatualizado", ("ultima_valida_em", "nome", "pk")),
        "falhas_de_comunicacao": bloco_de_chips("falha", ("-falhas_consecutivas", "nome", "pk")),
        "conectividade_pendente": bloco_de_chips("pendente", ("created_at", "pk")),
        "movimentacoes_recentes": {
            "total": FrotaMovimentacao.objects.filter(em__gte=agora - timedelta(days=7)).count(),
            "itens": list(recentes[:limite]),
        },
    }


# ═════════════════════════════════════════════════════════════
# Cadastros de apoio: tipos de veículo, operadoras e regras de alerta
# (as abas da Configuração; exigem `administrar_frota_config` na view)
# ═════════════════════════════════════════════════════════════

def _auditar_config(alvo: str, objeto_id, usuario, alteracoes: dict, criado: bool = False) -> None:
    M2MEvento.objects.create(
        tipo=M2MEvento.Tipo.CONFIG_FROTA, usuario=_usuario_ou_none(usuario),
        dados={"alvo": alvo, "id": objeto_id, "criado": criado, "alteracoes": alteracoes},
    )


def _nome_unico(modelo, nome: str, excluir_pk, rotulo: str, maximo: int = 80) -> str:
    nome = " ".join(str(nome or "").split())
    if not nome:
        raise ValidationError({"nome": f"Informe o nome d{rotulo}."})
    if len(nome) > maximo:
        raise ValidationError({"nome": f"O nome aceita até {maximo} caracteres."})
    existentes = modelo.objects.filter(nome__iexact=nome)
    if excluir_pk:
        existentes = existentes.exclude(pk=excluir_pk)
    if existentes.exists():
        raise ValidationError({"nome": f"Já existe {rotulo} com este nome."})
    return nome


def listar_tipos(somente_ativos: bool = False):
    """QuerySet de `FrotaTipo` por nome, com `n_veiculos` anotado."""
    qs = FrotaTipo.objects.annotate(n_veiculos=Count("veiculos")).order_by("nome")
    return qs.filter(ativo=True) if somente_ativos else qs


def salvar_tipo(dados: dict, usuario=None, tipo: FrotaTipo | None = None) -> FrotaTipo:
    """Cria (`tipo=None`) ou edita um tipo de veículo. `dados`: `nome` (único, sem distinguir
    caixa), `exige_conectividade`, `ativo` (só as chaves presentes mudam na edição; na criação
    o padrão é não exigir e ativo). Tipo inativo some do cadastro de veículo mas os veículos
    dele ficam. Audita em `M2MEvento(config_frota)` com o de/para. `ValidationError({campo: msg})`."""
    criando = tipo is None
    alvo = FrotaTipo() if criando else tipo
    antes = {"nome": alvo.nome, "exige_conectividade": alvo.exige_conectividade, "ativo": alvo.ativo} if not criando else {}
    novos: dict = {}
    if criando or "nome" in dados:
        novos["nome"] = _nome_unico(FrotaTipo, dados.get("nome"), None if criando else alvo.pk, " tipo de veículo")
    for campo, padrao in (("exige_conectividade", False), ("ativo", True)):
        if campo in dados:
            try:
                novos[campo] = _bool(dados[campo], padrao)
            except ValueError:
                raise ValidationError({campo: "Valor inválido."}) from None
        elif criando:
            novos[campo] = padrao
    alteracoes = {c: {"de": antes.get(c), "para": v} for c, v in novos.items() if antes.get(c) != v}
    if not criando and not alteracoes:
        return alvo
    for campo, valor in novos.items():
        setattr(alvo, campo, valor)
    try:
        with transaction.atomic():
            alvo.save()
            _auditar_config("tipo_veiculo", alvo.pk, usuario, alteracoes, criado=criando)
    except IntegrityError:
        raise ValidationError({"nome": "Já existe um tipo de veículo com este nome."}) from None
    return alvo


def listar_operadoras(somente_ativas: bool = False):
    """QuerySet de `ConectividadeOperadora` por nome, com `n_chips` anotado."""
    qs = ConectividadeOperadora.objects.annotate(n_chips=Count("chips")).order_by("nome")
    return qs.filter(ativo=True) if somente_ativas else qs


def salvar_operadora(dados: dict, usuario=None, operadora: ConectividadeOperadora | None = None) -> ConectividadeOperadora:
    """Cria (`operadora=None`) ou edita uma operadora. `dados`: `nome` (único), `integracao`
    (`tim_m2m` ou vazio = "sem integração": os chips dela são cadastrados e controlados, mas
    nunca consultados pela API), `ativo`. Operadora inativa deixa de aparecer no cadastro de
    chip; os chips dela ficam. Audita em `M2MEvento(config_frota)`. `ValidationError({campo: msg})`."""
    criando = operadora is None
    alvo = ConectividadeOperadora() if criando else operadora
    antes = {"nome": alvo.nome, "integracao": alvo.integracao, "ativo": alvo.ativo} if not criando else {}
    novos: dict = {}
    if criando or "nome" in dados:
        novos["nome"] = _nome_unico(ConectividadeOperadora, dados.get("nome"), None if criando else alvo.pk, "a operadora")
    if "integracao" in dados or criando:
        integracao = (dados.get("integracao") or "").strip()
        if integracao not in ConectividadeOperadora.Integracao.values and integracao != "":
            raise ValidationError({"integracao": "Escolha uma integração disponível ou deixe sem integração."})
        novos["integracao"] = integracao
    if "ativo" in dados:
        try:
            novos["ativo"] = _bool(dados["ativo"], True)
        except ValueError:
            raise ValidationError({"ativo": "Valor inválido."}) from None
    elif criando:
        novos["ativo"] = True
    alteracoes = {c: {"de": antes.get(c), "para": v} for c, v in novos.items() if antes.get(c) != v}
    if not criando and not alteracoes:
        return alvo
    for campo, valor in novos.items():
        setattr(alvo, campo, valor)
    try:
        with transaction.atomic():
            alvo.save()
            _auditar_config("operadora", alvo.pk, usuario, alteracoes, criado=criando)
    except IntegrityError:
        raise ValidationError({"nome": "Já existe uma operadora com este nome."}) from None
    return alvo


def listar_regras() -> list:
    """As regras de alerta de TODOS os tipos (criadas com o padrão se faltar), na ordem de
    `M2MOcorrencia.Tipo`, cada uma com `rotulo`, `acao_necessaria` e `destinatarios` (a lista
    de e-mails efetiva)."""
    regras = m2m.regras_de_alerta()
    saida = []
    for tipo in M2MOcorrencia.Tipo.values:
        regra = regras[tipo]
        regra.rotulo = regra.get_tipo_display()
        regra.acao_necessaria = m2m.acao_necessaria(tipo)
        regra.destinatarios = m2m.destinatarios_da_regra(regra)
        saida.append(regra)
    return saida


def salvar_regra_alerta(tipo: str, dados: dict, usuario=None) -> FrotaRegraAlerta:
    """Edita a regra do tipo de ocorrência `tipo`. `dados` (só as chaves presentes mudam):
    `gera_ocorrencia`, `nivel` (`info`/`atencao`/`critico`), `envia_email`, `destinatarios_emails`
    (separados por vírgula, ponto e vírgula, espaço ou quebra de linha; um e-mail inválido
    recusa o conjunto, citando-o) e `destinatarios_usuarios` (lista de ids de usuário). O
    e-mail só sai se o canal `m2m_ocorrencias` da Central de Alertas e a chave-mestra estiverem
    ativos. Audita em `M2MEvento(config_frota)`. `ValidationError({campo: msg})`."""
    if tipo not in M2MOcorrencia.Tipo.values:
        raise ValidationError({"tipo": "Tipo de ocorrência desconhecido."})
    regra = m2m.regra_do_tipo(tipo)
    antes = {
        "gera_ocorrencia": regra.gera_ocorrencia, "nivel": regra.nivel, "envia_email": regra.envia_email,
        "destinatarios_emails": regra.destinatarios_emails,
        "destinatarios_usuarios": sorted(regra.destinatarios_usuarios.values_list("pk", flat=True)),
    }
    novos: dict = {}
    erros: dict[str, str] = {}
    for campo in ("gera_ocorrencia", "envia_email"):
        if campo in dados:
            try:
                novos[campo] = _bool(dados[campo], False)
            except ValueError:
                erros[campo] = "Valor inválido."
    if "nivel" in dados:
        if dados["nivel"] not in M2MOcorrencia.Nivel.values:
            erros["nivel"] = "Escolha informativo, atenção ou crítico."
        else:
            novos["nivel"] = dados["nivel"]
    if "destinatarios_emails" in dados:
        texto = str(dados["destinatarios_emails"] or "")
        invalidos = []
        validos = []
        for bruto in re.split(r"[,;\s]+", texto):
            email = bruto.strip()
            if not email:
                continue
            try:
                validate_email(email)
                if email.lower() not in [v.lower() for v in validos]:
                    validos.append(email)
            except ValidationError:
                invalidos.append(email)
        if invalidos:
            erros["destinatarios_emails"] = "E-mail inválido: " + ", ".join(invalidos) + "."
        else:
            novos["destinatarios_emails"] = ", ".join(validos)
    usuarios = None
    if "destinatarios_usuarios" in dados:
        ids = []
        for bruto in dados["destinatarios_usuarios"] or []:
            if not str(bruto).isdigit():
                erros["destinatarios_usuarios"] = "Usuário inválido."
                break
            ids.append(int(bruto))
        else:
            usuarios = list(User.objects.filter(pk__in=ids, is_active=True))
            if len(usuarios) != len(set(ids)):
                erros["destinatarios_usuarios"] = "Algum usuário escolhido não existe ou está inativo."
    if erros:
        raise ValidationError(erros)
    with transaction.atomic():
        if novos:
            for campo, valor in novos.items():
                setattr(regra, campo, valor)
            regra.save(update_fields=list(novos))
        if usuarios is not None:
            regra.destinatarios_usuarios.set(usuarios)
        depois = {
            "gera_ocorrencia": regra.gera_ocorrencia, "nivel": regra.nivel, "envia_email": regra.envia_email,
            "destinatarios_emails": regra.destinatarios_emails,
            "destinatarios_usuarios": sorted(regra.destinatarios_usuarios.values_list("pk", flat=True)),
        }
        alteracoes = {c: {"de": antes[c], "para": depois[c]} for c in depois if antes[c] != depois[c]}
        if alteracoes:
            _auditar_config("regra_alerta", regra.pk, usuario, {"tipo": tipo, **alteracoes})
    return regra


def restaurar_regra_padrao(tipo: str, usuario=None) -> FrotaRegraAlerta:
    """Devolve a regra do tipo aos valores de fábrica (`REGRAS_DE_ALERTA_PADRAO`), sem destinatários."""
    padrao = REGRAS_DE_ALERTA_PADRAO[tipo]
    return salvar_regra_alerta(
        tipo, {**padrao, "destinatarios_emails": "", "destinatarios_usuarios": []}, usuario,
    )
