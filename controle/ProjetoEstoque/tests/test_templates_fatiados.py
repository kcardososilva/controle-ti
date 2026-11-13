"""
Telas grandes fatiadas: CSS e JS fora do template (ver static/css/, static/js/).

planta_editor.html tinha 5.064 linhas, equipamentos_list.html 2.851 e
equipamento_detalhe.html 2.535 — arquivos que não cabem na cabeça de quem vai
mexer, e cujo CSS/JS ia no HTML a cada page-load.

O risco da extração está TODO na ponte servidor → JS do editor de plantas: um
arquivo .js não passa pelo motor de template, então URL, token e nome de
arquivo têm que chegar por `json_script`. Se uma chave de `_config_editor` for
renomeada ou esquecida, o editor abre em branco e o erro só aparece no console
do navegador — nenhuma tela quebra no servidor. Daí estes testes.
"""
import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.urls import reverse

from ProjetoEstoque.models import PlantaProjeto

_TEMPLATES = Path(settings.BASE_DIR) / "ProjetoEstoque" / "templates" / "front"
_JS = Path(settings.BASE_DIR) / "ProjetoEstoque" / "static" / "js"
_CSS = Path(settings.BASE_DIR) / "ProjetoEstoque" / "static" / "css"

# template, teto de linhas, css extraído, js extraído
_FATIADOS = [
    ("plantas/planta_editor.html", 900, "planta-editor.css", "planta-editor.js"),
    ("equipamentos/equipamentos_list.html", 700, "equipamentos-list.css", "equipamentos-list.js"),
    ("equipamentos/equipamento_detalhe.html", 1500, "equipamento-detalhe.css", "equipamento-detalhe.js"),
]


class TamanhoDosTemplatesTests(TestCase):
    def test_templates_ficaram_abaixo_do_teto(self):
        for nome, teto, _, _ in _FATIADOS:
            linhas = (_TEMPLATES / nome).read_text(encoding="utf-8").count("\n")
            self.assertLess(
                linhas, teto,
                f"{nome} está com {linhas} linhas (teto {teto}). Se cresceu de "
                f"novo por CSS/JS inline, o lugar deles é static/.",
            )

    def test_arquivos_extraidos_existem(self):
        for nome, _, css, js in _FATIADOS:
            self.assertTrue((_CSS / css).is_file(), f"{css} não existe")
            self.assertTrue((_JS / js).is_file(), f"{js} não existe")

    def test_estaticos_nao_tem_tag_de_template(self):
        """Um .js/.css é servido cru: `{% url %}` ali vira texto literal e o
        script quebra em silêncio."""
        for arquivo in list(_JS.glob("*.js")) + list(_CSS.glob("*.css")):
            texto = arquivo.read_text(encoding="utf-8")
            self.assertNotRegex(
                texto, r"\{%|\{\{",
                f"{arquivo.name} tem tag de template — dado do servidor entra "
                f"por json_script, não interpolado no estático",
            )


class ConfigEditorPlantaTests(TestCase):
    """A ponte servidor → planta-editor.js."""

    # Chaves lidas no topo de planta-editor.js (const CFG). Tirar uma daqui
    # sem tirar do JS deixa o editor sem URL de autosave.
    CHAVES = {
        "salvar_url", "check_version_url", "historico_url", "restaurar_base",
        "item_search_url", "prtg_search_url", "prtg_status_url",
        "csrf_token", "bg_url", "prtg_ok", "layout_version", "planta_nome",
    }

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_superuser("plt_teste", "p@t.com", "x")
        self.client.force_login(self.user)
        self.planta = PlantaProjeto.objects.create(
            nome="Planta de teste", layout={"elements": [], "connections": []},
            criado_por=self.user, atualizado_por=self.user,
        )

    def _abrir(self):
        return self.client.get(reverse("planta_editor", args=[self.planta.pk]))

    def test_editor_abre(self):
        self.assertEqual(self._abrir().status_code, 200)

    def test_config_tem_exatamente_as_chaves_que_o_js_le(self):
        cfg = self._abrir().context["editor_config"]
        self.assertEqual(set(cfg), self.CHAVES)

    def test_urls_da_config_sao_resolvidas_e_nao_literais(self):
        cfg = self._abrir().context["editor_config"]
        for chave in ("salvar_url", "check_version_url", "historico_url",
                      "item_search_url", "prtg_search_url", "prtg_status_url"):
            valor = cfg[chave]
            self.assertTrue(valor.startswith("/"), f"{chave} = {valor!r}")
            self.assertNotIn("{%", valor)

    def test_js_le_todas_as_chaves_da_config(self):
        """Contrato nos dois sentidos: chave entregue que o JS não lê é peso
        morto; chave lida que não é entregue é `undefined` em produção."""
        js = (_JS / "planta-editor.js").read_text(encoding="utf-8")
        lidas = set(re.findall(r"\bCFG\.(\w+)", js))
        self.assertEqual(
            lidas, self.CHAVES,
            f"só no JS: {lidas - self.CHAVES} | só na view: {self.CHAVES - lidas}",
        )

    def test_editor_monta_todas_as_fatias(self):
        """
        A marcação do editor foi partida em seis `_pe_*.html`. Um include com
        caminho errado não levanta erro — renderiza vazio —, então a defesa é
        conferir que um elemento-âncora de cada fatia chegou ao HTML. Os ids
        aqui são os que `planta-editor.js` procura; perder um deixa o editor
        aberto e inerte.
        """
        html = self._abrir().content.decode("utf-8")
        ancoras = {
            "toolbar": 'id="btnSalvar"',
            "avisos": 'id="conflitoBanner"',
            "paleta": 'class="pe-palette"',
            "canvas": 'class="pe-workspace"',
            "propriedades": 'id="btnPropsToggle"',
            "modais": 'id="ctxMenu"',
        }
        for fatia, ancora in ancoras.items():
            self.assertIn(ancora, html, f"a fatia {fatia} não chegou ao HTML")
        self.assertNotIn("_pe_", html, "sobrou nome de include no HTML")

    def test_fatias_do_editor_existem(self):
        for fatia in ("toolbar", "avisos", "paleta", "canvas", "propriedades", "modais"):
            caminho = _TEMPLATES / "plantas" / f"_pe_{fatia}.html"
            self.assertTrue(caminho.is_file(), f"_pe_{fatia}.html não existe")

    def test_template_entrega_os_dois_json_script_antes_do_editor(self):
        html = self._abrir().content.decode("utf-8")
        pos_layout = html.find('id="__layout_data"')
        pos_config = html.find('id="__editor_config"')
        pos_script = html.find("planta-editor.js")
        self.assertGreater(pos_layout, -1, "faltou o json_script do layout")
        self.assertGreater(pos_config, -1, "faltou o json_script da config")
        self.assertLess(pos_layout, pos_script, "layout tem que vir antes do script")
        self.assertLess(pos_config, pos_script, "config tem que vir antes do script")

    def test_planta_sem_imagem_de_fundo_manda_bg_nulo(self):
        """O JS faz `CFG.bg_url || null` — string vazia funcionaria, mas None
        é o que diz 'não há fundo' sem depender de falsy."""
        self.assertIsNone(self._abrir().context["editor_config"]["bg_url"])


class FichaDoEquipamentoFatiadaTests(TestCase):
    """
    As 7 abas da ficha saíram para `_ad_tab_*.html`. O include herda o
    contexto, então o jeito de uma fatia quebrar é silencioso: `{% load %}`
    esquecido num pedaço que usa `intcomma`, ou uma aba que deixou de ser
    renderizada porque o `{% if %}` de guarda ficou na fatia errada.
    """

    ABAS = ["ficha", "historico", "manutencao", "financeiro",
            "lotes", "monitoracao", "ninja"]

    def setUp(self):
        from ProjetoEstoque.models import Categoria, Item, Subtipo

        self.client = Client()
        self.user = User.objects.create_superuser("ficha_teste", "f@t.com", "x")
        self.client.force_login(self.user)

        cat = Categoria.objects.create(
            nome="Informática", criado_por=self.user, atualizado_por=self.user
        )
        subtipo = Subtipo.objects.create(
            nome="Notebook", alocado="sim", categoria=cat,
            criado_por=self.user, atualizado_por=self.user,
        )
        self.item = Item.objects.create(
            nome="Notebook da ficha", numero_serie="SN-FICHA-1",
            subtipo=subtipo, criado_por=self.user, atualizado_por=self.user,
        )

    def test_todas_as_fatias_existem(self):
        for aba in self.ABAS:
            caminho = _TEMPLATES / "equipamentos" / f"_ad_tab_{aba}.html"
            self.assertTrue(caminho.is_file(), f"_ad_tab_{aba}.html não existe")

    def test_fatia_que_usa_humanize_carrega_humanize(self):
        """`intcomma` sem `{% load humanize %}` é TemplateSyntaxError na hora
        de renderizar aquela aba — e só aquela aba."""
        for aba in self.ABAS:
            texto = (_TEMPLATES / "equipamentos" / f"_ad_tab_{aba}.html").read_text(encoding="utf-8")
            if re.search(r"\|(intcomma|naturalday|naturaltime|apnumber|intword)", texto):
                self.assertRegex(
                    texto, r"\{%\s*load\b[^%]*humanize",
                    f"_ad_tab_{aba}.html usa filtro de humanize sem carregar a lib",
                )

    def test_ficha_abre_e_monta_as_abas_sempre_visiveis(self):
        """Ficha, histórico e monitoração não têm `{% if %}` de guarda: têm de
        aparecer para qualquer item, inclusive um recém-cadastrado."""
        resp = self.client.get(reverse("equipamento_detalhe", args=[self.item.pk]))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        for aba in ("ficha", "historico", "monitoracao"):
            self.assertIn(f'id="tab-{aba}"', html, f"a aba {aba} não foi renderizada")

    def test_voltar_qr_e_editar_ficam_no_corpo_e_nao_na_barra_superior(self):
        """Pedido de 2026-11-13: os três botões saíram da barra superior
        (`header_actions` → `.quick-actions`) para o cartão de identidade do ativo
        (`.ad-tools`). Voltar/QR/Editar na barra de novo seria regressão."""
        html = self.client.get(
            reverse("equipamento_detalhe", args=[self.item.pk]) + "?next=/equipamentos/%3Fq%3Dnote"
        ).content.decode("utf-8")
        topo = re.search(r'<div class="quick-actions">(.*?)</div>\s*(?:\{#|<div class="notif-wrap")', html, re.S)
        self.assertIsNotNone(topo, "a barra superior (.quick-actions) sumiu do base.html")
        for destino in (reverse("equipamento_qr", args=[self.item.pk]), reverse("item_update", args=[self.item.pk])):
            self.assertNotIn(destino, topo.group(1), "botão do detalhe voltou para a barra superior")
        self.assertNotIn("Voltar", topo.group(1))
        tools = re.search(r'<div class="ad-tools"[^>]*>(.*?)</div>', html, re.S)
        self.assertIsNotNone(tools, "faltam as ferramentas do cadastro no cartão do ativo")
        corpo = tools.group(1)
        self.assertIn(reverse("equipamento_qr", args=[self.item.pk]), corpo)
        self.assertIn(reverse("item_update", args=[self.item.pk]), corpo)
        self.assertIn("Voltar", corpo)
        # o "voltar" continua levando de volta à lista COM os filtros (next), e o editar leva o next adiante
        self.assertIn('href="/equipamentos/?q=note"', corpo)
        self.assertIn("next=/equipamentos/%3Fq%3Dnote", corpo)

    def test_ficha_nao_deixou_include_sem_resolver(self):
        """Um caminho de include errado não levanta erro: o Django renderiza
        vazio (ou o texto cru, dependendo do debug). Conferir a marcação de
        cada painel é o que pega isso."""
        html = self.client.get(
            reverse("equipamento_detalhe", args=[self.item.pk])
        ).content.decode("utf-8")
        self.assertNotIn("_ad_tab_", html)
        self.assertIn("ad-tab-panel", html)
