"""
Conversa dos chamados — RH e TI no mesmo lugar, e o e-mail continua valendo.

Cada chamado de estrutura (`ContratacaoChamado`) vira uma conversa: o e-mail de
abertura é a primeira mensagem e tudo o que vem depois — resposta pela
plataforma, resposta por e-mail capturada da caixa — entra como `ChamadoMensagem`
no mesmo fio. Este módulo é a regra de ESCRITA e de LEITURA da conversa; a
captura de e-mail recebido está em `chamado_email_entrada_service`.

Quatro decisões que a tela não mostra e que sustentam o resto:

  • O VÍNCULO NÃO DEPENDE DO ASSUNTO. Toda mensagem enviada leva um `Message-ID`
    NOSSO, gerado antes do envio e guardado. A resposta chega citando-o em
    `In-Reply-To`/`References` — é o vínculo principal. A referência assinada
    (`CH-<id>-<assinatura>`, no rodapé e no assunto das respostas) e o id de
    conversa do provedor são sinais complementares; um assunto digitado à mão
    não prova nada, e a assinatura HMAC impede de forjar o vínculo com outro chamado.

  • "ENVIADO" É O QUE O SISTEMA SABE. `enviada` = o servidor SMTP aceitou. Não
    há "entregue" nem "lido": a caixa só nos dá prova de NÃO entrega (aviso de
    devolução), e essa fica registrada na mensagem (`entrega_recusada`).

  • A SITUAÇÃO DO ATENDIMENTO É UM EIXO PRÓPRIO. Independe do status da vaga e
    do estado do envio do e-mail: concluir um chamado não preenche a vaga,
    preencher a vaga não conclui chamado. Só a TI muda a situação. A única
    mudança automática — resposta do RH num chamado que esperava por ele volta
    à fila da TI — é marcada como automática e tem o autor da mensagem como
    responsável. Toda mudança entra em `ChamadoStatusLog`.

  • REENVIO NÃO DUPLICA. A mensagem guarda o Message-ID e a tentativa é
    disputada por UPDATE condicional (como no chamado): dois cliques, um vence; o
    que o servidor já aceitou nunca é reenviado.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from dataclasses import dataclass
from email.utils import make_msgid

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, F
from django.utils import timezone

from ProjetoEstoque.models import (
    AtendimentoChamadoChoices as Atend,
    ChamadoMensagem as Msg,
    ChamadoStatusLog,
    ContratacaoChamado,
    StatusChamadoContratacao as StatusChamado,
    TipoChamadoContratacao as Tipo,
)
from services import acesso_restrito_service as ar
from services import chamado_anexo_service as anexos_svc
from services.contratacao_service import registrar_evento

logger = logging.getLogger(__name__)

LIMITE_TEXTO = 8000

# Grupos que a lista e os selos usam. "Pendente" é o que ainda não começou;
# "Em atendimento" inclui as esperas (de quem for), porque o chamado está vivo;
# "Concluído" inclui o cancelado: ambos saíram da fila.
GRUPOS_DE_ATENDIMENTO = {
    "pendentes": (Atend.AGUARDANDO_ATENDIMENTO, Atend.ABERTO),
    "em_atendimento": (Atend.EM_ANDAMENTO, Atend.AGUARDANDO_RH, Atend.AGUARDANDO_INFORMACOES),
    "concluidos": (Atend.CONCLUIDO, Atend.CANCELADO),
}
ROTULO_DOS_GRUPOS = {
    "pendentes": "Pendentes", "em_atendimento": "Em atendimento", "concluidos": "Concluídos",
}
ENCERRADOS = GRUPOS_DE_ATENDIMENTO["concluidos"]

# Quando o RH responde, estas situações esperavam por ele (ou estavam fechadas):
# a mensagem devolve o chamado à fila da TI.
_VOLTAM_PARA_A_FILA = (Atend.AGUARDANDO_RH, Atend.AGUARDANDO_INFORMACOES, Atend.CONCLUIDO)

_TOM_ATENDIMENTO = {
    Atend.AGUARDANDO_ATENDIMENTO: "aviso", Atend.ABERTO: "primary", Atend.EM_ANDAMENTO: "primary",
    Atend.AGUARDANDO_RH: "aviso", Atend.AGUARDANDO_INFORMACOES: "aviso",
    Atend.CONCLUIDO: "ok", Atend.CANCELADO: "neutro",
}


class ConversaRecusada(Exception):
    """A regra de negócio disse não. A mensagem já vem em texto de tela."""


def grupo_do_atendimento(atendimento: str) -> str:
    for grupo, valores in GRUPOS_DE_ATENDIMENTO.items():
        if atendimento in valores:
            return grupo
    return "pendentes"


def tom_do_atendimento(atendimento: str) -> str:
    return _TOM_ATENDIMENTO.get(atendimento, "neutro")


# ─────────────────────────────────────────────────────────────
# Identificadores: Message-ID e referência assinada
# ─────────────────────────────────────────────────────────────

_RE_REFERENCIA = re.compile(r"\bCH-(\d{1,9})-([0-9a-fA-F]{8})\b")
_RE_ID_ENTRE_ANGULOS = re.compile(r"<([^<>\s]+)>")


def _assinatura(chamado_pk: int) -> str:
    chave = settings.SECRET_KEY.encode("utf-8")
    return hmac.new(chave, f"chamado:{chamado_pk}".encode("utf-8"), hashlib.sha256).hexdigest()[:8]


def referencia_do_chamado(chamado_pk: int) -> str:
    """`CH-<id>-<assinatura>`. Vai no rodapé de todo e-mail do chamado e no
    assunto das respostas. A assinatura (HMAC da chave do projeto) faz uma
    referência inventada não valer: só o sistema sabe gerar a de cada chamado."""
    return f"CH-{chamado_pk}-{_assinatura(chamado_pk)}"


def chamados_da_referencia(texto: str) -> set[int]:
    """Ids de chamado cujas referências ASSINADAS aparecem em `texto`. Referência
    com assinatura errada é ignorada, como se não estivesse lá."""
    achados = set()
    for numero, assinatura in _RE_REFERENCIA.findall(texto or ""):
        if hmac.compare_digest(assinatura.lower(), _assinatura(int(numero))):
            achados.add(int(numero))
    return achados


def normalizar_id(valor: str) -> str:
    """Forma única de comparar Message-IDs: sem `<>`, sem espaço, minúsculo."""
    return (valor or "").strip().strip("<>").strip().lower()


def ids_de_cabecalho(valor: str) -> list[str]:
    """Todos os Message-IDs de um cabeçalho `References`/`In-Reply-To`."""
    achados = _RE_ID_ENTRE_ANGULOS.findall(valor or "")
    if not achados:
        achados = (valor or "").split()
    return [i for i in (normalizar_id(a) for a in achados) if i]


def novo_message_id() -> str:
    dominio = (settings.EMAIL_HOST_USER or "").split("@")[-1].strip() or "controle-ti.local"
    return normalizar_id(make_msgid(domain=dominio))


# ─────────────────────────────────────────────────────────────
# Quem é quem
# ─────────────────────────────────────────────────────────────

def eh_ti(user) -> bool:
    """Lado da TI: quem acompanha chamados e atende contratações (interno)."""
    return ar.pode_acompanhar_chamados(user) and ar.pode_gerenciar_contratacao(user)


def lado_do_usuario(user) -> str:
    return Msg.Lado.TI if eh_ti(user) else Msg.Lado.RH


def _exigir_acompanhar(user) -> None:
    if not ar.pode_acompanhar_chamados(user):
        raise PermissionDenied("Seu acesso não permite acompanhar os chamados de uma vaga.")


def _exigir_ti(user) -> None:
    if not eh_ti(user):
        raise PermissionDenied("Só a TI atualiza a situação dos chamados.")


def _nome_do_usuario(user) -> str:
    return (user.get_full_name() or user.get_username() or "").strip()


def enderecos_da_ti(chamado) -> list[str]:
    """Quem recebe do lado da TI: os destinatários do e-mail de abertura (o que
    foi efetivamente usado) ou, se ele ainda não saiu, o que a Central de Alertas
    define AGORA para o tipo."""
    base = list(chamado.lista_destinatarios)
    if not base:
        from services import email_alertas
        base = list(email_alertas._base_efetiva(email_alertas.CANAL_CHAMADO_CONTRATACAO[chamado.tipo]))
    return _unicos(base)


def enderecos_do_rh(chamado) -> list[str]:
    """Do lado do RH: o solicitante e quem já escreveu pelo lado do RH."""
    enderecos = []
    if chamado.criado_por_id and chamado.criado_por.email:
        enderecos.append(chamado.criado_por.email)
    enderecos += list(
        chamado.mensagens.filter(lado=Msg.Lado.RH).exclude(remetente_email="")
        .values_list("remetente_email", flat=True)
    )
    return _unicos(enderecos)


def _unicos(enderecos) -> list[str]:
    """Sem repetição (sem diferenciar maiúsculas), sem o remetente do sistema —
    ele É a caixa de onde a mensagem sai — e sem vazios."""
    sistema = (settings.EMAIL_HOST_USER or "").strip().lower()
    vistos, saida = set(), []
    for e in enderecos:
        e = (e or "").strip()
        if e and e.lower() not in vistos and e.lower() != sistema:
            vistos.add(e.lower())
            saida.append(e)
    return saida


def destinatarios_da_mensagem(chamado, lado: str, autor_email: str) -> tuple[list[str], list[str], str]:
    """`(para, cc, aviso)`. RH escreve para a TI; a TI escreve para o RH, com a
    própria equipe em cópia. O autor nunca recebe a própria mensagem. `aviso`
    explica, em texto de tela, por que a lista ficou vazia."""
    autor = (autor_email or "").strip().lower()
    ti = [e for e in enderecos_da_ti(chamado) if e.lower() != autor]
    rh = [e for e in enderecos_do_rh(chamado) if e.lower() != autor]
    if lado == Msg.Lado.TI:
        aviso = "" if rh else "O solicitante não tem e-mail cadastrado — não há para quem enviar."
        return rh, ti, aviso
    aviso = "" if ti else "Nenhum destinatário da TI configurado (Central de Alertas → Configurar notificações)."
    return ti, rh, aviso


# ─────────────────────────────────────────────────────────────
# Abertura: a primeira mensagem da conversa
# ─────────────────────────────────────────────────────────────

def garantir_mensagem_abertura(chamado, user) -> Msg:
    """A mensagem de abertura do chamado (cria se não existir). É ela que guarda
    o `Message-ID` do e-mail de abertura — o ponto onde as respostas se prendem
    — e a mesma é reutilizada num reenvio: o Message-ID não muda."""
    msg = chamado.mensagens.filter(tipo=Msg.Tipo.ABERTURA).first()
    if msg is not None:
        return msg
    return Msg.objects.create(
        chamado=chamado, tipo=Msg.Tipo.ABERTURA, direcao=Msg.Direcao.SAIDA,
        origem=Msg.Origem.PLATAFORMA, lado=Msg.Lado.RH, estado=Msg.Estado.PENDENTE,
        autor=user, remetente_nome=_nome_do_usuario(user) if user else "",
        remetente_email=(user.email if user else "") or "",
        assunto=chamado.assunto, message_id=novo_message_id(),
        criado_por=user, atualizado_por=user,
    )


def registrar_resultado_abertura(chamado, mensagem, resultado, assunto, user, numero) -> None:
    """Grava o que aconteceu com o e-mail de abertura na mensagem dele. Chamado
    pelo fluxo de envio do chamado, dentro da mesma transação do resultado."""
    agora = timezone.now()
    mensagem.estado = Msg.Estado.ENVIADA if resultado.ok else Msg.Estado.FALHA
    mensagem.assunto = assunto
    mensagem.destinatarios = ", ".join(resultado.destinatarios)
    mensagem.tentativas = numero
    mensagem.erro = "" if resultado.ok else resultado.motivo
    if resultado.ok:
        mensagem.enviado_em = agora
    mensagem.atualizado_por = user
    mensagem.save(update_fields=[
        "estado", "assunto", "destinatarios", "tentativas", "erro", "enviado_em",
        "atualizado_por", "updated_at",
    ])
    if resultado.ok:
        ContratacaoChamado.objects.filter(pk=chamado.pk).update(ultima_atividade_em=agora)


# ─────────────────────────────────────────────────────────────
# Responder pela plataforma
# ─────────────────────────────────────────────────────────────

def _assunto_da_resposta(chamado) -> str:
    base = (chamado.assunto or "Chamado").strip()
    base = re.sub(r"\s*\(reenvio\)\s*$", "", base)
    if not base.lower().startswith("re:"):
        base = f"Re: {base}"
    ref = referencia_do_chamado(chamado.pk)
    return base if ref in base else f"{base} [{ref}]"


def responder(*, chamado_pk: int, user, texto: str, arquivos=()) -> Msg:
    """Registra uma mensagem na conversa e a envia por e-mail.

    A mensagem é gravada ANTES do envio (estado pendente) e o SMTP roda depois
    do commit, como nos chamados: servidor de e-mail lento não segura lock do
    banco, e uma falha vira `falha` + motivo na própria mensagem — a conversa
    mostra o que não saiu, em vez de fingir que saiu.
    """
    _exigir_acompanhar(user)
    texto = (texto or "").strip()
    arquivos = [a for a in (arquivos or []) if a]
    if not texto and not arquivos:
        raise ConversaRecusada("Escreva uma mensagem ou anexe um arquivo.")
    if len(texto) > LIMITE_TEXTO:
        raise ConversaRecusada(f"A mensagem passa de {LIMITE_TEXTO} caracteres.")
    try:
        itens = anexos_svc.validar_envio(arquivos)
    except anexos_svc.AnexoRecusado as exc:
        raise ConversaRecusada(str(exc)) from exc

    lado = lado_do_usuario(user)
    criados = []
    try:
        with transaction.atomic():
            chamado = (
                ContratacaoChamado.objects.select_for_update()
                .select_related("contratacao", "criado_por").get(pk=chamado_pk)
            )
            if chamado.status != StatusChamado.ENVIADO:
                raise ConversaRecusada(
                    "O e-mail de abertura deste chamado ainda não foi enviado. Reenvie-o "
                    "(“Tentar novamente”) antes de conversar."
                )
            anteriores = list(
                chamado.mensagens.exclude(message_id="").values_list("message_id", flat=True)
            )
            msg = Msg.objects.create(
                chamado=chamado, tipo=Msg.Tipo.MENSAGEM, direcao=Msg.Direcao.SAIDA,
                origem=Msg.Origem.PLATAFORMA, lado=lado, estado=Msg.Estado.PENDENTE,
                autor=user, remetente_nome=_nome_do_usuario(user), remetente_email=user.email or "",
                assunto=_assunto_da_resposta(chamado), corpo=texto,
                message_id=novo_message_id(),
                in_reply_to=anteriores[-1] if anteriores else "",
                referencias=" ".join(anteriores[-20:]),
                criado_por=user, atualizado_por=user,
            )
            criados = anexos_svc.salvar(msg, itens, user)
            _efeitos_de_nova_mensagem(chamado, msg, user)
            registrar_evento(
                chamado.contratacao, "chamado",
                f"Mensagem de {lado.upper()} no chamado “{Tipo(chamado.tipo).label}”.", user,
            )
    except Exception:
        anexos_svc.limpar_arquivos(criados)
        raise

    _tentar_envio_mensagem(msg.pk, user)
    msg.refresh_from_db()
    return msg


def reenviar_mensagem(*, chamado_pk: int, mensagem_pk: int, user) -> Msg:
    """Nova tentativa de uma mensagem que FALHOU (ou ficou pendente além do
    prazo). O que o servidor já aceitou é recusado — reenviar duplicaria. A posse
    da tentativa é um UPDATE condicional: dois cliques, um vence."""
    _exigir_acompanhar(user)
    with transaction.atomic():
        msg = Msg.objects.select_for_update().get(pk=mensagem_pk, chamado_id=chamado_pk)
        if msg.tipo == Msg.Tipo.ABERTURA:
            raise ConversaRecusada("O e-mail de abertura é reenviado pelo próprio chamado (“Tentar novamente”).")
        if not msg.reenviavel:
            if msg.estado == Msg.Estado.ENVIADA:
                raise ConversaRecusada("Esta mensagem já foi enviada — reenviar a duplicaria.")
            raise ConversaRecusada("Esta mensagem está sendo enviada agora. Aguarde o resultado.")
        agora = timezone.now()
        alvo = Msg.objects.filter(pk=msg.pk)
        if msg.estado == Msg.Estado.FALHA:
            pegou = alvo.filter(estado=Msg.Estado.FALHA).update(
                estado=Msg.Estado.PENDENTE, updated_at=agora, atualizado_por=user,
            )
        else:
            pegou = alvo.filter(estado=Msg.Estado.PENDENTE, updated_at=msg.updated_at).update(
                updated_at=agora, atualizado_por=user,
            )
        if not pegou:
            raise ConversaRecusada("Esta mensagem já está sendo reenviada. Aguarde o resultado.")
    _tentar_envio_mensagem(mensagem_pk, user)
    msg.refresh_from_db()
    return msg


def _tentar_envio_mensagem(mensagem_pk: int, user) -> None:
    """Uma tentativa de envio + o registro dela. Nunca propaga: qualquer erro
    (SMTP, anexo que sumiu do disco, defeito ao montar) vira `falha` + motivo."""
    from services import email_alertas

    msg = Msg.objects.select_related("chamado", "chamado__contratacao", "chamado__criado_por").get(pk=mensagem_pk)
    chamado = msg.chamado
    numero = msg.tentativas + 1
    try:
        para, cc, aviso = destinatarios_da_mensagem(chamado, msg.lado, msg.remetente_email)
        anexos = [anexos_svc.ler_para_envio(a) for a in msg.anexos.filter(bloqueado=False)]
        referencia = referencia_do_chamado(chamado.pk)
        vaga = chamado.contratacao
        texto, html = email_alertas.montar_mensagem_chamado(
            assunto=msg.assunto, tipo_rotulo=Tipo(chamado.tipo).label,
            vaga_rotulo=f"{vaga.cargo} · Vaga nº {vaga.pk}",
            autor_nome=msg.remetente_nome, lado=msg.lado, texto_corpo=msg.corpo,
            quando=msg.created_at, nomes_anexos=[a[0] for a in anexos], referencia=referencia,
        )
        if not para:
            resultado = email_alertas.ResultadoEnvio(False, aviso)
        else:
            resultado = email_alertas.enviar_mensagem_chamado(
                assunto=msg.assunto, texto=texto, html=html, para=para, cc=cc,
                message_id=msg.message_id, em_resposta_a=msg.in_reply_to,
                referencias=msg.referencias.split(),
                anexos=anexos,
            )
    except Exception as exc:  # noqa: BLE001 — a regra é não deixar escapar
        logger.exception("mensagem %s do chamado %s: erro ao preparar/enviar", mensagem_pk, chamado.pk)
        resultado = email_alertas.ResultadoEnvio(False, f"Erro ao preparar a mensagem: {str(exc)[:200]}")
        para, cc = [], []

    with transaction.atomic():
        msg.tentativas = numero
        msg.estado = Msg.Estado.ENVIADA if resultado.ok else Msg.Estado.FALHA
        msg.erro = "" if resultado.ok else resultado.motivo
        msg.destinatarios = ", ".join(para) + (f" | Cc: {', '.join(cc)}" if cc else "")
        if resultado.ok:
            msg.enviado_em = timezone.now()
        msg.atualizado_por = user
        msg.save(update_fields=[
            "tentativas", "estado", "erro", "destinatarios", "enviado_em", "atualizado_por", "updated_at",
        ])
        if not resultado.ok:
            registrar_evento(
                chamado.contratacao, "chamado",
                f"Falha no envio de mensagem do chamado “{Tipo(chamado.tipo).label}” (tentativa {numero}).",
                user,
            )


# ─────────────────────────────────────────────────────────────
# Efeitos de uma mensagem nova (enviada OU recebida)
# ─────────────────────────────────────────────────────────────

def _efeitos_de_nova_mensagem(chamado, msg, user) -> None:
    """O que toda mensagem nova faz no chamado: marca a atividade e, se é do RH
    e o chamado esperava por ele (ou estava concluído), devolve à fila da TI.
    Roda na transação de quem grava a mensagem."""
    ContratacaoChamado.objects.filter(pk=chamado.pk).update(ultima_atividade_em=timezone.now())
    chamado.ultima_atividade_em = timezone.now()
    if msg.lado == Msg.Lado.RH and chamado.atendimento in _VOLTAM_PARA_A_FILA:
        aplicar_mudanca(
            chamado, Atend.AGUARDANDO_ATENDIMENTO, user=user, automatico=True,
            motivo="Resposta do RH recebida.",
        )


def registrar_mensagem_recebida(
    *, chamado, lado: str, autor, remetente_nome: str, remetente_email: str, destinatarios: str,
    assunto: str, corpo: str, message_id: str, in_reply_to: str, referencias: str,
    enviado_em, recebido_em, conversation_id: str = "",
) -> Msg:
    """Grava uma mensagem CAPTURADA do e-mail. Só escreve — a decisão de aceitar
    ou de pôr em quarentena já foi tomada por quem chama. Roda na transação dele."""
    msg = Msg.objects.create(
        chamado=chamado, tipo=Msg.Tipo.MENSAGEM, direcao=Msg.Direcao.ENTRADA,
        origem=Msg.Origem.EMAIL, lado=lado, estado=Msg.Estado.RECEBIDA, autor=autor,
        remetente_nome=remetente_nome[:150], remetente_email=remetente_email[:254],
        destinatarios=destinatarios, assunto=assunto[:255], corpo=corpo,
        message_id=message_id[:255], in_reply_to=in_reply_to[:255], referencias=referencias,
        enviado_em=enviado_em, recebido_em=recebido_em,
        criado_por=autor, atualizado_por=autor,
    )
    if conversation_id and not chamado.conversation_id:
        chamado.conversation_id = conversation_id[:255]
        ContratacaoChamado.objects.filter(pk=chamado.pk).update(conversation_id=chamado.conversation_id)
    _efeitos_de_nova_mensagem(chamado, msg, autor)
    registrar_evento(
        chamado.contratacao, "chamado",
        f"Resposta de {lado.upper()} recebida por e-mail no chamado “{Tipo(chamado.tipo).label}”.", autor,
    )
    return msg


# ─────────────────────────────────────────────────────────────
# Situação do atendimento
# ─────────────────────────────────────────────────────────────

def aplicar_mudanca(chamado, para: str, *, user, motivo: str = "", automatico: bool = False) -> bool:
    """Muda a situação e registra no histórico. Roda na transação de quem chama,
    com o chamado já travado. Devolve `False` (sem gravar nada) se já estava assim."""
    if chamado.atendimento == para:
        return False
    de = chamado.atendimento
    chamado.atendimento = para
    chamado.ultima_atividade_em = timezone.now()
    chamado.atualizado_por = user
    chamado.save(update_fields=["atendimento", "ultima_atividade_em", "atualizado_por", "updated_at"])
    ChamadoStatusLog.objects.create(
        chamado=chamado, de=de, para=para, motivo=(motivo or "")[:255], automatico=automatico,
        criado_por=user, atualizado_por=user,
    )
    registrar_evento(
        chamado.contratacao, "chamado",
        f"Chamado “{Tipo(chamado.tipo).label}”: {Atend(de).label} → {Atend(para).label}"
        + (" (automático)" if automatico else "") + ".",
        user,
    )
    return True


def atualizar_atendimento(*, chamado_pk: int, para: str, motivo: str, user, responsavel_id=None) -> ContratacaoChamado:
    """A TI atualiza a situação do chamado e/ou o responsável. Só a TI. Não toca
    na vaga: o status da vaga e o do chamado são independentes."""
    _exigir_ti(user)
    if para not in Atend.values:
        raise ConversaRecusada("Situação inválida.")
    motivo = (motivo or "").strip()
    if len(motivo) > 255:
        raise ConversaRecusada("O comentário da mudança tem no máximo 255 caracteres.")

    with transaction.atomic():
        chamado = (
            ContratacaoChamado.objects.select_for_update()
            .select_related("contratacao").get(pk=chamado_pk)
        )
        mudou_situacao = aplicar_mudanca(chamado, para, user=user, motivo=motivo)

        mudou_resp = False
        if responsavel_id is not None:
            novo = None
            if responsavel_id != "":
                novo = User.objects.filter(pk=responsavel_id, is_active=True).first()
                if novo is None or not eh_ti(novo):
                    raise ConversaRecusada("O responsável precisa ser alguém da TI.")
            if (novo.pk if novo else None) != chamado.responsavel_id:
                chamado.responsavel = novo
                chamado.atualizado_por = user
                chamado.save(update_fields=["responsavel", "atualizado_por", "updated_at"])
                registrar_evento(
                    chamado.contratacao, "chamado",
                    f"Responsável pelo chamado “{Tipo(chamado.tipo).label}”: "
                    f"{_nome_do_usuario(novo) if novo else 'ninguém'}.", user,
                )
                mudou_resp = True

        if not mudou_situacao and not mudou_resp:
            raise ConversaRecusada("Nada foi alterado.")
    return chamado


# ─────────────────────────────────────────────────────────────
# Leitura
# ─────────────────────────────────────────────────────────────

def consultar_chamados(*, grupo: str = "", q: str = "", tipo: str = "", com_falha: bool = False):
    """Os chamados da lista (RH e TI), mais recentes primeiro. Os filtros são os
    da tela: grupo de situação, texto livre (vaga, contratado, nº do chamado,
    assunto), tipo e "só os que têm envio com falha"."""
    from django.db.models import Q

    qs = (
        ContratacaoChamado.objects
        .select_related("contratacao", "responsavel", "criado_por")
        .annotate(n_mensagens=Count("mensagens", distinct=True))
    )
    if grupo in GRUPOS_DE_ATENDIMENTO:
        qs = qs.filter(atendimento__in=GRUPOS_DE_ATENDIMENTO[grupo])
    if tipo in Tipo.values:
        qs = qs.filter(tipo=tipo)
    if com_falha:
        qs = qs.filter(
            Q(status=StatusChamado.FALHA) | Q(mensagens__estado=Msg.Estado.FALHA)
            | Q(mensagens__entrega_recusada=True)
        ).distinct()
    q = (q or "").strip()
    if q:
        filtro = (
            Q(contratacao__cargo__icontains=q) | Q(contratacao__colaborador_nome__icontains=q)
            | Q(contratacao__colaborador__nome__icontains=q) | Q(identificador__icontains=q)
            | Q(assunto__icontains=q)
        )
        if q.isdigit():
            filtro |= Q(pk=int(q)) | Q(contratacao_id=int(q))
        qs = qs.filter(filtro)
    return qs.order_by(F("ultima_atividade_em").desc(nulls_last=True), "-created_at")


def contagem_por_grupo() -> dict:
    """Quantos chamados há em cada grupo — os números dos filtros da lista."""
    brutos = dict(
        ContratacaoChamado.objects.values_list("atendimento").annotate(n=Count("pk")).order_by()
    )
    return {
        grupo: sum(brutos.get(a, 0) for a in valores)
        for grupo, valores in GRUPOS_DE_ATENDIMENTO.items()
    }


def pendencias_de_ti(contratacoes) -> dict[int, int]:
    """`{vaga_pk: nº de chamados ainda não encerrados}` — UMA consulta para
    qualquer quantidade de vagas. É o que responde "a vaga está preenchida, mas
    falta a TI terminar o quê?"."""
    ids = [c.pk for c in contratacoes]
    if not ids:
        return {}
    return dict(
        ContratacaoChamado.objects.filter(contratacao_id__in=ids)
        .exclude(atendimento__in=ENCERRADOS)
        .values_list("contratacao_id").annotate(n=Count("pk")).order_by()
    )


def dados_da_solicitacao(chamado) -> tuple[list, list]:
    """`(vaga, específicas)` — o que a TI precisa para EXECUTAR o pedido, lido do
    snapshot confirmado na hora da solicitação (não do que a vaga diz hoje). A
    mesma lista que vai no e-mail de abertura, para a tela e o e-mail não
    divergirem."""
    from services import email_alertas

    return email_alertas._linhas_do_chamado(chamado.tipo, chamado.dados or {})


@dataclass
class ResumoDeChamadosDaVaga:
    total: int
    pendentes: int          # ainda não encerrados (concluído/cancelado saem da conta)
    por_equipe: list        # [(rotulo do tipo, responsável ou "Equipe de TI")] dos pendentes


def resumo_da_vaga(contratacao) -> ResumoDeChamadosDaVaga:
    """O que o RH quer saber depois de preencher a vaga: quais pedidos ainda
    estão com a TI, e com quem."""
    chamados = list(contratacao.chamados.all())
    abertos = [c for c in chamados if c.atendimento not in ENCERRADOS]
    return ResumoDeChamadosDaVaga(
        total=len(chamados), pendentes=len(abertos),
        por_equipe=[
            (Tipo(c.tipo).label, _nome_do_usuario(c.responsavel) if c.responsavel_id else "Equipe de TI")
            for c in abertos
        ],
    )
