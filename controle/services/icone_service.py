"""
A biblioteca de ícones do Zelo TI e do Zelo Agro — e a ÚNICA classificação de "que ícone é este equipamento".

Por que existe. Até aqui o desenho de um equipamento era decidido em cada tela: o mapa tinha uma tabela de nomes de tipo
para ícones do Font Awesome (`agro_mapa_service._ICONES_DE_VEICULO`), as listas escreviam o nome do tipo em texto e a ficha
do equipamento de TI não tinha desenho nenhum. A mesma regra em três lugares diverge na primeira categoria nova — e o
trator da lista não seria o trator do mapa. Aqui está a tabela, e as telas só PERGUNTAM (`icone_id`).

  • O ÍCONE REPRESENTA O TIPO, NUNCA A MARCA. "Rádio Motorola HT" é `radio-ht`; "Equipamento John Deere" é o tipo do
    equipamento que ele é. Fabricante, marca e modelo continuam sendo campos independentes: desenhar logomarca de
    terceiro seria usar marca registrada sem licença e, pior, esconder o tipo atrás da marca.
  • A CLASSIFICAÇÃO É POR NOME NORMALIZADO (minúsculo, sem acento, só letras e dígitos), na ordem de `REGRAS`: a
    primeira que casa vale. O cadastro real tem nomes como "Access-Point", "ANTENA BASTÃO", "Switches" e "CAMINHÃO
    PIPA/BOMBEIRO" — não existe tabela exata que sobreviva a isso. Nome que nenhuma regra reconhece cai no ícone
    genérico do ambiente (`ICONE_PADRAO`): desenhar algo específico para um tipo que o sistema não entende seria inventar.
  • O AMBIENTE DESEMPATA. "Monitor" é um monitor de mesa no Zelo TI e uma tela de bordo no Zelo Agro; o mesmo para modem.
    `ambiente` é obrigatório de propósito: sem ele a resposta dependeria da ordem das regras.
  • O ARQUIVO É O ATIVO. Cada ícone é um `.svg` em `static/icones/zelo/svg/<id>.svg` (grade 24, traço `currentColor` — por
    isso serve nos temas claro e escuro sem segundo desenho). O `sprite.svg` e o `manifesto.json` são GERADOS por
    `manage.py montar_icones_zelo`; há teste que falha se estiverem fora de sincronia com a pasta.
  • ORIGEM HONESTA. Os arquivos atuais são de autoria interna (`origem="interno"`). O Canva é o destino previsto para a
    arte final, mas o conector não exporta SVG (só PDF, PNG, JPG, GIF, PPTX, vídeo e CSV): um arquivo do Canva entra por
    PNG em `static/icones/zelo/png/<id>.png` (`formato="png"`) ou, exportado à mão pelo editor, por SVG no lugar do
    arquivo interno. O SVG tem precedência quando os dois existem.
  • EXPANDIR NÃO RECONSTRÓI NADA. Subtipo novo = uma entrada em `CATALOGO` (se o desenho for novo), uma linha em `REGRAS`
    e o arquivo; nenhuma tela muda.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

AMBIENTE_TI = "ti"
AMBIENTE_AGRO = "agro"
AMBIENTES = (AMBIENTE_TI, AMBIENTE_AGRO)

PASTA = Path(__file__).resolve().parent.parent / "ProjetoEstoque" / "static" / "icones" / "zelo"
PASTA_SVG = PASTA / "svg"
PASTA_PNG = PASTA / "png"
ARQUIVO_SPRITE = PASTA / "sprite.svg"
ARQUIVO_MANIFESTO = PASTA / "manifesto.json"
CAMINHO_ESTATICO_SPRITE = "icones/zelo/sprite.svg"
PREFIXO_SIMBOLO = "zi-"
ORIGEM_INTERNA = "interno"
ORIGEM_CANVA = "canva"


@dataclass(frozen=True)
class Icone:
    id: str
    rotulo: str
    grupo: str
    ambientes: tuple[str, ...]


_TI = (AMBIENTE_TI,)
_AGRO = (AMBIENTE_AGRO,)
_AMBOS = (AMBIENTE_TI, AMBIENTE_AGRO)

_G_COMPUTADORES = "Computadores e dispositivos"
_G_REDES = "Infraestrutura de redes"
_G_PERIFERICOS = "Impressão e periféricos"
_G_OUTROS_TI = "Outros equipamentos"
_G_MAQUINARIOS = "Maquinários e veículos"
_G_TEC_AGRO = "Tecnologia agrícola"
_G_RADIO = "Radiocomunicação"
_G_INFRA_AGRO = "Infraestrutura"

CATALOGO: tuple[Icone, ...] = (
    # ── Zelo TI ──
    Icone("desktop", "Desktop", _G_COMPUTADORES, _TI),
    Icone("notebook", "Notebook", _G_COMPUTADORES, _TI),
    Icone("workstation", "Workstation", _G_COMPUTADORES, _TI),
    Icone("monitor", "Monitor", _G_COMPUTADORES, _TI),
    Icone("tablet", "Tablet", _G_COMPUTADORES, _TI),
    Icone("smartphone", "Smartphone", _G_COMPUTADORES, _AMBOS),
    Icone("servidor", "Servidor", _G_COMPUTADORES, _TI),
    Icone("switch", "Switch", _G_REDES, _AMBOS),
    Icone("core", "Core", _G_REDES, _TI),
    Icone("roteador", "Roteador", _G_REDES, _AMBOS),
    Icone("access-point", "Access Point", _G_REDES, _AMBOS),
    Icone("firewall", "Firewall", _G_REDES, _TI),
    Icone("rack", "Rack", _G_REDES, _TI),
    Icone("patch-panel", "Patch panel", _G_REDES, _TI),
    Icone("modem", "Modem", _G_REDES, _TI),
    Icone("antena", "Antena", _G_REDES, _AMBOS),
    Icone("parabolica", "Antena parabólica", _G_REDES, _AMBOS),
    Icone("transceptor", "Transceptor / conversor de mídia", _G_REDES, _TI),
    Icone("impressora", "Impressora", _G_PERIFERICOS, _TI),
    Icone("scanner", "Scanner", _G_PERIFERICOS, _TI),
    Icone("nobreak", "Nobreak", _G_PERIFERICOS, _AMBOS),
    Icone("telefone", "Telefone", _G_PERIFERICOS, _TI),
    Icone("camera", "Câmera", _G_PERIFERICOS, _TI),
    Icone("teclado", "Teclado", _G_PERIFERICOS, _TI),
    Icone("mouse", "Mouse", _G_PERIFERICOS, _TI),
    Icone("projetor", "Projetor", _G_PERIFERICOS, _TI),
    Icone("headset", "Headset", _G_PERIFERICOS, _AMBOS),
    Icone("seguranca", "Equipamento de segurança", _G_OUTROS_TI, _TI),
    Icone("comunicacao", "Equipamento de comunicação", _G_OUTROS_TI, _AMBOS),
    Icone("infraestrutura", "Componente de infraestrutura", _G_OUTROS_TI, _TI),
    Icone("cabo", "Cabo / adaptador / conector", _G_OUTROS_TI, _AMBOS),
    Icone("fonte-energia", "Fonte / proteção elétrica", _G_OUTROS_TI, _AMBOS),
    Icone("bateria", "Bateria", _G_OUTROS_TI, _AMBOS),
    Icone("chip-sim", "Chip (SIM)", _G_OUTROS_TI, _AMBOS),
    Icone("generico-ti", "Equipamento de TI (tipo não reconhecido)", _G_OUTROS_TI, _TI),
    # ── Zelo Agro: maquinários e veículos ──
    Icone("trator", "Trator", _G_MAQUINARIOS, _AGRO),
    Icone("motocicleta", "Motocicleta", _G_MAQUINARIOS, _AGRO),
    Icone("caminhao", "Caminhão", _G_MAQUINARIOS, _AGRO),
    Icone("caminhao-pipa", "Caminhão-pipa", _G_MAQUINARIOS, _AGRO),
    Icone("caminhao-comboio", "Caminhão-comboio", _G_MAQUINARIOS, _AGRO),
    Icone("pulverizador", "Pulverizador", _G_MAQUINARIOS, _AGRO),
    Icone("colheitadeira", "Colheitadeira", _G_MAQUINARIOS, _AGRO),
    Icone("plantadeira", "Plantadeira", _G_MAQUINARIOS, _AGRO),
    Icone("distribuidor", "Distribuidor", _G_MAQUINARIOS, _AGRO),
    Icone("empilhadeira", "Empilhadeira", _G_MAQUINARIOS, _AGRO),
    Icone("pa-carregadeira", "Pá-carregadeira", _G_MAQUINARIOS, _AGRO),
    Icone("motoniveladora", "Motoniveladora", _G_MAQUINARIOS, _AGRO),
    Icone("gerador", "Gerador", _G_MAQUINARIOS, _AGRO),
    Icone("motor-estacionario", "Motor estacionário", _G_MAQUINARIOS, _AGRO),
    Icone("veiculo-utilitario", "Veículo utilitário", _G_MAQUINARIOS, _AMBOS),
    Icone("implemento", "Implemento agrícola", _G_MAQUINARIOS, _AGRO),
    Icone("maquina-generica", "Máquina (tipo não reconhecido)", _G_MAQUINARIOS, _AGRO),
    # ── Zelo Agro: tecnologia agrícola ──
    Icone("monitor-agricola", "Monitor agrícola", _G_TEC_AGRO, _AGRO),
    Icone("receptor-gps", "Receptor GPS / GNSS", _G_TEC_AGRO, _AGRO),
    Icone("antena-gnss", "Antena GNSS", _G_TEC_AGRO, _AGRO),
    Icone("modem-agricola", "Modem agrícola", _G_TEC_AGRO, _AGRO),
    Icone("controlador-eletronico", "Controlador eletrônico", _G_TEC_AGRO, _AGRO),
    Icone("sensor", "Sensor", _G_TEC_AGRO, _AGRO),
    Icone("tecnologia-embarcada", "Equipamento de tecnologia embarcada", _G_TEC_AGRO, _AGRO),
    # ── Zelo Agro: radiocomunicação ──
    Icone("radio-ht", "Rádio HT (portátil)", _G_RADIO, _AGRO),
    Icone("radio-veicular", "Rádio móvel veicular", _G_RADIO, _AGRO),
    Icone("radio-base", "Rádio base", _G_RADIO, _AGRO),
    Icone("repetidora", "Repetidora", _G_RADIO, _AGRO),
    Icone("antena-radio", "Antena de radiocomunicação", _G_RADIO, _AGRO),
    # ── Zelo Agro: infraestrutura ──
    Icone("torre", "Torre de comunicação", _G_INFRA_AGRO, _AGRO),
    Icone("estacao", "Estação", _G_INFRA_AGRO, _AGRO),
    Icone("base-rtk", "Base RTK", _G_INFRA_AGRO, _AGRO),
    Icone("enlace-ptp", "Enlace ponto a ponto", _G_INFRA_AGRO, _AGRO),
    Icone("antena-direcional", "Antena direcional", _G_INFRA_AGRO, _AMBOS),
    Icone("antena-omni", "Antena omnidirecional", _G_INFRA_AGRO, _AMBOS),
    Icone("ponto-referencia", "Ponto de referência", _G_INFRA_AGRO, _AGRO),
)
_POR_ID = {i.id: i for i in CATALOGO}

ICONE_PADRAO = {AMBIENTE_TI: "generico-ti", AMBIENTE_AGRO: "maquina-generica"}

# (padrão sobre o nome normalizado, ícone, ambientes em que vale). A PRIMEIRA que casa vence — o que é mais específico
# vem antes ("caminhão-pipa" antes de "caminhão", "omnidirecional" antes de "direcional", "antena GNSS" antes de "GNSS").
REGRAS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    # veículos e máquinas (o nome da CLASSE operacional vem antes do nome do tipo — ver `icone_do_veiculo`)
    (r"\bpipa\b|bombeiro", "caminhao-pipa", _AGRO),
    (r"comboio", "caminhao-comboio", _AGRO),
    (r"colheit", "colheitadeira", _AGRO),
    (r"plantad", "plantadeira", _AGRO),
    (r"pulveriz", "pulverizador", _AGRO),
    (r"distr|adubac", "distribuidor", _AGRO),
    (r"empilhad", "empilhadeira", _AGRO),
    (r"pa carregad|carregadeira|linha amarela", "pa-carregadeira", _AGRO),
    (r"motonivel|conservacao", "motoniveladora", _AGRO),
    (r"motor estacion", "motor-estacionario", _AGRO),
    (r"gerador", "gerador", _AGRO),
    (r"cavalo mec|caminh|roll off|munck", "caminhao", _AGRO),
    (r"trator", "trator", _AGRO),
    (r"motocicleta|\bmoto\b", "motocicleta", _AGRO),
    (r"utilitario|automovel|pick ?up|\bcarro\b|veiculo", "veiculo-utilitario", _AMBOS),
    (r"implemento|despontadeira", "implemento", _AGRO),
    # tecnologia agrícola, radiocomunicação e infraestrutura do Agro
    (r"antena.*gnss|gnss.*antena", "antena-gnss", _AGRO),
    (r"receptor|gps|gnss", "receptor-gps", _AGRO),
    (r"monitor|display", "monitor-agricola", _AGRO),
    (r"controlador|\becu\b", "controlador-eletronico", _AGRO),
    (r"modem|modulo de comunicacao", "modem-agricola", _AGRO),
    (r"embarcad|solinftec|telemetria", "tecnologia-embarcada", _AGRO),
    (r"sensor", "sensor", _AGRO),
    (r"portatil|\bht\b", "radio-ht", _AGRO),
    (r"radio.*(movel|veicular)", "radio-veicular", _AGRO),
    (r"radio base", "radio-base", _AGRO),
    (r"enlace|\bptp\b|ponto a ponto", "enlace-ptp", _AGRO),
    (r"repetidora", "repetidora", _AGRO),
    (r"antena.*radio", "antena-radio", _AGRO),
    (r"componente rtk|\brtk\b", "base-rtk", _AGRO),
    (r"\btorre\b", "torre", _AGRO),
    (r"estacao", "estacao", _AGRO),
    (r"referencia", "ponto-referencia", _AGRO),
    # antenas (os dois ambientes)
    (r"omnidirecional|bastao", "antena-omni", _AMBOS),
    (r"antena.*(direcional|painel)|direcional", "antena-direcional", _AMBOS),
    (r"parabolic|starlink", "parabolica", _AMBOS),
    (r"antena", "antena", _AMBOS),
    # energia ANTES dos equipamentos de rede: "FONTE AP OUTDOOR" é uma fonte, não um access point
    (r"bateria|carregador de pilha", "bateria", _AMBOS),
    (r"carregador|fonte|\bdps\b|inversor|injetor|power injector|controlador solar", "fonte-energia", _AMBOS),
    # equipamentos de TI e o que os dois ambientes compartilham
    (r"\bchip\b|\bsim\b", "chip-sim", _AMBOS),
    (r"celular|smartphone", "smartphone", _AMBOS),
    (r"tablet", "tablet", _AMBOS),
    (r"notebook|laptop", "notebook", _TI),
    (r"workstation|estacao de trabalho", "workstation", _TI),
    (r"desktop|computador|\bcpu\b", "desktop", _TI),
    (r"servidor|server", "servidor", _TI),
    (r"monitor|display", "monitor", _TI),
    (r"firewall", "firewall", _TI),
    (r"patch panel|patchpanel", "patch-panel", _TI),
    (r"switch|\bhub\b", "switch", _AMBOS),
    (r"\bcore\b", "core", _TI),
    (r"roteador|router", "roteador", _AMBOS),
    (r"access point|\bap\b", "access-point", _AMBOS),
    (r"\brack\b|gabinete", "rack", _TI),
    (r"modem", "modem", _TI),
    (r"impressora|printer|toner|fusor|cilindro", "impressora", _TI),
    (r"scanner|digitalizador", "scanner", _TI),
    (r"nobreak|\bups\b|estabilizador", "nobreak", _AMBOS),
    (r"telefone|ip phone|ramal", "telefone", _TI),
    (r"camera|cftv", "camera", _TI),
    (r"\bdvr\b|\bnvr\b|seguranca|alarme|catraca|biometr", "seguranca", _TI),
    (r"teclado", "teclado", _TI),
    (r"mouse", "mouse", _TI),
    (r"projetor", "projetor", _TI),
    (r"headset|\bfone\b|microfone", "headset", _AMBOS),
    (r"cabo|chicote|adaptador|conector|stack", "cabo", _AMBOS),
    (r"gbic|conversor|transceiver|\bsfp\b|midia", "transceptor", _TI),
    (r"infraestrutura", "infraestrutura", _TI),
    (r"radio|comunicacao", "comunicacao", _AMBOS),
)
_REGRAS_COMPILADAS = tuple((re.compile(padrao), ident, ambientes) for padrao, ident, ambientes in REGRAS)

# A estrutura georreferenciada tem tipo FECHADO (`TecEstrutura.Tipo`): é tabela direta, não casamento de nome.
ICONE_DA_ESTRUTURA = {"torre": "torre", "estacao": "estacao", "base": "base-rtk", "referencia": "ponto-referencia"}


def normalizar(texto) -> str:
    """Minúsculo, sem acento, só letras e dígitos separados por um espaço ("Access-Point" → "access point")."""
    sem_acento = "".join(c for c in unicodedata.normalize("NFD", str(texto or "")) if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", sem_acento.lower()).strip()


def _casar(nome, ambiente: str):
    texto = normalizar(nome)
    if not texto:
        return None
    for padrao, ident, ambientes in _REGRAS_COMPILADAS:
        if ambiente in ambientes and padrao.search(texto):
            return ident
    return None


def icone_id(nome, *, ambiente: str, categoria=None) -> str:
    """O id do ícone do tipo `nome` (o nome do subtipo, da categoria, da classe…). `categoria` é a segunda chance: o
    nome do grupo a que o tipo pertence, usada só quando o próprio nome não é reconhecido. Sem reconhecimento, o ícone
    genérico do ambiente — nunca um desenho específico por palpite."""
    if ambiente not in AMBIENTES:
        raise ValueError(f"ambiente inválido: {ambiente!r} (use 'ti' ou 'agro')")
    achado = _casar(nome, ambiente)
    if achado:
        return achado
    if callable(categoria):
        # `categoria` preguiçosa: só é avaliada se o nome não bastou — em lista, ler a categoria de cada linha seria
        # uma consulta por linha justamente para os tipos que a regra já reconhece pelo nome.
        categoria = categoria()
    return _casar(categoria, ambiente) or ICONE_PADRAO[ambiente]


def icone_do_veiculo(classe_operacional=None, tipo=None) -> str:
    """O ícone do veículo/máquina: a CLASSE operacional primeiro ("CAMINHÃO PIPA/BOMBEIRO", "MOTOR ESTACIONARIO" — que o
    tipo só enxerga como "Caminhão" e "Gerador"), depois o tipo. Aceita o nome (str) ou o objeto com `.nome`."""
    for candidato in (classe_operacional, tipo):
        achado = _casar(getattr(candidato, "nome", candidato), AMBIENTE_AGRO)
        if achado:
            return achado
    return ICONE_PADRAO[AMBIENTE_AGRO]


def icone_da_estrutura(tipo: str) -> str:
    return ICONE_DA_ESTRUTURA.get(tipo, "ponto-referencia")


def existe(ident: str) -> bool:
    return ident in _POR_ID


def rotulo_de(ident: str) -> str:
    return _POR_ID[ident].rotulo if ident in _POR_ID else ""


# ── o arquivo de cada ícone ──

@lru_cache(maxsize=512)
def formato_de(ident: str) -> str:
    """`svg` (o arquivo que vira símbolo do sprite), `png` (arte exportada do Canva, sem SVG) ou `""` (sem arquivo).
    Cacheado: uma lista de 100 linhas pergunta isto 100 vezes, e cada pergunta seria dois acessos ao disco. Trocar ou
    acrescentar um arquivo vale depois de reiniciar o servidor (como o `collectstatic`) ou de `limpar_cache()`."""
    if (PASTA_SVG / f"{ident}.svg").is_file():
        return "svg"
    if (PASTA_PNG / f"{ident}.png").is_file():
        return "png"
    return ""


def limpar_cache() -> None:
    formato_de.cache_clear()


def caminho_estatico_png(ident: str) -> str:
    return f"icones/zelo/png/{ident}.png"


def simbolo(ident: str) -> str:
    """O id do `<symbol>` dentro do sprite."""
    return f"{PREFIXO_SIMBOLO}{ident}"


# ── manifesto e sprite (gerados) ──

def _padroes_do_icone() -> dict[str, list[str]]:
    achados: dict[str, list[str]] = {}
    for padrao, ident, _ in REGRAS:
        achados.setdefault(ident, []).append(padrao)
    return achados


def manifesto() -> list[dict]:
    """Uma linha por ícone: identificador, categoria (grupo), subtipo(s) que ele representa, arquivo, formato, origem e
    a referência de utilização (onde a regra de classificação o aplica). Derivado do catálogo e da pasta — não é editado."""
    padroes = _padroes_do_icone()
    linhas = []
    for icone in CATALOGO:
        formato = formato_de(icone.id)
        linhas.append({
            "id": icone.id,
            "rotulo": icone.rotulo,
            "categoria": icone.grupo,
            "ambientes": list(icone.ambientes),
            "subtipo": icone.rotulo,
            "arquivo": f"svg/{icone.id}.svg" if formato == "svg" else (f"png/{icone.id}.png" if formato == "png" else ""),
            "formato": formato,
            "origem": ORIGEM_INTERNA if formato == "svg" else (ORIGEM_CANVA if formato == "png" else ""),
            "referencia": {
                "regras": padroes.get(icone.id, []),
                "uso": "Classificação por nome do subtipo/categoria/classe (icone_service.REGRAS)"
                       if icone.id in padroes else "Ícone-padrão do ambiente (tipo não reconhecido)",
            },
        })
    return linhas


def _miolo_do_svg(texto: str) -> str:
    achado = re.search(r"<svg\b[^>]*>(.*)</svg>", texto, re.S)
    return achado.group(1).strip() if achado else ""


def montar_sprite() -> str:
    """O `sprite.svg`: um `<symbol id="zi-<id>">` por ícone com arquivo `.svg`, na ordem do catálogo. O traço e o `fill`
    ficam num `<g>` dentro do símbolo (não na árvore da página), então o ícone se desenha igual onde quer que seja usado,
    e `currentColor` pega a cor do texto do lugar — é o que o faz servir nos dois temas."""
    simbolos = []
    for icone in CATALOGO:
        arquivo = PASTA_SVG / f"{icone.id}.svg"
        if not arquivo.is_file():
            continue
        miolo = _miolo_do_svg(arquivo.read_text(encoding="utf-8"))
        simbolos.append(
            f'<symbol id="{simbolo(icone.id)}" viewBox="0 0 24 24">'
            f'<g fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">{miolo}</g>'
            "</symbol>"
        )
    return '<svg xmlns="http://www.w3.org/2000/svg" style="display:none">\n' + "\n".join(simbolos) + "\n</svg>\n"


def montar_manifesto_json() -> str:
    return json.dumps(manifesto(), ensure_ascii=False, indent=2) + "\n"


def sprite_esta_em_dia() -> bool:
    """O sprite e o manifesto gravados batem com o que a pasta e o catálogo dizem agora?"""
    try:
        return (
            ARQUIVO_SPRITE.read_text(encoding="utf-8") == montar_sprite()
            and ARQUIVO_MANIFESTO.read_text(encoding="utf-8") == montar_manifesto_json()
        )
    except OSError:
        return False
