"""
Menu lateral e coerência visual do sistema.

Tudo aqui falha em silêncio: o menu abre, a tela responde 200, e sai errada.

O que motivou cada bloco:

  • ÍCONES — seis itens usavam Font Awesome SÓLIDO e onze usavam SVG em
    OUTLINE. Glifo preenchido e traço fino de 1.75 ao lado um do outro leem
    como dois conjuntos de ícones diferentes empilhados na mesma lista.

  • GEOMETRIA — a linha-guia do submenu estava em `left: 23px`, o recuo do
    sub-link em `34px` e o marcador em `-14px`. Três números calculados à mão
    a partir do ícone de 32px: mudar o ícone desalinhava os três.

  • FONTE — cinco folhas de módulo e ~40 templates declaravam `"Segoe UI"`
    ANTES da pilha do sistema. Segoe UI existe em toda máquina Windows, então
    vencia sempre: metade das telas não usava a fonte do sistema.

  • SHELL PRÓPRIO — os dois portais mantinham uma cópia inteira da paleta
    (claro E escuro). O Portal do Fornecedor rodava no desenho anterior
    enquanto o sistema interno já tinha migrado.
"""
import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

_CSS = Path(settings.BASE_DIR) / "ProjetoEstoque" / "static" / "css"
_TPL = Path(settings.BASE_DIR) / "ProjetoEstoque" / "templates"
_BASE_HTML = _TPL / "base.html"


def _texto(caminho):
    return caminho.read_text(encoding="utf-8")


class IconesDoMenuTests(TestCase):
    def test_nenhum_icone_do_menu_usa_font_awesome(self):
        """
        `.nav-icon` tem de ser sempre o sprite em outline. A seta de expandir
        (`.nav-arrow`) segue no Font Awesome de propósito — é um chevron, não
        um ícone de categoria.
        """
        html = _texto(_BASE_HTML)
        sobras = re.findall(r'<i class="fa-[^"]*nav-icon[^"]*"', html)
        self.assertFalse(
            sobras,
            "ícone de menu em Font Awesome sólido no meio de um conjunto "
            f"outline: {sobras}",
        )

    def test_todo_use_do_menu_aponta_para_um_simbolo_existente(self):
        """
        `<use href="#ic-x">` para um símbolo inexistente não dá erro: o SVG
        renderiza VAZIO e o item fica sem ícone, só com o espaço reservado.
        """
        html = _texto(_BASE_HTML)
        definidos = set(re.findall(r'<symbol id="(ic-[a-z-]+)"', html))
        usados = set(re.findall(r'<use href="#(ic-[a-z-]+)"', html))
        self.assertTrue(definidos, "o sprite de ícones sumiu do base.html")
        self.assertFalse(
            usados - definidos,
            f"itens apontam para símbolo inexistente: {sorted(usados - definidos)}",
        )

    def test_todo_item_de_menu_tem_icone(self):
        html = _texto(_BASE_HTML)
        # Cada <span class="nav-icon"> tem de conter um <use>.
        spans = re.findall(r'<span class="nav-icon"[^>]*>(.*?)</span>', html, re.S)
        self.assertGreaterEqual(len(spans), 14, "itens de menu a menos que o esperado")
        for conteudo in spans:
            self.assertIn("<use href=\"#ic-", conteudo)

    def test_icone_e_traco_e_nao_preenchimento(self):
        css = _texto(_CSS / "base.css")
        bloco = css[css.index(".nav-icon svg {"):]
        bloco = bloco[:bloco.index("}")]
        self.assertIn("fill: none", bloco)
        self.assertIn("stroke: currentColor", bloco)


class GeometriaDoMenuTests(TestCase):
    """
    A linha-guia, o recuo do sub-link e o marcador têm de SAIR do mesmo token
    do ícone. Voltar a escrever o número à mão é o que desalinhava os três.
    """

    def test_geometria_vem_de_token_escopado_na_sidebar(self):
        css = _texto(_CSS / "base.css")
        bloco = css[css.index(".sidebar {\n  --nav-icon"):]
        bloco = bloco[:bloco.index("}")]
        for token in ("--nav-icon", "--nav-pad-x", "--nav-gap",
                      "--nav-icon-eixo", "--nav-recuo"):
            self.assertIn(token, bloco, f"{token} não é definido")

    def test_tokens_do_menu_nao_vazam_para_o_root(self):
        """Regra 6 do CLAUDE.md: escopo, nunca `:root`."""
        css = _texto(_CSS / "base.css")
        raiz = re.search(r"^:root\s*\{(.*?)^\}", css, re.S | re.M)
        if raiz:
            self.assertNotIn("--nav-icon", raiz.group(1))

    def test_linha_guia_e_marcador_sao_calculados(self):
        css = _texto(_CSS / "base.css")
        guia = css[css.index(".submenu-inner::before {"):]
        guia = guia[:guia.index("}")]
        self.assertIn("--nav-icon-eixo", guia,
                      "a linha-guia voltou a usar um número fixo")

        marcador = css[css.index(".sub-link::before {"):]
        marcador = marcador[:marcador.index("}")]
        self.assertIn("--nav-icon-eixo", marcador)
        self.assertIn("--nav-recuo", marcador)

    def test_item_ativo_tem_os_tres_sinais(self):
        """Fundo + cor + barra indicadora. Um só não basta num menu longo."""
        css = _texto(_CSS / "base.css")
        ativo = css[css.index(".nav-item.is-active,\n.nav-button.is-active {"):]
        ativo = ativo[:ativo.index("}")]
        self.assertIn("var(--brand-soft)", ativo)
        self.assertIn("var(--brand)", ativo)
        self.assertIn("::before", css[css.index(".nav-item::before,"):][:60])


class UmaFonteNoSistemaTests(TestCase):
    """
    `"Segoe UI"` na FRENTE da pilha vence em toda máquina Windows, e a reserva
    nunca entra. O certo é a fonte do sistema primeiro, Segoe UI como reserva.
    """

    def test_nenhuma_folha_forca_segoe_ui_na_frente(self):
        erros = []
        for arquivo in _CSS.glob("*.css"):
            for m in re.finditer(r"(--[a-z0-9-]*font[a-z0-9-]*)\s*:\s*([^;]+);",
                                 arquivo.read_text(encoding="utf-8")):
                valor = m.group(2).strip()
                if valor.startswith('"Segoe UI"') or valor.startswith("'Segoe UI'"):
                    erros.append(f"{arquivo.name}: {m.group(1)}")
        self.assertFalse(erros, "folha com Segoe UI à frente da pilha: " + str(erros))

    def test_nenhum_template_de_tela_forca_segoe_ui_na_frente(self):
        """
        As telas de PDF e de impressão ficam de fora: são documento gerado,
        não carregam webfont e têm de ser autossuficientes.
        """
        erros = []
        for arquivo in _TPL.rglob("*.html"):
            if "_pdf" in arquivo.name or "_print" in arquivo.name:
                continue
            texto = arquivo.read_text(encoding="utf-8")
            for m in re.finditer(r"(--[a-z0-9-]*font[a-z0-9-]*|font-family)\s*:\s*([^;]+);",
                                 texto):
                valor = m.group(2).strip()
                if valor.startswith('"Segoe UI"') or valor.startswith("'Segoe UI'"):
                    erros.append(f"{arquivo.name}: {m.group(1)}")
        self.assertFalse(
            erros,
            "tela com Segoe UI à frente da pilha do sistema — ela vence em "
            "qualquer Windows e a tela fica com outra fonte: " + str(erros),
        )

    def test_graficos_seguem_a_fonte_do_body(self):
        """
        Repetir a pilha de fontes no Chart.js significa manter onze arquivos
        em sincronia à mão. `getComputedStyle(document.body)` segue sozinho.
        """
        erros = []
        for arquivo in _TPL.rglob("*.html"):
            texto = arquivo.read_text(encoding="utf-8")
            for m in re.finditer(r"Chart\.defaults\.font\.family\s*=\s*(.+)", texto):
                if "getComputedStyle" not in m.group(1):
                    erros.append(f"{arquivo.name}: {m.group(1).strip()[:50]}")
        self.assertFalse(erros, "gráfico com pilha de fontes própria: " + str(erros))


class TelasComShellProprioTests(TestCase):
    """
    Dezenove templates têm `<!DOCTYPE>` próprio. Os de PDF/impressão devem
    continuar autossuficientes; os que são TELA precisam dos tokens, senão
    mantêm uma cópia da paleta que envelhece — foi o que aconteceu com os dois
    portais, que ficaram no desenho anterior.
    """

    SHELLS_DE_TELA = [
        "front/portal/portal_base.html",
        "front/portal_licencas/portal_licencas_base.html",
        "front/sobre_plataforma.html",
        "front/apresentacao.html",
        "front/dashboards/avisos_contrato_vencer.html",
        "front/dashboards/validacao_custos_planilha.html",
        # As duas telas de "sem acesso" ficaram de fora da primeira poda
        # porque a lista mirou os `*_base.html`. São pouco vistas — aparecem
        # quando um login externo perde o vínculo — e é por isso que uma
        # cópia de paleta sobreviveria ali indefinidamente.
        "front/portal/portal_sem_acesso.html",
        "front/portal_licencas/portal_licencas_sem_acesso.html",
    ]

    def test_shell_de_tela_carrega_os_tokens(self):
        for rel in self.SHELLS_DE_TELA:
            texto = _texto(_TPL / rel)
            self.assertIn("css/tokens.css", texto, f"{rel} não carrega os tokens")

    def test_shell_de_tela_nao_redefine_token_do_sistema(self):
        """
        Redefinir no template vence o tokens.css (mesma especificidade,
        declarado depois) — e a tela congela na paleta do dia em que a cópia
        foi feita.
        """
        tokens = _texto(_CSS / "tokens.css")
        do_sistema = set(re.findall(r"^\s*(--[a-z0-9-]+)\s*:", tokens, re.M))

        for rel in self.SHELLS_DE_TELA:
            texto = _texto(_TPL / rel)
            # `[{;]` antes evita casar o nome dentro de um `var(--x)`.
            redefinidos = {
                k for k in re.findall(r"[{;]\s*(--[a-z0-9-]+)\s*:", texto)
                if k in do_sistema
            }
            self.assertFalse(
                redefinidos,
                f"{rel} redefine token do sistema: {sorted(redefinidos)}",
            )

    def test_portais_continuam_abrindo(self):
        """A poda tirou 61 declarações de dentro dos dois portais."""
        from ProjetoEstoque.models import (
            Fornecedor,
            GRUPO_FORNECEDOR,
            PerfilFornecedor,
        )
        from django.contrib.auth.models import Group

        user = User.objects.create_user("forn_menu", "f@f.com", "x")
        user.groups.add(Group.objects.get_or_create(name=GRUPO_FORNECEDOR)[0])
        empresa = Fornecedor.objects.create(
            nome="Externa", cnpj="22.222.222/2222-22",
            criado_por=user, atualizado_por=user,
        )
        PerfilFornecedor.objects.create(
            usuario=user, fornecedor=empresa, criado_por=user, atualizado_por=user
        )

        cliente = Client()
        cliente.force_login(user)
        resp = cliente.get(reverse("portal_home"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("css/tokens.css", resp.content.decode("utf-8"))


class AlturaDeControleUnificadaTests(TestCase):
    """
    Eram 40px em licenças, 40 em preventivas, 38 no quiosque, 38 em
    equipamentos e 36 no botão do base.css. Botão e campo na mesma linha
    saíam desencontrados, e a altura mudava ao trocar de módulo.
    """

    TOKENS = [
        ("ds-licencas.css", "--lic-control-h"),
        ("ds-preventivas.css", "--pv-control-h"),
        ("ds-quiosque.css", "--kq-control-h"),
        ("equipamentos-list.css", "--eq-control-h"),
    ]

    def test_altura_de_cada_modulo_deriva_do_sistema(self):
        for arquivo, token in self.TOKENS:
            css = _texto(_CSS / arquivo)
            m = re.search(rf"^\s*{re.escape(token)}\s*:\s*([^;]+);", css, re.M)
            self.assertIsNotNone(m, f"{token} sumiu de {arquivo}")
            self.assertIn(
                "var(--control-h", m.group(1),
                f"{arquivo}: {token} fixa a altura em {m.group(1).strip()!r}",
            )


class MenuRenderizaTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.client.force_login(
            User.objects.create_superuser("menu_teste", "m@m.com", "x")
        )

    def test_menu_monta_com_icones_e_sem_sobra_de_sprite(self):
        html = self.client.get(reverse("dashboard")).content.decode("utf-8")
        self.assertIn('id="sidebar"', html)
        # Os 4 símbolos acrescentados para aposentar o Font Awesome.
        for simbolo in ("ic-mobile", "ic-shield", "ic-layers", "ic-swatch"):
            self.assertIn(f'id="{simbolo}"', html, f"símbolo {simbolo} não foi emitido")
        self.assertNotRegex(html, r'<i class="fa-[^"]*nav-icon')


class ContrasteNoItemAtivoTests(TestCase):
    """
    Bug introduzido ao trocar o estado ativo de faixa azul CHEIA para fundo
    claro `--brand-soft`: o contador (`.nav-badge`) e a seta (`.nav-arrow`)
    continuaram com `#ffffff`/`rgba(255,255,255,.85)` — valores escritos para
    texto sobre azul saturado. Sobre fundo claro, branco some: o contador
    existe no HTML e não se lê na tela.

    É o risco de toda troca de fundo: o que estava legível por causa do fundo
    antigo some sem erro nenhum.
    """

    def test_nenhum_branco_fixo_nas_regras_do_item_ativo(self):
        css = _texto(_CSS / "base.css")
        suspeitas = []
        for m in re.finditer(
            r"((?:\.nav-(?:item|button)\.is-active[^{]*)\{[^}]*\})", css
        ):
            bloco = m.group(1)
            if re.search(r"#fff\b|#ffffff|255,\s*255,\s*255", bloco, re.I):
                suspeitas.append(bloco.split("{")[0].strip().replace("\n", " "))
        self.assertFalse(
            suspeitas,
            "branco fixo em regra do item ativo — o fundo do ativo é claro "
            f"(--brand-soft), então o branco some: {suspeitas}",
        )

    def test_contador_e_seta_usam_a_cor_da_marca(self):
        css = _texto(_CSS / "base.css")
        for seletor in (".nav-item.is-active .nav-badge",
                        ".nav-item.is-active .nav-arrow"):
            i = css.index(seletor)
            bloco = css[i:css.index("}", i)]
            self.assertIn(
                "var(--brand", bloco,
                f"{seletor} não acompanha a cor da marca",
            )
