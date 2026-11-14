"""O Zelo como app instalável no celular (Adicionar à tela inicial): a logo que aparece no aparelho.

O que se prova:
  • O MANIFESTO é JSON válido, declara os ícones que o Android exige (192 e 512, mais um "maskable") e CADA ícone
    declarado existe, é PNG e tem o tamanho que o manifesto promete — um `src` errado não dá erro em lugar nenhum,
    o aparelho só mostra a letra "Z" genérica no lugar da logo;
  • OS ÍCONES — "any" tem canto transparente (a marca com a forma dela); "maskable" e o do iPhone são quadrados
    inteiros e OPACOS (o iOS pinta transparência de preto); o Z do "maskable" cabe na zona segura que o Android
    reserva ao recorte (círculo de 40% do lado) — fora dela o sistema corta a letra;
  • O iPHONE — o `apple-touch-icon` é PNG. Ele apontava para o SVG do favicon, que o iOS ignora;
  • AS TELAS — todo shell que declara a marca Zelo no favicon carrega o parcial `_pwa_head.html`;
  • O TIPO — o WhiteNoise não conhece `.webmanifest`; sem a configuração ele serviria `octet-stream`;
  • O GERADOR — lê o traçado do Z do `zelo-mark.svg` (uma marca só) e grava os quatro arquivos."""

import io
import json
import tempfile
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.test import TestCase
from django.urls import reverse
from PIL import Image
from whitenoise.media_types import MediaTypes

from ProjetoEstoque.management.commands import gerar_icones_pwa as gerador

APP = Path(gerador.__file__).resolve().parents[2]
PASTA_PWA = APP / "static" / "pwa"
RAIZ_CONTROLE = APP.parent  # onde moram controle/templates e ProjetoEstoque/

SHELLS_COM_A_MARCA = (
    "templates/login.html",
    "templates/registration/_auth_shell.html",
    "ProjetoEstoque/templates/base.html",
    "ProjetoEstoque/templates/front/portal/portal_base.html",
    "ProjetoEstoque/templates/front/portal_licencas/portal_licencas_base.html",
)


def _manifesto() -> dict:
    return json.loads((PASTA_PWA / "zelo.webmanifest").read_text(encoding="utf-8"))


class ManifestoTests(TestCase):
    def test_declara_o_que_o_android_exige_para_mostrar_a_logo(self):
        m = _manifesto()
        for chave in ("name", "short_name", "start_url", "scope", "display", "icons"):
            self.assertTrue(m.get(chave), f"o manifesto não tem '{chave}'")
        self.assertEqual(m["short_name"], "Zelo")
        self.assertEqual(m["display"], "standalone")
        # nome curto: o Android trunca em ~12 caracteres sob o ícone
        self.assertLessEqual(len(m["short_name"]), 12)

        por_finalidade = {(i["purpose"], i["sizes"]) for i in m["icons"]}
        self.assertIn(("any", "192x192"), por_finalidade)
        self.assertIn(("any", "512x512"), por_finalidade)
        self.assertIn(("maskable", "512x512"), por_finalidade)
        # "any maskable" na mesma entrada é a armadilha clássica: o ícone sairia recortado ou pequeno demais
        for icone in m["icons"]:
            self.assertNotIn(" ", icone["purpose"], icone)

    def test_todo_icone_declarado_existe_e_tem_o_tamanho_prometido(self):
        for icone in _manifesto()["icons"]:
            arquivo = PASTA_PWA / icone["src"]
            self.assertTrue(arquivo.is_file(), f"{icone['src']} está no manifesto e não existe em static/pwa/")
            with Image.open(arquivo) as img:
                self.assertEqual(img.format, "PNG", icone["src"])
                self.assertEqual(f"{img.width}x{img.height}", icone["sizes"], icone["src"])

    def test_tipo_do_manifesto_no_whitenoise(self):
        tipos = MediaTypes(extra_types=settings.WHITENOISE_MIMETYPES)
        self.assertEqual(tipos.get_type("zelo.webmanifest"), "application/manifest+json")


class IconesTests(TestCase):
    def _abrir(self, nome):
        return Image.open(PASTA_PWA / nome).convert("RGBA")

    def test_any_tem_canto_transparente_e_o_z_no_centro(self):
        for nome in ("zelo-192.png", "zelo-512.png"):
            img = self._abrir(nome)
            self.assertEqual(img.getpixel((0, 0))[3], 0, f"{nome}: o canto deveria ser transparente")
            self.assertEqual(img.getpixel((img.width // 2, img.height // 2)), (255, 255, 255, 255), nome)

    def test_maskable_e_iphone_sao_quadrados_inteiros_e_opacos(self):
        azul = (85, 96, 238, 255)  # --brand (#5560ee)
        for nome in ("zelo-maskable-512.png", "zelo-apple-180.png"):
            img = self._abrir(nome)
            for canto in ((0, 0), (img.width - 1, 0), (0, img.height - 1), (img.width - 1, img.height - 1)):
                self.assertEqual(img.getpixel(canto), azul, f"{nome}: o canto {canto} precisa ser o azul da marca, opaco")

    def test_iphone_nao_tem_canal_alfa(self):
        # iOS pinta transparência de preto: o arquivo não pode nem ter canal alfa
        with Image.open(PASTA_PWA / "zelo-apple-180.png") as img:
            self.assertEqual(img.mode, "RGB")

    def test_z_do_maskable_cabe_na_zona_segura(self):
        """O Android recorta o maskable num círculo de 40% do lado a partir do centro; letra fora dele é cortada."""
        img = self._abrir("zelo-maskable-512.png")
        lado = img.width
        centro = lado / 2
        limite = 0.40 * lado
        fora = []
        for y in range(lado):
            for x in range(lado):
                r, g, b, _ = img.getpixel((x, y))
                if r > 200 and g > 200 and b > 200 and ((x - centro) ** 2 + (y - centro) ** 2) ** 0.5 > limite:
                    fora.append((x, y))
        self.assertEqual(fora, [], "pixels do Z fora da zona segura do ícone maskable")


class TelasTests(TestCase):
    def test_apple_touch_icon_e_png_em_toda_tela_renderizada(self):
        paginas = {
            "login": self.client.get(reverse("login")),
            "recuperar senha": self.client.get(reverse("password_reset")),
        }
        self.client.force_login(User.objects.create_superuser("pwa_teste", "p@p.com", "x"))
        paginas["sistema (base.html)"] = self.client.get(reverse("home"), follow=True)
        for nome, resposta in paginas.items():
            self.assertEqual(resposta.status_code, 200, nome)
            html = resposta.content.decode()
            self.assertIn('rel="manifest" href="/static/pwa/zelo.webmanifest', html, nome)
            self.assertIn('rel="apple-touch-icon" sizes="180x180" href="/static/pwa/zelo-apple-180.png', html, nome)
            self.assertIn('name="apple-mobile-web-app-title" content="Zelo"', html, nome)
            # o iPhone ignora SVG nesse atributo: se voltar, ele monta uma miniatura da página em vez da logo
            self.assertNotIn('rel="apple-touch-icon" href="/static/img/zelo-mark.svg', html, nome)

    def test_todo_shell_com_a_marca_zelo_inclui_o_parcial(self):
        """Os portais exigem login externo para renderizar; a garantia aqui é a do código-fonte."""
        for rel in SHELLS_COM_A_MARCA:
            fonte = (RAIZ_CONTROLE / rel).read_text(encoding="utf-8")
            self.assertIn("{% include '_pwa_head.html' %}", fonte, rel)

    def test_parcial_nao_vaza_comentario_na_tela(self):
        # regra 17 do CLAUDE.md: comentário de bloco tem de ser {% comment %}, senão o texto sai na página
        html = self.client.get(reverse("login")).content.decode()
        self.assertNotIn("apple-touch-icon e ignora SVG", html)
        self.assertNotIn("gerar_icones_pwa", html)


class GeradorTests(TestCase):
    def test_le_o_traco_do_z_do_svg_da_marca(self):
        pontos = gerador.pontos_do_traco(gerador.ARQUIVO_MARCA.read_text(encoding="utf-8"))
        self.assertEqual(
            pontos,
            [(12, 10.5), (28, 10.5), (28, 14.5), (18, 25.5), (28, 25.5), (28, 29.5), (12, 29.5), (12, 25.5), (22, 14.5), (12, 14.5)],
        )

    def test_recusa_marca_com_comando_que_ele_nao_sabe_desenhar(self):
        with self.assertRaises(CommandError):
            gerador.pontos_do_traco('<svg><path d="M0 0 C 1 1 2 2 3 3 Z"/></svg>')

    def test_comando_grava_os_quatro_arquivos_sem_tocar_nos_reais(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(gerador, "PASTA_SAIDA", Path(tmp)):
                call_command("gerar_icones_pwa", stdout=io.StringIO())
            gravados = sorted(p.name for p in Path(tmp).glob("*.png"))
        self.assertEqual(gravados, sorted(nome for nome, *_ in gerador.SAIDAS))
