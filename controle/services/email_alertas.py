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
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from email.mime.image import MIMEImage

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
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
        codigo="m2m_ocorrencias", nome="Ocorrências de Frotas e Conectividade", categoria="Monitoramento",
        descricao="Ocorrência nova em chips e veículos: falha de consulta, sem sessão, dados desatualizados, consumo "
        "alto, identificador divergente, veículo sem chip ou sem atualização, vínculo alterado. Só tipos com e-mail ligado.",
        icone="fa-sim-card", tipo_destinatarios="dinamico",
        destino_gerenciado_em="Frotas e Conectividade › Configuração",
        origem_disparo="Agendado — manage.py monitorar_m2m (Task Scheduler), atualização manual de um chip ou "
        "movimentação de vínculo, só para ocorrência NOVA (services/m2m_monitoramento_service.py)",
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
    dict(
        codigo="computadores_parados", nome="Computadores parados (backup / pausado)", categoria="Estoque",
        descricao="Notebooks e desktops em Backup ou Pausado, com a Licença Office presa a cada "
        "máquina e há quanto tempo estão parados — para cancelar o que paga no vazio e religar "
        "quando o equipamento volta a Ativo.",
        icone="fa-laptop", tipo_destinatarios="fixo",
        origem_disparo="Semanal (Task Scheduler, via 'agendar_computadores_parados') + a cada "
        "mudança de status de notebook/desktop que entra ou sai de Backup/Pausado",
    ),
    dict(
        codigo="ninja_licencas_liberar", nome="NinjaOne — licenças de máquinas paradas", categoria="Estoque",
        descricao="Máquinas em Backup, Defeito, Manutenção ou Pausado que ainda têm dispositivo no NinjaOne — cada uma "
        "ocupa uma licença paga até o TI removê-la lá. A lista completa vai em CSV anexo.",
        icone="fa-plug-circle-xmark", tipo_destinatarios="fixo",
        origem_disparo="Semanal (Task Scheduler, via 'agendar_ninja_licencas') + botão 'Enviar e-mail agora' "
        "na tela Licenças a liberar do NinjaOne",
    ),
    dict(
        codigo="chamado_contratacao_maquina", nome="Chamado de contratação — máquina", categoria="Contratações",
        descricao="Pedido de configuração e entrega de máquina para uma vaga em aberto. Um e-mail por "
        "pedido, com os dados da vaga e a descrição do equipamento.",
        icone="fa-laptop", tipo_destinatarios="fixo",
        origem_disparo="Manual — botão 'Solicitar estrutura e abrir chamados' na vaga em aberto (Contratações)",
    ),
    dict(
        codigo="chamado_contratacao_usuario_email", nome="Chamado de contratação — usuário e e-mail",
        categoria="Contratações",
        descricao="Pedido de criação de usuário de rede e e-mail corporativo para uma vaga em aberto. "
        "Só é enviado quando o solicitante marca que a criação é necessária.",
        icone="fa-user-plus", tipo_destinatarios="fixo",
        origem_disparo="Manual — botão 'Solicitar estrutura e abrir chamados' na vaga em aberto (Contratações)",
    ),
    dict(
        codigo="chamado_contratacao_sistemas", nome="Chamado de contratação — orientação e acesso a sistemas",
        categoria="Contratações",
        descricao="Pedido de orientação, configuração ou liberação de acesso aos sistemas para uma vaga "
        "em aberto. Só é enviado quando o solicitante marca que o suporte é necessário.",
        icone="fa-shield-halved", tipo_destinatarios="fixo",
        origem_disparo="Manual — botão 'Solicitar estrutura e abrir chamados' na vaga em aberto (Contratações)",
    ),
    dict(
        codigo="vaga_sem_equipamento", nome="Vaga sem equipamento reservado", categoria="Contratações",
        descricao="Avisa quando o equipamento reservado para uma vaga em aberto foi usado em outra "
        "necessidade e não há outro do mesmo tipo em Backup para repor a reserva. Um e-mail por ocorrência: "
        "vaga, equipamento usado, quem usou e quando.",
        icone="fa-laptop-medical", tipo_destinatarios="fixo",
        origem_disparo="Transacional — ao usar um equipamento reservado em outra movimentação, "
        "sem substituto em Backup (services/reserva_equipamento_service.py)",
    ),
    dict(
        codigo="requisicao_enviada_aprovacao", nome="Requisição enviada para aprovação", categoria="Requisições",
        descricao="Avisa o aprovador definido aqui (destinatário customizado) sempre que uma requisição de "
        "Compra ou Estoque é enviada para aprovação, com a lista de itens.",
        icone="fa-paper-plane", tipo_destinatarios="fixo",
        origem_disparo="Transacional — ao enviar para aprovação (quadro Kanban ou tela de detalhe da requisição)",
    ),
    dict(
        codigo="requisicao_itens_retirados", nome="Itens de requisição retirados", categoria="Requisições",
        descricao="Avisa que um ou mais itens de uma requisição (Compra ou Estoque) foram retirados no "
        "almoxarifado — individualmente ou a requisição inteira de uma vez.",
        icone="fa-box-open", tipo_destinatarios="dinamico",
        destino_gerenciado_em="E-mail de quem criou a requisição, somado à lista padrão do sistema (.env)",
        origem_disparo="Transacional — ao marcar item(ns) como retirado no quadro Kanban de Requisições",
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
_CANAIS_BASE_EDITAVEL_DINAMICOS = {
    "movimentacao_transacional", "transferencia_equipamento", "requisicao_itens_retirados",
}


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

    if canal.codigo == "m2m_ocorrencias":
        from services import m2m_monitoramento_service as m2m

        pessoas, vistos = [], set()
        for regra in m2m.regras_de_alerta().values():
            if not regra.envia_email:
                continue
            for email in m2m.destinatarios_da_regra(regra):
                if email.lower() not in vistos:
                    vistos.add(email.lower())
                    pessoas.append({
                        "nome": email, "email": email, "contexto": None, "removivel": False,
                        "remove_kind": None, "remove_id": None,
                    })
        nota = (
            "Os destinatários são definidos regra a regra em Frotas e Conectividade › Configuração › Regras de alerta. "
            "Regra com e-mail ligado e sem destinatário usa a lista padrão do sistema (.env)."
        )
        return pessoas, nota

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


@dataclass(frozen=True)
class ResultadoEnvio:
    """O que `_enviar_detalhado` devolve: o `bool` de sempre MAIS o porquê e
    para quem. `ok=True` significa que o servidor de e-mail ACEITOU a mensagem —
    não que ela foi lida nem que um chamado foi aberto. `motivo` já vem em texto
    de tela quando `ok=False`."""
    ok: bool
    motivo: str = ""
    destinatarios: tuple = field(default_factory=tuple)


def _enviar_detalhado(
    assunto: str, texto: str, html: str,
    destinatarios: list[str] | None = None, *, codigo: str = "",
    anexos: list[tuple[str, bytes, str]] | None = None,
    timeout: int | None = None,
    headers: dict[str, str] | None = None,
    cc: list[str] | None = None,
    imagens_inline: list[tuple[str, bytes, str]] | None = None,
) -> ResultadoEnvio:
    """Envia o e-mail e diz o que aconteceu. `codigo` identifica o canal em
    CanalNotificacao (opcional, mas todo alerta novo deveria ter um — ver
    CATALOGO_NOTIFICACOES): se o canal estiver desativado pelo TI, suprime o
    envio; se for do tipo 'fixo' e tiver `destinatarios_customizados_ativo=True`,
    a lista customizada substitui `destinatarios` por completo — mesmo vazia
    (ninguém removido é reintroduzido silenciosamente pelo padrão do .env).
    `anexos`: lista opcional de (nome_arquivo, conteudo, mimetype) anexados ao
    e-mail. `timeout` (segundos) limita a conversa com o SMTP: sem ele um
    servidor que não responde pendura a requisição que está esperando.
    `headers` (ex.: `Message-ID`, `In-Reply-To`, `References`) e `cc` existem
    para a conversa dos chamados: o `Message-ID` é gerado ANTES do envio e
    guardado, porque é por ele que a resposta, depois, volta para o chamado certo.

    É a fonte única da regra de envio; `_enviar` abaixo só reduz o resultado a
    `bool` para os alertas que não precisam do motivo."""
    if not _alertas_habilitados():
        logger.info("email_alertas: envio suprimido — alertas desativados nas configurações do sistema.")
        return ResultadoEnvio(False, "Os alertas por e-mail estão desativados nas configurações do sistema.")

    canal = None
    if codigo:
        from ProjetoEstoque.models import CanalNotificacao
        canal = CanalNotificacao.objects.filter(codigo=codigo).first()
        if canal is not None and not canal.ativo:
            logger.info("email_alertas: envio suprimido — canal '%s' desativado pelo TI.", codigo)
            return ResultadoEnvio(
                False, "Este tipo de e-mail está desativado em Central de Alertas → Configurar notificações.",
            )

    alvos = destinatarios or DESTINATARIOS
    if (
        canal is not None
        and canal.tipo_destinatarios == canal.TipoDestinatarios.FIXO
        and canal.destinatarios_customizados_ativo
    ):
        alvos = canal.destinatarios_lista()

    if not alvos:
        logger.warning("email_alertas: nenhum destinatário configurado.")
        return ResultadoEnvio(
            False, "Nenhum destinatário configurado (Central de Alertas → Configurar notificações).",
        )
    try:
        msg = EmailMultiAlternatives(
            subject=assunto,
            body=texto,
            from_email=REMETENTE,
            to=alvos,
            cc=cc or None,
            headers=headers or None,
            connection=get_connection(timeout=timeout) if timeout else None,
        )
        msg.attach_alternative(html, "text/html")
        if imagens_inline:
            # `related`: a imagem pertence ao corpo HTML (referenciada por
            # `cid:`), não é anexo — não aparece como arquivo para baixar.
            msg.mixed_subtype = "related"
            for cid, conteudo, subtipo in imagens_inline:
                imagem = MIMEImage(conteudo, _subtype=subtipo)
                imagem.add_header("Content-ID", f"<{cid}>")
                imagem.add_header("Content-Disposition", "inline", filename=f"{cid}.{subtipo}")
                msg.attach(imagem)
        for nome_arquivo, conteudo, mimetype in (anexos or []):
            msg.attach(nome_arquivo, conteudo, mimetype)
        msg.send(fail_silently=False)
        logger.info("email_alertas: '%s' enviado para %s", assunto, alvos)
        if canal is not None:
            from django.db.models import F
            type(canal).objects.filter(pk=canal.pk).update(
                ultimo_envio=timezone.now(), total_envios=F("total_envios") + 1,
            )
        return ResultadoEnvio(True, "", tuple(alvos))
    except Exception as exc:
        logger.error("email_alertas: falha ao enviar '%s': %s", assunto, exc)
        return ResultadoEnvio(
            False, f"O servidor de e-mail não aceitou a mensagem: {str(exc)[:300]}", tuple(alvos),
        )


def _enviar(
    assunto: str, texto: str, html: str,
    destinatarios: list[str] | None = None, *, codigo: str = "",
    anexos: list[tuple[str, bytes, str]] | None = None,
) -> bool:
    """`_enviar_detalhado` reduzido a `bool` — a forma que todo alerta
    transacional usa. A regra mora na outra; não duplicar aqui."""
    return _enviar_detalhado(
        assunto, texto, html, destinatarios, codigo=codigo, anexos=anexos,
    ).ok


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
# Chamados de contratação — estrutura para o novo colaborador (manual)
# ─────────────────────────────────────────────────────────────
#
# Três e-mails INDEPENDENTES, um por tipo de chamado: cada um tem o próprio
# canal em CATALOGO_NOTIFICACOES, então o TI define na Central de Alertas QUEM
# recebe cada tipo (a equipe de máquinas pode não ser a de acessos) e pode
# desligar um sem mexer nos outros. Nenhum endereço mora aqui: sem lista
# customizada vale a lista padrão do sistema (.env), como em todo alerta.
#
# O que o e-mail ALEGA é só o que o sistema sabe: que o pedido foi feito. O
# texto pede à TI que registre o chamado no sistema de atendimento e informe o
# número na vaga — é assim que o `identificador` chega ao Controle TI.

CANAL_CHAMADO_CONTRATACAO = {
    "maquina": "chamado_contratacao_maquina",
    "usuario_email": "chamado_contratacao_usuario_email",
    "sistemas": "chamado_contratacao_sistemas",
}

# Rótulo do tipo no ASSUNTO e no título — fixos aqui (e não lidos do model)
# porque são o texto que a equipe de TI aprende a filtrar na caixa de entrada.
_CHAMADO_TITULO = {
    "maquina": "Configuração e entrega de máquina",
    "usuario_email": "Criação de usuário e e-mail",
    "sistemas": "Orientação e acesso aos sistemas",
}
_CHAMADO_ETIQUETA = {
    "maquina": "Máquina",
    "usuario_email": "Usuário e e-mail",
    "sistemas": "Sistemas",
}
_CHAMADO_TIMEOUT_SMTP = 20  # segundos — ver `_enviar_detalhado`


def situacao_canal_chamado(tipo: str) -> dict:
    """Quem receberia o chamado `tipo` AGORA e se o envio está habilitado.

    Serve à janela de confirmação: o solicitante vê, antes de clicar, que o
    envio está desligado ou sem destinatário — em vez de descobrir depois, na
    linha "Falha no envio". Mesma resolução que `_enviar_detalhado` usa (via
    `_base_efetiva`), para a tela e o envio não divergirem.
    """
    from ProjetoEstoque.models import CanalNotificacao

    codigo = CANAL_CHAMADO_CONTRATACAO[tipo]
    canal = CanalNotificacao.objects.filter(codigo=codigo).first()
    canal_ativo = canal is None or canal.ativo
    destinatarios = _base_efetiva(codigo)

    problema = ""
    if not _alertas_habilitados():
        problema = "Os alertas por e-mail estão desativados nas configurações do sistema."
    elif not canal_ativo:
        problema = "Este tipo de e-mail está desativado em Central de Alertas."
    elif not destinatarios:
        problema = "Nenhum destinatário configurado para este chamado."
    return {"destinatarios": list(destinatarios), "problema": problema, "pronto": not problema}


# O assunto que a TI aprende a filtrar na caixa de entrada: um padrão FIXO por tipo,
# seguido do nome do colaborador (ou, enquanto a vaga não tem ninguém, da vaga).
_CHAMADO_ASSUNTO = {
    "maquina": "Nova solicitação de preparação de equipamento",
    "usuario_email": "Solicitação de criação de acessos corporativos",
    "sistemas": "Solicitação de acessos e integração aos sistemas",
}


def _assunto_chamado(tipo: str, dados: dict, tentativa: int, nome: str = "") -> str:
    # Uma linha só: nome e cargo vêm de formulário, e uma quebra de linha num
    # cabeçalho de e-mail é recusada pelo Django (BadHeaderError).
    quem = " ".join((nome or dados.get("nome_contratado") or "").split())[:80]
    if not quem:
        cargo = " ".join(str(dados.get("cargo", "")).split())[:80]
        quem = f"{cargo} (Vaga nº {dados.get('vaga_id', '?')})"
    sufixo = " (reenvio)" if tentativa > 1 else ""
    return f"{_CHAMADO_ASSUNTO[tipo]} | {quem}{sufixo}"


def _linhas_do_chamado(tipo: str, dados: dict) -> tuple[list, list]:
    """(vaga, específicas) — cada uma `[(rótulo, valor)]`. `valor` é texto puro
    (lista vira linhas com "• "); o escape de HTML é de quem desenha."""
    def sim_nao(v):
        return "Sim" if v else "Não"

    def lista(itens):
        itens = [str(i) for i in (itens or []) if str(i).strip()]
        return "\n".join(f"• {i}" for i in itens) if itens else "—"

    cargo = dados.get("cargo", "")
    funcao = dados.get("funcao", "")
    vaga = [
        ("Identificação da vaga", f"Nº {dados.get('vaga_id', '?')}"),
        ("Cargo / função", cargo + (f" ({funcao})" if funcao and funcao != cargo else "")),
        ("Departamento / setor", dados.get("departamento") or "Não informado"),
        ("Local de trabalho / unidade", dados.get("local") or "Não informado"),
        ("Previsão de início", dados.get("previsao_inicio") or "Não informada"),
        ("Tipo da vaga", dados.get("tipo_vaga") or "—"),
    ]

    if tipo == "maquina":
        espec = [
            ("Computador", dados.get("computador") or "—"),
            ("Telefonia", dados.get("telefonia") or "Não precisa"),
            ("Necessidades de equipamento", dados.get("descricao") or "Não detalhadas."),
        ]
    elif tipo == "usuario_email":
        email = sim_nao(dados.get("criar_email"))
        if dados.get("criar_email") and dados.get("email_sugerido"):
            email += f" — sugestão: {dados['email_sugerido']}"
        espec = [
            ("Colaborador", dados.get("nome_contratado") or "A definir (vaga ainda sem contratado)"),
            ("Criar usuário de rede (login)", sim_nao(dados.get("criar_usuario_rede"))),
            ("Criar e-mail corporativo", email),
            ("Usuário espelho (rede)", dados.get("usuario_espelho") or "Não informado"),
            ("Sistemas / recursos de acesso", lista(dados.get("acessos"))),
            ("Observações", dados.get("observacao") or "—"),
        ]
    else:  # sistemas
        espec = [
            ("Sistemas que o colaborador deverá utilizar", lista(dados.get("sistemas"))),
            ("Licenças de software", lista(dados.get("licencas"))),
            ("Tipo de orientação / suporte", lista(dados.get("suporte"))),
            ("Observações", dados.get("observacao") or "—"),
        ]
    return vaga, espec


def montar_chamado_contratacao(
    tipo: str, dados: dict, *, solicitado_em, enviado_em, tentativa: int = 1,
    referencia: str = "", protocolo: str = "", link: str = "", colaborador: str = "",
    equipamentos: list | None = None, pendencias: list | None = None,
) -> tuple[str, str, str]:
    """`(assunto, texto, html)` do chamado — função pura, sem SMTP nem banco.

    O que a TI RECEBE vem do SNAPSHOT `dados` guardado na hora da solicitação
    (um reenvio diz exatamente o que o solicitante confirmou). O que MUDA depois
    — quem foi contratado, quais equipamentos estão reservados, o que ainda está
    pendente — o chamador lê da vaga NO MOMENTO do envio e passa em
    `colaborador`, `equipamentos` e `pendencias`: a TI precisa do retrato de agora.

    Só entra o que a atividade precisa: sem CPF, telefone, endereço, senha ou
    credencial. O nome do contratado vai nos três tipos — a TI não prepara
    máquina, login nem acesso para "uma pessoa" — mas nada além dele.
    """
    from services import email_layout as layout

    equipamentos = equipamentos or []
    pendencias = pendencias or []
    nome = (colaborador or dados.get("nome_contratado") or "").strip()
    assunto = _assunto_chamado(tipo, dados, tentativa, nome)
    vaga_cod = dados.get("vaga_id", "?")
    cargo = dados.get("cargo", "")
    titulo = _CHAMADO_TITULO_EMAIL[tipo]

    def sim_nao(v):
        return "Sim" if v else "Não"

    quando = timezone.localtime(solicitado_em).strftime("%d/%m/%Y às %H:%M")
    envio = timezone.localtime(enviado_em).strftime("%d/%m/%Y às %H:%M")
    solicitante = dados.get("solicitante") or "—"
    if dados.get("solicitante_email"):
        solicitante += f" ({dados['solicitante_email']})"

    doc = layout.Documento(
        titulo=titulo, etiqueta=_CHAMADO_ETIQUETA_EMAIL[tipo],
        subtitulo=f"{cargo} · Vaga nº {vaga_cod}",
        preheader=f"{titulo} — {nome or cargo} (Vaga nº {vaga_cod})",
    )
    doc.identificacao([
        ("Chamado", protocolo or "—"),
        ("Vaga", f"Nº {vaga_cod} — {cargo}"),
        ("Abertura", quando),
        ("Área responsável", "Tecnologia da Informação (TI)"),
    ])

    funcao = dados.get("funcao", "")
    doc.bloco("Colaborador e vaga", [
        ("Colaborador", nome or "A definir — a vaga ainda não tem contratado"),
        ("Cargo / função", cargo + (f" ({funcao})" if funcao and funcao != cargo else "")),
        ("Departamento / setor", dados.get("departamento") or "Não informado"),
        ("Unidade de trabalho", dados.get("local") or "Não informada"),
        ("Previsão de início", dados.get("previsao_inicio") or "Não informada"),
        ("Tipo da vaga", dados.get("tipo_vaga") or "—"),
    ])

    if tipo == "maquina":
        linhas_eq = [
            [eq.get("equipamento", "—"), eq.get("identificacao") or "—",
             layout.Pilula(eq.get("situacao", "—"), eq.get("tom", "neutro"))]
            for eq in equipamentos
        ]
        if linhas_eq:
            doc.tabela("Equipamentos reservados", ["Equipamento", "Identificação", "Situação da reserva"], linhas_eq)
        else:
            doc.aviso(
                "Nenhum equipamento reservado para esta vaga até o momento. A TI pode reservar do "
                "estoque de Backup; a situação das reservas fica sempre atualizada na plataforma.",
                "aviso", "Equipamentos",
            )
        doc.bloco("Preparação solicitada", [
            ("Computador", dados.get("computador") or "—"),
            ("Telefonia", dados.get("telefonia") or "Não precisa"),
            ("Orientações do RH", dados.get("descricao") or "Não detalhadas."),
        ])
    elif tipo == "usuario_email":
        email = sim_nao(dados.get("criar_email"))
        if dados.get("criar_email") and dados.get("email_sugerido"):
            email += f" — sugestão: {dados['email_sugerido']}"
        doc.bloco("Acessos corporativos solicitados", [
            ("Criar usuário de rede (login)", sim_nao(dados.get("criar_usuario_rede"))),
            ("Criar e-mail corporativo", email),
            ("Usuário espelho (rede)", dados.get("usuario_espelho") or "Não informado"),
            ("Recursos e acessos", layout.lista_com_marcadores(dados.get("acessos"))),
            ("Observações", dados.get("observacao") or None),
        ])
    else:  # sistemas
        doc.bloco("Sistemas e acessos", [
            ("Sistemas que o colaborador deverá utilizar", layout.lista_com_marcadores(dados.get("sistemas"))),
            ("Licenças de software", layout.lista_com_marcadores(dados.get("licencas"))),
            ("Tipo de acesso / orientação necessária", layout.lista_com_marcadores(dados.get("suporte"))),
            ("Observações", dados.get("observacao") or None),
        ])
        if pendencias:
            doc.bloco("Pendências conhecidas", [("Em aberto na vaga", layout.lista_com_marcadores(pendencias))])

    registro = [("Responsável pela solicitação", solicitante), ("Enviado em", envio)]
    if tentativa > 1:
        registro.append(("Tentativa de envio", f"{tentativa}ª (reenvio após falha)"))
    doc.bloco("Registro da solicitação", registro)

    doc.aviso(
        "Esta mensagem é uma solicitação, não um chamado aberto no sistema de atendimento. "
        "Ao registrar o chamado, informe o número na vaga "
        "(Controle TI → Contratações → a vaga → Estrutura e chamados).",
        "info",
    )
    doc.botao("Abrir o chamado na plataforma", link)
    doc.rodape(_rodape_da_conversa(referencia), _rodape_da_conversa_texto(referencia))

    html = doc.html(com_logo=layout.logo_bytes() is not None)
    return assunto, doc.texto(), html


# Título e etiqueta do e-mail (o cabeçalho que a TI lê de relance). O assunto tem o
# próprio padrão (`_CHAMADO_ASSUNTO`).
_CHAMADO_TITULO_EMAIL = {
    "maquina": "Preparação de equipamento",
    "usuario_email": "Criação de acessos corporativos",
    "sistemas": "Acessos e integração aos sistemas",
}
_CHAMADO_ETIQUETA_EMAIL = {
    "maquina": "Solicitação de TI · Equipamento",
    "usuario_email": "Solicitação de TI · Usuário e e-mail",
    "sistemas": "Solicitação de TI · Sistemas",
}


def enviar_chamado_contratacao(
    tipo: str, dados: dict, *, solicitado_em, tentativa: int = 1,
    message_id: str = "", referencia: str = "", protocolo: str = "", link: str = "",
    colaborador: str = "", equipamentos: list | None = None, pendencias: list | None = None,
) -> tuple[str, ResultadoEnvio]:
    """Envia UM chamado e devolve `(assunto, resultado)`. Nunca levanta: erro de
    SMTP, canal desligado e lista vazia voltam em `resultado` — quem chama
    grava o resultado por chamado e segue para o próximo, de modo que a falha de
    um não impede os outros. O assunto volta mesmo na falha, para o histórico
    dizer o que se tentou enviar."""
    from services import email_layout as layout

    enviado_em = timezone.now()
    assunto, texto, html = montar_chamado_contratacao(
        tipo, dados, solicitado_em=solicitado_em, enviado_em=enviado_em, tentativa=tentativa,
        referencia=referencia, protocolo=protocolo, link=link, colaborador=colaborador,
        equipamentos=equipamentos, pendencias=pendencias,
    )
    logo = layout.logo_bytes()
    resultado = _enviar_detalhado(
        assunto, texto, html,
        codigo=CANAL_CHAMADO_CONTRATACAO[tipo], timeout=_CHAMADO_TIMEOUT_SMTP,
        # O Message-ID é nosso e fica guardado: a resposta chega apontando para ele.
        headers={"Message-ID": f"<{message_id}>"} if message_id else None,
        imagens_inline=[(layout.LOGO_CID, logo, "png")] if logo else None,
    )
    return assunto, resultado


# ─────────────────────────────────────────────────────────────
# Alerta: vaga em aberto sem equipamento reservado
# ─────────────────────────────────────────────────────────────

def alerta_vaga_sem_equipamento(reserva) -> ResultadoEnvio:
    """E-mail aos responsáveis pelas vagas e pelo inventário: o equipamento
    reservado para a vaga foi usado em outra necessidade e não há outro do mesmo
    tipo em Backup para repor.

    Diz o que precisa ser dito para agir sem abrir o sistema: QUAL vaga ficou
    sem equipamento, QUAL equipamento foi usado, POR QUEM e QUANDO, e que é
    preciso uma nova reserva. Um e-mail por OCORRÊNCIA (a reserva cedida) — o
    controle de repetição é de `reserva_equipamento_service`. Quem recebe é a
    lista do canal `vaga_sem_equipamento` na Central de Alertas.
    """
    from django.urls import reverse
    from services import email_layout as layout

    vaga = reserva.contratacao
    cargo = vaga.cargo if vaga else (reserva.vaga_rotulo or "Vaga")
    quando = (
        timezone.localtime(reserva.encerrada_em).strftime("%d/%m/%Y às %H:%M")
        if reserva.encerrada_em else "—"
    )
    quem = "—"
    if reserva.encerrada_por_id:
        quem = reserva.encerrada_por.get_full_name() or reserva.encerrada_por.get_username()
    tipo = reserva.item.subtipo.nome if reserva.item.subtipo_id else "equipamento"
    mov = reserva.movimentacao

    doc = layout.Documento(
        titulo="Vaga aberta sem equipamento reservado",
        etiqueta="Atenção · Inventário e contratações",
        subtitulo=f"{cargo} · {reserva.vaga_rotulo}",
        preheader=f"A vaga {reserva.vaga_rotulo} ficou sem equipamento reservado — é necessária uma nova reserva.",
    )
    doc.identificacao([
        ("Vaga", reserva.vaga_rotulo or "—"),
        ("Ocorrência", quando),
        ("Equipamento usado", tipo),
        ("Área responsável", "Tecnologia da Informação (TI)"),
    ])
    doc.aviso(
        "Existe uma vaga aberta sem equipamento vinculado e não há outro equipamento do mesmo tipo "
        "em Backup para substituir a reserva. É necessária uma nova reserva para esta vaga.",
        "aviso", "Ação necessária",
    )
    if vaga is not None:
        doc.bloco("Vaga", [
            ("Cargo", vaga.cargo),
            ("Departamento / setor", vaga.centro_custo.departamento if vaga.centro_custo_id else "Não informado"),
            ("Unidade de trabalho", vaga.localidade.local if vaga.localidade_id else "Não informada"),
            ("Previsão de início", vaga.data_prevista_inicio.strftime("%d/%m/%Y") if vaga.data_prevista_inicio else "Não informada"),
            ("Colaborador", vaga.nome_contratado or "A definir"),
            ("Situação da vaga", vaga.get_status_display()),
        ])
    doc.bloco("O que aconteceu", [
        ("Equipamento que estava reservado", reserva.item_rotulo),
        ("Usado em outra necessidade por", quem),
        ("Data e horário", quando),
        ("Justificativa informada", reserva.motivo or "—"),
        ("Movimentação registrada", f"Nº {mov.pk}" if mov is not None else None),
    ])
    link = layout.link_absoluto(reverse("contratacao_detail", args=[vaga.pk]) + "#reservas") if vaga else ""
    doc.botao("Abrir a vaga e reservar um equipamento", link)

    html = doc.html(com_logo=layout.logo_bytes() is not None)
    logo = layout.logo_bytes()
    return _enviar_detalhado(
        f"Vaga sem equipamento reservado | {reserva.vaga_rotulo}", doc.texto(), html,
        codigo="vaga_sem_equipamento", timeout=_CHAMADO_TIMEOUT_SMTP,
        imagens_inline=[(layout.LOGO_CID, logo, "png")] if logo else None,
    )


# ─────────────────────────────────────────────────────────────
# Conversa do chamado: resposta pela plataforma → e-mail
# ─────────────────────────────────────────────────────────────

def _rodape_da_conversa(referencia: str) -> str:
    """Rodapé que liga a mensagem ao chamado SEM depender do assunto: a
    referência assinada (um vínculo a mais para a resposta voltar ao lugar certo,
    junto dos cabeçalhos In-Reply-To/References) e o aviso de que responder o
    e-mail continua a conversa na plataforma."""
    if not referencia:
        return ""
    from django.utils.html import escape
    return (
        '<p style="margin:14px 0 0;color:#64748b;font-size:11px;line-height:1.5;">'
        'Responda a este e-mail para continuar a conversa — a resposta é registrada no chamado, '
        'no Controle TI. Não altere nem apague a linha abaixo.<br>'
        f'Ref. do chamado: <strong>{escape(referencia)}</strong></p>'
    )


def _rodape_da_conversa_texto(referencia: str) -> str:
    if not referencia:
        return ""
    return (
        "\nResponda a este e-mail para continuar a conversa — a resposta é registrada no chamado, "
        f"no Controle TI. Não altere nem apague a linha abaixo.\nRef. do chamado: {referencia}\n"
    )


_LADO_ROTULO = {"rh": "RH", "ti": "TI", "outro": "Participante"}


def montar_mensagem_chamado(
    *, assunto: str, tipo_rotulo: str, vaga_rotulo: str, autor_nome: str, lado: str,
    texto_corpo: str, quando, nomes_anexos: list[str], referencia: str,
) -> tuple[str, str]:
    """`(texto, html)` de uma resposta da conversa — função pura. O corpo vem de
    usuário e é ESCAPADO; quebra de linha vira `<br>`. O cabeçalho diz quem
    escreveu e de que lado (RH/TI), porque o e-mail sai sempre do endereço do
    sistema e o remetente real não aparece no "De"."""
    from django.utils.html import escape

    quando_txt = timezone.localtime(quando).strftime("%d/%m/%Y às %H:%M")
    lado_txt = _LADO_ROTULO.get(lado, "Participante")
    quem = f"{autor_nome or 'Usuário'} ({lado_txt})"

    corpo = (
        '<p style="margin:0 0 6px;color:#334155;font-size:13px;">'
        f'<strong>{escape(quem)}</strong> escreveu em {escape(quando_txt)}:</p>'
        '<div style="margin:0 0 16px;padding:12px 14px;background:#f8fafc;border:1px solid #e2e8f0;'
        'border-left:3px solid #1e3a8a;border-radius:8px;color:#0f172a;font-size:14px;line-height:1.55;">'
        f'{escape(texto_corpo).replace(chr(10), "<br>")}</div>'
    )
    if nomes_anexos:
        itens = "".join(f"<li>{escape(n)}</li>" for n in nomes_anexos)
        corpo += (
            '<p style="margin:0 0 4px;color:#334155;font-size:12px;"><strong>Anexos:</strong></p>'
            f'<ul style="margin:0 0 12px;padding-left:20px;color:#334155;font-size:12px;">{itens}</ul>'
        )
    corpo += _rodape_da_conversa(referencia)
    html = _base_html(f"Chamado — {tipo_rotulo}", escape(vaga_rotulo), corpo)

    texto = f"{quem} escreveu em {quando_txt}:\n\n{texto_corpo}\n"
    if nomes_anexos:
        texto += "\nAnexos: " + ", ".join(nomes_anexos) + "\n"
    texto += _rodape_da_conversa_texto(referencia)
    return texto, html


def enviar_mensagem_chamado(
    *, assunto: str, texto: str, html: str, para: list[str], cc: list[str],
    message_id: str, em_resposta_a: str, referencias: list[str],
    anexos: list[tuple[str, bytes, str]] | None = None,
) -> ResultadoEnvio:
    """Envia UMA resposta da conversa. Nunca levanta: o resultado diz o que
    aconteceu. `para` vazio é recusado AQUI — `_enviar_detalhado` trataria lista
    vazia como "use a lista padrão do .env" e mandaria a conversa para quem não
    participa dela."""
    if not para:
        return ResultadoEnvio(False, "Nenhum destinatário para esta mensagem (confira os e-mails cadastrados).")
    headers = {"Message-ID": f"<{message_id}>"}
    if em_resposta_a:
        headers["In-Reply-To"] = f"<{em_resposta_a}>"
    if referencias:
        headers["References"] = " ".join(f"<{r}>" for r in referencias[-20:])
    return _enviar_detalhado(
        assunto, texto, html, para, anexos=anexos, timeout=_CHAMADO_TIMEOUT_SMTP,
        headers=headers, cc=cc or None,
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
    equipamentos: agendamento explícito tem prioridade; senão, intervalo
    (Item.data_limite_preventiva → CheckListModelo.intervalo_dias) a partir da
    âncora de contagem (última execução OU última reativação do item — o que
    for mais recente); se não houver, usa o campo data_proxima.
    O campo data_proxima sozinho pode estar desatualizado, por isso não é usado direto.
    Retorna date | None.
    """
    if preventiva.data_agendamento:
        return preventiva.data_agendamento
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
    base = preventiva.base_contagem()
    if intervalo > 0 and base:
        return base + timedelta(days=intervalo)
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
    from ProjetoEstoque.models import Preventiva, StatusItemChoices

    hoje = timezone.localdate()
    vencidas, proximas = [], []
    for p in (
        Preventiva.objects
        # Contagem só corre para item ATIVO — backup/defeito/manutenção/pausado
        # não contam dias nem entram como vencidas nos alertas.
        .filter(pausada=False, equipamento__status=StatusItemChoices.ATIVO)
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
    """
    Assentos de licença no nome de quem NÃO está ativo no cadastro.

    **Era a SEXTA implementação da pergunta "este assento ainda está ativo?"**
    — e discordava das outras cinco em dois pontos, o que fazia o e-mail e
    `/licencas/saude/` apresentarem listas diferentes para a mesma base:

      (a) **casava a devolução por `(usuario, lote)`.** Quando o lote original
          é apagado, `licenca_service.devolver` devolve o assento ao lote mais
          recente da licença, de propósito, para o assento não se perder. Essa
          devolução tem OUTRO `lote_id`, então o `Exists` não a encontrava e o
          assento aparecia como pendente para sempre — cobrança de algo que já
          havia sido resolvido;

      (b) **filtrava só `status=DESLIGADO`.** Colaborador em qualquer outro
          estado não-ativo (afastado, status em branco por importação
          incompleta) ficava fora do aviso, enquanto o painel de saúde — que
          compara contra `"ativo"` — o mostrava.

    Hoje a apuração é `licenca_service.assentos_orfaos()`, a mesma do painel,
    do comando de auditoria e do badge do subnav. O e-mail não pode divergir
    da tela que o TI vai abrir para resolver o que o e-mail cobrou.
    """
    from services import licenca_service as ls

    orfaos = ls.assentos_orfaos()
    if not orfaos:
        logger.info("alerta_licencas: nenhum assento preso em colaborador não ativo.")
        return False

    linhas = []
    for item in orfaos:
        usuario = item["usuario"]
        termino = getattr(usuario, "data_termino", None)
        linhas.append([
            usuario.nome or usuario.email or "—",
            usuario.funcao.nome if getattr(usuario, "funcao", None) else "—",
            usuario.centro_custo.departamento if getattr(usuario, "centro_custo", None) else "—",
            item["licenca"].nome,
            termino.strftime("%d/%m/%Y") if termino else "—",
            # O custo é o argumento: "9 licenças pendentes" não move ninguém,
            # "R$ 752,75/mês saindo por licença de quem já saiu" move.
            _brl_alerta(item["mensal"]) + "/mês",
            _badge("PENDENTE", "#fee2e2", "#b91c1c"),
        ])

    tabela = _tabela_html(
        ["Colaborador", "Função", "Centro de Custo", "Licença", "Desligamento",
         "Custo", "Status"],
        linhas,
    )
    total = len(orfaos)
    hoje = date.today()
    mensal = sum((o["mensal"] for o in orfaos), Decimal("0.00"))
    anual = sum((o["anual"] for o in orfaos), Decimal("0.00"))

    intro = (
        f'<p style="margin:0 0 4px;color:#334155;font-size:14px;">'
        f'<strong style="color:#b91c1c;">{total}</strong> assento(s) de licença '
        f'no nome de colaborador que não está ativo — '
        f'<strong style="color:#b91c1c;">{_brl_alerta(mensal)}/mês</strong> '
        f'({_brl_alerta(anual)}/ano) saindo por licença de quem já saiu.</p>'
        f'<p style="margin:0 0 10px;color:#64748b;font-size:13px;">'
        f'Devolver libera o assento para outra pessoa e estorna o custo do '
        f'centro de custo. Resolva em <strong>Licenças &rsaquo; Saúde</strong>.</p>'
    )

    corpo = _secao("Assentos presos em colaborador não ativo", "🔑", "#b91c1c", intro + tabela)
    html = _base_html(
        f"{total} assento(s) de licença com colaborador não ativo",
        f"{_brl_alerta(mensal)}/mês a recuperar — apurado em {hoje.strftime('%d/%m/%Y')}",
        corpo,
    )
    texto = (
        f"ALERTA — Assentos de licença em colaborador não ativo\n"
        f"Total: {total} | {_brl_alerta(mensal)}/mês | {_brl_alerta(anual)}/ano\n\n"
        + "\n".join(
            f"- {o['usuario'].nome or '—'} | {o['licenca'].nome} | {_brl_alerta(o['mensal'])}/mês"
            for o in orfaos
        )
    )

    return _enviar(
        f"[Controle TI] {total} assento(s) de licença em colaborador não ativo "
        f"— {_brl_alerta(mensal)}/mês",
        texto, html, codigo="licencas_desligados",
    )


def _brl_alerta(valor) -> str:
    """R$ no formato pt-BR para o corpo do e-mail."""
    try:
        numero = Decimal(valor or 0)
    except (TypeError, ValueError, ArithmeticError):
        numero = Decimal("0")
    return "R$ " + f"{numero:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def alerta_licencas_vencendo(dias: int = 60) -> bool:
    """
    Contratos de licença que vencem dentro da janela.

    **Existe porque a renovação chegava pela fatura do fornecedor.** O acervo
    guardava `data_compra` e `periodicidade` e nunca cruzava as duas, então a
    empresa descobria o vencimento DEPOIS de renovar — quando já não havia
    decisão a tomar. São ~R$ 3,5 mi/ano em contrato.

    O e-mail leva, por lote, o valor do CICLO (o que vai ser cobrado) ao lado
    dos assentos ociosos daquele lote. É o par que muda a pergunta de
    "renova?" para "renova quantos?": renovar 225 assentos para usar 168 é a
    forma mais cara de não decidir nada.

    Contrato **sem renovação automática** aparece primeiro e marcado: ali o
    vencimento não gera cobrança, gera interrupção de serviço.

    Apurado por `licenca_service.resumo_renovacoes` — a mesma função da tela e
    do comando de auditoria.
    """
    from services import licenca_service as ls

    resumo = ls.resumo_renovacoes(dias=dias)
    linhas_dados = resumo["linhas"]

    if not linhas_dados:
        logger.info("alerta_licencas_vencendo: nada vencendo em %s dias.", dias)
        return False

    # Manual primeiro, depois por urgência — a ordem do e-mail é a ordem em que
    # vale a pena agir, não a ordem do calendário.
    linhas_dados = sorted(linhas_dados, key=lambda r: (r["automatica"], r["dias"]))

    linhas = []
    for item in linhas_dados:
        if item["vencido"]:
            prazo = f"VENCIDO há {item['dias_abs']}d"
            cor_bg, cor_txt = "#fee2e2", "#b91c1c"
        elif item["dias"] <= 30:
            prazo = f"{item['dias']} dias"
            cor_bg, cor_txt = "#fee2e2", "#b91c1c"
        else:
            prazo = f"{item['dias']} dias"
            cor_bg, cor_txt = "#fffbeb", "#92400e"

        linhas.append([
            item["licenca"].nome,
            f"#{item['lote'].pk}" + (f" · {item['lote'].numero_pedido}" if item["lote"].numero_pedido else ""),
            item["vencimento"].strftime("%d/%m/%Y") + (" (est.)" if item["derivado"] else ""),
            _badge(prazo, cor_bg, cor_txt),
            f"{item['em_uso']}/{item['total']}",
            _brl_alerta(item["custo_ciclo"]),
            (
                _badge("MANUAL", "#fee2e2", "#b91c1c") if not item["automatica"]
                else _badge("automática", "#f1f5f9", "#475569")
            ),
        ])

    tabela = _tabela_html(
        ["Licença", "Lote", "Vencimento", "Prazo", "Em uso", "Valor do ciclo",
         "Renovação"],
        linhas,
    )

    total = len(linhas_dados)
    valor = resumo["total_valor"]
    desperdicio = resumo["total_desperdicio"]
    manuais = resumo["manuais"]
    hoje = date.today()

    intro = (
        f'<p style="margin:0 0 4px;color:#334155;font-size:14px;">'
        f'<strong>{total}</strong> lote(s) de licença vencem nos próximos '
        f'<strong>{dias} dias</strong>, somando '
        f'<strong>{_brl_alerta(valor)}</strong> a renovar.</p>'
    )
    if desperdicio > 0:
        intro += (
            f'<p style="margin:0 0 4px;color:#b45309;font-size:13px;">'
            f'<strong>{_brl_alerta(desperdicio)}</strong> desse total é assento '
            f'comprado e <strong>não usado</strong> — é a economia disponível '
            f'sem negociar preço com ninguém, bastando renovar menos.</p>'
        )
    if manuais:
        intro += (
            f'<p style="margin:0 0 10px;color:#b91c1c;font-size:13px;">'
            f'<strong>{len(manuais)} contrato(s) não renovam automaticamente.</strong> '
            f'Nesses, o vencimento não gera cobrança: a licença simplesmente '
            f'para de funcionar.</p>'
        )
    else:
        intro += (
            '<p style="margin:0 0 10px;color:#64748b;font-size:13px;">'
            'Confira em <strong>Licenças &rsaquo; Renovações</strong> antes de '
            'autorizar a renovação.</p>'
        )

    corpo = _secao("Licenças a renovar", "📅", "#b45309", intro + tabela)
    html = _base_html(
        f"{total} licença(s) a renovar nos próximos {dias} dias",
        f"{_brl_alerta(valor)} em contrato — apurado em {hoje.strftime('%d/%m/%Y')}",
        corpo,
    )
    texto = (
        f"ALERTA — Licencas a renovar em {dias} dias\n"
        f"Total: {total} lote(s) | {_brl_alerta(valor)}\n\n"
        + "\n".join(
            f"- {r['licenca'].nome} | lote #{r['lote'].pk} | "
            f"{r['vencimento'].strftime('%d/%m/%Y')} | "
            f"{r['em_uso']}/{r['total']} em uso | {_brl_alerta(r['custo_ciclo'])}"
            + ("" if r["automatica"] else " | RENOVACAO MANUAL")
            for r in linhas_dados
        )
    )

    return _enviar(
        f"[Controle TI] {total} licença(s) a renovar em {dias} dias "
        f"— {_brl_alerta(valor)}",
        texto, html, codigo="licencas_vencendo",
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


def alerta_m2m_ocorrencias(ocorrencias, destinatarios: list[str] | None = None) -> bool:
    """
    UM e-mail consolidado com as ocorrências NOVAS do módulo Frotas e Conectividade
    (chips e veículos). Disparado por `m2m_monitoramento_service.notificar_ocorrencias`
    (via `transaction.on_commit`) para as ocorrências cuja REGRA manda avisar: ocorrência
    já aberta não gera e-mail de novo.

    Cada item de `ocorrencias` é uma `M2MOcorrencia` (com `chip` e/ou `veiculo`). O texto
    traz, por ocorrência, o veículo, o chip, o tipo, o nível, QUANDO foi detectada, o que
    foi detectado e a AÇÃO NECESSÁRIA — e nunca afirma "fora de cobertura" nem "defeito":
    falha de consulta (rede, credencial, API ou identificador) é diferente de chip
    indisponível, e o monitoramento só diz que o chip está sem sessão quando o mapeamento
    da resposta confirma.

    `destinatarios`: os e-mails da regra (o chamador agrupa por conjunto de destinatários).
    Vazio ou `None` = a lista padrão do sistema. O envio ainda respeita a chave-mestra de
    e-mails e o `ativo` do canal `m2m_ocorrencias`.
    """
    from django.urls import NoReverseMatch, reverse
    from services import email_layout as layout
    from services import m2m_monitoramento_service as m2m

    ocorrencias = list(ocorrencias or [])
    if not ocorrencias:
        return False

    n = len(ocorrencias)
    n_critico = sum(1 for o in ocorrencias if o.nivel == "critico")
    agora = timezone.localtime()
    doc = layout.Documento(
        titulo=f"{n} ocorrência(s) nova(s) em Frotas e Conectividade",
        etiqueta="Atenção · Frotas · Conectividade",
        subtitulo=f"Detecção automática · {agora.strftime('%d/%m/%Y às %H:%M')}",
        preheader=f"{n} ocorrência(s) nova(s) em Frotas e Conectividade" + (f", {n_critico} crítica(s)." if n_critico else "."),
    )
    doc.aviso(
        "Falha de consulta (rede, credencial, API ou identificador) é diferente de chip indisponível: "
        "o monitoramento só afirma que o chip está sem sessão quando o mapeamento da resposta confirma. "
        "O último dado válido de cada chip continua guardado.",
        "info", "Como ler",
    )
    def onde_esta(o) -> str:
        """O veículo da ocorrência; senão o tablet/celular (estoque de TI) onde o chip está; senão `—`."""
        if o.veiculo_id:
            return f"{o.veiculo.codigo} · {o.veiculo.nome}"
        if o.chip_id:
            from ProjetoEstoque.models import FrotaVinculo, rotulo_do_equipamento_ti

            vinculo = (
                FrotaVinculo.objects.filter(chip_id=o.chip_id, fim_em__isnull=True, item__isnull=False)
                .select_related("item").first()
            )
            if vinculo is not None:
                return f"Equipamento {rotulo_do_equipamento_ti(vinculo.item)}"
        return "—"

    doc.tabela(
        "Ocorrências novas",
        ["Veículo / equipamento", "Chip", "Ocorrência", "Nível", "Detectada em", "O que foi detectado", "Ação necessária"],
        [
            [
                onde_esta(o),
                o.chip.nome if o.chip_id else "—",
                o.get_tipo_display(),
                layout.Pilula(o.get_nivel_display(), "perigo" if o.nivel == "critico" else ("aviso" if o.nivel == "atencao" else "info")),
                timezone.localtime(o.detectada_em).strftime("%d/%m/%Y %H:%M"),
                o.descricao,
                m2m.acao_necessaria(o.tipo),
            ]
            for o in ocorrencias
        ],
    )
    try:
        link = layout.link_absoluto(reverse("m2m_alertas"))
    except NoReverseMatch:
        link = ""
    doc.botao("Abrir alertas e ocorrências", link)

    logo = layout.logo_bytes()
    assunto = f"[Controle TI] Frotas e Conectividade: {n} ocorrência(s) nova(s)" + (f" · {n_critico} crítica(s)" if n_critico else "")
    return _enviar_detalhado(
        assunto, doc.texto(), doc.html(com_logo=logo is not None), list(destinatarios) if destinatarios else None,
        codigo="m2m_ocorrencias", timeout=_CHAMADO_TIMEOUT_SMTP,
        imagens_inline=[(layout.LOGO_CID, logo, "png")] if logo else None,
    ).ok


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
# Requisições (Kanban de Solicitações de Compra/Estoque)
# ─────────────────────────────────────────────────────────────

def alerta_requisicao_enviada_aprovacao(requisicao_pk: int) -> bool:
    """Avisa o aprovador (destinatário customizado do canal, definido no painel
    de notificações) que uma requisição foi enviada para aprovação, com a lista
    de itens agrupados nela."""
    from ProjetoEstoque.models import Requisicao

    req = Requisicao.objects.select_related("criado_por").prefetch_related("itens__categoria").filter(pk=requisicao_pk).first()
    if req is None:
        return False
    itens = list(req.itens.all())
    if not itens:
        return False

    numero = req.numero_datasul or f"#{req.pk}"
    solicitante = (
        (req.criado_por.get_full_name() or req.criado_por.username) if req.criado_por else "—"
    )
    linhas = [
        [item.descricao, item.codigo or "—", item.categoria.nome if item.categoria_id else "—", str(item.quantidade)]
        for item in itens
    ]
    intro = (
        f'<p style="margin:0 0 10px;color:#334155;font-size:14px;">'
        f'A requisição <strong>{numero}</strong> ({req.get_tipo_display()}) foi enviada para '
        f'aprovação por <strong>{solicitante}</strong> e está aguardando decisão.</p>'
    )
    secao = _secao(
        "Requisição aguardando aprovação", "📝", "#1d4ed8",
        intro + _tabela_html(["Item", "Código", "Categoria", "Qtd"], linhas),
    )
    html = _base_html(
        f"Requisição {numero} aguardando aprovação",
        f"{len(itens)} item(ns) · {req.get_tipo_display()}",
        secao,
    )
    texto = (
        f"REQUISIÇÃO AGUARDANDO APROVAÇÃO — {numero}\n\n"
        f"Tipo: {req.get_tipo_display()}\nSolicitante: {solicitante}\n\n"
        + "\n".join(f"- {i.descricao} (cód. {i.codigo or '—'}) x{i.quantidade}" for i in itens)
    )
    return _enviar(
        f"[Controle TI] Requisição {numero} aguardando aprovação",
        texto, html, codigo="requisicao_enviada_aprovacao",
    )


def alerta_requisicao_itens_retirados(requisicao_pk: int, item_pks: list[int]) -> bool:
    """Avisa que um ou mais itens de uma requisição foram retirados no
    almoxarifado (compra recebida ou saída de estoque) — dispara tanto para
    retirada individual de um item quanto para a requisição inteira de uma vez."""
    from ProjetoEstoque.models import Requisicao, RequisicaoItem

    req = Requisicao.objects.select_related("criado_por").filter(pk=requisicao_pk).first()
    if req is None:
        return False
    itens = list(RequisicaoItem.objects.select_related("categoria").filter(pk__in=item_pks))
    if not itens:
        return False

    numero = req.numero_datasul or f"#{req.pk}"
    linhas = [
        [item.descricao, item.codigo or "—", item.categoria.nome if item.categoria_id else "—", str(item.quantidade)]
        for item in itens
    ]
    intro = (
        f'<p style="margin:0 0 10px;color:#334155;font-size:14px;">'
        f'{len(itens)} item(ns) da requisição <strong>{numero}</strong> ({req.get_tipo_display()}) '
        f'foram retirados no almoxarifado.</p>'
    )
    secao = _secao(
        "Itens retirados no almoxarifado", "📦", "#15803d",
        intro + _tabela_html(["Item", "Código", "Categoria", "Qtd"], linhas),
    )
    html = _base_html(f"Itens retirados — {numero}", f"{len(itens)} item(ns) · {req.get_tipo_display()}", secao)
    texto = (
        f"ITENS RETIRADOS — {numero}\n\n"
        + "\n".join(f"- {i.descricao} (cód. {i.codigo or '—'}) x{i.quantidade}" for i in itens)
    )

    destinatarios = _base_efetiva("requisicao_itens_retirados")
    solicitante_email = req.criado_por.email if req.criado_por_id else ""
    if solicitante_email and solicitante_email not in destinatarios:
        destinatarios.append(solicitante_email)

    return _enviar(
        f"[Controle TI] Itens retirados — {numero}",
        texto, html, destinatarios=destinatarios, codigo="requisicao_itens_retirados",
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


# ──────────────────────────────────────────────────────────────────────────────
# Termo de responsabilidade assinado — via para o próprio colaborador
# ──────────────────────────────────────────────────────────────────────────────

def enviar_termo_assinado_ao_colaborador(termo) -> tuple[bool, str]:
    """
    Envia a via em PDF do termo recém-assinado para o e-mail do COLABORADOR,
    a pedido dele, no tablet, logo depois de assinar.

    Retorna `(enviado, mensagem)` — mensagem já pronta para a tela, porque aqui
    quem espera é uma pessoa de pé na frente do TI, e "deu certo?" precisa de
    resposta de verdade.

    Duas diferenças deliberadas em relação aos outros envios deste módulo:

    • **Não passa por `_alertas_habilitados()` nem por `CanalNotificacao`.**
      Aquela chave-mestra existe para silenciar os ALERTAS automáticos do
      sistema (estoque crítico, PRTG, preventivas) quando o TI não quer ser
      incomodado. Isto não é alerta: é a via do documento que a pessoa acabou
      de assinar e pediu para receber. Suprimir por causa de uma configuração
      de alerta seria recusar ao signatário a cópia do que ele assinou.

    • **É SÍNCRONO**, contra a regra 3 do CLAUDE.md (e-mail fire-and-forget via
      `transaction.on_commit`). A regra existe para não pendurar a requisição
      de quem está operando o sistema num SMTP lento. Aqui o envio É a ação
      pedida, e responder "enviado" sem saber se foi faria a tela mentir para
      alguém que não tem como conferir depois — ela não tem login no sistema.
      O custo é a espera de um envio; o ganho é a pessoa saber se precisa
      pedir ao TI por outro caminho.
    """
    destinatario = (getattr(termo.colaborador, "email", "") or "").strip()
    if not destinatario:
        return False, (
            "Não há e-mail cadastrado para você no sistema. "
            "Peça a via ao responsável do setor de TI."
        )

    if not termo.pdf:
        return False, "O documento ainda não está disponível. Procure o responsável do TI."

    try:
        termo.pdf.open("rb")
        try:
            conteudo = termo.pdf.read()
        finally:
            termo.pdf.close()
    except (FileNotFoundError, OSError):
        logger.error("Termo %s: PDF não encontrado no storage ao enviar por e-mail.", termo.pk)
        return False, "Não foi possível ler o documento. Procure o responsável do TI."

    from services.termo_pdf_service import nome_arquivo_pdf

    acao = "entrega" if termo.tipo == "entrega" else "devolução"
    assinado_em = timezone.localtime(termo.assinado_em) if termo.assinado_em else timezone.localtime()

    equipamentos = (termo.dados or {}).get("equipamentos") or []
    linhas = [
        [
            e.get("nome") or "—",
            " ".join(p for p in (e.get("marca"), e.get("modelo")) if p) or "—",
            e.get("numero_serie") or "—",
        ]
        for e in equipamentos
    ]

    corpo_html = (
        f'<p style="margin:0 0 14px;color:#334155;font-size:14px;line-height:1.6;">'
        f'Olá, <strong>{termo.colaborador.nome}</strong>.</p>'
        f'<p style="margin:0 0 18px;color:#334155;font-size:14px;line-height:1.6;">'
        f'Segue em anexo a sua via do termo de responsabilidade de <strong>{acao}</strong> '
        f'de equipamento, assinado eletronicamente por você em '
        f'<strong>{assinado_em.strftime("%d/%m/%Y às %H:%M")}</strong>.</p>'
        + _tabela_html(["Equipamento", "Marca / Modelo", "Nº de série"], linhas)
        + f'<p style="margin:18px 0 0;color:#64748b;font-size:12px;line-height:1.6;">'
        f'Código de verificação do documento: '
        f'<strong style="font-family:monospace;color:#1e3a8a;">{termo.codigo_verificacao}</strong><br>'
        f'Guarde este e-mail: ele é a sua via do termo.</p>'
    )

    corpo_texto = (
        f"Olá, {termo.colaborador.nome}.\n\n"
        f"Segue em anexo a sua via do termo de responsabilidade de {acao} de equipamento, "
        f"assinado eletronicamente por você em {assinado_em.strftime('%d/%m/%Y às %H:%M')}.\n\n"
        + "\n".join(f"  - {l[0]} | {l[1]} | Nº série: {l[2]}" for l in linhas)
        + f"\n\nCódigo de verificação: {termo.codigo_verificacao}\n"
        "Guarde este e-mail: ele é a sua via do termo.\n"
    )

    assunto = f"Sua via do termo de {acao} de equipamento — Santa Colomba"

    try:
        msg = EmailMultiAlternatives(
            subject=assunto,
            body=corpo_texto,
            from_email=REMETENTE,
            to=[destinatario],
        )
        msg.attach_alternative(
            _base_html(f"Termo de {acao} assinado", "Sua via do documento", corpo_html),
            "text/html",
        )
        msg.attach(nome_arquivo_pdf(termo), conteudo, "application/pdf")
        # `send()` devolve QUANTAS mensagens saíram. Com `fail_silently=False`
        # um erro vira exceção, mas há caminho em que ele devolve 0 sem
        # levantar nada (backend recusando o destinatário, por exemplo) — e
        # tratar isso como sucesso faria a tela dizer "enviado" para uma via
        # que não saiu, exatamente o relato que não se consegue investigar.
        enviadas = msg.send(fail_silently=False)
    except Exception as exc:
        logger.error("Falha ao enviar termo %s para %s: %s", termo.pk, destinatario, exc)
        return False, "Não foi possível enviar agora. Peça a via ao responsável do setor de TI."

    if not enviadas:
        logger.error(
            "Termo %s: o servidor de e-mail não aceitou a mensagem para %s "
            "(send() devolveu 0, sem erro).", termo.pk, destinatario,
        )
        return False, "O servidor de e-mail não aceitou a mensagem. Procure o responsável do TI."

    logger.info("Termo %s enviado por e-mail para %s", termo.pk, destinatario)
    return True, f"Enviado para {_mascarar_email(destinatario)}."


def _mascarar_email(endereco: str) -> str:
    """Mostra o e-mail parcialmente: a tela do tablet fica à vista de quem
    estiver por perto, e o endereço completo do colaborador não precisa ser
    exposto só para confirmar que o envio saiu."""
    usuario, _, dominio = endereco.partition("@")
    if not dominio:
        return "seu e-mail cadastrado"
    visivel = usuario[:2] if len(usuario) > 3 else usuario[:1]
    return f"{visivel}{'*' * max(3, len(usuario) - len(visivel))}@{dominio}"


# ─────────────────────────────────────────────────────────────
# Computadores parados (Backup / Pausado) — licença a cancelar e a religar
# ─────────────────────────────────────────────────────────────
#
# Por que este alerta existe: quando um notebook ou desktop vai para BACKUP
# ou PAUSADO, o TI cancela licenças que seguem pagando no vazio (a chave
# Office presa à máquina, o assento do colaborador que a usava); quando ele
# volta a ATIVO, as mesmas licenças precisam ser religadas antes de alguém
# sentar na máquina. Hoje essa lista só existe na cabeça de quem movimentou,
# e a conta aparece na fatura do mês seguinte.
#
# Duas cadências de propósito:
#   • SEMANAL — a lista inteira, atualizada, para a conferência de rotina;
#   • NA MOVIMENTAÇÃO — o mesmo e-mail, com a máquina que acabou de mudar em
#     destaque no topo. É a hora em que a decisão sobre a licença é tomada;
#     um resumo semanal sozinho sempre chega depois.
#
# Mandar só a máquina que mudou não serviria: a pergunta do TI nunca é "o que
# mudou?", é "o que está parado agora?" — e essa é a lista inteira.

_SUBTIPOS_COMPUTADOR = ("notebook", "desktop")
# Reservado entra: o computador reservado para uma vaga está parado do mesmo jeito
# (a licença presa a ele segue sendo cobrada até a entrega).
_STATUS_PARADO = ("backup", "reservado", "pausado")


def _itens_computador_qs():
    """Notebooks e desktops do inventário.

    `icontains` em vez de igualdade porque `Subtipo.nome` é texto livre
    cadastrado à mão — é o mesmo critério já usado pelas Licenças Office
    (`views/licencas_office.SUBTIPOS_LICENCA_OFFICE`), e as duas telas têm de
    falar do mesmo parque."""
    from django.db.models import Q as _Q
    from ProjetoEstoque.models import Item

    filtro = _Q()
    for termo in _SUBTIPOS_COMPUTADOR:
        filtro |= _Q(subtipo__nome__icontains=termo)
    return Item.objects.filter(filtro)


def eh_computador_parado(item) -> bool:
    """Se este item entra na lista deste alerta. Usado pelo signal para não
    disparar e-mail a cada mudança de status de switch, toner ou cabo."""
    if item.status not in _STATUS_PARADO:
        return False
    nome = (item.subtipo.nome if item.subtipo_id else "") or ""
    return any(termo in nome.lower() for termo in _SUBTIPOS_COMPUTADOR)


def computadores_parados() -> list[dict]:
    """A lista de hoje: notebooks e desktops em Backup ou Pausado, com desde
    quando estão assim, qual licença Office está presa à máquina e quem a
    usava por último.

    Custo fixo em 3 consultas, independentemente do tamanho do parque (hoje
    são ~150 máquinas): uma para os itens, uma para o histórico de status e
    uma para a última movimentação. Resolver "desde quando" item a item seria
    N+1 num e-mail que existe justamente porque a lista é grande."""
    from ProjetoEstoque.models import (
        ItemStatusHistorico, MovimentacaoItem, TipoMovimentacaoChoices,
        TipoTransferenciaChoices,
    )

    itens = list(
        _itens_computador_qs()
        .filter(status__in=_STATUS_PARADO)
        .select_related("subtipo", "localidade", "centro_custo", "licenca_office")
        .order_by("status", "nome")
    )
    if not itens:
        return []

    ids = [i.pk for i in itens]

    # "Desde quando": a entrada mais recente do histórico que registra a
    # chegada ao status atual. O histórico vem ordenado por data decrescente,
    # então o PRIMEIRO que casar é o que vale.
    desde = {}
    historico = (
        ItemStatusHistorico.objects
        .filter(item_id__in=ids)
        .order_by("item_id", "-alterado_em")
        .values("item_id", "status_novo", "alterado_em")
    )
    por_item = {}
    for linha in historico:
        por_item.setdefault(linha["item_id"], []).append(linha)
    for item in itens:
        for linha in por_item.get(item.pk, []):
            if linha["status_novo"] == item.status:
                desde[item.pk] = linha["alterado_em"]
                break

    # Último colaborador: a devolução grava `usuario` só para auditoria (quem
    # devolveu), então ela NÃO conta como posse — mesma regra do detalhe do
    # equipamento. Sem essa exclusão, toda máquina devolvida apareceria ainda
    # "com" a pessoa que a entregou de volta.
    ultimo_colab = {}
    movs = (
        MovimentacaoItem.objects
        .filter(item_id__in=ids, usuario__isnull=False)
        .exclude(
            tipo_movimentacao=TipoMovimentacaoChoices.TRANSFERENCIA,
            tipo_transferencia=TipoTransferenciaChoices.DEVOLUCAO,
        )
        .select_related("usuario")
        .order_by("item_id", "-created_at")
    )
    for mov in movs:
        ultimo_colab.setdefault(mov.item_id, mov.usuario)

    agora = timezone.now()
    linhas = []
    for item in itens:
        inicio = desde.get(item.pk)
        dias = (agora - inicio).days if inicio else None
        licenca = getattr(item, "licenca_office", None)
        linhas.append({
            "item": item,
            "status": item.status,
            "status_label": item.get_status_display(),
            "desde": inicio,
            "dias": dias,
            "localidade": item.localidade.local if item.localidade_id else "",
            "centro_custo": item.centro_custo.departamento if item.centro_custo_id else "",
            "licenca_office": licenca,
            "ultimo_colaborador": ultimo_colab.get(item.pk),
        })
    return linhas


def _linha_computador_parado(dado: dict) -> list[str]:
    item = dado["item"]
    licenca = dado["licenca_office"]
    colaborador = dado["ultimo_colaborador"]

    if dado["status"] == "backup":
        selo = _badge("BACKUP", "#dbeafe", "#1d4ed8")
    elif dado["status"] == "reservado":
        selo = _badge("RESERVADO", "#ede9fe", "#6d28d9")
    else:
        selo = _badge("PAUSADO", "#fef3c7", "#b45309")

    if dado["dias"] is None:
        tempo = "—"
    elif dado["dias"] == 0:
        tempo = "hoje"
    else:
        tempo = f"{dado['dias']} dia(s)"

    return [
        f'<strong>{item.nome}</strong>',
        item.numero_serie or "—",
        item.subtipo.nome if item.subtipo_id else "—",
        selo,
        tempo,
        dado["localidade"] or dado["centro_custo"] or "—",
        (f'{licenca.produto}<br><span style="color:#94a3b8;font-size:11px;">'
         f'{licenca.chave_produto}</span>') if licenca else "—",
        colaborador.nome if colaborador else "—",
    ]


_CAB_COMPUTADOR_PARADO = [
    "Equipamento", "Nº de série", "Tipo", "Situação", "Parado há",
    "Localidade / Setor", "Licença Office na máquina", "Último colaborador",
]


def _csv_computadores_parados(dados: list[dict]) -> bytes:
    """A lista COMPLETA em anexo.

    O corpo do e-mail mostra as primeiras 50 linhas por tabela (`_tabela_html`)
    e o parque parado passa de 150 máquinas: só no corpo, o TI receberia uma
    lista cortada bem no meio do trabalho dele. Pior, HTML acima de ~102 KB é
    **truncado** por vários clientes de e-mail, e as linhas que somem não
    avisam que sumiram. O anexo leva todas, abre no Excel e serve de lista de
    conferência para cancelar/religar licença.

    `utf-8-sig` porque o Excel do Windows lê CSV sem BOM como ANSI e
    "Localidade" vira "Localidade" na tela de quem for usar a planilha."""
    import csv
    import io as _io

    buffer = _io.StringIO()
    escritor = csv.writer(buffer, delimiter=";")
    escritor.writerow([
        "Equipamento", "Número de série", "Tipo", "Situação", "Parado desde",
        "Dias parado", "Localidade", "Centro de custo", "Licença Office",
        "Chave", "Último colaborador",
    ])
    for d in dados:
        item = d["item"]
        licenca = d["licenca_office"]
        escritor.writerow([
            item.nome,
            item.numero_serie or "",
            item.subtipo.nome if item.subtipo_id else "",
            d["status_label"],
            timezone.localtime(d["desde"]).strftime("%d/%m/%Y") if d["desde"] else "",
            d["dias"] if d["dias"] is not None else "",
            d["localidade"],
            d["centro_custo"],
            licenca.produto if licenca else "",
            licenca.chave_produto if licenca else "",
            d["ultimo_colaborador"].nome if d["ultimo_colaborador"] else "",
        ])
    return buffer.getvalue().encode("utf-8-sig")


def alerta_computadores_parados(*, item_destaque_pk: int | None = None,
                                status_anterior: str = "") -> bool:
    """Envia a lista de notebooks/desktops em Backup ou Pausado.

    `item_destaque_pk` chega quando o disparo veio de uma movimentação: a
    máquina que acabou de mudar vai num bloco próprio no topo, com a
    transição (`status_anterior` → status atual) escrita por extenso, porque
    é ela que motiva a ação de agora. A lista completa continua embaixo.

    Devolve False quando não há nada parado — e aí nenhum e-mail sai, igual
    aos outros alertas deste módulo: caixa de entrada com "0 registros"
    semanal é o que faz o alerta ser ignorado quando passa a ter conteúdo."""
    from ProjetoEstoque.models import Item, StatusItemChoices

    dados = computadores_parados()

    destaque = None
    if item_destaque_pk:
        destaque = (
            Item.objects
            .filter(pk=item_destaque_pk)
            .select_related("subtipo", "localidade", "licenca_office")
            .first()
        )

    if not dados and destaque is None:
        logger.info("alerta_computadores_parados: nenhum notebook/desktop parado.")
        return False

    backup = [d for d in dados if d["status"] == "backup"]
    reservado = [d for d in dados if d["status"] == "reservado"]
    pausado = [d for d in dados if d["status"] == "pausado"]
    com_licenca = [d for d in dados if d["licenca_office"]]

    corpo = ""

    if destaque is not None:
        voltou = destaque.status == StatusItemChoices.ATIVO
        de = dict(StatusItemChoices.choices).get(status_anterior, status_anterior or "—")
        para = destaque.get_status_display()
        cor = "#166534" if voltou else "#b45309"
        fundo = "#dcfce7" if voltou else "#fef3c7"
        acao = (
            "voltou a operar — <strong>religue</strong> as licenças que haviam sido canceladas"
            if voltou else
            "saiu de operação — <strong>cancele</strong> as licenças que ficam pagando no vazio"
        )
        licenca = getattr(destaque, "licenca_office", None)
        detalhe_licenca = (
            f'<p style="margin:6px 0 0;color:#334155;font-size:13px;">'
            f'Licença presa a esta máquina: <strong>{licenca.produto}</strong> '
            f'({licenca.chave_produto}).</p>'
        ) if licenca else (
            '<p style="margin:6px 0 0;color:#64748b;font-size:13px;">'
            'Nenhuma Licença Office vinculada a esta máquina no cadastro.</p>'
        )
        corpo += (
            f'<div style="background:{fundo};border-left:4px solid {cor};'
            f'border-radius:8px;padding:14px 16px;margin-bottom:22px;">'
            f'<p style="margin:0;color:{cor};font-size:11px;font-weight:800;'
            f'text-transform:uppercase;letter-spacing:.08em;">Movimentação agora</p>'
            f'<p style="margin:6px 0 0;color:#0f172a;font-size:15px;font-weight:700;">'
            f'{destaque.nome}'
            f'{f" · série {destaque.numero_serie}" if destaque.numero_serie else ""}</p>'
            f'<p style="margin:6px 0 0;color:#334155;font-size:13px;">'
            f'{de} &rarr; <strong>{para}</strong> — {acao}.</p>'
            f'{detalhe_licenca}'
            f'</div>'
        )

    intro = (
        f'<p style="margin:0 0 4px;color:#334155;font-size:14px;">'
        f'<strong>{len(dados)}</strong> computador(es) fora de operação — '
        f'<strong>{len(backup)}</strong> em backup, <strong>{len(reservado)}</strong> reservado(s) '
        f'para vaga e <strong>{len(pausado)}</strong> pausado(s). '
        f'<strong>{len(com_licenca)}</strong> tem Licença Office vinculada no cadastro.</p>'
        f'<p style="margin:0 0 10px;color:#64748b;font-size:13px;">'
        f'Máquina parada com licença ativa é custo sem uso: cancele o que estiver preso a '
        f'estas máquinas e religue quando elas voltarem a Ativo.</p>'
        + (
            f'<p style="margin:0 0 10px;color:#64748b;font-size:13px;">'
            f'As tabelas abaixo mostram as primeiras 50 de cada situação. '
            f'<strong>A lista completa ({len(dados)} máquinas) vai no CSV em anexo.</strong></p>'
            if len(dados) > 50 else ''
        )
    )
    corpo += _secao("Panorama", "💻", "#1d4ed8", intro)

    if backup:
        corpo += _secao(
            f"Em backup ({len(backup)})", "🗄️", "#1d4ed8",
            _tabela_html(_CAB_COMPUTADOR_PARADO, [_linha_computador_parado(d) for d in backup]),
        )
    if reservado:
        corpo += _secao(
            f"Reservados para vagas ({len(reservado)})", "📌", "#6d28d9",
            _tabela_html(_CAB_COMPUTADOR_PARADO, [_linha_computador_parado(d) for d in reservado]),
        )
    if pausado:
        corpo += _secao(
            f"Pausados ({len(pausado)})", "⏸️", "#b45309",
            _tabela_html(_CAB_COMPUTADOR_PARADO, [_linha_computador_parado(d) for d in pausado]),
        )
    if not dados:
        corpo += _secao(
            "Situação atual", "✅", "#166534",
            _linha_ok("Nenhum notebook ou desktop em backup ou pausado no momento."),
        )

    hoje = date.today()
    titulo = (
        f"{len(dados)} computador(es) parado(s) — licenças a revisar"
        if dados else "Nenhum computador parado"
    )
    html = _base_html(
        titulo,
        f"Notebooks e desktops em Backup/Pausado — apurado em {hoje.strftime('%d/%m/%Y')}",
        corpo,
    )

    texto_linhas = "\n".join(
        f"- {d['item'].nome} | {d['status_label']} | "
        f"{'parado há ' + str(d['dias']) + ' dia(s)' if d['dias'] is not None else 'sem histórico'} | "
        f"licença: {d['licenca_office'].produto if d['licenca_office'] else '—'}"
        for d in dados
    )
    texto = (
        f"CONTROLE TI — Computadores em Backup/Pausado\n"
        f"Total: {len(dados)} ({len(backup)} backup, {len(reservado)} reservado, {len(pausado)} pausado)\n\n"
        + texto_linhas
    )

    assunto_destaque = ""
    if destaque is not None:
        assunto_destaque = f" — {destaque.nome} agora em {destaque.get_status_display()}"

    anexos = []
    if dados:
        anexos.append((
            f"computadores_parados_{hoje.strftime('%Y-%m-%d')}.csv",
            _csv_computadores_parados(dados),
            "text/csv",
        ))

    return _enviar(
        f"[Controle TI] {len(dados)} computador(es) parado(s){assunto_destaque}",
        texto, html, codigo="computadores_parados", anexos=anexos,
    )


# ─────────────────────────────────────────────────────────────
# NinjaOne — licença presa a máquina parada
# ─────────────────────────────────────────────────────────────
#
# A lista e as regras (o que conta como "ainda ocupa", o recado de "já removi") são de
# `services/ninja_licenca_service`; aqui só a mensagem. A tela "Licenças a liberar" lê a MESMA lista.

_CAB_NINJA = ["Equipamento", "Nº de série", "Dispositivo NinjaOne", "Parado há", "Último contato", "Localidade / Setor"]


def _linha_ninja(l) -> list[str]:
    from django.utils.html import escape

    if l.dias is None:
        tempo = "—"
    elif l.dias == 0:
        tempo = "hoje"
    else:
        tempo = f"{l.dias} dia(s)"

    if l.ultimo_contato:
        contato = timezone.localtime(l.ultimo_contato).strftime("%d/%m/%Y")
    else:
        contato = "—"
    if l.online:
        contato += " " + _badge("ONLINE", "#dcfce7", "#166534")
    if l.voltou:
        contato += " " + _badge("VOLTOU APÓS A REMOÇÃO", "#fee2e2", "#b91c1c")

    return [
        f"<strong>{escape(l.item.nome)}</strong>",
        escape(l.item.numero_serie) if l.item.numero_serie else "—",
        escape(l.device.display_name or l.device.hostname or "—"),
        tempo,
        contato,
        escape(l.localidade or l.centro_custo or "—"),
    ]


_COR_STATUS_NINJA = {
    "backup": ("🗄️", "#1d4ed8"), "defeito": ("🛠️", "#b91c1c"),
    "manutencao": ("🔧", "#b45309"), "pausado": ("⏸️", "#b45309"),
}


def _montar_aviso_ninja():
    """(assunto, texto, html, anexos) do aviso, ou None quando nenhuma máquina parada ainda ocupa licença do NinjaOne."""
    from ProjetoEstoque.models import StatusItemChoices
    from services import ninja_licenca_service as nl

    dados = nl.pendentes()
    if not dados:
        return None

    por_status = {s: [l for l in dados if l.status == s] for s in nl.STATUS_SEM_USO}
    online = [l for l in dados if l.online]
    voltaram = [l for l in dados if l.voltou]
    rotulos = dict(StatusItemChoices.choices)
    resumo = ", ".join(f"<strong>{len(por_status[s])}</strong> {rotulos[s].lower()}" for s in nl.STATUS_SEM_USO if por_status[s])
    lido_em = max((l.device.last_sync for l in dados if l.device.last_sync), default=None)

    intro = (
        f'<p style="margin:0 0 4px;color:#334155;font-size:14px;">'
        f'<strong>{len(dados)}</strong> máquina(s) fora de operação ainda têm dispositivo no NinjaOne — {resumo}. '
        f'<strong>{len(online)}</strong> aparece(m) online (o agente ainda fala com o NinjaOne).</p>'
        f'<p style="margin:0 0 10px;color:#64748b;font-size:13px;">'
        f'Cada dispositivo é uma licença paga. Remova estes dispositivos no NinjaOne para liberar a licença e, depois, '
        f'marque-os como removidos no Controle TI (NinjaOne › Licenças a liberar) — senão continuam nesta lista.</p>'
        + (f'<p style="margin:0 0 10px;color:#b91c1c;font-size:13px;"><strong>{len(voltaram)}</strong> já tinha(m) sido '
           f'marcada(s) como removida(s) e voltou(aram) a falar com o NinjaOne: a remoção não pegou.</p>' if voltaram else "")
        + (f'<p style="margin:0 0 10px;color:#64748b;font-size:13px;">As tabelas mostram as primeiras 50 de cada situação. '
           f'<strong>A lista completa ({len(dados)} máquinas) vai no CSV em anexo.</strong></p>' if len(dados) > 50 else "")
        + (f'<p style="margin:0;color:#94a3b8;font-size:12px;">Contato e situação online são os da última importação do '
           f'NinjaOne ({timezone.localtime(lido_em).strftime("%d/%m/%Y %H:%M")}).</p>' if lido_em else "")
    )
    corpo = _secao("Panorama", "🔌", "#1d4ed8", intro)
    for status in nl.STATUS_SEM_USO:
        if por_status[status]:
            icone, cor = _COR_STATUS_NINJA[status]
            corpo += _secao(
                f"{rotulos[status]} ({len(por_status[status])})", icone, cor,
                _tabela_html(_CAB_NINJA, [_linha_ninja(l) for l in por_status[status]]),
            )

    hoje = date.today()
    html = _base_html(
        f"{len(dados)} máquina(s) parada(s) com licença NinjaOne",
        f"Backup, defeito, manutenção e pausado — apurado em {hoje.strftime('%d/%m/%Y')}",
        corpo,
    )
    texto = (
        f"CONTROLE TI — Licenças do NinjaOne em máquinas paradas\n"
        f"Total: {len(dados)} ({', '.join(f'{len(por_status[s])} {rotulos[s].lower()}' for s in nl.STATUS_SEM_USO if por_status[s])})\n\n"
        + "\n".join(
            f"- {l.item.nome} | {l.status_label} | "
            f"{'parado há ' + str(l.dias) + ' dia(s)' if l.dias is not None else 'sem histórico'} | NinjaOne: {l.device.display_name}"
            for l in dados
        )
    )
    anexos = [(f"ninja_licencas_a_remover_{hoje.strftime('%Y-%m-%d')}.csv", nl.csv_da_lista(dados), "text/csv")]
    return f"[Controle TI] {len(dados)} máquina(s) parada(s) ainda com licença NinjaOne", texto, html, anexos


def enviar_ninja_licencas_detalhado() -> "ResultadoEnvio | None":
    """Envia o aviso e diz o que aconteceu (para a tela contar ao TI: enviado, canal desligado, sem destinatário…).
    `None` quando não há nada a avisar — nenhum e-mail sai, como nos outros alertas: caixa semanal com "0 máquinas"
    é o que faz o aviso ser ignorado no dia em que passa a ter conteúdo."""
    montado = _montar_aviso_ninja()
    if montado is None:
        logger.info("alerta_ninja_licencas: nenhuma máquina parada com licença do NinjaOne a remover.")
        return None
    assunto, texto, html, anexos = montado
    return _enviar_detalhado(assunto, texto, html, codigo="ninja_licencas_liberar", anexos=anexos)


def alerta_ninja_licencas() -> bool:
    """Forma `bool` para o `enviar_alertas`/agendador. False também quando não há nada a avisar."""
    resultado = enviar_ninja_licencas_detalhado()
    return bool(resultado and resultado.ok)
