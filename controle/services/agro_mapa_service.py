"""
Os dados do mapa do Zelo Agro: veículos e máquinas, estruturas (torres, estações, bases, pontos de referência) e
enlaces PTP — só o que tem coordenada CADASTRADA ou entregue por uma fonte de GPS, e dizendo de onde ela vem.

Regras que moldam este módulo (e não são óbvias olhando o código):

  • NENHUMA POSIÇÃO É INVENTADA. A TIM (`/simcards/…`) não informa localização, e ICCID, sessão e consumo não são
    posição (regra 63). Um ponto no mapa é uma coordenada que alguém cadastrou (`cadastral`) ou que um rastreador
    entregou (`gps`, pela porta `agro_localizacao_service.registrar_posicao`). Quem não tem coordenada NÃO aparece no
    mapa: entra na contagem de "sem localização", com o motivo — e, quando o mapa é aberto a partir de um chip, o
    `foco` diz "localização indisponível" em vez de levar a lugar nenhum.
  • CADA PONTO DIZ A ORIGEM, A DATA E O ESTADO. GPS atualizado, GPS antigo, GPS sem atualização recente e coordenada
    cadastral fixa são quatro coisas diferentes (`agro_localizacao_service.classificar`, com limites configuráveis) e o
    mapa as desenha diferente: uma coordenada antiga nunca é apresentada como a posição atual.
  • O ÍCONE É DA BIBLIOTECA CENTRAL. O mapa só repassa o id (`icone_service`): a tabela "tipo → desenho" não mora mais
    aqui, para o trator do mapa ser o mesmo trator da lista e da ficha.
  • A PERMISSÃO DECIDE O QUE SAI. Veículos só com `ver_frota`; estruturas e enlaces só com `ver_tec_estoque`; ICCID,
    estado e atividade dos chips só com `ver_chips`. O JSON do mapa é montado aqui, já filtrado: esconder uma camada
    no JavaScript nunca é autorização.
  • NENHUMA CONSULTA POR PONTO. Veículos vêm de `frota_service.listar_veiculos` (vínculos já carregados) e o resto de
    consultas agregadas (última posição GPS, última atividade dos chips); o custo do mapa não cresce com o número de
    marcadores.
"""

from __future__ import annotations

from django.urls import reverse
from django.utils import timezone

from ProjetoEstoque.models import FrotaVeiculo, TecEnlace, TecEstrutura
from services import agro_infra_service as infra
from services import agro_localizacao_service as localizacao
from services import frota_service, icone_service

LIMITE_DE_VEICULOS = 5000            # teto do que uma resposta carrega: o mapa de milhares de pontos já é ilegível

# Compatibilidade: estes nomes moravam aqui antes da localização ganhar serviço próprio.
ROTULO_DA_ORIGEM = localizacao.ROTULO_DA_ORIGEM
situacao_da_localizacao = lambda origem, em, agora=None, lim=None: localizacao.classificar(origem, em, agora, lim)  # noqa: E731


def registrar_posicao_gps(veiculo: FrotaVeiculo, latitude, longitude, em=None, fonte: str = localizacao.FONTE_PADRAO_DO_GANCHO_ANTIGO):
    """O gancho antigo de uma integração de rastreamento. Hoje só repassa para `agro_localizacao_service.registrar_posicao`
    (a porta de entrada, com histórico, fonte e validações); existe para quem já o chamava."""
    localizacao.registrar_posicao(veiculo, latitude, longitude, fonte=fonte, medida_em=em)
    return veiculo


def _quando(momento, agora) -> dict:
    return localizacao._quando(momento, agora)


# ── filtros ──
FILTROS_DE_VEICULO = ("q", "tipo", "localidade", "status", "fabricante", "conectividade", "natureza", "chip")
FOCO = "veiculo"                       # `?veiculo=<id>`: destacar e centralizar (NÃO é filtro: o veículo aparece mesmo fora dos filtros)


def opcoes_dos_filtros(*, ver_frota: bool, ver_infra: bool, ver_api: bool = True) -> dict:
    """O que os selects de filtro oferecem — só o que a pessoa pode ver. A situação da conectividade é o que a API disse:
    sem `ver_api`, o filtro não existe."""
    from ProjetoEstoque.models import FrotaTipo

    opcoes: dict = {
        "fazendas": [(f.pk, f.local) for f in frota_service.fazendas()],
        "tipos_estrutura": list(TecEstrutura.Tipo.choices),
        "estados_enlace": list(TecEnlace.Estado.choices),
    }
    if ver_frota:
        opcoes["tipos"] = [(t.pk, t.nome) for t in FrotaTipo.objects.filter(ativo=True).order_by("nome")]
        opcoes["status"] = list(FrotaVeiculo.Status.choices)
        opcoes["fabricantes"] = list(
            FrotaVeiculo.objects.exclude(fabricante="").order_by("fabricante").values_list("fabricante", flat=True).distinct()
        )
        if ver_api:
            opcoes["conectividade"] = [(k, v) for k, v in frota_service.ROTULO_VEICULO.items()]
    return opcoes


def _numero(valor) -> float:
    return float(valor)


def _ponto_de_veiculo(v: FrotaVeiculo, ver_chips: bool, agora, lim: dict, ultima=None, atividades=None, ver_api: bool = True) -> dict:
    local = localizacao.localizacao_do_veiculo(v, agora, lim, ultima)
    conect = getattr(v, "conectividade", None) or {}
    atividades = atividades or {}
    ponto = {
        "id": v.pk, "codigo": v.codigo, "nome": v.nome, "tipo": v.tipo.nome,
        "icone": icone_service.icone_do_veiculo(v.classe_operacional, v.tipo),
        "fabricante": v.fabricante, "modelo": v.modelo, "fazenda": v.localidade.local if v.localidade_id else "",
        "status": v.status, "status_rotulo": v.get_status_display(),
        "lat": local["lat"], "lon": local["lon"],
        "tem_chip": bool(getattr(v, "n_chips", 0)),
        "localizacao": {
            "estado": local["estado"], "estado_rotulo": local["estado_rotulo"], "origem": local["origem"],
            "rotulo": local["rotulo"], "desatualizada": local["desatualizada"], "aviso": local["aviso"],
            "atualizada_em": local["em"], "fonte": local["fonte"], "precisao_m": local["precisao_m"],
            "velocidade_kmh": local["velocidade_kmh"], "direcao_graus": local["direcao_graus"],
        },
        "url": reverse("frota_veiculo_detalhe", args=[v.pk]),
        "placa": v.placa,
    }
    if ver_chips:
        # O chip como cadastro e vínculo (nome, ICCID, ficha) sai com `ver_chips`; o que a API disse sobre ele — estado,
        # sessão, última atualização e atividade — só com `ver_api`. Sem ela essas chaves NÃO existem no JSON (o `agro-mapa.js`
        # só desenha a linha que veio): mandar vazio e esconder no JS seria dado de API na resposta.
        if ver_api:
            ponto["conectividade"] = {"estado": conect.get("estado", "sem_chip"), "rotulo": conect.get("rotulo", "Sem chip")}
        chips = []
        for i in conect.get("chips", []):
            chip = {
                "id": i["chip"].pk, "url": reverse("m2m_chip_detalhe", args=[i["chip"].pk]),
                "iccid": i["chip"].iccid, "nome": i["chip"].nome,
            }
            if ver_api:
                chip.update(
                    estado=i["estado"], rotulo=i["rotulo"], sessao=i["sessao_rotulo"],
                    ultima_atualizacao=_quando(i["ultima_valida_em"], agora),
                    # a última ATIVIDADE de sessão (fim da sessão ou último avanço da plataforma), que não é a hora da consulta
                    atividade=_quando(atividades[i["chip"].pk]["em"], agora) if i["chip"].pk in atividades else {"texto": "", "rotulo": "", "iso": ""},
                )
            chips.append(chip)
        ponto["chips"] = chips
    return ponto


LIMITE_SEM_LOCALIZACAO = 25      # quantos veículos com chip e sem localização a lista do mapa nomeia (o total vem inteiro)


def _veiculos_com_chip_sem_localizacao(todos, ver_chips: bool, agora) -> dict:
    """Os veículos que TÊM chip M2M vinculado e NÃO têm localização — o que o mapa não pode desenhar e o usuário quer ver.
    Nenhuma posição é inventada por causa disso: a lista só diz QUAIS são e leva ao cadastro, onde a coordenada se registra
    (ou onde uma fonte de GPS a entregará). `total` é o número inteiro; `itens` traz os primeiros `LIMITE_SEM_LOCALIZACAO`.
    Os nomes dos chips só saem com `ver_chips`."""
    base = todos.filter(latitude__isnull=True, n_chips__gt=0)
    total = base.count()
    itens = list(base[:LIMITE_SEM_LOCALIZACAO])
    if ver_chips and itens:
        frota_service.anotar_veiculos(itens, agora=agora)
    return {
        "total": total,
        "itens": [
            {
                "id": v.pk, "codigo": v.codigo, "nome": v.nome, "tipo": v.tipo.nome,
                "icone": icone_service.icone_do_veiculo(v.classe_operacional, v.tipo), "n_chips": getattr(v, "n_chips", 0),
                "chips": [i["chip"].nome for i in (getattr(v, "conectividade", None) or {}).get("chips", [])] if ver_chips else [],
                "url": reverse("frota_veiculo_detalhe", args=[v.pk]),
                "url_mapa": localizacao.url_do_mapa(v.pk),
            }
            for v in itens
        ],
    }


def _foco_do_veiculo(foco_id: int, agora, lim: dict) -> tuple[dict, FrotaVeiculo | None]:
    """O veículo pedido em `?veiculo=` (vindo do detalhe de um chip ou da ficha): `(foco, veiculo)`. O `foco` diz se ele
    foi achado e se TEM localização — sem coordenada a resposta é "indisponível", nunca um ponto em posição fictícia."""
    veiculo = frota_service.listar_veiculos({}, agora).filter(pk=foco_id).first()
    if veiculo is None:
        return {"veiculo": foco_id, "encontrado": False, "tem_localizacao": False, "estado": "indisponivel",
                "estado_rotulo": localizacao.ROTULO_DO_ESTADO["indisponivel"], "rotulo": "",
                "mensagem": "O equipamento pedido não foi encontrado."}, None
    local = localizacao.localizacao_do_veiculo(veiculo, agora, lim)
    mensagem = "" if local["tem_coordenada"] else (
        "Este equipamento não tem localização GPS nem coordenada cadastrada: a localização está indisponível e nada é desenhado no mapa."
    )
    return {
        "veiculo": veiculo.pk, "encontrado": True, "tem_localizacao": local["tem_coordenada"], "estado": local["estado"],
        "estado_rotulo": local["estado_rotulo"], "rotulo": f"{veiculo.codigo} · {veiculo.nome}", "mensagem": mensagem,
    }, veiculo


def _ponto_de_estrutura(e: TecEstrutura, agora) -> dict:
    return {
        "id": e.pk, "nome": e.nome, "tipo": e.tipo, "tipo_rotulo": e.get_tipo_display(), "icone": icone_service.icone_da_estrutura(e.tipo),
        "situacao": e.situacao, "situacao_rotulo": e.get_situacao_display(),
        "fazenda": e.fazenda.local if e.fazenda_id else "", "altura_m": str(e.altura_m) if e.altura_m is not None else "",
        "lat": _numero(e.latitude), "lon": _numero(e.longitude),
        "localizacao": {
            "origem": "estrutura", "rotulo": ROTULO_DA_ORIGEM["estrutura"], "desatualizada": False, "aviso": "",
            "atualizada_em": _quando(e.localizacao_em, agora),
        },
        "url": reverse("infra_estrutura_editar", args=[e.pk]),
    }


def _linha_de_enlace(en: TecEnlace, agora) -> dict:
    a, b = en.estrutura_a, en.estrutura_b
    return {
        "id": en.pk, "identificacao": en.identificacao,
        "a": {"id": a.pk, "nome": a.nome, "lat": _numero(a.latitude), "lon": _numero(a.longitude)},
        "b": {"id": b.pk, "nome": b.nome, "lat": _numero(b.latitude), "lon": _numero(b.longitude)},
        "estado": en.estado, "estado_rotulo": en.get_estado_display(),
        "estado_registrado_em": _quando(en.estado_em, agora),
        "estado_registrado_por": en.estado_por.get_username() if en.estado_por_id else "",
        "distancia_km": round(infra.distancia_do_enlace_km(en) or 0, 2),
        "radio_a": str(en.equipamento_a) if en.equipamento_a_id else "", "radio_b": str(en.equipamento_b) if en.equipamento_b_id else "",
        "frequencia_ghz": str(en.frequencia_ghz) if en.frequencia_ghz is not None else "",
        "url": reverse("infra_enlace_editar", args=[en.pk]),
    }


def montar_mapa(filtros: dict | None, *, ver_frota: bool, ver_infra: bool, ver_chips: bool, ver_api: bool = True, agora=None) -> dict:
    """O JSON do mapa. `filtros`: os de veículo (`FILTROS_DE_VEICULO`) mais `fazenda` (vale para estruturas e
    enlaces), `tipo_estrutura`, `estado_enlace` e `camadas` (lista: `veiculos`, `estruturas`, `enlaces`; vazio =
    todas as que a permissão deixa ver). Devolve:

        {"veiculos": [...], "estruturas": [...], "enlaces": [...],
         "semLocalizacao": {"veiculos": n, "estruturas": n, "enlaces": n},   # existem, mas não têm ponto
         "totais": {"veiculos": n, "estruturas": n, "enlaces": n},            # os desenhados
         "camadas": [...], "limite": bool}

    Só entra no mapa o que tem coordenada cadastrada; o resto é contado em `semLocalizacao`. Um enlace só é desenhado
    com as DUAS pontas localizadas."""
    filtros = dict(filtros or {})
    if not ver_api:
        filtros.pop("conectividade", None)        # filtrar por "sessão ativa" já seria ler o que a API disse
    agora = agora or timezone.now()
    lim = localizacao.limites()           # lido UMA vez: o custo do estado de cada ponto não é uma consulta por ponto
    pedidas = set(filtros.get("camadas") or ("veiculos", "estruturas", "enlaces"))
    saida: dict = {
        "veiculos": [], "estruturas": [], "enlaces": [],
        "semLocalizacao": {"veiculos": 0, "estruturas": 0, "enlaces": 0},
        "totais": {"veiculos": 0, "estruturas": 0, "enlaces": 0}, "camadas": [], "limite": False,
        "foco": None, "limitesGps": lim, "semLocalizacaoComChip": {"total": 0, "itens": []},
    }
    fazenda = str(filtros.get("fazenda") or filtros.get("localidade") or "")

    if ver_frota and "veiculos" in pedidas:
        saida["camadas"].append("veiculos")
        criterio = {c: filtros[c] for c in FILTROS_DE_VEICULO if filtros.get(c)}
        if fazenda.isdigit():
            criterio["localidade"] = fazenda
        todos = frota_service.listar_veiculos(criterio, agora)
        com_ponto = list(todos.filter(latitude__isnull=False)[: LIMITE_DE_VEICULOS + 1])
        saida["limite"] = len(com_ponto) > LIMITE_DE_VEICULOS
        com_ponto = com_ponto[:LIMITE_DE_VEICULOS]
        saida["semLocalizacao"]["veiculos"] = todos.filter(latitude__isnull=True).count()
        saida["semLocalizacaoComChip"] = _veiculos_com_chip_sem_localizacao(todos, ver_chips, agora)
        foco_pedido = str(filtros.get(FOCO) or "")
        if foco_pedido.isdigit():
            saida["foco"], foco_veiculo = _foco_do_veiculo(int(foco_pedido), agora, lim)
            # o veículo em foco aparece mesmo que os filtros o deixassem de fora: foi pedido pelo nome
            if foco_veiculo is not None and foco_veiculo.latitude is not None and all(v.pk != foco_veiculo.pk for v in com_ponto):
                com_ponto.append(foco_veiculo)
        if ver_chips:
            frota_service.anotar_veiculos(com_ponto, agora=agora)
        ultimas = localizacao.ultimas_posicoes([v.pk for v in com_ponto if v.localizacao_origem == "gps"])
        atividades = {}
        if ver_chips and ver_api:
            from services import m2m_analise_service as analise     # a análise parte do monitoramento: import tardio
            atividades = analise.ultima_atividade_por_chip(
                i["chip"].pk for v in com_ponto for i in (getattr(v, "conectividade", None) or {}).get("chips", [])
            )
        saida["veiculos"] = [_ponto_de_veiculo(v, ver_chips, agora, lim, ultimas.get(v.pk), atividades, ver_api) for v in com_ponto]
        saida["totais"]["veiculos"] = len(saida["veiculos"])

    if ver_infra and ("estruturas" in pedidas or "enlaces" in pedidas):
        criterio_estrutura = {"ativo": "1"}
        if fazenda.isdigit():
            criterio_estrutura["fazenda"] = fazenda
        if filtros.get("tipo_estrutura") in TecEstrutura.Tipo.values:
            criterio_estrutura["tipo"] = filtros["tipo_estrutura"]
        if filtros.get("q"):
            criterio_estrutura["q"] = filtros["q"]
        if "estruturas" in pedidas:
            saida["camadas"].append("estruturas")
            estruturas = list(infra.listar_estruturas(criterio_estrutura))
            saida["estruturas"] = [_ponto_de_estrutura(e, agora) for e in estruturas if e.tem_coordenada]
            saida["semLocalizacao"]["estruturas"] = sum(1 for e in estruturas if not e.tem_coordenada)
            saida["totais"]["estruturas"] = len(saida["estruturas"])
        if "enlaces" in pedidas:
            saida["camadas"].append("enlaces")
            criterio_enlace = {"ativo": "1"}
            if fazenda.isdigit():
                criterio_enlace["fazenda"] = fazenda
            if filtros.get("estado_enlace") in TecEnlace.Estado.values:
                criterio_enlace["estado"] = filtros["estado_enlace"]
            if filtros.get("q"):
                criterio_enlace["q"] = filtros["q"]
            enlaces = list(infra.listar_enlaces(criterio_enlace))
            desenhaveis = [e for e in enlaces if e.tem_geometria]
            saida["enlaces"] = [_linha_de_enlace(e, agora) for e in desenhaveis]
            saida["semLocalizacao"]["enlaces"] = len(enlaces) - len(desenhaveis)
            saida["totais"]["enlaces"] = len(saida["enlaces"])
    return saida
