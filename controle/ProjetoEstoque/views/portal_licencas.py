"""
Portal de Licenças Office — área isolada (sandbox) para parceiros externos
que também administram licenças Office da empresa (ex.: Routerlink).

Camadas de isolamento (defesa em profundidade — mesmo padrão do Portal do
Fornecedor, ver CLAUDE.md):
  1. LicencaOfficeAccessMiddleware — restringe o grupo "Parceiro de
     Licenças" às URLs sob /portal-licencas/.
  2. @parceiro_licenca_required    — resolve a empresa parceira do request
     (via PerfilParceiroLicenca) ou mostra uma tela orientativa (403).
  3. Todo dado exposto é só o módulo LicencaOffice + um resumo do Item
     vinculado (nome/modelo/status/CC/localidade/usuário atual) — nunca o
     restante da ficha do equipamento (financeiro, histórico, manutenção...).

Escopo de permissão (definido pelo TI): ver + atualizar status, conta
vinculada, usuário vinculado e data da licença, além de vincular/desvincular
o equipamento. NÃO pode criar, excluir nem importar planilha — isso
continua exclusivo do time interno (`views/licencas_office.py`).
"""
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from ..forms import LicencaOfficePortalForm
from ..models import (
    Item,
    LicencaOffice,
    MovimentacaoItem,
    StatusLicencaOfficeChoices,
    TipoMovimentacaoChoices,
    TipoTransferenciaChoices,
)
from .licencas_office import (
    detentor_pessoa_atual,
    executar_desvincular,
    executar_vincular,
    item_picker_info,
    itens_elegiveis_para_licenca_office,
)


# ─── Helpers de segurança ─────────────────────────────────────────────────────

def parceiro_licenca_do_request(request):
    """Retorna a empresa parceira vinculada ao usuário logado (perfil ativo), ou None."""
    perfil = getattr(request.user, "perfil_parceiro_licenca", None)
    if perfil is not None and perfil.ativo:
        return perfil.parceiro
    return None


def parceiro_licenca_required(view_func):
    """Garante que o request tem um parceiro de licenças ativo vinculado e
    injeta `request.parceiro_licenca`. Deve decorar TODA view do portal."""
    @wraps(view_func)
    @login_required
    def _wrapped(request, *args, **kwargs):
        parceiro = parceiro_licenca_do_request(request)
        if parceiro is None:
            return render(request, "front/portal_licencas/portal_licencas_sem_acesso.html", status=403)
        request.parceiro_licenca = parceiro
        return view_func(request, *args, **kwargs)
    return _wrapped


def _is_ajax(request):
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _resumo_item(item):
    """Resumo do equipamento vinculado exposto ao parceiro externo — só os
    campos aprovados pelo TI (nome, modelo, status, CC atual, localidade,
    usuário/detentor atual). Nunca o restante da ficha interna."""
    if not item:
        return None

    ultima_mov = (
        MovimentacaoItem.objects.filter(item=item)
        .select_related("usuario", "centro_custo_destino")
        .order_by("-created_at")
        .first()
    )
    usuario_atual = "Em estoque / Não definido"
    if ultima_mov:
        eh_devolucao = (
            ultima_mov.tipo_movimentacao == TipoMovimentacaoChoices.TRANSFERENCIA
            and ultima_mov.tipo_transferencia == TipoTransferenciaChoices.DEVOLUCAO
        )
        if ultima_mov.usuario_id and not eh_devolucao:
            usuario_atual = ultima_mov.usuario.nome
        elif ultima_mov.centro_custo_destino_id:
            usuario_atual = f"Setor: {ultima_mov.centro_custo_destino.departamento}"

    return {
        "pk": item.pk,
        "nome": item.nome,
        "modelo": item.modelo or "—",
        "status": item.get_status_display(),
        "centro_custo": str(item.centro_custo) if item.centro_custo else "—",
        "localidade": str(item.localidade) if item.localidade else "—",
        "usuario_vinculado": usuario_atual,
    }


# ─── Views ────────────────────────────────────────────────────────────────────

@parceiro_licenca_required
def portal_licencas_office_list(request):
    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()
    vinculo = (request.GET.get("vinculo") or "").strip()

    qs = LicencaOffice.objects.select_related("item").order_by("produto", "chave_produto")

    if q:
        qs = qs.filter(
            Q(produto__icontains=q)
            | Q(chave_produto__icontains=q)
            | Q(conta_vinculada__icontains=q)
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

    # "Usuário" exibido: prioriza o detentor ATUAL do equipamento (ver
    # `licencas_office.detentor_pessoa_atual`) e só cai pro texto salvo em
    # `usuario_vinculado` quando não há posse resolvível ou a licença não
    # está vinculada.
    for obj in page_obj.object_list:
        detentor = detentor_pessoa_atual(obj.item) if obj.item_id else None
        obj.usuario_exibicao = detentor or obj.usuario_vinculado

    get_copy = request.GET.copy()
    get_copy.pop("page", None)

    # Para o "Vincular" na lista — só notebook/desktop/servidor ainda sem
    # licença Office própria.
    itens_sem_licenca = itens_elegiveis_para_licenca_office()

    context = {
        "parceiro": request.parceiro_licenca,
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
    return render(request, "front/portal_licencas/portal_licencas_office_list.html", context)


@parceiro_licenca_required
def portal_licencas_office_editar(request, pk):
    obj = get_object_or_404(LicencaOffice, pk=pk)

    if request.method == "POST":
        form = LicencaOfficePortalForm(request.POST, instance=obj)
        if form.is_valid():
            licenca = form.save(commit=False)
            licenca.atualizado_por = request.user
            licenca.save()
            messages.success(request, f'Licença "{licenca.produto}" atualizada com sucesso.')
            return redirect("portal_licencas_office_list")
        messages.error(request, "Verifique os campos.")
    else:
        form = LicencaOfficePortalForm(instance=obj)

    return render(request, "front/portal_licencas/portal_licencas_form.html", {
        "parceiro": request.parceiro_licenca,
        "form": form,
        "obj": obj,
        "item_resumo": _resumo_item(obj.item),
    })


@require_POST
@parceiro_licenca_required
def portal_licencas_office_vincular(request, item_pk):
    item = get_object_or_404(Item, pk=item_pk)
    resultado = executar_vincular(item=item, licenca_id=request.POST.get("licenca_id"), user=request.user)

    if _is_ajax(request):
        if not resultado.ok:
            return JsonResponse({"ok": False, "erro": resultado.mensagem}, status=400)
        return JsonResponse({
            "ok": True,
            "mensagem": resultado.mensagem,
            "item": item_picker_info(item),
            "licenca": {
                "pk": resultado.licenca.pk,
                "produto": resultado.licenca.produto,
                "chave_produto": resultado.licenca.chave_produto,
                "usuario_vinculado": resultado.licenca.usuario_vinculado,
            },
        })

    if resultado.ok:
        messages.success(request, resultado.mensagem)
    else:
        messages.error(request, resultado.mensagem)
    return redirect("portal_licencas_office_list")


@require_POST
@parceiro_licenca_required
def portal_licencas_office_desvincular(request, item_pk):
    item = get_object_or_404(Item, pk=item_pk)
    resultado = executar_desvincular(item=item, user=request.user)

    if _is_ajax(request):
        if not resultado.ok:
            return JsonResponse({"ok": False, "erro": resultado.mensagem}, status=400)
        return JsonResponse({
            "ok": True,
            "mensagem": resultado.mensagem,
            "item": item_picker_info(item),
            "licenca": {
                "pk": resultado.licenca.pk,
                "produto": resultado.licenca.produto,
                "chave_produto": resultado.licenca.chave_produto,
                "usuario_vinculado": resultado.licenca.usuario_vinculado,
            },
        })

    if resultado.ok:
        messages.success(request, resultado.mensagem)
    else:
        messages.error(request, resultado.mensagem)
    return redirect("portal_licencas_office_list")
