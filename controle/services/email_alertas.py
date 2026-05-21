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

def _enviar(assunto: str, texto: str, html: str, destinatarios: list[str] | None = None) -> bool:
    alvos = destinatarios or DESTINATARIOS
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
        msg.send(fail_silently=False)
        logger.info("email_alertas: '%s' enviado para %s", assunto, alvos)
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


def _linha_ok(texto: str) -> str:
    return (
        f'<p style="margin:6px 0 0;color:#059669;font-size:13px;">✅ {texto}</p>'
    )


# ─────────────────────────────────────────────────────────────
# Alerta 1 — Preventivas próximas
# ─────────────────────────────────────────────────────────────

def alerta_preventivas_proximas(dias: int = 7) -> bool:
    from ProjetoEstoque.models import Preventiva

    hoje = timezone.localdate()
    limite = hoje + timedelta(days=dias)

    qs = (
        Preventiva.objects
        .filter(data_proxima__gte=hoje, data_proxima__lte=limite, pausada=False)
        .select_related("equipamento", "equipamento__localidade", "checklist_modelo")
        .order_by("data_proxima", "equipamento__nome")
    )

    if not qs.exists():
        logger.info("alerta_preventivas: nenhuma preventiva nos próximos %d dias.", dias)
        return False

    linhas = []
    for p in qs:
        dias_rest = (p.data_proxima - hoje).days
        if dias_rest == 0:
            badge = _badge("VENCE HOJE", "#fee2e2", "#b91c1c")
        elif dias_rest <= 2:
            badge = _badge(f"{dias_rest}d", "#fef3c7", "#b45309")
        else:
            badge = _badge(f"{dias_rest}d", "#dbeafe", "#1d4ed8")
        linhas.append([
            p.equipamento.nome,
            p.equipamento.numero_serie or "—",
            p.equipamento.localidade.local if p.equipamento.localidade else "—",
            p.checklist_modelo.nome if p.checklist_modelo else "—",
            p.data_proxima.strftime("%d/%m/%Y"),
            badge,
        ])

    tabela = _tabela_html(
        ["Equipamento", "Nº Série", "Localidade", "Checklist", "Próxima", "Prazo"],
        linhas,
    )
    total = len(linhas)
    vencendo_hoje = sum(1 for p in qs if p.data_proxima == hoje)

    intro = (
        f'<p style="margin:0 0 4px;color:#334155;font-size:14px;">'
        f'<strong>{total}</strong> preventiva(s) com vencimento nos próximos <strong>{dias} dias</strong>'
        + (f' — incluindo <strong style="color:#b91c1c;">{vencendo_hoje} que vencem hoje</strong>' if vencendo_hoje else "")
        + ".</p>"
    )

    corpo = _secao(f"Preventivas — próximos {dias} dias", "🛠️", "#1d4ed8", intro + tabela)
    html = _base_html(
        f"Preventivas: {total} agendada(s) nos próximos {dias} dias",
        f"Alerta gerado em {hoje.strftime('%d/%m/%Y')}",
        corpo,
    )
    texto = (
        f"ALERTA — Preventivas nos próximos {dias} dias\nTotal: {total}\n\n"
        + "\n".join(
            f"- {p.equipamento.nome} | {p.equipamento.numero_serie or '—'} | Vence: {p.data_proxima.strftime('%d/%m/%Y')}"
            for p in qs
        )
    )

    return _enviar(
        f"[Controle TI] {total} preventiva(s) nos próximos {dias} dias",
        texto, html,
    )


# ─────────────────────────────────────────────────────────────
# Alerta 2 — Estoque crítico
# ─────────────────────────────────────────────────────────────

def alerta_estoque_critico(limite_qtd: int = 2) -> bool:
    from ProjetoEstoque.models import Item, SimNaoChoices

    qs = (
        Item.objects
        .filter(item_consumo=SimNaoChoices.SIM, quantidade__lt=limite_qtd)
        .select_related("localidade", "centro_custo", "subtipo")
        .order_by("quantidade", "nome")
    )

    if not qs.exists():
        logger.info("alerta_estoque: nenhum item com estoque crítico.")
        return False

    linhas = []
    for item in qs:
        qtd = item.quantidade or 0
        badge = _badge("SEM ESTOQUE", "#fee2e2", "#b91c1c") if qtd == 0 else _badge(f"{qtd} un.", "#fef3c7", "#b45309")
        linhas.append([
            item.nome,
            item.subtipo.nome if item.subtipo else "—",
            item.localidade.local if item.localidade else "—",
            item.centro_custo.departamento if item.centro_custo else "—",
            badge,
        ])

    tabela = _tabela_html(["Item", "Subtipo", "Localidade", "Centro de Custo", "Estoque"], linhas)
    sem_estoque = sum(1 for i in qs if (i.quantidade or 0) == 0)
    total = len(linhas)
    hoje = date.today()

    intro = (
        f'<p style="margin:0 0 4px;color:#334155;font-size:14px;">'
        f'<strong>{total}</strong> item(ns) abaixo do estoque mínimo'
        + (f' — <strong style="color:#b91c1c;">{sem_estoque} completamente sem estoque</strong>' if sem_estoque else "")
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
            f"- {i.nome} | Qtd: {i.quantidade or 0} | {i.localidade.local if i.localidade else '—'}"
            for i in qs
        )
    )

    return _enviar(f"[Controle TI] {total} item(ns) com estoque crítico", texto, html)


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
        texto, html,
    )


# ─────────────────────────────────────────────────────────────
# Alerta 4 — Movimentação transacional (entrega / devolução / baixa)
# ─────────────────────────────────────────────────────────────

def alerta_movimentacao(mov) -> bool:
    """
    Envia e-mail de notificação imediata para qualquer movimentação registrada.
    Suporta: entrega, devolução e baixa.
    """
    item = mov.item
    usuario = mov.usuario
    registrado_por = getattr(mov, "criado_por", None)
    tipo = mov.tipo_movimentacao
    tipo_transferencia = getattr(mov, "tipo_transferencia", None)

    eh_baixa = tipo == "baixa"
    eh_devolucao = tipo_transferencia == "devolucao"
    tipo_label = _TIPO_LABELS.get(tipo, tipo)

    if eh_baixa:
        titulo_banner, subtipo_label = "Baixa / Consumo", ""
        banner_bg, banner_border = "#fef2f2", "#ef4444"
        banner_title_color, banner_sub_color = "#991b1b", "#dc2626"
    elif eh_devolucao:
        titulo_banner, subtipo_label = "Transferência", " — Devolução"
        banner_bg, banner_border = "#fffbeb", "#f59e0b"
        banner_title_color, banner_sub_color = "#92400e", "#b45309"
    else:
        titulo_banner, subtipo_label = "Transferência", " — Entrega ao colaborador"
        banner_bg, banner_border = "#eff6ff", "#3b82f6"
        banner_title_color, banner_sub_color = "#1e40af", "#3b82f6"

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
        secao_colaborador_titulo = "Colaborador (devolução)" if eh_devolucao else "Colaborador Destino"
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

    destinatarios = list(DESTINATARIOS)
    if colaborador_email and colaborador_email not in destinatarios:
        destinatarios.append(colaborador_email)

    if eh_baixa:
        assunto = f"[Controle TI] Baixa registrada: {item.nome} — {mov.quantidade or 0} un."
    elif eh_devolucao:
        assunto = f"[Controle TI] Devolução: {item.nome} — {usuario.nome if usuario else '—'}"
    else:
        assunto = f"[Controle TI] Entrega: {item.nome} — {usuario.nome if usuario else '—'}"

    return _enviar(assunto, texto, html, destinatarios=destinatarios)


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
        texto, html,
    )


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
        Item, SimNaoChoices, MovimentacaoItem,
        MovimentacaoLicenca, StatusUsuarioChoices, Preventiva,
    )

    agora = timezone.now()
    corte = agora - timedelta(hours=horas)
    hoje = timezone.localdate()
    limite_7d = hoje + timedelta(days=7)

    # ── Queries ────────────────────────────────────────────────

    itens_criticos = (
        Item.objects
        .filter(item_consumo=SimNaoChoices.SIM, quantidade__lt=2)
        .select_related("localidade", "centro_custo", "subtipo")
        .order_by("quantidade", "nome")
    )

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

    prev_vencidas = (
        Preventiva.objects
        .filter(data_proxima__lt=hoje, pausada=False)
        .select_related("equipamento", "equipamento__localidade", "checklist_modelo")
        .order_by("data_proxima")
    )

    prev_proximas = (
        Preventiva.objects
        .filter(data_proxima__gte=hoje, data_proxima__lte=limite_7d, pausada=False)
        .select_related("equipamento", "equipamento__localidade", "checklist_modelo")
        .order_by("data_proxima")
    )

    # ── Counts ─────────────────────────────────────────────────
    n_criticos = itens_criticos.count()
    n_baixas = baixas.count()
    n_movs = movimentacoes.count()
    n_licencas = licencas.count()
    n_vencidas = prev_vencidas.count()
    n_proximas = prev_proximas.count()
    n_sem_estoque = sum(1 for i in itens_criticos if (i.quantidade or 0) == 0)

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
            qtd = it.quantidade or 0
            badge = (
                _badge("SEM ESTOQUE", "#fee2e2", "#b91c1c") if qtd == 0
                else _badge(f"{qtd} un.", "#fef3c7", "#b45309")
            )
            linhas_c.append([
                f'<strong>{it.nome}</strong>',
                it.subtipo.nome if it.subtipo else "—",
                it.localidade.local if it.localidade else "—",
                it.centro_custo.departamento if it.centro_custo else "—",
                badge,
            ])
        bloco_criticos = (
            f'<p style="margin:0 0 4px;color:#334155;font-size:13px;">'
            f'<strong>{n_criticos}</strong> item(ns) abaixo do mínimo recomendado'
            + (f' — <strong style="color:#b91c1c;">{n_sem_estoque} completamente sem estoque</strong>' if n_sem_estoque else "")
            + ".</p>"
            + _tabela_html(["Item", "Subtipo", "Localidade", "Centro de Custo", "Estoque"], linhas_c)
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
        texto, html,
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
