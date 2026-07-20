---
name: novo-dashboard
description: Cria um novo dashboard de KPIs (hero + cards + gráficos Chart.js, opcionalmente com drawer de detalhe via AJAX) seguindo o padrão de custos_diretoria_dashboard / cc_custos_dashboard. Usar quando o pedido for "criar dashboard de X", "painel de indicadores de Y".
---

# Novo dashboard

Referências canônicas: `ProjetoEstoque/views/dashboards.py`
(`custos_diretoria_dashboard` + `custos_diretoria_detalhe`) e o template
`templates/front/dashboards/custos_diretoria.html`.

## Passo a passo

1. **View principal** em `views/dashboards.py`: monta os KPIs agregados
   (`aggregate`, `annotate`) e o contexto para o template. Evitar N+1 —
   `select_related`/`prefetch_related` sempre que iterar relações.
   - `@login_required`.
   - Se o dashboard tiver filtro de período (`dt_ini`/`dt_fim`), passar como
     objetos `date` (não `datetime`) para os inputs do form, e usar
     `_align_series`/`_align_series_date` já existentes se envolver série
     temporal com `TruncMonth` — **atenção**: `TruncMonth` em `DateField`
     retorna `date`, não `datetime`; usar `_align_series_date` nesse caso
     (ver nota do CLAUDE.md sobre esse bug já corrigido uma vez).

2. **Endpoint de detalhe (drawer), se necessário** — função separada
   `<dominio>_detalhe`, `GET`-only, `@login_required`, retorna `JsonResponse`.
   Seguir o padrão de `custos_diretoria_detalhe`: parâmetros via
   `request.GET.get(...)`, validar e devolver `{"erro": "..."}` com `status=400`
   se inválido, senão montar listas agregadas + totais em Decimal
   (`ROUND_HALF_UP`, `.quantize(Decimal("0.01"))`).

3. **Registrar em `views/__init__.py`** (bloco `# ── Dashboards ──`) e URLs:
   ```
   path("dashboards/<nome>/",         views.<nome>_dashboard, name="<nome>_dashboard"),
   path("dashboards/<nome>/detalhe/", views.<nome>_detalhe,   name="<nome>_detalhe"),   # se houver
   ```

4. **Template** em `templates/front/dashboards/<nome>.html`:
   - Hero + `kpi-row` de cards no topo (tokens de `base.html`).
   - Chart.js 4.4.3 via CDN **só no próprio template** (`extra_css`/`extra_js`),
     nunca global.
   - Se usar dropdowns (`Select2`), carregar jQuery + Select2 via CDN no próprio
     template também — não é global no projeto.
   - Drawer de detalhe (se houver): AJAX para o endpoint do passo 2, desktop
     desliza da direita (`translateX`), mobile (`≤559px`) sobe como bottom
     sheet (`translateY`, `max-height: 88dvh`, safe-area insets), swipe para
     fechar, body scroll lock preservando `window.scrollY`. Ver
     `custos_diretoria.html` como referência de implementação completa.
   - "Resumo inteligente" (texto em linguagem natural resumindo os números) é
     construído em JS a partir do JSON — só incluir se o dashboard realmente
     se beneficiar disso (não é obrigatório).

5. **Export**, se pedido: PDF/Excel vão em `relatorios.py`, seguindo o padrão
   `*_export_pdf` / `*_export_excel` já usado (ex.: `centrocusto_export_pdf`).

## Checklist final
- [ ] Sem N+1 (checar queries com `select_related`/`prefetch_related`)
- [ ] Datas do form como `date`, não `datetime`
- [ ] `TruncMonth` em `DateField` tratado com `_align_series_date`
- [ ] Endpoint AJAX de detalhe protegido com `@login_required`
- [ ] CSS escopado por `:where(.prefixo-page)`
- [ ] Nenhum item novo na navbar sem pedido explícito
