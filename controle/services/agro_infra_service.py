"""
Infraestrutura georreferenciada do Zelo Agro: coordenadas, estruturas (torre, estação, base, ponto de
referência) e enlaces ponto a ponto (PTP).

O que este módulo garante — e por quê:

  • COORDENADA É O QUE ALGUÉM CADASTROU. Latitude e longitude entram aos pares, em graus decimais (aceita
    vírgula), com no máximo 6 casas. Nada aqui calcula posição a partir de outro dado: a TIM não informa onde
    o chip está, e ICCID, sessão e consumo não são posição (regra 63). Coordenada ausente é "localização
    desconhecida", e a tela diz isso em vez de inventar um ponto.
  • COORDENADA IMPLAUSÍVEL É RECUSADA COM O MOTIVO. 0,0 (o "ponto nulo", no oceano), valores fora do globo e
    pontos fora da América do Sul são quase sempre erro de digitação (latitude e longitude trocadas, sinal
    esquecido) e põem a máquina num lugar onde ela não está — pior que não ter ponto.
  • ENLACE É O REGISTRADO, NÃO O PRESUMIDO. `estado` nasce "Não informado" e só muda quando alguém o
    registra (com quem e quando). Rádio cadastrado nas pontas não faz o enlace "operacional". A distância é
    geometria entre as duas coordenadas cadastradas, não medição de qualidade.
  • A GRAVAÇÃO É DO SERVICE. Texto aparado, duplicidade de nome checada sem diferença de caixa e acento,
    `ValidationError({campo: mensagem})` para a tela mostrar no campo certo.
"""

from __future__ import annotations

import math
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.utils import timezone

from ProjetoEstoque.models import Localidade, TecEnlace, TecEquipamento, TecEstrutura
from services import tec_base as base

# América do Sul, com folga: (lat mínima, lat máxima, lon mínima, lon máxima). Pega o erro comum (latitude e
# longitude trocadas, sinal da longitude esquecido) sem recusar nenhum ponto de uma fazenda no Brasil.
CAIXA_PLAUSIVEL = (-56.0, 13.0, -82.0, -34.0)
SEIS_CASAS = Decimal("0.000001")
RAIO_DA_TERRA_KM = 6371.0088


# ════════════════════════════════════════════════════════
# Coordenadas
# ════════════════════════════════════════════════════════

def _decimal(valor, campo: str, rotulo: str) -> Decimal | None:
    if valor in (None, ""):
        return None
    if isinstance(valor, Decimal):
        numero = valor
    else:
        texto = str(valor).strip().replace(",", ".")
        texto = texto.replace("°", "").replace("º", "").strip()
        if not re.fullmatch(r"[+-]?\d{1,3}(\.\d+)?", texto):
            raise ValidationError({campo: f"{rotulo}: informe em graus decimais (ex.: -14,776300)."})
        try:
            numero = Decimal(texto)
        except InvalidOperation:
            raise ValidationError({campo: f"{rotulo}: informe em graus decimais (ex.: -14,776300)."}) from None
    return numero.quantize(SEIS_CASAS, rounding=ROUND_HALF_UP)


def ler_coordenada(latitude, longitude, *, campo_lat: str = "latitude", campo_lon: str = "longitude") -> tuple[Decimal | None, Decimal | None]:
    """`(latitude, longitude)` como `Decimal` de 6 casas, ou `(None, None)` se as duas vierem vazias. Só uma das
    duas é erro (cada coordenada sozinha não localiza nada); 0,0, fora do globo ou fora da América do Sul também.
    `ValidationError({campo: mensagem})` com o campo certo."""
    lat = _decimal(latitude, campo_lat, "Latitude")
    lon = _decimal(longitude, campo_lon, "Longitude")
    if lat is None and lon is None:
        return None, None
    erros = {}
    if lat is None:
        erros[campo_lat] = "Informe a latitude: a longitude sozinha não localiza nada."
    if lon is None:
        erros[campo_lon] = "Informe a longitude: a latitude sozinha não localiza nada."
    if erros:
        raise ValidationError(erros)
    if not Decimal(-90) <= lat <= Decimal(90):
        erros[campo_lat] = "A latitude vai de -90 a 90."
    if not Decimal(-180) <= lon <= Decimal(180):
        erros[campo_lon] = "A longitude vai de -180 a 180."
    if erros:
        raise ValidationError(erros)
    if lat == 0 and lon == 0:
        raise ValidationError({campo_lat: "0, 0 é o ponto nulo (no oceano): quase sempre é um valor que ficou em branco."})
    lat_min, lat_max, lon_min, lon_max = CAIXA_PLAUSIVEL
    if not (lat_min <= float(lat) <= lat_max and lon_min <= float(lon) <= lon_max):
        raise ValidationError({
            campo_lat: "A coordenada cai fora da América do Sul. Confira o sinal e se latitude e longitude não estão trocadas "
                       "(no Brasil, as duas são negativas e a longitude costuma ter o valor maior em módulo).",
        })
    return lat, lon


def distancia_km(a: tuple, b: tuple) -> float:
    """Distância em linha reta (haversine, esfera de 6.371 km) entre `(lat, lon)` e `(lat, lon)`, em km."""
    lat1, lon1, lat2, lon2 = (math.radians(float(x)) for x in (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * RAIO_DA_TERRA_KM * math.asin(min(1.0, math.sqrt(h)))


def distancia_do_enlace_km(enlace: TecEnlace) -> float | None:
    """O comprimento do enlace em km, ou `None` se uma das pontas não tem coordenada."""
    a, b = enlace.estrutura_a, enlace.estrutura_b
    if not (a.tem_coordenada and b.tem_coordenada):
        return None
    return distancia_km((a.latitude, a.longitude), (b.latitude, b.longitude))


# ════════════════════════════════════════════════════════
# Estruturas
# ════════════════════════════════════════════════════════

def _nome_unico(nome: str, excluir_pk=None) -> None:
    from services import busca_service  # noqa: F401  (a busca sem acento é a do resto do sistema)
    repetido = TecEstrutura.objects.filter(nome__iexact=nome)
    if excluir_pk:
        repetido = repetido.exclude(pk=excluir_pk)
    if repetido.exists():
        raise ValidationError({"nome": "Já existe uma estrutura com este nome."})


def _fazenda(valor):
    if valor in (None, "", 0, "0"):
        return None
    if isinstance(valor, Localidade):
        return valor
    try:
        return Localidade.objects.get(pk=int(valor))
    except (ValueError, TypeError, Localidade.DoesNotExist):
        raise ValidationError({"fazenda": "Fazenda não encontrada."}) from None


def _altura(valor):
    if valor in (None, ""):
        return None
    try:
        numero = Decimal(str(valor).strip().replace(",", "."))
    except InvalidOperation:
        raise ValidationError({"altura_m": "Altura: informe um número em metros."}) from None
    if numero < 0 or numero > 400:
        raise ValidationError({"altura_m": "A altura vai de 0 a 400 m."})
    return numero.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def salvar_estrutura(dados: dict, usuario, estrutura: TecEstrutura | None = None) -> TecEstrutura:
    """Cadastra (`estrutura=None`) ou corrige uma estrutura. `dados`: `nome` (obrigatório, único sem diferença de
    caixa), `tipo`, `situacao`, `fazenda`, `latitude`/`longitude` (juntas ou nenhuma), `altura_m`, `observacoes` e, na
    edição, `ativo`. Só as chaves presentes mudam. A hora da localização (`localizacao_em`) é gravada quando a coordenada
    é cadastrada ou muda. `ValidationError({campo: msg})`."""
    usuario = base.exigir_usuario(usuario)
    erros: dict = {}
    atual = estrutura
    valores: dict = {}

    def tentar(nome, fn):
        try:
            fn()
        except ValidationError as exc:
            if hasattr(exc, "error_dict"):
                erros.update({k: " ".join(v) for k, v in exc.message_dict.items()})
            else:
                erros[nome] = " ".join(exc.messages)

    if atual is None or "nome" in dados:
        def nome_():
            nome = base.texto(dados.get("nome"), 120, "nome", "o nome", obrigatorio=True)
            _nome_unico(nome, atual.pk if atual else None)
            valores["nome"] = nome
        tentar("nome", nome_)
    if "tipo" in dados or atual is None:
        tipo = dados.get("tipo") or TecEstrutura.Tipo.TORRE
        if tipo in TecEstrutura.Tipo.values:
            valores["tipo"] = tipo
        else:
            erros["tipo"] = "Escolha o tipo da estrutura."
    if "situacao" in dados:
        if dados["situacao"] in TecEstrutura.Situacao.values:
            valores["situacao"] = dados["situacao"]
        else:
            erros["situacao"] = "Escolha a situação."
    if "fazenda" in dados:
        tentar("fazenda", lambda: valores.__setitem__("fazenda", _fazenda(dados["fazenda"])))
    if "altura_m" in dados:
        tentar("altura_m", lambda: valores.__setitem__("altura_m", _altura(dados["altura_m"])))
    if "observacoes" in dados:
        tentar("observacoes", lambda: valores.__setitem__("observacoes", base.texto_livre(dados["observacoes"], "observacoes", "as observações")))
    if "ativo" in dados and atual is not None:
        valores["ativo"] = base.booleano(dados["ativo"], atual.ativo)
    if "latitude" in dados or "longitude" in dados:
        def coordenada():
            lat = dados.get("latitude", atual.latitude if atual else None)
            lon = dados.get("longitude", atual.longitude if atual else None)
            valores["latitude"], valores["longitude"] = ler_coordenada(lat, lon)
        tentar("latitude", coordenada)
    if erros:
        raise ValidationError(erros)

    agora = timezone.now()
    if atual is None:
        if valores.get("latitude") is not None:
            valores["localizacao_em"] = agora
        try:
            with transaction.atomic():
                return TecEstrutura.objects.create(**valores, criado_por=usuario, atualizado_por=usuario)
        except IntegrityError:
            raise ValidationError({"nome": "Já existe uma estrutura com este nome."}) from None
    mudou = {c: v for c, v in valores.items() if getattr(atual, c) != v}
    if not mudou:
        return atual
    if "latitude" in mudou or "longitude" in mudou:
        mudou["localizacao_em"] = agora
    for campo, valor in mudou.items():
        setattr(atual, campo, valor)
    atual.atualizado_por = usuario
    try:
        with transaction.atomic():
            atual.save(update_fields=[*mudou, "atualizado_por", "updated_at"])
    except IntegrityError:
        raise ValidationError({"nome": "Já existe uma estrutura com este nome."}) from None
    return atual


def listar_estruturas(filtros: dict | None = None):
    """Estruturas filtradas: `q` (nome ou observações), `tipo`, `situacao`, `fazenda` (id), `coordenada`
    (`com`|`sem`) e `ativo` (`1` só ativas, `0` só inativas; vazio = todas)."""
    filtros = filtros or {}
    qs = TecEstrutura.objects.select_related("fazenda")
    q = (filtros.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(nome__icontains=q) | Q(observacoes__icontains=q))
    if filtros.get("tipo") in TecEstrutura.Tipo.values:
        qs = qs.filter(tipo=filtros["tipo"])
    if filtros.get("situacao") in TecEstrutura.Situacao.values:
        qs = qs.filter(situacao=filtros["situacao"])
    if str(filtros.get("fazenda") or "").isdigit():
        qs = qs.filter(fazenda_id=int(filtros["fazenda"]))
    if filtros.get("coordenada") == "com":
        qs = qs.filter(latitude__isnull=False)
    elif filtros.get("coordenada") == "sem":
        qs = qs.filter(latitude__isnull=True)
    if filtros.get("ativo") in ("0", "1"):
        qs = qs.filter(ativo=filtros["ativo"] == "1")
    return qs.order_by("nome")


# ════════════════════════════════════════════════════════
# Enlaces PTP
# ════════════════════════════════════════════════════════

def _estrutura_para_enlace(valor, campo: str, rotulo: str, atual=None) -> TecEstrutura:
    if valor in (None, "", 0, "0"):
        raise ValidationError({campo: f"Escolha a {rotulo}."})
    try:
        estrutura = valor if isinstance(valor, TecEstrutura) else TecEstrutura.objects.get(pk=int(valor))
    except (ValueError, TypeError, TecEstrutura.DoesNotExist):
        raise ValidationError({campo: f"{rotulo.capitalize()}: estrutura não encontrada."}) from None
    if not estrutura.ativo and (atual is None or atual.pk != estrutura.pk):
        raise ValidationError({campo: f"A estrutura “{estrutura.nome}” está inativa."})
    return estrutura


def _radio(valor, campo: str, atual=None):
    if valor in (None, "", 0, "0"):
        return None
    try:
        equipamento = valor if isinstance(valor, TecEquipamento) else TecEquipamento.objects.select_related("modelo").get(pk=int(valor))
    except (ValueError, TypeError, TecEquipamento.DoesNotExist):
        raise ValidationError({campo: "Equipamento não encontrado."}) from None
    if equipamento.situacao == TecEquipamento.Situacao.INATIVO and (atual is None or atual.pk != equipamento.pk):
        raise ValidationError({campo: f"O equipamento {equipamento.codigo} está inativo."})
    return equipamento


def salvar_enlace(dados: dict, usuario, enlace: TecEnlace | None = None) -> TecEnlace:
    """Cadastra ou corrige um enlace PTP. `dados`: `identificacao` (única), `estrutura_a`, `estrutura_b` (diferentes),
    `equipamento_a`/`equipamento_b` (o rádio de cada ponta, opcionais e diferentes), `estado`, `frequencia_ghz`,
    `observacoes` e, na edição, `ativo`. O `estado` só ganha data e autor (`estado_em`, `estado_por`) quando muda
    de verdade. `ValidationError({campo: msg})`."""
    usuario = base.exigir_usuario(usuario)
    erros: dict = {}
    valores: dict = {}

    def tentar(nome, fn):
        try:
            fn()
        except ValidationError as exc:
            if hasattr(exc, "error_dict"):
                erros.update({k: " ".join(v) for k, v in exc.message_dict.items()})
            else:
                erros[nome] = " ".join(exc.messages)

    if enlace is None or "identificacao" in dados:
        def identificacao():
            texto = base.texto(dados.get("identificacao"), 80, "identificacao", "a identificação do enlace", obrigatorio=True)
            repetido = TecEnlace.objects.filter(identificacao__iexact=texto)
            if enlace:
                repetido = repetido.exclude(pk=enlace.pk)
            if repetido.exists():
                raise ValidationError({"identificacao": "Já existe um enlace com esta identificação."})
            valores["identificacao"] = texto
        tentar("identificacao", identificacao)
    for chave, rotulo in (("estrutura_a", "ponta A"), ("estrutura_b", "ponta B")):
        if enlace is None or chave in dados:
            tentar(chave, lambda c=chave, r=rotulo: valores.__setitem__(
                c, _estrutura_para_enlace(dados.get(c), c, r, getattr(enlace, c) if enlace else None)))
    for chave in ("equipamento_a", "equipamento_b"):
        if chave in dados:
            tentar(chave, lambda c=chave: valores.__setitem__(c, _radio(dados.get(c), c, getattr(enlace, c) if enlace else None)))
    a = valores.get("estrutura_a", enlace.estrutura_a if enlace else None)
    b = valores.get("estrutura_b", enlace.estrutura_b if enlace else None)
    if a is not None and b is not None and a.pk == b.pk:
        erros["estrutura_b"] = "As duas pontas do enlace precisam ser estruturas diferentes."
    ra = valores.get("equipamento_a", enlace.equipamento_a if enlace else None)
    rb = valores.get("equipamento_b", enlace.equipamento_b if enlace else None)
    if ra is not None and rb is not None and ra.pk == rb.pk:
        erros["equipamento_b"] = "O mesmo rádio não pode estar nas duas pontas."
    if "estado" in dados:
        if dados["estado"] in TecEnlace.Estado.values:
            valores["estado"] = dados["estado"]
        else:
            erros["estado"] = "Escolha o estado do enlace."
    if "frequencia_ghz" in dados:
        def frequencia():
            bruto = dados["frequencia_ghz"]
            if bruto in (None, ""):
                valores["frequencia_ghz"] = None
                return
            try:
                numero = Decimal(str(bruto).strip().replace(",", "."))
            except InvalidOperation:
                raise ValidationError({"frequencia_ghz": "Frequência: informe um número em GHz (ex.: 5,8)."}) from None
            if not Decimal("0.1") <= numero <= Decimal("100"):
                raise ValidationError({"frequencia_ghz": "A frequência vai de 0,1 a 100 GHz."})
            valores["frequencia_ghz"] = numero.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        tentar("frequencia_ghz", frequencia)
    if "observacoes" in dados:
        tentar("observacoes", lambda: valores.__setitem__("observacoes", base.texto_livre(dados["observacoes"], "observacoes", "as observações")))
    if "ativo" in dados and enlace is not None:
        valores["ativo"] = base.booleano(dados["ativo"], enlace.ativo)
    if erros:
        raise ValidationError(erros)

    agora = timezone.now()
    if enlace is None:
        estado = valores.get("estado", TecEnlace.Estado.NAO_INFORMADO)
        extra = {}
        if estado != TecEnlace.Estado.NAO_INFORMADO:
            extra = {"estado_em": agora, "estado_por": usuario}
        try:
            with transaction.atomic():
                return TecEnlace.objects.create(**valores, **extra, criado_por=usuario, atualizado_por=usuario)
        except IntegrityError:
            raise ValidationError({"identificacao": "Já existe um enlace com esta identificação."}) from None
    mudou = {c: v for c, v in valores.items() if getattr(enlace, c) != v}
    if not mudou:
        return enlace
    if "estado" in mudou:
        mudou["estado_em"], mudou["estado_por"] = agora, usuario
    for campo, valor in mudou.items():
        setattr(enlace, campo, valor)
    enlace.atualizado_por = usuario
    try:
        with transaction.atomic():
            enlace.save(update_fields=[*mudou, "atualizado_por", "updated_at"])
    except IntegrityError:
        raise ValidationError({"identificacao": "Já existe um enlace com esta identificação."}) from None
    return enlace


def listar_enlaces(filtros: dict | None = None):
    """Enlaces filtrados: `q` (identificação, nome de uma das pontas), `estado`, `estrutura` (id de uma das
    pontas), `fazenda` (id da fazenda de uma das pontas) e `ativo`."""
    filtros = filtros or {}
    qs = TecEnlace.objects.select_related("estrutura_a", "estrutura_b", "equipamento_a__modelo", "equipamento_b__modelo", "estado_por")
    q = (filtros.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(identificacao__icontains=q) | Q(estrutura_a__nome__icontains=q) | Q(estrutura_b__nome__icontains=q))
    if filtros.get("estado") in TecEnlace.Estado.values:
        qs = qs.filter(estado=filtros["estado"])
    if str(filtros.get("estrutura") or "").isdigit():
        eid = int(filtros["estrutura"])
        qs = qs.filter(Q(estrutura_a_id=eid) | Q(estrutura_b_id=eid))
    if str(filtros.get("fazenda") or "").isdigit():
        fid = int(filtros["fazenda"])
        qs = qs.filter(Q(estrutura_a__fazenda_id=fid) | Q(estrutura_b__fazenda_id=fid))
    if filtros.get("ativo") in ("0", "1"):
        qs = qs.filter(ativo=filtros["ativo"] == "1")
    return qs.order_by("identificacao")


# ════════════════════════════════════════════════════════
# Indicadores
# ════════════════════════════════════════════════════════

def indicadores_infra() -> dict:
    """Os números do dashboard de infraestrutura — todos contados do cadastro, nenhum estimado:

        torres, estacoes, bases, referencias   estruturas ATIVAS por tipo
        sem_coordenada                         estruturas ativas sem localização
        enlaces                                enlaces ativos
        enlaces_por_estado                     `{estado: n}` (só os enlaces ativos)
        operacionais, manutencao, indisponiveis, planejados, nao_informados
        equipamentos_instalados, equipamentos_disponiveis   do estoque tecnológico (todos os equipamentos)
    "Operacional" é o estado REGISTRADO no enlace; ter rádio cadastrado nas pontas não conta."""
    por_tipo = dict(TecEstrutura.objects.filter(ativo=True).values_list("tipo").annotate(n=Count("pk")))
    por_estado = dict(TecEnlace.objects.filter(ativo=True).values_list("estado").annotate(n=Count("pk")))
    equipamentos = dict(TecEquipamento.objects.values_list("situacao").annotate(n=Count("pk")))
    E = TecEnlace.Estado
    return {
        "torres": por_tipo.get(TecEstrutura.Tipo.TORRE, 0),
        "estacoes": por_tipo.get(TecEstrutura.Tipo.ESTACAO, 0),
        "bases": por_tipo.get(TecEstrutura.Tipo.BASE, 0),
        "referencias": por_tipo.get(TecEstrutura.Tipo.REFERENCIA, 0),
        "sem_coordenada": TecEstrutura.objects.filter(ativo=True, latitude__isnull=True).count(),
        "enlaces": sum(por_estado.values()),
        "enlaces_por_estado": por_estado,
        "operacionais": por_estado.get(E.OPERACIONAL, 0),
        "manutencao": por_estado.get(E.MANUTENCAO, 0),
        "indisponiveis": por_estado.get(E.INDISPONIVEL, 0),
        "planejados": por_estado.get(E.PLANEJADO, 0),
        "nao_informados": por_estado.get(E.NAO_INFORMADO, 0),
        "equipamentos_instalados": equipamentos.get(TecEquipamento.Situacao.INSTALADO, 0),
        "equipamentos_disponiveis": equipamentos.get(TecEquipamento.Situacao.DISPONIVEL, 0),
    }
