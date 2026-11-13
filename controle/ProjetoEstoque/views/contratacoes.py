"""
Contratação de colaborador (admissão / onboarding de TI).

Telas:
  /contratacoes/                 painel com KPIs + colunas por etapa
  /contratacoes/nova/            formulário da vaga
  /contratacoes/<pk>/            detalhe + checklist de provisionamento + linha do tempo
  /contratacoes/<pk>/editar/     mesmo formulário
  /contratacoes/<pk>/excluir/
  /contratacoes/<pk>/chamados/   pede à TI, por e-mail, a estrutura da vaga (máquina,
                                 usuário/e-mail, sistemas) + reenvio + nº do chamado
  /contratacoes/sistemas/        cadastro dos sistemas que o formulário pergunta

Toda regra de etapa/provisionamento vive em `services/contratacao_service.py`
— a view só valida o formulário, chama o service e escolhe a mensagem.
"""

from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Count, Prefetch
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from services import caixa_graph_service
from services import chamado_anexo_service as anexos_svc
from services import chamado_contratacao_service as chamados_svc
from services import chamado_conversa_service as conversa_svc
from services import chamado_email_entrada_service as entrada_svc
from services import contratacao_service as cs
from services import reserva_equipamento_service as reservas_svc
from services.acesso_restrito_service import (
    exige_acompanhar_chamados,
    exige_concluir_contratacao,
    exige_gestao_contratacao,
    exige_preencher_vaga,
    exige_solicitar_estrutura,
    perfis_de,
    pode_concluir_contratacao,
    pode_gerenciar_contratacao,
    pode_preencher_vaga,
    pode_solicitar_estrutura,
)

from ..forms import (
    ContratacaoCadastroColaboradorForm,
    ContratacaoChamadoForm,
    ContratacaoCorrecaoForm,
    ContratacaoEntregaForm,
    ContratacaoForm,
    ContratacaoMotivoForm,
    ContratacaoPreenchimentoForm,
    ContratacaoReaberturaForm,
    ContratacaoVagaForm,
    SistemaCorporativoForm,
)
from ..models import (
    AtendimentoChamadoChoices,
    CentroCusto,
    ChamadoAnexo,
    ChamadoCaixaEstado,
    ChamadoEmailRecebido,
    ChamadoMensagem,
    Contratacao,
    ContratacaoChamado,
    ContratacaoChamadoTentativa,
    ContratacaoLicenca,
    ContratacaoSistema,
    ContratacaoVagaHistorico,
    Item,
    Localidade,
    ReservaEquipamento,
    SistemaCorporativo,
    StatusChamadoContratacao,
    StatusContratacaoChoices,
    TipoChamadoContratacao,
    Usuario,
)


# Quantas vagas a fila "pedir estrutura" mostra de uma vez. A fila é um atalho,
# não uma segunda listagem: o resto fica no quadro, com o selo no cartão.
FILA_ESTRUTURA_LIMITE = 5


# ─────────────────────────────────────────────────────────────
# Painel
# ─────────────────────────────────────────────────────────────

@login_required
def contratacao_painel(request):
    filtros = {
        "q": request.GET.get("q", "").strip(),
        "centro_custo": request.GET.get("centro_custo", "").strip(),
        "localidade": request.GET.get("localidade", "").strip(),
    }
    painel = cs.painel(filtros=filtros)

    # As pendências e o placar são do card, não da consulta: o template não
    # pode chamar função com argumento, então vêm anexados aqui.
    for coluna in painel["colunas"]:
        for card in coluna["cards"]:
            etapas = cs.etapas_do_processo(card)
            card.lista_pendencias = cs.pendencias_das(etapas)
            card.processo = cs.resumo_processo(etapas)
            card.placar = cs.resumo_provisionamento(card)

    # Atalho de quem PEDE (o RH): selo "estrutura" em cada vaga em aberto e a
    # fila do que ainda não foi pedido, logo abaixo do cabeçalho — é a primeira
    # tela do Portal de Contratações. Uma consulta para todos os cartões.
    fila_estrutura = []
    if pode_solicitar_estrutura(request.user):
        cartoes_abertos = [
            card
            for coluna in painel["colunas"]
            if coluna["chave"] == StatusContratacaoChoices.ABERTA
            for card in coluna["cards"]
        ]
        situacoes = chamados_svc.situacao_de_estrutura(cartoes_abertos)
        for card in cartoes_abertos:
            card.estrutura = situacoes.get(card.pk)
            if card.estrutura and card.estrutura["acao"]:
                fila_estrutura.append(card)

    # Vaga já preenchida: quantos chamados ainda estão com a TI. Uma consulta para
    # todos os cartões — a pergunta do RH depois de fechar o processo seletivo.
    cartoes_adiante = [
        card
        for coluna in painel["colunas"]
        if coluna["chave"] in (
            StatusContratacaoChoices.VAGA_PREENCHIDA, StatusContratacaoChoices.EQUIPAMENTO_ENTREGUE,
        )
        for card in coluna["cards"]
    ]
    pendentes_ti = conversa_svc.pendencias_de_ti(cartoes_adiante)
    for card in cartoes_adiante:
        card.chamados_ti = pendentes_ti.get(card.pk, 0)

    return render(request, "front/contratacao/contratacao_painel.html", {
        "painel": painel,
        "filtros": filtros,
        "centros_custo": CentroCusto.objects.order_by("numero"),
        "localidades": Localidade.objects.order_by("local"),
        "tem_sistemas": SistemaCorporativo.objects.filter(ativo=True).exists(),
        "fila_estrutura": fila_estrutura[:FILA_ESTRUTURA_LIMITE],
        "fila_estrutura_total": len(fila_estrutura),
        "fila_estrutura_resto": max(len(fila_estrutura) - FILA_ESTRUTURA_LIMITE, 0),
    })


# ─────────────────────────────────────────────────────────────
# CRUD da contratação
# ─────────────────────────────────────────────────────────────

def _usuarios_para_espelho():
    """Nomes que alimentam o <datalist> do campo "usuário espelho" — um só
    para as 39 linhas do catálogo de acessos (ver `_campo_de_espelho` no
    forms.py)."""
    return (
        Usuario.objects.filter(status="ativo")
        .order_by("nome").values_list("nome", flat=True)
    )


@login_required
def contratacao_create(request):
    if request.method == "POST":
        form = ContratacaoForm(request.POST)
        if form.is_valid():
            obj = cs.criar(form=form, user=request.user)
            messages.success(request, "Contratação aberta com sucesso!")
            # Quem entra pelo Portal de Contratações abriu a vaga para pedir a
            # estrutura: a janela já vem aberta, sem caça ao botão. Para o TI
            # não — quem abre a vaga ali costuma ser quem vai atendê-la, e uma
            # janela de pedido a cada vaga criada seria ruído.
            if "contratacao" in perfis_de(request.user) and pode_solicitar_estrutura(request.user):
                return redirect(f"{reverse('contratacao_detail', args=[obj.pk])}?solicitar=1")
            return redirect("contratacao_detail", pk=obj.pk)
        messages.error(request, "Verifique os campos destacados.")
    else:
        form = ContratacaoForm()

    return render(request, "front/contratacao/contratacao_form.html", {
        "form": form, "editar": False,
        "usuarios_espelho": _usuarios_para_espelho(),
    })


@login_required
def contratacao_update(request, pk):
    obj = get_object_or_404(Contratacao, pk=pk)
    if request.method == "POST":
        form = ContratacaoForm(request.POST, instance=obj)
        if form.is_valid():
            cs.atualizar(contratacao=obj, form=form, user=request.user)
            messages.success(request, "Contratação atualizada com sucesso!")
            return redirect("contratacao_detail", pk=obj.pk)
        messages.error(request, "Verifique os campos destacados.")
    else:
        form = ContratacaoForm(instance=obj)

    return render(request, "front/contratacao/contratacao_form.html", {
        "form": form, "editar": True, "obj": obj,
        "usuarios_espelho": _usuarios_para_espelho(),
    })


def _contexto_chamados(request, obj, form_chamados, abrir_modal):
    """Tudo que o card "Estrutura e chamados" e a janela de confirmação usam.

    Separado do `contratacao_detail` porque a MESMA tela é renderizada de novo
    quando a solicitação é recusada (janela aberta, com o que a pessoa digitou).
    """
    pode_pedir = pode_solicitar_estrutura(request.user)
    vaga_aceita = chamados_svc.pode_solicitar(obj)
    pendentes = chamados_svc.tipos_pendentes(obj)
    linhas = chamados_svc.painel_de_chamados(obj)
    pode_abrir_janela = pode_pedir and vaga_aceita and bool(pendentes)

    ctx = {
        "chamados": linhas,
        "chamados_pode_solicitar": pode_abrir_janela,
        # "Tentar novamente" não depende de haver tipo pendente: vale para o
        # que falhou, desde que a pessoa possa pedir e a vaga siga em aberto.
        "chamados_pode_reenviar": pode_pedir and chamados_svc.pode_acompanhar_envio(obj),
        # A pessoa pode pedir, mas a vaga já saiu de "Em aberto": explicar por
        # que o botão não está lá, em vez de sumir sem dizer nada.
        "chamados_bloqueio_status": pode_pedir and not vaga_aceita and not obj.encerrada,
        "chamados_todos_solicitados": not pendentes,
        "abrir_modal_chamados": abrir_modal,
        "modal_chamados": None,
        "form_chamados": None,
    }
    if pode_abrir_janela:
        # Só calcula (3 consultas de canal) quando a janela vai existir.
        from services.email_alertas import situacao_canal_chamado

        por_tipo = {linha["tipo"]: linha for linha in linhas}
        ctx["modal_chamados"] = {
            tipo: {
                "rotulo": por_tipo[tipo]["rotulo"],
                "chamado": por_tipo[tipo]["chamado"],
                "obrigatorio": por_tipo[tipo]["obrigatorio"],
                "canal": situacao_canal_chamado(tipo) if tipo in pendentes else None,
            }
            for tipo in chamados_svc.ORDEM_TIPOS
        }
        # Uma solicitação recusada volta com o que a pessoa digitou (`form_chamados`);
        # só a primeira abertura leva o pedido já preenchido pela vaga.
        ctx["form_chamados"] = form_chamados or ContratacaoChamadoForm(
            initial=chamados_svc.pedido_sugerido(obj),
        )
        ctx["chamados_falta_pedir"] = chamados_svc.falta_pedir(obj)
    return ctx


def _inicial_do_contratado(obj) -> dict:
    """O contratado como a vaga o tem agora — é o que a janela de correção abre."""
    return {
        "colaborador": obj.colaborador_id,
        "colaborador_nome": obj.colaborador_nome,
        "colaborador_email": obj.colaborador_email,
        "colaborador_telefone": obj.colaborador_telefone,
        "data_admissao": obj.data_admissao,
        "colaborador_observacoes": obj.colaborador_observacoes,
    }


_ROTULO_DO_CAMPO = (
    ("colaborador_nome", "Nome"),
    ("colaborador_email", "E-mail"),
    ("colaborador_telefone", "Telefone"),
    ("data_admissao", "Data de admissão"),
    ("colaborador_observacoes", "Observações"),
)


def _historico_para_tela(registros) -> list[dict]:
    """Linhas prontas do histórico do preenchimento: o template não calcula
    diferença nem traduz status. Correção mostra só o que MUDOU; reabertura
    mostra quem era o contratado que saiu da vaga."""
    saida = []
    for r in registros:
        mudancas = []
        if r.acao == ContratacaoVagaHistorico.Acao.CORRECAO:
            for campo, rotulo in _ROTULO_DO_CAMPO:
                antes, depois = r.antes.get(campo, ""), r.depois.get(campo, "")
                if antes != depois:
                    mudancas.append((rotulo, antes or "—", depois or "—"))
        anterior = ""
        if r.acao == ContratacaoVagaHistorico.Acao.REABERTURA:
            anterior = r.antes.get("colaborador_nome", "")
        rotulo_status = lambda v: StatusContratacaoChoices(v).label if v else ""  # noqa: E731
        saida.append({
            "registro": r,
            "status_antes": rotulo_status(r.status_antes),
            "status_depois": rotulo_status(r.status_depois),
            "mudancas": mudancas,
            "contratado_anterior": anterior,
        })
    return saida


def _render_detalhe(request, pk, *, form_chamados=None, abrir_modal=False,
                    forms_vaga=None, abrir_dialogo=""):
    obj = get_object_or_404(
        cs.queryset_base().prefetch_related(
            "sistemas__sistema__licenca", "licencas_necessarias__licenca", "eventos",
            "reservas__item__subtipo", "etapas_registro__criado_por",
            Prefetch(
                "chamados",
                queryset=ContratacaoChamado.objects.select_related("criado_por", "responsavel")
                .annotate(n_mensagens=Count("mensagens", distinct=True)).prefetch_related(
                    Prefetch(
                        "historico",
                        queryset=ContratacaoChamadoTentativa.objects.select_related("criado_por"),
                    ),
                ),
            ),
        ),
        pk=pk,
    )
    sistemas = list(obj.sistemas.all())
    sistemas_pedidos = [s for s in sistemas if s.necessario]
    licencas = list(obj.licencas_necessarias.all())

    contexto = {
        "obj": obj,
        "etapas": cs.ETAPAS,
        "etapa_atual": obj.status,
        # O detalhe mostra acesso e assento numa lista só, como o formulário
        # — o total vem pronto daqui porque o template não soma dois `length`.
        "total_acessos": len(sistemas_pedidos) + len(licencas),
        "pendencias": cs.pendencias(obj),
        "placar": cs.resumo_provisionamento(obj),
        "sistemas_pedidos": sistemas_pedidos,
        "sistemas_dispensados": [s for s in sistemas if not s.necessario],
        "licencas": licencas,
        "eventos": list(obj.eventos.all()[:40]),
        "form_entrega": ContratacaoEntregaForm(initial={"item": obj.item_entregue_id}),
        "status_choices": StatusContratacaoChoices,
    }

    # Preenchimento da vaga pelo RH: três janelas (preencher · corrigir · reabrir).
    # Um formulário recusado volta com o que a pessoa digitou (`forms_vaga`), e a
    # janela dele reabre (`abrir_dialogo`).
    forms_vaga = forms_vaga or {}
    contexto.update({
        "pode_preencher": pode_preencher_vaga(request.user),
        "form_preencher": forms_vaga.get("preencher") or ContratacaoPreenchimentoForm(
            prefix="preencher",
            initial={"colaborador": obj.colaborador_id, "colaborador_nome": obj.colaborador_nome},
        ),
        "form_corrigir": forms_vaga.get("corrigir") or ContratacaoCorrecaoForm(
            prefix="corrigir", initial=_inicial_do_contratado(obj),
        ),
        "form_reabrir": forms_vaga.get("reabrir") or ContratacaoReaberturaForm(prefix="reabrir"),
        "abrir_dialogo": abrir_dialogo,
        "historico_vaga": _historico_para_tela(obj.historico_vaga.select_related("criado_por")[:20]),
    })
    contexto["form_cadastro"] = forms_vaga.get("cadastro") or ContratacaoCadastroColaboradorForm(prefix="cadastro")
    contexto.update(_contexto_chamados(request, obj, form_chamados, abrir_modal))
    # O que a TI ainda tem a fazer nesta vaga — a pergunta do RH depois de preencher.
    resumo = conversa_svc.resumo_da_vaga(obj)
    contexto["chamados_resumo"] = resumo
    contexto["chamados_pendentes"] = resumo.pendentes
    contexto.update(_contexto_do_processo(request, obj, resumo))
    contexto.update(_contexto_das_reservas(request, obj))
    return render(request, "front/contratacao/contratacao_detail.html", contexto)


def _contexto_do_processo(request, obj, resumo_chamados) -> dict:
    """As nove etapas até "concluída" e os três eixos que o RH precisa ler
    separados: a situação da VAGA, a da INTEGRAÇÃO e a dos CHAMADOS de TI."""
    etapas = cs.etapas_do_processo(obj)
    faltas = cs.pendencias_das(etapas)
    em_andamento = obj.status in (
        StatusContratacaoChoices.VAGA_PREENCHIDA, StatusContratacaoChoices.EQUIPAMENTO_ENTREGUE,
    )
    return {
        "etapas_processo": etapas,
        "resumo_processo": cs.resumo_processo(etapas),
        "faltas_para_concluir": faltas,
        "integracao": cs.situacao_integracao(obj),
        "pode_concluir": pode_concluir_contratacao(request.user),
        "pode_concluir_agora": em_andamento and not faltas,
        "processo_em_andamento": em_andamento,
        "chamados_concluidos": resumo_chamados.total - resumo_chamados.pendentes,
        "chamados_total": resumo_chamados.total,
    }


def _contexto_das_reservas(request, obj) -> dict:
    """A reserva de equipamento da vaga: o que está reservado/entregue, o que foi
    liberado ou usado em outra necessidade, a pendência (se houver) e — só para o
    TI, com a vaga ainda aberta ao atendimento — o seletor de equipamentos em Backup."""
    pode_reservar = pode_gerenciar_contratacao(request.user) and not obj.encerrada
    ctx = {
        "reservas": reservas_svc.reservas_da_vaga(obj),
        "pendencia_equipamento": reservas_svc.pendencias_de_equipamento(obj),
        "pode_reservar": pode_reservar,
        "reposicao": None,
        "candidatos_reposicao": [],
        "form_motivo": ContratacaoMotivoForm(),
    }
    if not pode_reservar:
        return ctx

    tipo = (request.GET.get("tipo") or "").strip()[:40]
    busca = (request.GET.get("q") or "").strip()[:60]
    todos = request.GET.get("todos") == "1"
    elegiveis = reservas_svc.itens_elegiveis(tipo=tipo, busca=busca, todos_os_tipos=todos)
    ctx.update({
        "elegiveis": list(elegiveis[:40]),
        "elegiveis_total": elegiveis.count(),
        "filtro_tipo": tipo,
        "filtro_busca": busca,
        "filtro_todos": todos,
        "tipos_usuais": reservas_svc.TIPOS_USUAIS,
    })

    # `?reposicao=<n>` chega do aviso depois de um equipamento reservado ser usado em
    # outra necessidade: mostra os candidatos do mesmo tipo para repor a reserva.
    reposicao = request.GET.get("reposicao", "")
    if reposicao.isdigit():
        reserva = obj.reservas.filter(
            pk=int(reposicao), estado=ReservaEquipamento.Estado.CEDIDA, substituida_por__isnull=True,
        ).select_related("item", "item__subtipo").first()
        if reserva is not None:
            ctx["reposicao"] = reserva
            ctx["candidatos_reposicao"] = list(reservas_svc.candidatos_para_substituir(reserva)[:30])
    return ctx


@login_required
def contratacao_detail(request, pk):
    # `?solicitar=1` é o atalho (fila do painel, vaga recém-criada): abre a
    # janela de pedido. Só vale se a janela existe — vaga fora de "Em aberto",
    # tudo já pedido ou quem não pode pedir ficam com a tela normal.
    return _render_detalhe(request, pk, abrir_modal=request.GET.get("solicitar") == "1")


@login_required
@permission_required("ProjetoEstoque.delete_contratacao", raise_exception=True)
@exige_gestao_contratacao
def contratacao_delete(request, pk):
    obj = get_object_or_404(Contratacao, pk=pk)
    if request.method == "POST":
        # Vaga excluída sem contratação: o que estava reservado volta ao Backup
        # ANTES de a vaga sumir. As linhas da reserva ficam (a FK é SET_NULL e
        # guardam o rótulo da vaga): o histórico do equipamento sobrevive.
        with transaction.atomic():
            reservas_svc.liberar_da_vaga(
                obj, motivo_tipo=ReservaEquipamento.Motivo.EXCLUSAO,
                motivo="Vaga excluída", user=request.user,
            )
            obj.delete()
        messages.success(request, "Contratação excluída.")
        return redirect("contratacao_painel")
    return render(request, "front/contratacao/contratacao_confirm_delete.html", {"obj": obj})


# ─────────────────────────────────────────────────────────────
# Etapas
# ─────────────────────────────────────────────────────────────

@require_POST
@login_required
@exige_gestao_contratacao
def contratacao_etapa(request, pk):
    """Único endpoint de avanço de etapa — a ação vem no POST. Um endpoint
    por botão multiplicaria o mesmo bloco de permissão e redirecionamento."""
    obj = get_object_or_404(Contratacao, pk=pk)
    acao = request.POST.get("acao", "")

    if acao == "preencher_vaga":
        form = ContratacaoVagaForm(request.POST)
        if form.is_valid():
            cs.definir_contratado(
                contratacao=obj,
                colaborador=form.cleaned_data["colaborador"],
                nome=form.cleaned_data["colaborador_nome"],
                user=request.user,
            )
            messages.success(request, "Vaga marcada como preenchida.")
        else:
            messages.error(request, form.errors.as_text().replace("* __all__", "").strip())

    elif acao == "registrar_entrega":
        form = ContratacaoEntregaForm(request.POST)
        if form.is_valid():
            try:
                cs.registrar_entrega(
                    contratacao=obj,
                    item=form.cleaned_data["item"],
                    sem_equipamento=form.cleaned_data["sem_equipamento"],
                    user=request.user,
                )
            except cs.TransicaoRecusada as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Entrega registrada na vaga.")
        else:
            messages.error(request, form.errors.as_text().replace("* __all__", "").strip())

    elif acao == "concluir":
        ok, faltas = cs.concluir(contratacao=obj, user=request.user)
        if ok:
            messages.success(request, "Contratação concluída!")
        else:
            messages.warning(
                request,
                "Ainda não dá para concluir: " + "; ".join(faltas)
                + ". Resolva essas etapas ou, se não se aplicam a esta contratação, dispense-as com justificativa.",
            )

    elif acao == "cancelar":
        cs.cancelar(contratacao=obj, motivo=request.POST.get("motivo", ""), user=request.user)
        messages.success(request, "Contratação cancelada.")

    elif acao == "reabrir":
        cs.reabrir(contratacao=obj, user=request.user)
        messages.success(request, "Contratação reaberta.")

    else:
        messages.error(request, "Ação desconhecida.")

    return redirect("contratacao_detail", pk=obj.pk)


@require_POST
@login_required
@exige_gestao_contratacao
def contratacao_sistema_toggle(request, pk, linha_pk):
    linha = get_object_or_404(
        ContratacaoSistema.objects.select_related("sistema", "contratacao"),
        pk=linha_pk, contratacao_id=pk,
    )
    cs.alternar_sistema(linha=linha, user=request.user)
    estado = "criado" if linha.provisionado else "pendente"
    messages.success(request, f"{linha.sistema.nome}: acesso marcado como {estado}.")
    return redirect("contratacao_detail", pk=pk)


@require_POST
@login_required
@exige_gestao_contratacao
def contratacao_licenca_toggle(request, pk, linha_pk):
    linha = get_object_or_404(
        ContratacaoLicenca.objects.select_related("licenca", "contratacao"),
        pk=linha_pk, contratacao_id=pk,
    )
    cs.alternar_licenca(linha=linha, user=request.user)
    estado = "atribuído" if linha.provisionada else "pendente"
    messages.success(request, f"{linha.licenca.nome}: assento marcado como {estado}.")
    return redirect("contratacao_detail", pk=pk)


# ─────────────────────────────────────────────────────────────
# Preenchimento da vaga (RH)
# ─────────────────────────────────────────────────────────────

def _post_do_preenchimento(request, pk, *, chave, form_cls, executar, sucesso):
    """Esqueleto das três operações: valida o formulário, chama o service e, se
    a regra recusar ou o formulário vier inválido, devolve a MESMA tela com a
    janela reaberta e o que a pessoa digitou. A regra é do service; aqui só o
    formulário e a escolha da mensagem."""
    get_object_or_404(Contratacao, pk=pk)
    form = form_cls(request.POST, prefix=chave)
    if form.is_valid():
        try:
            executar(form.cleaned_data)
        except cs.TransicaoRecusada as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, sucesso)
            return redirect("contratacao_detail", pk=pk)
    else:
        messages.error(request, "Verifique os campos destacados.")
    return _render_detalhe(request, pk, forms_vaga={chave: form}, abrir_dialogo=chave)


@require_POST
@login_required
@exige_preencher_vaga
def contratacao_preencher(request, pk):
    """O RH marca a vaga como preenchida e registra o contratado. Não conclui
    nem encerra nenhum chamado de TI."""
    return _post_do_preenchimento(
        request, pk, chave="preencher", form_cls=ContratacaoPreenchimentoForm,
        executar=lambda dados: cs.preencher_vaga(contratacao_pk=pk, dados=dados, user=request.user),
        sucesso="Vaga marcada como preenchida. Os chamados de TI seguem o próprio andamento.",
    )


@require_POST
@login_required
@exige_preencher_vaga
def contratacao_colaborador_corrigir(request, pk):
    """Corrige os dados do contratado (com motivo, guardando o que havia antes)."""
    return _post_do_preenchimento(
        request, pk, chave="corrigir", form_cls=ContratacaoCorrecaoForm,
        executar=lambda dados: cs.corrigir_colaborador(
            contratacao_pk=pk, dados=dados, motivo=dados["motivo"], user=request.user,
        ),
        sucesso="Dados do contratado corrigidos.",
    )


@require_POST
@login_required
@exige_preencher_vaga
def contratacao_vaga_reabrir(request, pk):
    """Reabre uma vaga preenchida (com motivo). Os chamados de TI não são tocados."""
    return _post_do_preenchimento(
        request, pk, chave="reabrir", form_cls=ContratacaoReaberturaForm,
        executar=lambda dados: cs.reabrir_vaga(contratacao_pk=pk, motivo=dados["motivo"], user=request.user),
        sucesso="Vaga reaberta. Os dados do contratado anterior ficaram no histórico.",
    )


@require_POST
@login_required
@exige_preencher_vaga
def contratacao_colaborador_cadastrar(request, pk):
    """Cadastra no sistema o colaborador de uma vaga já preenchida, com os dados
    da própria vaga (só matrícula e área administrativa são complemento)."""
    return _post_do_preenchimento(
        request, pk, chave="cadastro", form_cls=ContratacaoCadastroColaboradorForm,
        executar=lambda dados: cs.cadastrar_colaborador(contratacao_pk=pk, dados=dados, user=request.user),
        sucesso="Colaborador cadastrado. As reservas de equipamento da vaga passaram a ser dele.",
    )


# ─────────────────────────────────────────────────────────────
# Processo até a conclusão: integração, etapas, conclusão
# ─────────────────────────────────────────────────────────────

def _voltar(pk, ancora=""):
    return redirect(reverse("contratacao_detail", args=[pk]) + ancora)


@require_POST
@login_required
@exige_concluir_contratacao
def contratacao_integracao(request, pk):
    """O RH (ou o TI) confirma — ou desfaz a confirmação — da integração do
    colaborador. Registra quem confirmou, quando e a observação."""
    get_object_or_404(Contratacao, pk=pk)
    try:
        if request.POST.get("acao") == "desfazer":
            form = ContratacaoMotivoForm(request.POST)
            if not form.is_valid():
                messages.error(request, "Informe o motivo para desfazer a confirmação da integração.")
                return _voltar(pk, "#processo")
            cs.desfazer_integracao(contratacao_pk=pk, motivo=form.cleaned_data["motivo"], user=request.user)
            messages.success(request, "Confirmação da integração desfeita.")
        else:
            cs.confirmar_integracao(
                contratacao_pk=pk, observacao=request.POST.get("observacao", ""), user=request.user,
            )
            messages.success(request, "Integração do colaborador confirmada.")
    except cs.TransicaoRecusada as exc:
        messages.error(request, str(exc))
    return _voltar(pk, "#processo")


@require_POST
@login_required
@exige_concluir_contratacao
def contratacao_concluir(request, pk):
    """Conclui a contratação — só com todas as etapas obrigatórias feitas ou
    dispensadas. O RH conclui; o TI também pode."""
    obj = get_object_or_404(Contratacao, pk=pk)
    ok, faltas = cs.concluir(contratacao=obj, user=request.user)
    if ok:
        messages.success(request, "Contratação concluída. O colaborador está cadastrado, equipado e integrado.")
    else:
        messages.warning(
            request,
            "Ainda não dá para concluir: " + "; ".join(faltas) + ".",
        )
    return _voltar(pk, "#processo")


@require_POST
@login_required
@exige_gestao_contratacao
def contratacao_etapa_processo(request, pk):
    """O TI dispensa uma etapa que não se aplica (com justificativa), registra a
    que não tem fato que a prove (orientações) ou desfaz um desses registros."""
    get_object_or_404(Contratacao, pk=pk)
    acao = request.POST.get("acao", "")
    chave = request.POST.get("chave", "")
    texto = (request.POST.get("motivo") or request.POST.get("observacao") or "").strip()
    try:
        if acao == "dispensar":
            cs.dispensar_etapa(contratacao_pk=pk, chave=chave, justificativa=texto, user=request.user)
            messages.success(request, "Etapa dispensada. A justificativa ficou registrada.")
        elif acao == "registrar":
            cs.registrar_etapa(contratacao_pk=pk, chave=chave, observacao=texto, user=request.user)
            messages.success(request, "Etapa registrada como feita.")
        elif acao == "desfazer":
            cs.desfazer_registro_de_etapa(contratacao_pk=pk, chave=chave, motivo=texto, user=request.user)
            messages.success(request, "Registro da etapa desfeito.")
        else:
            messages.error(request, "Ação desconhecida.")
    except cs.TransicaoRecusada as exc:
        messages.error(request, str(exc))
    return _voltar(pk, "#processo")


# ─────────────────────────────────────────────────────────────
# Reserva de equipamentos (TI)
# ─────────────────────────────────────────────────────────────

@require_POST
@login_required
@exige_gestao_contratacao
def contratacao_reservar(request, pk):
    """Reserva para a vaga os equipamentos em Backup que o TI marcou. Tudo ou
    nada: a disponibilidade é revalidada no service, com o equipamento travado."""
    get_object_or_404(Contratacao, pk=pk)
    try:
        criadas = reservas_svc.reservar(
            contratacao_pk=pk, item_ids=request.POST.getlist("item"), user=request.user,
            adicional=request.POST.get("adicional") == "1",
            substitui_pk=request.POST.get("substitui") or None,
        )
    except reservas_svc.ReservaRecusada as exc:
        messages.error(request, str(exc))
    except (ValueError, TypeError):
        messages.error(request, "Seleção de equipamento inválida.")
    else:
        messages.success(
            request,
            "Equipamento reservado: " + "; ".join(r.item_rotulo for r in criadas)
            + ". O status passou de Backup para Reservado — a entrega só acontece pela movimentação.",
        )
    return _voltar(pk, "#reservas")


@require_POST
@login_required
@exige_gestao_contratacao
def contratacao_reserva_liberar(request, pk, reserva_pk):
    """O TI desfaz uma reserva ativa (com motivo): o equipamento volta ao Backup."""
    reserva = get_object_or_404(ReservaEquipamento, pk=reserva_pk, contratacao_id=pk)
    form = ContratacaoMotivoForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Informe o motivo para liberar o equipamento.")
        return _voltar(pk, "#reservas")
    try:
        reservas_svc.liberar(reserva_pk=reserva.pk, motivo=form.cleaned_data["motivo"], user=request.user)
    except reservas_svc.ReservaRecusada as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Reserva liberada: {reserva.item_rotulo} voltou ao Backup.")
    return _voltar(pk, "#reservas")


@require_POST
@login_required
@exige_gestao_contratacao
def contratacao_reserva_alerta(request, pk, reserva_pk):
    """Manda de novo o alerta de "vaga sem equipamento" que NÃO saiu."""
    reserva = get_object_or_404(ReservaEquipamento, pk=reserva_pk, contratacao_id=pk)
    try:
        reservas_svc.reenviar_alerta(reserva_pk=reserva.pk, user=request.user)
    except reservas_svc.ReservaRecusada as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Alerta reenviado. O resultado aparece na própria pendência.")
    return _voltar(pk, "#reservas")


@require_POST
@login_required
@exige_gestao_contratacao
def equipamento_reservar(request, pk):
    """Pela ficha do EQUIPAMENTO (inventário): reserva este item, em Backup, para
    uma vaga escolhida. É o mesmo service da tela da vaga."""
    item = get_object_or_404(Item, pk=pk)
    vaga_pk = request.POST.get("contratacao", "")
    destino = reverse("equipamento_detalhe", args=[item.pk])
    if not vaga_pk.isdigit():
        messages.error(request, "Escolha a vaga para a qual reservar o equipamento.")
        return redirect(destino)
    try:
        reservas_svc.reservar(
            contratacao_pk=int(vaga_pk), item_ids=[item.pk], user=request.user,
            adicional=request.POST.get("adicional") == "1",
        )
    except reservas_svc.ReservaRecusada as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Equipamento reservado para a vaga. O status passou para Reservado.")
    return redirect(destino)


# ─────────────────────────────────────────────────────────────
# Chamados de estrutura (e-mail para a TI)
# ─────────────────────────────────────────────────────────────

def _rotulos(tipos):
    return ", ".join(TipoChamadoContratacao(t).label for t in tipos)


def _mensagens_da_solicitacao(request, r):
    """Uma mensagem por desfecho — e NENHUMA diz "chamado aberto": o que o
    sistema sabe é que o e-mail foi enviado."""
    if r.enviados:
        messages.success(
            request,
            "E-mail enviado com sucesso: "
            + ", ".join(c.get_tipo_display() for c in r.enviados)
            + ". Isto não confirma a abertura do chamado — ele só consta como aberto "
              "quando o TI registrar o número.",
        )
    if r.falhas:
        messages.warning(
            request,
            "Falha no envio: "
            + ", ".join(c.get_tipo_display() for c in r.falhas)
            + ". Os demais não foram afetados; use “Tentar novamente” só no que falhou.",
        )
    if r.ja_solicitados:
        messages.info(
            request,
            ("Já solicitado antes, sem novo envio: " if r.criados else
             "Estes chamados já tinham sido solicitados — nenhum e-mail foi reenviado: ")
            + _rotulos(r.ja_solicitados) + ".",
        )


@require_POST
@login_required
@exige_solicitar_estrutura
def contratacao_chamados_solicitar(request, pk):
    """Confirma a janela: registra e envia os chamados marcados. Toda a regra
    (vaga em aberto, máquina obrigatória, duplicidade) é do service; aqui só o
    formulário e a escolha da mensagem."""
    get_object_or_404(Contratacao, pk=pk)
    form = ContratacaoChamadoForm(request.POST)

    if form.is_valid():
        try:
            resultado = chamados_svc.solicitar(
                contratacao_pk=pk, user=request.user, selecao=form.selecao(),
            )
        except chamados_svc.SolicitacaoRecusada as exc:
            messages.error(request, str(exc))
        else:
            _mensagens_da_solicitacao(request, resultado)
            return redirect("contratacao_detail", pk=pk)
    else:
        messages.error(request, "Verifique os campos da solicitação.")

    # Recusada: a mesma tela, com a janela aberta e o que a pessoa digitou.
    return _render_detalhe(request, pk, form_chamados=form, abrir_modal=True)


@require_POST
@login_required
@exige_solicitar_estrutura
def contratacao_chamado_reenviar(request, pk, chamado_pk):
    """Nova tentativa SÓ do que falhou. O que já foi enviado é recusado pelo
    service — reenviar duplicaria o chamado."""
    get_object_or_404(ContratacaoChamado, pk=chamado_pk, contratacao_id=pk)
    try:
        chamado = chamados_svc.reenviar(contratacao_pk=pk, chamado_pk=chamado_pk, user=request.user)
    except chamados_svc.SolicitacaoRecusada as exc:
        messages.error(request, str(exc))
    else:
        if chamado.status == StatusChamadoContratacao.ENVIADO:
            messages.success(request, f"E-mail enviado com sucesso: {chamado.get_tipo_display()}.")
        else:
            messages.warning(
                request, f"Falha no envio: {chamado.get_tipo_display()}. Veja o histórico abaixo.",
            )
    return redirect("contratacao_detail", pk=pk)


@require_POST
@login_required
@exige_gestao_contratacao
def contratacao_chamado_identificador(request, pk, chamado_pk):
    """O TI registra o número do chamado aberto no atendimento — o que faz o
    sistema poder dizer que o chamado existe, e não só que o e-mail saiu."""
    get_object_or_404(ContratacaoChamado, pk=chamado_pk, contratacao_id=pk)
    try:
        chamados_svc.registrar_identificador(
            contratacao_pk=pk, chamado_pk=chamado_pk,
            identificador=request.POST.get("identificador", ""), user=request.user,
        )
    except chamados_svc.SolicitacaoRecusada as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Número do chamado registrado.")
    return redirect("contratacao_detail", pk=pk)


# ─────────────────────────────────────────────────────────────
# Conversa dos chamados (RH ↔ TI, e-mail nos dois sentidos)
# ─────────────────────────────────────────────────────────────

# A leitura da caixa é considerada PARADA quando passa deste prazo sem sucesso.
# Com a tarefa a cada 3 minutos, 15 é folga de sobra para uma execução lenta.
CAIXA_PARADA_APOS_MIN = 15
# Corpo da requisição de resposta: os anexos + folga para o texto. Recusa antes de ler.
LIMITE_CORPO_RESPOSTA = anexos_svc.LIMITE_TOTAL + 2 * 1024 * 1024


def _estado_da_caixa() -> dict:
    """Saúde da sincronização, em linguagem de tela. Mostrada na lista (a todos)
    e na página de sincronização (TI): quem espera uma resposta por e-mail precisa
    saber se o sistema está, de fato, lendo a caixa."""
    configurada = caixa_graph_service.configurada()
    estado = ChamadoCaixaEstado.get()
    parada = False
    if configurada:
        referencia = estado.ultimo_sucesso
        parada = referencia is None or (
            timezone.now() - referencia > timedelta(minutes=CAIXA_PARADA_APOS_MIN)
        )
    return {
        "configurada": configurada, "estado": estado, "parada": parada,
        "caixa": caixa_graph_service._chaves()[3] if configurada else "",
    }


@login_required
@exige_acompanhar_chamados
def chamados_lista(request):
    """Todos os chamados de estrutura — a visão de RH e TI, a mesma fila."""
    filtros = {
        "grupo": request.GET.get("grupo", "").strip(),
        "q": request.GET.get("q", "").strip(),
        "tipo": request.GET.get("tipo", "").strip(),
        "com_falha": request.GET.get("com_falha") == "1",
    }
    chamados = list(conversa_svc.consultar_chamados(**filtros)[:200])
    for c in chamados:
        c.grupo = conversa_svc.grupo_do_atendimento(c.atendimento)
        c.tom = conversa_svc.tom_do_atendimento(c.atendimento)
    eh_ti = conversa_svc.eh_ti(request.user)
    contexto = {
        "chamados": chamados,
        "filtros": filtros,
        "contagem": conversa_svc.contagem_por_grupo(),
        "grupos": conversa_svc.ROTULO_DOS_GRUPOS,
        "tipos": TipoChamadoContratacao,
        "caixa": _estado_da_caixa(),
        "eh_ti": eh_ti,
    }
    if eh_ti:
        contexto["em_revisao"] = ChamadoEmailRecebido.objects.filter(
            estado=ChamadoEmailRecebido.Estado.QUARENTENA).count()
        contexto["com_falha_sync"] = ChamadoEmailRecebido.objects.filter(
            estado=ChamadoEmailRecebido.Estado.FALHA).count()
    return render(request, "front/contratacao/chamados_lista.html", contexto)


def _carregar_chamado(pk, chamado_pk):
    return get_object_or_404(
        ContratacaoChamado.objects.select_related("contratacao", "criado_por", "responsavel"),
        pk=chamado_pk, contratacao_id=pk,
    )


def _render_chamado(request, pk, chamado_pk, *, texto=""):
    chamado = _carregar_chamado(pk, chamado_pk)
    mensagens = list(
        chamado.mensagens.select_related("autor").prefetch_related("anexos")
    )
    eh_ti = conversa_svc.eh_ti(request.user)
    abertura = next((m for m in mensagens if m.tipo == ChamadoMensagem.Tipo.ABERTURA), None)
    ultima = mensagens[-1] if mensagens else None
    contexto = {
        "obj": chamado.contratacao,
        "chamado": chamado,
        "mensagens": [m for m in mensagens if m.tipo != ChamadoMensagem.Tipo.ABERTURA],
        "abertura": abertura,
        "ultima": ultima,
        "historico_status": list(chamado.historico_status.select_related("criado_por")),
        "eh_ti": eh_ti,
        "grupo": conversa_svc.grupo_do_atendimento(chamado.atendimento),
        "tom": conversa_svc.tom_do_atendimento(chamado.atendimento),
        "referencia": conversa_svc.referencia_do_chamado(chamado.pk),
        "solicitacao": conversa_svc.dados_da_solicitacao(chamado),
        "pode_responder": chamado.status == StatusChamadoContratacao.ENVIADO,
        "texto_digitado": texto,
        "situacoes": AtendimentoChamadoChoices,
        "limite_arquivo_mb": anexos_svc.LIMITE_ARQUIVO // (1024 * 1024),
        "extensoes": ", ".join(sorted(anexos_svc.TIPOS_PERMITIDOS)),
        "caixa": _estado_da_caixa(),
    }
    if eh_ti:
        contexto["responsaveis"] = User.objects.filter(is_active=True, is_staff=True).order_by("first_name", "username")
    # No chamado da MÁQUINA a TI precisa ver o que está reservado e o que já foi
    # ENTREGUE (e por qual movimentação): é a informação de entrega do equipamento.
    if chamado.tipo == TipoChamadoContratacao.MAQUINA:
        contexto["equipamentos_da_vaga"] = reservas_svc.reservas_da_vaga(chamado.contratacao)
        contexto["pendencia_equipamento"] = reservas_svc.pendencias_de_equipamento(chamado.contratacao)
    return render(request, "front/contratacao/chamado_detail.html", contexto)


@login_required
@exige_acompanhar_chamados
def chamado_detail(request, pk, chamado_pk):
    return _render_chamado(request, pk, chamado_pk)


@require_POST
@login_required
@exige_acompanhar_chamados
def chamado_responder(request, pk, chamado_pk):
    """Responde pela plataforma: registra a mensagem e a envia por e-mail aos
    participantes. A regra (e-mail de abertura enviado, anexos, destinatários) é
    do service; aqui só a requisição e a escolha da mensagem."""
    _carregar_chamado(pk, chamado_pk)
    texto = request.POST.get("texto", "")
    if int(request.META.get("CONTENT_LENGTH") or 0) > LIMITE_CORPO_RESPOSTA:
        messages.error(request, "Os anexos passam do limite por mensagem.")
        return _render_chamado(request, pk, chamado_pk, texto=texto)
    try:
        msg = conversa_svc.responder(
            chamado_pk=chamado_pk, user=request.user, texto=texto,
            arquivos=request.FILES.getlist("arquivos"),
        )
    except conversa_svc.ConversaRecusada as exc:
        messages.error(request, str(exc))
        return _render_chamado(request, pk, chamado_pk, texto=texto)
    if msg.estado == ChamadoMensagem.Estado.ENVIADA:
        messages.success(request, "Mensagem registrada e enviada por e-mail aos participantes.")
    else:
        messages.warning(
            request,
            "A mensagem foi registrada, mas o e-mail não saiu"
            + (f": {msg.erro}" if conversa_svc.eh_ti(request.user) and msg.erro else "")
            + ". Use “Tentar novamente” na mensagem.",
        )
    return redirect(f"{reverse('chamado_detail', args=[pk, chamado_pk])}#fim")


@require_POST
@login_required
@exige_acompanhar_chamados
def chamado_mensagem_reenviar(request, pk, chamado_pk, mensagem_pk):
    """Nova tentativa só da mensagem que falhou (o que saiu nunca é reenviado)."""
    get_object_or_404(ChamadoMensagem, pk=mensagem_pk, chamado_id=chamado_pk, chamado__contratacao_id=pk)
    try:
        msg = conversa_svc.reenviar_mensagem(chamado_pk=chamado_pk, mensagem_pk=mensagem_pk, user=request.user)
    except conversa_svc.ConversaRecusada as exc:
        messages.error(request, str(exc))
    else:
        if msg.estado == ChamadoMensagem.Estado.ENVIADA:
            messages.success(request, "Mensagem enviada por e-mail.")
        else:
            messages.warning(request, "O e-mail ainda não saiu. Veja o motivo na mensagem.")
    return redirect(f"{reverse('chamado_detail', args=[pk, chamado_pk])}#fim")


@require_POST
@login_required
@exige_gestao_contratacao
def chamado_atendimento(request, pk, chamado_pk):
    """A TI atualiza a situação do chamado e o responsável. Não mexe na vaga."""
    _carregar_chamado(pk, chamado_pk)
    responsavel = request.POST.get("responsavel")
    try:
        conversa_svc.atualizar_atendimento(
            chamado_pk=chamado_pk, para=request.POST.get("situacao", ""),
            motivo=request.POST.get("motivo", ""), user=request.user,
            responsavel_id=int(responsavel) if responsavel and responsavel.isdigit() else ("" if responsavel == "" else None),
        )
    except conversa_svc.ConversaRecusada as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Situação do chamado atualizada.")
    return redirect("chamado_detail", pk=pk, chamado_pk=chamado_pk)


@login_required
@exige_acompanhar_chamados
def chamado_anexo_download(request, pk, chamado_pk, anexo_pk):
    """ÚNICO caminho até o arquivo de um anexo: autenticado, conferido contra o
    chamado da URL, sempre como download (nunca abre no navegador) e com o tipo
    da NOSSA tabela — o que o remetente declarou não vale."""
    anexo = get_object_or_404(
        ChamadoAnexo, pk=anexo_pk, mensagem__chamado_id=chamado_pk,
        mensagem__chamado__contratacao_id=pk,
    )
    if anexo.bloqueado or not anexo.arquivo:
        raise Http404("Anexo indisponível.")
    try:
        arquivo = anexo.arquivo.open("rb")
    except (FileNotFoundError, OSError) as exc:
        raise Http404("Arquivo não encontrado no servidor.") from exc
    resposta = FileResponse(
        arquivo, as_attachment=True, filename=anexo.nome_original,
        content_type=anexo.content_type or "application/octet-stream",
    )
    resposta["X-Content-Type-Options"] = "nosniff"
    resposta["Cache-Control"] = "private, no-store"
    return resposta


# ── Sincronização com a caixa (TI) ───────────────────────────

@login_required
@exige_gestao_contratacao
@exige_acompanhar_chamados
def chamados_sincronizacao(request):
    """Saúde da leitura da caixa, e-mails em revisão e falhas a reprocessar."""
    E = ChamadoEmailRecebido.Estado
    return render(request, "front/contratacao/chamados_sincronizacao.html", {
        "caixa": _estado_da_caixa(),
        "em_revisao": list(
            ChamadoEmailRecebido.objects.filter(estado=E.QUARENTENA).select_related("chamado", "chamado__contratacao")
        ),
        "falhas": list(ChamadoEmailRecebido.objects.filter(estado=E.FALHA)),
        "recentes": list(ChamadoEmailRecebido.objects.exclude(estado=E.IGNORADO)
                         .select_related("chamado", "chamado__contratacao")[:15]),
        "lados": ChamadoMensagem.Lado,
        "max_tentativas": entrada_svc.MAX_TENTATIVAS_AUTOMATICAS,
    })


@require_POST
@login_required
@exige_gestao_contratacao
@exige_acompanhar_chamados
def chamados_sincronizacao_acao(request):
    """Uma ação da TI sobre a sincronização: ler agora · liberar · descartar ·
    reprocessar. A regra é do service; aqui a escolha da mensagem."""
    acao = request.POST.get("acao", "")
    registro_pk = request.POST.get("registro")
    try:
        if acao == "sincronizar":
            resumo = entrada_svc.sincronizar()
            (messages.error if resumo.erro else messages.success if resumo.configurada else messages.warning)(
                request, resumo.texto(),
            )
        elif acao == "liberar":
            entrada_svc.liberar_quarentena(
                registro_pk=int(registro_pk), lado=request.POST.get("lado", ""), user=request.user)
            messages.success(request, "E-mail liberado: a mensagem entrou na conversa.")
        elif acao == "descartar":
            entrada_svc.descartar_quarentena(registro_pk=int(registro_pk), user=request.user)
            messages.success(request, "E-mail descartado.")
        elif acao == "reprocessar":
            fonte = caixa_graph_service.CaixaGraph() if caixa_graph_service.configurada() else None
            r = entrada_svc.reprocessar_falha(registro_pk=int(registro_pk), user=request.user, fonte=fonte)
            (messages.error if r.estado == ChamadoEmailRecebido.Estado.FALHA else messages.success)(
                request,
                "Ainda com falha: " + r.motivo if r.estado == ChamadoEmailRecebido.Estado.FALHA
                else "E-mail reprocessado.",
            )
        else:
            messages.error(request, "Ação desconhecida.")
    except (conversa_svc.ConversaRecusada, ChamadoEmailRecebido.DoesNotExist, TypeError, ValueError) as exc:
        messages.error(request, str(exc) if isinstance(exc, conversa_svc.ConversaRecusada) else "Registro inválido.")
    return redirect("chamados_sincronizacao")


# ─────────────────────────────────────────────────────────────
# Cadastro de sistemas corporativos
# ─────────────────────────────────────────────────────────────

@login_required
@exige_gestao_contratacao
def sistema_corporativo_list(request):
    return render(request, "front/contratacao/sistema_list.html", {
        "sistemas": cs.catalogo_de_sistemas(),
    })


@login_required
@exige_gestao_contratacao
def sistema_corporativo_create(request):
    if request.method == "POST":
        form = SistemaCorporativoForm(request.POST)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.criado_por = request.user
            obj.atualizado_por = request.user
            obj.save()
            messages.success(request, "Sistema cadastrado com sucesso!")
            return redirect("sistema_corporativo_list")
        messages.error(request, "Verifique os campos destacados.")
    else:
        form = SistemaCorporativoForm()
    return render(request, "front/contratacao/sistema_form.html", {"form": form, "editar": False})


@login_required
@exige_gestao_contratacao
def sistema_corporativo_update(request, pk):
    obj = get_object_or_404(SistemaCorporativo, pk=pk)
    if request.method == "POST":
        form = SistemaCorporativoForm(request.POST, instance=obj)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.atualizado_por = request.user
            obj.save()
            messages.success(request, "Sistema atualizado com sucesso!")
            return redirect("sistema_corporativo_list")
        messages.error(request, "Verifique os campos destacados.")
    else:
        form = SistemaCorporativoForm(instance=obj)
    return render(request, "front/contratacao/sistema_form.html", {
        "form": form, "editar": True, "obj": obj,
    })


@login_required
@permission_required("ProjetoEstoque.delete_sistemacorporativo", raise_exception=True)
@exige_gestao_contratacao
def sistema_corporativo_delete(request, pk):
    obj = get_object_or_404(SistemaCorporativo, pk=pk)
    usos = ContratacaoSistema.objects.filter(sistema=obj).count()

    if request.method == "POST":
        if usos:
            # Mesmo padrão de `subtipo_delete`: bloquear com explicação em vez
            # de deixar o ProtectedError estourar. Aqui o caminho certo é
            # desativar — o histórico das contratações antigas continua válido.
            messages.error(
                request,
                f"“{obj.nome}” está em {usos} contratação(ões) e não pode ser excluído. "
                "Desmarque “Ativo” para tirá-lo das próximas sem apagar o histórico.",
            )
            return redirect("sistema_corporativo_list")
        obj.delete()
        messages.success(request, "Sistema excluído.")
        return redirect("sistema_corporativo_list")

    return render(request, "front/contratacao/sistema_confirm_delete.html", {
        "obj": obj, "usos": usos, "bloqueado": bool(usos),
    })
