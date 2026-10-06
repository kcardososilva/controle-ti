"""
Design system v5 — os contratos que quebram em SILÊNCIO.

Um erro de CSS não levanta exceção: a tela abre, responde 200 e sai errada.
Os quatro modos de falhar que estes testes cobrem:

  1. TOKEN FANTASMA — uma folha consome `var(--x)` que ninguém define. O
     navegador resolve no valor inicial (transparente, `medium`, `normal`) e a
     tela sai sem cor, sem borda ou sem espaçamento, sem nenhum aviso.

  2. ORDEM DE CARREGAMENTO — `tokens.css` tem de vir ANTES de `base.css`. Fora
     de ordem, TODA cor do layout cai no valor inicial.

  3. NOME DE CLASSE REMOVIDO — 99 templates usam `.btn-primary`, 28 usam
     `.badge-success`. Renomear uma dessas no CSS não quebra nenhum teste de
     view: só apaga o estilo da tela.

  4. CACHE — sem subir ASSETS_VERSION, quem tem a folha antiga em cache recebe
     o HTML novo com o CSS velho. No computador de quem fez o deploy está tudo
     certo, o que torna isso particularmente difícil de diagnosticar.
"""
import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

_CSS = Path(settings.BASE_DIR) / "ProjetoEstoque" / "static" / "css"
_TPL = Path(settings.BASE_DIR) / "ProjetoEstoque" / "templates"
_TPL_RAIZ = Path(settings.BASE_DIR) / "templates"
_JS = Path(settings.BASE_DIR) / "ProjetoEstoque" / "static" / "js"

# ATENÇÃO: um token NÃO nasce só em regra de CSS. Dezenas deles (`--tom`,
# `--ic`, `--dx`, `--k`, `--rot`...) são definidos em `style="--tom: ..."` no
# template ou por `setProperty('--x', ...)` no JS. Por isso o padrão aceita a
# declaração depois de `{`, `;`, `"` ou `'` — e não só em início de linha.
# Exigir início de linha, como uma primeira versão deste arquivo fazia, acusa
# 60 "tokens fantasma" que estão perfeitamente definidos.
_RE_DEFINE = re.compile(r"""(?:^|\{|;|"|')\s*(--[a-z0-9-]+)\s*:""", re.M)
_RE_DEFINE_JS = re.compile(r"""setProperty\(\s*['"](--[a-z0-9-]+)""")
_RE_USA_SEM_RESERVA = re.compile(r"var\(\s*(--[a-z0-9-]+)\s*\)")


def _texto(*partes):
    return Path(*partes).read_text(encoding="utf-8")


class TokensSaoFonteUnicaTests(TestCase):
    """`tokens.css` é o único lugar onde token nasce."""

    def test_tokens_css_existe_e_define_os_tres_grupos(self):
        css = _texto(_CSS, "tokens.css")
        self.assertIn(":root", css)
        self.assertIn("body.theme-dark", css)
        for token in ("--brand", "--text-primary", "--bg-page",
                      "--radius-md", "--shadow-md", "--space-4",
                      "--text-base", "--control-h", "--chart-1"):
            self.assertRegex(
                css, rf"(?m)^\s*{re.escape(token)}\s*:", f"{token} não é definido",
            )

    def test_auth_css_nao_tem_mais_copia_local_de_tokens(self):
        """
        O login mantinha uma CÓPIA dos tokens e a cópia envelheceu: a tela de
        acesso ficou numa paleta anterior à do resto do sistema. Se um `:root`
        voltar para cá, o problema volta junto — e não dá erro.
        """
        css = _texto(_CSS, "auth.css")
        self.assertNotIn(
            ":root {", css,
            "auth.css voltou a definir tokens próprios; eles moram em tokens.css",
        )

    def test_nenhum_token_consumido_fica_sem_definicao(self):
        """
        Modo de falha nº 1: `var(--x)` SEM valor de reserva onde `--x` não é
        definido em lugar nenhum. O navegador resolve no valor inicial —
        transparente, `medium`, `normal` — e a tela sai sem cor, sem borda ou
        sem respiro, sem nada em nenhum log.

        `var(--x, reserva)` fica fora da conta: tem reserva por construção.
        """
        arquivos = list(_CSS.glob("*.css")) + list(_JS.glob("*.js"))
        for raiz in (_TPL, _TPL_RAIZ):
            arquivos += list(raiz.rglob("*.html"))

        definidos, sem_reserva = set(), {}
        for arquivo in arquivos:
            texto = arquivo.read_text(encoding="utf-8")
            definidos |= set(_RE_DEFINE.findall(texto))
            definidos |= set(_RE_DEFINE_JS.findall(texto))
            for token in _RE_USA_SEM_RESERVA.findall(texto):
                sem_reserva.setdefault(token, arquivo.name)

        fantasmas = sorted(set(sem_reserva) - definidos)
        self.assertFalse(
            fantasmas,
            "tokens usados sem reserva e sem definição em lugar nenhum: "
            + ", ".join(f"{t} (em {sem_reserva[t]})" for t in fantasmas),
        )

    def test_v4_nao_perdeu_nenhum_token(self):
        """
        Os 80 tokens da v4 são o contrato com os 174 templates e as 8 folhas
        de módulo. A v5 trocou VALORES, não nomes.
        """
        css = _texto(_CSS, "tokens.css")
        v4 = [
            "--font-display", "--font-sans", "--font-mono",
            "--bg-page", "--bg-page-2", "--bg-elevated", "--bg-elevated-2",
            "--bg-vibrancy", "--bg-vibrancy-strong", "--bg-fill-tertiary",
            "--bg-fill-secondary", "--bg-fill-primary", "--bg-hover", "--bg-pressed",
            "--text-primary", "--text-secondary", "--text-tertiary",
            "--text-quaternary", "--text-inverted", "--text-link", "--text-link-hover",
            "--separator-opaque", "--separator-non-opaque", "--separator-hairline",
            "--separator-strong",
            "--brand", "--brand-hover", "--brand-pressed", "--brand-soft",
            "--brand-softer", "--brand-ring",
            "--system-blue", "--system-green", "--system-orange", "--system-red",
            "--system-yellow", "--system-indigo", "--system-purple", "--system-pink",
            "--system-teal",
            "--success", "--success-soft", "--warning", "--warning-soft",
            "--danger", "--danger-soft", "--info", "--info-soft",
            "--shadow-xs", "--shadow-sm", "--shadow-md", "--shadow-lg",
            "--shadow-xl", "--shadow-brand",
            "--radius-xs", "--radius-sm", "--radius-md", "--radius-lg",
            "--radius-xl", "--radius-2xl", "--radius-pill",
            "--sidebar-width", "--sidebar-collapsed", "--topbar-height", "--content-max",
            "--ease-apple", "--ease-apple-out", "--ease-apple-in", "--ease",
            "--transition-fast", "--transition-base", "--transition-slow",
            "--safe-top", "--safe-bottom",
        ]
        for token in v4:
            self.assertRegex(
                css, rf"(?m)^\s*{re.escape(token)}\s*:",
                f"{token} existia na v4 e desapareceu — alguma tela depende dele",
            )

    def test_aliases_de_tint_sao_redeclarados_no_escuro(self):
        """
        Pegadinha de custom property: `--success-soft: var(--system-green-soft)`
        nasce em `:root` (html). Custom property só re-resolve o var() aninhado
        pela cascata DO ELEMENTO onde foi declarada — então trocar só o
        `--system-green-soft` no escuro (escopo body) NÃO chega ao alias, e o
        chip de status "apaga" no fundo escuro.
        """
        css = _texto(_CSS, "tokens.css")
        escuro = css[css.index("body.theme-dark"):]
        for alias in ("--success-soft", "--warning-soft", "--danger-soft", "--info-soft"):
            self.assertRegex(
                escuro, rf"(?m)^\s*{re.escape(alias)}\s*:",
                f"{alias} não é redeclarado no modo escuro",
            )


class OrdemDeCarregamentoTests(TestCase):
    """Modo de falha nº 2."""

    def setUp(self):
        self.client = Client()
        self.client.force_login(
            User.objects.create_superuser("ds_ordem", "o@o.com", "x")
        )

    def _posicoes(self, html, *arquivos):
        return [html.find(a) for a in arquivos]

    def test_base_html_carrega_tokens_antes_de_base_e_ui_kit(self):
        html = self.client.get(reverse("dashboard")).content.decode("utf-8")
        p_tokens, p_base, p_ui = self._posicoes(
            html, "css/tokens.css", "css/base.css", "css/ui-kit.css"
        )
        self.assertGreater(p_tokens, -1, "base.html não carrega tokens.css")
        self.assertGreater(p_ui, -1, "base.html não carrega ui-kit.css")
        self.assertLess(p_tokens, p_base, "tokens.css tem de vir ANTES de base.css")
        self.assertLess(p_base, p_ui, "ui-kit.css vem DEPOIS de base.css")

    def test_telas_de_autenticacao_tambem_carregam_tokens(self):
        """Sem isso o login fica sem cor — ele não carrega o base.css."""
        for url in (reverse("login"), reverse("password_reset")):
            html = Client().get(url).content.decode("utf-8")
            p_tokens, p_auth = self._posicoes(html, "css/tokens.css", "css/auth.css")
            self.assertGreater(p_tokens, -1, f"{url} não carrega tokens.css")
            self.assertLess(p_tokens, p_auth, f"{url}: ordem invertida")

    def test_as_tres_fontes_da_v5_sao_pedidas(self):
        html = self.client.get(reverse("dashboard")).content.decode("utf-8")
        for familia in ("family=Inter:", "family=Inter+Tight:", "family=JetBrains+Mono:"):
            self.assertIn(familia, html, f"{familia} não é carregada")

    def test_toda_folha_e_versionada(self):
        """Modo de falha nº 4: sem `?v=`, o navegador decide sozinho quando
        revalidar — e o projeto usa storage SEM manifesto, então não há hash
        no nome do arquivo para forçar o download."""
        html = self.client.get(reverse("dashboard")).content.decode("utf-8")
        for folha in re.findall(r'href="(/static/css/[^"]+)"', html):
            self.assertIn("?v=", folha, f"{folha} sem cache-busting")


class ClassesCompartilhadasTests(TestCase):
    """
    Modo de falha nº 3. A contagem é o argumento: estes nomes não podem ser
    renomeados sem passar nos 174 templates.
    """

    # classe, em quantos templates ela aparece hoje (piso)
    CONTRATO = [
        ("btn-primary", 90),
        ("badge-success", 25),
        ("kpi-card", 15),
        ("empty-state", 10),
    ]

    def test_classes_do_contrato_continuam_definidas(self):
        base = _texto(_CSS, "base.css")
        for classe in ("btn-primary", "btn-secondary", "btn-muted", "btn-danger",
                       "btn-outline", "btn-sm", "btn-lg", "btn-icon-only",
                       "card", "panel", "kpi-card", "dashboard-card", "stat-card",
                       "badge", "status-badge", "badge-success", "badge-warning",
                       "badge-danger", "badge-info",
                       "table-responsive", "section-title", "section-subtitle",
                       "alert", "alert-error", "alert-success", "alert-warning",
                       "alert-info", "form-grid", "form-control"):
            self.assertIn(
                f".{classe}", base,
                f".{classe} saiu do base.css — os templates que a usam perdem o estilo",
            )

    def test_quantos_templates_dependem_de_cada_classe(self):
        """Documenta o tamanho do estrago para quem pensar em renomear."""
        arquivos = list(_TPL.rglob("*.html")) + list(_TPL_RAIZ.rglob("*.html"))
        textos = [a.read_text(encoding="utf-8") for a in arquivos]
        for classe, piso in self.CONTRATO:
            quantos = sum(1 for t in textos if classe in t)
            self.assertGreaterEqual(
                quantos, piso,
                f"{classe}: esperava ao menos {piso} templates, achei {quantos}",
            )

    def test_estaticos_nao_tem_tag_de_template(self):
        """Um .css é servido cru: `{% static %}` ali vira texto literal."""
        for arquivo in _CSS.glob("*.css"):
            self.assertNotRegex(
                arquivo.read_text(encoding="utf-8"), r"\{%|\{\{",
                f"{arquivo.name} tem tag de template",
            )


class AcentoUnicoTests(TestCase):
    """
    Havia QUATRO azuis: #0a84ff no base, #0078d4 em licenças/quiosque/ficha,
    #1858a8 em equipamentos e #0071e3 em preventivas/plantas. A mesma ação
    aparecia de cor diferente dependendo do módulo.
    """

    FOLHAS_DE_MODULO = [
        ("ds-licencas.css", "--lic-brand"),
        ("ds-quiosque.css", "--kq-accent"),
        ("ds-preventivas.css", "--pv-primary"),
        ("equipamento-detalhe.css", "--ad-brand"),
        ("equipamentos-list.css", "--eq-brand"),
    ]

    def test_acento_de_cada_modulo_deriva_do_brand(self):
        for arquivo, token in self.FOLHAS_DE_MODULO:
            css = _texto(_CSS, arquivo)
            for m in re.finditer(rf"^\s*{re.escape(token)}\s*:\s*([^;]+);", css, re.M):
                valor = m.group(1).strip()
                self.assertIn(
                    "var(--brand", valor,
                    f"{arquivo}: {token} fixa a cor em {valor!r} em vez de "
                    f"derivar de --brand — mudar a cor do sistema não chega nesta tela",
                )

    def test_claro_e_escuro_tem_brand_diferente(self):
        """Azul fechado o bastante para texto no claro é ilegível no escuro."""
        css = _texto(_CSS, "tokens.css")
        claro = re.search(r":root \{(.*?)\n\}", css, re.S).group(1)
        escuro = css[css.index("body.theme-dark"):]
        v_claro = re.search(r"^\s*--brand\s*:\s*([^;]+);", claro, re.M).group(1).strip()
        v_escuro = re.search(r"^\s*--brand\s*:\s*([^;]+);", escuro, re.M).group(1).strip()
        self.assertNotEqual(v_claro, v_escuro)


class CatalogoDoDesignSystemTests(TestCase):
    """A tela de referência: se ela abre, as folhas resolvem."""

    def setUp(self):
        self.client = Client()
        self.staff = User.objects.create_superuser("ds_cat", "c@c.com", "x")

    def test_exige_staff(self):
        comum = User.objects.create_user("comum_ds", "k@k.com", "SenhaK!2026")
        self.client.force_login(comum)
        resp = self.client.get(reverse("design_system"))
        self.assertIn(resp.status_code, (302, 403),
                      "o catálogo não é tela de operação")

    def test_abre_para_staff_com_todos_os_componentes(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("design_system"))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        # Um por família de componente: se a marcação de um sumir, o
        # catálogo deixa de documentá-lo e ninguém nota.
        for marca in ("ui-kpi-value", "ui-trend", "ui-empty", "ui-skeleton",
                      "ui-segmented", "ui-switch", "ui-toolbar", "ui-filtro-chip",
                      "ui-pager-link", "ui-dica", "ui-toast", "ui-progress",
                      "ui-timeline", "ui-kbd", "ui-avatar-lg", "ui-dl-label",
                      "badge-dot-lead", "btn-ghost", "btn-group", "table-zebra",
                      'aria-sort="ascending"', 'aria-invalid="true"',
                      "card-foot", "is-carregando"):
            self.assertIn(marca, html, f"o catálogo não mostra {marca}")

    def test_catalogo_nao_duplica_estilo_de_componente(self):
        """
        O CSS da tela é só a grade do catálogo. Se ela copiar o estilo dos
        componentes, o catálogo passa a mentir na primeira mudança de token.
        """
        tpl = _texto(_TPL, "front", "sistema", "design_system.html")
        estilo = tpl[tpl.index("{% block extra_css %}"):tpl.index("{% endblock %}", tpl.index("{% block extra_css %}"))]
        for proibido in (".btn-primary", ".badge", ".ui-kpi", ".ui-toast",
                         ".ui-timeline", ".table-responsive"):
            self.assertNotIn(
                proibido + " {", estilo,
                f"o catálogo redefine {proibido} em vez de consumir a folha real",
            )

    def test_estilo_do_catalogo_e_escopado(self):
        """
        Regra do CLAUDE.md: tela nunca sobrescreve token no `:root`.

        A verificação tira os comentários ANTES de procurar. O comentário no
        topo do `<style>` desta tela explica justamente que não se usa
        `:root` — e procurar a palavra crua acusaria o comentário que
        documenta a regra.
        """
        tpl = _texto(_TPL, "front", "sistema", "design_system.html")
        ini = tpl.index("{% block extra_css %}")
        estilo = tpl[ini:tpl.index("{% endblock %}", ini)]
        sem_comentario = re.sub(r"/\*.*?\*/", "", estilo, flags=re.S)
        self.assertNotRegex(sem_comentario, r":root\s*[,{]")
        self.assertIn(".ds-page", sem_comentario)


class TelasExistentesAindaAbremTests(TestCase):
    """
    A v5 trocou valores em componentes que 174 templates usam. Nenhuma troca
    de valor deveria quebrar render — mas "deveria" não é verificação.
    """

    ROTAS = [
        "dashboard", "equipamentos_list", "licencas_dashboard",
        "preventiva_dashboard", "usuario_dashboard", "alertas_dashboard",
        "movimentacao_list", "fornecedor_list", "categorias_list",
        "subtipo_list", "localidade_list", "funcoes_list",
        "planta_list", "requisicoes_list", "design_system",
    ]

    def setUp(self):
        self.client = Client()
        self.client.force_login(
            User.objects.create_superuser("ds_render", "r@r.com", "x")
        )

    def test_rotas_principais_respondem_200(self):
        falhas = []
        for nome in self.ROTAS:
            try:
                codigo = self.client.get(reverse(nome)).status_code
            except Exception as erro:  # noqa: BLE001
                falhas.append(f"{nome}: {type(erro).__name__}: {erro}")
                continue
            if codigo != 200:
                falhas.append(f"{nome}: HTTP {codigo}")
        self.assertFalse(falhas, "telas que pararam de abrir: " + "; ".join(falhas))


class MovimentoRespeitaPreferenciaTests(TestCase):
    """
    Animação disparada por rolagem ou troca de tela provoca enjoo em quem tem
    sensibilidade vestibular. `prefers-reduced-motion` não é preferência
    estética.
    """

    def test_base_desliga_animacao_quando_pedido(self):
        css = _texto(_CSS, "base.css")
        self.assertIn("prefers-reduced-motion", css)

    def test_skeleton_para_de_brilhar_quando_pedido(self):
        css = _texto(_CSS, "ui-kit.css")
        self.assertIn("prefers-reduced-motion", css)
        reduzido = css[css.index("prefers-reduced-motion"):]
        self.assertIn("ui-skeleton", reduzido)


class HigieneDeTemplateTests(TestCase):
    """
    Varredura que devia existir desde a primeira vez.

    `{# ... #}` do Django é comentário de UMA LINHA. O `tag_re` do motor de
    template casa `{#.*?#}` SEM `re.DOTALL`, então um `{# #}` que atravessa
    linha não é reconhecido como comentário: o texto é emitido como conteúdo e
    aparece na tela do usuário.

    Já aconteceu QUATRO vezes neste projeto (Quiosque, Preventivas, login.html
    e a própria tela de catálogo do design system). É a regra 17 do CLAUDE.md,
    e regra que se repete quatro vezes não é falta de atenção — é falta de
    teste.
    """

    # `{#` seguido de qualquer coisa que não feche, uma quebra de linha, e só
    # então o `#}`.
    _RE_MULTILINHA = re.compile(r"\{#(?:[^#]|#(?!\}))*?\n(?:[^#]|#(?!\}))*?#\}")

    def test_nenhum_comentario_de_uma_linha_atravessa_linha(self):
        achados = []
        for raiz in (_TPL, _TPL_RAIZ):
            for arquivo in raiz.rglob("*.html"):
                texto = arquivo.read_text(encoding="utf-8")
                for m in self._RE_MULTILINHA.finditer(texto):
                    linha = texto[:m.start()].count("\n") + 1
                    achados.append(f"{arquivo.name}:{linha}")

        self.assertFalse(
            achados,
            "comentário {# #} atravessando linha — o Django RENDERIZA esse "
            "texto para o usuário. Usar {% comment %}...{% endcomment %}. "
            "Em: " + ", ".join(achados),
        )
