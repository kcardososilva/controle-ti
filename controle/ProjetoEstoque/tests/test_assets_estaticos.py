"""
Contrato dos arquivos de CSS externos (ver static/css/ e ASSETS_VERSION).

O CSS do layout interno e dos 4 design systems de módulo vivia dentro de
`<style>` no template — ~3.350 linhas que iam no HTML a cada page-load e que o
navegador nunca podia cachear. Depois da extração, estes testes impedem a
regressão mais provável: alguém voltar a colar CSS no template "só pra testar"
e aquilo ficar.

Também travam o cache-busting: sem `?v=`, um usuário com a folha antiga em
cache vê a tela quebrada depois do deploy.
"""
import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.template.loader import render_to_string
from django.test import TestCase, RequestFactory

from ProjetoEstoque.context_processors import ASSETS_VERSION

_TEMPLATES = Path(settings.BASE_DIR) / "ProjetoEstoque" / "templates"
_CSS = Path(settings.BASE_DIR) / "ProjetoEstoque" / "static" / "css"

# Teto generoso: a ideia não é proibir CSS inline de tela (isso é legítimo e
# existe em dezenas de templates), e sim impedir que a folha COMPARTILHADA
# volte pra dentro do HTML.
_MAX_LINHAS_STYLE_NO_BASE = 40

_DESIGN_SYSTEMS = [
    ("front/dashboards/_dash_design_system.html", "ds-dashboards.css"),
    ("front/licencas/_lic_design_system.html", "ds-licencas.css"),
    ("front/preventivas/_pv_design_system.html", "ds-preventivas.css"),
    ("front/quiosque/_kq_design_system.html", "ds-quiosque.css"),
]


class CssExternoTests(TestCase):
    def test_base_html_nao_tem_folha_inline(self):
        texto = (_TEMPLATES / "base.html").read_text(encoding="utf-8")
        inline = sum(
            b.count("\n") for b in re.findall(r"<style.*?</style>", texto, re.S)
        )
        self.assertLessEqual(
            inline, _MAX_LINHAS_STYLE_NO_BASE,
            f"base.html voltou a ter {inline} linhas de CSS inline — a folha do "
            f"layout pertence a static/css/base.css.",
        )

    def test_base_html_referencia_a_folha_externa(self):
        texto = (_TEMPLATES / "base.html").read_text(encoding="utf-8")
        self.assertIn("css/base.css", texto)
        self.assertIn("assets_version", texto)

    def test_arquivos_css_existem_e_tem_conteudo(self):
        esperados = ["base.css"] + [css for _, css in _DESIGN_SYSTEMS]
        for nome in esperados:
            caminho = _CSS / nome
            self.assertTrue(caminho.is_file(), f"{nome} não existe")
            self.assertGreater(
                len(caminho.read_text(encoding="utf-8")), 500,
                f"{nome} está vazio ou truncado",
            )

    def test_design_systems_sao_link_e_nao_style(self):
        for partial, css in _DESIGN_SYSTEMS:
            texto = (_TEMPLATES / partial).read_text(encoding="utf-8")
            self.assertNotIn(
                "<style", texto,
                f"{partial} voltou a embutir CSS — o lugar é static/css/{css}",
            )
            self.assertIn(css, texto, f"{partial} não referencia {css}")
            self.assertIn("assets_version", texto, f"{partial} sem cache-busting")


class CacheBustingTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_superuser("asset_teste", "a@t.com", "x")

    def _renderizar_base(self):
        request = self.factory.get("/")
        request.user = self.user
        return render_to_string("base.html", request=request)

    def test_url_do_css_sai_versionada(self):
        html = self._renderizar_base()
        self.assertIn(f"css/base.css?v={ASSETS_VERSION}", html)

    def test_versao_nao_fica_vazia(self):
        """`?v=` sem valor não fura cache nenhum — seria pior que não ter."""
        self.assertTrue(ASSETS_VERSION.strip())
        html = self._renderizar_base()
        self.assertNotIn("?v=\"", html)
        self.assertNotIn("?v=&", html)
