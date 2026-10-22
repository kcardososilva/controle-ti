"""
A vitrine pública (`/apresentacao/`) — o filme de 30s alcançado pelo login.

Esta é a única rota de tela do sistema sem `@login_required`, e é por isso
que ela tem teste próprio. Os três riscos reais:

  • VAZAMENTO — a tela irmã `/sobre/` mostra contagem real de equipamentos,
    colaboradores e licenças, que diz o tamanho da operação da empresa. A
    vitrine é aberta, então não pode tocar no banco. O teste mede isso por
    onde não há como discordar: número de queries.

  • TELA PRETA SILENCIOSA — o conteúdo todo nasce invisível para o GSAP
    animar. Se o CDN não responder, a página seria preta e vazia com HTTP
    200 e nada em log nenhum. O fallback (`ap-sem-filme` + watchdog) é
    funcionalidade, não enfeite.

  • CÓPIA DE PALETA — shell próprio que não carrega `tokens.css` vira cópia
    que envelhece (regras 26 e 31). Aqui ele carrega, e não redefine nada.
"""
import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

_TPL = Path(settings.BASE_DIR) / "ProjetoEstoque" / "templates" / "front" / "apresentacao.html"
_TPL_LOGIN = Path(settings.BASE_DIR) / "templates" / "login.html"
_CSS = Path(settings.BASE_DIR) / "ProjetoEstoque" / "static" / "css"
_JS = Path(settings.BASE_DIR) / "ProjetoEstoque" / "static" / "js" / "apresentacao.js"
_SHELL_SENHA = Path(settings.BASE_DIR) / "templates" / "registration" / "_auth_shell.html"


def _texto(caminho):
    return caminho.read_text(encoding="utf-8")


class RotaPublicaTests(TestCase):
    def test_abre_sem_login(self):
        resposta = self.client.get(reverse("apresentacao"))
        self.assertEqual(resposta.status_code, 200)
        self.assertTemplateUsed(resposta, "front/apresentacao.html")

    def test_nao_toca_no_banco(self):
        """
        Tela aberta não conta acervo. Se alguém acrescentar um `.count()`
        aqui "só para dar credibilidade ao número", este teste cai — e é
        para cair: a contagem vai para `/sobre/`, que exige login.
        """
        with self.assertNumQueries(0):
            self.client.get(reverse("apresentacao"))

    def test_sobre_continua_exigindo_login(self):
        """A irmã com números reais não pode seguir o mesmo caminho."""
        resposta = self.client.get(reverse("sobre_plataforma"))
        self.assertEqual(resposta.status_code, 302)
        self.assertIn("/login/", resposta["Location"])

    def test_tambem_abre_para_quem_ja_entrou(self):
        User.objects.create_superuser("ap_admin", "ap@test.com", "x")
        self.client.login(username="ap_admin", password="x")
        self.assertEqual(self.client.get(reverse("apresentacao")).status_code, 200)

    def test_login_oferece_o_caminho(self):
        """Sem o link na tela de acesso a página existe e ninguém acha."""
        html = self.client.get(reverse("login")).content.decode("utf-8")
        self.assertIn(reverse("apresentacao"), html)
        self.assertIn("Conheça o Zelo", html)


class DegradacaoSemGsapTests(TestCase):
    """O pior modo de falha desta tela é abrir preta e calada."""

    def test_template_tem_watchdog_e_estado_estatico(self):
        texto = _texto(_TPL)
        self.assertIn("ap-sem-filme", texto)
        self.assertIn("ap-filme-pronto", texto)
        self.assertIn("prefers-reduced-motion", texto)

    def test_folha_sabe_desenhar_a_versao_sem_filme(self):
        css = _texto(_CSS / "apresentacao.css")
        self.assertIn(".ap-sem-filme .ap-cena", css)
        self.assertIn("prefers-reduced-motion", css)

    def test_cena_so_nasce_invisivel_quando_o_filme_vai_rodar(self):
        """
        `opacity: 0` incondicional na cena é exatamente o que deixaria a
        tela vazia quando o GSAP falhasse. A regra é escopada em
        `.ap-com-filme`, classe que o <head> só aplica se houver filme.
        """
        css = _texto(_CSS / "apresentacao.css")
        self.assertIn(".ap-com-filme .ap-cena { opacity: 0; }", css)

    def test_versao_estatica_rola(self):
        """
        O `ap-travado` do <body> vem do servidor e quem o tira é o filme.
        Se o JS não chegar, ele fica — e a página estática ficaria presa na
        primeira dobra, com o conteúdo logo abaixo e inalcançável. Dois
        cintos: a regra de CSS e o watchdog do <head>.
        """
        css = _texto(_CSS / "apresentacao.css")
        self.assertIn(".ap-html.ap-sem-filme body.ap-travado", css)
        self.assertIn("classList.remove('ap-travado')", _texto(_TPL))

    def test_js_cai_para_a_versao_estatica_sozinho(self):
        js = _texto(_JS)
        self.assertIn("if (!gsap", js)
        self.assertIn("semFilme()", js)

    def test_quantidade_de_cenas_bate_com_a_montagem(self):
        """
        O JS recusa montar o filme se o número de cenas no HTML for
        diferente do número de tempos — acrescentar cena sem acrescentar
        tempo cairia na versão estática em silêncio. O teste é o aviso.
        """
        cenas = len(re.findall(r"data-cena\b", _texto(_TPL)))
        tempos = len(re.findall(r"^\s*\[\d+\.\d+, \d+\.\d+\]", _texto(_JS), re.M))
        self.assertEqual(
            cenas, tempos,
            f"{cenas} cenas no HTML para {tempos} tempos em TEMPOS — o filme "
            f"não monta e a página cai para a versão estática.",
        )


class DesignSystemDaVitrineTests(TestCase):
    def test_carrega_tokens_antes_da_folha_propria(self):
        texto = _texto(_TPL)
        pos_tokens = texto.find("css/tokens.css")
        pos_folha = texto.find("css/apresentacao.css")
        self.assertGreater(pos_tokens, -1, "a vitrine não carrega os tokens")
        self.assertGreater(pos_folha, pos_tokens, "a folha da tela vem antes dos tokens")

    def test_nao_redefine_token_do_sistema(self):
        """Redefinir no escopo da tela vence o tokens.css e congela a paleta."""
        do_sistema = set(re.findall(
            r"^\s*(--[a-z0-9-]+)\s*:", _texto(_CSS / "tokens.css"), re.M))
        for folha in ("apresentacao.css", "cinema.css", "auth.css"):
            usados = set(re.findall(
                r"[{;]\s*(--[a-z0-9-]+)\s*:", _texto(_CSS / folha)))
            invasores = sorted(usados & do_sistema)
            self.assertFalse(
                invasores, f"{folha} redefine token do sistema: {invasores}")

    def test_acento_da_vitrine_deriva_do_brand(self):
        """
        Sobre preto o azul do sistema apaga. A saída é CLAREAR o mesmo azul,
        não escolher outro — o projeto já teve quatro azuis (regra 33).
        """
        css = _texto(_CSS / "cinema.css")
        linha = re.search(r"--ap-acento:\s*([^;]+);", css)
        self.assertIsNotNone(linha, "--ap-acento sumiu de cinema.css")
        self.assertIn("var(--brand", linha.group(1))

    def test_a_paleta_do_cinema_mora_num_lugar_so(self):
        """
        Duas telas usam esta paleta (a vitrine e o acesso no tema escuro).
        Se ela voltar a ser declarada na folha de uma delas, a outra fica
        para trás no primeiro ajuste — foi assim que o `auth.css` passou
        meses numa paleta anterior à do sistema (regras 26 e 31).
        """
        for folha in ("apresentacao.css", "auth.css"):
            self.assertNotRegex(
                _texto(_CSS / folha), r"(?m)^\s*--ap-acento\s*:",
                f"{folha} voltou a declarar a paleta do cinema; ela mora em cinema.css",
            )

    def test_cinema_e_escopado_e_nao_vaza_para_o_sistema(self):
        """
        `:root` aqui pintaria TODA tela que carrega a folha. Ela é carregada
        por telas sem `base.css` — o escopo é o que impede o preto de
        escorrer para o resto do sistema.
        """
        css = re.sub(r"/\*.*?\*/", "", _texto(_CSS / "cinema.css"), flags=re.S)
        self.assertNotIn(":root", css)
        self.assertIn(":where(.ap-page, .au-page)", css)

    def test_estatico_sem_tag_de_template(self):
        """Um .js servido cru: `{% url %}` ali vira texto literal."""
        self.assertNotRegex(_texto(_JS), r"\{%|\{\{")

    def test_folhas_versionadas_na_resposta(self):
        html = self.client.get(reverse("apresentacao")).content.decode("utf-8")
        for folha in re.findall(r'href="(/static/css/[^"]+)"', html):
            self.assertIn("?v=", folha, f"{folha} sem cache-busting")
        for script in re.findall(r'src="(/static/js/[^"]+)"', html):
            self.assertIn("?v=", script, f"{script} sem cache-busting")


class ComentarioDeTemplateTests(TestCase):
    """
    Regra 17: `{# #}` é comentário de UMA linha, e já vazou para a tela do
    usuário quatro vezes neste projeto — uma delas no próprio login. Aqui
    a varredura é na saída RENDERIZADA, que é onde o estrago aparece.
    """

    def test_nenhum_comentario_vaza_para_a_tela(self):
        html = self.client.get(reverse("apresentacao")).content.decode("utf-8")
        self.assertNotIn("{#", html)
        self.assertNotIn("{% comment %}", html)
        self.assertNotIn("endcomment", html)


class MaquetesEspelhamAsTelasReaisTests(TestCase):
    """
    As maquetes do filme reproduzem telas que existem. Não é capricho: o
    que a vitrine mostra é o que o visitante vai cobrar depois de entrar.
    Cada asserção aqui é um pedaço de UI que tem equivalente no sistema —
    se a tela real mudar de vocabulário, isto aqui é o lembrete.

    Os VALORES são fictícios de propósito (ver `test_nao_toca_no_banco`):
    a tela é pública e contagem real diz o tamanho da operação.
    """

    def setUp(self):
        self.html = self.client.get(reverse("apresentacao")).content.decode("utf-8")

    def test_lista_de_ativos_tem_as_colunas_de_equipamentos(self):
        """As mesmas cinco colunas de `front/equipamentos/_tbody.html`."""
        for coluna in ("Ativo", "Tipo", "Status", "Localidade / CC", "Lotes / Saldo"):
            self.assertIn(coluna, self.html, f"coluna {coluna!r} sumiu da maquete")
        # Rótulos que vêm de StatusItemChoices e do tipo operacional da view.
        for rotulo in ("Patrimônio", "Com lote", "Locado", "Manutenção", "Estoque"):
            self.assertIn(rotulo, self.html)

    def test_preventiva_e_de_equipamento_de_rede_com_resposta_real(self):
        """
        Checklist booleano no sistema é o par Conforme / Não conforme
        (`preventiva_exec.html`), não um check genérico. E o checklist
        mostrado é de rede — "Check List Switches" existe no cadastro.
        """
        self.assertIn("Check List Switches", self.html)
        self.assertIn("Conforme", self.html)
        self.assertIn("Não conforme", self.html)
        for campo in ("Nº Série", "Localidade", "Próxima"):
            self.assertIn(campo, self.html, f"a faixa de identificação perdeu {campo!r}")

    def test_mapa_do_quiosque_tem_o_painel_da_frota(self):
        """As três métricas clicáveis e a lista de `quiosque_mapa.html`."""
        for pedaco in ("No mapa", "Online", "Offline", "Dispositivos no mapa"):
            self.assertIn(pedaco, self.html, f"o painel da frota perdeu {pedaco!r}")

    def test_o_mapa_e_desenhado_e_nao_puxa_tile_de_fora(self):
        """
        Dois motivos para o terreno ser SVG e não um recorte do satélite
        real: a tela é pública, e um recorte entregaria a planta da
        propriedade e a posição dos aparelhos; e os tiles do ArcGIS que a
        tela real usa não são redistribuíveis fora dela. O aviso de que a
        imagem é ilustrativa fica na própria maquete.
        """
        for host in ("arcgisonline", "openstreetmap", "cartocdn", "unpkg.com/leaflet"):
            self.assertNotIn(host, self.html, f"a vitrine passou a buscar {host}")
        self.assertIn("Imagem ilustrativa", self.html)


class CarrosselTests(TestCase):
    """
    O carrossel vive na página de venda, abaixo do filme — e por isso NÃO
    pode depender do GSAP: quando o GSAP falta, é justamente a página de
    rolagem que sobra.
    """

    def test_a_secao_existe_com_mais_de_um_slide(self):
        html = self.client.get(reverse("apresentacao")).content.decode("utf-8")
        self.assertIn("data-carrossel", html)
        self.assertGreaterEqual(len(re.findall(r"data-slide\b", html)), 2)

    def test_e_ligado_antes_da_desistencia_do_filme(self):
        """
        Se a chamada ficasse depois do `return` que cai para a versão
        estática, o carrossel morreria exatamente no caso em que ele é a
        única coisa interativa da página — e sem erro nenhum no console.
        """
        js = _texto(_JS)
        chamada = js.index("ligarCarrossel();")
        desistencia = js.index("semFilme();\n    return;")
        self.assertLess(
            chamada, desistencia,
            "ligarCarrossel() ficou depois da queda para a versão estática",
        )

    def test_a_faixa_rola_sozinha_sem_javascript(self):
        """Scroll-snap nativo: sem JS a faixa ainda se arrasta com o dedo."""
        css = _texto(_CSS / "apresentacao.css")
        self.assertIn("scroll-snap-type: x mandatory", css)
        self.assertIn("scroll-snap-align", css)


class TelaDeAcessoNaLinguagemDoFilmeTests(TestCase):
    """
    O login passou a falar a língua da vitrine, com DOIS temas: claro na
    paleta do sistema, escuro no palco do filme. O risco é o de sempre
    nesta folha — ela já manteve uma cópia da paleta até ficar atrás do
    resto do sistema. Daí a regra aqui: `auth.css` não escreve cor.
    """

    URLS = ("login", "password_reset")

    def test_as_telas_de_acesso_carregam_a_folha_do_cinema_na_ordem(self):
        for nome in self.URLS:
            html = self.client.get(reverse(nome)).content.decode("utf-8")
            p_tokens = html.find("css/tokens.css")
            p_cinema = html.find("css/cinema.css")
            p_auth = html.find("css/auth.css")
            self.assertGreater(p_cinema, -1, f"{nome} não carrega cinema.css")
            self.assertLess(p_tokens, p_cinema, f"{nome}: cinema.css antes dos tokens")
            self.assertLess(p_cinema, p_auth, f"{nome}: auth.css antes do cinema.css")

    def test_o_corpo_entra_no_escopo_da_linguagem(self):
        """Sem `au-page` no <body>, nenhum token `--ap-*` resolve e a tela
        de acesso sai sem cor — a folha inteira pendura nesse escopo."""
        for nome in self.URLS:
            html = self.client.get(reverse(nome)).content.decode("utf-8")
            self.assertIn('class="au-page', html, f"{nome} sem a classe de escopo")

    def test_auth_css_nao_escreve_cor_nenhuma(self):
        """
        Todo `--au-*` é APELIDO: aponta para um token do sistema (claro) ou
        do cinema (escuro). Um valor literal aqui é uma cor que deixa de
        acompanhar os dois.
        """
        css = _texto(_CSS / "auth.css")
        soltos = []
        for nome, valor in re.findall(r"(--au-[a-z0-9-]+)\s*:\s*([^;]+);", css):
            v = valor.strip()
            if v.startswith("var(") or v in ("transparent", "none"):
                continue
            if nome.endswith(("-h1", "-lento")) or v.startswith(("clamp(", "0 ", "rgba(")):
                continue
            soltos.append(f"{nome}: {v}")
        self.assertFalse(soltos, f"auth.css fixou cor em vez de apelidar token: {soltos}")

    def test_o_escuro_do_login_e_o_mesmo_palco_do_filme(self):
        """
        O pedido era literal: tema escuro igual ao da apresentação. Se o
        bloco escuro parar de apontar para os tokens do cinema, as duas
        telas voltam a ser pretos diferentes.
        """
        css = _texto(_CSS / "auth.css")
        escuro = css[css.index(".au-page.theme-dark"):]
        escuro = escuro[:escuro.index("\n}")]
        for token in ("--ap-preto", "--ap-branco", "--ap-acento", "--ap-traco"):
            self.assertIn(token, escuro, f"o tema escuro do acesso não usa {token}")

    def test_as_camadas_de_atmosfera_estao_nas_duas_telas(self):
        for caminho in (_TPL_LOGIN, _SHELL_SENHA):
            texto = _texto(caminho)
            for camada in ("ap-luz", "ap-grao", "ap-vinheta"):
                self.assertIn(camada, texto, f"{caminho.name} sem a camada {camada}")

    def test_o_tema_e_pintado_antes_da_primeira_pintura(self):
        """
        Agora que o escuro é preto puro, aplicar o tema só depois do load
        faz a tela inteira piscar branco. A classe tem de sair do <head>.
        """
        for caminho in (_TPL_LOGIN, _SHELL_SENHA):
            texto = _texto(caminho)
            cabeca = texto[:texto.index("<body")]
            self.assertIn(
                "document.documentElement.classList.add('theme-'", cabeca,
                f"{caminho.name} decide o tema depois do <head>",
            )

    def test_a_marca_pega_o_azul_do_tema(self):
        """
        O .svg tem o azul gravado dentro e sobre preto ele apaga. Inline,
        o retângulo usa `--au-acento` — que no escuro é o azul clareado.
        """
        marca = _texto(Path(settings.BASE_DIR) / "templates" / "_marca_zelo.html")
        self.assertIn("var(--au-acento)", marca)
        for nome in self.URLS:
            html = self.client.get(reverse(nome)).content.decode("utf-8")
            self.assertIn("var(--au-acento)", html, f"{nome} não usa a marca inline")
