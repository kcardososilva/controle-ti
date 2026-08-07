"""
Busca textual por relevância via SQLite FTS5 (WHERE ... MATCH).

Cada domínio pesquisável tem sua própria tabela virtual FTS5:
- usuario_busca_fts — sincronizada por signal Django (post_save/post_delete
  em Usuario, ver ProjetoEstoque/signals.py). Seguro porque o import de
  planilha só usa .save(), nunca bulk_create/bulk_update.
- item_busca_fts — sincronizada por TRIGGER SQL (migration 0149), não por
  signal: o projeto tem scripts de correção em massa que usam
  Item.objects.bulk_update(...) (ver deploy_scripts_routerlink/), que não
  dispara post_save. Cobre só o campo "nome" — numero_serie/modelo continuam
  em icontains, ver nota na migration 0149.
- requisicaoitem_busca_fts / itempadraodatasul_busca_fts — sincronizadas por
  signal (ambos os models só usam .save(), ver migrations 0151/0153). Cobrem
  só "descricao" — "codigo" (Datasul, numérico) continua em icontains pelo
  mesmo motivo de numero_serie em Item: busca por fragmento no meio da
  string, que o FTS5 (prefixo de token) não cobre. As views combinam os dois.

As funções de leitura (buscar_*_ids) nunca levantam exceção — se a tabela
FTS não existir por qualquer motivo, retornam None e a tela cai no fallback
icontains de sempre.
"""
import re

from django.db import connection

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_MAX_TOKENS = 8


def _montar_match_query(termo: str) -> str | None:
    """Converte texto livre do usuário numa query FTS5 segura: cada palavra
    vira uma frase entre aspas (aspas internas escapadas) com prefixo `*`,
    unidas por AND explícito. Aspas literais neutralizam qualquer operador de
    sintaxe FTS5 (AND/OR/NOT/NEAR/coluna:termo) que o usuário tenha digitado."""
    tokens = _TOKEN_RE.findall(termo or "")[:_MAX_TOKENS]
    if not tokens:
        return None
    partes = [f'"{tok.replace(chr(34), chr(34) * 2)}"*' for tok in tokens]
    return " AND ".join(partes)


def buscar_usuario_ids(termo: str) -> list[int] | None:
    """Retorna IDs de Usuario que casam com o termo, ordenados por relevância
    (bm25). Lista vazia = busca válida sem resultado (a chamadora deve
    respeitar isso). `None` = índice FTS indisponível — a chamadora deve
    cair de volta para o filtro icontains."""
    match_query = _montar_match_query(termo)
    if not match_query:
        return []
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT usuario_id FROM usuario_busca_fts "
                "WHERE usuario_busca_fts MATCH %s ORDER BY rank",
                [match_query],
            )
            return [row[0] for row in cursor.fetchall()]
    except Exception:
        return None


def sincronizar_usuario_fts(usuario) -> None:
    """Atualiza (delete+insert) a linha do usuário na tabela FTS."""
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM usuario_busca_fts WHERE usuario_id = %s", [usuario.pk])
        cursor.execute(
            "INSERT INTO usuario_busca_fts (usuario_id, nome, email, matricula) "
            "VALUES (%s, %s, %s, %s)",
            [usuario.pk, usuario.nome or "", usuario.email or "", getattr(usuario, "matricula", "") or ""],
        )


def remover_usuario_fts(usuario_id) -> None:
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM usuario_busca_fts WHERE usuario_id = %s", [usuario_id])


def buscar_item_ids(termo: str) -> list[int] | None:
    """Retorna IDs de Item cujo nome casa com o termo, ordenados por
    relevância (bm25). Sincronizado por trigger SQL — não precisa de função
    de sync aqui (ver migration 0149_item_busca_fts)."""
    match_query = _montar_match_query(termo)
    if not match_query:
        return []
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT item_id FROM item_busca_fts "
                "WHERE item_busca_fts MATCH %s ORDER BY rank",
                [match_query],
            )
            return [row[0] for row in cursor.fetchall()]
    except Exception:
        return None


def buscar_requisicao_item_ids(termo: str) -> list[int] | None:
    """Retorna IDs de RequisicaoItem cuja "descricao" casa com o termo,
    ordenados por relevância (bm25). Não cobre "codigo" — a chamadora deve
    combinar com um filtro icontains(codigo=termo) à parte (ver nota no
    cabeçalho deste módulo)."""
    match_query = _montar_match_query(termo)
    if not match_query:
        return []
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT requisicaoitem_id FROM requisicaoitem_busca_fts "
                "WHERE requisicaoitem_busca_fts MATCH %s ORDER BY rank",
                [match_query],
            )
            return [row[0] for row in cursor.fetchall()]
    except Exception:
        return None


def sincronizar_requisicao_item_fts(item) -> None:
    """Atualiza (delete+insert) a linha do item de requisição na tabela FTS."""
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM requisicaoitem_busca_fts WHERE requisicaoitem_id = %s", [item.pk])
        cursor.execute(
            "INSERT INTO requisicaoitem_busca_fts (requisicaoitem_id, descricao) VALUES (%s, %s)",
            [item.pk, item.descricao or ""],
        )


def remover_requisicao_item_fts(item_id) -> None:
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM requisicaoitem_busca_fts WHERE requisicaoitem_id = %s", [item_id])


def buscar_item_padrao_ids(termo: str) -> list[int] | None:
    """Retorna IDs de ItemPadraoDatasul cuja "descricao" casa com o termo,
    ordenados por relevância (bm25). Não cobre "codigo" — mesma ressalva de
    buscar_requisicao_item_ids."""
    match_query = _montar_match_query(termo)
    if not match_query:
        return []
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT itempadraodatasul_id FROM itempadraodatasul_busca_fts "
                "WHERE itempadraodatasul_busca_fts MATCH %s ORDER BY rank",
                [match_query],
            )
            return [row[0] for row in cursor.fetchall()]
    except Exception:
        return None


def sincronizar_item_padrao_fts(obj) -> None:
    """Atualiza (delete+insert) a linha do item padrão na tabela FTS."""
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM itempadraodatasul_busca_fts WHERE itempadraodatasul_id = %s", [obj.pk])
        cursor.execute(
            "INSERT INTO itempadraodatasul_busca_fts (itempadraodatasul_id, descricao) VALUES (%s, %s)",
            [obj.pk, obj.descricao or ""],
        )


def remover_item_padrao_fts(obj_id) -> None:
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM itempadraodatasul_busca_fts WHERE itempadraodatasul_id = %s", [obj_id])
