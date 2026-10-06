"""
Painel de Consumo de Toner.

Dois tipos de coisa estão protegidos aqui.

1. O BUG QUE QUEBRAVA A TELA SEM DADOS (regra 18 do CLAUDE.md). As nove
   séries levavam `|default:"[]"` antes do `json_script`. Lista vazia é
   falsy, então o `default` a trocava pela STRING `"[]"`; o `json_script`
   serializava isso como texto JSON e o `JSON.parse` devolvia uma string de
   dois caracteres. O efeito: `.length` vira 2, `hasData` dá `true`, e o
   Chart.js recebe texto onde esperava números — o painel quebra EXATAMENTE
   quando um filtro não encontra nada, que é quando mais se olha para ele.
   Como as séries já vinham sempre como lista de `views/relatorios.py`, o
   `default` era risco sem contrapartida.

2. A COR COMO INFORMAÇÃO. A tabela de centros de custo, a barra e o donut
   mostram as MESMAS entidades na MESMA ordem. A cor é o que liga os três —
   se o ciclo da paleta no CSS sair de sincronia com o do JS, o ponto da
   linha deixa de corresponder à fatia e a tela passa a mentir, sem erro
   nenhum.
"""
import json
import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

_TELA = (
    Path(settings.BASE_DIR)
    / "ProjetoEstoque" / "templates" / "front" / "dashboards"
    / "dashboard_toner.html"
)

# Os ids dos `json_script` desta tela.
_SERIES = [
    "toner-cc-labels", "toner-cc-gasto",
    "toner-user-labels", "toner-user-gasto",
    "toner-item-labels", "toner-item-gasto",
    "toner-mes-labels", "toner-mes-gasto", "toner-mes-qtd",
]


class SeriesChegamComoListaTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.client.force_login(
            User.objects.create_superuser("toner_t", "t@t.com", "x")
        )

    def _html(self, **filtros):
        return self.client.get(
            reverse("dashboard_toner"), filtros
        ).content.decode("utf-8")

    def test_tela_abre(self):
        resp = self.client.get(reverse("dashboard_toner"))
        self.assertEqual(resp.status_code, 200)

    def test_nenhuma_serie_usa_default_antes_do_json_script(self):
        """A varredura é no ARQUIVO: é lá que o filtro é escrito."""
        texto = _TELA.read_text(encoding="utf-8")
        achados = re.findall(r'\|default:"\[\]"\s*\|json_script', texto)
        self.assertFalse(
            achados,
            f"{len(achados)} série(s) voltaram a usar `|default:\"[]\"` antes "
            "do json_script — o painel quebra quando o filtro não acha nada",
        )

    def test_serie_vazia_continua_sendo_lista_e_nao_string(self):
        """
        O teste que importa: com o banco vazio, o que chega ao navegador tem
        de ser `[]` (lista), nunca `"[]"` (string). São dois caracteres de
        diferença no HTML e a tela inteira depende disso.
        """
        html = self._html()
        for sid in _SERIES:
            m = re.search(rf'id="{sid}"[^>]*>(.*?)</script>', html, re.S)
            self.assertIsNotNone(m, f"{sid} não foi emitido")
            valor = json.loads(m.group(1))
            self.assertIsInstance(
                valor, list,
                f"{sid} chegou como {type(valor).__name__} "
                f"({m.group(1)[:20]!r}) — é o bug da regra 18",
            )

    def test_serie_continua_lista_mesmo_com_filtro_sem_resultado(self):
        """O caso real: um filtro que não casa com nada."""
        html = self._html(item="inexistente-zzz", cc="999999")
        for sid in ("toner-cc-labels", "toner-cc-gasto"):
            m = re.search(rf'id="{sid}"[^>]*>(.*?)</script>', html, re.S)
            self.assertIsNotNone(m)
            self.assertIsInstance(json.loads(m.group(1)), list)


class CorComSignificadoTests(TestCase):
    """Cada cor desta tela responde a uma pergunta; nenhuma é decoração."""

    def setUp(self):
        self.client = Client()
        self.client.force_login(
            User.objects.create_superuser("toner_c", "c@c.com", "x")
        )
        self.html = self.client.get(
            reverse("dashboard_toner")
        ).content.decode("utf-8")

    def test_cada_kpi_tem_a_propria_faixa(self):
        """
        Os quatro indicadores eram idênticos — mesmo chip cinza, mesmo ícone.
        Sem identidade própria, não dá para saltar direto ao número que se
        procura.
        """
        for modificador in ("kpi-card--gasto", "kpi-card--volume",
                            "kpi-card--cc", "kpi-card--medio"):
            self.assertIn(modificador, self.html, f"faltou {modificador}")

    def test_faixas_derivam_da_paleta_de_series(self):
        """
        As faixas saem de `--chart-*`, a MESMA paleta dos gráficos — é o que
        faz o azul do KPI e o azul da primeira série serem a mesma cor.
        Fixar um HEX aqui quebraria essa correspondência em silêncio.
        """
        texto = _TELA.read_text(encoding="utf-8")
        bloco = texto[texto.index("--td-kpi-gasto"):texto.index("--td-kpi-medio") + 120]
        self.assertEqual(
            len(re.findall(r"var\(--chart-\d\)", bloco)), 4,
            "alguma faixa de KPI deixou de derivar de --chart-*",
        )

    def test_graficos_de_categoria_usam_a_paleta_de_series(self):
        """
        A rampa `accent → cinza` comunicava relevância por saturação. Num
        donut isso falha: fatias vizinhas ficam quase iguais e a legenda vira
        a única leitura possível.
        """
        self.assertIn("paletaSeries", self.html)
        self.assertNotIn(
            "relevanceRamp", self.html,
            "a rampa monocromática voltou aos gráficos de categoria",
        )

    def test_paleta_do_js_cicla_em_oito_como_a_do_css(self):
        """
        O ponto da linha da tabela e a fatia do gráfico só correspondem
        enquanto os dois ciclos tiverem o MESMO tamanho. Se um mudar sem o
        outro, a tela passa a mentir sem dar erro.
        """
        texto = _TELA.read_text(encoding="utf-8")

        js = texto[texto.index("const RESERVA_SERIES"):]
        js = js[:js.index("]")]
        self.assertEqual(len(re.findall(r"#[0-9a-f]{6}", js)), 8)

        css = texto[texto.index(".cc-row[style*="):]
        css = css[:css.index(".cc-name {")]
        usados = set(re.findall(r"var\(--chart-(\d)\)", css))
        self.assertEqual(
            usados, {"1", "2", "3", "4", "5", "6", "7", "8"},
            "o ciclo do CSS saiu de sincronia com o do JS",
        )

    def test_linha_da_tabela_carrega_o_proprio_indice(self):
        """
        É o `--i` que liga a linha à fatia correspondente.

        A verificação é na MARCAÇÃO, não no HTML renderizado: a tabela sai do
        `{% for l in linhas %}`, então num banco sem baixa de toner ela
        renderiza vazia e o teste passaria sem ter olhado nada.
        """
        texto = _TELA.read_text(encoding="utf-8")
        self.assertIn('<tr class="cc-row" style="--i: {{ forloop.counter0 }};">', texto)

    def test_variacao_de_custo_e_vermelha_subindo_e_verde_caindo(self):
        """
        Painel de custo: subir é ruim, cair é bom. Antes subir era âmbar e
        cair era cinza — a má notícia tinha cor e a boa não tinha nenhuma.
        """
        texto = _TELA.read_text(encoding="utf-8")

        # O seletor aparece em DOIS blocos: a regra compartilhada, que reúne
        # `.up` e `.down` e define só a forma da pílula, e o bloco individual,
        # que traz a cor. Numa lista de seletores cada um fica em sua linha,
        # então "casar o seletor no início da linha" acha os dois — por isso a
        # verificação é se ALGUM bloco daquele seletor tem a cor.
        def tem_cor(seletor, cor):
            for m in re.finditer(
                rf"^{re.escape(seletor)}\s*[,{{](.*?)^\}}", texto, re.S | re.M
            ):
                if cor in m.group(1):
                    return True
            return False

        self.assertTrue(
            tem_cor(".evo-stat.up b", "var(--danger)"),
            "variação para cima deixou de ser vermelha",
        )
        self.assertTrue(
            tem_cor(".evo-stat.down b", "var(--success)"),
            "variação para baixo deixou de ser verde — economizar volta a não "
            "aparecer na tela",
        )

    def test_tela_nao_fixa_cor_da_paleta_anterior(self):
        """Ela segue o design system; a cor vem de token."""
        texto = _TELA.read_text(encoding="utf-8")
        estilo = texto[texto.index("<style>"):texto.index("</style>")]
        estilo = re.sub(r"/\*.*?\*/", "", estilo, flags=re.S)

        # Reserva de `var(--x, #hex)` NÃO é deriva: ali a tela já está ligada
        # ao token e o HEX só entra se ele faltar. É a mesma distinção da
        # regra 33 — ignorá-la acusa dezenas de declarações corretas.
        antigas = ("#0071e3", "#0078d4", "#ff3b30", "#34c759", "#1d1d1f")
        soltas = []
        for m in re.finditer(
            r"(?P<prop>--[a-z0-9-]+|[a-z-]+)\s*:\s*(?P<valor>[^;{}]*?)"
            r"(?P<hex>#[0-9a-fA-F]{6})", estilo
        ):
            if m.group("hex").lower() not in antigas:
                continue
            if m.group("valor").count("(") > m.group("valor").count(")"):
                continue                      # está dentro de um var()
            soltas.append(m.group(0).strip())

        self.assertFalse(soltas, f"cor da paleta anterior solta: {soltas}")
