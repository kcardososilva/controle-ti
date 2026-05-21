from collections import Counter
from decimal import Decimal
from datetime import timedelta
from django.core.exceptions import FieldDoesNotExist
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.http import JsonResponse
from django.db.models import Q
from django.core.paginator import Paginator
from django.template.loader import render_to_string
from django.utils import timezone
from django.db import transaction

from ..models import (
    Usuario, CentroCusto, Localidade, Funcao,
    StatusUsuarioChoices, SimNaoChoices,
    MovimentacaoLicenca, MovimentacaoItem, LicencaLote,
    TipoMovLicencaChoices,
)
from ..forms import UsuarioForm, ImportarUsuariosForm
from services.usuario_import_service import UsuarioImportService

def _model_has_field(model, field_name):
    try:
        model._meta.get_field(field_name)
        return True
    except FieldDoesNotExist:
        return False


def _safe_int(value, default=20):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _get_status_value(attr_name, fallback):
    """
    Mantém compatibilidade caso as choices tenham nomes diferentes.
    Exemplo esperado: StatusUsuarioChoices.ATIVO / StatusUsuarioChoices.DESLIGADO.
    """
    value = getattr(StatusUsuarioChoices, attr_name, None)

    if value:
        return value

    choices = dict(StatusUsuarioChoices.choices)

    for key in choices.keys():
        if str(key).lower() == fallback.lower():
            return key

    return fallback


def _usuario_queryset_filtrado(request):
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "").strip()
    pmb = request.GET.get("pmb", "").strip()
    cc = request.GET.get("cc", "").strip()
    loc = request.GET.get("loc", "").strip()
    func = request.GET.get("func", "").strip()
    order = request.GET.get("order", "nome").strip()

    allowed_order = {
        "nome": "nome",
        "-nome": "-nome",
        "matricula": "matricula",
        "-matricula": "-matricula",
        "status": "status",
        "-status": "-status",
        "data_inicio": "data_inicio",
        "-data_inicio": "-data_inicio",
    }

    order_by = allowed_order.get(order, "nome")

    qs = (
        Usuario.objects
        .select_related("centro_custo", "localidade", "funcao")
        .order_by(order_by, "nome")
    )

    if q:
        busca = (
            Q(nome__icontains=q)
            | Q(email__icontains=q)
        )

        if _model_has_field(Usuario, "matricula"):
            busca |= Q(matricula__icontains=q)

        qs = qs.filter(busca)

    if status:
        qs = qs.filter(status=status)

    if pmb:
        qs = qs.filter(pmb=pmb)

    if cc and cc.isdigit():
        qs = qs.filter(centro_custo_id=int(cc))

    if loc and loc.isdigit():
        qs = qs.filter(localidade_id=int(loc))

    if func and func.isdigit():
        qs = qs.filter(funcao_id=int(func))

    return qs


@login_required
def usuario_list(request):
    qs = _usuario_queryset_filtrado(request)
    total_filtrado = qs.count()

    status_ativo = _get_status_value("ATIVO", "ativo")
    status_desligado = _get_status_value("DESLIGADO", "desligado")

    base_global = Usuario.objects.all()

    kpi_total = base_global.count()
    kpi_ativos = base_global.filter(status=status_ativo).count()
    kpi_desligados = base_global.filter(status=status_desligado).count()
    kpi_pmb = base_global.filter(pmb=SimNaoChoices.SIM).count()

    if _model_has_field(Usuario, "matricula"):
        kpi_sem_matricula = base_global.filter(
            Q(matricula__isnull=True) | Q(matricula="")
        ).count()
    else:
        kpi_sem_matricula = 0

    kpi_sem_email = base_global.filter(
        Q(email__isnull=True) | Q(email="")
    ).count()

    per_page = _safe_int(request.GET.get("pp"), 20)

    if per_page not in [10, 20, 50, 100]:
        per_page = 20

    paginator = Paginator(qs, per_page)
    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)

    get_copy = request.GET.copy()

    if "page" in get_copy:
        del get_copy["page"]

    qs_keep = get_copy.urlencode()

    context = {
        "usuarios": page_obj.object_list,
        "page_obj": page_obj,
        "total": total_filtrado,
        "qs_keep": qs_keep,
        "per_page": per_page,

        "kpi_total": kpi_total,
        "kpi_ativos": kpi_ativos,
        "kpi_desligados": kpi_desligados,
        "kpi_pmb": kpi_pmb,
        "kpi_sem_matricula": kpi_sem_matricula,
        "kpi_sem_email": kpi_sem_email,

        "f_q": request.GET.get("q", "").strip(),
        "f_status": request.GET.get("status", "").strip(),
        "f_pmb": request.GET.get("pmb", "").strip(),
        "f_cc": int(request.GET.get("cc")) if request.GET.get("cc", "").isdigit() else "",
        "f_loc": int(request.GET.get("loc")) if request.GET.get("loc", "").isdigit() else "",
        "f_func": int(request.GET.get("func")) if request.GET.get("func", "").isdigit() else "",
        "f_order": request.GET.get("order", "nome").strip(),

        "opt_status": StatusUsuarioChoices.choices,
        "opt_pmb": SimNaoChoices.choices,
        "opt_cc": CentroCusto.objects.values("id", "numero", "departamento").order_by("numero"),
        "opt_loc": Localidade.objects.values("id", "local").order_by("local"),
        "opt_func": Funcao.objects.values("id", "nome").order_by("nome"),
    }

    is_partial = (
        request.GET.get("partial") == "1"
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
    )

    if is_partial:
        return JsonResponse({
            "ok": True,
            "table": render_to_string("front/usuarios/_usuario_table.html", context, request=request),
            "pagination": render_to_string("front/usuarios/_usuario_pagination.html", context, request=request),
            "kpis": render_to_string("front/usuarios/_usuario_kpis.html", context, request=request),
            "total": total_filtrado,
        })

    return render(request, "front/usuarios/usuario_list.html", context)


@require_POST
@login_required
def usuario_desligar(request, pk):
    usuario = get_object_or_404(Usuario, pk=pk)

    status_desligado = _get_status_value("DESLIGADO", "desligado")

    usuario.status = status_desligado
    usuario.data_termino = timezone.localdate()

    update_fields = ["status", "data_termino"]

    if hasattr(usuario, "atualizado_por"):
        usuario.atualizado_por = request.user
        update_fields.append("atualizado_por")

    usuario.save(update_fields=update_fields)

    messages.success(request, f"Funcionário {usuario.nome} desligado com sucesso.")
    return redirect("usuario_list")



# CREATE
from services.usuario_import_service import UsuarioImportService


@login_required
def usuario_create(request):
    if request.method == "POST":
        form = UsuarioForm(request.POST)

        if form.is_valid():
            obj = form.save(commit=False)

            if hasattr(obj, "criado_por"):
                obj.criado_por = request.user

            if hasattr(obj, "atualizado_por"):
                obj.atualizado_por = request.user

            obj.save()

            messages.success(request, "Funcionário criado com sucesso.")
            return redirect("usuario_list")

        messages.error(request, "Corrija os erros do formulário.")

    else:
        form = UsuarioForm()

    return render(
        request,
        "front/usuarios/usuario_form.html",
        {
            "form": form,
            "editar": False,
        }
    )


@login_required
def usuario_update(request, pk):
    usuario = get_object_or_404(Usuario, pk=pk)

    if request.method == "POST":
        form = UsuarioForm(request.POST, instance=usuario)

        if form.is_valid():
            obj = form.save(commit=False)

            if hasattr(obj, "atualizado_por"):
                obj.atualizado_por = request.user

            obj.save()

            messages.success(request, "Funcionário atualizado com sucesso.")
            return redirect("usuario_list")

        messages.error(request, "Corrija os erros do formulário.")

    else:
        form = UsuarioForm(instance=usuario)

    return render(
        request,
        "front/usuarios/usuario_form.html",
        {
            "form": form,
            "editar": True,
            "usuario": usuario,
        }
    )


@login_required
def usuario_importar(request):
    if request.method == "POST":
        form = ImportarUsuariosForm(request.POST, request.FILES)

        if form.is_valid():
            try:
                service = UsuarioImportService(
                    arquivo=form.cleaned_data["arquivo"],
                    user=request.user,
                    modo_importacao=form.cleaned_data.get("modo_importacao"),
                    nome_aba=form.cleaned_data.get("nome_aba"),
                    desligar_ausentes=form.cleaned_data.get("desligar_ausentes", False),
                )

                resultado = service.executar()

                if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                    return JsonResponse({
                        "ok": True,
                        "resultado": resultado,
                    })

                messages.success(
                    request,
                    (
                        f"Importação concluída. "
                        f"Abas: {', '.join(resultado.get('abas_processadas', []))}. "
                        f"Criados: {resultado['totais']['criados']}, "
                        f"Atualizados: {resultado['totais']['atualizados']}, "
                        f"Desligados: {resultado['totais']['desligados']}, "
                        f"Ignorados: {resultado['totais']['ignorados']}, "
                        f"Erros: {resultado['totais']['erros']}."
                    )
                )

                return redirect("usuario_list")

            except Exception as e:
                msg = f"Erro ao importar a planilha do RH: {str(e)}"

                if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                    return JsonResponse({"ok": False, "mensagem": msg}, status=400)

                messages.error(request, msg)

    else:
        form = ImportarUsuariosForm()

    return render(
        request,
        "front/usuarios/usuario_importar.html",
        {
            "form": form,
        }
    )

@login_required
def usuario_update(request, pk: int):
    obj = get_object_or_404(Usuario, pk=pk)
    if request.method == "POST":
        form = UsuarioForm(request.POST, instance=obj)
        if form.is_valid():
            sobj = form.save(commit=False)
            sobj.atualizado_por = request.user
            sobj.save()
            messages.success(request, "Usuário atualizado com sucesso!")
            return redirect("usuario_list")
        messages.error(request, "Corrija os erros do formulário.")
    else:
        form = UsuarioForm(instance=obj)
    return render(request, "front/usuarios/usuario_form.html", {"form": form, "editar": True})


# DETAIL
def _status_desligado(usuario):
    status = str(getattr(usuario, "status", "") or "").lower()
    return status in ("desligado", "inativo", "demitido", "encerrado")


def _tipo_mov_licenca_devolucao():
    if hasattr(TipoMovLicencaChoices, "DEVOLUCAO"):
        return TipoMovLicencaChoices.DEVOLUCAO

    if hasattr(TipoMovLicencaChoices, "REMOCAO"):
        return TipoMovLicencaChoices.REMOCAO

    return "devolucao"


def _tipo_mov_licenca_atribuicao():
    if hasattr(TipoMovLicencaChoices, "ATRIBUICAO"):
        return TipoMovLicencaChoices.ATRIBUICAO

    return "atribuicao"


def _model_has_field(model, field_name):
    try:
        model._meta.get_field(field_name)
        return True
    except Exception:
        return False


def _safe_decimal(value):
    if value is None:
        return Decimal("0.00")

    try:
        return Decimal(value)
    except Exception:
        return Decimal("0.00")


def _incrementar_saldo_lote(lote, quantidade=1):
    """
    Devolve saldo para o lote sem quebrar caso o seu model use nomes diferentes.
    """
    if not lote:
        return

    campos_possiveis = [
        "quantidade_disponivel",
        "saldo_disponivel",
        "quantidade_atual",
        "disponivel",
    ]

    update_fields = []

    for campo in campos_possiveis:
        if hasattr(lote, campo):
            valor_atual = getattr(lote, campo) or 0
            setattr(lote, campo, valor_atual + quantidade)
            update_fields.append(campo)
            break

    if hasattr(lote, "quantidade_usada"):
        valor_usado = getattr(lote, "quantidade_usada") or 0
        setattr(lote, "quantidade_usada", max(valor_usado - quantidade, 0))
        update_fields.append("quantidade_usada")

    if update_fields:
        lote.save(update_fields=list(set(update_fields)))


def _criar_mov_licenca_devolucao(usuario, licenca, lote, valor_unitario, request_user, observacao):
    """
    Cria movimentação de devolução usando apenas campos existentes no model.
    Assim evita quebrar se seu MovimentacaoLicenca tiver campos opcionais diferentes.
    """
    data = {
        "usuario": usuario,
        "licenca": licenca,
        "lote": lote,
        "tipo": _tipo_mov_licenca_devolucao(),
        "valor_unitario": valor_unitario or Decimal("0.00"),
    }

    if _model_has_field(MovimentacaoLicenca, "observacao"):
        data["observacao"] = observacao

    if _model_has_field(MovimentacaoLicenca, "criado_por"):
        data["criado_por"] = request_user

    if _model_has_field(MovimentacaoLicenca, "atualizado_por"):
        data["atualizado_por"] = request_user

    return MovimentacaoLicenca.objects.create(**data)


def _licencas_ativas_do_usuario(usuario):
    movs_lic = (
        MovimentacaoLicenca.objects
        .filter(usuario=usuario)
        .select_related("licenca", "licenca__fornecedor", "lote")
        .order_by("licenca_id", "-created_at", "-id")
    )

    licencas_ativas = []
    licencas_processadas = set()

    total_lic_mensal = Decimal("0.00")
    total_lic_anual = Decimal("0.00")

    tipo_atribuicao = _tipo_mov_licenca_atribuicao()

    for mov in movs_lic:
        if mov.licenca_id in licencas_processadas:
            continue

        licencas_processadas.add(mov.licenca_id)

        if mov.tipo != tipo_atribuicao:
            continue

        lic = mov.licenca
        lote = mov.lote

        custo_base = _safe_decimal(getattr(mov, "valor_unitario", None))

        quantidade_lote = Decimal("1.00")
        valor_ciclo = custo_base
        periodicidade = ""

        if lote:
            quantidade_lote = _safe_decimal(getattr(lote, "quantidade_total", None)) or Decimal("1.00")
            valor_ciclo = _safe_decimal(getattr(lote, "custo_ciclo", None)) or custo_base
            periodicidade = str(getattr(lote, "periodicidade", "") or "").lower()
        else:
            periodicidade = str(getattr(lic, "periodicidade", "") or "").lower()

        custo_mensal = Decimal("0.00")
        custo_anual = Decimal("0.00")

        if periodicidade == "anual":
            custo_anual = valor_ciclo / quantidade_lote
            custo_mensal = custo_anual / Decimal("12")
        elif periodicidade == "semestral":
            custo_mensal = custo_base / Decimal("6")
            custo_anual = custo_base * Decimal("2")
        elif periodicidade == "trimestral":
            custo_mensal = custo_base / Decimal("3")
            custo_anual = custo_base * Decimal("4")
        else:
            custo_mensal = custo_base
            custo_anual = custo_base * Decimal("12")

        total_lic_mensal += custo_mensal
        total_lic_anual += custo_anual

        if lote and hasattr(lote, "get_periodicidade_display"):
            periodicidade_label = lote.get_periodicidade_display()
        elif hasattr(lic, "get_periodicidade_display"):
            periodicidade_label = lic.get_periodicidade_display()
        else:
            periodicidade_label = periodicidade or "-"

        licencas_ativas.append({
            "movimentacao": mov,
            "licenca": lic,
            "lote": lote,
            "data_atribuicao": mov.created_at,
            "custo_mensal": custo_mensal,
            "custo_anual": custo_anual,
            "custo_base": custo_base,
            "periodicidade_label": periodicidade_label,
        })

    return licencas_ativas, total_lic_mensal, total_lic_anual


def _itens_ativos_do_usuario(usuario):
    movs_itens = (
        MovimentacaoItem.objects
        .select_related("item", "item__subtipo", "item__localidade", "item__centro_custo")
        .filter(item__isnull=False)
        .order_by("item_id", "-created_at", "-id")
    )

    itens_ativos = []
    itens_processados = set()

    for mov in movs_itens:
        if mov.item_id in itens_processados:
            continue

        itens_processados.add(mov.item_id)

        if mov.usuario_id != usuario.pk:
            continue

        if mov.tipo_movimentacao in ["baixa", "devolucao"]:
            continue

        item = mov.item
        custo_item = item.valor or Decimal("0.00")
        tipo_custo = "aquisicao"

        if getattr(item, "locado", "nao") == "sim":
            try:
                loc = item.locacao
                if loc and loc.valor_mensal:
                    custo_item = loc.valor_mensal
                    tipo_custo = "locacao"
            except Exception:
                pass

        item.custo_calc = custo_item
        item.tipo_custo_calc = tipo_custo
        item.ultima_movimentacao_usuario = mov
        itens_ativos.append(item)

    return itens_ativos


def _historico_licencas_usuario(usuario):
    return (
        MovimentacaoLicenca.objects
        .filter(usuario=usuario)
        .select_related("licenca", "lote", "criado_por")
        .order_by("-created_at", "-id")[:20]
    )


@login_required
def usuario_detail(request, pk):
    usuario = get_object_or_404(
        Usuario.objects.select_related("centro_custo", "localidade", "funcao"),
        pk=pk
    )

    itens_ativos = _itens_ativos_do_usuario(usuario)
    licencas_ativas, total_lic_mensal, total_lic_anual = _licencas_ativas_do_usuario(usuario)
    historico_licencas = _historico_licencas_usuario(usuario)

    total_itens_loc = sum(
        i.custo_calc for i in itens_ativos
        if i.tipo_custo_calc == "locacao"
    )

    total_itens_aq = sum(
        i.custo_calc for i in itens_ativos
        if i.tipo_custo_calc == "aquisicao"
    )

    burn_rate_total = total_itens_loc + total_lic_mensal

    usuario_desligado = _status_desligado(usuario)
    hoje = timezone.localdate()

    data_desligamento = usuario.data_termino if usuario.data_termino else None
    prazo_final_licencas = None
    dias_restantes_licencas = None
    prazo_vencido_licencas = False

    if usuario_desligado and data_desligamento:
        prazo_final_licencas = data_desligamento + timedelta(days=30)
        dias_restantes_licencas = (prazo_final_licencas - hoje).days
        prazo_vencido_licencas = dias_restantes_licencas <= 0
        dias_restantes_licencas = max(dias_restantes_licencas, 0)

    context = {
        "obj": usuario,
        "usuario_desligado": usuario_desligado,
        "data_desligamento": data_desligamento,
        "prazo_final_licencas": prazo_final_licencas,
        "dias_restantes_licencas": dias_restantes_licencas,
        "prazo_vencido_licencas": prazo_vencido_licencas,

        "itens_ativos": itens_ativos,
        "licencas_ativas": licencas_ativas,
        "historico_licencas": historico_licencas,

        "kpi": {
            "itens_qtd": len(itens_ativos),
            "licencas_qtd": len(licencas_ativas),
            "custo_mensal_lic": total_lic_mensal,
            "custo_anual_lic": total_lic_anual,
            "custo_mensal_loc": total_itens_loc,
            "total_aquisicao": total_itens_aq,
            "burn_rate_total": burn_rate_total,
        }
    }

    return render(request, "front/usuarios/usuario_detail.html", context)


@require_POST
@login_required
def usuario_remover_todas_licencas(request, pk):
    usuario = get_object_or_404(Usuario, pk=pk)

    senha = request.POST.get("senha_confirmacao", "")

    if not senha:
        messages.error(request, "Informe sua senha para confirmar a remoção das licenças.")
        return redirect("usuario_detail", pk=pk)

    if not request.user.check_password(senha):
        messages.error(request, "Senha incorreta. Nenhuma licença foi removida.")
        return redirect("usuario_detail", pk=pk)

    if not _status_desligado(usuario):
        messages.error(request, "A remoção em massa só é permitida para funcionário desligado/inativo.")
        return redirect("usuario_detail", pk=pk)

    licencas_ativas, _, _ = _licencas_ativas_do_usuario(usuario)

    if not licencas_ativas:
        messages.info(request, "Este funcionário não possui licenças ativas para remover.")
        return redirect("usuario_detail", pk=pk)

    total_removidas = 0

    with transaction.atomic():
        for item in licencas_ativas:
            licenca = item["licenca"]
            lote = item["lote"]
            custo_base = item["custo_base"]

            _criar_mov_licenca_devolucao(
                usuario=usuario,
                licenca=licenca,
                lote=lote,
                valor_unitario=custo_base,
                request_user=request.user,
                observacao=(
                    f"Remoção em massa de licença por desligamento do funcionário "
                    f"{usuario.nome}. Operador: {request.user}."
                )
            )

            _incrementar_saldo_lote(lote, quantidade=1)
            total_removidas += 1

    messages.success(
        request,
        f"{total_removidas} licença(s) removida(s) do funcionário {usuario.nome} e devolvida(s) ao estoque."
    )

    return redirect("usuario_detail", pk=pk)


@login_required
@transaction.atomic
def licenca_devolver_rapido(request, usuario_id, licenca_id):
    """
    Ação rápida para devolver uma licença do usuário ao estoque.
    Restaura o saldo do lote original e ajusta o centro de custo.
    """
    if request.method != "POST":
        messages.warning(request, "Ação não permitida via GET.")
        return redirect("usuario_detail", pk=usuario_id)

    usuario = get_object_or_404(Usuario, pk=usuario_id)
    
    # 1. Encontrar a atribuição ativa
    # Buscamos a última movimentação. Deve ser uma ATRIBUIÇÃO para podermos devolver.
    last_mov = MovimentacaoLicenca.objects.filter(
        usuario_id=usuario_id,
        licenca_id=licenca_id
    ).order_by('-created_at', '-id').first()

    if not last_mov or last_mov.tipo != TipoMovLicencaChoices.ATRIBUICAO:
        messages.error(request, "Não foi encontrada uma atribuição ativa desta licença para este usuário.")
        return redirect("usuario_detail", pk=usuario_id)

    # 2. Identificar Lote de Origem
    lote_origem = last_mov.lote # O lote de onde saiu
    
    # Se o lote original não existir mais (deletado?), tentamos um fallback (LIFO)
    if not lote_origem:
        lote_origem = LicencaLote.objects.filter(licenca_id=licenca_id).order_by('-data_compra').first()

    # 3. Criar Movimentação de Devolução
    nova_mov = MovimentacaoLicenca(
        tipo=TipoMovLicencaChoices.DEVOLUCAO,
        licenca_id=licenca_id,
        usuario_id=usuario_id,
        lote=lote_origem,
        criado_por=request.user,
        atualizado_por=request.user,
        # Mantém o valor histórico para estorno contábil
        valor_unitario=last_mov.valor_unitario
    )

    # 4. Restaurar Estoque e Definir Destino do Custo
    if lote_origem:
        # Trava lote para atualização segura
        lote_atual = LicencaLote.objects.select_for_update().get(pk=lote_origem.pk)
        lote_atual.quantidade_disponivel += 1
        lote_atual.save()
        
        # O custo volta para o dono do lote (ex: TI, ou Depto que comprou)
        nova_mov.centro_custo_destino = lote_atual.centro_custo
    else:
        # Se não tem lote, tenta CC da licença
        from ..models import Licenca
        lic = Licenca.objects.get(pk=licenca_id)
        nova_mov.centro_custo_destino = lic.centro_custo

    nova_mov.save()

    messages.success(request, f"Licença devolvida com sucesso! O saldo retornou ao estoque.")
    return redirect("usuario_detail", pk=usuario_id)




# DELETE (POST via modal)
@login_required
@require_POST
def usuario_delete(request, pk: int):
    obj = get_object_or_404(Usuario, pk=pk)
    obj.delete()
    messages.success(request, "Usuário removido com sucesso.")
    return redirect("usuario_list")


def _safe_decimal(value):
    if value is None:
        return Decimal("0.00")
    try:
        return Decimal(value)
    except Exception:
        return Decimal("0.00")


def _is_usuario_desligado(usuario):
    status = str(getattr(usuario, "status", "") or "").lower()
    return status in ["desligado", "inativo", "demitido", "encerrado"]


def _tipo_mov_licenca_atribuicao():
    if hasattr(TipoMovLicencaChoices, "ATRIBUICAO"):
        return TipoMovLicencaChoices.ATRIBUICAO
    return "atribuicao"


def _format_tempo_empresa(dias):
    if not dias or dias <= 0:
        return "—"

    anos = dias // 365
    meses = (dias % 365) // 30
    dias_rest = (dias % 365) % 30

    partes = []
    if anos:
        partes.append(f"{anos}a")
    if meses:
        partes.append(f"{meses}m")
    if dias_rest and not anos:
        partes.append(f"{dias_rest}d")

    return " ".join(partes) if partes else "0d"

from collections import Counter
@login_required
def usuario_dashboard(request):
    hoje = timezone.localdate()

    usuarios = list(
        Usuario.objects.select_related("centro_custo", "localidade", "funcao").order_by("nome")
    )

    user_metrics = {
        u.pk: {
            "usuario": u,
            "itens_qtd": 0,
            "licencas_qtd": 0,
            "custo_mensal_loc": Decimal("0.00"),
            "total_aquisicao": Decimal("0.00"),
            "custo_mensal_lic": Decimal("0.00"),
            "custo_anual_lic": Decimal("0.00"),
            "itens_preview": [],
            "licencas_preview": [],
        }
        for u in usuarios
    }

    # =========================================================
    # SNAPSHOT DE ITENS ATIVOS POR USUÁRIO
    # =========================================================
    movs_itens = (
        MovimentacaoItem.objects
        .select_related("usuario", "item", "item__subtipo", "item__centro_custo", "item__localidade")
        .filter(item__isnull=False)
        .order_by("item_id", "-created_at", "-id")
    )

    itens_processados = set()

    for mov in movs_itens:
        if mov.item_id in itens_processados:
            continue
        itens_processados.add(mov.item_id)

        if not mov.usuario_id:
            continue

        tipo_mov = str(getattr(mov, "tipo_movimentacao", "") or "").lower()
        if tipo_mov in ["baixa", "devolucao"]:
            continue

        if mov.usuario_id not in user_metrics:
            continue

        item = mov.item
        metric = user_metrics[mov.usuario_id]

        custo_loc_mensal = Decimal("0.00")
        custo_aquisicao = Decimal("0.00")

        if str(getattr(item, "locado", "nao")).lower() == "sim":
            try:
                locacao = item.locacao
                if locacao and getattr(locacao, "valor_mensal", None):
                    custo_loc_mensal = _safe_decimal(locacao.valor_mensal)
            except Exception:
                pass
        else:
            custo_aquisicao = _safe_decimal(getattr(item, "valor", None))

        metric["itens_qtd"] += 1
        metric["custo_mensal_loc"] += custo_loc_mensal
        metric["total_aquisicao"] += custo_aquisicao

        if len(metric["itens_preview"]) < 3:
            metric["itens_preview"].append(item.nome)

# =========================================================
# SNAPSHOT DE LICENÇAS ATIVAS POR USUÁRIO
# Correção:
# Antes agrupava somente por licenca_id.
# Agora agrupa por usuario + licença + lote.
# Isso permite que a mesma licença apareça corretamente
# para vários usuários diferentes.
# =========================================================

    movs_lic = (
        MovimentacaoLicenca.objects
        .select_related("usuario", "licenca", "licenca__fornecedor", "lote")
        .filter(usuario__isnull=False, licenca__isnull=False)
        .order_by("usuario_id", "licenca_id", "lote_id", "-created_at", "-id")
    )

    licencas_processadas = set()
    tipo_atribuicao = _tipo_mov_licenca_atribuicao()

    for mov in movs_lic:
        chave_licenca_usuario = (
            mov.usuario_id,
            mov.licenca_id,
            mov.lote_id,
        )

        if chave_licenca_usuario in licencas_processadas:
            continue

        licencas_processadas.add(chave_licenca_usuario)

        if mov.usuario_id not in user_metrics:
            continue

        # Se a última movimentação dessa licença para esse usuário
        # não for atribuição, ela não está ativa.
        if mov.tipo != tipo_atribuicao:
            continue

        lic = mov.licenca
        lote = mov.lote
        metric = user_metrics[mov.usuario_id]

        custo_base = _safe_decimal(getattr(mov, "valor_unitario", None))
        quantidade_lote = Decimal("1.00")
        valor_ciclo = custo_base
        periodicidade = ""

        if lote:
            quantidade_lote = _safe_decimal(getattr(lote, "quantidade_total", None)) or Decimal("1.00")
            valor_ciclo = _safe_decimal(getattr(lote, "custo_ciclo", None)) or custo_base
            periodicidade = str(getattr(lote, "periodicidade", "") or "").lower()
        else:
            periodicidade = str(getattr(lic, "periodicidade", "") or "").lower()

        custo_mensal = Decimal("0.00")
        custo_anual = Decimal("0.00")

        if periodicidade == "anual":
            custo_anual = valor_ciclo / quantidade_lote
            custo_mensal = custo_anual / Decimal("12")

        elif periodicidade == "semestral":
            custo_mensal = custo_base / Decimal("6")
            custo_anual = custo_base * Decimal("2")

        elif periodicidade == "trimestral":
            custo_mensal = custo_base / Decimal("3")
            custo_anual = custo_base * Decimal("4")

        else:
            custo_mensal = custo_base
            custo_anual = custo_base * Decimal("12")

        metric["licencas_qtd"] += 1
        metric["custo_mensal_lic"] += custo_mensal
        metric["custo_anual_lic"] += custo_anual

        if len(metric["licencas_preview"]) < 3:
            metric["licencas_preview"].append(lic.nome)

    # =========================================================
    # ENRIQUECIMENTO DOS DADOS
    # =========================================================
    usuarios_enriquecidos = []

    for usuario in usuarios:
        metric = user_metrics[usuario.pk]

        burn_rate_total = metric["custo_mensal_loc"] + metric["custo_mensal_lic"]
        ativos_total = metric["itens_qtd"] + metric["licencas_qtd"]

        # Custo consolidado:
        # considera recorrência mensal + patrimônio/aquisição vinculado.
        custo_total_consolidado = burn_rate_total + metric["total_aquisicao"]

        desligado = _is_usuario_desligado(usuario)

        dias_empresa = 0
        tempo_empresa_label = "—"
        if usuario.data_inicio:
            dias_empresa = max((hoje - usuario.data_inicio).days, 0)
            tempo_empresa_label = _format_tempo_empresa(dias_empresa)

        prazo_final_licencas = None
        dias_restantes_licencas = None
        prazo_vencido = False

        if desligado and usuario.data_termino:
            prazo_final_licencas = usuario.data_termino + timedelta(days=30)
            dias_restantes_licencas = (prazo_final_licencas - hoje).days
            prazo_vencido = dias_restantes_licencas <= 0
            dias_restantes_licencas = max(dias_restantes_licencas, 0)

        usuarios_enriquecidos.append({
            "usuario": usuario,
            "status_label": usuario.get_status_display() if hasattr(usuario, "get_status_display") else usuario.status,
            "usuario_desligado": desligado,
            "itens_qtd": metric["itens_qtd"],
            "licencas_qtd": metric["licencas_qtd"],
            "ativos_total": ativos_total,
            "custo_mensal_loc": metric["custo_mensal_loc"],
            "custo_mensal_lic": metric["custo_mensal_lic"],
            "custo_anual_lic": metric["custo_anual_lic"],
            "total_aquisicao": metric["total_aquisicao"],
            "burn_rate_total": burn_rate_total,
            "dias_empresa": dias_empresa,
            "tempo_empresa_label": tempo_empresa_label,
            "prazo_final_licencas": prazo_final_licencas,
            "dias_restantes_licencas": dias_restantes_licencas,
            "prazo_vencido": prazo_vencido,
            "itens_preview": metric["itens_preview"],
            "licencas_preview": metric["licencas_preview"],
            "custo_total_consolidado": custo_total_consolidado,
        })

    max_burn = max((u["burn_rate_total"] for u in usuarios_enriquecidos), default=Decimal("0.00"))
    max_custo_consolidado = max((u["custo_total_consolidado"] for u in usuarios_enriquecidos), default=Decimal("0.00"))
    max_tempo = max((u["dias_empresa"] for u in usuarios_enriquecidos), default=0)
    max_ativos = max((u["ativos_total"] for u in usuarios_enriquecidos), default=0)

    for item in usuarios_enriquecidos:
        item["custo_pct"] = int((item["custo_total_consolidado"] / max_custo_consolidado) * 100) if max_custo_consolidado > 0 else 0
        item["tempo_pct"] = int((item["dias_empresa"] / max_tempo) * 100) if max_tempo > 0 else 0
        item["ativos_pct"] = int((item["ativos_total"] / max_ativos) * 100) if max_ativos > 0 else 0

    usuarios_mais_custosos = sorted(
        [
            u for u in usuarios_enriquecidos
            if u["custo_total_consolidado"] > 0
        ],
        key=lambda x: (
            x["custo_total_consolidado"],
            x["burn_rate_total"],
            x["licencas_qtd"],
            x["itens_qtd"],
        ),
        reverse=True
    )[:10]

    usuarios_mais_antigos = sorted(
        [u for u in usuarios_enriquecidos if u["dias_empresa"] > 0],
        key=lambda x: x["dias_empresa"],
        reverse=True
    )[:10]

    usuarios_com_mais_ativos = sorted(
        [u for u in usuarios_enriquecidos if u["ativos_total"] > 0],
        key=lambda x: (x["ativos_total"], x["licencas_qtd"], x["burn_rate_total"]),
        reverse=True
    )[:10]

    desligados_com_pendencias = sorted(
        [
            u for u in usuarios_enriquecidos
            if u["usuario_desligado"] and (u["itens_qtd"] > 0 or u["licencas_qtd"] > 0)
        ],
        key=lambda x: (
            0 if x["prazo_vencido"] else 1,
            x["dias_restantes_licencas"] if x["dias_restantes_licencas"] is not None else 999999,
            -x["ativos_total"]
        )
    )[:15]

    top_consolidado = sorted(
        usuarios_enriquecidos,
        key=lambda x: (x["burn_rate_total"], x["ativos_total"], x["dias_empresa"]),
        reverse=True
    )[:20]

    # =========================================================
    # DISTRIBUIÇÕES
    # =========================================================
    cc_counter = Counter()
    loc_counter = Counter()

    for u in usuarios:
        if u.centro_custo:
            cc_counter[f"{u.centro_custo.numero} - {u.centro_custo.departamento}"] += 1
        if u.localidade:
            loc_counter[f"{u.localidade.local}"] += 1

    centros_top = [{"label": k, "total": v} for k, v in cc_counter.most_common(8)]
    localidades_top = [{"label": k, "total": v} for k, v in loc_counter.most_common(8)]

    max_cc = max((i["total"] for i in centros_top), default=0)
    max_loc = max((i["total"] for i in localidades_top), default=0)

    for item in centros_top:
        item["pct"] = int((item["total"] / max_cc) * 100) if max_cc > 0 else 0

    for item in localidades_top:
        item["pct"] = int((item["total"] / max_loc) * 100) if max_loc > 0 else 0

    # =========================================================
    # KPIs GERAIS
    # =========================================================
    total_usuarios = len(usuarios_enriquecidos)
    total_desligados = sum(1 for u in usuarios_enriquecidos if u["usuario_desligado"])
    total_ativos = total_usuarios - total_desligados
    total_desligados_pendencias = len(desligados_com_pendencias)

    total_burn_rate = sum((u["burn_rate_total"] for u in usuarios_enriquecidos), Decimal("0.00"))
    total_custo_anual_lic = sum((u["custo_anual_lic"] for u in usuarios_enriquecidos), Decimal("0.00"))
    total_itens_vinculados = sum(u["itens_qtd"] for u in usuarios_enriquecidos)
    total_licencas_vinculadas = sum(u["licencas_qtd"] for u in usuarios_enriquecidos)

    total_sem_email = sum(1 for u in usuarios if not getattr(u, "email", None))
    total_sem_matricula = sum(1 for u in usuarios if not getattr(u, "matricula", None))
    total_pmb = sum(1 for u in usuarios if str(getattr(u, "pmb", "") or "").lower() == "sim")

    context = {
        "kpi": {
            "total_usuarios": total_usuarios,
            "total_ativos": total_ativos,
            "total_desligados": total_desligados,
            "desligados_pendencias": total_desligados_pendencias,
            "total_burn_rate": total_burn_rate,
            "total_custo_anual_lic": total_custo_anual_lic,
            "total_itens_vinculados": total_itens_vinculados,
            "total_licencas_vinculadas": total_licencas_vinculadas,
            "total_sem_email": total_sem_email,
            "total_sem_matricula": total_sem_matricula,
            "total_pmb": total_pmb,
        },
        "usuarios_mais_custosos": usuarios_mais_custosos,
        "usuarios_mais_antigos": usuarios_mais_antigos,
        "usuarios_com_mais_ativos": usuarios_com_mais_ativos,
        "desligados_com_pendencias": desligados_com_pendencias,
        "top_consolidado": top_consolidado,
        "centros_top": centros_top,
        "localidades_top": localidades_top,
    }

    return render(request, "front/usuarios/usuario_dashboard.html", context)
############### FORNECEDOR ##############################
