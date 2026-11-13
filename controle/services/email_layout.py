"""
Layout institucional dos e-mails de chamado e de alerta de vaga.

Os e-mails automáticos do sistema usam o `_base_html` de `email_alertas.py`: um
cartão genérico, pensado para relatório. Os e-mails que a TI recebe para
ATENDER alguém (preparar máquina, criar acesso, liberar sistema) pedem outra
coisa — uma comunicação de serviço, com identidade da empresa, em que dê para
ler de relance O QUE precisa ser feito. Este módulo é essa apresentação.

Decisões que não são óbvias olhando o HTML:

  • TABELAS E ESTILO INLINE. Outlook (motor do Word), Gmail e os apps de celular
    ignoram trechos diferentes de CSS; tabela com estilo inline é o único
    denominador comum. O único bloco `<style>` serve ao celular (`@media`) e,
    onde é ignorado, o e-mail simplesmente fica na largura fixa de 640 px.

  • LOGO POR CID. A logo (branca, linha sobre transparente — por isso o
    cabeçalho é azul-escuro) vai embutida na mensagem (`Content-ID`), não como
    link: clientes bloqueiam imagem remota por padrão e o servidor interno não é
    alcançável de fora. Se o arquivo faltar, o cabeçalho cai para o nome da
    empresa em texto — o e-mail nunca deixa de sair por falta de enfeite.

  • FUNÇÕES PURAS. Nada aqui lê o banco ou envia e-mail: recebe texto, devolve
    `(html, texto)`. Todo valor vindo de formulário é ESCAPADO aqui, e só aqui.

  • UM DOCUMENTO, DUAS SAÍDAS. `Documento` junta as seções uma vez e produz o
    HTML e a versão em texto puro, para as duas nunca divergirem.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.utils.html import escape

LOGO_CID = "logo-santa-colomba"

_FONTE = "'Segoe UI',Roboto,Helvetica,Arial,sans-serif"

# Paleta sóbria: um azul institucional e cinzas. Cor de estado (verde, âmbar,
# vermelho) só aparece em quem muda de estado — selo de situação e aviso.
_AZUL_ESCURO = "#0f2a5c"
_AZUL = "#1e4fa8"
_FILETE = "#2f6fd6"
_TEXTO = "#1f2937"
_SUAVE = "#64748b"
_LINHA = "#e2e8f0"
_LINHA_SUAVE = "#edf1f6"
_FUNDO = "#eef1f6"
_FAIXA = "#f4f7fb"

_TONS = {
    "info": ("#eff6ff", "#bfdbfe", "#1e3a8a"),
    "aviso": ("#fffbeb", "#fde68a", "#92400e"),
    "perigo": ("#fef2f2", "#fecaca", "#991b1b"),
    "ok": ("#f0fdf4", "#bbf7d0", "#166534"),
}

_PILULAS = {
    "ok": ("#dcfce7", "#166534"),
    "aviso": ("#fef3c7", "#92400e"),
    "perigo": ("#fee2e2", "#991b1b"),
    "info": ("#dbeafe", "#1e40af"),
    "roxo": ("#ede9fe", "#5b21b6"),
    "neutro": ("#e5e7eb", "#374151"),
}

_ESTILO_CELULAR = """
body{margin:0;padding:0;}
@media only screen and (max-width:540px){
  .wrap{padding:12px 6px !important;}
  .px{padding-left:18px !important;padding-right:18px !important;}
  .col{display:block !important;width:100% !important;box-sizing:border-box !important;}
  .kv-l{display:block !important;width:100% !important;padding:10px 0 0 0 !important;border-bottom:0 !important;}
  .kv-v{display:block !important;width:100% !important;padding:2px 0 10px 0 !important;}
  .esconde{display:none !important;}
  .btn a{display:block !important;text-align:center !important;}
}
"""


@lru_cache(maxsize=1)
def logo_bytes() -> bytes | None:
    """A logo (PNG) do projeto, lida uma vez. `None` se o arquivo não existir."""
    caminho = Path(settings.BASE_DIR) / "ProjetoEstoque" / "static" / "img" / "logo.png"
    try:
        return caminho.read_bytes()
    except OSError:
        return None


def link_absoluto(caminho: str) -> str:
    """URL completa de uma tela, ou "" quando `SISTEMA_URL` não está configurada.
    Link relativo num e-mail não leva a lugar nenhum, e apontar para `localhost`
    seria pior que não ter link — por isso o vazio é o estado honesto."""
    base = getattr(settings, "SISTEMA_URL", "") or ""
    return f"{base}{caminho}" if base and caminho else ""


@dataclass(frozen=True)
class Pilula:
    """Selo de situação dentro de uma célula (ex.: "Reservado", "Entregue")."""
    texto: str
    tom: str = "neutro"


@dataclass
class _Secao:
    tipo: str
    titulo: str = ""
    dados: object = None
    extra: object = None


@dataclass
class Documento:
    """Um e-mail em construção: título, identificação e seções, na ordem."""
    titulo: str
    etiqueta: str
    subtitulo: str = ""
    preheader: str = ""
    area: str = "Tecnologia da Informação"
    _identificacao: list = field(default_factory=list)
    _secoes: list = field(default_factory=list)
    _rodape_html: str = ""
    _rodape_texto: str = ""

    # ── construção ──────────────────────────────────────────────────────
    def identificacao(self, linhas) -> "Documento":
        self._identificacao = [(r, v) for r, v in linhas if str(v).strip()]
        return self

    def bloco(self, titulo: str, linhas) -> "Documento":
        """Seção rótulo → valor. Valor com `\\n` vira linhas; `None`/vazio some."""
        linhas = [(r, v) for r, v in linhas if v is not None and str(v).strip()]
        if linhas:
            self._secoes.append(_Secao("kv", titulo, linhas))
        return self

    def tabela(self, titulo: str, cabecalhos, linhas) -> "Documento":
        if linhas:
            self._secoes.append(_Secao("tabela", titulo, linhas, cabecalhos))
        return self

    def aviso(self, texto: str, tom: str = "info", titulo: str = "") -> "Documento":
        if texto:
            self._secoes.append(_Secao("aviso", titulo, texto, tom))
        return self

    def botao(self, texto: str, url: str) -> "Documento":
        if url:
            self._secoes.append(_Secao("botao", texto, url))
        return self

    def rodape(self, html: str = "", texto: str = "") -> "Documento":
        self._rodape_html, self._rodape_texto = html, texto
        return self

    # ── HTML ────────────────────────────────────────────────────────────
    def html(self, *, com_logo: bool = True) -> str:
        e = escape
        logo = (
            f'<img src="cid:{LOGO_CID}" width="150" alt="Santa Colomba" '
            'style="display:block;border:0;outline:none;height:auto;width:150px;max-width:150px;">'
            if com_logo else
            f'<span style="font:700 17px {_FONTE};letter-spacing:.14em;color:#ffffff;">SANTA COLOMBA</span>'
        )
        partes = [
            "<!DOCTYPE html>"
            '<html lang="pt-BR"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="x-apple-disable-message-reformatting">'
            f"<title>{e(self.titulo)}</title><style>{_ESTILO_CELULAR}</style></head>"
            f'<body style="margin:0;padding:0;background:{_FUNDO};">'
            f'<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:{_FUNDO};font-size:1px;line-height:1px;">'
            f"{e(self.preheader or self.titulo)}</div>"
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="{_FUNDO}">'
            f'<tr><td align="center" class="wrap" style="padding:28px 12px;">'
            f'<table role="presentation" width="640" cellpadding="0" cellspacing="0" border="0" bgcolor="#ffffff" '
            f'style="width:100%;max-width:640px;background:#ffffff;border:1px solid {_LINHA};">'
        ]

        # Cabeçalho institucional (logo branca → fundo azul-escuro) + filete.
        partes.append(
            f'<tr><td class="px" bgcolor="{_AZUL_ESCURO}" style="padding:20px 32px;background:{_AZUL_ESCURO};">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
            f'<td align="left" valign="middle">{logo}</td>'
            f'<td align="right" valign="middle" class="esconde" style="font:12px {_FONTE};color:#c7d4ee;line-height:1.5;">'
            f"Tecnologia da Informação<br>Controle TI</td>"
            "</tr></table></td></tr>"
            f'<tr><td height="3" bgcolor="{_FILETE}" style="height:3px;line-height:3px;font-size:0;background:{_FILETE};">&nbsp;</td></tr>'
        )

        # Título.
        partes.append(
            '<tr><td class="px" style="padding:28px 32px 6px 32px;">'
            f'<div style="font:700 11px {_FONTE};letter-spacing:.12em;text-transform:uppercase;color:{_AZUL};">{e(self.etiqueta)}</div>'
            f'<h1 style="margin:6px 0 0 0;font:700 23px/1.25 {_FONTE};color:{_TEXTO};">{e(self.titulo)}</h1>'
            + (f'<p style="margin:8px 0 0 0;font:14px/1.5 {_FONTE};color:{_SUAVE};">{e(self.subtitulo)}</p>' if self.subtitulo else "")
            + "</td></tr>"
        )

        # Faixa de identificação (chamado, vaga, abertura, área responsável).
        if self._identificacao:
            celulas = [
                f'<td class="col" width="50%" valign="top" style="padding:10px 14px;">'
                f'<div style="font:700 10px {_FONTE};letter-spacing:.1em;text-transform:uppercase;color:{_SUAVE};">{e(r)}</div>'
                f'<div style="margin-top:2px;font:600 14px/1.4 {_FONTE};color:{_TEXTO};">{e(v)}</div></td>'
                for r, v in self._identificacao
            ]
            # Duas células por linha; a ímpar que sobra é completada com uma vazia.
            linhas_html = ""
            for i in range(0, len(celulas), 2):
                par = celulas[i:i + 2]
                linhas_html += "<tr>" + "".join(par) + (
                    '<td class="col" width="50%"></td>' if len(par) == 1 else ""
                ) + "</tr>"
            partes.append(
                '<tr><td class="px" style="padding:16px 32px 0 32px;">'
                f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
                f'bgcolor="{_FAIXA}" style="background:{_FAIXA};border:1px solid {_LINHA};">{linhas_html}</table></td></tr>'
            )

        for secao in self._secoes:
            partes.append(self._html_secao(secao))

        # Rodapé institucional.
        partes.append(
            f'<tr><td class="px" style="padding:26px 32px 8px 32px;"><div style="border-top:1px solid {_LINHA};height:1px;line-height:1px;font-size:0;">&nbsp;</div></td></tr>'
            f'<tr><td class="px" style="padding:6px 32px 28px 32px;font:12px/1.6 {_FONTE};color:{_SUAVE};">'
            f'<strong style="color:{_TEXTO};">Santa Colomba Agropecuária</strong> · {e(self.area)}<br>'
            "Mensagem enviada pelo sistema <strong>Controle TI</strong>. "
            'Dúvidas: <a href="mailto:ti@santacolomba.com.br" style="color:' + _AZUL + ';text-decoration:none;">ti@santacolomba.com.br</a>.'
            + (f'<div style="margin-top:10px;">{self._rodape_html}</div>' if self._rodape_html else "")
            + "</td></tr>"
        )
        partes.append("</table></td></tr></table></body></html>")
        return "".join(partes)

    def _html_secao(self, s: _Secao) -> str:
        e = escape
        if s.tipo == "kv":
            linhas = "".join(
                "<tr>"
                f'<td class="kv-l" width="36%" valign="top" style="padding:10px 14px 10px 0;border-bottom:1px solid {_LINHA_SUAVE};font:13px/1.45 {_FONTE};color:{_SUAVE};">{e(r)}</td>'
                f'<td class="kv-v" valign="top" style="padding:10px 0;border-bottom:1px solid {_LINHA_SUAVE};font:14px/1.5 {_FONTE};color:{_TEXTO};word-break:break-word;overflow-wrap:anywhere;">{_celula(v)}</td>'
                "</tr>"
                for r, v in s.dados
            )
            return (
                '<tr><td class="px" style="padding:22px 32px 0 32px;">'
                f'<h2 style="margin:0 0 4px 0;font:700 12px {_FONTE};letter-spacing:.09em;text-transform:uppercase;color:{_AZUL};">{e(s.titulo)}</h2>'
                f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
                f'style="border-collapse:collapse;border-top:1px solid {_LINHA};">{linhas}</table></td></tr>'
            )
        if s.tipo == "tabela":
            cab = "".join(
                f'<th align="left" style="padding:8px 10px;background:{_FAIXA};border-bottom:1px solid {_LINHA};'
                f'font:700 11px {_FONTE};letter-spacing:.06em;text-transform:uppercase;color:{_SUAVE};">{e(c)}</th>'
                for c in s.extra
            )
            corpo = "".join(
                "<tr>" + "".join(
                    f'<td valign="top" style="padding:10px;border-bottom:1px solid {_LINHA_SUAVE};font:13px/1.45 {_FONTE};color:{_TEXTO};word-break:break-word;overflow-wrap:anywhere;">{_celula(c)}</td>'
                    for c in linha
                ) + "</tr>"
                for linha in s.dados
            )
            return (
                '<tr><td class="px" style="padding:22px 32px 0 32px;">'
                f'<h2 style="margin:0 0 8px 0;font:700 12px {_FONTE};letter-spacing:.09em;text-transform:uppercase;color:{_AZUL};">{e(s.titulo)}</h2>'
                f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
                f'style="border-collapse:collapse;border:1px solid {_LINHA};"><thead><tr>{cab}</tr></thead><tbody>{corpo}</tbody></table></td></tr>'
            )
        if s.tipo == "aviso":
            fundo, borda, cor = _TONS.get(s.extra, _TONS["info"])
            return (
                '<tr><td class="px" style="padding:18px 32px 0 32px;">'
                f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="{fundo}" '
                f'style="background:{fundo};border:1px solid {borda};"><tr><td style="padding:12px 14px;font:13px/1.55 {_FONTE};color:{cor};">'
                + (f'<strong>{e(s.titulo)}</strong><br>' if s.titulo else "")
                + f"{_celula(s.dados)}</td></tr></table></td></tr>"
            )
        # botão
        return (
            '<tr><td class="px" style="padding:24px 32px 0 32px;">'
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0" class="btn"><tr>'
            f'<td bgcolor="{_AZUL}" style="background:{_AZUL};border-radius:4px;">'
            f'<a href="{e(s.dados)}" style="display:inline-block;padding:12px 24px;font:600 14px {_FONTE};color:#ffffff;text-decoration:none;">{e(s.titulo)}</a>'
            "</td></tr></table></td></tr>"
        )

    # ── texto puro ──────────────────────────────────────────────────────
    def texto(self) -> str:
        linhas = [self.etiqueta.upper(), self.titulo, "=" * min(len(self.titulo), 70)]
        if self.subtitulo:
            linhas.append(self.subtitulo)
        linhas.append("")
        for r, v in self._identificacao:
            linhas.append(f"{r}: {v}")
        if self._identificacao:
            linhas.append("")
        for s in self._secoes:
            if s.tipo == "kv":
                linhas.append(s.titulo.upper())
                for r, v in s.dados:
                    v = _texto_da_celula(v)
                    linhas.append(f"{r}: {v}" if "\n" not in v else f"{r}:\n{v}")
                linhas.append("")
            elif s.tipo == "tabela":
                linhas.append(s.titulo.upper())
                for linha in s.dados:
                    linhas.append("- " + " | ".join(_texto_da_celula(c) for c in linha))
                linhas.append("")
            elif s.tipo == "aviso":
                linhas.append((f"{s.titulo}: " if s.titulo else "") + _texto_da_celula(s.dados))
                linhas.append("")
            else:
                linhas.append(f"{s.titulo}: {s.dados}")
                linhas.append("")
        linhas.append(f"Santa Colomba Agropecuária · {self.area} — Controle TI")
        if self._rodape_texto:
            linhas.append(self._rodape_texto.strip("\n"))
        return "\n".join(linhas).rstrip() + "\n"


def _celula(valor) -> str:
    """Conteúdo de uma célula, já escapado: texto (com `\\n` → `<br>`) ou `Pilula`."""
    if isinstance(valor, Pilula):
        fundo, cor = _PILULAS.get(valor.tom, _PILULAS["neutro"])
        return (
            f'<span style="display:inline-block;padding:2px 9px;border-radius:999px;background:{fundo};'
            f'color:{cor};font:700 11px {_FONTE};">{escape(valor.texto)}</span>'
        )
    return escape(str(valor)).replace("\n", "<br>")


def _texto_da_celula(valor) -> str:
    return valor.texto if isinstance(valor, Pilula) else str(valor)


def lista_com_marcadores(itens, vazio: str = "—") -> str:
    """Lista de textos como linhas `• item` — o formato que `Documento.bloco` quebra em `<br>`."""
    itens = [str(i) for i in (itens or []) if str(i).strip()]
    return "\n".join(f"• {i}" for i in itens) if itens else vazio
