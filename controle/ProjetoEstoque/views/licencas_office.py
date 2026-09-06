"""
Licenças Office (chave por equipamento) — diferente do módulo `licencas.py`
(pool de assentos por nome de software, alocado a colaboradores). Aqui cada
registro é uma chave/serial real (ex.: Office Home and Business preso a um
notebook), e o vínculo relevante é com o `Item` de estoque — visível e
gerenciável direto na ficha do equipamento (ver `equipamentos.py`).
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from ..forms import LicencaOfficeForm, LicencaOfficeImportForm
from ..models import (
    Item,
    LicencaOffice,
    MovimentacaoItem,
    SimNaoChoices,
    StatusLicencaOfficeChoices,
    TipoMovimentacaoChoices,
    TipoTransferenciaChoices,
)
from services.licenca_office_import_service import LicencaOfficeImportService


def _safe_next(request, url):
    if url and url_has_allowed_host_and_scheme(
        url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return url
    return None


def _is_ajax(request):
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


# Subtipos de equipamento que fazem sentido receber uma Licença Office —
# usado para não oferecer impressora/switch/monitor/etc. no seletor de
# "vincular a um equipamento" (lista interna e Portal de Licenças Office) E
# como regra de verdade dentro de `executar_vincular` — sem isso a restrição
# seria só cosmética (um POST forjado direto no endpoint ignoraria o filtro
# do <select>). `icontains` (em vez de igualdade exata) porque `Subtipo.nome`
# é texto livre cadastrado manualmente (ex.: pode existir "Servidor" ou
# "Servidores").
SUBTIPOS_LICENCA_OFFICE = ("notebook", "desktop", "servidor")


def _item_elegivel_licenca_office(item):
    nome = item.subtipo.nome.lower() if item.subtipo_id else ""
    return any(termo in nome for termo in SUBTIPOS_LICENCA_OFFICE)


def itens_elegiveis_para_licenca_office():
    query_subtipo = Q()
    for termo in SUBTIPOS_LICENCA_OFFICE:
        query_subtipo |= Q(subtipo__nome__icontains=termo)
    return (
        Item.objects.filter(item_consumo=SimNaoChoices.NAO, licenca_office__isnull=True)
        .filter(query_subtipo)
        .order_by("nome")
        .values("pk", "nome", "numero_serie", "modelo")
    )


def item_picker_info(item):
    """Formato usado pelo <select> de "vincular a um equipamento" (lista
    interna e Portal) — id + os campos que ajudam a achar/diferenciar
    máquinas com nome parecido (nº de série, modelo). Reaproveitado tanto na
    resposta AJAX de vincular/desvincular quanto ao recompor uma opção do
    seletor no JS após um desvínculo."""
    return {
        "pk": item.pk,
        "nome": item.nome,
        "numero_serie": item.numero_serie or "",
        "modelo": item.modelo or "",
    }


def licenca_office_contexto(item):
    """(licenca_office, licencas_office_disponiveis) para este item — única
    fonte de verdade da consulta, usada tanto pela ficha do equipamento
    (`equipamentos.equipamento_detalhe`) quanto pelo re-render AJAX do card
    (`_card_html` abaixo), pra nunca divergir entre as duas."""
    licenca_office = LicencaOffice.objects.select_related("item").filter(item=item).first()
    licencas_office_disponiveis = (
        LicencaOffice.objects.filter(item__isnull=True).order_by("produto", "chave_produto")
        if not licenca_office and not item.eh_consumo
        else LicencaOffice.objects.none()
    )
    return licenca_office, licencas_office_disponiveis


def _card_html(request, item):
    """Renderiza o fragmento "Licença Office" da ficha do equipamento com
    dados frescos do banco — usado pelas respostas AJAX de vincular/
    desvincular, que recarregam só este pedaço da página (ver
    `_licenca_office_card.html`)."""
    licenca_office, licencas_office_disponiveis = licenca_office_contexto(item)
    return render_to_string(
        "front/equipamentos/_licenca_office_card.html",
        {
            "item": item,
            "licenca_office": licenca_office,
            "licencas_office_disponiveis": licencas_office_disponiveis,
        },
        request=request,
    )


@login_required
def licenca_office_list(request):
    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()
    vinculo = (request.GET.get("vinculo") or "").strip()

    qs = LicencaOffice.objects.select_related("item").order_by("produto", "chave_produto")

    if q:
        qs = qs.filter(
            Q(produto__icontains=q)
            | Q(chave_produto__icontains=q)
            | Q(conta_vinculada__icontains=q)
            | Q(id_dell__icontains=q)
            | Q(usuario_vinculado__icontains=q)
            | Q(item__nome__icontains=q)
            | Q(item__numero_serie__icontains=q)
        )

    valid_status = [c[0] for c in StatusLicencaOfficeChoices.choices]
    if status in valid_status:
        qs = qs.filter(status=status)

    if vinculo == "com":
        qs = qs.filter(item__isnull=False)
    elif vinculo == "sem":
        qs = qs.filter(item__isnull=True)

    resumo = {
        "total": LicencaOffice.objects.count(),
        "vinculadas": LicencaOffice.objects.filter(item__isnull=False).count(),
        "com_problema": LicencaOffice.objects.exclude(status=StatusLicencaOfficeChoices.OK).count(),
    }
    resumo["sem_vinculo"] = resumo["total"] - resumo["vinculadas"]

    paginator = Paginator(qs, 20)
    page_obj = paginator.get_page(request.GET.get("page"))

    # "Usuário" exibido: prioriza o detentor ATUAL do equipamento (recalculado
    # a cada carregamento — não fica preso ao que estava certo no momento do
    # vínculo) e só cai pro texto salvo em `usuario_vinculado` quando não há
    # posse resolvível (equipamento em estoque/setor) ou quando a licença
    # nem está vinculada a um equipamento.
    for obj in page_obj.object_list:
        detentor = detentor_pessoa_atual(obj.item) if obj.item_id else None
        obj.usuario_exibicao = detentor or obj.usuario_vinculado

    get_copy = request.GET.copy()
    get_copy.pop("page", None)

    # Para o modal "Vincular" na lista: só notebook/desktop/servidor ainda
    # sem licença Office própria — evita oferecer um alvo que o backend
    # rejeitaria (ou que não faz sentido, tipo impressora/switch).
    itens_sem_licenca = itens_elegiveis_para_licenca_office()

    context = {
        "page_obj": page_obj,
        "licencas": page_obj.object_list,
        "qs_keep": get_copy.urlencode(),
        "q": q,
        "status": status,
        "vinculo": vinculo,
        "status_choices": StatusLicencaOfficeChoices.choices,
        "resumo": resumo,
        "tem_filtro": bool(q or status or vinculo),
        "itens_sem_licenca": itens_sem_licenca,
    }
    return render(request, "front/licencas_office/licenca_office_list.html", context)


@login_required
def licenca_office_form(request, pk=None):
    obj = get_object_or_404(LicencaOffice, pk=pk) if pk else None

    initial = {}
    item_id = (request.GET.get("item") or "").strip()
    if not obj and item_id.isdigit():
        initial["item"] = item_id

    next_url = _safe_next(request, request.POST.get("next") or request.GET.get("next"))

    if request.method == "POST":
        form = LicencaOfficeForm(request.POST, instance=obj)
        if form.is_valid():
            licenca = form.save(commit=False)
            if not obj:
                licenca.criado_por = request.user
            licenca.atualizado_por = request.user
            licenca.save()

            verbo = "atualizada" if obj else "cadastrada"
            messages.success(request, f'Licença "{licenca.produto}" {verbo} com sucesso.')

            if next_url:
                return redirect(next_url)
            return redirect("licenca_office_list")

        messages.error(request, "Verifique os campos obrigatórios.")
    else:
        form = LicencaOfficeForm(instance=obj, initial=initial)

    return render(request, "front/licencas_office/licenca_office_form.html", {
        "form": form,
        "obj": obj,
        "next_url": next_url,
    })


@login_required
def licenca_office_delete(request, pk):
    obj = get_object_or_404(LicencaOffice, pk=pk)
    if request.method == "POST":
        produto = obj.produto
        obj.delete()
        messages.success(request, f'Licença "{produto}" excluída.')
        return redirect("licenca_office_list")
    return render(request, "front/licencas_office/licenca_office_confirm_delete.html", {"obj": obj})


@login_required
def licenca_office_importar(request):
    form = LicencaOfficeImportForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            criados, atualizados, vinculados, erros = LicencaOfficeImportService.importar(
                arquivo=form.cleaned_data["arquivo"], user=request.user,
            )
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
            return redirect("licenca_office_importar")

        if criados or atualizados:
            messages.success(
                request,
                f"Importação concluída: {criados} criada(s), {atualizados} atualizada(s), "
                f"{vinculados} vinculada(s) automaticamente a um equipamento.",
            )
        if erros:
            messages.warning(
                request,
                f"{len(erros)} linha(s) com aviso: " + " | ".join(erros[:10])
                + (f" (+{len(erros) - 10} outras)" if len(erros) > 10 else ""),
            )
        if not criados and not atualizados and not erros:
            messages.info(request, "Nenhuma linha encontrada na planilha.")
        return redirect("licenca_office_list")

    return render(request, "front/licencas_office/licenca_office_importar.html", {"form": form})


# ── Núcleo de vincular/desvincular (sem decidir resposta HTTP) ──────────────
# `transaction.atomic()` + `select_for_update()` fecham a janela de corrida
# entre "checar se já tem vínculo" e "salvar": sem isso, dois cliques quase
# simultâneos (ou duas abas) podiam passar os dois pela checagem e um deles
# estourar `IntegrityError` na constraint UNIQUE do `OneToOneField` — agora
# vira uma mensagem de erro amigável.
#
# Reaproveitado por DOIS chamadores com permissões bem diferentes — a ficha
# do equipamento (`licenca_office_vincular`/`desvincular` abaixo, uso
# interno) e o Portal de Licenças Office (`views/portal_licencas.py`, uso
# externo/parceiro) — por isso fica separado da parte HTTP: um único lugar
# testado/hardened para a operação sensível, cada chamador decide só a
# resposta (JSON, redirect, template) e a própria autorização.

class ResultadoVinculo:
    def __init__(self, ok, licenca=None, mensagem=None):
        self.ok = ok
        self.licenca = licenca
        self.mensagem = mensagem


def detentor_pessoa_atual(item):
    """Nome do colaborador atualmente de posse do equipamento (via última
    `MovimentacaoItem`), ou None se estiver em estoque/setor/manutenção
    externa/devolvido — mesma regra de "devolução não é posse" já usada no
    resumo do Portal de Licenças (`portal_licencas._resumo_item`). Usado só
    para pré-preencher `usuario_vinculado` ao vincular; diferente do
    "Detentor atual" completo da ficha do equipamento (que também mostra
    setor/local/fornecedor), porque aqui o campo é estritamente o nome de
    uma pessoa."""
    ultima_mov = (
        MovimentacaoItem.objects.filter(item=item)
        .select_related("usuario")
        .order_by("-created_at")
        .first()
    )
    if not ultima_mov or not ultima_mov.usuario_id:
        return None
    eh_devolucao = (
        ultima_mov.tipo_movimentacao == TipoMovimentacaoChoices.TRANSFERENCIA
        and ultima_mov.tipo_transferencia == TipoTransferenciaChoices.DEVOLUCAO
    )
    return None if eh_devolucao else ultima_mov.usuario.nome


def executar_vincular(*, item, licenca_id, user):
    licenca_id = (licenca_id or "").strip()
    if not licenca_id.isdigit():
        return ResultadoVinculo(False, mensagem="Selecione uma licença para vincular.")

    if not _item_elegivel_licenca_office(item):
        return ResultadoVinculo(False, mensagem=(
            f'"{item.nome}" não é elegível para Licença Office — só notebook, '
            "desktop ou servidor podem receber vínculo."
        ))

    try:
        with transaction.atomic():
            try:
                licenca = LicencaOffice.objects.select_for_update().get(pk=licenca_id)
            except LicencaOffice.DoesNotExist:
                return ResultadoVinculo(False, mensagem="Licença não encontrada.")

            if licenca.item_id and licenca.item_id != item.pk:
                return ResultadoVinculo(False, mensagem=(
                    f'Esta licença já está vinculada ao equipamento "{licenca.item}". '
                    "Desvincule-a lá antes de vincular aqui."
                ))

            vinculo_atual = (
                LicencaOffice.objects.select_for_update()
                .filter(item_id=item.pk).exclude(pk=licenca.pk).first()
            )
            if vinculo_atual:
                return ResultadoVinculo(False, mensagem=(
                    f'Este equipamento já possui a licença "{vinculo_atual.produto}" vinculada. '
                    "Desvincule-a antes de vincular outra."
                ))

            detentor = detentor_pessoa_atual(item)
            update_fields = ["item", "atualizado_por", "updated_at"]
            licenca.item = item
            licenca.atualizado_por = user
            if detentor:
                licenca.usuario_vinculado = detentor
                update_fields.append("usuario_vinculado")
            licenca.save(update_fields=update_fields)
    except IntegrityError:
        return ResultadoVinculo(False, mensagem=(
            "Este equipamento ou esta licença acabou de ser vinculado por outra ação. "
            "Atualize a página e tente de novo."
        ))

    return ResultadoVinculo(
        True, licenca=licenca,
        mensagem=f'Licença "{licenca.produto}" vinculada a "{item.nome}".',
    )


def executar_desvincular(*, item, user):
    with transaction.atomic():
        licenca = LicencaOffice.objects.select_for_update().filter(item=item).first()
        if not licenca:
            return ResultadoVinculo(False, mensagem="Este equipamento não tem licença Office vinculada.")

        licenca.item = None
        licenca.atualizado_por = user
        licenca.save(update_fields=["item", "atualizado_por", "updated_at"])

    return ResultadoVinculo(
        True, licenca=licenca,
        mensagem=f'Licença "{licenca.produto}" desvinculada de "{item.nome}".',
    )


# ── Ações rápidas a partir da ficha do equipamento (uso interno) ───────────
# Chamadas tanto pela ficha (fetch — troca só o card, sem recarregar a
# página) quanto por qualquer form/fallback sem JS (POST clássico + redirect).

def _erro(request, item, mensagem):
    if _is_ajax(request):
        return JsonResponse({"ok": False, "erro": mensagem}, status=400)
    messages.error(request, mensagem)
    return redirect("equipamento_detalhe", pk=item.pk)


def _sucesso(request, item, licenca, mensagem):
    if _is_ajax(request):
        return JsonResponse({
            "ok": True,
            "mensagem": mensagem,
            "item": item_picker_info(item),
            "licenca": {
                "pk": licenca.pk, "produto": licenca.produto, "chave_produto": licenca.chave_produto,
                "usuario_vinculado": licenca.usuario_vinculado,
            },
            "html": _card_html(request, item),
        })
    messages.success(request, mensagem)
    return redirect("equipamento_detalhe", pk=item.pk)


@require_POST
@login_required
def licenca_office_vincular(request, item_pk):
    """Vincula uma licença Office SEM equipamento a este item — chamado a
    partir do card "Licença Office" na ficha do equipamento (ou da lista)."""
    item = get_object_or_404(Item, pk=item_pk)
    resultado = executar_vincular(item=item, licenca_id=request.POST.get("licenca_id"), user=request.user)
    if not resultado.ok:
        return _erro(request, item, resultado.mensagem)
    return _sucesso(request, item, resultado.licenca, resultado.mensagem)


@require_POST
@login_required
def licenca_office_desvincular(request, item_pk):
    """Remove o vínculo do item com sua licença Office — a licença continua
    cadastrada (histórico/reaproveitável em outro equipamento), só deixa de
    aparecer na ficha deste item."""
    item = get_object_or_404(Item, pk=item_pk)
    resultado = executar_desvincular(item=item, user=request.user)
    if not resultado.ok:
        return _erro(request, item, resultado.mensagem)
    return _sucesso(request, item, resultado.licenca, resultado.mensagem)
