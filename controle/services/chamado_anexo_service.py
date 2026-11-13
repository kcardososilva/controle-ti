"""
Anexos da conversa dos chamados — o que se aceita, onde se guarda, como se entrega.

Anexo é a superfície de ataque mais óbvia de uma caixa de e-mail ligada a uma
tela: o conteúdo vem de fora, é aberto por gente de RH e de TI, e o nome do
arquivo é texto livre. Quatro decisões seguram isso:

  • LISTA DE PERMITIDOS, não de proibidos. Só entra tipo que a conversa tem
    motivo para trocar (documento, planilha, imagem, texto). Executável, script,
    HTML, SVG e pacote compactado ficam de fora — e o que não está na lista é
    recusado mesmo que ninguém tenha pensado nele.
  • O CONTEÚDO tem de bater com a extensão. Um `.pdf` que começa com `MZ` é um
    executável de nome trocado.
  • O NOME ORIGINAL nunca vira caminho. O arquivo é gravado com nome aleatório,
    num disco privado (`ArmazenamentoChamados`, fora de qualquer pasta servida);
    o nome original só existe como texto de exibição e como `filename` do download.
  • A ENTREGA é por view autenticada, sempre como anexo (`attachment`), com o
    tipo vindo da NOSSA tabela — nunca do que o remetente declarou — e
    `nosniff`. O navegador não "abre" o arquivo: baixa.

Anexo recusado numa mensagem RECEBIDA não derruba a mensagem: vira uma linha
`bloqueado` com o motivo, para a conversa dizer que havia um arquivo e por que
ele não está disponível. Na mensagem ENVIADA pela plataforma a recusa devolve
erro para quem anexou, que pode corrigir na hora.
"""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
import uuid
from dataclasses import dataclass

from django.core.files.base import ContentFile

from ProjetoEstoque.models import ChamadoAnexo

LIMITE_ARQUIVO = 10 * 1024 * 1024        # por arquivo
LIMITE_TOTAL = 25 * 1024 * 1024          # por mensagem
MAX_ARQUIVOS = 10                        # por mensagem

# extensão → (tipo servido no download, assinaturas aceitas no início do arquivo).
# `None` nas assinaturas = texto puro, sem cabeçalho a conferir.
_OFFICE_MODERNO = (b"PK\x03\x04",)
_OFFICE_LEGADO = (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",)
TIPOS_PERMITIDOS: dict[str, tuple[str, tuple | None]] = {
    "pdf": ("application/pdf", (b"%PDF",)),
    "png": ("image/png", (b"\x89PNG\r\n\x1a\n",)),
    "jpg": ("image/jpeg", (b"\xff\xd8\xff",)),
    "jpeg": ("image/jpeg", (b"\xff\xd8\xff",)),
    "gif": ("image/gif", (b"GIF87a", b"GIF89a")),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", _OFFICE_MODERNO),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", _OFFICE_MODERNO),
    "pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", _OFFICE_MODERNO),
    "doc": ("application/msword", _OFFICE_LEGADO),
    "xls": ("application/vnd.ms-excel", _OFFICE_LEGADO),
    "ppt": ("application/vnd.ms-powerpoint", _OFFICE_LEGADO),
    "msg": ("application/vnd.ms-outlook", _OFFICE_LEGADO),
    "odt": ("application/vnd.oasis.opendocument.text", _OFFICE_MODERNO),
    "ods": ("application/vnd.oasis.opendocument.spreadsheet", _OFFICE_MODERNO),
    "rtf": ("application/rtf", (b"{\\rtf",)),
    "txt": ("text/plain; charset=utf-8", None),
    "csv": ("text/csv; charset=utf-8", None),
    "eml": ("message/rfc822", None),
}


class AnexoRecusado(Exception):
    """O anexo não pode ser aceito. A mensagem já vem em texto de tela."""


@dataclass
class Arquivo:
    """Um arquivo já conferido, pronto para gravar."""
    nome: str
    conteudo: bytes
    content_type: str


def nome_seguro(nome: str) -> str:
    """Nome para EXIBIR e para o `filename` do download: sem caminho, sem
    caractere de controle, sem aspas/barras, tamanho limitado. Nunca é usado
    como caminho em disco."""
    nome = (nome or "").replace("\\", "/").split("/")[-1]
    nome = unicodedata.normalize("NFC", nome)
    nome = "".join(c for c in nome if c.isprintable() and c not in '<>:"|?*\r\n\t\x00')
    nome = re.sub(r"\s+", " ", nome).strip(" .")
    if len(nome) > 120:
        base, ext = os.path.splitext(nome)
        nome = base[: 120 - len(ext)] + ext
    return nome or "arquivo"


def _extensao(nome: str) -> str:
    return os.path.splitext(nome)[1].lower().lstrip(".")


def avaliar(nome: str, conteudo: bytes) -> tuple[bool, str, str]:
    """`(aceito, motivo, content_type)` para UM arquivo. Não levanta: quem
    chama decide se a recusa derruba a mensagem (envio) ou só marca o anexo
    (recebimento)."""
    nome = nome_seguro(nome)
    ext = _extensao(nome)
    if ext not in TIPOS_PERMITIDOS:
        return False, f"tipo de arquivo não permitido (.{ext or 'sem extensão'})", ""
    if not conteudo:
        return False, "arquivo vazio", ""
    if len(conteudo) > LIMITE_ARQUIVO:
        return False, f"maior que {LIMITE_ARQUIVO // (1024 * 1024)} MB", ""
    content_type, assinaturas = TIPOS_PERMITIDOS[ext]
    if assinaturas is not None and not conteudo.startswith(assinaturas):
        return False, "o conteúdo não corresponde à extensão do arquivo", ""
    if assinaturas is None and b"\x00" in conteudo[:4096]:
        return False, "o conteúdo não corresponde à extensão do arquivo", ""
    return True, "", content_type


def validar_envio(arquivos) -> list[Arquivo]:
    """Confere os arquivos que a pessoa anexou na plataforma. Qualquer problema
    recusa a mensagem inteira (a pessoa corrige e reenvia) — nada é enviado pela
    metade."""
    arquivos = [a for a in (arquivos or []) if a]
    if len(arquivos) > MAX_ARQUIVOS:
        raise AnexoRecusado(f"No máximo {MAX_ARQUIVOS} arquivos por mensagem.")
    itens, total = [], 0
    for a in arquivos:
        nome = nome_seguro(getattr(a, "name", ""))
        if a.size > LIMITE_ARQUIVO:  # antes de ler: não carrega 500 MB na memória
            raise AnexoRecusado(f"“{nome}”: maior que {LIMITE_ARQUIVO // (1024 * 1024)} MB.")
        conteudo = a.read()
        ok, motivo, content_type = avaliar(nome, conteudo)
        if not ok:
            raise AnexoRecusado(f"“{nome}”: {motivo}.")
        total += len(conteudo)
        if total > LIMITE_TOTAL:
            raise AnexoRecusado(f"O total dos anexos passa de {LIMITE_TOTAL // (1024 * 1024)} MB.")
        itens.append(Arquivo(nome, conteudo, content_type))
    return itens


def salvar(mensagem, itens: list[Arquivo], user=None) -> list[ChamadoAnexo]:
    """Grava os arquivos já conferidos no disco privado e cria as linhas."""
    criados = []
    for item in itens:
        anexo = ChamadoAnexo(
            mensagem=mensagem, nome_original=item.nome, content_type=item.content_type,
            tamanho=len(item.conteudo), sha256=hashlib.sha256(item.conteudo).hexdigest(),
            criado_por=user, atualizado_por=user,
        )
        # Nome em disco: aleatório + extensão da lista. O nome do remetente não participa.
        anexo.arquivo.save(f"{uuid.uuid4().hex}.{_extensao(item.nome)}", ContentFile(item.conteudo), save=False)
        anexo.save()
        criados.append(anexo)
    return criados


def registrar_bloqueado(mensagem, nome: str, tamanho: int, motivo: str, user=None) -> ChamadoAnexo:
    """Anexo recebido que não pôde ser aceito: a linha existe, sem arquivo."""
    return ChamadoAnexo.objects.create(
        mensagem=mensagem, nome_original=nome_seguro(nome), tamanho=tamanho,
        bloqueado=True, motivo_bloqueio=motivo[:255], criado_por=user, atualizado_por=user,
    )


def limpar_arquivos(anexos) -> None:
    """Apaga do disco os arquivos de anexos criados numa operação que falhou.
    Melhor esforço: sobrar um arquivo órfão é feio, não é perigoso (não há URL)."""
    for a in anexos:
        try:
            if a.arquivo:
                a.arquivo.delete(save=False)
        except Exception:  # noqa: BLE001
            pass


def ler_para_envio(anexo: ChamadoAnexo) -> tuple[str, bytes, str]:
    """`(nome, conteúdo, tipo)` de um anexo gravado, no formato que o e-mail
    espera. Levanta `AnexoRecusado` se o arquivo sumiu do disco."""
    try:
        with anexo.arquivo.open("rb") as f:
            return anexo.nome_original, f.read(), anexo.content_type or "application/octet-stream"
    except (FileNotFoundError, ValueError, OSError) as exc:
        raise AnexoRecusado(f"O arquivo “{anexo.nome_original}” não está mais disponível no servidor.") from exc
