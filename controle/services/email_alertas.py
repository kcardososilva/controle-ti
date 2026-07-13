"""
Serviço central de alertas por e-mail — Controle TI / Santa Colomba.

Alertas transacionais (disparados automaticamente ao registrar movimentação):
  alerta_movimentacao(mov)        — entrega ou devolução de item a colaborador / baixa
  alerta_baixa_estoque(mov)       — foco em estoque: quantidade restante, custo, fornecedor

Alertas periódicos individuais (uso manual ou fallback):
  alerta_preventivas_proximas()   — preventivas nos próximos N dias
  alerta_estoque_critico()        — itens de consumo com quantidade < 2
  alerta_licencas_desligados()    — licenças ativas de colaboradores desligados

Relatório diário consolidado (principal agendamento automático):
  relatorio_diario(horas=24)      — digest completo: estoque, baixas, movimentações,
                                    licenças, preventivas — UM único e-mail por dia
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db.models import Exists, OuterRef
from django.utils import timezone

logger = logging.getLogger(__name__)

DESTINATARIOS: list[str] = getattr(settings, "ALERTA_EMAILS", [settings.ALERTA_EMAIL])
REMETENTE: str = settings.EMAIL_HOST_USER

# Destinatário das notificações de movimentação de manutenção (fornecedor ↔ TI).
TI_EMAILS: list[str] = getattr(settings, "TI_EMAILS", ["ti@santacolomba.com.br"])

# ─────────────────────────────────────────────────────────────
# Catálogo de notificações — fonte da verdade para o painel
# "Central de Alertas → Configurar Notificações". Cada função de e-mail deste
# módulo que chama `_enviar(..., codigo=...)` deve ter uma entrada aqui.
# Adicionar uma notificação nova ao sistema = adicionar uma linha aqui; o
# painel se atualiza sozinho no próximo acesso (via `sincronizar_catalogo`).
# ─────────────────────────────────────────────────────────────

CATALOGO_NOTIFICACOES: list[dict] = [
    dict(
        codigo="relatorio_diario", nome="Relatório diário consolidado", categoria="Relatórios",
        descricao="Um e-mail por dia com estoque crítico, baixas, movimentações, licenças e preventivas.",
        icone="fa-file-lines", tipo_destinatarios="fixo",
        origem_disparo="Agendado (Task Scheduler) via 'agendar_relatorio' ou manage.py enviar_alertas --tipo diario",
    ),
    dict(
        codigo="preventivas_proximas", nome="Preventivas vencidas / próximas", categoria="Preventivas",
        descricao="Preventivas vencidas ou nos próximos 7 dias.",
        icone="fa-screwdriver-wrench", tipo_destinatarios="fixo",
        origem_disparo="Manual (Central de Alertas) ou manage.py enviar_alertas --tipo preventivas",
    ),
    dict(
        codigo="estoque_critico", nome="Estoque crítico", categoria="Estoque",
        descricao="Itens de consumo abaixo do estoque mínimo (menos de 2 unidades).",
        icone="fa-boxes-stacked", tipo_destinatarios="fixo",
        origem_disparo="Manual (Central de Alertas) ou manage.py enviar_alertas --tipo estoque",
    ),
    dict(
        codigo="licencas_desligados", nome="Licenças de colaboradores desligados", categoria="Licenças",
        descricao="Licenças ainda atribuídas a colaboradores desligados, sem devolução.",
        icone="fa-key", tipo_destinatarios="fixo",
        origem_disparo="Manual (Central de Alertas) ou manage.py enviar_alertas --tipo licencas",
    ),
    dict(
        codigo="movimentacao_transacional", nome="Movimentação de estoque (entrega / devolução)", categoria="Estoque",
        descricao="Confirmação por e-mail a cada entrega, devolução ou transferência registrada.",
        icone="fa-arrow-right-arrow-left", tipo_destinatarios="dinamico",
        destino_gerenciado_em="E-mail do colaborador envolvido, somado à lista padrão do sistema (.env)",
        origem_disparo="Transacional — a cada movimentação registrada (services/movimentacao_service.py)",
    ),
    dict(
        codigo="baixa_estoque", nome="Baixa de estoque", categoria="Estoque",
        descricao="Detalhe de custo, fornecedor e NF a cada baixa de item de consumo.",
        icone="fa-box-open", tipo_destinatarios="fixo",
        origem_disparo="Transacional — a cada baixa registrada (services/movimentacao_service.py)",
    ),
    dict(
        codigo="entrada_estoque", nome="Entrada de estoque", categoria="Estoque",
        descricao="Novo lote recebido: quantidade, custo, fornecedor e NF.",
        icone="fa-box", tipo_destinatarios="fixo",
        origem_disparo="Transacional — a cada entrada registrada (services/movimentacao_service.py)",
    ),
    dict(
        codigo="transferencia_equipamento", nome="Transferência de Equipamento", categoria="Estoque",
        descricao="Equipamento transferido entre localidades/centros de custo (fora do fluxo de entrega a colaborador).",
        icone="fa-truck-arrow-right", tipo_destinatarios="dinamico",
        destino_gerenciado_em="E-mail do colaborador vinculado à transferência (se houver), somado à lista padrão do sistema (.env)",
        origem_disparo="Transacional — a cada transferência de equipamento registrada (services/movimentacao_service.py)",
    ),
    dict(
        codigo="manutencao_movimentacao", nome="Movimentação de manutenção (fornecedor ↔ TI)", categoria="Manutenção",
        descricao="Avisa o time de TI a cada mudança de status de uma ordem de manutenção.",
        icone="fa-truck-ramp-box", tipo_destinatarios="fixo",
        origem_disparo="Transacional — a cada transição de OS (services/ordem_manutencao_service.py)",
    ),
    dict(
        codigo="item_defeito", nome="Equipamento em Defeito (aviso ao fornecedor)", categoria="Portal do Fornecedor",
        descricao="Avisa o(s) login(s) do fornecedor quando um equipamento dele é marcado como Defeito.",
        icone="fa-triangle-exclamation", tipo_destinatarios="dinamico",
        destino_gerenciado_em="Fornecedores → Acesso ao Portal (toggle por login de acesso)",
        origem_disparo="Signal — Item.post_save ao transicionar status para Defeito",
    ),
    dict(
        codigo="prtg_transicoes", nome="Alarme PRTG (offline / instável)", categoria="Monitoramento",
        descricao="Equipamentos que entraram ou saíram de estado de alarme no PRTG.",
        icone="fa-tower-broadcast", tipo_destinatarios="fixo",
        origem_disparo="Agendado — manage.py monitorar_prtg (Task Scheduler)",
    ),
    dict(
        codigo="acesso_suspeito", nome="Acesso suspeito (segurança)", categoria="Segurança",
        descricao="Rajada de falhas de login ou login bem-sucedido após várias tentativas.",
        icone="fa-shield-halved", tipo_destinatarios="fixo",
        origem_disparo="Signal — monitoramento de autenticação (ISO 27001 A.8.16)",
    ),
    dict(
        codigo="documento_fiscal_remessa", nome="Aviso ao Fiscal — Remessa de Equipamento", categoria="Remessa",
        descricao="E-mail ao setor Fiscal com PDF anexo (fornecedor, modelo, nº de série) ao gerar um "
        "documento fiscal de envio ou devolução de equipamento ao fornecedor.",
        icone="fa-file-invoice", tipo_destinatarios="fixo",
        origem_disparo="Manual — botão 'Gerar Documento Fiscal' nas telas de Remessa",
    ),
]


def sincronizar_catalogo_notificacoes():
    """Garante que toda notificação do CATALOGO_NOTIFICACOES tenha uma linha em
    CanalNotificacao — cria as que faltam e realinha os campos descritivos
    (nome/descrição/categoria/ícone/origem) sem tocar no estado por instalação
    (ativo, destinatarios_customizados, contadores). Chamado no painel de
    configuração; idempotente e barato (poucas linhas)."""
    from ProjetoEstoque.models import CanalNotificacao

    existentes = {c.codigo: c for c in CanalNotificacao.objects.all()}
    for meta in CATALOGO_NOTIFICACOES:
        obj = existentes.get(meta["codigo"])
        if obj is None:
            CanalNotificacao.objects.create(**meta)
            continue
        mudou = False
        for campo, valor in meta.items():
            if campo == "codigo":
                continue
            if getattr(obj, campo) != valor:
                setattr(obj, campo, valor)
                mudou = True
        if mudou:
            obj.save()

    codigos = [m["codigo"] for m in CATALOGO_NOTIFICACOES]
    return CanalNotificacao.objects.filter(codigo__in=codigos)


def _destinatarios_padrao(codigo: str) -> list[str]:
    """Lista padrão (sem override) usada por cada canal — mesma fonte que
    `_enviar()` usaria na ausência de `destinatarios_customizados`. Central
    para o painel poder mostrar o destinatário REAL, sem duplicar a regra."""
    if codigo == "manutencao_movimentacao":
        return list(TI_EMAILS)
    return list(DESTINATARIOS)


# Canais "dinâmicos" cuja parte dinâmica é só um e-mail ADICIONADO por cima de
# uma lista-base (o colaborador/usuário do evento) — a lista-base em si é tão
# editável (adicionar/remover por e-mail) quanto a de um canal 'fixo'. Only
# `item_defeito` é dinâmico "de verdade" (destinatários = PerfilFornecedor,
# sem lista-base nenhuma) e por isso fica de fora desta lista.
_CANAIS_BASE_EDITAVEL_DINAMICOS = {"movimentacao_transacional", "transferencia_equipamento"}


def _base_efetiva(codigo: str) -> list[str]:
    """Lista-base efetiva de um canal (customizada, se ativa; senão o padrão do
    sistema) — usada tanto para MONTAR o e-mail (`alerta_movimentacao`) quanto
    para EXIBIR no painel (`resolver_destinatarios_atuais`), para as duas nunca
    divergirem. Em canais com parte dinâmica (movimentacao_transacional,
    transferencia_equipamento), esta é só a base: o e-mail do evento (ex.:
    colaborador) é sempre adicionado por cima, nunca substituído por ela."""
    from ProjetoEstoque.models import CanalNotificacao
    canal = CanalNotificacao.objects.filter(codigo=codigo).first()
    if canal is not None and canal.destinatarios_customizados_ativo:
        return canal.destinatarios_lista()
    return _destinatarios_padrao(codigo)


def resolver_destinatarios_atuais(canal) -> tuple[list[dict], str]:
    """Resolve, em tempo real, QUEM recebe um canal hoje — para o painel exibir
    pessoas de verdade em vez de um campo de texto que pode estar 'vazio mas na
    verdade usando o padrão do .env', ou um link vago para 'outra tela'.

    Retorna (pessoas, nota):
      pessoas: [{"nome": str, "email": str, "contexto": str|None, "removivel": bool,
                 "remove_kind": "email"|"perfil"|None, "remove_id": str|int|None}, ...]
      nota:    explicação complementar (origem da lista / o que varia por evento)

    `remove_kind`/`remove_id` dizem ao template QUAL ação de desvincular usar por
    pessoa: "email" (remover da lista-base, identificado pelo próprio e-mail) ou
    "perfil" (desativar a notificação daquele PerfilFornecedor, identificado por pk).
    """
    from ProjetoEstoque.models import CanalNotificacao

    eh_fixo = canal.tipo_destinatarios == CanalNotificacao.TipoDestinatarios.FIXO
    eh_dinamico_com_base = canal.codigo in _CANAIS_BASE_EDITAVEL_DINAMICOS

    if eh_fixo or eh_dinamico_com_base:
        if canal.destinatarios_customizados_ativo:
            emails = canal.destinatarios_lista()
            nota = (
                "Lista customizada — substitui o padrão do sistema."
                if emails else
                "Lista customizada esvaziada — nenhum destinatário (este canal não enviará)."
            )
        else:
            emails = _destinatarios_padrao(canal.codigo)
            nota = "Padrão do sistema (definido no .env)."
        if eh_dinamico_com_base:
            nota += " + o e-mail do colaborador/responsável envolvido em cada evento (adicionado automaticamente, não listado aqui)."
        return (
            [{"nome": e, "email": e, "contexto": None, "removivel": True, "remove_kind": "email", "remove_id": e} for e in emails],
            nota,
        )

    if canal.codigo == "item_defeito":
        from ProjetoEstoque.models import PerfilFornecedor
        perfis = (
            PerfilFornecedor.objects
            .filter(ativo=True, notificar_defeito_email=True)
            .exclude(usuario__email="")
            .select_related("usuario", "fornecedor")
            .order_by("fornecedor__nome", "usuario__username")
        )
        pessoas = [
            {
                "nome": p.usuario.get_full_name() or p.usuario.username,
                "email": p.usuario.email,
                "contexto": p.fornecedor.nome if p.fornecedor else None,
                "removivel": True, "remove_kind": "perfil", "remove_id": p.pk,
            }
            for p in perfis
        ]
        return pessoas, "Logins do Portal do Fornecedor com a notificação de Defeito ativada (Fornecedores → Acesso ao Portal)."

    return [], "Destinatários resolvidos individualmente a cada evento."


_TIPO_LABELS = {
    "transferencia": "Transferência",
    "transferencia_equipamento": "Transferência de Equipamento",
    "entrada": "Entrada de Estoque",
    "baixa": "Baixa / Consumo",
    "envio_manutencao": "Envio para Manutenção",
    "retorno_manutencao": "Retorno de Manutenção",
    "retorno": "Retorno",
}


# ─────────────────────────────────────────────────────────────
# Utilitários internos
# ─────────────────────────────────────────────────────────────

def _alertas_habilitados() -> bool:
    try:
        from ProjetoEstoque.models import ConfiguracaoSistema
        return ConfiguracaoSistema.get().alertas_email_ativos
    except Exception:
        return True


def _enviar(
    assunto: str, texto: str, html: str,
    destinatarios: list[str] | None = None, *, codigo: str = "",
    anexos: list[tuple[str, bytes, str]] | None = None,
) -> bool:
    """Envia o e-mail. `codigo` identifica o canal em CanalNotificacao (opcional,
    mas todo alerta novo deveria ter um — ver CATALOGO_NOTIFICACOES): se o canal
    estiver desativado pelo TI, suprime o envio; se for do tipo 'fixo' e tiver
    `destinatarios_customizados_ativo=True`, a lista customizada substitui
    `destinatarios` por completo — mesmo vazia (ninguém removido é reintroduzido
    silenciosamente pelo padrão do .env). `anexos`: lista opcional de
    (nome_arquivo, conteudo, mimetype) anexados ao e-mail."""
    if not _alertas_habilitados():
        logger.info("email_alertas: envio suprimido — alertas desativados nas configurações do sistema.")
        return False

    canal = None
    if codigo:
        from ProjetoEstoque.models import CanalNotificacao
        canal = CanalNotificacao.objects.filter(codigo=codigo).first()
        if canal is not None and not canal.ativo:
            logger.info("email_alertas: envio suprimido — canal '%s' desativado pelo TI.", codigo)
            return False

    alvos = destinatarios or DESTINATARIOS
    if (
        canal is not None
        and canal.tipo_destinatarios == canal.TipoDestinatarios.FIXO
        and canal.destinatarios_customizados_ativo
    ):
        alvos = canal.destinatarios_lista()

    if not alvos:
        logger.warning("email_alertas: nenhum destinatário configurado.")
        return False
    try:
        msg = EmailMultiAlternatives(
            subject=assunto,
            body=texto,
            from_email=REMETENTE,
            to=alvos,
        )
        msg.attach_alternative(html, "text/html")
        for nome_arquivo, conteudo, mimetype in (anexos or []):
            msg.attach(nome_arquivo, conteudo, mimetype)
        msg.send(fail_silently=False)
        logger.info("email_alertas: '%s' enviado para %s", assunto, alvos)
        if canal is not None:
            from django.db.models import F
            type(canal).objects.filter(pk=canal.pk).update(
                ultimo_envio=timezone.now(), total_envios=F("total_envios") + 1,
            )
        return True
    except Exception as exc:
        logger.error("email_alertas: falha ao enviar '%s': %s", assunto, exc)
        return False


def _fmt_brl(valor) -> str:
    """Formata número como moeda BRL: R$ 1.234,56"""
    if valor is None:
        return "—"
    try:
        return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return str(valor)


def _base_html(titulo: str, subtitulo: str, corpo: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="pt-br">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;padding:32px 16px;">
    <tr><td align="center">
      <table width="640" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08);">

        <tr>
          <td style="background:linear-gradient(135deg,#1e3a8a,#2563eb);padding:28px 32px;">
            <p style="margin:0;color:rgba(255,255,255,.72);font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;">Santa Colomba · Controle TI</p>
            <h1 style="margin:6px 0 0;color:#ffffff;font-size:22px;font-weight:800;line-height:1.2;">{titulo}</h1>
            <p style="margin:6px 0 0;color:rgba(255,255,255,.78);font-size:13px;">{subtitulo}</p>
          </td>
        </tr>

        <tr>
          <td style="padding:28px 32px;">
            {corpo}
          </td>
        </tr>

        <tr>
          <td style="background:#f8fafc;padding:16px 32px;border-top:1px solid #e2e8f0;">
            <p style="margin:0;color:#94a3b8;font-size:11px;text-align:center;">
              Alerta automático · Controle TI · Santa Colomba Agropecuária<br>
              Não responda este e-mail · Suporte: <a href="mailto:ti@santacolomba.com.br" style="color:#3b82f6;text-decoration:none;">ti@santacolomba.com.br</a>
            </p>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _tabela_html(cabecalhos: list[str], linhas: list[list[str]], max_linhas: int = 50) -> str:
    ths = "".join(
        f'<th style="padding:8px 10px;text-align:left;background:#f1f5f9;color:#475569;'
        f'font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;'
        f'border-bottom:2px solid #e2e8f0;white-space:nowrap;">{h}</th>'
        for h in cabecalhos
    )
    exibir = linhas[:max_linhas]
    trs = ""
    for i, linha in enumerate(exibir):
        bg = "#ffffff" if i % 2 == 0 else "#f8fafc"
        tds = "".join(
            f'<td style="padding:9px 10px;color:#334155;font-size:12px;border-bottom:1px solid #f1f5f9;'
            f'vertical-align:middle;">{c}</td>'
            for c in linha
        )
        trs += f'<tr style="background:{bg};">{tds}</tr>'
    rodape = ""
    if len(linhas) > max_linhas:
        rodape = (
            f'<tr><td colspan="{len(cabecalhos)}" style="padding:10px;text-align:center;'
            f'color:#94a3b8;font-size:11px;font-style:italic;">'
            f'... e mais {len(linhas) - max_linhas} registro(s). Acesse o sistema para ver todos.</td></tr>'
        )
    return (
        f'<table width="100%" cellpadding="0" cellspacing="0" '
        f'style="border-collapse:collapse;border:1px solid #e2e8f0;border-radius:8px;'
        f'overflow:hidden;margin-top:10px;">'
        f'<thead><tr>{ths}</tr></thead>'
        f'<tbody>{trs}{rodape}</tbody>'
        f'</table>'
    )


def _secao(titulo: str, icone: str, cor: str, conteudo: str) -> str:
    return (
        f'<div style="margin-bottom:24px;">'
        f'<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:8px;">'
        f'<tr>'
        f'<td style="font-size:16px;width:24px;">{icone}</td>'
        f'<td><h2 style="margin:0;color:{cor};font-size:14px;font-weight:800;">{titulo}</h2></td>'
        f'</tr></table>'
        f'{conteudo}'
        f'</div>'
    )


def _badge(texto: str, cor_bg: str, cor_texto: str) -> str:
    return (
        f'<span style="display:inline-block;padding:2px 8px;border-radius:999px;'
        f'background:{cor_bg};color:{cor_texto};font-size:11px;font-weight:700;'
        f'white-space:nowrap;">{texto}</span>'
    )


def _linha_vazia(texto: str = "Nenhum registro encontrado.") -> str:
    return (
        f'<p style="margin:6px 0 0;color:#94a3b8;font-size:13px;font-style:italic;">{texto}</p>'
    )


# ─────────────────────────────────────────────────────────────
# Movimentação de manutenção (fornecedor ↔ TI) — transacional
# ─────────────────────────────────────────────────────────────

def alerta_movimentacao_manutencao(ordem_pk: int, novo_status: str, ator: str = "", observacao: str = "") -> bool:
    """Avisa o time de TI (TI_EMAILS) a cada movimentação de uma ordem de
    manutenção entre fornecedor e TI (Portal do Fornecedor). Respeita o
    toggle global de e-mails (ConfiguracaoSistema.alertas_email_ativos) via
    `_enviar`.

    O corpo é montado de forma incremental: valores monetários e observações
    só aparecem quando o fluxo já os produziu até este ponto (ex.: valor do
    conserto só existe depois do reparo) — evita linhas vazias/redundantes e
    mantém o e-mail relevante para quem está lendo naquele momento."""
    from ProjetoEstoque.models import OrdemManutencao, StatusOrdemManutencaoChoices

    ordem = (
        OrdemManutencao.objects
        .select_related("item", "fornecedor", "item_substituto")
        .filter(pk=ordem_pk)
        .first()
    )
    if not ordem:
        return False

    novo_status = str(novo_status)
    status_label = dict(StatusOrdemManutencaoChoices.choices).get(novo_status, novo_status)
    item_nome = ordem.item.nome if ordem.item else "Equipamento"
    serie = ordem.item.numero_serie if (ordem.item and ordem.item.numero_serie) else "—"
    forn = ordem.fornecedor.nome if ordem.fornecedor else "—"
    origem = "Fornecedor" if ator == "fornecedor" else ("TI" if ator == "ti" else "Sistema")
    tipo_os = "Troca antecipada" if ordem.troca_antecipada else "Manutenção"
    quando = timezone.localtime(ordem.updated_at if hasattr(ordem, "updated_at") else timezone.now()).strftime("%d/%m/%Y %H:%M")
    observacao = (observacao or "").strip()
    eh_reprovacao = novo_status == str(StatusOrdemManutencaoChoices.REPROVADO)
    eh_aprovacao = novo_status == str(StatusOrdemManutencaoChoices.APROVADO)

    linhas = [
        ["Ordem", f"OS #{ordem.pk} · {tipo_os}"],
        ["Equipamento", f"{item_nome} · Série {serie}"],
        ["Fornecedor", forn],
        ["Novo status", status_label],
        ["Ação por", origem],
        ["Data/hora", quando],
    ]
    if ordem.chamado:
        linhas.append(["Chamado", ordem.chamado])

    corpo = _secao(
        "Movimentação de manutenção",
        "🔧", "#1e3a8a",
        _tabela_html(["Campo", "Detalhe"], linhas),
    )

    # ── Valores envolvidos — só os campos que o fluxo já preencheu até aqui.
    valores = []
    if ordem.valor_orcamento is not None:
        rotulo = "Orçamento reprovado" if eh_reprovacao else "Orçamento aprovado" if eh_aprovacao else "Orçamento proposto"
        valores.append([rotulo, _fmt_brl(ordem.valor_orcamento)])
    if ordem.valor_conserto is not None:
        valores.append(["Valor do conserto", _fmt_brl(ordem.valor_conserto)])
    if ordem.valor_total is not None and ordem.valor_total != ordem.valor_conserto:
        valores.append(["Valor total (conserto + extras)", _fmt_brl(ordem.valor_total)])
    if ordem.valor_avaliacao_tecnica is not None:
        valores.append(["Avaliação técnica", _fmt_brl(ordem.valor_avaliacao_tecnica)])
    if ordem.substituto_valor is not None:
        rotulo_sub = "Valor da substituição"
        if ordem.substituto_contrato:
            rotulo_sub += f" ({ordem.substituto_contrato})"
        valores.append([rotulo_sub, _fmt_brl(ordem.substituto_valor)])
    if ordem.reparo_valor is not None:
        valores.append(["Valor considerado no retorno", _fmt_brl(ordem.reparo_valor)])

    if valores:
        corpo += _secao("Valores envolvidos", "💰", "#166534", _tabela_html(["Item", "Valor"], valores))

    # ── Observações — a nota desta atualização ganha destaque no tom do
    # status (reprovação em vermelho, demais em azul neutro); o diagnóstico
    # técnico só aparece quando traz informação nova (evita repetir o texto).
    obs_bloco = ""
    if observacao:
        cor = "#b91c1c" if eh_reprovacao else "#1e40af"
        bg = "#fef2f2" if eh_reprovacao else "#eff6ff"
        rotulo_obs = "Motivo da reprovação" if eh_reprovacao else "Observação registrada"
        obs_bloco += (
            f'<div style="padding:12px 14px;border-radius:8px;background:{bg};border-left:3px solid {cor};">'
            f'<p style="margin:0 0 3px;color:{cor};font-size:11px;font-weight:800;'
            f'text-transform:uppercase;letter-spacing:.04em;">{rotulo_obs}</p>'
            f'<p style="margin:0;color:#334155;font-size:13px;line-height:1.5;">{observacao}</p>'
            f'</div>'
        )
    diagnostico = (ordem.diagnostico or "").strip()
    if diagnostico and diagnostico != observacao:
        if obs_bloco:
            obs_bloco += '<div style="height:8px;"></div>'
        obs_bloco += (
            f'<div style="padding:12px 14px;border-radius:8px;background:#f8fafc;border-left:3px solid #94a3b8;">'
            f'<p style="margin:0 0 3px;color:#475569;font-size:11px;font-weight:800;'
            f'text-transform:uppercase;letter-spacing:.04em;">Diagnóstico do fornecedor</p>'
            f'<p style="margin:0;color:#334155;font-size:13px;line-height:1.5;">{diagnostico}</p>'
            f'</div>'
        )
    if obs_bloco:
        corpo += _secao("Observações", "📝", "#475569", obs_bloco)

    corpo += (
        '<p style="margin:18px 0 0;color:#64748b;font-size:12px;">'
        'Acesse o sistema em <b>Manutenção → Recebimentos</b> para acompanhar a ordem.'
        '</p>'
    )
    html = _base_html(
        f"OS #{ordem.pk} — {status_label}",
        f"{tipo_os} · {item_nome} · {forn}",
        corpo,
    )

    texto_linhas = [
        f"Movimentação de manutenção — OS #{ordem.pk} ({tipo_os})",
        f"Equipamento: {item_nome} (Série {serie})",
        f"Fornecedor: {forn}",
        f"Novo status: {status_label}",
        f"Ação por: {origem}",
        f"Data/hora: {quando}",
    ]
    if valores:
        texto_linhas.append("")
        texto_linhas.append("Valores envolvidos:")
        texto_linhas.extend(f"  {rotulo}: {valor}" for rotulo, valor in valores)
    if observacao:
        texto_linhas.append("")
        texto_linhas.append(f"{'Motivo da reprovação' if eh_reprovacao else 'Observação'}: {observacao}")
    if diagnostico and diagnostico != observacao:
        texto_linhas.append(f"Diagnóstico do fornecedor: {diagnostico}")
    texto = "\n".join(texto_linhas) + "\n"

    assunto = f"[Manutenção] OS #{ordem.pk} — {status_label}"
    return _enviar(assunto, texto, html, destinatarios=TI_EMAILS, codigo="manutencao_movimentacao")


# ─────────────────────────────────────────────────────────────
# Item em Defeito — aviso ao fornecedor (destinatários configuráveis)
# ─────────────────────────────────────────────────────────────

def alerta_item_defeito(item_pk: int) -> bool:
    """Avisa o fornecedor responsável quando um equipamento dele é marcado como
    Defeito. Destinatários: logins do Portal (`PerfilFornecedor`) do fornecedor
    do item, ativos, com `notificar_defeito_email=True` — configurável pelo TI
    em Fornecedores → Acesso ao Portal. Sem destinatário configurado, não envia
    (não é erro). Respeita o toggle global via `_enviar`."""
    from ProjetoEstoque.models import Item, PerfilFornecedor

    item = (
        Item.objects
        .select_related("fornecedor", "localidade", "subtipo")
        .filter(pk=item_pk)
        .first()
    )
    if not item or not item.fornecedor_id:
        return False

    emails = list(
        PerfilFornecedor.objects
        .filter(fornecedor_id=item.fornecedor_id, ativo=True, notificar_defeito_email=True)
        .exclude(usuario__email="")
        .values_list("usuario__email", flat=True)
        .distinct()
    )
    if not emails:
        return False

    serie = item.numero_serie or "—"
    marca_modelo = " ".join(p for p in [item.marca, item.modelo] if p) or "—"
    local = item.localidade.local if item.localidade else "—"
    quando = timezone.localtime(item.updated_at).strftime("%d/%m/%Y %H:%M")

    linhas = [
        ["Equipamento", item.nome],
        ["Nº de série", serie],
        ["Marca / Modelo", marca_modelo],
        ["Localidade", local],
        ["Status", "Defeito"],
        ["Data/hora", quando],
    ]
    corpo = _secao(
        "Equipamento em Defeito",
        "⚠️", "#b91c1c",
        _tabela_html(["Campo", "Detalhe"], linhas),
    )
    corpo += (
        '<p style="margin:18px 0 0;color:#64748b;font-size:12px;">'
        'Acesse o Portal do Fornecedor para iniciar a manutenção ou solicitar uma troca antecipada.'
        '</p>'
    )
    html = _base_html(
        f"{item.nome} — Defeito",
        f"{item.fornecedor.nome} · Série {serie}",
        corpo,
    )
    texto = (
        f"Equipamento em Defeito — {item.nome}\n"
        f"Nº de série: {serie}\n"
        f"Marca/Modelo: {marca_modelo}\n"
        f"Localidade: {local}\n"
        f"Data/hora: {quando}\n"
        f"Acesse o Portal do Fornecedor para iniciar a manutenção ou solicitar uma troca antecipada.\n"
    )
    assunto = f"[Defeito] {item.nome} — ação necessária"
    return _enviar(assunto, texto, html, destinatarios=emails, codigo="item_defeito")


# ─────────────────────────────────────────────────────────────
# Documento Fiscal de Remessa — aviso ao Fiscal (destinatários configuráveis)
# ─────────────────────────────────────────────────────────────

def alerta_documento_fiscal_remessa(documento, pdf_bytes: bytes) -> tuple[bool, list[str]]:
    """Avisa o setor Fiscal (e-mail configurável em `/alertas/notificacoes/`, canal
    'documento_fiscal_remessa') que equipamento(s) estão indo ao fornecedor
    (Envio) ou voltando (Devolução), com o Documento Fiscal de Remessa (PDF, um
    aviso interno de controle — não é a NF-e real do fornecedor) anexado.
    Retorna (enviado, destinatarios) — os destinatários são resolvidos ANTES do
    envio (via `_base_efetiva`, mesma fonte usada pelo painel) para que o
    chamador possa registrar o snapshot mesmo se o canal estiver desativado."""
    from ProjetoEstoque.models import TipoSeparacaoChoices
    from services.documento_fiscal_service import DocumentoFiscalService

    itens = list(
        documento.itens
        .select_related("item", "item__subtipo", "item__subtipo__categoria", "fornecedor")
        .order_by("item__nome")
    )
    destinatarios = _base_efetiva("documento_fiscal_remessa")
    if not itens:
        return False, destinatarios

    eh_envio = documento.tipo == TipoSeparacaoChoices.ENVIO
    tipo_label = "Envio ao Fornecedor" if eh_envio else "Devolução ao Fornecedor"
    acao = "enviado(s) ao" if eh_envio else "devolvido(s) ao"

    itens_locados, itens_proprios = DocumentoFiscalService.separar_itens(itens)

    def _subtipo(i):
        return str(i.item.subtipo) if i.item.subtipo_id else "—"

    blocos_html = ""
    blocos_texto = []
    if itens_locados:
        linhas = [
            [i.fornecedor.nome, _subtipo(i), i.item.modelo or "—", i.item.numero_serie or "—"]
            for i in itens_locados
        ]
        blocos_html += _secao(
            "Itens Locados", "🔑", "#1e3a8a",
            _tabela_html(["Fornecedor", "Subtipo", "Modelo", "Nº de Série"], linhas),
        )
        blocos_texto.append("Itens Locados:")
        blocos_texto.extend(
            f"  - {i.fornecedor.nome} | Subtipo: {_subtipo(i)} | Modelo: {i.item.modelo or '—'} | "
            f"Nº Série: {i.item.numero_serie or '—'}"
            for i in itens_locados
        )
    if itens_proprios:
        linhas = [
            [i.fornecedor.nome, _subtipo(i), i.item.modelo or "—", i.item.numero_serie or "—", i.valor_fmt]
            for i in itens_proprios
        ]
        blocos_html += _secao(
            "Itens Próprios", "📦", "#1e3a8a",
            _tabela_html(["Fornecedor", "Subtipo", "Modelo", "Nº de Série", "Valor do Equipamento"], linhas),
        )
        if blocos_texto:
            blocos_texto.append("")
        blocos_texto.append("Itens Próprios:")
        blocos_texto.extend(
            f"  - {i.fornecedor.nome} | Subtipo: {_subtipo(i)} | Modelo: {i.item.modelo or '—'} | "
            f"Nº Série: {i.item.numero_serie or '—'} | Valor: {i.valor_fmt}"
            for i in itens_proprios
        )

    intro = (
        f'<p style="margin:0 0 10px;color:#334155;font-size:13px;">'
        f'O(s) equipamento(s) abaixo está(ão) sendo {acao} fornecedor. '
        f'Documento {documento.numero} anexo (PDF) para os registros do setor Fiscal.</p>'
    )
    corpo = _secao(f"Aviso Fiscal — {tipo_label}", "🧾", "#1e3a8a", intro) + blocos_html
    html = _base_html(
        f"Documento Fiscal {documento.numero}",
        f"{tipo_label} · {len(itens)} equipamento(s)",
        corpo,
    )
    texto = (
        f"Aviso Fiscal — {tipo_label} — Documento {documento.numero}\n\n"
        + "\n".join(blocos_texto)
    )
    assunto = f"[Fiscal] {tipo_label} — Documento {documento.numero}"
    anexos = [(f"documento_fiscal_{documento.numero}.pdf", pdf_bytes, "application/pdf")]

    enviado = _enviar(
        assunto, texto, html, destinatarios=destinatarios,
        codigo="documento_fiscal_remessa", anexos=anexos,
    )
    return enviado, destinatarios


def _linha_ok(texto: str) -> str:
    return (
        f'<p style="margin:6px 0 0;color:#059669;font-size:13px;">✅ {texto}</p>'
    )


# ─────────────────────────────────────────────────────────────
# Alerta 1 — Preventivas próximas
# ─────────────────────────────────────────────────────────────

def _proxima_efetiva(preventiva, hoje):
    """
    Data efetiva da próxima preventiva — MESMA regra das telas de preventivas/
    equipamentos: intervalo (Item.data_limite_preventiva → CheckListModelo.intervalo_dias)
    a partir da última execução; se não houver, usa o campo data_proxima.
    O campo data_proxima sozinho pode estar desatualizado, por isso não é usado direto.
    Retorna date | None.
    """
    intervalo = 0
    try:
        intervalo = int(getattr(preventiva.equipamento, "data_limite_preventiva", 0) or 0)
    except (TypeError, ValueError):
        intervalo = 0
    if intervalo <= 0 and preventiva.checklist_modelo:
        try:
            intervalo = int(preventiva.checklist_modelo.intervalo_dias or 0)
        except (TypeError, ValueError):
            intervalo = 0
    if intervalo > 0 and preventiva.data_ultima:
        return preventiva.data_ultima + timedelta(days=intervalo)
    return preventiva.data_proxima


def preventivas_relevantes(dias: int = 7):
    """
    Separa as preventivas em (vencidas, proximas) usando a data EFETIVA
    (data_ultima + intervalo) — a MESMA regra das telas de preventivas/
    equipamentos. Cada preventiva recebe, em memória:
      · ``data_proxima`` (data efetiva)
      · ``dias_rest``    (dias até o vencimento; negativo = em atraso)
      · ``atraso``       (apenas nas vencidas: dias de atraso, positivo)
    Retorna ``(vencidas, proximas)``:
      · vencidas → dias_rest < 0
      · proximas → 0 <= dias_rest <= dias
    """
    from ProjetoEstoque.models import Preventiva

    hoje = timezone.localdate()
    vencidas, proximas = [], []
    for p in (
        Preventiva.objects
        .filter(pausada=False)
        .select_related("equipamento", "equipamento__localidade", "checklist_modelo")
    ):
        proxima = _proxima_efetiva(p, hoje)
        if not proxima:
            continue
        p.data_proxima = proxima
        p.dias_rest = (proxima - hoje).days
        if p.dias_rest < 0:
            p.atraso = -p.dias_rest
            vencidas.append(p)
        elif p.dias_rest <= dias:
            proximas.append(p)
    vencidas.sort(key=lambda p: (p.data_proxima, p.equipamento.nome))
    proximas.sort(key=lambda p: (p.data_proxima, p.equipamento.nome))
    return vencidas, proximas


def alerta_preventivas_proximas(dias: int = 7) -> bool:
    hoje = timezone.localdate()
    vencidas, proximas = preventivas_relevantes(dias)

    if not vencidas and not proximas:
        logger.info("alerta_preventivas: nenhuma preventiva vencida ou nos próximos %d dias.", dias)
        return False

    blocos = []

    if vencidas:
        linhas_v = []
        for p in vencidas:
            linhas_v.append([
                p.equipamento.nome,
                p.equipamento.numero_serie or "—",
                p.equipamento.localidade.local if p.equipamento.localidade else "—",
                p.checklist_modelo.nome if p.checklist_modelo else "—",
                p.data_proxima.strftime("%d/%m/%Y"),
                _badge(f"{p.atraso}d em atraso", "#fee2e2", "#b91c1c"),
            ])
        blocos.append(
            f'<p style="margin:4px 0 4px;color:#991b1b;font-size:13px;font-weight:700;">'
            f'Vencidas ({len(vencidas)})</p>'
            + _tabela_html(
                ["Equipamento", "Nº Série", "Localidade", "Checklist", "Data prevista", "Prazo"],
                linhas_v,
            )
        )

    if proximas:
        linhas_p = []
        for p in proximas:
            dias_rest = p.dias_rest
            if dias_rest == 0:
                badge = _badge("VENCE HOJE", "#fee2e2", "#b91c1c")
            elif dias_rest <= 2:
                badge = _badge(f"{dias_rest}d", "#fef3c7", "#b45309")
            else:
                badge = _badge(f"{dias_rest}d", "#dbeafe", "#1d4ed8")
            linhas_p.append([
                p.equipamento.nome,
                p.equipamento.numero_serie or "—",
                p.equipamento.localidade.local if p.equipamento.localidade else "—",
                p.checklist_modelo.nome if p.checklist_modelo else "—",
                p.data_proxima.strftime("%d/%m/%Y"),
                badge,
            ])
        blocos.append(
            f'<p style="margin:{"16px" if vencidas else "4px"} 0 4px;color:#1d4ed8;'
            f'font-size:13px;font-weight:700;">Próximas {dias} dias ({len(proximas)})</p>'
            + _tabela_html(
                ["Equipamento", "Nº Série", "Localidade", "Checklist", "Próxima", "Prazo"],
                linhas_p,
            )
        )

    n_venc = len(vencidas)
    n_prox = len(proximas)
    total = n_venc + n_prox

    intro = (
        '<p style="margin:0 0 4px;color:#334155;font-size:14px;">'
        + (f'<strong style="color:#b91c1c;">{n_venc} vencida(s)</strong>' if n_venc else "")
        + (" · " if n_venc and n_prox else "")
        + (f'<strong>{n_prox}</strong> nos próximos <strong>{dias} dias</strong>' if n_prox else "")
        + ".</p>"
    )

    corpo = _secao(f"Preventivas — vencidas e próximos {dias} dias", "🛠️", "#1d4ed8", intro + "".join(blocos))
    html = _base_html(
        f"Preventivas: {total} requer(em) atenção" + (f" · {n_venc} vencida(s)" if n_venc else ""),
        f"Alerta gerado em {hoje.strftime('%d/%m/%Y')}",
        corpo,
    )
    texto = (
        f"ALERTA — Preventivas ({n_venc} vencida(s), {n_prox} nos próximos {dias} dias)\n\n"
        + "\n".join(
            f"- [{'VENCIDA' if p.dias_rest < 0 else 'PRÓXIMA'}] {p.equipamento.nome} | "
            f"{p.equipamento.numero_serie or '—'} | {p.data_proxima.strftime('%d/%m/%Y')}"
            for p in (vencidas + proximas)
        )
    )

    return _enviar(
        f"[Controle TI] Preventivas: {n_venc} vencida(s), {n_prox} próxima(s)",
        texto, html, codigo="preventivas_proximas",
    )


# ─────────────────────────────────────────────────────────────
# Alerta 2 — Estoque crítico
# ─────────────────────────────────────────────────────────────

def itens_estoque_critico(limite_qtd: int = 2):
    """
    Itens de consumo abaixo do mínimo, com o estoque EFETIVO calculado a partir
    dos LOTES quando o item é controlado por lote (soma de ``quantidade_disponivel``
    dos vínculos Item×Lote); caso contrário usa o campo ``Item.quantidade``.

    Cada item recebe, em memória:
      · ``estoque_efetivo``  (int) — saldo real considerado no alerta
      · ``controla_lote``    (bool)
      · ``lotes_count``      (int) — nº de lotes vinculados
      · ``lotes_disponivel`` (int) — soma disponível nos lotes
      · ``lotes_info``       (list) — [{nf, fornecedor, disponivel, entrada}]
    """
    from ProjetoEstoque.models import Item, SimNaoChoices

    qs = (
        Item.objects
        .filter(item_consumo=SimNaoChoices.SIM)
        .select_related("localidade", "centro_custo", "subtipo")
        .prefetch_related("vinculos_lote__lote__fornecedor")
        .order_by("nome")
    )
    criticos = []
    for item in qs:
        vinculos = list(item.vinculos_lote.all())
        controla_lote = bool(getattr(item, "tem_lote", False)) or bool(vinculos)
        lotes_disponivel = sum((v.quantidade_disponivel or 0) for v in vinculos)
        estoque = lotes_disponivel if controla_lote else (item.quantidade or 0)
        if estoque >= limite_qtd:
            continue
        item.estoque_efetivo = estoque
        item.controla_lote = controla_lote
        item.lotes_count = len(vinculos)
        item.lotes_disponivel = lotes_disponivel
        item.lotes_info = [
            {
                "nf": (v.lote.numero_nf if v.lote else None) or "—",
                "fornecedor": (v.lote.fornecedor.nome if v.lote and v.lote.fornecedor else "—"),
                "disponivel": v.quantidade_disponivel or 0,
                "entrada": v.quantidade_entrada or 0,
            }
            for v in vinculos
        ]
        criticos.append(item)
    criticos.sort(key=lambda i: (i.estoque_efetivo, (i.nome or "").lower()))
    return criticos


def _lotes_celula(item) -> str:
    """Célula HTML com o detalhamento dos lotes de um item (para os e-mails)."""
    if not getattr(item, "controla_lote", False):
        return '<span style="color:#94a3b8;font-size:12px;">Controle simples</span>'
    info = getattr(item, "lotes_info", None)
    if not info:
        return _badge("Sem lote cadastrado", "#fef3c7", "#b45309")
    partes = []
    for l in info:
        cor = "#b91c1c" if l["disponivel"] == 0 else "#334155"
        partes.append(
            f'<div style="font-size:11px;color:{cor};line-height:1.55;white-space:nowrap;">'
            f'<strong>NF {l["nf"]}</strong> · {l["fornecedor"]} — {l["disponivel"]}/{l["entrada"]} un.</div>'
        )
    return "".join(partes)


def alerta_estoque_critico(limite_qtd: int = 2) -> bool:
    itens = itens_estoque_critico(limite_qtd)

    if not itens:
        logger.info("alerta_estoque: nenhum item com estoque crítico.")
        return False

    linhas = []
    for item in itens:
        qtd = item.estoque_efetivo
        badge = _badge("SEM ESTOQUE", "#fee2e2", "#b91c1c") if qtd == 0 else _badge(f"{qtd} un.", "#fef3c7", "#b45309")
        linhas.append([
            item.nome,
            item.subtipo.nome if item.subtipo else "—",
            item.localidade.local if item.localidade else "—",
            item.centro_custo.departamento if item.centro_custo else "—",
            _lotes_celula(item),
            badge,
        ])

    tabela = _tabela_html(
        ["Item", "Subtipo", "Localidade", "Centro de Custo", "Lotes (disp./entrada)", "Estoque"],
        linhas,
    )
    sem_estoque = sum(1 for i in itens if i.estoque_efetivo == 0)
    com_lote = sum(1 for i in itens if i.controla_lote)
    total = len(linhas)
    hoje = date.today()

    intro = (
        f'<p style="margin:0 0 4px;color:#334155;font-size:14px;">'
        f'<strong>{total}</strong> item(ns) abaixo do estoque mínimo'
        + (f' — <strong style="color:#b91c1c;">{sem_estoque} completamente sem estoque</strong>' if sem_estoque else "")
        + (f' · {com_lote} controlado(s) por lote' if com_lote else "")
        + ".</p>"
    )

    corpo = _secao("Estoque crítico — itens de consumo", "📦", "#b45309", intro + tabela)
    html = _base_html(
        f"Estoque crítico: {total} item(ns) abaixo do mínimo",
        f"Alerta gerado em {hoje.strftime('%d/%m/%Y')}",
        corpo,
    )
    texto = (
        f"ALERTA — Itens com estoque crítico (< {limite_qtd} unidades)\nTotal: {total}\n\n"
        + "\n".join(
            f"- {i.nome} | Estoque: {i.estoque_efetivo} un."
            + (f" (por lote: {i.lotes_disponivel} disp. em {i.lotes_count} lote(s))" if i.controla_lote else "")
            for i in itens
        )
    )

    return _enviar(f"[Controle TI] {total} item(ns) com estoque crítico", texto, html, codigo="estoque_critico")


# ─────────────────────────────────────────────────────────────
# Alerta 3 — Licenças de colaboradores desligados
# ─────────────────────────────────────────────────────────────

def alerta_licencas_desligados() -> bool:
    from ProjetoEstoque.models import MovimentacaoLicenca, StatusUsuarioChoices

    mov_devolucao_posterior = MovimentacaoLicenca.objects.filter(
        usuario=OuterRef("usuario_id"),
        lote=OuterRef("lote_id"),
        tipo="devolucao",
        created_at__gt=OuterRef("created_at"),
    )

    qs = (
        MovimentacaoLicenca.objects
        .filter(tipo="atribuicao", usuario__status=StatusUsuarioChoices.DESLIGADO)
        .annotate(foi_devolvida=Exists(mov_devolucao_posterior))
        .filter(foi_devolvida=False)
        .select_related("usuario", "lote", "lote__licenca", "usuario__funcao", "usuario__centro_custo")
        .order_by("usuario__nome", "lote__licenca__nome")
    )

    if not qs.exists():
        logger.info("alerta_licencas: nenhuma licença pendente de usuário desligado.")
        return False

    linhas = []
    for mov in qs:
        data_term = mov.usuario.data_termino
        licenca_nome = mov.lote.licenca.nome if mov.lote and mov.lote.licenca else "—"
        linhas.append([
            mov.usuario.nome or mov.usuario.email or "—",
            mov.usuario.funcao.nome if mov.usuario.funcao else "—",
            mov.usuario.centro_custo.departamento if mov.usuario.centro_custo else "—",
            licenca_nome,
            data_term.strftime("%d/%m/%Y") if data_term else "—",
            _badge("PENDENTE", "#fee2e2", "#b91c1c"),
        ])

    tabela = _tabela_html(
        ["Colaborador", "Função", "Centro de Custo", "Licença", "Desligamento", "Status"],
        linhas,
    )
    total = len(linhas)
    hoje = date.today()

    intro = (
        f'<p style="margin:0 0 4px;color:#334155;font-size:14px;">'
        f'<strong style="color:#b91c1c;">{total}</strong> licença(s) ainda atribuída(s) a colaboradores desligados. '
        f'Acesse o sistema para realizar a devolução.</p>'
    )

    corpo = _secao("Licenças pendentes — colaboradores desligados", "🔑", "#b91c1c", intro + tabela)
    html = _base_html(
        f"Atenção: {total} licença(s) de colaboradores desligados",
        f"Alerta gerado em {hoje.strftime('%d/%m/%Y')}",
        corpo,
    )
    texto = (
        f"ALERTA — Licenças de colaboradores desligados sem devolução\nTotal: {total}\n\n"
        + "\n".join(
            f"- {m.usuario.nome or m.usuario.email or '—'} | {m.lote.licenca.nome if m.lote and m.lote.licenca else '—'}"
            for m in qs
        )
    )

    return _enviar(
        f"[Controle TI] {total} licença(s) pendente(s) de colaboradores desligados",
        texto, html, codigo="licencas_desligados",
    )


# ─────────────────────────────────────────────────────────────
# Alerta 4 — Movimentação transacional (entrega / devolução / baixa)
# ─────────────────────────────────────────────────────────────

def alerta_movimentacao(mov) -> bool:
    """
    Envia e-mail de notificação imediata para qualquer movimentação registrada.
    Suporta: entrega, devolução, baixa e transferência de equipamento.
    """
    item = mov.item
    usuario = mov.usuario
    registrado_por = getattr(mov, "criado_por", None)
    tipo = mov.tipo_movimentacao
    tipo_transferencia = getattr(mov, "tipo_transferencia", None)

    eh_baixa = tipo == "baixa"
    eh_devolucao = tipo_transferencia == "devolucao"
    eh_transf_equip = tipo == "transferencia_equipamento"
    tipo_label = _TIPO_LABELS.get(tipo, tipo)

    if eh_baixa:
        titulo_banner, subtipo_label = "Baixa / Consumo", ""
        banner_bg, banner_border = "#fef2f2", "#ef4444"
        banner_title_color, banner_sub_color = "#991b1b", "#dc2626"
    elif eh_devolucao:
        titulo_banner, subtipo_label = "Transferência", " — Devolução"
        banner_bg, banner_border = "#fffbeb", "#f59e0b"
        banner_title_color, banner_sub_color = "#92400e", "#b45309"
    elif eh_transf_equip:
        titulo_banner, subtipo_label = "Transferência de Equipamento", ""
        banner_bg, banner_border = "#eef2ff", "#6366f1"
        banner_title_color, banner_sub_color = "#4338ca", "#4f46e5"
    else:
        titulo_banner, subtipo_label = "Transferência", " — Entrega ao colaborador"
        banner_bg, banner_border = "#eff6ff", "#3b82f6"
        banner_title_color, banner_sub_color = "#1e40af", "#3b82f6"

    # codigo do canal — definido cedo pois a lista-base de destinatários
    # (`_base_efetiva`) já depende dele, antes de montar o e-mail.
    if eh_baixa:
        codigo = "baixa_estoque"
    elif eh_transf_equip:
        codigo = "transferencia_equipamento"
    else:
        codigo = "movimentacao_transacional"

    localidade_dest = mov.localidade_destino.local if mov.localidade_destino else "—"
    cc_dest = mov.centro_custo_destino.departamento if mov.centro_custo_destino else "—"
    localidade_orig = mov.localidade_origem.local if mov.localidade_origem else "—"
    cc_orig = mov.centro_custo_origem.departamento if mov.centro_custo_origem else "—"
    registrado_nome = (
        registrado_por.get_full_name() or registrado_por.username
        if registrado_por else "Sistema"
    )
    data_mov = (
        timezone.localtime(mov.created_at).strftime("%d/%m/%Y às %H:%M")
        if mov.created_at else "—"
    )
    obs = mov.observacao or "—"

    if usuario and not eh_baixa:
        if eh_devolucao:
            secao_colaborador_titulo = "Colaborador (devolução)"
        elif eh_transf_equip:
            secao_colaborador_titulo = "Colaborador Vinculado"
        else:
            secao_colaborador_titulo = "Colaborador Destino"
        colaborador_nome = usuario.nome or usuario.email or "—"
        colaborador_funcao = usuario.funcao.nome if usuario.funcao else "—"
        colaborador_cc = usuario.centro_custo.departamento if usuario.centro_custo else "—"
        colaborador_email = usuario.email
        bloco_colaborador = (
            f'<tr><td colspan="2" style="padding:16px 0 4px;color:#64748b;font-size:11px;font-weight:700;'
            f'text-transform:uppercase;letter-spacing:.06em;border-bottom:1px solid #e2e8f0;">{secao_colaborador_titulo}</td></tr>'
            f'<tr><td style="padding:10px 0;width:140px;color:#64748b;font-size:13px;font-weight:600;">Nome</td>'
            f'<td style="padding:10px 0;color:#0f172a;font-size:13px;font-weight:800;">{colaborador_nome}</td></tr>'
            f'<tr><td style="padding:6px 0;color:#64748b;font-size:13px;font-weight:600;">Função</td>'
            f'<td style="padding:6px 0;color:#334155;font-size:13px;">{colaborador_funcao}</td></tr>'
            f'<tr><td style="padding:6px 0;color:#64748b;font-size:13px;font-weight:600;">Centro de Custo</td>'
            f'<td style="padding:6px 0;color:#334155;font-size:13px;">{colaborador_cc}</td></tr>'
        )
        linha_texto_colaborador = f"Colaborador: {colaborador_nome} | {colaborador_funcao} | {colaborador_cc}\n"
    else:
        colaborador_email = None
        bloco_colaborador = ""
        linha_texto_colaborador = ""

    corpo_html = f"""
<div style="margin-bottom:20px;padding:14px 16px;background:{banner_bg};border-left:4px solid {banner_border};border-radius:0 8px 8px 0;">
  <p style="margin:0;color:{banner_title_color};font-size:13px;font-weight:700;">{titulo_banner}{subtipo_label}</p>
  <p style="margin:4px 0 0;color:{banner_sub_color};font-size:12px;">{data_mov} · Registrado por {registrado_nome}</p>
</div>

<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin-bottom:20px;">
  <tr><td colspan="2" style="padding:8px 0 4px;color:#64748b;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;border-bottom:1px solid #e2e8f0;">Item</td></tr>
  <tr><td style="padding:10px 0;width:140px;color:#64748b;font-size:13px;font-weight:600;vertical-align:top;">Nome</td>
      <td style="padding:10px 0;color:#0f172a;font-size:13px;font-weight:800;">{item.nome}</td></tr>
  <tr><td style="padding:6px 0;color:#64748b;font-size:13px;font-weight:600;">Nº Série</td>
      <td style="padding:6px 0;color:#334155;font-size:13px;">{item.numero_serie or '—'}</td></tr>
  <tr><td style="padding:6px 0;color:#64748b;font-size:13px;font-weight:600;">Modelo</td>
      <td style="padding:6px 0;color:#334155;font-size:13px;">{item.modelo or '—'}</td></tr>
  <tr><td style="padding:6px 0;color:#64748b;font-size:13px;font-weight:600;">Marca</td>
      <td style="padding:6px 0;color:#334155;font-size:13px;">{item.marca or '—'}</td></tr>
  {bloco_colaborador}
  <tr><td colspan="2" style="padding:16px 0 4px;color:#64748b;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;border-bottom:1px solid #e2e8f0;">Movimentação</td></tr>
  <tr><td style="padding:10px 0;color:#64748b;font-size:13px;font-weight:600;">Origem</td>
      <td style="padding:10px 0;color:#334155;font-size:13px;">{localidade_orig} · {cc_orig}</td></tr>
  <tr><td style="padding:6px 0;color:#64748b;font-size:13px;font-weight:600;">Destino</td>
      <td style="padding:6px 0;color:#334155;font-size:13px;">{localidade_dest} · {cc_dest}</td></tr>
  <tr><td style="padding:6px 0;color:#64748b;font-size:13px;font-weight:600;">Observação</td>
      <td style="padding:6px 0;color:#334155;font-size:13px;">{obs}</td></tr>
</table>"""

    html = _base_html(
        f"{tipo_label} — {item.nome}",
        f"{titulo_banner}{subtipo_label} · {data_mov}",
        corpo_html,
    )
    texto = (
        f"MOVIMENTAÇÃO REGISTRADA\n"
        f"Tipo: {tipo_label}{subtipo_label}\nData: {data_mov}\n"
        f"Item: {item.nome} | NS: {item.numero_serie or '—'}\n"
        + linha_texto_colaborador
        + f"Origem: {localidade_orig} · {cc_orig}\n"
        f"Destino: {localidade_dest} · {cc_dest}\n"
        f"Registrado por: {registrado_nome}\nObservação: {obs}"
    )

    # Lista-base respeita customização do painel mesmo neste canal "dinâmico" —
    # o e-mail do colaborador/evento é sempre ADICIONADO por cima, nunca a
    # substitui (é por isso que o painel consegue oferecer adicionar/remover
    # pessoas aqui sem quebrar o e-mail dirigido ao colaborador da movimentação).
    destinatarios = _base_efetiva(codigo)
    if colaborador_email and colaborador_email not in destinatarios:
        destinatarios.append(colaborador_email)

    if eh_baixa:
        assunto = f"[Controle TI] Baixa registrada: {item.nome} — {mov.quantidade or 0} un."
    elif eh_devolucao:
        assunto = f"[Controle TI] Devolução: {item.nome} — {usuario.nome if usuario else '—'}"
    elif eh_transf_equip:
        assunto = f"[Controle TI] Transferência de equipamento: {item.nome}"
    else:
        assunto = f"[Controle TI] Entrega: {item.nome} — {usuario.nome if usuario else '—'}"

    return _enviar(assunto, texto, html, destinatarios=destinatarios, codigo=codigo)


# ─────────────────────────────────────────────────────────────
# Alerta — Entrada de estoque (novo lote / recebimento)
# ─────────────────────────────────────────────────────────────

def alerta_entrada_estoque(mov) -> bool:
    """Avisa quando uma nova ENTRADA de estoque (recebimento de lote) é
    registrada. Canal 'entrada_estoque' (lista fixa, configurável no
    gerenciador de notificações — mesmo padrão de estoque_critico/baixa_estoque)."""
    item = mov.item
    lote = mov.lote
    registrado_por = getattr(mov, "criado_por", None)

    quantidade = mov.quantidade or 0
    custo_unitario = lote.custo_unitario if lote else None
    fornecedor = lote.fornecedor.nome if lote and lote.fornecedor else "—"
    nf = lote.numero_nf if lote else "—"

    localidade_dest = mov.localidade_destino.local if mov.localidade_destino else "—"
    cc_dest = mov.centro_custo_destino.departamento if mov.centro_custo_destino else "—"
    registrado_nome = (
        registrado_por.get_full_name() or registrado_por.username
        if registrado_por else "Sistema"
    )
    data_mov = (
        timezone.localtime(mov.created_at).strftime("%d/%m/%Y às %H:%M")
        if mov.created_at else "—"
    )
    obs = mov.observacao or "—"

    linhas = [
        ["Item", item.nome],
        ["Quantidade recebida", f"+ {quantidade} un."],
        ["Custo unitário", _fmt_brl(custo_unitario)],
        ["Custo total", _fmt_brl(mov.custo)],
        ["Fornecedor", fornecedor],
        ["Nº NF / Pedido", nf],
        ["Localidade", f"{localidade_dest} · {cc_dest}"],
        ["Registrado por", registrado_nome],
        ["Data/hora", data_mov],
        ["Observação", obs],
    ]
    corpo = _secao("Nova entrada de estoque", "📥", "#15803d", _tabela_html(["Campo", "Detalhe"], linhas))
    html = _base_html(
        f"Entrada de estoque — {item.nome}",
        f"+ {quantidade} un. · {fornecedor} · {data_mov}",
        corpo,
    )
    texto = (
        f"ENTRADA DE ESTOQUE\nItem: {item.nome}\nQuantidade: + {quantidade} un.\n"
        f"Custo unitário: {_fmt_brl(custo_unitario)}\nCusto total: {_fmt_brl(mov.custo)}\n"
        f"Fornecedor: {fornecedor}\nNF: {nf}\nLocalidade: {localidade_dest} · {cc_dest}\n"
        f"Registrado por: {registrado_nome}\nData: {data_mov}\nObservação: {obs}"
    )

    return _enviar(
        f"[Controle TI] Entrada de estoque: {item.nome} — {quantidade} un.",
        texto, html, codigo="entrada_estoque",
    )


# ─────────────────────────────────────────────────────────────
# Alerta 5 — Baixa de estoque (foco em inventário)
# ─────────────────────────────────────────────────────────────

def alerta_baixa_estoque(mov, *, qtd_restante: int | None = None) -> bool:
    """E-mail focado em estoque: quantidade restante, custo, fornecedor, NF."""
    item = mov.item
    registrado_por = getattr(mov, "criado_por", None)

    quantidade = mov.quantidade or 0
    custo_unitario = mov.lote.custo_unitario if mov.lote and mov.lote.custo_unitario else None
    custo_total = mov.custo
    lote_nf = mov.lote.numero_nf if mov.lote else None
    lote_fornecedor = (
        mov.lote.fornecedor.nome
        if mov.lote and mov.lote.fornecedor else "—"
    )
    qtd_restante_str = str(qtd_restante) if qtd_restante is not None else "—"
    estoque_critico = qtd_restante is not None and qtd_restante < 2

    localidade_orig = mov.localidade_origem.local if mov.localidade_origem else "—"
    cc_orig = mov.centro_custo_origem.departamento if mov.centro_custo_origem else "—"
    registrado_nome = (
        registrado_por.get_full_name() or registrado_por.username
        if registrado_por else "Sistema"
    )
    data_mov = (
        timezone.localtime(mov.created_at).strftime("%d/%m/%Y às %H:%M")
        if mov.created_at else "—"
    )
    obs = mov.observacao or "—"

    alerta_critico_html = ""
    if estoque_critico:
        alerta_critico_html = (
            f'<tr><td colspan="2" style="padding:10px 12px;background:#fef2f2;'
            f'border-radius:6px;color:#991b1b;font-size:12px;font-weight:700;'
            f'border:1px solid #fecaca;">⚠️ Estoque abaixo do mínimo: {qtd_restante_str} un. restante(s). '
            f'Verifique a necessidade de reposição.</td></tr>'
        )

    linha_cu = (
        f'<tr><td style="padding:6px 0;color:#64748b;font-size:13px;font-weight:600;">Custo unitário</td>'
        f'<td style="padding:6px 0;color:#334155;font-size:13px;">{_fmt_brl(custo_unitario)}</td></tr>'
        if custo_unitario else ""
    )
    linha_ct = (
        f'<tr><td style="padding:6px 0;color:#64748b;font-size:13px;font-weight:600;">Custo total baixado</td>'
        f'<td style="padding:6px 0;color:#334155;font-size:13px;font-weight:800;">{_fmt_brl(custo_total)}</td></tr>'
        if custo_total else ""
    )
    linha_nf = (
        f'<tr><td style="padding:6px 0;color:#64748b;font-size:13px;font-weight:600;">Nº NF / Pedido</td>'
        f'<td style="padding:6px 0;color:#334155;font-size:13px;">{lote_nf}</td></tr>'
        if lote_nf else ""
    )

    corpo_html = f"""
<div style="margin-bottom:20px;padding:14px 16px;background:#fef2f2;border-left:4px solid #ef4444;border-radius:0 8px 8px 0;">
  <p style="margin:0;color:#991b1b;font-size:13px;font-weight:700;">Baixa / Consumo de Estoque</p>
  <p style="margin:4px 0 0;color:#dc2626;font-size:12px;">{data_mov} · Registrado por {registrado_nome}</p>
</div>

<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin-bottom:20px;">
  <tr><td colspan="2" style="padding:8px 0 4px;color:#64748b;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;border-bottom:1px solid #e2e8f0;">Item</td></tr>
  <tr><td style="padding:10px 0;width:140px;color:#64748b;font-size:13px;font-weight:600;vertical-align:top;">Nome</td>
      <td style="padding:10px 0;color:#0f172a;font-size:13px;font-weight:800;">{item.nome}</td></tr>
  <tr><td style="padding:6px 0;color:#64748b;font-size:13px;font-weight:600;">Subtipo</td>
      <td style="padding:6px 0;color:#334155;font-size:13px;">{item.subtipo.nome if item.subtipo else '—'}</td></tr>
  <tr><td style="padding:6px 0;color:#64748b;font-size:13px;font-weight:600;">Localidade</td>
      <td style="padding:6px 0;color:#334155;font-size:13px;">{localidade_orig} · {cc_orig}</td></tr>

  <tr><td colspan="2" style="padding:16px 0 4px;color:#64748b;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;border-bottom:1px solid #e2e8f0;">Detalhes da Baixa</td></tr>
  <tr><td style="padding:10px 0;color:#64748b;font-size:13px;font-weight:600;">Quantidade baixada</td>
      <td style="padding:10px 0;color:#991b1b;font-size:16px;font-weight:800;">&#8722; {quantidade} un.</td></tr>
  <tr><td style="padding:6px 0;color:#64748b;font-size:13px;font-weight:600;">Estoque restante</td>
      <td style="padding:6px 0;color:#0f172a;font-size:14px;font-weight:800;">{qtd_restante_str} un.</td></tr>
  {linha_cu}
  {linha_ct}
  <tr><td style="padding:6px 0;color:#64748b;font-size:13px;font-weight:600;">Fornecedor (lote)</td>
      <td style="padding:6px 0;color:#334155;font-size:13px;">{lote_fornecedor}</td></tr>
  {linha_nf}
  <tr><td style="padding:6px 0;color:#64748b;font-size:13px;font-weight:600;">Observação</td>
      <td style="padding:6px 0;color:#334155;font-size:13px;">{obs}</td></tr>
  {alerta_critico_html}
</table>"""

    html = _base_html(
        f"Baixa de estoque — {item.nome}",
        f"Baixa / Consumo · {data_mov}",
        corpo_html,
    )
    texto = (
        f"BAIXA DE ESTOQUE\nItem: {item.nome}\n"
        f"Quantidade baixada: {quantidade} un.\nEstoque restante: {qtd_restante_str} un.\n"
        + (f"Custo unitário: {_fmt_brl(custo_unitario)}\n" if custo_unitario else "")
        + (f"Custo total: {_fmt_brl(custo_total)}\n" if custo_total else "")
        + f"Fornecedor (lote): {lote_fornecedor}\n"
        + (f"Nº NF: {lote_nf}\n" if lote_nf else "")
        + f"Localidade: {localidade_orig} · {cc_orig}\n"
        f"Data: {data_mov}\nRegistrado por: {registrado_nome}\nObservação: {obs}"
        + ("\n\n⚠️ ATENÇÃO: estoque abaixo do mínimo!" if estoque_critico else "")
    )

    return _enviar(
        f"[Controle TI] Baixa de estoque: {item.nome} — {quantidade} un.",
        texto, html, codigo="baixa_estoque",
    )


# ─────────────────────────────────────────────────────────────
# Alerta 6 — Alarme PRTG (equipamento offline / instável em tempo real)
# ─────────────────────────────────────────────────────────────

_PRTG_LABEL = {
    "up": "Online", "down": "Offline", "warning": "Instável",
    "unusual": "Incomum", "unknown": "Desconhecido", "collecting": "Coletando",
}
_PRTG_BADGE = {
    "down": ("#fee2e2", "#b91c1c"),
    "warning": ("#fef3c7", "#b45309"),
    "unusual": ("#fef3c7", "#b45309"),
    "up": ("#dcfce7", "#15803d"),
}


def _prtg_label(slug: str) -> str:
    return _PRTG_LABEL.get(slug, (slug or "—").replace("_", " ").title())


def _prtg_badge(slug: str) -> str:
    bg, fg = _PRTG_BADGE.get(slug, ("#f1f5f9", "#475569"))
    return _badge(_prtg_label(slug), bg, fg)


def _fmt_dt(dt) -> str:
    if not dt:
        return "—"
    try:
        return timezone.localtime(dt).strftime("%d/%m/%Y às %H:%M")
    except Exception:
        return "—"


def _fmt_decorrido(dt, agora) -> str:
    """'há 2h15min' / 'há 3d' desde `dt` — deixa o horário da queda acionável
    sem exigir que quem lê o e-mail faça a conta de cabeça."""
    if not dt:
        return ""
    total_min = int((agora - dt).total_seconds() // 60)
    if total_min < 1:
        return "há instantes"
    dias, resto_min = divmod(total_min, 1440)
    horas, minutos = divmod(resto_min, 60)
    partes = []
    if dias:
        partes.append(f"{dias}d")
    if horas:
        partes.append(f"{horas}h")
    if minutos or not partes:
        partes.append(f"{minutos}min")
    return "há " + "".join(partes)


def _prtg_linha(a: dict, status_forcado: str | None = None, agora=None) -> list[str]:
    local = a.get("item_localidade") or a.get("grupo") or "—"
    desde_dt = a.get("desde")
    desde_txt = _fmt_dt(desde_dt)
    if agora and desde_dt:
        decorrido = _fmt_decorrido(desde_dt, agora)
        if decorrido:
            desde_txt += f'<br><span style="color:#94a3b8;font-size:11px;">{decorrido}</span>'
    return [
        f'<strong>{a.get("nome") or "—"}</strong>',
        a.get("host") or "—",
        local,
        _prtg_badge(status_forcado or a.get("status")),
        desde_txt,
        a.get("item_nome") or "—",
    ]


def alerta_prtg_transicoes(alarmes, recuperados=None) -> bool:
    """
    Envia UM e-mail consolidado com os equipamentos que entraram em alarme
    (offline/instável) e, opcionalmente, os que se recuperaram — detecção em
    tempo real pelo coletor `monitorar_prtg`.

    Cada item de `alarmes`/`recuperados` é um dict:
      {nome, host, grupo, status, status_anterior, statustext, desde(datetime),
       item_nome, item_localidade}
    """
    alarmes = list(alarmes or [])
    recuperados = list(recuperados or [])
    if not alarmes and not recuperados:
        return False

    _sev = {"down": 0, "warning": 1, "unusual": 1}
    alarmes.sort(key=lambda a: (_sev.get(a.get("status"), 2), (a.get("nome") or "").lower()))
    recuperados.sort(key=lambda a: (a.get("nome") or "").lower())

    n_alarme = len(alarmes)
    n_offline = sum(1 for a in alarmes if a.get("status") == "down")
    n_rec = len(recuperados)
    agora = timezone.localtime()
    cabecalho = ["Equipamento", "Host / IP", "Localidade / Grupo", "Status", "Desde", "Item vinculado"]

    secoes = []
    if alarmes:
        intro = (
            f'<p style="margin:0 0 4px;color:#334155;font-size:14px;">'
            f'<strong style="color:#b91c1c;">{n_alarme}</strong> equipamento(s) entrou(aram) em estado de alarme'
            + (f' — <strong style="color:#b91c1c;">{n_offline} offline</strong>' if n_offline else "")
            + ".</p>"
        )
        secoes.append(_secao(
            "Equipamentos em alarme", "🚨", "#b91c1c",
            intro + _tabela_html(cabecalho, [_prtg_linha(a, agora=agora) for a in alarmes]),
        ))

    if recuperados:
        intro = (
            f'<p style="margin:0 0 4px;color:#334155;font-size:14px;">'
            f'<strong style="color:#15803d;">{n_rec}</strong> equipamento(s) voltou(aram) ao normal.</p>'
        )
        secoes.append(_secao(
            "Recuperados", "✅", "#15803d",
            intro + _tabela_html(cabecalho, [_prtg_linha(a, "up", agora=agora) for a in recuperados]),
        ))

    if alarmes:
        titulo = f"PRTG: {n_alarme} equipamento(s) em alarme"
        assunto = f"[Controle TI] PRTG: {n_alarme} em alarme" + (f" · {n_offline} offline" if n_offline else "")
        if recuperados:
            assunto += f" · {n_rec} recuperado(s)"
    else:
        titulo = f"PRTG: {n_rec} equipamento(s) recuperado(s)"
        assunto = f"[Controle TI] PRTG: {n_rec} equipamento(s) recuperado(s)"

    html = _base_html(titulo, f"Detecção automática · {agora.strftime('%d/%m/%Y às %H:%M')}", "".join(secoes))

    linhas_txt = [f"ALARME PRTG — {agora.strftime('%d/%m/%Y %H:%M')}", ""]
    for a in alarmes:
        decorrido = _fmt_decorrido(a.get("desde"), agora)
        linhas_txt.append(
            f"- [{_prtg_label(a.get('status')).upper()}] {a.get('nome')} | {a.get('host') or '—'} | "
            f"desde {_fmt_dt(a.get('desde'))}" + (f" ({decorrido})" if decorrido else "")
            + (f" | Item: {a.get('item_nome')}" if a.get("item_nome") else "")
        )
    if recuperados:
        linhas_txt += ["", "RECUPERADOS", ""]
        for a in recuperados:
            linhas_txt.append(
                f"- [ONLINE] {a.get('nome')} | {a.get('host') or '—'} | desde {_fmt_dt(a.get('desde'))}"
            )
    texto = "\n".join(linhas_txt)

    return _enviar(assunto, texto, html, codigo="prtg_transicoes")


# ─────────────────────────────────────────────────────────────
# Segurança — acesso suspeito (ISO 27001 A.8.16)
# ─────────────────────────────────────────────────────────────

def alerta_acesso_suspeito(evento) -> bool:
    """
    Alerta de acesso suspeito à aplicação (ISO 27001 A.8.16 Monitoramento):
    rajada de falhas de login ou login bem-sucedido logo após várias falhas.
    `evento` é um RegistroSeguranca (disparado por services/seguranca_service.py).
    """
    if evento is None:
        return False

    agora = timezone.localtime(evento.criado_em) if evento.criado_em else timezone.localtime()
    tipo_txt = evento.get_tipo_display()
    # Login bem-sucedido após rajada de falhas é mais grave que só falhas.
    critico = (evento.tipo == "login_ok")
    cor = "#b91c1c" if critico else "#c2410c"
    emoji = "⛔" if critico else "⚠️"

    linhas = [
        ["Evento", tipo_txt],
        ["Usuário", evento.username or "—"],
        ["Origem (IP)", evento.ip or "—"],
        ["Detalhe", evento.detalhe or "—"],
        ["Caminho", evento.caminho or "—"],
        ["Data/hora", agora.strftime("%d/%m/%Y %H:%M:%S")],
        ["Agente", (evento.user_agent or "—")[:160]],
    ]
    intro = (
        f'<p style="margin:0 0 10px;color:#334155;font-size:14px;">'
        f'Atividade de autenticação sinalizada como '
        f'<strong style="color:{cor};">suspeita</strong>. Revise em '
        f'<em>Registros de segurança</em> (admin) e, se não reconhecer, troque a '
        f'senha da conta e bloqueie a origem.</p>'
    )
    secao = _secao("Acesso suspeito", emoji, cor, intro + _tabela_html(["Campo", "Valor"], linhas))
    titulo = "Acesso suspeito detectado"
    assunto = f"[Controle TI] Segurança: acesso suspeito · {evento.username or 'desconhecido'}"
    html = _base_html(titulo, f"Monitoramento · {agora.strftime('%d/%m/%Y às %H:%M')}", secao)

    texto = (
        f"ACESSO SUSPEITO — {agora.strftime('%d/%m/%Y %H:%M')}\n\n"
        f"Evento: {tipo_txt}\n"
        f"Usuario: {evento.username or '—'}\n"
        f"IP: {evento.ip or '—'}\n"
        f"Detalhe: {evento.detalhe or '—'}\n"
        f"Caminho: {evento.caminho or '—'}\n"
    )
    return _enviar(assunto, texto, html, codigo="acesso_suspeito")


# ─────────────────────────────────────────────────────────────
# Relatório Diário Consolidado
# ─────────────────────────────────────────────────────────────

def relatorio_diario(horas: int = 24) -> bool:
    """
    Envia UM único e-mail diário com todas as informações relevantes do sistema:
      · Estoque — itens de consumo críticos (qtd < 2)
      · Baixas de estoque nas últimas N horas
      · Movimentações (entregas, devoluções, transferências) nas últimas N horas
      · Licenças vinculadas a colaboradores desligados
      · Preventivas vencidas e próximas (7 dias)

    Sempre envia, mesmo que tudo esteja OK (confirma que o sistema está rodando).
    """
    from ProjetoEstoque.models import (
        MovimentacaoItem, MovimentacaoLicenca, StatusUsuarioChoices,
    )

    agora = timezone.now()
    corte = agora - timedelta(hours=horas)
    hoje = timezone.localdate()

    # ── Queries ────────────────────────────────────────────────

    # Estoque crítico considerando LOTES (mesma regra do alerta dedicado).
    itens_criticos = itens_estoque_critico(2)

    baixas = (
        MovimentacaoItem.objects
        .filter(tipo_movimentacao="baixa", created_at__gte=corte)
        .select_related(
            "item", "item__subtipo",
            "localidade_origem", "centro_custo_origem",
            "lote", "lote__fornecedor",
            "criado_por",
        )
        .order_by("-created_at")
    )

    movimentacoes = (
        MovimentacaoItem.objects
        .filter(created_at__gte=corte)
        .exclude(tipo_movimentacao__in=["baixa", "entrada"])
        .select_related(
            "item", "usuario",
            "localidade_origem", "localidade_destino",
            "centro_custo_destino", "criado_por",
        )
        .order_by("-created_at")
    )

    _mov_dev_sub = MovimentacaoLicenca.objects.filter(
        usuario=OuterRef("usuario_id"),
        lote=OuterRef("lote_id"),
        tipo="devolucao",
        created_at__gt=OuterRef("created_at"),
    )
    licencas = (
        MovimentacaoLicenca.objects
        .filter(tipo="atribuicao", usuario__status=StatusUsuarioChoices.DESLIGADO)
        .annotate(foi_devolvida=Exists(_mov_dev_sub))
        .filter(foi_devolvida=False)
        .select_related(
            "usuario", "lote", "lote__licenca",
            "usuario__funcao", "usuario__centro_custo",
        )
        .order_by("usuario__nome")
    )

    # Vencidas e próximas (data efetiva = data_ultima + intervalo), mesma regra das telas.
    prev_vencidas, prev_proximas = preventivas_relevantes(7)

    # ── Counts ─────────────────────────────────────────────────
    n_criticos = len(itens_criticos)
    n_baixas = baixas.count()
    n_movs = movimentacoes.count()
    n_licencas = licencas.count()
    n_vencidas = len(prev_vencidas)
    n_proximas = len(prev_proximas)
    n_sem_estoque = sum(1 for i in itens_criticos if i.estoque_efetivo == 0)

    # ── KPI boxes ─────────────────────────────────────────────
    def _kpi(label: str, valor, cor: str, nota: str = "") -> str:
        nota_html = f'<p style="margin:2px 0 0;color:#94a3b8;font-size:10px;">{nota}</p>' if nota else ""
        return (
            f'<td align="center" style="padding:14px 6px;border-right:1px solid #e2e8f0;min-width:80px;">'
            f'<p style="margin:0;font-size:26px;font-weight:900;color:{cor};line-height:1;">{valor}</p>'
            f'<p style="margin:4px 0 0;font-size:10px;font-weight:700;color:#64748b;'
            f'text-transform:uppercase;letter-spacing:.05em;line-height:1.3;">{label}</p>'
            f'{nota_html}</td>'
        )

    kpis = (
        '<table width="100%" cellpadding="0" cellspacing="0" '
        'style="border-collapse:collapse;border:1px solid #e2e8f0;border-radius:10px;'
        'overflow:hidden;margin-bottom:28px;background:#f8fafc;">'
        '<tr>'
        + _kpi("Itens críticos", n_criticos,
               "#b45309" if n_criticos > 0 else "#059669",
               nota=f"{n_sem_estoque} sem estoque" if n_sem_estoque else "")
        + _kpi("Baixas", n_baixas,
               "#991b1b" if n_baixas > 0 else "#334155",
               nota=f"últimas {horas}h")
        + _kpi("Movimentações", n_movs,
               "#1d4ed8",
               nota=f"últimas {horas}h")
        + _kpi("Lic. pendentes", n_licencas,
               "#b91c1c" if n_licencas > 0 else "#059669")
        + _kpi("Prev. vencidas", n_vencidas,
               "#b91c1c" if n_vencidas > 0 else "#059669")
        + _kpi("Prev. próximas", n_proximas,
               "#b45309" if n_proximas > 0 else "#334155",
               nota="7 dias")
        + '</tr></table>'
    )

    # ── Seção 1: Estoque crítico ───────────────────────────────
    if n_criticos > 0:
        linhas_c = []
        for it in itens_criticos:
            qtd = it.estoque_efetivo
            badge = (
                _badge("SEM ESTOQUE", "#fee2e2", "#b91c1c") if qtd == 0
                else _badge(f"{qtd} un.", "#fef3c7", "#b45309")
            )
            linhas_c.append([
                f'<strong>{it.nome}</strong>',
                it.subtipo.nome if it.subtipo else "—",
                it.localidade.local if it.localidade else "—",
                it.centro_custo.departamento if it.centro_custo else "—",
                _lotes_celula(it),
                badge,
            ])
        bloco_criticos = (
            f'<p style="margin:0 0 4px;color:#334155;font-size:13px;">'
            f'<strong>{n_criticos}</strong> item(ns) abaixo do mínimo recomendado'
            + (f' — <strong style="color:#b91c1c;">{n_sem_estoque} completamente sem estoque</strong>' if n_sem_estoque else "")
            + ".</p>"
            + _tabela_html(["Item", "Subtipo", "Localidade", "Centro de Custo", "Lotes (disp./entrada)", "Estoque"], linhas_c)
        )
    else:
        bloco_criticos = _linha_ok("Todos os itens de consumo estão com estoque adequado.")

    secao1 = _secao("Estoque — Itens Críticos de Consumo", "📦", "#b45309", bloco_criticos)

    # ── Seção 2: Baixas recentes ───────────────────────────────
    if n_baixas > 0:
        linhas_b = []
        for b in baixas:
            hora = timezone.localtime(b.created_at).strftime("%d/%m %H:%M")
            operador = ""
            if b.criado_por:
                operador = b.criado_por.get_full_name() or b.criado_por.username
            fornecedor = (
                b.lote.fornecedor.nome
                if b.lote and b.lote.fornecedor else "—"
            )
            linhas_b.append([
                f'<strong>{b.item.nome}</strong>',
                f'<strong style="color:#991b1b;">&#8722; {b.quantidade or 0} un.</strong>',
                _fmt_brl(b.custo),
                b.localidade_origem.local if b.localidade_origem else "—",
                fornecedor,
                operador or "—",
                hora,
            ])
        bloco_baixas = (
            f'<p style="margin:0 0 4px;color:#334155;font-size:13px;">'
            f'<strong>{n_baixas}</strong> baixa(s) registrada(s) nas últimas {horas}h.</p>'
            + _tabela_html(
                ["Item", "Quantidade", "Custo", "Localidade", "Fornecedor", "Operador", "Horário"],
                linhas_b,
            )
        )
    else:
        bloco_baixas = _linha_vazia(f"Nenhuma baixa registrada nas últimas {horas}h.")

    secao2 = _secao(f"Baixas de Estoque — últimas {horas}h", "⬇️", "#991b1b", bloco_baixas)

    # ── Seção 3: Movimentações recentes ───────────────────────
    _MOV_LABELS = {
        "transferencia": "Transferência",
        "transferencia_equipamento": "Transf. Equipamento",
        "envio_manutencao": "Envio Manutenção",
        "retorno_manutencao": "Retorno Manutenção",
        "retorno": "Retorno",
    }

    if n_movs > 0:
        linhas_m = []
        for m in movimentacoes:
            hora = timezone.localtime(m.created_at).strftime("%d/%m %H:%M")
            tipo_label = _MOV_LABELS.get(m.tipo_movimentacao, m.tipo_movimentacao)
            orig = m.localidade_origem.local if m.localidade_origem else "—"
            dest = m.localidade_destino.local if m.localidade_destino else "—"
            operador = ""
            if m.criado_por:
                operador = m.criado_por.get_full_name() or m.criado_por.username
            usuario_nome = (
                getattr(m.usuario, "nome", None) or getattr(m.usuario, "email", None)
                if m.usuario else "—"
            ) or "—"
            linhas_m.append([
                f'<strong>{m.item.nome}</strong>',
                tipo_label,
                f'{orig} → {dest}',
                usuario_nome,
                operador or "—",
                hora,
            ])
        bloco_movs = (
            f'<p style="margin:0 0 4px;color:#334155;font-size:13px;">'
            f'<strong>{n_movs}</strong> movimentação(ões) registrada(s) nas últimas {horas}h.</p>'
            + _tabela_html(
                ["Item", "Tipo", "Origem → Destino", "Colaborador", "Operador", "Horário"],
                linhas_m,
            )
        )
    else:
        bloco_movs = _linha_vazia(f"Nenhuma movimentação registrada nas últimas {horas}h.")

    secao3 = _secao(f"Movimentações — últimas {horas}h", "🔄", "#1d4ed8", bloco_movs)

    # ── Seção 4: Licenças de desligados ───────────────────────
    if n_licencas > 0:
        linhas_l = []
        for m in licencas:
            dt = m.usuario.data_termino
            linhas_l.append([
                m.usuario.nome or m.usuario.email or "—",
                m.usuario.funcao.nome if m.usuario.funcao else "—",
                m.usuario.centro_custo.departamento if m.usuario.centro_custo else "—",
                m.lote.licenca.nome if m.lote and m.lote.licenca else "—",
                dt.strftime("%d/%m/%Y") if dt else "—",
                _badge("PENDENTE", "#fee2e2", "#b91c1c"),
            ])
        bloco_lic = (
            f'<p style="margin:0 0 4px;color:#334155;font-size:13px;">'
            f'<strong style="color:#b91c1c;">{n_licencas}</strong> licença(s) pendente(s) de devolução. '
            f'Acesse o sistema para regularizar.</p>'
            + _tabela_html(
                ["Colaborador", "Função", "Centro de Custo", "Licença", "Desligamento", "Status"],
                linhas_l,
            )
        )
    else:
        bloco_lic = _linha_ok("Nenhuma licença pendente de colaboradores desligados.")

    secao4 = _secao("Licenças — Colaboradores Desligados", "🔑", "#b91c1c", bloco_lic)

    # ── Seção 5: Preventivas ──────────────────────────────────
    blocos_prev = []

    if n_vencidas > 0:
        linhas_pv = []
        for p in prev_vencidas:
            dias_atraso = (hoje - p.data_proxima).days
            linhas_pv.append([
                f'<strong>{p.equipamento.nome}</strong>',
                p.equipamento.numero_serie or "—",
                p.equipamento.localidade.local if p.equipamento.localidade else "—",
                p.checklist_modelo.nome if p.checklist_modelo else "—",
                p.data_proxima.strftime("%d/%m/%Y"),
                _badge(f"{dias_atraso}d atraso", "#fee2e2", "#b91c1c"),
            ])
        blocos_prev.append(
            f'<p style="margin:8px 0 4px;color:#991b1b;font-size:13px;font-weight:700;">'
            f'Vencidas ({n_vencidas})</p>'
            + _tabela_html(
                ["Equipamento", "Nº Série", "Localidade", "Checklist", "Vencimento", "Prazo"],
                linhas_pv,
            )
        )

    if n_proximas > 0:
        linhas_pp = []
        for p in prev_proximas:
            dias_rest = (p.data_proxima - hoje).days
            if dias_rest == 0:
                badge = _badge("HOJE", "#fee2e2", "#b91c1c")
            elif dias_rest <= 2:
                badge = _badge(f"{dias_rest}d", "#fef3c7", "#b45309")
            else:
                badge = _badge(f"{dias_rest}d", "#dbeafe", "#1d4ed8")
            linhas_pp.append([
                f'<strong>{p.equipamento.nome}</strong>',
                p.equipamento.numero_serie or "—",
                p.equipamento.localidade.local if p.equipamento.localidade else "—",
                p.checklist_modelo.nome if p.checklist_modelo else "—",
                p.data_proxima.strftime("%d/%m/%Y"),
                badge,
            ])
        blocos_prev.append(
            f'<p style="margin:{"16px" if n_vencidas else "8px"} 0 4px;color:#1d4ed8;'
            f'font-size:13px;font-weight:700;">Próximas 7 dias ({n_proximas})</p>'
            + _tabela_html(
                ["Equipamento", "Nº Série", "Localidade", "Checklist", "Próxima", "Prazo"],
                linhas_pp,
            )
        )

    if not blocos_prev:
        blocos_prev.append(_linha_ok("Nenhuma preventiva vencida ou próxima do vencimento."))

    bloco_prev_intro = (
        f'<p style="margin:0 0 4px;color:#334155;font-size:13px;">'
        f'{n_vencidas} vencida(s) · {n_proximas} nos próximos 7 dias.</p>'
    )
    secao5 = _secao("Manutenção Preventiva", "🛠️", "#1d4ed8", bloco_prev_intro + "".join(blocos_prev))

    # ── Montagem final ─────────────────────────────────────────
    corpo = kpis + secao1 + secao2 + secao3 + secao4 + secao5

    html = _base_html(
        "Relatório Diário — Controle TI",
        f"Referência: {hoje.strftime('%d/%m/%Y')} · Período: últimas {horas}h",
        corpo,
    )

    texto = (
        f"RELATÓRIO DIÁRIO — {hoje.strftime('%d/%m/%Y')}\n"
        f"{'='*48}\n"
        f"Itens de consumo críticos : {n_criticos} ({n_sem_estoque} sem estoque)\n"
        f"Baixas (últimas {horas}h)       : {n_baixas}\n"
        f"Movimentações (últimas {horas}h): {n_movs}\n"
        f"Licenças pendentes        : {n_licencas}\n"
        f"Preventivas vencidas      : {n_vencidas}\n"
        f"Preventivas próximas (7d) : {n_proximas}\n"
    )

    return _enviar(
        f"[Controle TI] Relatório Diário — {hoje.strftime('%d/%m/%Y')}",
        texto, html, codigo="relatorio_diario",
    )


# ─────────────────────────────────────────────────────────────
# Dispatcher de alertas periódicos individuais (uso ad-hoc)
# ─────────────────────────────────────────────────────────────

def enviar_todos_alertas() -> dict:
    """Envia os três alertas periódicos individuais. Prefira relatorio_diario() para o agendamento."""
    return {
        "preventivas": alerta_preventivas_proximas(dias=7),
        "estoque_critico": alerta_estoque_critico(limite_qtd=2),
        "licencas_desligados": alerta_licencas_desligados(),
    }
