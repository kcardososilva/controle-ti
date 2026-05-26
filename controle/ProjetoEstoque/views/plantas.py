import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from django.core.cache import cache

from ..forms import PlantaProjetoForm
from ..models import Item, Localidade, PlantaProjeto, PlantaLayoutHistorico
from services import prtg_service

_KPIS_CACHE_KEY = 'planta_kpis_globais'


# ── Lista de Plantas ──────────────────────────────────────────────────────────

@login_required
def planta_list(request):
    localidade_id = request.GET.get("localidade")
    qs = PlantaProjeto.objects.select_related("localidade", "criado_por").order_by(
        "localidade__local", "nome"
    )
    if localidade_id:
        qs = qs.filter(localidade_id=localidade_id)

    localidades = Localidade.objects.order_by("local")

    # KPIs globais — cacheados por 60s, invalidados ao salvar layout
    kpis = cache.get(_KPIS_CACHE_KEY)
    if kpis is None:
        todas = list(PlantaProjeto.objects.all())
        kpis = {
            "total_plantas":   len(todas),
            "total_elementos": sum(p.total_elementos for p in todas),
            "total_com_prtg":  sum(p.elementos_com_prtg for p in todas),
        }
        cache.set(_KPIS_CACHE_KEY, kpis, 60)
    total_plantas   = kpis["total_plantas"]
    total_elementos = kpis["total_elementos"]
    total_com_prtg  = kpis["total_com_prtg"]
    total_sem_prtg  = total_elementos - total_com_prtg

    # Evaluate queryset once; build PRTG IDs map for client-side status display
    plantas_list = list(qs)
    prtg_ids_map = {
        str(p.pk): [str(e["prtg_objid"]) for e in p.layout.get("elements", []) if e.get("prtg_objid")]
        for p in plantas_list
    }

    return render(request, "front/plantas/planta_list.html", {
        "plantas":         plantas_list,
        "localidades":     localidades,
        "localidade_sel":  localidade_id,
        "total_plantas":   total_plantas,
        "total_elementos": total_elementos,
        "total_com_prtg":  total_com_prtg,
        "total_sem_prtg":  total_sem_prtg,
        "prtg_ok":         prtg_service.is_configured(),
        "prtg_ids_json":   prtg_ids_map,
    })


# ── Criar Planta ──────────────────────────────────────────────────────────────

@login_required
def planta_create(request):
    if request.method == "POST":
        form = PlantaProjetoForm(request.POST, request.FILES)
        if form.is_valid():
            planta = form.save(commit=False)
            planta.criado_por = request.user
            planta.atualizado_por = request.user
            planta.layout = {"elements": [], "connections": [], "canvas": {"width": 1400, "height": 900}}
            planta.save()
            messages.success(request, f'Planta "{planta.nome}" criada com sucesso!')
            return redirect("planta_editor", pk=planta.pk)
    else:
        form = PlantaProjetoForm()
    return render(request, "front/plantas/planta_form.html", {
        "form":  form,
        "titulo": "Nova Planta",
        "modo":  "criar",
    })


# ── Editar Metadados da Planta ────────────────────────────────────────────────

@login_required
def planta_update(request, pk):
    planta = get_object_or_404(PlantaProjeto, pk=pk)
    if request.method == "POST":
        form = PlantaProjetoForm(request.POST, request.FILES, instance=planta)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.atualizado_por = request.user
            obj.save()
            messages.success(request, f'Planta "{planta.nome}" atualizada com sucesso!')
            return redirect("planta_viewer", pk=planta.pk)
    else:
        form = PlantaProjetoForm(instance=planta)
    return render(request, "front/plantas/planta_form.html", {
        "form":   form,
        "planta": planta,
        "titulo": f"Editar — {planta.nome}",
        "modo":   "editar",
    })


# ── Excluir Planta ────────────────────────────────────────────────────────────

@login_required
def planta_delete(request, pk):
    planta = get_object_or_404(PlantaProjeto, pk=pk)
    if request.method == "POST":
        nome = planta.nome
        planta.delete()
        messages.success(request, f'Planta "{nome}" excluída.')
        return redirect("planta_list")
    return render(request, "front/plantas/planta_confirm_delete.html", {"planta": planta})


# ── Editor Canvas ─────────────────────────────────────────────────────────────

_TIPOS_ELEMENTOS = [
    ("camera",       "Câmera",       "#5856d6", "camera"),
    ("access_point", "Access Point", "#30b0c7", "wifi"),
    ("switch",       "Switch",       "#0071e3", "network-wired"),
    ("rack",         "Rack",         "#8e8e93", "server"),
    ("desktop",      "Desktop",      "#34c759", "desktop"),
    ("impressora",   "Impressora",   "#ff9500", "print"),
    ("nobreak",      "Nobreak",      "#ff6b35", "bolt"),
    ("servidor",     "Servidor",     "#5ac8fa", "cloud"),
    ("ponto_rede",   "Ponto de Rede","#6e6e73", "circle-dot"),
    ("texto",        "Texto",        "#1d1d1f", "font"),
    ("quadro",       "Quadro/Área",  "#0071e3", "square"),
    ("circulo",      "Círculo/Zona", "#5856d6", "circle"),
]

@login_required
def planta_editor(request, pk):
    planta = get_object_or_404(PlantaProjeto, pk=pk)
    return render(request, "front/plantas/planta_editor.html", {
        "planta":          planta,
        "layout_json":     planta.layout,
        "prtg_ok":         prtg_service.is_configured(),
        "tipos_elementos": _TIPOS_ELEMENTOS,
    })


# ── Visualizador com Status PRTG ──────────────────────────────────────────────

@login_required
def planta_viewer(request, pk):
    planta = get_object_or_404(PlantaProjeto, pk=pk)
    return render(request, "front/plantas/planta_viewer.html", {
        "planta":      planta,
        "layout_json": planta.layout,
        "prtg_ok":     prtg_service.is_configured(),
    })


# ── API: Salvar Layout (AJAX POST) ────────────────────────────────────────────

_ALLOWED_EL_KEYS = {
    "id", "type", "label", "x", "y", "x2", "y2", "color",
    "width", "height", "strokeWidth", "dash", "arrowEnd", "arrowStart",
    "item_id", "prtg_objid", "ip", "observacoes", "fontSize",
    "fontFamily", "fontBold", "fontItalic",
    "fillOpacity", "borderWidth", "borderColor", "borderStyle", "cornerRadius", "fillType",
    "zIndex", "locked", "rotation", "groupId",
}
_ALLOWED_CN_KEYS = {
    "id", "from", "fromEdge", "to", "toEdge", "type", "label",
    "cpx", "cpy", "cp1x", "cp1y", "cp2x", "cp2y", "waypoints",
    "color", "strokeWidth", "dash", "arrow",
}
_MAX_HISTORICO = 20


@login_required
def planta_salvar_layout(request, pk):
    if request.method != "POST":
        return JsonResponse({"ok": False, "erro": "Método não permitido"}, status=405)
    planta = get_object_or_404(PlantaProjeto, pk=pk)
    try:
        payload = json.loads(request.body)
        if not isinstance(payload.get("elements"), list):
            raise ValueError("Campo 'elements' ausente ou inválido.")
        if not isinstance(payload.get("connections"), list):
            raise ValueError("Campo 'connections' ausente ou inválido.")

        # Detecção de conflito: cliente envia a versão que conhecia
        client_version = payload.get("client_version")
        if client_version is not None:
            try:
                client_version = int(client_version)
            except (TypeError, ValueError):
                client_version = None

        if client_version is not None and client_version != planta.layout_version:
            editado_por = ""
            if planta.atualizado_por:
                editado_por = (
                    planta.atualizado_por.get_full_name()
                    or planta.atualizado_por.username
                )
            return JsonResponse({
                "ok": False,
                "conflito": True,
                "versao_atual": planta.layout_version,
                "editado_por": editado_por or "outro usuário",
            })

        # Sanitização
        clean_elements = []
        for el in payload["elements"]:
            clean_el = {k: v for k, v in el.items() if k in _ALLOWED_EL_KEYS}
            clean_el["label"] = str(clean_el.get("label", ""))[:120]
            clean_el["observacoes"] = str(clean_el.get("observacoes", ""))[:500]
            clean_elements.append(clean_el)
        clean_connections = [
            {k: v for k, v in cn.items() if k in _ALLOWED_CN_KEYS}
            for cn in payload["connections"]
        ]
        novo_layout = {
            "elements":    clean_elements,
            "connections": clean_connections,
            "canvas":      payload.get("canvas", {"width": 1400, "height": 900}),
        }

        # Criar entrada no histórico se solicitado explicitamente
        if payload.get("nova_versao"):
            descricao = str(payload.get("descricao", ""))[:200]
            PlantaLayoutHistorico.objects.create(
                planta=planta,
                versao=planta.layout_version,
                layout=planta.layout,
                salvo_por=request.user,
                descricao=descricao,
            )
            # Manter apenas os últimos N
            ids_antigos = (
                PlantaLayoutHistorico.objects
                .filter(planta=planta)
                .order_by("-versao")
                .values_list("id", flat=True)[_MAX_HISTORICO:]
            )
            if ids_antigos:
                PlantaLayoutHistorico.objects.filter(id__in=list(ids_antigos)).delete()

        nova_versao = planta.layout_version + 1
        planta.layout = novo_layout
        planta.layout_version = nova_versao
        planta.atualizado_por = request.user
        planta.save(update_fields=["layout", "layout_version", "atualizado_por", "updated_at"])
        cache.delete(_KPIS_CACHE_KEY)
        return JsonResponse({"ok": True, "versao": nova_versao})
    except (json.JSONDecodeError, ValueError, KeyError) as exc:
        return JsonResponse({"ok": False, "erro": str(exc)}, status=400)


# ── API: Verificar versão atual (polling de conflito) ─────────────────────────

@login_required
def planta_check_version(request, pk):
    planta = get_object_or_404(PlantaProjeto, pk=pk)
    editado_por = ""
    if planta.atualizado_por:
        editado_por = (
            planta.atualizado_por.get_full_name()
            or planta.atualizado_por.username
        )
    return JsonResponse({"versao": planta.layout_version, "editado_por": editado_por})


# ── API: Listar histórico de versões ─────────────────────────────────────────

@login_required
def planta_historico_api(request, pk):
    planta = get_object_or_404(PlantaProjeto, pk=pk)
    entries = (
        PlantaLayoutHistorico.objects
        .filter(planta=planta)
        .select_related("salvo_por")
        .order_by("-versao")[:_MAX_HISTORICO]
    )
    data = []
    for e in entries:
        salvo_por_nome = "—"
        if e.salvo_por:
            salvo_por_nome = e.salvo_por.get_full_name() or e.salvo_por.username
        data.append({
            "id":          e.id,
            "versao":      e.versao,
            "salvo_por":   salvo_por_nome,
            "salvo_em":    e.salvo_em.strftime("%d/%m/%Y %H:%M"),
            "descricao":   e.descricao,
            "n_elementos": len(e.layout.get("elements", [])),
            "n_conexoes":  len(e.layout.get("connections", [])),
        })
    return JsonResponse({"ok": True, "historico": data})


# ── API: Restaurar versão do histórico ────────────────────────────────────────

@login_required
def planta_restaurar_versao(request, pk, hist_pk):
    if request.method != "POST":
        return JsonResponse({"ok": False}, status=405)
    planta = get_object_or_404(PlantaProjeto, pk=pk)
    entrada = get_object_or_404(PlantaLayoutHistorico, pk=hist_pk, planta=planta)

    # Salva o estado atual no histórico antes de restaurar
    PlantaLayoutHistorico.objects.get_or_create(
        planta=planta,
        versao=planta.layout_version,
        defaults={
            "layout":    planta.layout,
            "salvo_por": request.user,
            "descricao": f"Backup antes de restaurar v{entrada.versao}",
        },
    )
    nova_versao = planta.layout_version + 1
    planta.layout = entrada.layout
    planta.layout_version = nova_versao
    planta.atualizado_por = request.user
    planta.save(update_fields=["layout", "layout_version", "atualizado_por", "updated_at"])
    cache.delete(_KPIS_CACHE_KEY)
    return JsonResponse({"ok": True, "versao": nova_versao})


# ── Modo TV ─────────────────────────────────────────────────────────────────

@login_required
def planta_tv(request, pk):
    planta   = get_object_or_404(PlantaProjeto, pk=pk)
    interval = max(10, min(300, int(request.GET.get("interval", 30))))
    return render(request, "front/plantas/planta_tv.html", {
        "planta":      planta,
        "layout_json": planta.layout,
        "interval":    interval,
        "prtg_ok":     prtg_service.is_configured(),
    })


# ── API: Status PRTG (proxy seguro) ──────────────────────────────────────────

@login_required
def prtg_status_api(request):
    """
    Proxy server-side para PRTG.
    Credenciais NUNCA chegam ao browser — o cliente recebe apenas status filtrado.
    Resultado cacheado 30s no servidor via FileBasedCache.
    """
    if not prtg_service.is_configured():
        return JsonResponse({"ok": False, "erro": "PRTG não configurado.", "devices_map": {}})
    devices_map = prtg_service.get_devices_map()
    return JsonResponse({"ok": True, "devices_map": devices_map})


# ── API: Buscar Devices PRTG (autocomplete editor) ───────────────────────────

@login_required
def prtg_search_api(request):
    q = request.GET.get("q", "").strip()
    if len(q) < 2:
        return JsonResponse({"ok": True, "results": []})
    results = prtg_service.search_devices(q)
    return JsonResponse({"ok": True, "results": results})


# ── API: Buscar Items do sistema (autocomplete editor) ────────────────────────

@login_required
def item_search_api(request):
    q = request.GET.get("q", "").strip()
    if len(q) < 2:
        return JsonResponse({"ok": True, "results": []})
    qs = Item.objects.select_related("localidade", "subtipo")
    if q.startswith("id:"):
        try:
            item_id = int(q[3:])
            qs = qs.filter(pk=item_id)
        except ValueError:
            return JsonResponse({"ok": True, "results": []})
    else:
        qs = qs.filter(nome__icontains=q, status="ativo")
    itens = qs.values("id", "nome", "numero_serie", "marca", "modelo",
                      "status", "localidade__local", "subtipo__nome")[:20]
    results = [
        {
            "id":         i["id"],
            "nome":       i["nome"],
            "serie":      i["numero_serie"] or "",
            "marca":      i["marca"] or "",
            "modelo":     i["modelo"] or "",
            "localidade": i["localidade__local"] or "",
            "subtipo":    i["subtipo__nome"] or "",
        }
        for i in itens
    ]
    return JsonResponse({"ok": True, "results": results})
