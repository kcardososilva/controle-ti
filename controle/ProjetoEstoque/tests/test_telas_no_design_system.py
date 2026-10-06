"""
As telas consomem o design system, não uma cópia dele.

O estado antes desta rodada, medido:

    cores HEX literais nas telas .......... 1.861
    font-size escritos à mão .............. 2.374  (~40 tamanhos distintos)
    border-radius escritos à mão .............476

Não era um problema estético. Era que mudar um token do sistema **não
chegava** nessas telas: elas estavam congeladas nos valores do dia em que
foram escritas. É o mesmo defeito do `auth.css` e dos portais (regras 26 e
31), só que espalhado por 167 arquivos.

O que estes testes protegem:

  • COR — `#0071e3`, `#1d1d1f`, `#ff3b30`… soltos. Repare que o mesmo HEX
    como RESERVA de `var(--brand, #0071e3)` é CORRETO e não conta: ali a tela
    já está ligada e o valor só entra se o token faltar.

  • ESCALA — tamanho de texto e raio fora da escala. O teto é um limite
    superior, não uma meta: ele existe para a conta não voltar a subir.
"""
import collections
import re
from pathlib import Path

from django.conf import settings
from django.test import TestCase

_RAIZES = [
    Path(settings.BASE_DIR) / "ProjetoEstoque" / "templates",
    Path(settings.BASE_DIR) / "templates",
    Path(settings.BASE_DIR) / "ProjetoEstoque" / "static" / "css",
]

# A paleta anterior. Encontrar um destes SOLTO numa tela significa que ela
# ficou para trás do design system.
_V4 = {
    "#0071e3", "#0077ed", "#0a84ff", "#2b93ff", "#0078d4", "#106ebe",
    "#2997ff", "#409cff", "#ff3b30", "#d70015", "#34c759", "#30d158",
    "#ff9500", "#ff9f0a", "#5ac8fa", "#1d1d1f", "#424245", "#6e6e73",
    "#86868b", "#a1a1a6",
}

# Exceções deliberadas, cada uma com o motivo. Não acrescente nada aqui sem
# um motivo que caiba numa linha.
_EXCECOES = {
    # Superfície SEMPRE escura, com texto branco por cima. Trocar pelo token
    # a inverteria no modo escuro (o token clareia) e o texto sumiria.
    ("planta-editor.css", "#1d1d1f"),
    ("planta_tv_gerenciar.html", "#1d1d1f"),
    ("avisos_contrato_vencer.html", "#1d1d1f"),
    ("validacao_custos_planilha.html", "#1d1d1f"),
    # Chip CLARO atrás da marca no modo escuro, para a logo escura continuar
    # legível. É o inverso do token, de propósito.
    ("auth.css", "#f5f5f7"),
    # Trilho recolhido do menu: é escuro mesmo no tema claro, então redefine
    # os neutros no próprio escopo (ver regra 32) — são os valores do modo
    # escuro aplicados a um pedaço de tela clara, não deriva de paleta.
    ("base.css", "#f5f5f7"),
    ("base.css", "#a1a1a6"),
    # Cor de série de um diagrama, não de interface.
    ("organograma_usuarios.html", "#1d1d1f"),
}

_RE_DECL = re.compile(
    r"(?P<prop>--[a-z0-9-]+|[a-z-]+)\s*:\s*(?P<valor>[^;{}]*?)(?P<hex>#[0-9a-fA-F]{6})\b"
)
_RE_FS = re.compile(r"font-size:\s*[\d.]+(?:px|rem)\b")
_RE_BR = re.compile(r"border-radius:\s*[\d.]+(?:px|rem)\b")


def _blocos_de_estilo(caminho):
    """O CSS de um arquivo, sem comentários e sem o bloco de impressão."""
    texto = caminho.read_text(encoding="utf-8")
    if caminho.suffix == ".css":
        brutos = [texto]
    else:
        brutos = re.findall(r"<style[^>]*>(.*?)</style>", texto, re.S)
    for bruto in brutos:
        # Papel não tem token nem modo escuro: `@media print` fica de fora.
        sem_print = re.split(r"@media\s+print\s*\{", bruto)[0]
        yield re.sub(r"/\*.*?\*/", "", sem_print, flags=re.S)


def _arquivos():
    for raiz in _RAIZES:
        for caminho in sorted(raiz.rglob("*")):
            if caminho.suffix not in (".html", ".css"):
                continue
            # PDF e impressão são documento gerado: autossuficientes de
            # propósito, não carregam webfont nem tokens.
            if "_pdf" in caminho.name or "_print" in caminho.name:
                continue
            # `estilo.css` é código morto (nenhum template o referencia).
            if caminho.name in ("estilo.css", "tokens.css", "ui-kit.css"):
                continue
            yield caminho


class PaletaDasTelasTests(TestCase):
    def test_nenhuma_cor_da_paleta_anterior_solta(self):
        """
        "Solta" = fora de um `var(--token, #reserva)`. A reserva é o jeito
        certo e não conta: ali a tela já está ligada ao sistema.
        """
        achados = []
        for caminho in _arquivos():
            for bloco in _blocos_de_estilo(caminho):
                for m in _RE_DECL.finditer(bloco):
                    hexa = m.group("hex").lower()
                    if hexa not in _V4:
                        continue
                    # Parêntese aberto e não fechado antes do HEX = reserva
                    # dentro de `var(...)`.
                    valor = m.group("valor")
                    if valor.count("(") > valor.count(")"):
                        continue
                    if (caminho.name, hexa) in _EXCECOES:
                        continue
                    achados.append(f"{caminho.name}: {m.group(0).strip()[:48]}")

        self.assertFalse(
            achados,
            "cor da paleta anterior escrita à mão — a tela não acompanha uma "
            "mudança de token: " + "; ".join(achados[:12]),
        )

    def test_reserva_dentro_de_var_continua_permitida(self):
        """
        Guarda contra um "conserto" que piore: alguém poderia remover as
        reservas achando que são cores soltas. Elas são a rede de segurança
        para quando a folha é servida sem o tokens.css.
        """
        total = 0
        for caminho in _arquivos():
            for bloco in _blocos_de_estilo(caminho):
                total += len(re.findall(r"var\(--[a-z0-9-]+,\s*#[0-9a-fA-F]{3,8}", bloco))
        self.assertGreater(
            total, 300,
            "as reservas de var() sumiram; elas são o que mantém a tela "
            "coerente se o token faltar",
        )


class EscalaDasTelasTests(TestCase):
    """
    Tetos de regressão. Cada número é o estado real depois da unificação —
    se subir, alguém voltou a escrever tamanho à mão.
    """

    TETO_FONT_SIZE = 420
    TETO_RADIUS = 380

    def _contar(self, padrao):
        por_arquivo = collections.Counter()
        for caminho in _arquivos():
            for bloco in _blocos_de_estilo(caminho):
                n = len(padrao.findall(bloco))
                if n:
                    por_arquivo[caminho.name] += n
        return por_arquivo

    def test_tamanho_de_texto_quase_todo_na_escala(self):
        por_arquivo = self._contar(_RE_FS)
        total = sum(por_arquivo.values())
        self.assertLessEqual(
            total, self.TETO_FONT_SIZE,
            f"{total} tamanhos de texto fora da escala (teto {self.TETO_FONT_SIZE}). "
            f"Piores: {por_arquivo.most_common(5)}. Use var(--text-2xs…3xl).",
        )

    def test_raio_quase_todo_na_escala(self):
        por_arquivo = self._contar(_RE_BR)
        total = sum(por_arquivo.values())
        self.assertLessEqual(
            total, self.TETO_RADIUS,
            f"{total} raios fora da escala (teto {self.TETO_RADIUS}). "
            f"Piores: {por_arquivo.most_common(5)}. Use var(--radius-xs…2xl).",
        )

    def test_a_escala_e_de_fato_usada(self):
        """Teto baixo sem adoção também passaria; este mede o outro lado."""
        usos = 0
        for caminho in _arquivos():
            for bloco in _blocos_de_estilo(caminho):
                usos += len(re.findall(
                    r"var\(--text-(?:2xs|xs|sm|base|md|lg|xl|2xl|3xl)\)", bloco
                ))
        self.assertGreater(
            usos, 1800,
            f"só {usos} usos da escala tipográfica — a adoção regrediu",
        )

    def test_pilula_e_token_e_nao_999px(self):
        """`border-radius: 999px` e `99px` são a mesma intenção de
        `--radius-pill`. Eram 210 ocorrências do número."""
        achados = []
        for caminho in _arquivos():
            for bloco in _blocos_de_estilo(caminho):
                if re.search(r"border-radius:\s*(?:999|99)px", bloco):
                    achados.append(caminho.name)
        self.assertFalse(achados, f"pílula escrita como número em: {set(achados)}")


class ShellProprioResolveSeusTokensTests(TestCase):
    """
    Tela AUTÔNOMA (`<!DOCTYPE>` próprio, sem `{% extends %}`) só enxerga o que
    ela mesma carrega. Se usar `var(--text-sm)` sem carregar o `tokens.css` e
    sem definir o token localmente, o navegador cai no valor INICIAL —
    `font-size: medium` (16px), `color: canvastext`, `border-radius: 0` — e a
    tela sai descaracterizada, com HTTP 200 e nenhum erro em lugar nenhum.

    O teste global de token fantasma (`test_design_system.py`) não pega isto:
    lá basta o token existir em ALGUM arquivo do projeto. Aqui a conta é por
    arquivo, que é o escopo real de uma tela autônoma.
    """

    _PREFIXOS_DO_SISTEMA = (
        "--text-", "--radius-", "--space-", "--brand", "--bg-", "--separator-",
        "--shadow-", "--control-h", "--font-", "--tracking-", "--leading-",
        "--success", "--warning", "--danger", "--info", "--chart-", "--ring",
    )

    def test_toda_tela_autonoma_resolve_os_tokens_que_usa(self):
        falhas = []
        for raiz in _RAIZES[:2]:                      # só os templates
            for caminho in sorted(raiz.rglob("*.html")):
                texto = caminho.read_text(encoding="utf-8")
                if "{% extends" in texto[:400] or "<!DOCTYPE" not in texto[:300]:
                    continue
                if "css/tokens.css" in texto:
                    continue                          # carrega o sistema: ok

                definidos = set(re.findall(r"[{;\n]\s*(--[a-z0-9-]+)\s*:", texto))
                usados = set(re.findall(r"var\(\s*(--[a-z0-9-]+)\s*\)", texto))
                orfaos = sorted(
                    t for t in usados - definidos
                    if t.startswith(self._PREFIXOS_DO_SISTEMA)
                )
                if orfaos:
                    falhas.append(f"{caminho.name}: {orfaos[:5]}")

        self.assertFalse(
            falhas,
            "tela autônoma usa token do sistema sem carregar `tokens.css` nem "
            "definir localmente — resolve no valor inicial do navegador: "
            + "; ".join(falhas),
        )
