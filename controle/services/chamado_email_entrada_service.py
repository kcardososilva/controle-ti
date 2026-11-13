"""
E-mail RECEBIDO → conversa do chamado.

O caminho inverso do envio: a TI (ou o RH) responde pela caixa de e-mail e a
resposta aparece na conversa da plataforma. Este módulo recebe o e-mail BRUTO
(RFC 822) — não importa se veio da caixa, de uma importação manual ou de um
reprocessamento — e decide o que fazer com ele. A leitura da caixa em si é do
transporte (`caixa_graph_service`); aqui não há rede.

O que cada decisão protege:

  • VÍNCULO POR VÁRIOS SINAIS, NUNCA SÓ O ASSUNTO. (1) `In-Reply-To`/`References`
    contra os Message-IDs que NÓS geramos; (2) o id de conversa do provedor,
    aprendido na primeira resposta; (3) a referência ASSINADA `CH-<id>-<hmac>`
    no corpo/assunto. Se os sinais apontam para chamados DIFERENTES, o e-mail não
    é encaixado por palpite: vai para a quarentena. Com várias vagas em andamento,
    errar o chamado é pior do que pedir revisão.

  • REMETENTE RECONHECIDO. Só vira mensagem quem é participante do chamado: um
    destinatário da TI daquele tipo, o solicitante, um usuário do sistema com
    acesso ao módulo, ou alguém que já escreveu naquele chamado. Qualquer outro
    endereço — inclusive um que cite o Message-ID certo — cai na quarentena: o
    vínculo prova que o e-mail fala do chamado, não que quem escreve pode
    falar nele.

  • AUTENTICIDADE. O Exchange registra SPF/DKIM/DMARC em `Authentication-Results`.
    Falha declarada (`=fail`) manda o e-mail para a quarentena, mesmo de
    remetente conhecido: é o caso de alguém se passando por um participante.

  • DEDUPLICAÇÃO EM DOIS NÍVEIS. `provider_id` (o id do e-mail no provedor) evita
    baixar de novo; `message_id` (RFC 822) evita registrar duas vezes o mesmo
    e-mail mesmo vindo por caminhos diferentes (caixa + importação + reprocesso).
    Duas leituras concorrentes esbarram na mesma restrição única — e a perdedora
    trata como "já registrado", sem erro nem duplicata.

  • NADA SE PERDE. O e-mail bruto de tudo o que não foi ignorado é guardado em
    disco privado: uma falha (disco cheio, defeito) é reprocessada a partir dele,
    sem depender de a caixa ainda tê-lo. E-mail SEM relação com chamado guarda
    só os identificadores — o conteúdo de e-mail que não é nosso não é retido.

  • AVISO DE NÃO ENTREGA (DSN) não é mensagem: marca a mensagem NOSSA citada como
    `entrega_recusada`. É a única evidência de entrega que a caixa fornece.

  • RESPOSTA AUTOMÁTICA (fora do escritório) e o que o próprio sistema enviou
    são ignorados — senão a conversa teria ruído e laço.
"""

from __future__ import annotations

import email
import hashlib
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as dt_timezone
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.utils import timezone

from ProjetoEstoque.models import (
    ChamadoCaixaEstado,
    ChamadoEmailRecebido as Registro,
    ChamadoMensagem as Msg,
    ContratacaoChamado,
)
from services import acesso_restrito_service as ar
from services import chamado_anexo_service as anexos_svc
from services import chamado_conversa_service as conversa
from services.contratacao_service import registrar_evento

logger = logging.getLogger(__name__)

MAX_BRUTO = 40 * 1024 * 1024          # e-mail maior que isto não é processado
JANELA_INICIAL = timedelta(days=2)    # primeira leitura: não importa a caixa inteira
SOBREPOSICAO = timedelta(minutes=15)  # cada leitura recomeça um pouco antes da marca-d'água
MAX_TENTATIVAS_AUTOMATICAS = 5        # falhas além disso só voltam por ação do TI

_FINAIS = (Registro.Estado.PROCESSADO, Registro.Estado.IGNORADO,
           Registro.Estado.DESCARTADO, Registro.Estado.QUARENTENA)


# ─────────────────────────────────────────────────────────────
# Interpretar o e-mail bruto
# ─────────────────────────────────────────────────────────────

@dataclass
class AnexoLido:
    nome: str
    content_type: str
    conteudo: bytes


@dataclass
class EmailLido:
    message_id: str
    in_reply_to: list
    referencias: list
    remetente_nome: str
    remetente_email: str
    destinatarios: list
    assunto: str
    data: datetime | None
    texto_completo: str
    texto_visivel: str
    anexos: list = field(default_factory=list)
    automatico: bool = False
    retorno_entrega: bool = False
    diagnostico_entrega: str = ""
    ids_citados: list = field(default_factory=list)
    autenticacao_falhou: str = ""


_RE_FALHA_AUTH = re.compile(r"\b(spf|dkim|dmarc|compauth)\s*=\s*fail\b", re.I)
_RE_CITACAO = (
    re.compile(r"^\s*(Em|On)\s.{3,200}\b(escreveu|wrote)\s*:?\s*$", re.I),
    re.compile(r"^\s*-{2,}\s*(Original Message|Mensagem original|Forwarded message)\s*-{2,}\s*$", re.I),
    re.compile(r"^\s*_{8,}\s*$"),
)
_RE_CAB_DE = re.compile(r"^\s*(De|From)\s*:\s*\S", re.I)
_RE_CAB_SEGUINTE = re.compile(r"^\s*(Enviado|Sent|Data|Date|Para|To)\s*:", re.I)
_RE_REFERENCIA_LINHA = re.compile(r"^\s*Ref\. do chamado\s*:.*$", re.I | re.M)


def _cabecalho(msg, nome: str) -> str:
    try:
        return " ".join(str(msg.get(nome, "") or "").split())
    except Exception:  # noqa: BLE001 — cabeçalho malformado não derruba a leitura
        return ""


def _html_para_texto(html: str) -> str:
    """Texto legível de um corpo HTML. lxml remove script/estilo e os blocos
    viram quebras de linha; nada do HTML original sobrevive."""
    from lxml import html as lxml_html

    try:
        arvore = lxml_html.fromstring(html)
    except Exception:  # noqa: BLE001
        return re.sub(r"<[^>]+>", " ", html)
    for ruido in arvore.xpath("//script|//style|//head"):
        ruido.getparent().remove(ruido)
    for br in arvore.xpath("//br"):
        br.tail = "\n" + (br.tail or "")
    for bloco in arvore.xpath("//p|//div|//li|//tr|//h1|//h2|//h3"):
        bloco.tail = "\n" + (bloco.tail or "")
    texto = arvore.text_content()
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t\xa0]+", " ", texto))


def _texto_do_corpo(msg) -> str:
    corpo = msg.get_body(preferencelist=("plain", "html"))
    if corpo is None:
        return ""
    try:
        conteudo = corpo.get_content()
    except Exception:  # noqa: BLE001 — charset desconhecido etc.
        bruto = corpo.get_payload(decode=True) or b""
        conteudo = bruto.decode("utf-8", "replace")
    if corpo.get_content_type() == "text/html":
        conteudo = _html_para_texto(conteudo)
    return conteudo.replace("\r\n", "\n").replace("\r", "\n")


def _comeca_citacao(linhas: list, i: int) -> bool:
    linha = linhas[i]
    if any(r.match(linha) for r in _RE_CITACAO):
        return True
    # "On ... wrote:" que o cliente quebrou em duas linhas.
    if i + 1 < len(linhas) and re.match(r"^\s*(Em|On)\s", linha, re.I):
        junto = f"{linha.strip()} {linhas[i + 1].strip()}"
        if re.search(r"\b(escreveu|wrote)\s*:?\s*$", junto, re.I):
            return True
    # Bloco de cabeçalho do Outlook: "De: ... / Enviado: ...".
    if _RE_CAB_DE.match(linha):
        return any(_RE_CAB_SEGUINTE.match(l) for l in linhas[i + 1:i + 5])
    return False


def texto_sem_citacao(texto: str) -> str:
    """A parte NOVA da resposta: corta no primeiro marcador de citação e tira as
    linhas citadas (`>`) e o nosso rodapé de referência. Se sobrar nada (resposta
    escrita só dentro da citação), devolve o texto inteiro — melhor repetir o
    contexto do que mostrar uma mensagem vazia."""
    texto = _RE_REFERENCIA_LINHA.sub("", texto or "")
    linhas = texto.split("\n")
    corte = next((i for i in range(len(linhas)) if _comeca_citacao(linhas, i)), None)
    visivel = linhas if corte is None else linhas[:corte]
    visivel = [l for l in visivel if not l.lstrip().startswith(">")]
    resultado = "\n".join(visivel).strip()
    return resultado or (texto or "").strip()


def _ids_do_dsn(msg) -> list[str]:
    """Message-IDs da mensagem ORIGINAL citada dentro de um aviso de não entrega."""
    achados = []
    for parte in msg.walk():
        tipo = parte.get_content_type()
        try:
            if tipo == "message/rfc822":
                interna = parte.get_payload(0)
                achados.append(interna.get("Message-ID", ""))
            elif tipo == "text/rfc822-headers":
                interna = email.message_from_string(parte.get_content())
                achados.append(interna.get("Message-ID", ""))
        except Exception:  # noqa: BLE001
            continue
    return [i for i in (conversa.normalizar_id(a) for a in achados) if i]


def _diagnostico_do_dsn(msg) -> str:
    for parte in msg.walk():
        if parte.get_content_type() == "message/delivery-status":
            try:
                for bloco in parte.get_payload():
                    codigo = _cabecalho(bloco, "Diagnostic-Code") or _cabecalho(bloco, "Status")
                    if codigo:
                        return codigo[:500]
            except Exception:  # noqa: BLE001
                break
    return " ".join(_texto_do_corpo(msg).split())[:300]


def interpretar(raw: bytes) -> EmailLido:
    """Lê o e-mail bruto. Só interpreta — nenhuma decisão, nenhuma escrita."""
    msg = BytesParser(policy=policy.default).parsebytes(raw)

    message_id = conversa.normalizar_id(_cabecalho(msg, "Message-ID"))
    if not message_id:
        # Sem Message-ID a deduplicação cai no conteúdo: o mesmo e-mail dá o mesmo hash.
        message_id = "sha256:" + hashlib.sha256(raw).hexdigest()

    nome, endereco = parseaddr(_cabecalho(msg, "From"))
    destinatarios = [a for _, a in getaddresses(
        [str(h) for h in (msg.get_all("To") or [])] + [str(h) for h in (msg.get_all("Cc") or [])]
    ) if a]

    data = None
    try:
        data = parsedate_to_datetime(_cabecalho(msg, "Date"))
        if data is not None and timezone.is_naive(data):
            data = timezone.make_aware(data, dt_timezone.utc)
    except Exception:  # noqa: BLE001 — Date ausente ou torto
        data = None

    texto_completo = _texto_do_corpo(msg)

    anexos, retorno = [], False
    local_remetente = endereco.split("@")[0].lower() if endereco else ""
    if (msg.get_content_type() == "multipart/report" and msg.get_param("report-type") == "delivery-status") \
            or local_remetente in ("mailer-daemon", "postmaster"):
        retorno = True
    if not retorno:
        for parte in msg.iter_attachments():
            tipo = parte.get_content_type()
            nome_arquivo = parte.get_filename() or ""
            if tipo == "message/rfc822":
                try:
                    conteudo = parte.get_payload(0).as_bytes()
                except Exception:  # noqa: BLE001
                    conteudo = b""
                nome_arquivo = nome_arquivo or "mensagem-encaminhada.eml"
            else:
                conteudo = parte.get_payload(decode=True) or b""
            # Imagem embutida da assinatura (logo, ícone): não é anexo, é enfeite.
            if (parte.get_content_disposition() == "inline" and parte.get("Content-ID")
                    and tipo.startswith("image/")):
                continue
            if not nome_arquivo and parte.get_content_disposition() != "attachment":
                continue
            anexos.append(AnexoLido(nome_arquivo or "anexo", tipo, conteudo))

    automatico = (
        _cabecalho(msg, "Auto-Submitted").lower().startswith("auto-")
        or _cabecalho(msg, "Precedence").lower() in ("bulk", "junk", "auto_reply")
        or re.match(r"^(automatic reply|resposta autom[aá]tica)\s*:", _cabecalho(msg, "Subject"), re.I) is not None
    )

    auth = ""
    for h in (msg.get_all("Authentication-Results") or []):
        achou = _RE_FALHA_AUTH.search(" ".join(str(h).split()))
        if achou:
            auth = achou.group(0).replace(" ", "")
            break

    return EmailLido(
        message_id=message_id,
        in_reply_to=conversa.ids_de_cabecalho(_cabecalho(msg, "In-Reply-To")),
        referencias=conversa.ids_de_cabecalho(_cabecalho(msg, "References")),
        remetente_nome=nome or "", remetente_email=(endereco or "").strip().lower(),
        destinatarios=destinatarios, assunto=_cabecalho(msg, "Subject"), data=data,
        texto_completo=texto_completo, texto_visivel=texto_sem_citacao(texto_completo),
        anexos=anexos, automatico=automatico, retorno_entrega=retorno,
        diagnostico_entrega=_diagnostico_do_dsn(msg) if retorno else "",
        ids_citados=_ids_do_dsn(msg) if retorno else [],
        autenticacao_falhou=auth,
    )


# ─────────────────────────────────────────────────────────────
# Decidir: a que chamado pertence, e quem escreveu
# ─────────────────────────────────────────────────────────────

@dataclass
class Decisao:
    acao: str                    # registrar | retorno | quarentena | ignorar
    motivo: str = ""
    chamado: ContratacaoChamado | None = None
    lado: str = ""
    autor: User | None = None


def vincular(lido: EmailLido, conversation_id: str = "") -> set[int]:
    """Ids dos chamados a que o e-mail aponta, somando os três sinais. Vazio =
    sem relação; mais de um = ambíguo (quem chama manda para a quarentena)."""
    citados = set(lido.in_reply_to) | set(lido.referencias) | set(lido.ids_citados)
    achados = set()
    if citados:
        achados |= set(Msg.objects.filter(message_id__in=citados).values_list("chamado_id", flat=True))
    if conversation_id:
        achados |= set(ContratacaoChamado.objects.filter(conversation_id=conversation_id).values_list("pk", flat=True))
    assinados = conversa.chamados_da_referencia(f"{lido.assunto}\n{lido.texto_completo}")
    if assinados:
        achados |= set(ContratacaoChamado.objects.filter(pk__in=assinados).values_list("pk", flat=True))
    return achados


def classificar_remetente(chamado, endereco: str):
    """`(lado, usuário)` de quem escreveu, ou `(None, None)` se não é participante."""
    addr = (endereco or "").strip().lower()
    if not addr:
        return None, None
    usuarios = list(User.objects.filter(email__iexact=addr, is_active=True)[:2])
    usuario = usuarios[0] if len(usuarios) == 1 else None

    if addr in {e.lower() for e in conversa.enderecos_da_ti(chamado)}:
        return Msg.Lado.TI, usuario
    solicitante = chamado.criado_por
    if solicitante is not None and (solicitante.email or "").strip().lower() == addr:
        return Msg.Lado.RH, solicitante
    anterior = (
        chamado.mensagens.filter(remetente_email__iexact=addr).exclude(remetente_email="")
        .order_by("-pk").first()
    )
    if anterior is not None:
        return anterior.lado, usuario
    if usuario is not None and ar.pode_acompanhar_chamados(usuario):
        return (Msg.Lado.RH if "contratacao" in ar.perfis_de(usuario) else Msg.Lado.TI), usuario
    return None, None


def decidir(lido: EmailLido, conversation_id: str = "") -> Decisao:
    """O que fazer com o e-mail. Só LÊ o banco."""
    sistema = (settings.EMAIL_HOST_USER or "").strip().lower()
    if sistema and lido.remetente_email == sistema:
        return Decisao("ignorar", "enviado pelo próprio sistema")

    if lido.retorno_entrega:
        return Decisao("retorno")
    if lido.automatico:
        return Decisao("ignorar", "resposta automática")

    achados = vincular(lido, conversation_id)
    if not achados:
        return Decisao("ignorar", "sem vínculo com chamado")
    if len(achados) > 1:
        return Decisao("quarentena", "vínculo ambíguo: o e-mail aponta para mais de um chamado")

    chamado = ContratacaoChamado.objects.select_related("contratacao", "criado_por").get(pk=achados.pop())
    if lido.autenticacao_falhou:
        return Decisao(
            "quarentena", f"falha na verificação de autenticidade do remetente ({lido.autenticacao_falhou})",
            chamado=chamado,
        )
    lado, autor = classificar_remetente(chamado, lido.remetente_email)
    if lado is None:
        return Decisao(
            "quarentena", f"remetente não reconhecido como participante do chamado ({lido.remetente_email or 'sem endereço'})",
            chamado=chamado,
        )
    return Decisao("registrar", chamado=chamado, lado=lado, autor=autor)


# ─────────────────────────────────────────────────────────────
# Aplicar
# ─────────────────────────────────────────────────────────────

def _gravar_mensagem(lido: EmailLido, chamado, lado: str, autor, *, recebido_em, conversation_id) -> Msg:
    """Cria a mensagem e os anexos. Roda numa transação; se ela desfaz, os
    arquivos já gravados em disco são apagados."""
    criados = []
    try:
        with transaction.atomic():
            chamado = ContratacaoChamado.objects.select_for_update().select_related("contratacao").get(pk=chamado.pk)
            msg = conversa.registrar_mensagem_recebida(
                chamado=chamado, lado=lado, autor=autor,
                remetente_nome=lido.remetente_nome or lido.remetente_email,
                remetente_email=lido.remetente_email,
                destinatarios=", ".join(lido.destinatarios),
                assunto=lido.assunto, corpo=lido.texto_visivel,
                message_id=lido.message_id,
                in_reply_to=lido.in_reply_to[0] if lido.in_reply_to else "",
                referencias=" ".join(lido.referencias[-20:]),
                enviado_em=lido.data, recebido_em=recebido_em, conversation_id=conversation_id,
            )
            total = 0
            for i, a in enumerate(lido.anexos):
                ok, motivo, tipo = anexos_svc.avaliar(a.nome, a.conteudo)
                if ok and i >= anexos_svc.MAX_ARQUIVOS:
                    ok, motivo = False, f"passou do máximo de {anexos_svc.MAX_ARQUIVOS} arquivos por mensagem"
                if ok and total + len(a.conteudo) > anexos_svc.LIMITE_TOTAL:
                    ok, motivo = False, "o total dos anexos passou do limite por mensagem"
                if ok:
                    total += len(a.conteudo)
                    criados += anexos_svc.salvar(msg, [anexos_svc.Arquivo(anexos_svc.nome_seguro(a.nome), a.conteudo, tipo)], autor)
                else:
                    anexos_svc.registrar_bloqueado(msg, a.nome, len(a.conteudo), motivo, autor)
            return msg
    except Exception:
        anexos_svc.limpar_arquivos(criados)
        raise


def _marcar_nao_entregue(lido: EmailLido) -> tuple[ContratacaoChamado | None, int]:
    """Aviso de não entrega: marca as mensagens NOSSAS citadas. Devolve o chamado
    e quantas mensagens marcou."""
    ids = set(lido.in_reply_to) | set(lido.referencias) | set(lido.ids_citados)
    if not ids:
        return None, 0
    alvos = list(
        Msg.objects.select_related("chamado", "chamado__contratacao")
        .filter(message_id__in=ids, direcao=Msg.Direcao.SAIDA)
    )
    for m in alvos:
        Msg.objects.filter(pk=m.pk).update(
            entrega_recusada=True, entrega_erro=lido.diagnostico_entrega[:500],
        )
        registrar_evento(
            m.chamado.contratacao, "chamado",
            f"Entrega recusada: e-mail do chamado “{m.chamado.get_tipo_display()}” não chegou ao destinatário.", None,
        )
        ContratacaoChamado.objects.filter(pk=m.chamado_id).update(ultima_atividade_em=timezone.now())
    return (alvos[0].chamado if alvos else None), len(alvos)


# ─────────────────────────────────────────────────────────────
# Tratar um e-mail (o ponto de entrada)
# ─────────────────────────────────────────────────────────────

@dataclass
class Resultado:
    estado: str                  # estado final em ChamadoEmailRecebido
    motivo: str = ""
    chamado: ContratacaoChamado | None = None
    mensagem: Msg | None = None
    duplicado: bool = False


def _gravar_registro(registro, *, estado, motivo, raw, lido, provider_id, recebido_em,
                     conversation_id, chamado=None, mensagem=None) -> Registro:
    """Cria/atualiza a linha de controle do e-mail. E-mail ignorado guarda só
    identificadores; os demais guardam remetente, assunto e o bruto."""
    guardar_conteudo = estado != Registro.Estado.IGNORADO
    campos = dict(
        estado=estado, motivo=motivo[:2000], recebido_em=recebido_em,
        message_id=(lido.message_id if lido else "")[:255],
        conversation_id=conversation_id[:255], chamado=chamado, mensagem=mensagem,
        remetente=(lido.remetente_email if lido and guardar_conteudo else "")[:254],
        assunto=(lido.assunto if lido and guardar_conteudo else "")[:255],
    )
    if registro is None:
        registro = Registro(provider_id=provider_id[:255], tentativas=1, **campos)
    else:
        for k, v in campos.items():
            setattr(registro, k, v)
        registro.tentativas += 1
    if guardar_conteudo and raw and not registro.bruto:
        registro.bruto.save(f"{uuid.uuid4().hex}.eml", ContentFile(raw), save=False)
    try:
        with transaction.atomic():
            registro.save()
    except IntegrityError:
        # Outra leitura gravou este provider_id no mesmo instante: vale a dela.
        existente = Registro.objects.filter(provider_id=provider_id).first()
        if existente is None:
            raise
        return existente
    return registro


def tratar_email(raw: bytes, *, provider_id: str = "", recebido_em=None,
                 conversation_id: str = "", registro: Registro | None = None) -> Resultado:
    """Interpreta, decide e aplica UM e-mail bruto. Nunca levanta: qualquer erro
    vira `falha` com o motivo, e o bruto fica guardado para reprocessar."""
    recebido_em = recebido_em or timezone.now()
    if registro is None and provider_id:
        registro = Registro.objects.filter(provider_id=provider_id).first()
    if registro is not None and registro.estado in _FINAIS:
        return Resultado(registro.estado, "Já tratado.", registro.chamado, registro.mensagem, duplicado=True)

    if len(raw) > MAX_BRUTO:
        _gravar_registro(registro, estado=Registro.Estado.FALHA,
                         motivo=f"E-mail maior que {MAX_BRUTO // (1024 * 1024)} MB: não processado.",
                         raw=b"", lido=None, provider_id=provider_id, recebido_em=recebido_em,
                         conversation_id=conversation_id)
        return Resultado(Registro.Estado.FALHA, "e-mail grande demais")

    lido = None
    try:
        lido = interpretar(raw)

        existente = Msg.objects.filter(message_id=lido.message_id).select_related("chamado").first()
        if existente is not None:
            # `mensagem` NÃO é ligada aqui: a relação é 1-para-1 com o registro que a
            # criou. Este é só o segundo caminho de um e-mail já registrado.
            _gravar_registro(registro, estado=Registro.Estado.PROCESSADO, motivo="Mensagem já registrada.",
                             raw=b"", lido=lido, provider_id=provider_id, recebido_em=recebido_em,
                             conversation_id=conversation_id, chamado=existente.chamado)
            return Resultado(Registro.Estado.PROCESSADO, "Mensagem já registrada.", existente.chamado,
                             existente, duplicado=True)

        decisao = decidir(lido, conversation_id)
        mensagem = None
        if decisao.acao == "registrar":
            mensagem = _gravar_mensagem(lido, decisao.chamado, decisao.lado, decisao.autor,
                                        recebido_em=recebido_em, conversation_id=conversation_id)
            estado, motivo = Registro.Estado.PROCESSADO, ""
        elif decisao.acao == "retorno":
            chamado, n = _marcar_nao_entregue(lido)
            decisao.chamado = chamado
            estado = Registro.Estado.PROCESSADO if n else Registro.Estado.IGNORADO
            motivo = "aviso de não entrega" if n else "aviso de não entrega sem vínculo com chamado"
        elif decisao.acao == "quarentena":
            estado, motivo = Registro.Estado.QUARENTENA, decisao.motivo
        else:
            estado, motivo = Registro.Estado.IGNORADO, decisao.motivo
    except IntegrityError:
        # Corrida: o mesmo Message-ID foi gravado por outra leitura entre a checagem e o INSERT.
        existente = Msg.objects.filter(message_id=lido.message_id).select_related("chamado").first() if lido else None
        _gravar_registro(registro, estado=Registro.Estado.PROCESSADO, motivo="Mensagem já registrada.",
                         raw=b"", lido=lido, provider_id=provider_id, recebido_em=recebido_em,
                         conversation_id=conversation_id,
                         chamado=existente.chamado if existente else None)
        return Resultado(Registro.Estado.PROCESSADO, "Mensagem já registrada.", duplicado=True)
    except Exception as exc:  # noqa: BLE001 — a regra é registrar a falha, não propagá-la
        logger.exception("e-mail recebido (%s): falha ao processar", provider_id or "sem id")
        _gravar_registro(registro, estado=Registro.Estado.FALHA, motivo=f"Erro ao processar: {str(exc)[:300]}",
                         raw=raw, lido=lido, provider_id=provider_id, recebido_em=recebido_em,
                         conversation_id=conversation_id)
        return Resultado(Registro.Estado.FALHA, str(exc)[:300])

    _gravar_registro(registro, estado=estado, motivo=motivo, raw=raw, lido=lido,
                     provider_id=provider_id, recebido_em=recebido_em, conversation_id=conversation_id,
                     chamado=decisao.chamado, mensagem=mensagem)
    return Resultado(estado, motivo, decisao.chamado, mensagem)


# ─────────────────────────────────────────────────────────────
# O TI trata a quarentena e as falhas
# ─────────────────────────────────────────────────────────────

def _exigir_ti(user) -> None:
    if not conversa.eh_ti(user):
        raise PermissionDenied("Só a TI trata e-mails em revisão.")


def liberar_quarentena(*, registro_pk: int, lado: str, user) -> Msg:
    """A TI aceita um e-mail em revisão: ele entra na conversa, do lado
    escolhido. Só se o e-mail aponta para UM chamado — vínculo ambíguo não se
    resolve por aqui. Quem foi liberado vira participante: as próximas mensagens
    desse endereço, naquele chamado, passam sem revisão."""
    _exigir_ti(user)
    if lado not in Msg.Lado.values:
        raise conversa.ConversaRecusada("Escolha o lado do remetente (RH, TI ou outro participante).")
    with transaction.atomic():
        registro = Registro.objects.select_for_update().select_related("chamado", "chamado__contratacao").get(pk=registro_pk)
        if registro.estado != Registro.Estado.QUARENTENA:
            raise conversa.ConversaRecusada("Este e-mail já foi tratado.")
        if registro.chamado is None or not registro.bruto:
            raise conversa.ConversaRecusada(
                "Este e-mail não aponta para um único chamado — não dá para liberá-lo. "
                "Descarte-o e responda pela plataforma."
            )
        with registro.bruto.open("rb") as arquivo:
            raw = arquivo.read()
        lido = interpretar(raw)
        usuarios = list(User.objects.filter(email__iexact=lido.remetente_email, is_active=True)[:2])
        autor = usuarios[0] if len(usuarios) == 1 else None
        mensagem = _gravar_mensagem(lido, registro.chamado, lado, autor,
                                    recebido_em=registro.recebido_em, conversation_id=registro.conversation_id)
        registro.estado = Registro.Estado.PROCESSADO
        registro.motivo = f"Liberado pela TI ({registro.motivo})"[:2000]
        registro.mensagem = mensagem
        registro.resolvido_por = user
        registro.resolvido_em = timezone.now()
        registro.save()
    return mensagem


def descartar_quarentena(*, registro_pk: int, user) -> Registro:
    """A TI descarta um e-mail em revisão. O bruto é apagado do disco: era a única
    razão de guardá-lo, e ele não faz parte de conversa nenhuma."""
    _exigir_ti(user)
    with transaction.atomic():
        registro = Registro.objects.select_for_update().get(pk=registro_pk)
        if registro.estado != Registro.Estado.QUARENTENA:
            raise conversa.ConversaRecusada("Este e-mail já foi tratado.")
        registro.estado = Registro.Estado.DESCARTADO
        registro.resolvido_por = user
        registro.resolvido_em = timezone.now()
        registro.save()
    try:
        registro.bruto.delete(save=True)
    except Exception:  # noqa: BLE001 — melhor esforço
        pass
    return registro


def reprocessar_falha(*, registro_pk: int, user, fonte=None) -> Resultado:
    """Nova tentativa de um e-mail que falhou. Parte do bruto guardado; se não há
    (a falha foi na hora de baixar), pede de novo ao provedor pelo `provider_id`.
    Idempotente: o que já virou mensagem é reconhecido pelo Message-ID."""
    _exigir_ti(user)
    registro = Registro.objects.get(pk=registro_pk)
    if registro.estado != Registro.Estado.FALHA:
        raise conversa.ConversaRecusada("Só e-mails com falha podem ser reprocessados.")
    return _reprocessar(registro, fonte)


def _reprocessar(registro: Registro, fonte) -> Resultado:
    raw = None
    if registro.bruto:
        try:
            with registro.bruto.open("rb") as arquivo:
                raw = arquivo.read()
        except Exception:  # noqa: BLE001
            raw = None
    if raw is None:
        if fonte is None or not registro.provider_id:
            registro.motivo = "Não há cópia do e-mail e a caixa não está disponível para baixá-lo de novo."
            registro.tentativas += 1
            registro.save(update_fields=["motivo", "tentativas", "updated_at"])
            return Resultado(Registro.Estado.FALHA, registro.motivo)
        try:
            raw = fonte.baixar(registro.provider_id)
        except Exception as exc:  # noqa: BLE001
            registro.motivo = f"Falha ao baixar o e-mail da caixa: {str(exc)[:300]}"
            registro.tentativas += 1
            registro.save(update_fields=["motivo", "tentativas", "updated_at"])
            return Resultado(Registro.Estado.FALHA, registro.motivo)
    return tratar_email(raw, provider_id=registro.provider_id, recebido_em=registro.recebido_em,
                        conversation_id=registro.conversation_id, registro=registro)


# ─────────────────────────────────────────────────────────────
# Sincronizar com a caixa
# ─────────────────────────────────────────────────────────────

@dataclass
class ResumoSincronizacao:
    configurada: bool = True
    lidos: int = 0
    registrados: int = 0
    ignorados: int = 0
    quarentena: int = 0
    duplicados: int = 0
    falhas: int = 0
    reprocessados: int = 0
    erro: str = ""

    def texto(self) -> str:
        if not self.configurada:
            return "Sincronização desligada: a caixa não está configurada."
        base = (
            f"{self.lidos} lido(s): {self.registrados} registrado(s), {self.quarentena} em revisão, "
            f"{self.ignorados} sem relação, {self.duplicados} já tratado(s), {self.falhas} falha(s)"
        )
        if self.reprocessados:
            base += f"; {self.reprocessados} falha(s) reprocessada(s)"
        return base + (f". Erro: {self.erro}" if self.erro else ".")


def sincronizar(*, fonte=None, agora=None) -> ResumoSincronizacao:
    """Lê os e-mails novos da caixa e trata cada um; depois reprocessa as falhas
    que ainda têm tentativas automáticas.

    `fonte` é o transporte (`listar(desde)` + `baixar(id)`); por padrão o Graph.
    Leituras concorrentes são seguras: a deduplicação é do banco. A marca-d'água
    só avança até onde TUDO foi tratado ou registrado como falha — nenhum e-mail
    fica para trás sem uma linha dizendo por quê."""
    from services import caixa_graph_service

    agora = agora or timezone.now()
    resumo = ResumoSincronizacao()
    if fonte is None:
        if not caixa_graph_service.configurada():
            resumo.configurada = False
            return resumo
        fonte = caixa_graph_service.CaixaGraph()

    estado = ChamadoCaixaEstado.get()
    desde = (estado.marca_dagua or (agora - JANELA_INICIAL)) - SOBREPOSICAO
    marca = estado.marca_dagua

    try:
        for item in fonte.listar(desde):
            resumo.lidos += 1
            pid = item["id"]
            existente = Registro.objects.filter(provider_id=pid).first()
            if existente is not None and existente.estado in _FINAIS:
                resumo.duplicados += 1
                marca = max(filter(None, [marca, item.get("recebido_em")]), default=marca)
                continue
            try:
                raw = fonte.baixar(pid)
            except Exception as exc:  # noqa: BLE001 — uma falha de download não para a leitura
                _gravar_registro(existente, estado=Registro.Estado.FALHA,
                                 motivo=f"Falha ao baixar o e-mail da caixa: {str(exc)[:300]}",
                                 raw=b"", lido=None, provider_id=pid,
                                 recebido_em=item.get("recebido_em"),
                                 conversation_id=item.get("conversation_id", ""))
                resumo.falhas += 1
                continue
            r = tratar_email(raw, provider_id=pid, recebido_em=item.get("recebido_em"),
                             conversation_id=item.get("conversation_id", ""), registro=existente)
            _contar(resumo, r)
            if r.estado != Registro.Estado.FALHA and item.get("recebido_em"):
                marca = max(filter(None, [marca, item["recebido_em"]]), default=marca)
        resumo.reprocessados = _reprocessar_falhas(fonte, resumo)
    except Exception as exc:  # noqa: BLE001 — caixa fora do ar, token recusado...
        logger.exception("sincronização da caixa de chamados falhou")
        resumo.erro = str(exc)[:300]

    estado.ultima_execucao = agora
    estado.marca_dagua = marca
    estado.ultimo_resumo = resumo.texto()[:255]
    if resumo.erro:
        estado.ultimo_erro = resumo.erro
    else:
        estado.ultimo_sucesso = agora
        estado.ultimo_erro = ""
    estado.save()
    return resumo


def _contar(resumo: ResumoSincronizacao, r: Resultado) -> None:
    if r.duplicado:
        resumo.duplicados += 1
    elif r.estado == Registro.Estado.PROCESSADO:
        resumo.registrados += 1
    elif r.estado == Registro.Estado.QUARENTENA:
        resumo.quarentena += 1
    elif r.estado == Registro.Estado.FALHA:
        resumo.falhas += 1
    else:
        resumo.ignorados += 1


def _reprocessar_falhas(fonte, resumo: ResumoSincronizacao) -> int:
    """Tenta de novo as falhas que ainda não esgotaram as tentativas automáticas.
    O que não esgotou e falha outra vez continua contando na linha da própria falha."""
    refeitos = 0
    for registro in Registro.objects.filter(
        estado=Registro.Estado.FALHA, tentativas__lt=MAX_TENTATIVAS_AUTOMATICAS,
    ):
        r = _reprocessar(registro, fonte)
        if r.estado != Registro.Estado.FALHA:
            refeitos += 1
            resumo.falhas = max(resumo.falhas - 1, 0)
            _contar(resumo, r)
    return refeitos
