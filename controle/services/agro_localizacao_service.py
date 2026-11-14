"""
A localização GPS dos veículos do Zelo Agro: o que se sabe, de onde vem e o quanto ainda vale.

O ponto de partida é um fato que molda tudo aqui: **não existe, hoje, nenhuma fonte de GPS de veículo ligada ao sistema.**
A TIM (`/simcards/…`) não informa posição — ICCID, IMSI, sessão e consumo NÃO são localização — e a única telemetria com
coordenadas que o sistema recebe é a dos celulares do Quiosque, que são aparelhos corporativos e não veículos. Então:

  • NENHUMA POSIÇÃO É INVENTADA NEM DEDUZIDA. Um veículo sem coordenada está "localização indisponível": não vira ponto
    no mapa, não é posicionado perto de outro, não herda a posição do chip. Coordenada cadastrada à mão é dita assim
    ("fixa — não é a posição atual da máquina") e nunca é tratada como GPS.
  • A ESTRUTURA PARA A FONTE FUTURA ESTÁ PRONTA. `registrar_posicao` é a ÚNICA porta de entrada de uma integração de
    rastreamento (telemetria, rastreador, plataforma de máquinas): valida, grava o histórico imutável (`FrotaPosicao`),
    é idempotente por (veículo, fonte, instante) e atualiza o espelho "posição atual" do veículo só quando a leitura é
    mais nova que a que já vale. Nada chama isto hoje; conectar uma fonte é chamar esta função.
  • QUATRO ESTADOS, E UM LIMITE QUE O TI DECIDE. GPS atualizado (até `minutos_atualizada`), antigo (daí até
    `horas_sem_atualizacao`), sem atualização recente (depois disso) e indisponível. Os limites são
    configuráveis (`FrotaLocalizacaoConfig`) porque cada fonte entrega num ritmo — e são INDEPENDENTES da sincronização
    de consumo da TIM: uma não atrasa nem esconde a outra.
  • A POSIÇÃO É DO VEÍCULO; O CHIP É LIDO. "Onde está o chip" é "onde está o veículo onde ele está instalado AGORA"
    (o vínculo ativo). Nada é copiado para o chip, então uma transferência leva a localização atual junto com o vínculo
    no instante em que ele muda — e o histórico de posições do veículo anterior fica com o veículo anterior.
  • PRIVACIDADE. Quem decide o que cada pessoa vê é a camada de cima (`ver_frota` para a posição do veículo): este módulo
    não olha permissão, mas tudo o que devolve é montado a partir do cadastro, sem chamada externa.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import OuterRef, Subquery
from django.urls import reverse
from django.utils import timezone

from ProjetoEstoque.models import FrotaEvento, FrotaLocalizacaoConfig, FrotaPosicao, FrotaVeiculo, FrotaVinculo
from services import agro_infra_service as infra

# ── os estados ───────────────────────────────────────────────────────────
ESTADOS = ("atualizada", "antiga", "sem_atualizacao", "cadastral", "indisponivel")
ROTULO_DO_ESTADO = {
    "atualizada": "Localização GPS atualizada",
    "antiga": "Localização GPS antiga",
    "sem_atualizacao": "Localização GPS sem atualização recente",
    "cadastral": "Coordenada cadastral (fixa)",
    "indisponivel": "Localização indisponível",
}
ROTULO_DA_ORIGEM = {
    "cadastral": "Coordenada cadastral fixa",
    "gps": "GPS",
    "estrutura": "Localização da estrutura",
}
TOLERANCIA_DE_RELOGIO = timedelta(minutes=5)     # posição "do futuro" além disto é erro da fonte, não adiantamento
LIMITE_MINUTOS = (1, 24 * 60)
LIMITE_HORAS = (1, 24 * 30)
FONTE_PADRAO_DO_GANCHO_ANTIGO = "integração GPS"


# ═════════════════════════════════════════════════════════════════════════
# Configuração dos limites
# ═════════════════════════════════════════════════════════════════════════

def limites() -> dict:
    cfg = FrotaLocalizacaoConfig.get()
    return {"minutos_atualizada": cfg.minutos_atualizada, "horas_sem_atualizacao": cfg.horas_sem_atualizacao}


def salvar_configuracao(dados: dict, usuario=None) -> FrotaLocalizacaoConfig:
    """Grava os dois limites (inteiros): `minutos_atualizada` (1 a 1440) e `horas_sem_atualizacao` (1 a 720), e o
    segundo tem de ser MAIOR que o primeiro (senão a faixa "antiga" não existe). Audita o de/para em
    `M2MEvento(config_frota)`. `ValidationError({campo: msg})`."""
    from services import frota_service

    cfg = FrotaLocalizacaoConfig.get()
    erros: dict = {}

    def inteiro(campo, rotulo, faixa, atual):
        bruto = dados.get(campo, atual)
        try:
            valor = int(str(bruto).strip())
        except (TypeError, ValueError):
            erros[campo] = f"{rotulo}: informe um número inteiro."
            return atual
        if not faixa[0] <= valor <= faixa[1]:
            erros[campo] = f"{rotulo}: use de {faixa[0]} a {faixa[1]}."
        return valor

    minutos = inteiro("minutos_atualizada", "Minutos até a posição deixar de ser atual", LIMITE_MINUTOS, cfg.minutos_atualizada)
    horas = inteiro("horas_sem_atualizacao", "Horas até a localização ficar sem atualização recente", LIMITE_HORAS, cfg.horas_sem_atualizacao)
    if not erros and horas * 60 <= minutos:
        erros["horas_sem_atualizacao"] = "O limite de \"sem atualização recente\" tem de ser maior que o de \"atual\"."
    if erros:
        raise ValidationError(erros)
    alteracoes = {
        c: {"de": getattr(cfg, c), "para": v}
        for c, v in (("minutos_atualizada", minutos), ("horas_sem_atualizacao", horas)) if getattr(cfg, c) != v
    }
    if not alteracoes:
        return cfg
    with transaction.atomic():
        cfg.minutos_atualizada, cfg.horas_sem_atualizacao = minutos, horas
        cfg.atualizado_em = timezone.now()
        cfg.atualizado_por = usuario if getattr(usuario, "pk", None) else None
        cfg.save()
        frota_service.auditar_config("localizacao_gps", cfg.pk, usuario, alteracoes)
    return cfg


# ═════════════════════════════════════════════════════════════════════════
# Como a localização deve ser LIDA
# ═════════════════════════════════════════════════════════════════════════

def _rotulo_de_idade(idade: timedelta) -> str:
    minutos = max(0, int(idade.total_seconds() // 60))
    if minutos < 1:
        return "agora há pouco"
    if minutos < 60:
        return f"há {minutos} min"
    if minutos < 48 * 60:
        return f"há {minutos // 60} h"
    return f"há {minutos // 1440} dias"


def classificar(origem: str, em, agora=None, lim: dict | None = None) -> dict:
    """O estado da localização de um ponto: `{"estado", "estado_rotulo", "origem", "rotulo", "desatualizada",
    "tem_coordenada", "aviso"}`.

    `gps`: pela idade da última posição medida — atualizada (até `minutos_atualizada`), antiga (até
    `horas_sem_atualizacao`) ou sem atualização recente. `cadastral`: coordenada fixa, que NUNCA é chamada de atual.
    Qualquer outra origem (vazia): indisponível. `rotulo` é a leitura curta da ORIGEM (e do "informação desatualizada",
    que as telas já mostravam); `estado_rotulo` é o nome do estado, com as palavras da operação."""
    agora = agora or timezone.now()
    lim = lim or limites()
    if origem == "gps" and em is not None:
        idade = agora - em
        if idade <= timedelta(minutes=lim["minutos_atualizada"]):
            estado, aviso = "atualizada", ""
        elif idade <= timedelta(hours=lim["horas_sem_atualizacao"]):
            estado, aviso = "antiga", f"Última posição recebida {_rotulo_de_idade(idade)}: o veículo pode já ter se movido."
        else:
            estado, aviso = "sem_atualizacao", f"Última posição recebida há mais de {lim['horas_sem_atualizacao']} h."
        return {
            "estado": estado, "estado_rotulo": ROTULO_DO_ESTADO[estado], "origem": "gps",
            "rotulo": ROTULO_DA_ORIGEM["gps"] + (" — informação desatualizada" if estado == "sem_atualizacao" else ""),
            "desatualizada": estado == "sem_atualizacao", "tem_coordenada": True, "aviso": aviso,
        }
    if origem == "cadastral":
        return {
            "estado": "cadastral", "estado_rotulo": ROTULO_DO_ESTADO["cadastral"], "origem": "cadastral",
            "rotulo": ROTULO_DA_ORIGEM["cadastral"], "desatualizada": False, "tem_coordenada": True,
            "aviso": "É o ponto cadastrado, não a posição atual da máquina.",
        }
    return {
        "estado": "indisponivel", "estado_rotulo": ROTULO_DO_ESTADO["indisponivel"], "origem": "",
        "rotulo": "Localização desconhecida", "desatualizada": False, "tem_coordenada": False,
        "aviso": "Este equipamento não tem coordenada nem fonte de GPS: nada é desenhado no mapa nem deduzido.",
    }


# ═════════════════════════════════════════════════════════════════════════
# Registrar uma posição (a porta de entrada de uma integração de rastreamento)
# ═════════════════════════════════════════════════════════════════════════

def _numero_opcional(valor, campo: str, rotulo: str, minimo, maximo, casas: int = 1, inteiro: bool = False):
    if valor in (None, ""):
        return None
    try:
        numero = Decimal(str(valor).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        raise ValidationError({campo: f"{rotulo}: informe um número."}) from None
    if not numero.is_finite() or not minimo <= numero <= maximo:
        raise ValidationError({campo: f"{rotulo}: use de {minimo} a {maximo}."})
    return int(numero) if inteiro else numero.quantize(Decimal(1).scaleb(-casas))


def registrar_posicao(
    veiculo: FrotaVeiculo, latitude, longitude, *, fonte: str, medida_em=None, precisao_m=None, velocidade_kmh=None,
    direcao_graus=None, agora=None,
) -> FrotaPosicao:
    """Grava uma posição GPS que uma FONTE entregou para o veículo e devolve a linha do histórico (a que já existia, se
    esta mesma leitura — veículo, fonte e instante — já tinha chegado: reenviar não duplica nem mexe em nada).

    Recusa, com o motivo: coordenada inválida ou implausível (`agro_infra_service.ler_coordenada`: fora da América do Sul,
    0,0, latitude/longitude trocadas), fonte vazia, instante no futuro (além de 5 min de tolerância), direção fora de
    0–359, velocidade ou precisão negativas. O histórico é SEMPRE gravado; o espelho "posição atual" do veículo
    (`latitude`, `longitude`, `localizacao_origem="gps"`, `localizacao_em`) só muda se esta leitura é mais nova que a
    que já vale — uma resposta atrasada de um rastreador não volta o veículo no tempo. Na primeira posição GPS de um
    veículo que tinha coordenada cadastrada, o de/para fica no histórico do veículo (`FrotaEvento`): o ponto cadastrado
    deixa de ser o que o mapa mostra, mas não some do registro."""
    lat, lon = infra.ler_coordenada(latitude, longitude)
    if lat is None:
        raise ValidationError({"latitude": "Informe latitude e longitude."})
    fonte = " ".join(str(fonte or "").split())
    if not fonte:
        raise ValidationError({"fonte": "Informe a fonte da posição (o sistema ou rastreador que a entregou)."})
    if len(fonte) > 60:
        raise ValidationError({"fonte": "A fonte aceita até 60 caracteres."})
    agora = agora or timezone.now()
    medida_em = medida_em or agora
    if medida_em > agora + TOLERANCIA_DE_RELOGIO:
        raise ValidationError({"medida_em": "A hora da posição está no futuro: confira o relógio da fonte."})
    precisao = _numero_opcional(precisao_m, "precisao_m", "Precisão (m)", Decimal(0), Decimal(100000))
    velocidade = _numero_opcional(velocidade_kmh, "velocidade_kmh", "Velocidade (km/h)", Decimal(0), Decimal(500))
    direcao = _numero_opcional(direcao_graus, "direcao_graus", "Direção (graus)", Decimal(0), Decimal(359), inteiro=True)

    with transaction.atomic():
        atual = FrotaVeiculo.objects.select_for_update().get(pk=veiculo.pk)
        ativos = list(FrotaVinculo.objects.filter(veiculo=atual, fim_em__isnull=True).values_list("chip_id", flat=True)[:2])
        posicao, criada = FrotaPosicao.objects.get_or_create(
            veiculo=atual, fonte=fonte, medida_em=medida_em,
            defaults={
                "chip_id": ativos[0] if len(ativos) == 1 else None,       # a foto do chip: só quando era exatamente um
                "latitude": lat, "longitude": lon, "precisao_m": precisao, "velocidade_kmh": velocidade,
                "direcao_graus": direcao, "recebida_em": agora,
            },
        )
        if criada and (atual.localizacao_origem != "gps" or atual.localizacao_em is None or medida_em >= atual.localizacao_em):
            if atual.localizacao_origem != "gps" and atual.latitude is not None:
                FrotaEvento.objects.create(
                    veiculo=atual, tipo=FrotaEvento.Tipo.EDICAO, usuario=None,
                    dados={
                        "alteracoes": {
                            "latitude": {"de": str(atual.latitude), "para": str(lat)},
                            "longitude": {"de": str(atual.longitude), "para": str(lon)},
                        },
                        "origem": "gps", "fonte": fonte,
                    },
                )
            FrotaVeiculo.objects.filter(pk=atual.pk).update(
                latitude=lat, longitude=lon, localizacao_origem="gps", localizacao_em=medida_em,
            )
    veiculo.refresh_from_db()
    return posicao


# ═════════════════════════════════════════════════════════════════════════
# Leitura: o veículo, o chip e o histórico
# ═════════════════════════════════════════════════════════════════════════

def _quando(momento, agora) -> dict:
    if not momento:
        return {"texto": "", "rotulo": "", "iso": ""}
    return {
        "texto": timezone.localtime(momento).strftime("%d/%m/%Y %H:%M"),
        "rotulo": _rotulo_de_idade(agora - momento), "iso": momento.isoformat(),
    }


def ultimas_posicoes(veiculo_ids) -> dict[int, FrotaPosicao]:
    """A posição mais recente de cada veículo (`{veiculo_id: FrotaPosicao}`) numa consulta só."""
    ids = list(veiculo_ids)
    if not ids:
        return {}
    ultima = FrotaPosicao.objects.filter(veiculo=OuterRef("veiculo")).order_by("-medida_em", "-pk").values("pk")[:1]
    return {p.veiculo_id: p for p in FrotaPosicao.objects.filter(veiculo_id__in=ids, pk=Subquery(ultima))}


def localizacao_do_veiculo(veiculo: FrotaVeiculo, agora=None, lim: dict | None = None, ultima: FrotaPosicao | None = None) -> dict:
    """A localização de UM veículo para as telas (detalhe, chip): o estado (`classificar`) mais coordenada, quando foi, de
    onde veio e — só com GPS — fonte, precisão, velocidade e direção da última posição. Sem coordenada devolve o
    estado `indisponivel` e `lat`/`lon` `None`: a tela diz "indisponível" e não desenha nada."""
    agora = agora or timezone.now()
    info = classificar(veiculo.localizacao_origem if veiculo.latitude is not None else "", veiculo.localizacao_em, agora, lim)
    info.update({
        "lat": float(veiculo.latitude) if veiculo.latitude is not None else None,
        "lon": float(veiculo.longitude) if veiculo.longitude is not None else None,
        "em": _quando(veiculo.localizacao_em, agora),
        "fonte": "", "precisao_m": None, "velocidade_kmh": None, "direcao_graus": None,
        "veiculo_id": veiculo.pk,
        "url_mapa": url_do_mapa(veiculo.pk) if veiculo.latitude is not None else "",
    })
    if info["origem"] == "gps":
        if ultima is None:
            ultima = FrotaPosicao.objects.filter(veiculo=veiculo).order_by("-medida_em", "-pk").first()
        if ultima is not None:
            info.update({
                "fonte": ultima.fonte,
                "precisao_m": float(ultima.precisao_m) if ultima.precisao_m is not None else None,
                "velocidade_kmh": float(ultima.velocidade_kmh) if ultima.velocidade_kmh is not None else None,
                "direcao_graus": ultima.direcao_graus,
            })
    return info


def historico_do_veiculo(veiculo: FrotaVeiculo, limite: int = 10, agora=None) -> list[dict]:
    """As últimas posições GPS do veículo (mais novas primeiro). Fica com o veículo mesmo quando o chip muda de lugar."""
    agora = agora or timezone.now()
    posicoes = FrotaPosicao.objects.filter(veiculo=veiculo).select_related("chip").order_by("-medida_em", "-pk")[:limite]
    return [
        {
            "lat": float(p.latitude), "lon": float(p.longitude), "em": _quando(p.medida_em, agora), "fonte": p.fonte,
            "precisao_m": float(p.precisao_m) if p.precisao_m is not None else None,
            "velocidade_kmh": float(p.velocidade_kmh) if p.velocidade_kmh is not None else None,
            "direcao_graus": p.direcao_graus, "chip": p.chip.nome if p.chip_id else "",
        }
        for p in posicoes
    ]


def localizacao_do_chip(chip, agora=None, lim: dict | None = None) -> dict:
    """Onde está o chip: a localização do veículo onde ele está instalado AGORA (vínculo ativo). Devolve
    `{"vinculo": "veiculo"|"item"|"nenhum", "veiculo", "localizacao", "mensagem"}`. Chip em tablet/celular de TI ou sem
    vínculo não tem fonte de GPS neste módulo, e a resposta diz isso em vez de inventar uma posição."""
    vinculo = FrotaVinculo.objects.filter(chip=chip, fim_em__isnull=True).select_related(
        "veiculo", "veiculo__tipo", "veiculo__classe_operacional", "veiculo__localidade", "item",
    ).first()
    if vinculo is None:
        return {"vinculo": "nenhum", "veiculo": None, "localizacao": None, "mensagem": "O chip não está vinculado a nenhum equipamento: não há o que localizar."}
    if vinculo.item_id:
        return {
            "vinculo": "item", "veiculo": None, "localizacao": None,
            "mensagem": "O chip está num equipamento do estoque de TI (tablet ou celular): o Zelo Agro não tem fonte de GPS para ele.",
        }
    return {"vinculo": "veiculo", "veiculo": vinculo.veiculo, "localizacao": localizacao_do_veiculo(vinculo.veiculo, agora, lim), "mensagem": ""}


def url_do_mapa(veiculo_id: int) -> str:
    """O mapa com as três camadas (veículos, estruturas e enlaces) e o veículo em foco: o destaque e o centro vêm do
    parâmetro `veiculo`, os mesmos `camada=` de sempre continuam valendo."""
    return reverse("agro_mapa") + f"?camada=veiculos&camada=estruturas&camada=enlaces&veiculo={int(veiculo_id)}"
