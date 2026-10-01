"""
quiosque_mapa_export_service.py — Exporta o mapa de rota do Quiosque em PDF
(vetorial sobre a imagem de satélite) ou PNG de alta resolução.

Por que renderizar no SERVIDOR, e não capturar a tela do navegador:
  - Qualidade: a captura entrega o mapa no tamanho da janela (~1000 px de
    largura). Aqui montamos o mosaico de tiles na resolução NATIVA do provedor
    e desenhamos por cima, chegando a ~200 DPI em A4 — imprimível.
  - Fidelidade: a captura pega a tela como está, inclusive tiles que ainda não
    carregaram. Aqui cada tile é baixado e conferido antes de compor.
  - Sem navegador headless: nada de Chromium/Playwright em produção.

No PDF a imagem de satélite entra como raster e TODO o resto (rota, pontos,
rótulos, legenda, escala) é vetor — então o traço e o texto continuam nítidos
em qualquer ampliação, ao contrário de um PNG ampliado.

Escolha do zoom: o maior que couber na moldura, limitado pelo que o provedor
realmente TEM naquele lugar. Isso é medido, não presumido — ver `_placeholders`.

Fonte das imagens: ArcGIS/Esri, o mesmo provedor das telas de mapa (ver a regra
sobre tiles do OpenStreetMap no CLAUDE.md e `_kq_mapa_js.html`).
"""
import hashlib
import io
import math
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from django.utils import timezone

from services import quiosque_service as qs

# ──────────────────────────────────────────────────────────────────────────────
# Tiles
# ──────────────────────────────────────────────────────────────────────────────
TILE_PX = 256

TILE_SATELITE = ("https://server.arcgisonline.com/ArcGIS/rest/services/"
                 "World_Imagery/MapServer/tile/{z}/{y}/{x}")
TILE_ROTULOS = ("https://server.arcgisonline.com/ArcGIS/rest/services/Reference/"
                "World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}")

ATRIBUICAO = "Imagem de satélite: Esri, Maxar, Earthstar Geographics"

ZOOM_MIN = 3
ZOOM_MAX = 19          # teto absoluto; o real é descoberto por tentativa
# (conexão, leitura). Separados porque as duas falhas são diferentes: provedor
# inalcançável falha no connect e não adianta esperar; tile pesado demora na
# leitura e vale a pena aguardar.
TILE_TIMEOUT_S = (4, 10)
TILE_WORKERS = 12
# Prazo TOTAL para montar o fundo, incluindo as tentativas em zooms menores.
# Sem ele, uma queda de rede faria cada um dos 400 tiles esgotar o timeout e a
# requisição HTTP do usuário ficaria minutos pendurada; com ele, o que passar do
# prazo simplesmente não é desenhado e a exportação sai com o que deu tempo.
TEMPO_MAX_TILES_S = 45
# Teto de tiles por exportação. 400 tiles ≈ 26 MP de mosaico — acima disso o
# ganho visual é nulo (a moldura do PDF não tem essa resolução) e o custo de
# rede/memória deixa de valer a pena.
TILES_MAX = 400

# Fração de tiles repetidos a partir da qual o zoom é considerado indisponível.
_PLACEHOLDER_LIMITE = 0.30

# Raio mínimo do enquadramento (m). Sem isso, um aparelho parado num ponto só
# geraria uma caixa de lado zero e um zoom absurdo.
RAIO_MINIMO_M = 120.0

CORES = {
    "online": (52, 199, 89),
    "offline": (255, 59, 48),
    "parado": (0, 120, 212),
    "inicio": (255, 255, 255),
    "texto": (26, 26, 26),
    "papel": (255, 255, 255),
}


def _sessao():
    s = requests.Session()
    # Provedores de tile recusam cliente sem identificação.
    s.headers["User-Agent"] = "ProjetoEstoque-Quiosque/1.0"
    return s


# ──────────────────────────────────────────────────────────────────────────────
# Projeção Web Mercator
# ──────────────────────────────────────────────────────────────────────────────
def _projetar(lat: float, lon: float, z: int) -> tuple:
    """(lat, lon) → pixel global (float) na pirâmide Web Mercator do zoom `z`."""
    n = TILE_PX * (2 ** z)
    x = (lon + 180.0) / 360.0 * n
    s = math.sin(math.radians(lat))
    s = min(max(s, -0.99999), 0.99999)
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n
    return x, y


def _metros_por_pixel(lat: float, z: int) -> float:
    return 156543.03392 * math.cos(math.radians(lat)) / (2 ** z)


def _caixa(pontos: list) -> tuple:
    """Envolvente (lat_min, lon_min, lat_max, lon_max) com raio mínimo."""
    lats = [p["lat"] for p in pontos]
    lons = [p["lon"] for p in pontos]
    lat0, lat1 = min(lats), max(lats)
    lon0, lon1 = min(lons), max(lons)

    lat_c = (lat0 + lat1) / 2
    grau_lat = RAIO_MINIMO_M / 111_320.0
    grau_lon = RAIO_MINIMO_M / (111_320.0 * max(0.1, math.cos(math.radians(lat_c))))
    if lat1 - lat0 < 2 * grau_lat:
        lat0, lat1 = lat_c - grau_lat, lat_c + grau_lat
    if lon1 - lon0 < 2 * grau_lon:
        lon_c = (lon0 + lon1) / 2
        lon0, lon1 = lon_c - grau_lon, lon_c + grau_lon
    return lat0, lon0, lat1, lon1


def _zoom_que_cabe(caixa: tuple, larg: int, alt: int, teto: int) -> int:
    """Maior zoom em que a envolvente cabe na moldura pedida."""
    lat0, lon0, lat1, lon1 = caixa
    for z in range(teto, ZOOM_MIN - 1, -1):
        x0, y1 = _projetar(lat0, lon0, z)
        x1, y0 = _projetar(lat1, lon1, z)
        if abs(x1 - x0) <= larg and abs(y1 - y0) <= alt:
            return z
    return ZOOM_MIN


def _placeholders(conteudos: dict) -> set:
    """
    Tiles que são o aviso "sem imagem neste zoom" do provedor, e não terreno.

    Detecta por CONTEÚDO IDÊNTICO em coordenadas diferentes: o provedor devolve
    HTTP 200 com um PNG válido de aviso, sempre o mesmo, em qualquer x/y — não
    há status de erro para conferir (é o mesmo tipo de armadilha que tornou o
    OpenStreetMap inútil aqui). Dois tiles de satélite reais nunca saem
    byte-a-byte iguais, então a repetição é assinatura segura.

    Exige 3 repetições para não confundir com uma grade minúscula.
    """
    grupos = {}
    for chave, bruto in conteudos.items():
        if bruto:
            grupos.setdefault(hashlib.md5(bruto).digest(), []).append(chave)
    return {c for g in grupos.values() if len(g) >= 3 for c in g}


def _baixar_grade(sessao, modelo: str, z: int, tx0: int, ty0: int,
                  tx1: int, ty1: int, prazo: float | None = None) -> dict:
    """Baixa a grade de tiles em paralelo. {(x, y): bytes|None}.

    `prazo` é um instante de `time.monotonic()`: passado ele, os tiles restantes
    são descartados sem nem tentar a requisição. É o que impede que uma rede
    fora do ar transforme a exportação numa espera de vários minutos.
    """
    def buscar(chave):
        tx, ty = chave
        if prazo is not None and time.monotonic() > prazo:
            return chave, None
        try:
            r = sessao.get(modelo.format(z=z, x=tx, y=ty), timeout=TILE_TIMEOUT_S)
            # Tiles reais têm centenas de bytes no mínimo; resposta curta é erro
            # travestido de 200.
            if r.status_code == 200 and len(r.content) > 300:
                return chave, r.content
        except requests.RequestException:
            pass
        return chave, None

    alvos = [(tx, ty) for ty in range(ty0, ty1 + 1) for tx in range(tx0, tx1 + 1)]
    with ThreadPoolExecutor(max_workers=TILE_WORKERS) as executor:
        return dict(executor.map(buscar, alvos))


def montar_base(caixa: tuple, larg: int, alt: int, *, rotulos: bool = False) -> dict:
    """
    Monta a imagem de fundo (satélite) que cobre a envolvente.

    Devolve {imagem, projetar, zoom, escala_m_px, degradado}, onde `projetar` é
    (lat, lon) → (x, y) em pixels DENTRO da imagem devolvida.

    Se o provedor não tiver imagem no zoom escolhido, tenta zooms menores; se
    nada vier (sem rede, firewall), devolve um fundo neutro com `degradado=True`
    — a rota ainda é exportada, apenas sem o terreno. Exportar um mapa mudo é
    melhor do que falhar a exportação inteira.
    """
    from PIL import Image

    sessao = _sessao()
    teto = ZOOM_MAX
    lat_centro = (caixa[0] + caixa[2]) / 2
    prazo = time.monotonic() + TEMPO_MAX_TILES_S

    for _ in range(ZOOM_MAX - ZOOM_MIN + 1):
        z = _zoom_que_cabe(caixa, larg, alt, teto)

        # Moldura em pixels globais, centrada na envolvente.
        x0, y1 = _projetar(caixa[0], caixa[1], z)
        x1, y0 = _projetar(caixa[2], caixa[3], z)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        px0, py0 = cx - larg / 2, cy - alt / 2

        tx0, ty0 = int(px0 // TILE_PX), int(py0 // TILE_PX)
        tx1, ty1 = int((px0 + larg) // TILE_PX), int((py0 + alt) // TILE_PX)
        if (tx1 - tx0 + 1) * (ty1 - ty0 + 1) > TILES_MAX:
            teto = z - 1
            continue

        conteudos = _baixar_grade(sessao, TILE_SATELITE, z, tx0, ty0, tx1, ty1, prazo)
        vazios = _placeholders(conteudos)
        validos = [c for c, b in conteudos.items() if b and c not in vazios]
        total = len(conteudos)

        if (total and len(vazios) / total > _PLACEHOLDER_LIMITE
                and z > ZOOM_MIN and time.monotonic() < prazo):
            teto = z - 1          # este zoom não existe aqui: desce um nível
            continue

        if not validos:
            break                 # nada utilizável: cai no fundo neutro

        mosaico = Image.new("RGB", ((tx1 - tx0 + 1) * TILE_PX,
                                    (ty1 - ty0 + 1) * TILE_PX), (232, 232, 230))
        for (tx, ty), bruto in conteudos.items():
            if not bruto or (tx, ty) in vazios:
                continue
            try:
                tile = Image.open(io.BytesIO(bruto)).convert("RGB")
            except OSError:
                continue
            mosaico.paste(tile, ((tx - tx0) * TILE_PX, (ty - ty0) * TILE_PX))

        if rotulos:
            _sobrepor_rotulos(sessao, mosaico, z, tx0, ty0, tx1, ty1, prazo)

        recorte = mosaico.crop((
            int(px0 - tx0 * TILE_PX), int(py0 - ty0 * TILE_PX),
            int(px0 - tx0 * TILE_PX) + larg, int(py0 - ty0 * TILE_PX) + alt,
        ))

        def projetar(lat, lon, _z=z, _px0=px0, _py0=py0):
            gx, gy = _projetar(lat, lon, _z)
            return gx - _px0, gy - _py0

        return {
            "imagem": recorte, "projetar": projetar, "zoom": z,
            "escala_m_px": _metros_por_pixel(lat_centro, z), "degradado": False,
        }

    # Sem imagem: fundo neutro, mas com a geometria correta para a rota.
    z = _zoom_que_cabe(caixa, larg, alt, ZOOM_MAX)
    x0, y1 = _projetar(caixa[0], caixa[1], z)
    x1, y0 = _projetar(caixa[2], caixa[3], z)
    px0, py0 = (x0 + x1) / 2 - larg / 2, (y0 + y1) / 2 - alt / 2

    def projetar_neutro(lat, lon):
        gx, gy = _projetar(lat, lon, z)
        return gx - px0, gy - py0

    return {
        "imagem": Image.new("RGB", (larg, alt), (238, 240, 243)),
        "projetar": projetar_neutro, "zoom": z,
        "escala_m_px": _metros_por_pixel(lat_centro, z), "degradado": True,
    }


def _sobrepor_rotulos(sessao, mosaico, z, tx0, ty0, tx1, ty1, prazo=None) -> None:
    """Aplica a camada de nomes (estradas/localidades) por cima da imagem."""
    from PIL import Image

    conteudos = _baixar_grade(sessao, TILE_ROTULOS, z, tx0, ty0, tx1, ty1, prazo)
    vazios = _placeholders(conteudos)
    for (tx, ty), bruto in conteudos.items():
        if not bruto or (tx, ty) in vazios:
            continue
        try:
            camada = Image.open(io.BytesIO(bruto)).convert("RGBA")
        except OSError:
            continue
        mosaico.paste(camada, ((tx - tx0) * TILE_PX, (ty - ty0) * TILE_PX), camada)


# ──────────────────────────────────────────────────────────────────────────────
# Geometria da rota (compartilhada entre PNG e PDF)
# ──────────────────────────────────────────────────────────────────────────────
def segmentos_conexao(trilha: list) -> list:
    """
    Quebra a trilha em trechos consecutivos de mesma conectividade.
    [{online: bool, pontos: [(x, y), ...]}] — o traço muda de cor onde a conexão
    muda, que é a leitura que o mapa existe para permitir.

    O ponto de transição entra nos DOIS trechos: sem isso ficaria um vão branco
    entre o fim de um e o começo do outro.
    """
    if not trilha:
        return []
    saida = []
    atual = {"online": bool(trilha[0]["online"]), "pontos": []}
    for ponto in trilha:
        if bool(ponto["online"]) != atual["online"]:
            atual["pontos"].append(ponto)
            saida.append(atual)
            atual = {"online": bool(ponto["online"]), "pontos": [ponto]}
        else:
            atual["pontos"].append(ponto)
    saida.append(atual)
    return [s for s in saida if len(s["pontos"]) >= 1]


def escala_bonita(escala_m_px: float, largura_px: int, fracao: float = 0.12) -> tuple:
    """Comprimento 'redondo' (1/2/5 × 10ⁿ) para a barra de escala.
    Devolve (metros, pixels, rótulo).

    Arredonda sempre para BAIXO do alvo: a barra precisa caber no painel que a
    contém, e um valor redondo maior que o alvo estourava a moldura e jogava o
    rótulo para fora da imagem.
    """
    alvo_m = max(escala_m_px * largura_px * fracao, 1)
    expoente = math.floor(math.log10(alvo_m))
    metros = 10 ** expoente
    for mult in (1, 2, 5):
        candidato = mult * (10 ** expoente)
        if candidato <= alvo_m:
            metros = candidato
    rotulo = f"{metros / 1000:g} km" if metros >= 1000 else f"{metros:g} m"
    return metros, metros / escala_m_px, rotulo


def contexto_periodo(dia, horas: int | None) -> str:
    """Texto do período exportado, igual ao filtro que o usuário aplicou."""
    if dia is not None:
        return f"Dia {dia.strftime('%d/%m/%Y')}"
    if horas:
        return f"Últimas {horas} h"
    return "Leituras mais recentes"


# ──────────────────────────────────────────────────────────────────────────────
# Desenho (PNG) — Pillow
# ──────────────────────────────────────────────────────────────────────────────
# A sobreposição é desenhada numa camada TRANSPARENTE com o dobro da resolução e
# depois reduzida com LANCZOS antes de entrar na imagem. É o que dá contorno
# suave a traços e texto sem borrar o satélite: ampliar a base para desenhar em
# cima estragaria justamente o que a exportação quer preservar — a nitidez do
# terreno.
_SUPER = 2

_FONTES = ("segoeui.ttf", "arial.ttf", "calibri.ttf", "DejaVuSans.ttf")
_FONTES_NEGRITO = ("seguisb.ttf", "arialbd.ttf", "calibrib.ttf", "DejaVuSans-Bold.ttf")


def _fonte(tamanho: int, negrito: bool = False):
    """Fonte do sistema, com degradação silenciosa para a embutida do Pillow.
    Sem TTF o texto sai menor e sem antialias, mas a exportação não falha."""
    from PIL import ImageFont

    for nome in (_FONTES_NEGRITO if negrito else _FONTES):
        try:
            return ImageFont.truetype(nome, tamanho)
        except OSError:
            continue
    return ImageFont.load_default()


def _linha(desenho, pontos, cor, largura, tracejada=False, traco=22, vao=14):
    """Polilinha contínua ou tracejada (o Pillow não tem tracejado nativo)."""
    if len(pontos) < 2:
        return
    if not tracejada:
        desenho.line(pontos, fill=cor, width=largura, joint="curve")
        return
    for a, b in zip(pontos, pontos[1:]):
        distancia = math.hypot(b[0] - a[0], b[1] - a[1])
        if distancia < 0.5:
            continue
        ux, uy = (b[0] - a[0]) / distancia, (b[1] - a[1]) / distancia
        posicao = 0.0
        while posicao < distancia:
            fim = min(posicao + traco, distancia)
            desenho.line([(a[0] + ux * posicao, a[1] + uy * posicao),
                          (a[0] + ux * fim, a[1] + uy * fim)],
                         fill=cor, width=largura)
            posicao = fim + vao


def _disco(desenho, x, y, raio, preenchimento, contorno=None, espessura=2):
    desenho.ellipse([x - raio, y - raio, x + raio, y + raio],
                    fill=preenchimento, outline=contorno, width=espessura)


def _caixa_texto(desenho, x, y, texto, fonte, cor_fundo, cor_texto,
                 pad=(10, 5), raio=7, ancora="mm"):
    """Pílula com texto centrado. Devolve a caixa ocupada (para evitar colisão)."""
    esq, topo, dir_, base = desenho.textbbox((0, 0), texto, font=fonte)
    larg, alt = dir_ - esq, base - topo
    cx = x - larg / 2 if ancora == "mm" else x
    cy = y - alt / 2
    caixa = [cx - pad[0], cy - pad[1], cx + larg + pad[0], cy + alt + pad[1]]
    desenho.rounded_rectangle(caixa, radius=raio, fill=cor_fundo)
    desenho.text((cx - esq, cy - topo), texto, font=fonte, fill=cor_texto)
    return caixa


def _colide(caixa, ocupadas) -> bool:
    for o in ocupadas:
        if caixa[0] < o[2] and caixa[2] > o[0] and caixa[1] < o[3] and caixa[3] > o[1]:
            return True
    return False


def _barras_sinal(desenho, x, y, nivel, cor_on, cor_off, escala=1.0):
    """Quatro barrinhas crescentes, como no selo da tela."""
    largura, vao = 5 * escala, 3 * escala
    for i in range(4):
        altura = (5 + i * 4) * escala
        bx = x + i * (largura + vao)
        desenho.rectangle([bx, y - altura, bx + largura, y],
                          fill=cor_on if i < (nivel or 0) else cor_off)


def _rotulo_ponto(ponto: dict) -> str:
    """Texto do selo: transporte e, quando houver, o nível medido."""
    if not ponto["online"]:
        return "Sem conexão"
    return ponto.get("sinal_rotulo") or "Rede"


def _seta(desenho, a, b, tamanho, cor):
    """Triângulo no meio do segmento a→b indicando o sentido do percurso."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    comprimento = math.hypot(dx, dy)
    if comprimento < tamanho * 2.5:
        return
    ux, uy = dx / comprimento, dy / comprimento
    mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
    px, py = -uy, ux          # normal
    desenho.polygon([
        (mx + ux * tamanho, my + uy * tamanho),
        (mx - ux * tamanho * 0.6 + px * tamanho * 0.7,
         my - uy * tamanho * 0.6 + py * tamanho * 0.7),
        (mx - ux * tamanho * 0.6 - px * tamanho * 0.7,
         my - uy * tamanho * 0.6 - py * tamanho * 0.7),
    ], fill=cor)


def _painel(desenho, caixa, raio=16, alfa=232):
    desenho.rounded_rectangle(caixa, radius=raio, fill=(255, 255, 255, alfa),
                              outline=(0, 0, 0, 40), width=2)


def desenhar_rota(desenho, trilha: list, projetar, filtros: dict, s: int = 1) -> list:
    """
    Desenha traço, pontos, marcos e selos. `s` é o fator de supersampling — todas
    as medidas são multiplicadas por ele, para o mesmo código servir a qualquer
    resolução. Devolve a lista de marcos desenhados, na ordem, para a tabela de
    detalhes do PDF poder referenciá-los pelo mesmo número.
    """
    if not trilha:
        return []

    tela = [projetar(p["lat"], p["lon"]) for p in trilha]

    # Círculos de precisão do GPS primeiro: ficam por baixo de tudo.
    if filtros.get("precisao"):
        for ponto, (x, y) in zip(trilha, tela):
            if not ponto.get("precisao"):
                continue
            raio = ponto["precisao"] / filtros["_m_por_px"]
            if raio > 2:
                desenho.ellipse([x - raio, y - raio, x + raio, y + raio],
                                fill=(0, 120, 212, 26), outline=(0, 120, 212, 70), width=s)

    if filtros.get("rota", True):
        for trecho in segmentos_conexao(trilha):
            pontos = [projetar(p["lat"], p["lon"]) for p in trecho["pontos"]]
            if len(pontos) < 2:
                continue
            se_online = trecho["online"]
            if not se_online and not filtros.get("offline", True):
                continue
            # Contorno escuro por baixo: sobre imagem de satélite (verde, marrom,
            # palha) um traço fino de cor pura some. O casing é o que garante
            # leitura sobre QUALQUER terreno.
            _linha(desenho, pontos, (0, 0, 0, 110), 9 * s,
                   tracejada=not se_online, traco=22 * s, vao=14 * s)
            _linha(desenho, pontos,
                   CORES["online"] + (255,) if se_online else CORES["offline"] + (255,),
                   5 * s, tracejada=not se_online, traco=22 * s, vao=14 * s)

    if filtros.get("setas", True):
        for i in range(0, len(tela) - 1, max(1, len(tela) // 22)):
            a, b = tela[i], tela[i + 1]
            cor = CORES["online"] if trilha[i]["online"] else CORES["offline"]
            _seta(desenho, a, b, 7 * s, cor + (235,))

    # Pontos comuns
    for ponto, (x, y) in zip(trilha, tela):
        if ponto.get("marco"):
            continue
        cor = CORES["online"] if ponto["online"] else CORES["offline"]
        _disco(desenho, x, y, 3.2 * s, cor + (240,), (255, 255, 255, 190), max(1, int(s)))

    # Paradas
    if filtros.get("paradas", True):
        for ponto, (x, y) in zip(trilha, tela):
            if ponto.get("parado"):
                _disco(desenho, x, y, 10 * s, None, CORES["parado"] + (255,), 3 * s)

    marcos = [(i, p, tela[i]) for i, p in enumerate(trilha) if p.get("marco")]

    # Selos dos marcos, com o mesmo critério de colisão da tela: quem está SEM
    # conexão reserva espaço primeiro, porque é a informação que o mapa existe
    # para dar; os demais cedem.
    ocupadas = []
    if filtros.get("selos", True):
        fonte = _fonte(int(15 * s), negrito=True)
        # Só recebe selo o marco em que o rótulo MUDA em relação ao anterior.
        # Repetir "Sem conexão" em cada ponto de um trecho offline não acrescenta
        # nada — o tracejado vermelho já diz isso ao longo de todo o trecho — e
        # enche o mapa a ponto de esconder o terreno. Assim sobra um selo por
        # trecho e um por troca de tecnologia (4G→3G), que é a informação real.
        candidatos, rotulo_anterior = [], None
        for posicao, marco in enumerate(marcos):
            rotulo = _rotulo_ponto(marco[1])
            if rotulo != rotulo_anterior or posicao in (0, len(marcos) - 1):
                candidatos.append(marco)
            rotulo_anterior = rotulo

        ordem = ([m for m in candidatos if not m[1]["online"]] +
                 [m for m in candidatos if m[1]["online"]])
        for _, ponto, (x, y) in ordem:
            texto = _rotulo_ponto(ponto)
            nivel = ponto.get("sinal_nivel")
            extra = 30 * s if (ponto["online"] and nivel is not None) else 0
            esq, topo, dir_, base = desenho.textbbox((0, 0), texto, font=fonte)
            larg = (dir_ - esq) + extra + 22 * s
            alt = (base - topo) + 12 * s
            caixa = [x - larg / 2, y - 34 * s - alt, x + larg / 2, y - 34 * s]
            if _colide(caixa, ocupadas):
                continue
            ocupadas.append(caixa)
            fundo = (28, 30, 34, 235) if ponto["online"] else (176, 28, 22, 240)
            desenho.rounded_rectangle(caixa, radius=9 * s, fill=fundo)
            desenho.text((caixa[0] + 11 * s - esq, caixa[1] + 6 * s - topo),
                         texto, font=fonte, fill=(255, 255, 255, 255))
            if extra:
                _barras_sinal(desenho, caixa[2] - extra + 4 * s, caixa[3] - 8 * s,
                              nivel, (255, 255, 255, 255), (255, 255, 255, 85), s)
            # Bico ligando o selo ao ponto.
            desenho.polygon([(x - 6 * s, caixa[3]), (x + 6 * s, caixa[3]),
                             (x, caixa[3] + 8 * s)], fill=fundo)

    # Marcos por cima de tudo
    for _, ponto, (x, y) in marcos:
        cor = CORES["online"] if ponto["online"] else CORES["offline"]
        _disco(desenho, x, y, 6.5 * s, cor + (255,), (255, 255, 255, 255), 2 * s)

    # Início e fim
    inicio, fim = tela[0], tela[-1]
    fonte_ab = _fonte(int(17 * s), negrito=True)
    for (x, y), letra, cor in ((inicio, "A", (0, 122, 204)), (fim, "B", (20, 20, 24))):
        _disco(desenho, x, y, 15 * s, cor + (255,), (255, 255, 255, 255), 3 * s)
        desenho.text((x, y), letra, font=fonte_ab, fill=(255, 255, 255, 255), anchor="mm")

    return [(i, p) for i, p, _ in marcos]


def _kpis(cobertura: dict) -> list:
    """Indicadores do cabeçalho — os mesmos números da barra sob o mapa na tela,
    para o arquivo exportado não contar uma história diferente do sistema."""
    itens = [
        ("Leituras", f"{cobertura.get('leituras', 0)}"),
        ("Com conexão", f"{cobertura.get('pct_online', 0)}%"),
        ("Distância", f"{cobertura.get('distancia_km', 0):g} km".replace(".", ",")),
        ("Trechos sem sinal", f"{cobertura.get('segmentos_offline', 0)}"),
    ]
    if cobertura.get("tem_sinal"):
        itens.append(("Sinal médio", f"{cobertura.get('sinal_medio')}/4"))
    if cobertura.get("operadora_predominante"):
        itens.append(("Operadora", cobertura["operadora_predominante"]))
    return itens


def render_png(device, trilha: list, cobertura: dict, *, dia=None, horas=None,
               filtros: dict | None = None, largura: int = 2400,
               altura: int = 1500) -> bytes:
    """Mapa da rota em PNG de alta resolução, pronto para compartilhar."""
    from PIL import Image, ImageDraw

    filtros = dict(filtros or {})
    base = montar_base(_caixa(trilha), largura, altura)
    filtros["_m_por_px"] = base["escala_m_px"]

    imagem = base["imagem"].convert("RGBA")
    s = _SUPER
    camada = Image.new("RGBA", (largura * s, altura * s), (0, 0, 0, 0))
    desenho = ImageDraw.Draw(camada)

    def projetar(lat, lon):
        x, y = base["projetar"](lat, lon)
        return x * s, y * s

    desenhar_rota(desenho, trilha, projetar, filtros, s)

    # ── Cabeçalho ────────────────────────────────────────────────────────────
    nome = device.apelido or device.modelo or "Quiosque"
    periodo = contexto_periodo(dia, horas)
    f_titulo = _fonte(int(40 * s), negrito=True)
    f_sub = _fonte(int(23 * s))
    f_rot = _fonte(int(19 * s))
    f_val = _fonte(int(27 * s), negrito=True)

    kpis = _kpis(cobertura)
    cab_alt = 168 * s
    _painel(desenho, [24 * s, 24 * s, largura * s - 24 * s, 24 * s + cab_alt])
    desenho.text((52 * s, 52 * s), nome, font=f_titulo, fill=(17, 17, 20, 255))
    desenho.text((52 * s, 104 * s), f"{periodo}  ·  Rota percorrida",
                 font=f_sub, fill=(90, 95, 105, 255))

    x_kpi = largura * s - 52 * s
    for rotulo, valor in reversed(kpis):
        larg_v = desenho.textlength(valor, font=f_val)
        larg_r = desenho.textlength(rotulo, font=f_rot)
        bloco = max(larg_v, larg_r)
        desenho.text((x_kpi, 58 * s), valor, font=f_val, fill=(17, 17, 20, 255), anchor="ra")
        desenho.text((x_kpi, 104 * s), rotulo, font=f_rot, fill=(110, 116, 126, 255), anchor="ra")
        x_kpi -= bloco + 56 * s

    # ── Legenda ──────────────────────────────────────────────────────────────
    itens_legenda = [
        ("linha", CORES["online"], "Com conexão"),
        ("tracejada", CORES["offline"], "Sem conexão (guardado na memória)"),
        ("anel", CORES["parado"], "Parada"),
        ("ab", (0, 122, 204), "A = início · B = fim do percurso"),
    ]
    leg_alt = (34 * len(itens_legenda) + 46) * s
    leg_larg = 560 * s
    lx, ly = 24 * s, altura * s - 24 * s - leg_alt
    _painel(desenho, [lx, ly, lx + leg_larg, ly + leg_alt])
    desenho.text((lx + 26 * s, ly + 18 * s), "LEGENDA", font=_fonte(int(17 * s), True),
                 fill=(120, 126, 136, 255))
    yy = ly + 52 * s
    for tipo, cor, texto in itens_legenda:
        if tipo == "linha":
            desenho.line([(lx + 26 * s, yy + 9 * s), (lx + 76 * s, yy + 9 * s)],
                         fill=cor + (255,), width=6 * s)
        elif tipo == "tracejada":
            _linha(desenho, [(lx + 26 * s, yy + 9 * s), (lx + 76 * s, yy + 9 * s)],
                   cor + (255,), 6 * s, tracejada=True, traco=13 * s, vao=9 * s)
        elif tipo == "anel":
            _disco(desenho, lx + 51 * s, yy + 9 * s, 11 * s, None, cor + (255,), 3 * s)
        else:
            _disco(desenho, lx + 40 * s, yy + 9 * s, 11 * s, cor + (255,), (255, 255, 255, 255), 2 * s)
            _disco(desenho, lx + 64 * s, yy + 9 * s, 11 * s, (20, 20, 24, 255), (255, 255, 255, 255), 2 * s)
        desenho.text((lx + 96 * s, yy), texto, font=_fonte(int(20 * s)), fill=(40, 44, 52, 255))
        yy += 34 * s

    # ── Escala + crédito ─────────────────────────────────────────────────────
    metros, barra_px, rotulo_escala = escala_bonita(base["escala_m_px"], largura)
    credito = ATRIBUICAO if not base["degradado"] else "Imagem de satélite indisponível no momento da exportação"
    # O painel acompanha a barra: com largura fixa, uma escala longa (rota de
    # dezenas de km) empurrava o rótulo para fora da imagem.
    painel_larg = max(
        barra_px * s + 26 * s + 16 * s + desenho.textlength(rotulo_escala, font=_fonte(int(21 * s), True)) + 26 * s,
        desenho.textlength(credito, font=_fonte(int(15 * s))) + 52 * s,
    )
    bx = largura * s - 24 * s - painel_larg
    by = altura * s - 24 * s - 96 * s
    _painel(desenho, [bx, by, largura * s - 24 * s, by + 96 * s])
    desenho.line([(bx + 26 * s, by + 40 * s), (bx + 26 * s + barra_px * s, by + 40 * s)],
                 fill=(20, 20, 24, 255), width=5 * s)
    for extremo in (bx + 26 * s, bx + 26 * s + barra_px * s):
        desenho.line([(extremo, by + 30 * s), (extremo, by + 50 * s)],
                     fill=(20, 20, 24, 255), width=5 * s)
    desenho.text((bx + 26 * s + barra_px * s + 16 * s, by + 28 * s), rotulo_escala,
                 font=_fonte(int(21 * s), True), fill=(20, 20, 24, 255))
    desenho.text((bx + 26 * s, by + 62 * s), credito, font=_fonte(int(15 * s)),
                 fill=(110, 116, 126, 255))

    # ── Norte ────────────────────────────────────────────────────────────────
    nx, ny = largura * s - 84 * s, 24 * s + cab_alt + 54 * s
    desenho.ellipse([nx - 34 * s, ny - 34 * s, nx + 34 * s, ny + 34 * s],
                    fill=(255, 255, 255, 225), outline=(0, 0, 0, 40), width=2 * s)
    desenho.polygon([(nx, ny - 22 * s), (nx - 11 * s, ny + 12 * s), (nx, ny + 4 * s),
                     (nx + 11 * s, ny + 12 * s)], fill=(200, 35, 30, 255))
    desenho.text((nx, ny + 22 * s), "N", font=_fonte(int(17 * s), True),
                 fill=(20, 20, 24, 255), anchor="mm")

    camada = camada.resize((largura, altura), Image.LANCZOS)
    imagem.alpha_composite(camada)

    saida = io.BytesIO()
    imagem.convert("RGB").save(saida, format="PNG", optimize=True)
    return saida.getvalue()


def trechos_sem_conexao(trilha: list) -> list:
    """
    Corridas consecutivas de leituras offline, com início, fim e posição.

    É a tabela que responde à pergunta que motiva o mapa: EM QUE PONTO da
    fazenda o sinal cai, e por quanto tempo. Cada trecho recebe o mesmo número
    que aparece desenhado no mapa, para conferência ponto a ponto.
    """
    trechos, atual = [], None
    for ponto in trilha:
        if not ponto["online"]:
            if atual is None:
                atual = {"pontos": [ponto]}
            else:
                atual["pontos"].append(ponto)
        elif atual is not None:
            trechos.append(atual)
            atual = None
    if atual is not None:
        trechos.append(atual)

    saida = []
    for numero, trecho in enumerate(trechos, start=1):
        pontos = trecho["pontos"]
        meio = pontos[len(pontos) // 2]
        leituras = sum(p.get("pontos_originais", 1) for p in pontos)
        distancia = sum(
            _haversine(a["lat"], a["lon"], b["lat"], b["lon"])
            for a, b in zip(pontos, pontos[1:])
        )
        # Uma parada colapsada cobre um intervalo: o começo do trecho é o começo
        # DA PARADA, não o instante do ponto representativo — senão um aparelho
        # parado 3 h sem sinal apareceria com duração zero.
        saida.append({
            "numero": numero,
            "inicio": pontos[0].get("quando_inicio") or pontos[0]["quando"],
            "fim": pontos[-1]["quando"],
            "duracao": _duracao(pontos[0].get("ts_inicio") or pontos[0].get("ts"),
                                pontos[-1].get("ts")),
            "leituras": leituras,
            "lat": meio["lat"], "lon": meio["lon"],
            "distancia_km": round(distancia / 1000, 2),
            "ponto_meio": meio,
        })
    return saida


def _duracao(ts_inicio: str | None, ts_fim: str | None) -> str:
    """Intervalo entre dois carimbos ISO, como "2 h 40 min". Vazio se faltar
    algum — melhor uma célula em branco do que um número inventado."""
    from datetime import datetime

    if not ts_inicio or not ts_fim:
        return "—"
    try:
        segundos = int((datetime.fromisoformat(ts_fim) - datetime.fromisoformat(ts_inicio))
                       .total_seconds())
    except (TypeError, ValueError):
        return "—"
    if segundos < 60:
        return f"{max(segundos, 0)} s"
    minutos, horas = segundos // 60, segundos // 3600
    return f"{horas} h {minutos % 60:02d} min" if horas else f"{minutos} min"


def _haversine(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ──────────────────────────────────────────────────────────────────────────────
# PDF — reportlab
# ──────────────────────────────────────────────────────────────────────────────
# A imagem de satélite entra como raster; rota, pontos, rótulos, legenda e
# tabelas são VETOR. É o que permite ampliar o PDF ou imprimi-lo em A3 sem o
# traço e o texto virarem um borrão — que é exatamente o que aconteceria
# exportando um PNG ampliado.
_PDF_MARGEM = 22
_PDF_CAB_ALT = 52
_PDF_RODAPE_ALT = 18


def _cor(rgb, alfa=1.0):
    from reportlab.lib.colors import Color
    return Color(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255, alpha=alfa)


def _rodape(c, largura, texto_esq: str, pagina: int, total: int | None = None):
    from reportlab.lib.colors import Color

    c.setFont("Helvetica", 7.5)
    c.setFillColor(Color(0.45, 0.47, 0.51))
    c.drawString(_PDF_MARGEM, _PDF_MARGEM - 8, texto_esq)
    marcador = f"Página {pagina}" + (f" de {total}" if total else "")
    c.drawRightString(largura - _PDF_MARGEM, _PDF_MARGEM - 8, marcador)


def _cabecalho(c, largura, altura, titulo: str, subtitulo: str):
    from reportlab.lib.colors import Color

    topo = altura - _PDF_MARGEM
    c.setFillColor(Color(0.07, 0.09, 0.12))
    c.setFont("Helvetica-Bold", 16)
    c.drawString(_PDF_MARGEM, topo - 16, titulo)
    c.setFillColor(Color(0.42, 0.45, 0.50))
    c.setFont("Helvetica", 9.5)
    c.drawString(_PDF_MARGEM, topo - 31, subtitulo)
    c.setStrokeColor(Color(0.85, 0.86, 0.88))
    c.setLineWidth(0.7)
    c.line(_PDF_MARGEM, topo - _PDF_CAB_ALT + 12, largura - _PDF_MARGEM, topo - _PDF_CAB_ALT + 12)
    return topo - _PDF_CAB_ALT


def _kpis_pdf(c, largura, y, kpis: list):
    """Faixa de indicadores alinhada à direita do cabeçalho."""
    from reportlab.lib.colors import Color

    x = largura - _PDF_MARGEM
    for rotulo, valor in reversed(kpis):
        c.setFont("Helvetica-Bold", 13)
        larg_v = c.stringWidth(valor, "Helvetica-Bold", 13)
        c.setFont("Helvetica", 7.5)
        larg_r = c.stringWidth(rotulo, "Helvetica", 7.5)
        bloco = max(larg_v, larg_r)
        c.setFillColor(Color(0.07, 0.09, 0.12))
        c.setFont("Helvetica-Bold", 13)
        c.drawRightString(x, y + 16, valor)
        c.setFillColor(Color(0.45, 0.47, 0.51))
        c.setFont("Helvetica", 7.5)
        c.drawRightString(x, y + 5, rotulo)
        x -= bloco + 26


def _desenhar_rota_pdf(c, trilha: list, mapear, filtros: dict):
    """Rota, pontos e marcos em vetor sobre a imagem já posicionada."""
    from reportlab.lib.colors import Color

    if filtros.get("rota", True):
        for trecho in segmentos_conexao(trilha):
            if not trecho["online"] and not filtros.get("offline", True):
                continue
            pontos = [mapear(p["lat"], p["lon"]) for p in trecho["pontos"]]
            if len(pontos) < 2:
                continue
            # Contorno escuro por baixo: garante leitura sobre qualquer terreno.
            for cor, espessura, tracejado in (
                (Color(0, 0, 0, alpha=0.45), 4.2, None),
                (_cor(CORES["online"] if trecho["online"] else CORES["offline"]), 2.4,
                 None if trecho["online"] else (5, 3)),
            ):
                c.setStrokeColor(cor)
                c.setLineWidth(espessura)
                c.setLineCap(1)
                c.setLineJoin(1)
                c.setDash(*tracejado) if tracejado else c.setDash()
                caminho = c.beginPath()
                caminho.moveTo(*pontos[0])
                for ponto in pontos[1:]:
                    caminho.lineTo(*ponto)
                c.drawPath(caminho, stroke=1, fill=0)
            c.setDash()

    for ponto in trilha:
        if ponto.get("marco"):
            continue
        x, y = mapear(ponto["lat"], ponto["lon"])
        c.setFillColor(_cor(CORES["online"] if ponto["online"] else CORES["offline"]))
        c.setStrokeColor(Color(1, 1, 1, alpha=0.8))
        c.setLineWidth(0.5)
        c.circle(x, y, 1.6, stroke=1, fill=1)

    if filtros.get("paradas", True):
        for ponto in trilha:
            if not ponto.get("parado"):
                continue
            x, y = mapear(ponto["lat"], ponto["lon"])
            c.setStrokeColor(_cor(CORES["parado"]))
            c.setLineWidth(1.4)
            c.circle(x, y, 4.6, stroke=1, fill=0)

    for ponto in trilha:
        if not ponto.get("marco"):
            continue
        x, y = mapear(ponto["lat"], ponto["lon"])
        c.setFillColor(_cor(CORES["online"] if ponto["online"] else CORES["offline"]))
        c.setStrokeColor(Color(1, 1, 1))
        c.setLineWidth(1.0)
        c.circle(x, y, 3.0, stroke=1, fill=1)


def _numerar_trechos_pdf(c, trechos: list, mapear):
    """Etiqueta numerada no meio de cada trecho sem conexão — é o que amarra o
    desenho à tabela "Trechos sem conexão" das páginas seguintes."""
    from reportlab.lib.colors import Color

    for trecho in trechos:
        x, y = mapear(trecho["lat"], trecho["lon"])
        rotulo = str(trecho["numero"])
        raio = 7.0
        c.setFillColor(_cor(CORES["offline"]))
        c.setStrokeColor(Color(1, 1, 1))
        c.setLineWidth(1.2)
        c.circle(x, y + 13, raio, stroke=1, fill=1)
        c.setFillColor(Color(1, 1, 1))
        c.setFont("Helvetica-Bold", 8)
        c.drawCentredString(x, y + 13 - 2.8, rotulo)


def _legenda_pdf(c, x, y, tem_offline: bool):
    """Legenda em caixa branca sobre o mapa (canto inferior esquerdo)."""
    from reportlab.lib.colors import Color

    linhas = [("linha", CORES["online"], "Com conexão")]
    if tem_offline:
        linhas.append(("tracejada", CORES["offline"], "Sem conexão (guardado na memória)"))
        linhas.append(("numero", CORES["offline"], "Nº do trecho sem conexão (ver tabela)"))
    linhas.append(("anel", CORES["parado"], "Parada"))
    linhas.append(("ab", (0, 122, 204), "A = início · B = fim do percurso"))

    larg, alt = 186, 16 * len(linhas) + 22
    c.setFillColor(Color(1, 1, 1, alpha=0.92))
    c.setStrokeColor(Color(0, 0, 0, alpha=0.18))
    c.setLineWidth(0.7)
    c.roundRect(x, y, larg, alt, 5, stroke=1, fill=1)

    c.setFillColor(Color(0.47, 0.49, 0.53))
    c.setFont("Helvetica-Bold", 6)
    c.drawString(x + 10, y + alt - 12, "LEGENDA")

    yy = y + alt - 26
    for tipo, cor, texto in linhas:
        if tipo == "linha":
            c.setStrokeColor(_cor(cor)); c.setLineWidth(2.2); c.setDash()
            c.line(x + 10, yy + 3, x + 30, yy + 3)
        elif tipo == "tracejada":
            c.setStrokeColor(_cor(cor)); c.setLineWidth(2.2); c.setDash(3, 2)
            c.line(x + 10, yy + 3, x + 30, yy + 3); c.setDash()
        elif tipo == "anel":
            c.setStrokeColor(_cor(cor)); c.setLineWidth(1.3)
            c.circle(x + 20, yy + 3, 4.4, stroke=1, fill=0)
        elif tipo == "numero":
            c.setFillColor(_cor(cor)); c.setStrokeColor(Color(1, 1, 1)); c.setLineWidth(0.8)
            c.circle(x + 20, yy + 3, 5.2, stroke=1, fill=1)
            c.setFillColor(Color(1, 1, 1)); c.setFont("Helvetica-Bold", 6)
            c.drawCentredString(x + 20, yy + 1, "1")
        else:
            c.setFillColor(_cor(cor)); c.setStrokeColor(Color(1, 1, 1)); c.setLineWidth(0.8)
            c.circle(x + 14, yy + 3, 4.4, stroke=1, fill=1)
            c.setFillColor(Color(0.08, 0.08, 0.09))
            c.circle(x + 27, yy + 3, 4.4, stroke=1, fill=1)
        c.setFillColor(Color(0.16, 0.17, 0.20))
        c.setFont("Helvetica", 7)
        c.drawString(x + 38, yy, texto)
        yy -= 16


def _escala_pdf(c, x, y, escala_m_px: float, img_larg: int, fator: float, credito: str):
    """Barra de escala + crédito da imagem, ancorados pela direita."""
    from reportlab.lib.colors import Color

    _, barra_px, rotulo = escala_bonita(escala_m_px, img_larg)
    barra_pt = barra_px * fator
    larg = max(barra_pt + 18 + c.stringWidth(rotulo, "Helvetica-Bold", 7.5) + 20,
               c.stringWidth(credito, "Helvetica", 5.6) + 20)
    alt = 34
    x0 = x - larg
    c.setFillColor(Color(1, 1, 1, alpha=0.92))
    c.setStrokeColor(Color(0, 0, 0, alpha=0.18))
    c.setLineWidth(0.7)
    c.roundRect(x0, y, larg, alt, 5, stroke=1, fill=1)

    c.setStrokeColor(Color(0.08, 0.08, 0.09))
    c.setLineWidth(1.4)
    c.setDash()
    base_y = y + alt - 13
    c.line(x0 + 10, base_y, x0 + 10 + barra_pt, base_y)
    for extremo in (x0 + 10, x0 + 10 + barra_pt):
        c.line(extremo, base_y - 3.5, extremo, base_y + 3.5)
    c.setFillColor(Color(0.08, 0.08, 0.09))
    c.setFont("Helvetica-Bold", 7.5)
    c.drawString(x0 + 10 + barra_pt + 8, base_y - 2.6, rotulo)
    c.setFillColor(Color(0.47, 0.49, 0.53))
    c.setFont("Helvetica", 5.6)
    c.drawString(x0 + 10, y + 7, credito)


def _tabela_pdf(c, largura, altura, titulo: str, colunas: list, linhas: list,
                rodape_texto: str, pagina_inicial: int, subtitulo: str = "") -> int:
    """
    Desenha uma tabela paginada. `colunas` = [(cabeçalho, largura_pt, alinhamento)].
    Devolve o número da próxima página livre.
    """
    from reportlab.lib.colors import Color

    pagina = pagina_inicial
    topo = _cabecalho(c, largura, altura, titulo, subtitulo)
    y = topo - 6
    total_larg = sum(col[1] for col in colunas)
    x0 = _PDF_MARGEM

    def desenhar_cabecalho_tabela(yy):
        c.setFillColor(Color(0.94, 0.95, 0.96))
        c.rect(x0, yy - 14, total_larg, 14, stroke=0, fill=1)
        c.setFillColor(Color(0.30, 0.33, 0.38))
        c.setFont("Helvetica-Bold", 6.8)
        cx = x0
        for cabecalho, larg_col, alinhamento in colunas:
            if alinhamento == "r":
                c.drawRightString(cx + larg_col - 5, yy - 10, cabecalho)
            else:
                c.drawString(cx + 5, yy - 10, cabecalho)
            cx += larg_col
        return yy - 14

    y = desenhar_cabecalho_tabela(y)
    limite = _PDF_MARGEM + _PDF_RODAPE_ALT
    alternado = False

    for linha in linhas:
        if y - 13 < limite:
            _rodape(c, largura, rodape_texto, pagina)
            c.showPage()
            pagina += 1
            topo = _cabecalho(c, largura, altura, titulo, subtitulo + " (continuação)")
            y = desenhar_cabecalho_tabela(topo - 6)
            alternado = False
        if alternado:
            c.setFillColor(Color(0.975, 0.978, 0.982))
            c.rect(x0, y - 13, total_larg, 13, stroke=0, fill=1)
        alternado = not alternado

        cx = x0
        for (_, larg_col, alinhamento), valor in zip(colunas, linha):
            texto, cor = (valor if isinstance(valor, tuple) else (valor, (26, 28, 32)))
            c.setFillColor(_cor(cor))
            c.setFont("Helvetica", 6.8)
            if alinhamento == "r":
                c.drawRightString(cx + larg_col - 5, y - 9.2, str(texto))
            else:
                c.drawString(cx + 5, y - 9.2, str(texto))
            cx += larg_col
        y -= 13

    _rodape(c, largura, rodape_texto, pagina)
    c.showPage()
    return pagina + 1


def render_pdf(device, trilha: list, cobertura: dict, *, dia=None, horas=None,
               filtros: dict | None = None, usuario: str = "") -> bytes:
    """
    Relatório em PDF: mapa da rota (satélite + vetor) e as tabelas de detalhe.

    Páginas: 1) mapa · 2) trechos sem conexão · 3+) leituras ponto a ponto.
    """
    from reportlab.lib.colors import Color
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas as rl_canvas

    filtros = dict(filtros or {})
    largura, altura = landscape(A4)
    buffer = io.BytesIO()
    c = rl_canvas.Canvas(buffer, pagesize=(largura, altura))

    nome = device.apelido or device.modelo or "Quiosque"
    periodo = contexto_periodo(dia, horas)
    gerado = timezone.localtime().strftime("%d/%m/%Y às %H:%M")
    subtitulo = f"{periodo}  ·  Rota percorrida"
    rodape_texto = f"Quiosque · {nome} · {periodo} · Emitido em {gerado}"
    if usuario:
        rodape_texto += f" por {usuario}"
    c.setTitle(f"Mapa da rota — {nome} — {periodo}")
    c.setAuthor("Sistema de Controle de TI — Santa Colomba")

    # ── Página 1: mapa ───────────────────────────────────────────────────────
    topo = _cabecalho(c, largura, altura, nome, subtitulo)
    _kpis_pdf(c, largura, topo + 18, _kpis(cobertura))

    mapa_x = _PDF_MARGEM
    mapa_y = _PDF_MARGEM + _PDF_RODAPE_ALT
    mapa_larg = largura - 2 * _PDF_MARGEM
    mapa_alt = topo - mapa_y - 4

    # Mosaico na proporção EXATA da moldura: assim a imagem não é esticada nem
    # sobra, e a projeção do desenho coincide com o que se vê.
    img_larg = 2200
    img_alt = int(img_larg * mapa_alt / mapa_larg)
    base = montar_base(_caixa(trilha), img_larg, img_alt)
    filtros["_m_por_px"] = base["escala_m_px"]

    # Imagem de satélite embutida como JPEG: é fotografia, e o PNG sem perdas
    # produzia um PDF de ~8 MB — grande demais para anexar em e-mail — sem ganho
    # visível. Só o FUNDO é comprimido; rota, texto e tabelas continuam vetor.
    fundo = io.BytesIO()
    base["imagem"].convert("RGB").save(fundo, format="JPEG", quality=88, optimize=True)
    fundo.seek(0)
    c.drawImage(ImageReader(fundo), mapa_x, mapa_y, mapa_larg, mapa_alt,
                preserveAspectRatio=False, mask=None)
    c.setStrokeColor(Color(0.78, 0.80, 0.82))
    c.setLineWidth(0.7)
    c.rect(mapa_x, mapa_y, mapa_larg, mapa_alt, stroke=1, fill=0)

    fator = mapa_larg / img_larg

    def mapear(lat, lon):
        """(lat, lon) → ponto do PDF. O eixo Y do PDF cresce para CIMA, ao
        contrário do da imagem, por isso a altura é invertida aqui."""
        px, py = base["projetar"](lat, lon)
        return mapa_x + px * fator, mapa_y + mapa_alt - py * fator

    c.saveState()
    caminho = c.beginPath()
    caminho.rect(mapa_x, mapa_y, mapa_larg, mapa_alt)
    c.clipPath(caminho, stroke=0, fill=0)     # nada escapa da moldura do mapa

    _desenhar_rota_pdf(c, trilha, mapear, filtros)
    trechos = trechos_sem_conexao(trilha)
    if filtros.get("offline", True):
        _numerar_trechos_pdf(c, trechos, mapear)

    # Início e fim
    for ponto, letra, cor in ((trilha[0], "A", (0, 122, 204)),
                              (trilha[-1], "B", (20, 20, 24))):
        x, y = mapear(ponto["lat"], ponto["lon"])
        c.setFillColor(_cor(cor)); c.setStrokeColor(Color(1, 1, 1)); c.setLineWidth(1.4)
        c.circle(x, y, 7.5, stroke=1, fill=1)
        c.setFillColor(Color(1, 1, 1)); c.setFont("Helvetica-Bold", 8.5)
        c.drawCentredString(x, y - 3, letra)
    c.restoreState()

    _legenda_pdf(c, mapa_x + 8, mapa_y + 8, bool(trechos))
    credito = ATRIBUICAO if not base["degradado"] else \
        "Imagem de satélite indisponível no momento da exportação"
    _escala_pdf(c, mapa_x + mapa_larg - 8, mapa_y + 8, base["escala_m_px"],
                img_larg, fator, credito)

    _rodape(c, largura, rodape_texto, 1)
    c.showPage()
    pagina = 2

    # ── Página 2: trechos sem conexão ────────────────────────────────────────
    if trechos:
        linhas = [
            (t["numero"], t["inicio"], t["fim"], t["duracao"], t["leituras"],
             f"{t['lat']:.6f}", f"{t['lon']:.6f}",
             f"{t['distancia_km']:.2f}".replace(".", ","))
            for t in trechos
        ]
        colunas = [("Nº", 30, "l"), ("Início", 108, "l"), ("Fim", 108, "l"),
                   ("Duração", 76, "r"), ("Leituras", 56, "r"),
                   ("Latitude", 88, "r"), ("Longitude", 88, "r"),
                   ("Extensão (km)", 76, "r")]
        pagina = _tabela_pdf(
            c, largura, altura, nome, colunas, linhas, rodape_texto, pagina,
            subtitulo=f"{periodo} · Trechos sem conexão — a posição é o meio do trecho",
        )
    else:
        _cabecalho(c, largura, altura, nome, f"{periodo} · Trechos sem conexão")
        c.setFillColor(Color(0.35, 0.38, 0.42))
        c.setFont("Helvetica", 10)
        c.drawString(_PDF_MARGEM, altura - _PDF_MARGEM - 78,
                     "O aparelho manteve conexão durante todo o período exportado.")
        _rodape(c, largura, rodape_texto, pagina)
        c.showPage()
        pagina += 1

    # ── Páginas 3+: leituras ─────────────────────────────────────────────────
    linhas = []
    for indice, ponto in enumerate(trilha, start=1):
        # "Guardado na memória" é diferente de "sem rede": o aparelho podia até
        # ver a torre/Wi-Fi, mas não conseguiu transmitir e a leitura só chegou
        # depois (ver quiosque_service.conexao_do_checkin). Sem essa distinção a
        # tabela mostrava "Sem conexão" ao lado de "Wi-Fi 4/4 (-41 dBm)" na mesma
        # linha, o que parece contradição e é justamente o caso mais revelador.
        if ponto["online"]:
            conexao = ("Com conexão", (22, 130, 60))
        elif ponto.get("fila"):
            conexao = ("Guardado na memória", (170, 105, 10))
        else:
            conexao = ("Sem conexão", (176, 28, 22))
        nivel = ponto.get("sinal_nivel")
        sinal = ponto.get("sinal_rotulo") or "—"
        if nivel is not None:
            sinal += f" · {nivel}/4"
        if ponto.get("sinal_dbm") is not None:
            sinal += f" ({ponto['sinal_dbm']} dBm)"
        linhas.append((
            indice, ponto["quando"], f"{ponto['lat']:.6f}", f"{ponto['lon']:.6f}",
            f"{ponto['precisao']:.0f}" if ponto.get("precisao") is not None else "—",
            conexao, sinal,
            f"{ponto['bateria']}%" if ponto.get("bateria") is not None else "—",
            "Sim" if ponto.get("parado") else "",
        ))
    colunas = [("Nº", 30, "l"), ("Quando", 100, "l"), ("Latitude", 80, "r"),
               ("Longitude", 80, "r"), ("Prec. (m)", 52, "r"), ("Conexão", 112, "l"),
               ("Rede / sinal", 158, "l"), ("Bateria", 48, "r"), ("Parada", 42, "l")]
    _tabela_pdf(c, largura, altura, nome, colunas, linhas, rodape_texto, pagina,
                subtitulo=f"{periodo} · Leituras do percurso ({len(trilha)} pontos)")

    c.save()
    return buffer.getvalue()


def exportar(device, trilha: list, cobertura: dict, *, formato: str = "pdf",
             dia=None, horas=None, filtros: dict | None = None,
             usuario: str = "") -> tuple:
    """
    Ponto único de entrada da view. Devolve (conteúdo, content_type, nome).

    Sem pontos com posição não há mapa a exportar — quem chama decide o aviso.
    """
    if not trilha:
        raise ValueError("Não há pontos com localização no período selecionado.")

    nome = (device.apelido or device.modelo or "quiosque").lower()
    nome = "".join(ch if ch.isalnum() else "-" for ch in nome).strip("-")[:40]
    marca = (dia.strftime("%Y-%m-%d") if dia else timezone.localtime().strftime("%Y-%m-%d-%H%M"))

    if formato == "png":
        return (render_png(device, trilha, cobertura, dia=dia, horas=horas, filtros=filtros),
                "image/png", f"mapa-{nome}-{marca}.png")
    return (render_pdf(device, trilha, cobertura, dia=dia, horas=horas,
                       filtros=filtros, usuario=usuario),
            "application/pdf", f"mapa-{nome}-{marca}.pdf")
