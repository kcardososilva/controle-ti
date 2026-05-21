import csv
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.template.loader import render_to_string
from services.sistema_inteligencia_service import SistemaInteligenciaService
from services.sistema_noticias_service import SistemaNoticiasService


@login_required
def sistema_inteligencia_dashboard(request):
    service = SistemaInteligenciaService()

    filters = {
        "q": request.GET.get("q", "").strip(),
        "severity": request.GET.get("severity", "").strip(),
        "scope": request.GET.get("scope", "").strip(),
        "type": request.GET.get("type", "").strip(),
    }

    report = service.build_report(filters)

    context = {
        "issues": report["issues"],
        "kpi": report["kpis"],
        "filters": filters,
        "severity_options": [
            ("", "Todas severidades"),
            ("critico", "Crítico"),
            ("alto", "Alto"),
            ("medio", "Médio"),
            ("baixo", "Baixo"),
        ],
        "scope_options": [
            ("", "Todos módulos"),
            ("usuario", "Usuários"),
            ("item", "Itens / Equipamentos"),
            ("licenca", "Licenças"),
            ("lote", "Lotes"),
            ("movimentacao", "Movimentações"),
            ("preventiva", "Preventivas"),
            ("cadastro", "Cadastros Base"),
            ("sistema", "Sistema"),
        ],
        "type_options": [
            ("", "Todos tipos"),
            ("duplicado", "Duplicado"),
            ("divergente", "Divergente"),
            ("pendencia", "Pendência"),
            ("cadastro_incompleto", "Cadastro incompleto"),
            ("saldo", "Saldo / Estoque"),
            ("vencimento", "Vencimento"),
            ("risco", "Risco operacional"),
        ],
    }

    is_ajax = (
        request.GET.get("partial") == "1" or
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
    )

    if is_ajax:
        return JsonResponse({
            "ok": True,
            "html": render_to_string(
                "front/inteligencia/_sistema_inteligencia_issues.html",
                context,
                request=request,
            ),
            "kpis": render_to_string(
                "front/inteligencia/_sistema_inteligencia_kpis.html",
                context,
                request=request,
            ),
            "total": len(report["issues"]),
        })

    return render(request, "front/inteligencia/sistema_inteligencia.html", context)


@login_required
def sistema_inteligencia_busca_global(request):
    q = request.GET.get("q", "").strip()
    service = SistemaInteligenciaService()
    results = service.global_search(q)
    return JsonResponse({"ok": True, "results": results})


@login_required
def sistema_inteligencia_export_csv(request):
    service = SistemaInteligenciaService()

    filters = {
        "q": request.GET.get("q", "").strip(),
        "severity": request.GET.get("severity", "").strip(),
        "scope": request.GET.get("scope", "").strip(),
        "type": request.GET.get("type", "").strip(),
    }

    report = service.build_report(filters)

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="relatorio_inteligencia_sistema.csv"'
    response.write("﻿")

    writer = csv.writer(response, delimiter=";")
    writer.writerow([
        "Severidade", "Tipo", "Módulo", "Título", "Descrição",
        "Identificador", "Quantidade Afetada", "Recomendação", "URL",
    ])

    for issue in report["issues"]:
        writer.writerow([
            issue["severity_label"], issue["type_label"], issue["scope_label"],
            issue["title"], issue["description"], issue["identifier"],
            issue["affected_count"], issue["hint"], issue["url"],
        ])

    return response


@login_required
def sistema_noticias(request):
    service = SistemaNoticiasService()
    context = service.build()
    return render(request, "front/noticias/sistema_noticias.html", context)
