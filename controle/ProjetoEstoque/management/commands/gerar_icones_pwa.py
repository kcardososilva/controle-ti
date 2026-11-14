"""
Gera os ícones PNG com que o Zelo aparece quando alguém o instala no celular (Adicionar à tela inicial).

    python manage.py gerar_icones_pwa

Por que PNG: o iPhone ignora SVG no `apple-touch-icon` (e sem ele monta uma miniatura da página), e o Android só aceita
PNG no manifesto para o ícone de instalação. O DESENHO não é redefinido aqui: o traçado do Z é lido de
`static/img/zelo-mark.svg`, a mesma marca do favicon e do login, para as três superfícies nunca divergirem.

Saída em `static/pwa/` (depois: `collectstatic`):
    zelo-192.png / zelo-512.png   "any"       cantos arredondados, fundo transparente (instalação, atalho, splash)
    zelo-maskable-512.png         "maskable"  quadrado inteiro; o Android recorta (círculo, gota, quadrado arredondado)
    zelo-apple-180.png            iOS         quadrado inteiro e OPACO: o iOS pinta transparência de preto e aplica
                                              o próprio arredondamento
"""
import re
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from PIL import Image, ImageDraw

APP = Path(__file__).resolve().parents[2]
ARQUIVO_MARCA = APP / "static" / "img" / "zelo-mark.svg"
PASTA_SAIDA = APP / "static" / "pwa"

# O azul da marca no sistema HOJE: `--brand` de tokens.css, o mesmo que a marca do login pinta (`_marca_zelo.html`).
# O SVG do favicon ainda guarda o azul anterior (#0071e3) e NÃO serve de fonte para a cor.
COR_FUNDO = "#5560ee"
COR_TRACO = "#ffffff"

LADO_VIEWBOX = 40          # o SVG da marca é desenhado em 40x40
RAIO_CANTO = 9             # `rx` do retângulo do SVG, na mesma unidade
SUPERAMOSTRAGEM = 4        # desenha em 4x e reduz: borda do Z e do canto sem serrilhado

# (arquivo, lado em px, cantos arredondados?, fundo opaco?)
SAIDAS = (
    ("zelo-192.png", 192, True, False),
    ("zelo-512.png", 512, True, False),
    ("zelo-maskable-512.png", 512, False, True),
    ("zelo-apple-180.png", 180, False, True),
)


def pontos_do_traco(svg: str) -> list[tuple[float, float]]:
    """Os vértices do Z, lidos do atributo `d` do SVG (só M, L, H, V e Z — o que a marca usa)."""
    achado = re.search(r'<path[^>]*\sd="([^"]+)"', svg)
    if not achado:
        raise CommandError("zelo-mark.svg não tem <path d=...>: a marca mudou de forma e este gerador precisa acompanhar.")
    pontos: list[tuple[float, float]] = []
    x = y = 0.0
    itens = re.findall(r"[A-Za-z]|-?\d+(?:\.\d+)?", achado.group(1))
    i = 0
    while i < len(itens):
        cmd = itens[i]
        i += 1
        if cmd in ("M", "L"):
            x, y = float(itens[i]), float(itens[i + 1])
            i += 2
        elif cmd == "H":
            x = float(itens[i])
            i += 1
        elif cmd == "V":
            y = float(itens[i])
            i += 1
        elif cmd in ("Z", "z"):
            continue
        else:
            raise CommandError(f"Comando de traçado '{cmd}' não suportado: o SVG da marca ganhou curvas ou comandos relativos.")
        pontos.append((x, y))
    return pontos


def desenhar(lado: int, pontos, *, arredondado: bool, opaco: bool) -> Image.Image:
    n = lado * SUPERAMOSTRAGEM
    k = n / LADO_VIEWBOX
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    pincel = ImageDraw.Draw(img)
    if arredondado:
        pincel.rounded_rectangle((0, 0, n - 1, n - 1), radius=RAIO_CANTO * k, fill=COR_FUNDO)
    else:
        pincel.rectangle((0, 0, n, n), fill=COR_FUNDO)
    pincel.polygon([(px * k, py * k) for px, py in pontos], fill=COR_TRACO)
    img = img.resize((lado, lado), Image.LANCZOS)
    return img.convert("RGB") if opaco else img


class Command(BaseCommand):
    help = "Gera os ícones PNG do app instalável (static/pwa/) a partir da marca static/img/zelo-mark.svg."

    def handle(self, *args, **opcoes):
        pontos = pontos_do_traco(ARQUIVO_MARCA.read_text(encoding="utf-8"))
        PASTA_SAIDA.mkdir(parents=True, exist_ok=True)
        for nome, lado, arredondado, opaco in SAIDAS:
            desenhar(lado, pontos, arredondado=arredondado, opaco=opaco).save(PASTA_SAIDA / nome, optimize=True)
            self.stdout.write(f"  {nome} ({lado}x{lado})")
        self.stdout.write(self.style.SUCCESS(f"{len(SAIDAS)} ícones gravados em static/pwa/."))
