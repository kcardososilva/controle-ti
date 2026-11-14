"""
Movimentações e histórico de Frotas e Maquinários — a leitura, por equipamento e da frota inteira.

Nada aqui grava e nenhuma tabela é nova: tudo é lido de `FrotaEvento`, a linha do tempo imutável do cadastro.
Duas leituras sobre a MESMA base, com recortes diferentes (não são duas páginas com dados próprios):

  • MOVIMENTAÇÕES — o que muda o equipamento de LUGAR, de RESPONSÁVEL ou de SITUAÇÃO: as movimentações registradas
    (`FrotaEvento.Tipo.MOVIMENTACAO`, com o motivo), as mudanças de status e as edições de cadastro que alteraram
    fazenda, setor, área, centro de custo ou responsável. Uma edição que só corrigiu o nome NÃO é movimentação.
  • HISTÓRICO — tudo o que aconteceu com o cadastro: cadastro, edição, movimentação, status e validação do inventário.

As conexões do equipamento com o resto (instalação de equipamento tecnológico, vínculo de chip, ocorrência) entram
na linha do tempo de UM equipamento por `frota_service.linha_do_tempo_veiculo`; o histórico de chips da frota inteira
é o de Chips e Conectividade, e o de equipamentos tecnológicos, o do Estoque tecnológico: aqui não se repete.
"""

from __future__ import annotations

from django.db.models import Q

from ProjetoEstoque.models import FrotaEvento, FrotaVeiculo
from services import busca_service as busca
from services import frota_service as frota
from services import m2m_monitoramento_service as m2m

Tipo = FrotaEvento.Tipo

# Que "grupo" de movimentação cada campo de cadastro representa (o rótulo que a tela mostra).
GRUPO_DO_CAMPO = {
    "localidade": "Localização", "responsavel": "Responsável", "setor": "Setor", "area_operacao": "Área de operação",
    "centro_custo": "Centro de custo",
}
GRUPO_DE_SITUACAO = "Situação"
# O filtro "tipo" da tela de Movimentações: chave -> (rótulo, campos do cadastro que o definem).
TIPOS_DE_MOVIMENTACAO = {
    "localizacao": ("Localização", ("localidade",)),
    "responsavel": ("Responsável", ("responsavel",)),
    "setor": ("Setor e centro de custo", ("setor", "area_operacao", "centro_custo")),
    "situacao": ("Situação", ()),
}
# O filtro "tipo" da tela de Histórico.
TIPOS_DE_HISTORICO = tuple(Tipo.choices)


def _q_edicao_que_movimenta() -> Q:
    """Edição de cadastro cujo de/para mexeu em lugar ou responsável (consulta ao JSON do evento)."""
    movel = Q()
    for campo in frota.CAMPOS_DE_MOVIMENTACAO:
        movel |= Q(**{"dados__alteracoes__has_key": campo})
    return Q(tipo=Tipo.EDICAO) & movel


def q_movimentacao() -> Q:
    return Q(tipo__in=(Tipo.MOVIMENTACAO, Tipo.STATUS)) | _q_edicao_que_movimenta()


def descrever(evento: FrotaEvento) -> dict:
    """Um evento como as telas o mostram:

        {"quando", "tipo", "titulo" (o que aconteceu), "grupos": [rótulos], "mudancas": [{"rotulo", "de", "para"}],
         "motivo", "por" (o login), "veiculo": o equipamento, "pk"}

    `mudancas` traz o de/para por RÓTULO; no status, o de/para do próprio status."""
    dados = evento.dados or {}
    grupos: list[str] = []
    mudancas: list[dict] = []
    if evento.tipo in (Tipo.MOVIMENTACAO, Tipo.EDICAO):
        mudancas = [{"rotulo": m["rotulo"], "de": m["de"], "para": m["para"]} for m in frota.de_para_do_evento(dados.get("alteracoes"))]
        grupos = [GRUPO_DO_CAMPO[c] for c in GRUPO_DO_CAMPO if c in (dados.get("alteracoes") or {})]
    elif evento.tipo == Tipo.STATUS:
        rotulos = dict(frota.Status.choices)
        grupos = [GRUPO_DE_SITUACAO]
        mudancas = [{"rotulo": "Situação", "de": rotulos.get(dados.get("de"), dados.get("de") or "—"), "para": rotulos.get(dados.get("para"), dados.get("para") or "—")}]
    elif evento.tipo == Tipo.VALIDACAO:
        grupos = ["Validação"]
    titulo = " · ".join(grupos) if grupos and evento.tipo != Tipo.EDICAO else evento.get_tipo_display()
    if evento.tipo == Tipo.EDICAO and grupos:
        titulo = f"Cadastro editado ({' · '.join(grupos)})"
    return {
        "pk": evento.pk, "quando": evento.em, "tipo": evento.tipo, "titulo": titulo, "grupos": grupos, "mudancas": mudancas,
        "motivo": dados.get("motivo", ""), "por": evento.usuario.get_username() if evento.usuario_id else "",
        "veiculo": evento.veiculo, "resumo": frota.resumo_do_evento(evento),
    }


def movimentacoes_do_veiculo(veiculo: FrotaVeiculo, limite: int = 100) -> list[dict]:
    """As movimentações de UM equipamento, da mais nova para a mais antiga (aba "Movimentações" do detalhe)."""
    eventos = (
        veiculo.eventos.filter(q_movimentacao()).select_related("usuario", "veiculo").order_by("-em", "-pk")[: max(1, limite)]
    )
    return [descrever(e) for e in eventos]


def eventos_da_frota(filtros: dict | None = None, *, so_movimentacoes: bool):
    """Os eventos da frota inteira (QuerySet, com equipamento e usuário carregados), do mais novo ao mais antigo.
    `filtros` (todos opcionais; inválido é ignorado):

        q        código do ERP, código interno ou nome do equipamento (sem acento nem caixa)
        tipo     movimentações: uma chave de `TIPOS_DE_MOVIMENTACAO`;  histórico: um `FrotaEvento.Tipo`
        usuario  o login de quem registrou (parte do nome)
        de, ate  `AAAA-MM-DD`, `ate` inclusive
        natureza `proprio` | `terceiro` (do equipamento)"""
    filtros = filtros or {}
    qs = FrotaEvento.objects.select_related("veiculo", "veiculo__tipo", "veiculo__classe_operacional", "usuario").order_by("-em", "-pk")
    if so_movimentacoes:
        qs = qs.filter(q_movimentacao())
        escolha = TIPOS_DE_MOVIMENTACAO.get(filtros.get("tipo") or "")
        if escolha is not None:
            rotulo, campos = escolha
            if not campos:
                qs = qs.filter(tipo=Tipo.STATUS)
            else:
                condicao = Q()
                for campo in campos:
                    condicao |= Q(**{"dados__alteracoes__has_key": campo})
                qs = qs.filter(Q(tipo__in=(Tipo.MOVIMENTACAO, Tipo.EDICAO)) & condicao)
    elif filtros.get("tipo") in Tipo.values:
        qs = qs.filter(tipo=filtros["tipo"])
    termo = busca.normalizar_busca(filtros.get("q"))
    if termo:
        qs = qs.alias(
            nome_busca=busca.SemAcento("veiculo__nome"),
        ).filter(
            Q(veiculo__codigo__icontains=filtros["q"].strip()) | Q(veiculo__codigo_legado__icontains=filtros["q"].strip())
            | Q(nome_busca__contains=termo)
        )
    if (filtros.get("usuario") or "").strip():
        qs = qs.filter(usuario__username__icontains=filtros["usuario"].strip())
    if filtros.get("natureza") in ("proprio", "terceiro"):
        qs = qs.filter(veiculo__natureza=filtros["natureza"])
    inicio, fim = m2m._intervalo_de_datas(m2m.interpretar_data(filtros.get("de")), m2m.interpretar_data(filtros.get("ate")))
    if inicio:
        qs = qs.filter(em__gte=inicio)
    if fim:
        qs = qs.filter(em__lt=fim)
    return qs
