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
from datetime import date, datetime, timedelta

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
    FrotaCentroCusto,
    FrotaClasseMecanica,
    FrotaClasseOperacional,
    FrotaEmpresa,
    FrotaEvento,
    FrotaMovimentacao,
    FrotaNatureza,
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
from services import busca_service as busca
from services import frota_qualidade_service as qualidade
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
    "codigo", "codigo_legado", "nome", "tipo", "fabricante", "modelo", "ano_fabricacao", "ano_modelo",
    "natureza", "empresa", "classe_operacional", "classe_mecanica", "centro_custo", "data_compra", "data_inicio",
    "placa", "renavam", "chassi_serie", "localidade", "setor", "area_operacao", "responsavel", "observacoes",
    "exige_conectividade", "latitude", "longitude",
)
_PADROES_VEICULO = dict(
    codigo="", codigo_legado="", nome="", tipo=None, fabricante="", modelo="", ano_fabricacao=None, ano_modelo=None,
    natureza=FrotaNatureza.PROPRIO, empresa=None, classe_operacional=None, classe_mecanica=None, centro_custo=None,
    data_compra=None, data_inicio=None, placa="", renavam="", chassi_serie="", localidade=None, setor="",
    area_operacao="", responsavel=None, observacoes="", exige_conectividade=None, latitude=None, longitude=None,
)
_LIMITES_DE_TEXTO = {
    "nome": (120, "o nome", True), "fabricante": (80, "o fabricante", False), "modelo": (80, "o modelo", False),
    "setor": (100, "o setor", False), "area_operacao": (100, "a área de operação", False),
}
# Os cadastros de apoio que o equipamento escolhe: campo -> (modelo, rótulo). Um cadastro INATIVO continua
# valendo para quem já o usa, mas não pode ser escolhido de novo.
_CATALOGOS = {
    "empresa": (FrotaEmpresa, "Empresa"),
    "classe_operacional": (FrotaClasseOperacional, "Classe operacional"),
    "classe_mecanica": (FrotaClasseMecanica, "Classe mecânica"),
    "centro_custo": (FrotaCentroCusto, "Centro de custo"),
}
# O que é MOVIMENTAÇÃO do equipamento (muda de lugar ou de responsável) e não correção de cadastro.
CAMPOS_DE_MOVIMENTACAO = ("localidade", "responsavel", "setor", "area_operacao", "centro_custo")
PREFIXO_DO_CODIGO = "MQ-"
ANO_DA_IMPORTACAO = (1900, 2100)           # na importação o ano só precisa ser um ano de verdade


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


def normalizar_identificacao(valor) -> str:
    """A placa como IDENTIFICAÇÃO do equipamento: caixa alta, sem hífen nem espaços, de 1 a 8 letras
    e números. É a regra dos tipos que NÃO esperam placa de rua (trator, gerador, implemento): eles
    têm etiqueta interna como "C8110035" ou o próprio código da máquina. Vazio devolve `""`."""
    texto = re.sub(r"[\s\-]", "", str(valor or "")).upper()
    if not texto:
        return ""
    if not re.fullmatch(r"[A-Z0-9]{1,8}", texto):
        raise ValidationError("A placa / identificação aceita até 8 letras e números.")
    return texto


def normalizar_codigo_legado(valor) -> str:
    """O código do ERP, como veio: aparado e com os espaços internos colapsados (sem mudar a caixa
    nem completar nada). Vazio é válido; passar de 30 caracteres não."""
    codigo = " ".join(str(valor or "").split())
    if len(codigo) > 30:
        raise ValidationError("O código original aceita até 30 caracteres.")
    return codigo


def ler_data(valor, *, rotulo: str = "A data"):
    """Uma data de cadastro: `date`/`datetime`, `AAAA-MM-DD` ou `DD/MM/AAAA`. Vazio devolve `None`.
    `ValidationError` para o que não é uma data de verdade (31/02, texto)."""
    if valor in (None, ""):
        return None
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    texto = str(valor).strip()
    for formato in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(texto, formato).date()
        except ValueError:
            continue
    raise ValidationError(f"{rotulo} não é uma data válida (use dia/mês/ano).")


def _ano(bruto, rotulo: str, importando: bool):
    if bruto in (None, ""):
        return None
    minimo, maximo = ANO_DA_IMPORTACAO if importando else (ANO_MINIMO, timezone.localdate().year + 1)
    try:
        ano = int(str(bruto).strip())
        if not minimo <= ano <= maximo:
            raise ValueError
    except ValueError:
        raise ValidationError(f"{rotulo} deve estar entre {minimo} e {maximo}.") from None
    return ano


ler_ano = _ano                      # público: a importação de equipamentos lê os anos pela mesma regra


def _placa_do_cadastro(valor, placa_atual: str, tipo, importando: bool) -> str:
    """Uma placa que JÁ está gravada e não mudou nunca bloqueia a edição do equipamento (senão
    corrigir o setor de um trator com a etiqueta "C8110035" exigiria apagar a etiqueta). Placa NOVA:
    formato oficial (AAA9999 ou Mercosul) nos tipos que esperam placa de rua; nos demais, a
    identificação livre de até 8 letras e números. A importação nunca recusa pelo formato."""
    livre = normalizar_identificacao(valor)
    if livre == (placa_atual or "") or importando or not (tipo is not None and tipo.espera_placa):
        return livre
    return normalizar_placa(valor)


def _limpar_dados_veiculo(dados: dict, atual: dict, *, importando: bool = False) -> dict:
    """Valida e normaliza os campos do veículo. `atual` traz os valores vigentes (na
    criação, os padrões); só as chaves presentes em `dados` mudam. `importando` afrouxa o que é
    JULGAMENTO (formato de placa, faixa de anos) sem afrouxar o que é estrutura (tamanho, tipo de
    dado): a importação não corrige nem recusa por suspeita — a suspeita vira conferência.
    `ValidationError({campo: msg})`."""
    erros: dict[str, str] = {}
    v = dict(atual)
    if "codigo" in dados:
        try:
            v["codigo"] = normalizar_codigo(dados["codigo"])
        except ValidationError as exc:
            erros["codigo"] = exc.messages[0]
    if "codigo_legado" in dados:
        try:
            v["codigo_legado"] = normalizar_codigo_legado(dados["codigo_legado"])
        except ValidationError as exc:
            erros["codigo_legado"] = exc.messages[0]
    for campo, (maximo, rotulo, obrigatorio) in _LIMITES_DE_TEXTO.items():
        if campo in dados:
            texto = " ".join(str(dados[campo] if dados[campo] is not None else "").split())
            if obrigatorio and not texto:
                erros[campo] = f"Informe {rotulo}."
            elif len(texto) > maximo:
                erros[campo] = f"{rotulo[:1].upper() + rotulo[1:]} aceita até {maximo} caracteres."
            v[campo] = texto

    # o tipo vem ANTES da placa: é ele que diz que formato de placa se espera
    if "tipo" in dados:
        tipo = _entidade(dados["tipo"], FrotaTipo, "Tipo", "tipo", erros)
        if tipo is None:
            erros.setdefault("tipo", "Escolha a categoria do equipamento.")
        elif not tipo.ativo and getattr(atual.get("tipo"), "pk", None) != tipo.pk:
            erros["tipo"] = f"O tipo {tipo.nome} está inativo."
        else:
            v["tipo"] = tipo
    elif v.get("tipo") is None:
        erros["tipo"] = "Escolha a categoria do equipamento."

    if "placa" in dados:
        try:
            v["placa"] = _placa_do_cadastro(dados["placa"], atual.get("placa", ""), v.get("tipo"), importando)
        except ValidationError as exc:
            erros["placa"] = exc.messages[0]
    for campo, funcao in (("renavam", normalizar_renavam), ("chassi_serie", normalizar_chassi)):
        if campo in dados:
            try:
                v[campo] = funcao(dados[campo])
            except ValidationError as exc:
                erros[campo] = exc.messages[0]
    for campo, rotulo in (("ano_fabricacao", "O ano de fabricação"), ("ano_modelo", "O ano-modelo")):
        if campo in dados:
            try:
                v[campo] = _ano(dados[campo], rotulo, importando)
            except ValidationError as exc:
                # um ano que JÁ está gravado e não mudou nunca bloqueia a edição de outro campo
                try:
                    igual = _ano(dados[campo], rotulo, True) == atual.get(campo)
                except ValidationError:
                    igual = False
                if igual:
                    v[campo] = atual.get(campo)
                else:
                    erros[campo] = exc.messages[0]
    if "natureza" in dados:
        if dados["natureza"] in FrotaNatureza.values:
            v["natureza"] = dados["natureza"]
        else:
            erros["natureza"] = "Escolha Próprio ou Terceiro."
    for campo, (modelo, rotulo) in _CATALOGOS.items():
        if campo in dados:
            escolhido = _entidade(dados[campo], modelo, rotulo, campo, erros)
            if escolhido is not None and not escolhido.ativo and getattr(atual.get(campo), "pk", None) != escolhido.pk:
                erros[campo] = f"{rotulo}: “{escolhido.nome}” está inativo(a)."
            elif campo not in erros:
                v[campo] = escolhido
    for campo, rotulo in (("data_compra", "A data de compra"), ("data_inicio", "A data de início")):
        if campo in dados:
            try:
                data = ler_data(dados[campo], rotulo=rotulo)
                if data is not None and data == atual.get(campo):
                    v[campo] = data                    # a data que já está gravada nunca bloqueia a edição de outro campo
                    continue
                if data is not None and data.year < ANO_DA_IMPORTACAO[0]:
                    raise ValidationError(f"{rotulo} está antes de {ANO_DA_IMPORTACAO[0]}.")
                if data is not None and not importando and data > timezone.localdate():
                    raise ValidationError(f"{rotulo} não pode estar no futuro.")
                v[campo] = data
            except ValidationError as exc:
                erros[campo] = exc.messages[0]
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
    if "latitude" in dados or "longitude" in dados:
        # A coordenada é a que alguém CADASTROU (regra 63): par completo, em graus decimais, plausível. Uma
        # coordenada que já está gravada e não mudou nunca bloqueia a edição de outro campo (o ERP não tinha esta
        # informação; quem a digitou antes de a regra ficar mais estrita não pode ser barrado ao corrigir o setor).
        from services import agro_infra_service as infra
        try:
            lat, lon = infra.ler_coordenada(
                dados.get("latitude", atual.get("latitude")), dados.get("longitude", atual.get("longitude")),
            )
            v["latitude"], v["longitude"] = lat, lon
        except ValidationError as exc:
            igual = False
            try:
                igual = (
                    infra._decimal(dados.get("latitude", atual.get("latitude")), "latitude", "Latitude") == atual.get("latitude")
                    and infra._decimal(dados.get("longitude", atual.get("longitude")), "longitude", "Longitude") == atual.get("longitude")
                )
            except ValidationError:
                igual = False
            if igual:
                v["latitude"], v["longitude"] = atual.get("latitude"), atual.get("longitude")
            else:
                erros.update({k: " ".join(m) for k, m in exc.message_dict.items()})
    if not v.get("codigo") and "codigo" not in erros:
        erros["codigo"] = "Informe o código do veículo."
    if not v.get("nome") and "nome" not in erros:
        erros["nome"] = "Informe o nome."
    if erros:
        raise ValidationError(erros)
    return v


def _conferir_unicidade_veiculo(valores: dict, excluir_pk=None, *, chassi_original: str | None = None, importando: bool = False) -> None:
    """O que é ÚNICO: o código interno, o código original do ERP e a placa. O chassi também, mas só
    quando alguém o DIGITA: o que já está gravado (`chassi_original`) e o que vem do ERP
    (`importando`) pode repetir — é conferência, e recusar impediria importar a planilha inteira."""
    outros = FrotaVeiculo.objects.exclude(pk=excluir_pk) if excluir_pk else FrotaVeiculo.objects.all()
    erros: dict[str, str] = {}
    if outros.filter(codigo__iexact=valores["codigo"]).exists():
        erros["codigo"] = "Já existe um veículo com este código."
    if valores.get("codigo_legado") and outros.filter(codigo_legado=valores["codigo_legado"]).exists():
        erros["codigo_legado"] = "Já existe um equipamento com este código original."
    if valores.get("placa"):
        dono = outros.filter(placa=valores["placa"]).first()
        if dono is not None:
            erros["placa"] = f"Já existe um veículo com esta placa ({dono.codigo_legado or dono.codigo})."
    chassi = valores.get("chassi_serie")
    if chassi and not importando and chassi != chassi_original:
        dono = outros.filter(chassi_serie=chassi).first()
        if dono is not None:
            erros["chassi_serie"] = f"Já existe um veículo com este chassi / nº de série ({dono.codigo_legado or dono.codigo})."
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


def proximo_codigo() -> str:
    """O próximo identificador INTERNO do Zelo Agro (`MQ-00001`…): o maior número já usado mais um.
    É o que o equipamento ganha quando ninguém digita um código — o código do ERP nunca vira
    interno, e o interno nunca substitui o do ERP (`codigo_legado`)."""
    maior = 0
    for codigo in FrotaVeiculo.objects.filter(codigo__startswith=PREFIXO_DO_CODIGO).values_list("codigo", flat=True):
        sufixo = codigo[len(PREFIXO_DO_CODIGO):]
        if sufixo.isdigit():
            maior = max(maior, int(sufixo))
    return f"{PREFIXO_DO_CODIGO}{maior + 1:05d}"


def criar_veiculo(dados: dict, usuario=None, *, importacao=None) -> FrotaVeiculo:
    """Cadastra um veículo/equipamento. `dados`: codigo (vazio = gerado: `MQ-00001`), codigo_legado,
    nome, tipo, fabricante, modelo, ano_fabricacao, ano_modelo, natureza, empresa, classe_operacional,
    classe_mecanica, centro_custo, data_compra, data_inicio, placa, renavam, chassi_serie, localidade,
    setor, area_operacao, responsavel, observacoes, exige_conectividade (as chaves de cadastro de apoio,
    `tipo`, `localidade` e `responsavel` aceitam instância ou id; `exige_conectividade` vazio = herda do
    tipo) e, opcionais, `status` (padrão `ativo`), `situacao_origem` (o texto da planilha) e
    `validacao_pendente`. Normaliza (código em caixa alta, placa sem hífen) e valida; código, código
    original e placa são ÚNICOS (os dois últimos só quando preenchidos), e o chassi também — salvo na
    importação. Placa, RENAVAM e chassi são opcionais. Grava `FrotaEvento(cadastro)`.

    `importacao` (uma `FrotaImportacaoEquipamentos`) liga o modo da planilha do ERP: o formato da placa e
    a faixa dos anos deixam de recusar, o chassi pode repetir, e o evento diz de onde o equipamento veio.
    Levanta `ValidationError({campo: mensagem})`."""
    dados = dict(dados)
    status = dados.pop("status", Status.ATIVO) or Status.ATIVO
    situacao_origem = str(dados.pop("situacao_origem", "") or "").strip()[:40]
    validacao_pendente = bool(dados.pop("validacao_pendente", False))
    importando = importacao is not None
    erros_status = {}
    if status not in Status.values:
        erros_status["status"] = "Escolha um status válido."
    codigo_gerado = not str(dados.get("codigo") or "").strip()
    if codigo_gerado:
        dados["codigo"] = proximo_codigo()
    try:
        valores = _limpar_dados_veiculo(dados, _PADROES_VEICULO, importando=importando)
        _conferir_unicidade_veiculo(valores, importando=importando)
    except ValidationError as exc:
        erros = dict(exc.message_dict) if hasattr(exc, "error_dict") else {"__all__": exc.messages}
        raise ValidationError({**erros, **erros_status}) from None
    if erros_status:
        raise ValidationError(erros_status)
    evento = {
        "codigo": valores["codigo"], "nome": valores["nome"], "tipo": valores["tipo"].nome, "status": status,
        "codigo_legado": valores["codigo_legado"],
    }
    if importando:
        evento.update(origem="importacao", importacao=importacao.pk, situacao_origem=situacao_origem)
    for tentativa in range(5):
        try:
            with transaction.atomic():
                localizacao = (
                    {"localizacao_origem": "cadastral", "localizacao_em": timezone.now()} if valores.get("latitude") is not None else {}
                )
                veiculo = FrotaVeiculo(
                    **valores, **localizacao, status=status, situacao_origem=situacao_origem, validacao_pendente=validacao_pendente,
                    criado_por=_usuario_ou_none(usuario), atualizado_por=_usuario_ou_none(usuario),
                )
                veiculo.save()
                FrotaEvento.objects.create(
                    veiculo=veiculo, tipo=FrotaEvento.Tipo.CADASTRO, usuario=_usuario_ou_none(usuario), dados=evento,
                )
            return veiculo
        except IntegrityError:
            # o identificador gerado pode ter sido tomado por outra requisição no meio-tempo: gera outro
            if not codigo_gerado or tentativa == 4:
                raise ValidationError("Já existe um veículo com este código, placa ou código original.") from None
            valores["codigo"] = evento["codigo"] = proximo_codigo()


def editar_veiculo(veiculo: FrotaVeiculo, dados: dict, usuario=None, *, importacao=None) -> FrotaVeiculo:
    """Altera o cadastro. Só as chaves presentes em `dados` mudam; se nada mudou de fato,
    não grava nada. Grava `FrotaEvento(edicao)` com o de/para dos campos alterados. O STATUS
    não se edita aqui: `mudar_status`. As mesmas validações e unicidades de `criar_veiculo` — com a
    exceção de que o que JÁ está gravado (placa, chassi) nunca bloqueia a edição de OUTRO campo.
    `importacao` marca a origem no evento (a atualização autorizada de uma planilha do ERP).
    Levanta `ValidationError({campo: mensagem})`."""
    dados = {k: v for k, v in dict(dados).items() if k != "status"}
    importando = importacao is not None
    atuais = {campo: getattr(veiculo, campo) for campo in CAMPOS_DO_VEICULO}
    valores = _limpar_dados_veiculo(dados, atuais, importando=importando)
    _conferir_unicidade_veiculo(valores, excluir_pk=veiculo.pk, chassi_original=veiculo.chassi_serie, importando=importando)
    alteracoes = {
        campo: {"de": _json(atuais[campo]), "para": _json(valores[campo])}
        for campo in CAMPOS_DO_VEICULO if valores[campo] != atuais[campo]
    }
    if not alteracoes:
        return veiculo
    for campo in alteracoes:
        setattr(veiculo, campo, valores[campo])
    extras = []
    if "latitude" in alteracoes or "longitude" in alteracoes:
        # Coordenada digitada à mão é CADASTRAL; apagar a coordenada apaga também a origem e a hora (localização desconhecida).
        veiculo.localizacao_origem = "cadastral" if valores["latitude"] is not None else ""
        veiculo.localizacao_em = timezone.now() if valores["latitude"] is not None else None
        extras = ["localizacao_origem", "localizacao_em"]
    veiculo.atualizado_por = _usuario_ou_none(usuario)
    evento = {"alteracoes": alteracoes}
    if importando:
        evento.update(origem="importacao", importacao=importacao.pk)
    try:
        with transaction.atomic():
            veiculo.save(update_fields=[*alteracoes, *extras, "atualizado_por", "updated_at"])
            FrotaEvento.objects.create(
                veiculo=veiculo, tipo=FrotaEvento.Tipo.EDICAO, usuario=_usuario_ou_none(usuario), dados=evento,
            )
    except IntegrityError:
        raise ValidationError("Já existe um veículo com este código, placa ou código original.") from None
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


# ═════════════════════════════════════════════════════════════
# Inventário: validar, movimentar, verificar antes de excluir, indicadores
# ═════════════════════════════════════════════════════════════

def validar_cadastro(veiculo: FrotaVeiculo, usuario, *, status: str = "", motivo: str = "") -> FrotaVeiculo:
    """O responsável pelo inventário CONFIRMA o cadastro vindo da planilha do ERP: a pendência de
    validação sai, e o status pode ser ajustado na mesma hora (`status` vazio mantém o atual; o ajuste
    passa por `mudar_status`, com as mesmas recusas). Grava `FrotaEvento(validacao)` com quem confirmou.
    Importar NÃO é validar: o ERP diz "Em Uso", mas só quem confere o parque sabe se a máquina está lá.

    Idempotente contra clique duplo: a pendência é tomada por atualização condicional, então só uma das
    chamadas simultâneas valida (a outra recebe "já foi validado"). Levanta `ValidationError`."""
    motivo = (motivo or "").strip()
    if len(motivo) > 255:
        raise ValidationError({"motivo": "O motivo aceita até 255 caracteres."})
    if status and status not in Status.values:
        raise ValidationError({"status": "Escolha um status válido."})
    with transaction.atomic():
        atual = FrotaVeiculo.objects.get(pk=veiculo.pk)
        tomou = FrotaVeiculo.objects.filter(pk=atual.pk, validacao_pendente=True).update(
            validacao_pendente=False, atualizado_por=_usuario_ou_none(usuario), updated_at=timezone.now(),
        )
        if not tomou:
            raise ValidationError("Este equipamento já foi validado.")
        if status and status != atual.status:
            mudar_status(atual, status, usuario, motivo or "Validação do inventário")
        FrotaEvento.objects.create(
            veiculo=atual, tipo=FrotaEvento.Tipo.VALIDACAO, usuario=_usuario_ou_none(usuario),
            dados={"situacao_origem": atual.situacao_origem, "status": status or atual.status, "motivo": motivo},
        )
    veiculo.refresh_from_db()
    return veiculo


def validar_varios(pks, usuario) -> dict:
    """Valida os equipamentos `pks` que ainda estão pendentes, um a um (cada um com o seu evento).
    Devolve `{"validados": n, "ignorados": n}`; ignorado é o que já estava validado ou não existe."""
    pedidos = {int(pk) for pk in pks if str(pk).isdigit()}
    validados = 0
    for veiculo in FrotaVeiculo.objects.filter(pk__in=pedidos, validacao_pendente=True):
        try:
            validar_cadastro(veiculo, usuario)
            validados += 1
        except ValidationError:
            continue
    return {"validados": validados, "ignorados": len(pedidos) - validados}


def registrar_movimentacao(veiculo: FrotaVeiculo, dados: dict, usuario=None, *, motivo: str = "") -> FrotaVeiculo:
    """Registra que o equipamento MUDOU DE LUGAR OU DE RESPONSÁVEL: `dados` pode trazer `localidade`
    (fazenda), `setor`, `area_operacao`, `centro_custo` e `responsavel` (só os que mudaram). O `motivo`
    é obrigatório — é a justificativa que a tela de Movimentações mostra — e o evento guarda o
    de/para. Diferente de editar o cadastro (correção de dado), isto conta a história do equipamento.
    Recusa: equipamento desativado, motivo vazio e nada que tenha mudado. Levanta `ValidationError`."""
    motivo = " ".join(str(motivo or "").split())
    erros: dict[str, str] = {}
    if not motivo:
        erros["motivo"] = "Informe o motivo da movimentação."
    elif len(motivo) > 255:
        erros["motivo"] = "O motivo aceita até 255 caracteres."
    if veiculo.status == Status.DESATIVADO:
        raise ValidationError("O veículo está desativado e não pode ser movimentado.")
    entrada = {campo: valor for campo, valor in dict(dados).items() if campo in CAMPOS_DE_MOVIMENTACAO}
    atuais = {campo: getattr(veiculo, campo) for campo in CAMPOS_DO_VEICULO}
    try:
        valores = _limpar_dados_veiculo(entrada, atuais)
    except ValidationError as exc:
        erros.update(exc.message_dict if hasattr(exc, "error_dict") else {"__all__": exc.messages})
    if erros:
        raise ValidationError({campo: (m if isinstance(m, list) else [m]) for campo, m in erros.items()})
    alteracoes = {
        campo: {"de": _json(atuais[campo]), "para": _json(valores[campo])}
        for campo in entrada if valores[campo] != atuais[campo]
    }
    if not alteracoes:
        raise ValidationError("Nada mudou: informe o novo lugar ou o novo responsável.")
    for campo in alteracoes:
        setattr(veiculo, campo, valores[campo])
    veiculo.atualizado_por = _usuario_ou_none(usuario)
    with transaction.atomic():
        veiculo.save(update_fields=[*alteracoes, "atualizado_por", "updated_at"])
        FrotaEvento.objects.create(
            veiculo=veiculo, tipo=FrotaEvento.Tipo.MOVIMENTACAO, usuario=_usuario_ou_none(usuario),
            dados={"alteracoes": alteracoes, "motivo": motivo},
        )
    return veiculo


def verificar_exclusao(veiculo: FrotaVeiculo) -> dict:
    """O que existe em volta do equipamento ANTES de excluí-lo. Excluir aqui é DESATIVAR (exclusão
    lógica): o registro, o histórico e as movimentações ficam guardados, porque o equipamento aparece
    em cadastro, instalação e movimentação que não podem perder o dono. Devolve:

        {"pode": bool, "ja_desativado": bool,
         "impedimentos": [frases],                  # chip vinculado e equipamento tecnológico instalado
         "chips_ativos": n, "equipamentos_instalados": n,
         "historico": {"vinculos_de_chip": n, "instalacoes": n, "movimentacoes_de_chip": n, "eventos": n}}

    Chip vinculado ou equipamento instalado IMPEDEM (a máquina desativada não recebe nenhum dos dois, e
    deixar um preso nela criaria um estado que o sistema não permite); o resto é histórico a preservar."""
    chips = FrotaVinculo.objects.filter(veiculo=veiculo, fim_em__isnull=True).count()
    instalados = TecInstalacao.objects.filter(veiculo=veiculo, fim_em__isnull=True).count()
    impedimentos = []
    if chips:
        impedimentos.append(
            f"Tem {chips} chip{'s' if chips != 1 else ''} vinculado{'s' if chips != 1 else ''}: desvincule ou transfira antes."
        )
    if instalados:
        impedimentos.append(
            f"Tem {instalados} equipamento{'s' if instalados != 1 else ''} tecnológico{'s' if instalados != 1 else ''} "
            f"instalado{'s' if instalados != 1 else ''}: retire ou transfira antes."
        )
    ja = veiculo.status == Status.DESATIVADO
    return {
        "pode": not impedimentos and not ja, "ja_desativado": ja, "impedimentos": impedimentos,
        "chips_ativos": chips, "equipamentos_instalados": instalados,
        "historico": {
            "vinculos_de_chip": FrotaVinculo.objects.filter(veiculo=veiculo).count(),
            "instalacoes": TecInstalacao.objects.filter(veiculo=veiculo).count(),
            "movimentacoes_de_chip": FrotaMovimentacao.objects.filter(Q(veiculo_origem=veiculo) | Q(veiculo_destino=veiculo)).count(),
            "eventos": veiculo.eventos.count(),
        },
    }


def indicadores_equipamentos() -> dict:
    """A faixa de indicadores da tela principal, calculada do que está CADASTRADO (nada inventado), em
    uma consulta. Chaves: `total` (todos, inclusive os excluídos/desativados), `proprios` e `terceiros` (só os que a
    lista mostra: sem os excluídos), `em_uso` (status ativo), `em_manutencao`,
    `inativos`, `desativados`, `sem_identificacao` (falta o que o tipo espera: placa, chassi — ver
    `frota_qualidade_service`; desativado não conta) e `a_validar` (importado e ainda não confirmado),
    mais `em_uso_a_validar`: quantos dos "em uso" ainda dependem da confirmação do responsável."""
    return FrotaVeiculo.objects.aggregate(
        total=Count("pk"),
        proprios=Count("pk", filter=Q(natureza=FrotaNatureza.PROPRIO) & ~Q(status=Status.DESATIVADO)),
        terceiros=Count("pk", filter=Q(natureza=FrotaNatureza.TERCEIRO) & ~Q(status=Status.DESATIVADO)),
        em_uso=Count("pk", filter=Q(status=Status.ATIVO)),
        em_manutencao=Count("pk", filter=Q(status=Status.MANUTENCAO)),
        inativos=Count("pk", filter=Q(status=Status.INATIVO)),
        desativados=Count("pk", filter=Q(status=Status.DESATIVADO)),
        sem_identificacao=Count("pk", filter=qualidade.q_identificacao_incompleta() & ~Q(status=Status.DESATIVADO)),
        a_validar=Count("pk", filter=Q(validacao_pendente=True)),
        em_uso_a_validar=Count("pk", filter=Q(status=Status.ATIVO, validacao_pendente=True)),
    )


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

ORDENS_DE_VEICULOS = (
    "atencao", "codigo", "nome", "tipo", "legado", "classe", "modelo", "empresa", "setor", "placa", "ano", "situacao",
)
# ordem -> campos do `order_by` (a ordem `atencao` e a `legado` têm tratamento próprio em `_ordenar`)
_CAMPOS_DE_ORDEM = {
    "codigo": ("codigo",),
    "nome": ("nome", "codigo"),
    "tipo": ("tipo__nome", "codigo"),
    "classe": ("classe_operacional__nome", "codigo_legado", "codigo"),
    "modelo": ("modelo", "codigo_legado", "codigo"),
    "empresa": ("empresa__nome", "codigo_legado", "codigo"),
    "setor": ("setor", "codigo_legado", "codigo"),
    "placa": ("placa", "codigo_legado", "codigo"),
    "ano": ("ano_fabricacao", "ano_modelo", "codigo_legado", "codigo"),
    "situacao": ("status", "codigo_legado", "codigo"),
}


def _ordenar(qs, ordem: str, descendente: bool):
    """A ordem pedida, no sentido pedido. Em `legado` o código do ERP manda e quem não tem (cadastro
    feito à mão) fica por último nos dois sentidos."""
    sentido = "-" if descendente else ""
    if ordem == "legado":
        sem_legado = Case(When(codigo_legado="", then=Value(1)), default=Value(0), output_field=IntegerField())
        return qs.annotate(sem_legado=sem_legado).order_by("sem_legado", f"{sentido}codigo_legado", f"{sentido}codigo", "pk")
    campos = _CAMPOS_DE_ORDEM[ordem]
    return qs.order_by(*(f"{sentido}{c}" for c in campos), "pk")


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

        q            código interno, código original (ERP), nome, modelo, fabricante, placa (sem hífen),
                     chassi/série, centro de custo (código ou nome) e, dos chips ATIVOS do veículo, nome,
                     ICCID, IMSI e número da linha
        tipo         id do tipo (a categoria);   localidade  id da unidade;   status  um de `FrotaVeiculo.Status`
        natureza     `proprio` | `terceiro`;   validacao  `pendente` | `validado`
        empresa, classe_operacional, classe_mecanica, centro_custo   ids dos cadastros de apoio
        setor        o setor, pelo nome exato;   ano_fabricacao, ano_modelo   o ano
        pendencia    uma chave de `frota_qualidade_service.PENDENCIAS` (identificação incompleta, chassi
                     repetido, sem classe, sem setor, situação a validar)
        chip         `com` | `sem`  (tem ou não chip vinculado)
        conectividade  um de `ESTADOS_DE_CONECTIVIDADE` (o resumo do veículo, ver `conectividade_do_veiculo`)
        operadora    id da operadora de algum chip ativo
        de, ate      `AAAA-MM-DD`: última CONSULTA (tentativa) de algum chip ativo dentro do período, ate inclusive
        ordem        `atencao` (padrão: quem precisa de atenção primeiro) | `legado` (código do ERP) | `codigo` |
                     `nome` | `tipo` | `classe` | `modelo` | `empresa` | `setor` | `placa` | `ano` | `situacao`;
                     `dir=desc` inverte (a `atencao` não inverte)

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
        FrotaVeiculo.objects.select_related(
            "tipo", "localidade", "responsavel", "empresa", "classe_operacional", "classe_mecanica", "centro_custo",
        )
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
        # Os textos do ERP vêm em caixa alta e acentuados ("CAMINHÕES TABACO"): nome, modelo, fabricante e
        # centro de custo são comparados sem acento e sem diferença de caixa (o `LIKE` do SQLite só ignora
        # a caixa de ASCII). Códigos, placa e chassi são ASCII, e seguem no `icontains`.
        termo = busca.normalizar_busca(q)
        qs = qs.alias(
            nome_busca=busca.SemAcento("nome"), modelo_busca=busca.SemAcento("modelo"),
            fabricante_busca=busca.SemAcento("fabricante"), centro_busca=busca.SemAcento("centro_custo__nome"),
        )
        condicao = (
            Q(codigo__icontains=q) | Q(codigo_legado__icontains=q) | Q(nome_busca__contains=termo)
            | Q(modelo_busca__contains=termo) | Q(fabricante_busca__contains=termo) | Q(placa__icontains=sem_hifen)
            | Q(chassi_serie__icontains=q) | Q(centro_busca__contains=termo) | Q(centro_custo__codigo_erp__icontains=q)
            | algum(Q(chip__nome__icontains=q))
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
    elif filtros.get("sem_excluidos") == "1":
        qs = qs.exclude(status=Status.DESATIVADO)          # "excluído" é desativado: sai da lista, a não ser que se peça a situação
    if filtros.get("natureza") in FrotaNatureza.values:
        qs = qs.filter(natureza=filtros["natureza"])
    if filtros.get("validacao") in ("pendente", "validado"):
        qs = qs.filter(validacao_pendente=filtros["validacao"] == "pendente")
    for campo in ("empresa", "classe_operacional", "classe_mecanica", "centro_custo"):
        if str(filtros.get(campo) or "").isdigit():
            qs = qs.filter(**{f"{campo}_id": int(filtros[campo])})
    if filtros.get("setor"):
        qs = qs.alias(setor_busca=busca.SemAcento("setor")).filter(setor_busca=busca.normalizar_busca(filtros["setor"]))
    if filtros.get("fabricante"):
        qs = qs.alias(fabricante_exato=busca.SemAcento("fabricante")).filter(
            fabricante_exato=busca.normalizar_busca(filtros["fabricante"]),
        )
    if filtros.get("localizacao") == "com":
        qs = qs.filter(latitude__isnull=False)
    elif filtros.get("localizacao") == "sem":
        qs = qs.filter(latitude__isnull=True)
    for campo in ("ano_fabricacao", "ano_modelo"):
        if str(filtros.get(campo) or "").isdigit():
            qs = qs.filter(**{campo: int(filtros[campo])})
    qs = qualidade.filtrar_pendencia(qs, filtros.get("pendencia"))
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
    if ordem != "atencao":
        return _ordenar(qs, ordem, filtros.get("dir") == "desc")
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


ROTULOS_DE_CAMPO = {
    "codigo": "Identificador interno", "codigo_legado": "Código original", "nome": "Nome", "tipo": "Categoria",
    "fabricante": "Fabricante", "modelo": "Modelo", "ano_fabricacao": "Ano de fabricação", "ano_modelo": "Ano-modelo",
    "natureza": "Natureza", "empresa": "Empresa", "classe_operacional": "Classe operacional",
    "classe_mecanica": "Classe mecânica", "centro_custo": "Centro de custo", "data_compra": "Data de compra",
    "data_inicio": "Data de início", "placa": "Placa", "renavam": "RENAVAM", "chassi_serie": "Chassi / nº de série",
    "localidade": "Fazenda", "setor": "Setor", "area_operacao": "Área de operação", "responsavel": "Responsável",
    "observacoes": "Observações", "exige_conectividade": "Exige chip", "latitude": "Latitude", "longitude": "Longitude",
}


def rotulo_do_campo(campo: str) -> str:
    return ROTULOS_DE_CAMPO.get(campo, campo)


def de_para_do_evento(alteracoes: dict) -> list[dict]:
    """O de/para de um evento de edição ou movimentação como `[{"campo", "rotulo", "de", "para"}]`, por
    rótulo — é o que as telas mostram, e vazio aparece como "—"."""
    return [
        {"campo": campo, "rotulo": rotulo_do_campo(campo), "de": m.get("de") or "—", "para": m.get("para") or "—"}
        for campo, m in sorted((alteracoes or {}).items(), key=lambda item: rotulo_do_campo(item[0]))
    ]


def _resumo_do_evento_de_veiculo(e: FrotaEvento) -> str:
    d = e.dados or {}
    if e.tipo == FrotaEvento.Tipo.CADASTRO:
        if d.get("origem") == "importacao":
            return (
                f"Importado da planilha do ERP como {d.get('codigo_legado') or '—'} · {d.get('nome', '')}"
                f" (identificador interno {d.get('codigo', '')})."
            )
        return f"Cadastrado como {d.get('codigo', '')} · {d.get('nome', '')}."
    if e.tipo == FrotaEvento.Tipo.EDICAO:
        rotulos = sorted(rotulo_do_campo(c) for c in (d.get("alteracoes") or {}))
        texto = "Campos alterados: " + ", ".join(rotulos) + "."
        return texto + (" Atualização pela planilha do ERP." if d.get("origem") == "importacao" else "")
    if e.tipo == FrotaEvento.Tipo.MOVIMENTACAO:
        mudancas = "; ".join(f"{m['rotulo']}: {m['de']} → {m['para']}" for m in de_para_do_evento(d.get("alteracoes")))
        return f"{mudancas}. Motivo: {d['motivo']}" if d.get("motivo") else f"{mudancas}."
    if e.tipo == FrotaEvento.Tipo.VALIDACAO:
        origem = f" (planilha: {d['situacao_origem']})" if d.get("situacao_origem") else ""
        texto = f"Situação confirmada: {dict(Status.choices).get(d.get('status'), d.get('status') or '—')}{origem}."
        return f"{texto} {d['motivo']}" if d.get("motivo") else texto
    if e.tipo == FrotaEvento.Tipo.STATUS:
        rotulos = dict(Status.choices)
        texto = f"De {rotulos.get(d.get('de'), d.get('de') or '—')} para {rotulos.get(d.get('para'), d.get('para') or '—')}."
        return f"{texto} Motivo: {d['motivo']}" if d.get("motivo") else texto
    return ""


resumo_do_evento = _resumo_do_evento_de_veiculo       # público: as telas de movimentação e histórico descrevem o evento igual


def linha_do_tempo_veiculo(veiculo: FrotaVeiculo, tipos=None, limite: int = 100) -> list[dict]:
    """Histórico do veículo, do MAIS NOVO para o mais antigo, misturando as quatro
    fontes. `tipos`: subconjunto de {"evento", "vinculo", "movimentacao", "ocorrencia"} (None =
    todas). Cada item:

        {"quando": datetime, "fonte": "evento"|"vinculo"|"movimentacao"|"ocorrencia", "tipo": slug,
         "titulo": str, "detalhe": str, "nivel": "ok"|"info"|"aviso"|"erro", "ref_id": int}

    `instalacao` são os equipamentos tecnológicos que entraram e saíram da máquina (lidos de
    `TecInstalacao`, com quem e o motivo da saída). `evento` é o cadastro do veículo (criação, edição,
    status, movimentação, validação); `vinculo` são os PERÍODOS
    (início e fim de cada chip, lidos de `FrotaVinculo`); `movimentacao` são as AÇÕES com
    motivo e responsável (lidas de `FrotaMovimentacao`, como origem ou destino); `ocorrencia`
    são os alertas do veículo. Vínculo e movimentação descrevem o mesmo fato por ângulos
    diferentes — a tela escolhe qual mostrar. Nada é duplicado no banco."""
    fontes = set(tipos) if tipos else {"evento", "vinculo", "movimentacao", "ocorrencia", "instalacao"}
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
    if "instalacao" in fontes:
        instalacoes = (
            TecInstalacao.objects.filter(veiculo=veiculo)
            .select_related("equipamento__modelo__fabricante", "equipamento__modelo__categoria", "inicio_por", "fim_por")
            .order_by("-inicio_em", "-pk")[:limite]
        )
        for i in instalacoes:
            nome = f"{i.equipamento.modelo.fabricante.nome} {i.equipamento.modelo.nome}"
            itens.append({
                "quando": i.inicio_em, "fonte": "instalacao", "tipo": "inicio", "titulo": f"{nome} instalado",
                "detalhe": f"{i.equipamento.codigo}. Por {i.inicio_por.get_username()}." if i.inicio_por_id else f"{i.equipamento.codigo}.",
                "nivel": "ok", "ref_id": i.pk,
            })
            if i.fim_em:
                partes = [i.equipamento.codigo + "."]
                if i.fim_por_id:
                    partes.append(f"Por {i.fim_por.get_username()}.")
                if i.motivo_fim:
                    partes.append(f"Motivo: {i.motivo_fim}")
                itens.append({
                    "quando": i.fim_em, "fonte": "instalacao", "tipo": "fim", "titulo": f"{nome} retirado",
                    "detalhe": " ".join(partes), "nivel": "info", "ref_id": i.pk,
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


def indicadores_frota(agora=None, com_api: bool = True) -> dict:
    """Os KPIs da Visão geral, em dois blocos SEPARADOS (frota e conectividade não se
    misturam), com poucas consultas agregadas. `com_api=False` (quem não vê API) devolve só a parte de CADASTRO e VÍNCULO dos
    chips — total, disponíveis, vinculados, em manutenção, inativos — sem `com_sessao_ativa`, `sem_sessao` e `com_falha`, que
    são o que a operadora respondeu (e nem as calcula). Chaves:

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
    da_api = dict(
        com_sessao_ativa=Count("pk", filter=vinculado & _q_estado_do_chip("ativa", limite)),
        sem_sessao=Count("pk", filter=vinculado & _q_estado_do_chip("sem_sessao", limite)),
        com_falha=Count("pk", filter=vinculado & _q_estado_do_chip("falha", limite)),
    ) if com_api else {}
    c = M2MChip.objects.aggregate(
        chips_total=Count("pk"),
        chips_disponiveis=Count("pk", filter=Q(situacao_uso=M2MChip.SituacaoUso.DISPONIVEL)),
        chips_vinculados=Count("pk", filter=vinculado),
        chips_manutencao=Count("pk", filter=Q(situacao_uso=M2MChip.SituacaoUso.MANUTENCAO)),
        chips_inativos=Count("pk", filter=Q(situacao_uso=M2MChip.SituacaoUso.INATIVO)),
        **da_api,
    )
    return {
        "frota": {
            "total": total, "ativos": f["ativos"], "em_manutencao": f["em_manutencao"], "inativos": f["inativos"],
            "desativados": f["desativados"], "com_chip": com_chip, "sem_chip": total - com_chip,
        },
        "conectividade": c,
    }


def area_de_atencao(limite: int = 8, agora=None, com_api: bool = True) -> dict:
    """O que precisa de olho agora (`com_api=False`: os três blocos de chip — sem atualização, falha e pendente — saem VAZIOS, são
    o que a operadora respondeu), cinco blocos `{"total": N, "itens": [...até `limite`]}`:

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
        if not com_api:
            return {"total": 0, "itens": []}
        qs = base.filter(_q_estado_do_chip(estado, limite_data)).order_by(*ordem)
        return {"total": qs.count(), "itens": m2m.anotar_chips(qs[:limite], cfg, agora)}

    sem_chip = m2m.veiculos_sem_chip_exigido().order_by("codigo")
    recentes = FrotaMovimentacao.objects.select_related(
        "chip", "veiculo_origem", "veiculo_destino", "item_origem", "item_destino",
    ).order_by("-em", "-pk")
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


auditar_config = _auditar_config       # público: os outros cadastros de apoio da frota auditam do mesmo jeito


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
    """Cria (`tipo=None`) ou edita um tipo de veículo (a CATEGORIA superior das classes operacionais).
    `dados`: `nome` (único, sem distinguir caixa), `exige_conectividade`, `espera_placa`, `espera_chassi`
    (o que o tipo costuma ter: a falta vira pendência, nunca bloqueio), `ativo` (só as chaves presentes
    mudam na edição; na criação o padrão é não exigir, não esperar nada e ativo). Tipo inativo some do
    cadastro de veículo mas os veículos dele ficam. Audita em `M2MEvento(config_frota)` com o de/para.
    `ValidationError({campo: msg})`."""
    criando = tipo is None
    alvo = FrotaTipo() if criando else tipo
    antes = (
        {
            "nome": alvo.nome, "exige_conectividade": alvo.exige_conectividade, "espera_placa": alvo.espera_placa,
            "espera_chassi": alvo.espera_chassi, "ativo": alvo.ativo,
        }
        if not criando else {}
    )
    novos: dict = {}
    if criando or "nome" in dados:
        novos["nome"] = _nome_unico(FrotaTipo, dados.get("nome"), None if criando else alvo.pk, " tipo de veículo")
    for campo, padrao in (("exige_conectividade", False), ("espera_placa", False), ("espera_chassi", False), ("ativo", True)):
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
