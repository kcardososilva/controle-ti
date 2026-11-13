"""
Chamados da contratação — pedir à TI, por e-mail, a estrutura do novo colaborador.

Uma vaga em aberto pode pedir até TRÊS chamados independentes: configuração e
entrega de máquina, criação de usuário e e-mail, e orientação/acesso aos
sistemas. Este módulo é a fonte única de quatro perguntas:

  1. PODE pedir? — vaga Em aberto + capacidade do usuário (`pode_solicitar`);
  2. O QUE JÁ FOI pedido e em que situação está (`painel_de_chamados`);
  3. COMO pedir sem duplicar (`solicitar`, `reenviar`);
  4. O QUE a TI recebe (`montar_dados` → snapshot → `email_alertas`).

Três decisões que a tela não mostra e que sustentam o resto:

  • A UNICIDADE é do BANCO. `ContratacaoChamado` tem
    `UniqueConstraint(contratacao, tipo)`: clique repetido, duas abas e reenvio
    esbarram no mesmo INSERT, e o segundo vira "já solicitado". Um `if` na view
    não aguenta duas requisições simultâneas; a constraint aguenta.

  • O ENVIO é DEPOIS do commit e por chamado. A linha nasce PENDENTE numa
    transação curta; o SMTP roda fora dela (nunca se segura lock de SQLite
    esperando o servidor de e-mail) e o resultado é gravado numa segunda
    transação, uma por chamado. Falha de um não desfaz nem impede os outros.

  • "ENVIADO" é o que o sistema SABE: o servidor aceitou a mensagem. Nada aqui
    afirma que o chamado foi aberto no sistema de atendimento — não há
    integração. Essa confirmação é o `identificador`, registrado pelo TI
    (`registrar_identificador`).

O que este módulo NÃO faz: mudar o status da vaga. Pedir estrutura não avança
nem recua etapa nenhuma (o `Contratacao.status` só é LIDO aqui).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.utils import timezone

from ProjetoEstoque.models import (
    AtendimentoChamadoChoices,
    Contratacao,
    ContratacaoChamado,
    ContratacaoChamadoTentativa,
    StatusChamadoContratacao as StatusChamado,
    StatusContratacaoChoices,
    SuporteSistemasChoices,
    TipoChamadoContratacao as Tipo,
    TipoTelefoniaChoices,
)
from services import acesso_restrito_service as ar
from services import chamado_conversa_service as conversa
from services.contratacao_service import registrar_evento

logger = logging.getLogger(__name__)

# Ordem de exibição e de envio. A máquina vai primeiro: é a única obrigatória.
ORDEM_TIPOS = (Tipo.MAQUINA, Tipo.USUARIO_EMAIL, Tipo.SISTEMAS)

# Só a vaga EM ABERTO pede estrutura. É uma tupla, e não uma comparação solta,
# para a regra ter um lugar só para mudar — a view, o template e o service leem
# daqui (via `pode_solicitar`).
STATUS_QUE_PERMITEM = (StatusContratacaoChoices.ABERTA,)

_TOM = {
    StatusChamado.ENVIADO: "ok",
    StatusChamado.PENDENTE: "aviso",
    StatusChamado.FALHA: "perigo",
}


class SolicitacaoRecusada(Exception):
    """A regra de negócio disse não. A mensagem já vem em texto de tela."""


@dataclass
class ResultadoSolicitacao:
    """O que aconteceu numa solicitação, por chamado."""
    enviados: list = field(default_factory=list)
    falhas: list = field(default_factory=list)
    ja_solicitados: list = field(default_factory=list)  # tipos que já existiam

    @property
    def criados(self) -> int:
        return len(self.enviados) + len(self.falhas)


# ─────────────────────────────────────────────────────────────
# Leitura
# ─────────────────────────────────────────────────────────────

def pode_solicitar(contratacao) -> bool:
    """A VAGA admite pedir estrutura? (Quem pede é outra pergunta:
    `acesso_restrito_service.pode_solicitar_estrutura`.)"""
    return contratacao.status in STATUS_QUE_PERMITEM


def pode_acompanhar_envio(contratacao) -> bool:
    """O que JÁ foi pedido ainda pode ser retomado? (reenviar o e-mail que falhou)

    É uma pergunta diferente de `pode_solicitar` (pedir algo NOVO, só com a vaga
    em aberto). Preencher a vaga não encerra o pedido: a TI segue preparando a
    estrutura, e um e-mail de abertura que falhou precisa poder ser reenviado
    enquanto a vaga está em andamento — sem isso o pedido nunca chegaria à TI.
    Só a vaga ENCERRADA (concluída ou cancelada) bloqueia."""
    return not contratacao.encerrada


def _motivo_bloqueio(contratacao) -> str:
    return (
        "Chamados de estrutura só podem ser solicitados com a vaga em aberto — "
        f"esta está “{contratacao.get_status_display()}”."
    )


def painel_de_chamados(contratacao) -> list[dict]:
    """Uma linha por TIPO, sempre as três — inclusive as não solicitadas, para
    a tela dizer "Não solicitado" em vez de calar.

    Os quatro estados que a tela distingue: Não solicitado · Envio pendente ·
    E-mail enviado com sucesso · Falha no envio. O estado "enviado" traz uma
    nota que separa "o servidor aceitou a mensagem" de "o TI confirmou o
    chamado" — o sistema não pode chamar de aberto o que só enviou.

    Lê `contratacao.chamados.all()`: com `prefetch_related` a tela inteira
    custa o mesmo número de consultas com 0 ou 3 chamados.
    """
    por_tipo = {c.tipo: c for c in contratacao.chamados.all()}
    linhas = []
    for tipo in ORDEM_TIPOS:
        chamado = por_tipo.get(tipo)
        linha = {
            "tipo": tipo, "rotulo": Tipo(tipo).label, "chamado": chamado,
            "obrigatorio": tipo == Tipo.MAQUINA and contratacao.precisa_computador,
        }
        if chamado is None:
            linha.update(estado="nao_solicitado", estado_rotulo="Não solicitado", tom="neutro", nota="")
        else:
            nota = ""
            if chamado.status == StatusChamado.ENVIADO:
                nota = (
                    f"Chamado nº {chamado.identificador} registrado pelo TI."
                    if chamado.confirmado else
                    "O servidor de e-mail aceitou a mensagem. Ainda sem confirmação de "
                    "abertura do chamado pelo atendimento."
                )
            elif chamado.interrompido:
                nota = "O envio não terminou e não se sabe se a mensagem saiu. Pode tentar de novo."
            elif chamado.status == StatusChamado.PENDENTE:
                nota = "Envio em andamento."
            linha.update(
                estado=chamado.status, estado_rotulo=chamado.get_status_display(),
                tom=_TOM[chamado.status], nota=nota,
                atendimento_rotulo=chamado.get_atendimento_display(),
                atendimento_tom=conversa.tom_do_atendimento(chamado.atendimento),
            )
        linhas.append(linha)
    return linhas


def tipos_pendentes(contratacao) -> list:
    """Tipos que ainda podem ser pedidos — os que não têm linha."""
    existentes = {c.tipo for c in contratacao.chamados.all()}
    return [t for t in ORDEM_TIPOS if t not in existentes]


# ─────────────────────────────────────────────────────────────
# Atalho do solicitante (RH): o que falta pedir, e o pedido já preenchido
# ─────────────────────────────────────────────────────────────

def _falta_pedir_dos(contratacao, tipos_existentes) -> bool:
    """A vaga ainda DEVE um pedido à TI? A régua é a mesma da validação: a
    máquina é o pedido obrigatório; só quando a vaga declara que não precisa de
    computador é que "nada pedido ainda" passa a ser a pendência."""
    if contratacao.precisa_computador:
        return Tipo.MAQUINA not in tipos_existentes
    return not tipos_existentes


def falta_pedir(contratacao) -> bool:
    """`True` quando a vaga em aberto ainda não fez o pedido que a TI espera."""
    return pode_solicitar(contratacao) and _falta_pedir_dos(
        contratacao, {c.tipo for c in contratacao.chamados.all()},
    )


# Situação do pedido de estrutura de UMA vaga, na linguagem do quadro. Mesma
# honestidade dos quatro estados do detalhe: "solicitada" quer dizer que o
# sistema pediu — nunca que o chamado foi aberto.
_SITUACAO_ESTRUTURA = {
    "falha": {"rotulo": "Falha no envio à TI", "tom": "perigo", "acao": "reenviar"},
    "nao_solicitada": {"rotulo": "Estrutura não solicitada", "tom": "aviso", "acao": "pedir"},
    "enviando": {"rotulo": "Envio à TI em andamento", "tom": "aviso", "acao": ""},
    "solicitada": {"rotulo": "Estrutura solicitada", "tom": "ok", "acao": ""},
}


def situacao_de_estrutura(vagas) -> dict:
    """`{pk: {estado, rotulo, tom, acao}}` para as vagas EM ABERTO de `vagas`.

    UMA consulta para qualquer quantidade de vagas — é o que o quadro usa para
    pôr o selo em cada cartão e montar a fila "pedir estrutura". Vaga que não
    admite pedido (fora de "Em aberto") não entra: o selo não prometeria uma
    ação que o service recusaria.

    `acao` diz o que o atalho oferece: `pedir` (nada foi pedido ainda),
    `reenviar` (há envio que falhou ou foi interrompido) ou vazio (nada a fazer).
    A falha vence: um e-mail que não saiu é o que mais precisa de olho.
    """
    vagas = [v for v in vagas if pode_solicitar(v)]
    if not vagas:
        return {}

    por_vaga: dict[int, list] = {}
    # `updated_at` entra no `only` porque `reenviavel`/`interrompido` o leem —
    # sem ele cada chamado dispararia uma consulta para buscá-lo.
    for c in ContratacaoChamado.objects.filter(
        contratacao_id__in=[v.pk for v in vagas],
    ).only("contratacao_id", "tipo", "status", "updated_at"):
        por_vaga.setdefault(c.contratacao_id, []).append(c)

    situacoes = {}
    for vaga in vagas:
        chamados = por_vaga.get(vaga.pk, [])
        if any(c.reenviavel for c in chamados):
            estado = "falha"
        elif _falta_pedir_dos(vaga, {c.tipo for c in chamados}):
            estado = "nao_solicitada"
        elif any(c.status == StatusChamado.PENDENTE for c in chamados):
            estado = "enviando"
        else:
            estado = "solicitada"
        situacoes[vaga.pk] = {"estado": estado, **_SITUACAO_ESTRUTURA[estado]}
    return situacoes


def pedido_sugerido(contratacao) -> dict:
    """O que a janela já abre preenchido — para o solicitante só conferir.

    Sai do que a PRÓPRIA vaga diz, nunca de um padrão fixo, e só para o que
    ainda não foi pedido:

      • máquina: a justificativa/configuração que a vaga já tem;
      • usuário e e-mail: ligado só quando a vaga JÁ TEM contratado — sem nome,
        a TI não tem o que criar, e as caixas "usuário de rede"/"e-mail" da vaga
        nascem marcadas por padrão (não são uma escolha de quem preencheu);
      • sistemas: ligado quando a vaga marca algum sistema ou licença, com
        "liberação de acesso" sugerida — é o que a lista da vaga pede.

    É sugestão, não decisão: a janela continua mostrando o resumo, e o
    solicitante desmarca o que não quer antes de confirmar. O service revalida
    tudo no POST.
    """
    inicial = {
        "maquina_descricao": contratacao.computador_justificativa,
        "criar_usuario_rede": contratacao.usuario_rede,
        "criar_email": contratacao.email_corporativo,
    }
    pendentes = tipos_pendentes(contratacao)

    if Tipo.USUARIO_EMAIL in pendentes and contratacao.nome_contratado:
        inicial["usuario_email"] = True

    if Tipo.SISTEMAS in pendentes and (
        any(s.necessario for s in contratacao.sistemas.all())
        or contratacao.licencas_necessarias.all()
    ):
        inicial["sistemas"] = True
        inicial["sistemas_suporte"] = [SuporteSistemasChoices.LIBERACAO]
    return inicial


# ─────────────────────────────────────────────────────────────
# O que a TI recebe
# ─────────────────────────────────────────────────────────────

def montar_dados(contratacao, tipo, entrada: dict, user) -> dict:
    """O SNAPSHOT do chamado: só o que a atividade precisa, congelado agora.

    Congelar importa: o reenvio de uma falha manda o que o solicitante
    CONFIRMOU, não o que a vaga passou a dizer depois. E o que entra é mínimo —
    sem CPF, telefone ou endereço (a vaga nem os guarda). O nome do contratado
    só vai no chamado de usuário/e-mail, onde o login precisa dele; o e-mail e o
    nome do solicitante vão porque a TI precisa saber com quem falar.
    """
    cc = contratacao.centro_custo
    departamento = f"{cc.departamento} (CC {cc.numero})" if cc else ""
    dados = {
        "tipo": tipo,
        "vaga_id": contratacao.pk,
        "cargo": contratacao.cargo,
        "funcao": contratacao.funcao.nome if contratacao.funcao_id else "",
        "departamento": departamento,
        "local": contratacao.localidade.local if contratacao.localidade_id else "",
        "tipo_vaga": contratacao.get_tipo_vaga_display(),
        "previsao_inicio": (
            contratacao.data_prevista_inicio.strftime("%d/%m/%Y")
            if contratacao.data_prevista_inicio else ""
        ),
        "solicitante": user.get_full_name() or user.get_username(),
        "solicitante_email": user.email or "",
    }

    pedidos = [
        s for s in contratacao.sistemas.select_related("sistema").all() if s.necessario
    ]

    if tipo == Tipo.MAQUINA:
        telefonia = ""
        if contratacao.telefonia != TipoTelefoniaChoices.NENHUM:
            telefonia = contratacao.get_telefonia_display()
            if contratacao.ramal_sugerido:
                telefonia += f" (ramal sugerido {contratacao.ramal_sugerido})"
        dados.update(
            computador=contratacao.get_computador_display(),
            telefonia=telefonia,
            descricao=(entrada.get("descricao") or "").strip(),
        )
    elif tipo == Tipo.USUARIO_EMAIL:
        acessos = [s.sistema.nome for s in pedidos]
        if contratacao.acesso_vpn:
            acessos.append("Acesso VPN")
        if contratacao.acesso_impressora:
            acessos.append("Acesso a impressora")
        dados.update(
            nome_contratado=contratacao.nome_contratado,
            criar_usuario_rede=bool(entrada.get("criar_usuario_rede")),
            criar_email=bool(entrada.get("criar_email")),
            email_sugerido=contratacao.email_sugerido,
            usuario_espelho=contratacao.perfil_espelho,
            acessos=acessos,
            observacao=(entrada.get("observacao") or "").strip(),
        )
    else:  # Tipo.SISTEMAS
        dados.update(
            sistemas=[
                s.sistema.nome + (f" (espelho: {s.perfil_espelho})" if s.perfil_espelho else "")
                for s in pedidos
            ],
            licencas=[
                lic.licenca.nome + (f" (espelho: {lic.perfil_espelho})" if lic.perfil_espelho else "")
                for lic in contratacao.licencas_necessarias.select_related("licenca").all()
            ],
            suporte=list(entrada.get("suporte") or []),
            observacao=(entrada.get("observacao") or "").strip(),
        )
    return dados


def _validar_selecao(contratacao, selecao: dict, existentes: set) -> None:
    """As regras do que se pode pedir. Levanta `SolicitacaoRecusada`.

    Roda DENTRO da transação, com a vaga já travada: o que se valida é o estado
    de agora, não o que a tela mostrava quando foi aberta.
    """
    if not selecao:
        raise SolicitacaoRecusada("Selecione ao menos um chamado para enviar.")

    # Máquina é obrigatória — exceto quando a vaga declara que NÃO precisa de
    # computador, caso em que forçar o chamado seria abrir um pedido sem objeto.
    if (
        Tipo.MAQUINA not in existentes
        and contratacao.precisa_computador
        and Tipo.MAQUINA not in selecao
    ):
        raise SolicitacaoRecusada(
            "A configuração e entrega de máquina é obrigatória nesta solicitação."
        )

    if Tipo.USUARIO_EMAIL in selecao:
        e = selecao[Tipo.USUARIO_EMAIL]
        if not (e.get("criar_usuario_rede") or e.get("criar_email")):
            raise SolicitacaoRecusada(
                "Criação de usuário e e-mail: marque o que precisa ser criado (usuário de "
                "rede e/ou e-mail). Se não há nada a criar, desligue este chamado."
            )

    if Tipo.SISTEMAS in selecao:
        e = selecao[Tipo.SISTEMAS]
        if not e.get("suporte"):
            raise SolicitacaoRecusada(
                "Orientação e uso dos sistemas: marque o tipo de suporte necessário. "
                "Se não há o que pedir, desligue este chamado."
            )
        tem_sistema = (
            contratacao.sistemas.filter(necessario=True).exists()
            or contratacao.licencas_necessarias.exists()
        )
        if not tem_sistema and not (e.get("observacao") or "").strip():
            raise SolicitacaoRecusada(
                "Orientação e uso dos sistemas: a vaga não tem sistema marcado — descreva "
                "nas observações quais sistemas a pessoa vai usar."
            )


# ─────────────────────────────────────────────────────────────
# Escrita
# ─────────────────────────────────────────────────────────────

def _exigir_capacidade(user) -> None:
    # Terceira camada de autorização (as outras são o middleware e o decorador
    # da view): o service recusa por si, para uma chamada vinda de outro lugar
    # não contornar a régua.
    if not ar.pode_solicitar_estrutura(user):
        raise PermissionDenied("Seu acesso não permite solicitar a estrutura de uma vaga.")


def solicitar(*, contratacao_pk: int, user, selecao: dict) -> ResultadoSolicitacao:
    """Registra e envia os chamados selecionados. `selecao` é
    `{tipo: detalhes}` (ver `ContratacaoChamadoForm.selecao`).

    Seguro contra repetição: tipo que já tem linha é pulado e devolvido em
    `ja_solicitados` — nunca reenviado. Os demais seguem normalmente.
    """
    _exigir_capacidade(user)

    novos = []
    resultado = ResultadoSolicitacao()

    with transaction.atomic():
        # `select_for_update` serializa quem pede ao mesmo tempo em bancos que
        # o suportam. No SQLite é ignorado — quem garante, ali, é a constraint
        # única, tratada logo abaixo.
        vaga = (
            Contratacao.objects.select_for_update()
            .select_related("centro_custo", "localidade", "funcao", "colaborador")
            .get(pk=contratacao_pk)
        )
        if not pode_solicitar(vaga):
            raise SolicitacaoRecusada(_motivo_bloqueio(vaga))

        existentes = set(vaga.chamados.values_list("tipo", flat=True))
        _validar_selecao(vaga, selecao, existentes)

        for tipo in ORDEM_TIPOS:
            if tipo not in selecao:
                continue
            if tipo in existentes:
                resultado.ja_solicitados.append(tipo)
                continue
            try:
                # Savepoint próprio: o IntegrityError de uma corrida não pode
                # envenenar a transação inteira e derrubar os outros tipos.
                with transaction.atomic():
                    chamado = ContratacaoChamado.objects.create(
                        contratacao=vaga, tipo=tipo, status=StatusChamado.PENDENTE,
                        dados=montar_dados(vaga, tipo, selecao[tipo], user),
                        criado_por=user, atualizado_por=user,
                    )
            except IntegrityError:
                resultado.ja_solicitados.append(tipo)
                continue
            novos.append(chamado)
            registrar_evento(
                vaga, "chamado", f"Chamado solicitado: {Tipo(tipo).label}.", user,
            )

    # Fora da transação: SMTP lento não pode segurar lock do banco.
    for chamado in novos:
        _tentar_envio(chamado, user)
        (resultado.enviados if chamado.status == StatusChamado.ENVIADO else resultado.falhas).append(chamado)
    return resultado


def reenviar(*, contratacao_pk: int, chamado_pk: int, user) -> ContratacaoChamado:
    """Nova tentativa de um chamado que FALHOU (ou cujo envio foi interrompido).

    O que já foi enviado com sucesso é recusado: reenviar duplicaria o chamado
    na caixa da TI. A vaga só precisa não estar encerrada — preencher a vaga não
    impede de retomar um pedido que não chegou (ver `pode_acompanhar_envio`).

    A posse da tentativa é decidida por um UPDATE condicional (compare-and-swap):
    dois cliques simultâneos fazem dois UPDATEs, e só um encontra o estado
    esperado — o outro recebe "já está sendo reenviado".
    """
    _exigir_capacidade(user)

    with transaction.atomic():
        vaga = Contratacao.objects.select_for_update().get(pk=contratacao_pk)
        if not pode_acompanhar_envio(vaga):
            raise SolicitacaoRecusada(
                f"Esta vaga está “{vaga.get_status_display()}” — o envio não pode mais ser retomado."
            )

        chamado = ContratacaoChamado.objects.select_for_update().get(
            pk=chamado_pk, contratacao_id=contratacao_pk,
        )
        if not chamado.reenviavel:
            if chamado.status == StatusChamado.ENVIADO:
                raise SolicitacaoRecusada(
                    "Este chamado já foi enviado com sucesso — reenviar o duplicaria."
                )
            raise SolicitacaoRecusada("Este chamado está sendo enviado agora. Aguarde o resultado.")

        agora = timezone.now()
        alvo = ContratacaoChamado.objects.filter(pk=chamado.pk)
        if chamado.status == StatusChamado.FALHA:
            pegou = alvo.filter(status=StatusChamado.FALHA).update(
                status=StatusChamado.PENDENTE, updated_at=agora, atualizado_por=user,
            )
        else:  # interrompido: o carimbo antigo é a "versão" esperada
            pegou = alvo.filter(
                status=StatusChamado.PENDENTE, updated_at=chamado.updated_at,
            ).update(updated_at=agora, atualizado_por=user)
        if not pegou:
            raise SolicitacaoRecusada("Este chamado já está sendo reenviado. Aguarde o resultado.")
        chamado.refresh_from_db()

    _tentar_envio(chamado, user)
    return chamado


_SITUACAO_DA_RESERVA = {
    "ativa": ("Reservado", "roxo"),
    "entregue": ("Entregue", "ok"),
    "cedida": ("Pendente de nova reserva", "aviso"),
}


def contexto_do_email(chamado) -> dict:
    """O retrato da vaga NO MOMENTO do envio — o que o snapshot do chamado não
    tem como saber: o nome de quem foi contratado, os equipamentos reservados e o
    que ainda está pendente. Um reenvio semanas depois mostra a situação de hoje,
    e não a da época do pedido.

    Nunca levanta: falha ao montar o contexto não pode impedir o e-mail de sair
    (ele sai com o que o snapshot tem)."""
    from django.urls import reverse
    from services import email_layout as layout
    from services import contratacao_service as cs
    from services import reserva_equipamento_service as reservas

    contexto = {"protocolo": f"CH-{chamado.pk:05d}"}
    try:
        vaga = chamado.contratacao
        contexto["link"] = layout.link_absoluto(
            reverse("chamado_detail", args=[vaga.pk, chamado.pk])
        )
        contexto["colaborador"] = vaga.nome_contratado
        equipamentos = []
        for r in reservas.reservas_da_vaga(vaga):
            if r.estado not in _SITUACAO_DA_RESERVA:
                continue
            if r.estado == "cedida" and r.substituida_por_id:
                continue
            rotulo, tom = _SITUACAO_DA_RESERVA[r.estado]
            equipamentos.append({
                "equipamento": (r.item.subtipo.nome + " — " if r.item.subtipo_id else "") + (r.item.nome or "—"),
                "identificacao": f"Série {r.item.numero_serie}" if r.item.numero_serie else "",
                "situacao": rotulo, "tom": tom,
            })
        contexto["equipamentos"] = equipamentos
        if chamado.tipo == Tipo.SISTEMAS:
            contexto["pendencias"] = [
                e.detalhe for e in cs.etapas_do_processo(vaga)
                if e.pendente and e.chave in (cs.CADASTRO, cs.USUARIO_EMAIL, cs.SISTEMAS)
            ]
    except Exception:  # noqa: BLE001
        logger.exception("chamado %s: erro ao montar o contexto do e-mail", chamado.pk)
    return contexto


def _tentar_envio(chamado, user) -> None:
    """Uma tentativa de envio + o registro dela. Nunca propaga erro: qualquer
    falha (SMTP, canal desligado, defeito ao montar a mensagem) vira uma linha
    de histórico e o estado FALHA, para o próximo chamado seguir."""
    from services import email_alertas

    numero = chamado.tentativas + 1
    assunto = chamado.assunto
    mensagem = None
    try:
        # A abertura é a PRIMEIRA mensagem da conversa: guarda o Message-ID que
        # vai no e-mail, e é nele que a resposta da TI se prende. Reenvio reusa a
        # mesma linha (e o mesmo Message-ID).
        mensagem = conversa.garantir_mensagem_abertura(chamado, user)
        assunto, resultado = email_alertas.enviar_chamado_contratacao(
            chamado.tipo, chamado.dados,
            solicitado_em=chamado.created_at, tentativa=numero,
            message_id=mensagem.message_id,
            referencia=conversa.referencia_do_chamado(chamado.pk),
            **contexto_do_email(chamado),
        )
    except Exception as exc:  # noqa: BLE001 — a regra é justamente não deixar escapar
        logger.exception("chamado %s: erro ao preparar/enviar", chamado.pk)
        resultado = email_alertas.ResultadoEnvio(False, f"Erro ao preparar a mensagem: {str(exc)[:200]}")

    destinatarios = ", ".join(resultado.destinatarios)
    rotulo = Tipo(chamado.tipo).label
    agora = timezone.now()
    with transaction.atomic():
        ContratacaoChamadoTentativa.objects.create(
            chamado=chamado, sucesso=resultado.ok, assunto=assunto,
            destinatarios=destinatarios, erro=resultado.motivo,
            criado_por=user, atualizado_por=user,
        )
        chamado.tentativas = numero
        chamado.assunto = assunto
        chamado.destinatarios = destinatarios
        chamado.atualizado_por = user
        chamado.ultima_atividade_em = agora
        if resultado.ok:
            chamado.status = StatusChamado.ENVIADO
            chamado.enviado_em = agora
            chamado.ultimo_erro = ""
        else:
            chamado.status = StatusChamado.FALHA
            chamado.ultimo_erro = resultado.motivo
        chamado.save(update_fields=[
            "tentativas", "assunto", "destinatarios", "atualizado_por", "status",
            "enviado_em", "ultimo_erro", "ultima_atividade_em", "updated_at",
        ])
        if mensagem is not None:
            conversa.registrar_resultado_abertura(chamado, mensagem, resultado, assunto, user, numero)
        if resultado.ok:
            registrar_evento(
                chamado.contratacao, "chamado",
                f"E-mail do chamado “{rotulo}” enviado ({len(resultado.destinatarios)} destinatário(s)).",
                user,
            )
        else:
            registrar_evento(
                chamado.contratacao, "chamado",
                f"Falha no envio do e-mail do chamado “{rotulo}” (tentativa {numero}).", user,
            )


def registrar_identificador(*, contratacao_pk: int, chamado_pk: int, identificador: str, user) -> ContratacaoChamado:
    """O TI informa o número do chamado aberto no sistema de atendimento — a
    única forma de o Controle TI saber que o chamado EXISTE. Vazio limpa o campo
    (corrigir um número errado). Só vale para chamado cujo e-mail foi enviado."""
    if not ar.pode_gerenciar_contratacao(user):
        raise PermissionDenied("Só o TI registra o número do chamado.")

    identificador = (identificador or "").strip()
    if len(identificador) > 60:
        raise SolicitacaoRecusada("O número do chamado tem no máximo 60 caracteres.")

    with transaction.atomic():
        chamado = ContratacaoChamado.objects.select_for_update().select_related("contratacao").get(
            pk=chamado_pk, contratacao_id=contratacao_pk,
        )
        if chamado.status != StatusChamado.ENVIADO:
            raise SolicitacaoRecusada(
                "O número só pode ser registrado depois que o e-mail do chamado foi enviado."
            )
        chamado.identificador = identificador
        chamado.identificador_em = timezone.now() if identificador else None
        chamado.atualizado_por = user
        chamado.save(update_fields=["identificador", "identificador_em", "atualizado_por", "updated_at"])
        # Registrar o nº é a prova de que o chamado EXISTE no atendimento: sai de
        # "Aguardando atendimento" para "Aberto". Só se ainda estava lá — uma TI que
        # já pôs o chamado "Em andamento" não volta para trás por corrigir um número.
        if identificador and chamado.atendimento == AtendimentoChamadoChoices.AGUARDANDO_ATENDIMENTO:
            conversa.aplicar_mudanca(
                chamado, AtendimentoChamadoChoices.ABERTO, user=user, automatico=True,
                motivo=f"Nº do chamado registrado: {identificador}.",
            )
        rotulo = Tipo(chamado.tipo).label
        registrar_evento(
            chamado.contratacao, "chamado",
            (f"Chamado “{rotulo}” registrado no atendimento sob o nº {identificador}."
             if identificador else f"Nº do chamado “{rotulo}” removido."),
            user,
        )
    return chamado
