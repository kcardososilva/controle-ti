# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Sistema de Controle de TI para Santa Colomba Agropecuária. Gerencia equipamentos (itens), licenças de software, colaboradores (usuários), movimentações de estoque, preventivas (manutenção), plantas de infraestrutura (mapa visual com integração PRTG), e emite alertas por e-mail. Interface 100% em português (pt-BR).

## Running the Application

All commands must be run from the `controle/` directory (where `manage.py` lives):

```bash
cd controle
python manage.py runserver          # dev server on http://127.0.0.1:8000
python manage.py migrate            # apply migrations
python manage.py makemigrations     # generate new migration files
python manage.py createsuperuser    # create admin user
```

### Management Commands

```bash
# E-mail alerts
python manage.py enviar_alertas                          # all individual alerts
python manage.py enviar_alertas --tipo diario            # consolidated daily digest
python manage.py enviar_alertas --tipo diario --horas 48 # last 48h window

# Daily digest scheduler (Windows Task Scheduler)
python manage.py agendar_relatorio criar --hora 07:00   # register daily task (needs Admin terminal)
python manage.py agendar_relatorio listar               # check task status
python manage.py agendar_relatorio remover              # unregister task
python manage.py agendar_relatorio executar             # run digest immediately (for testing)

# Spreadsheet import
python manage.py importar_itens_planilha <caminho.xlsx>
```

### Environment Variables

Copy `.env.example` to `.env` in `controle/` and fill in:

```
DJANGO_SECRET_KEY=...
DJANGO_DEBUG=True
EMAIL_HOST=smtp.outlook.com
EMAIL_PORT=587
EMAIL_USE_TLS=True
EMAIL_HOST_USER=...
EMAIL_HOST_PASSWORD=...
ALERTA_EMAIL=...
PRTG_URL=https://<prtg-host>
PRTG_USER=...
PRTG_PASSHASH=...
```

## Architecture

### Directory Layout

```
controle/                        ← Django project root (manage.py lives here)
├── controle/                    ← Project settings package (settings.py, urls.py)
├── ProjetoEstoque/              ← Main Django app
│   ├── models.py                ← All models in one file
│   ├── forms.py                 ← All forms in one file
│   ├── admin.py                 ← Django admin registration
│   ├── urls.py                  ← All URL patterns
│   ├── views/                   ← Views split by domain (one file per domain)
│   │   ├── __init__.py          ← Re-exports ALL views (add new views here too)
│   │   ├── dashboards.py        ← All dashboards (home KPIs, custos CC, custos diretoria, preventiva, toner)
│   │   ├── equipamentos.py      ← Item/equipment CRUD + exports
│   │   ├── plantas.py           ← Plant map editor/viewer/TV + PRTG proxy APIs
│   │   ├── usuarios.py          ← Collaborator CRUD + import + hierarchy + organogram
│   │   ├── licencas.py          ← License CRUD + allocation + dashboard
│   │   ├── preventivas.py       ← Maintenance scheduling + execution
│   │   ├── movimentacoes.py     ← Stock movement CRUD
│   │   ├── inteligencia.py      ← Sistema de Inteligência (search + KPIs + CSV export)
│   │   ├── alertas.py           ← Alert dashboard
│   │   ├── relatorios.py        ← Excel/PDF exports
│   │   ├── home.py              ← Home view
│   │   ├── locacoes.py          ← Rental/leasing contracts
│   │   ├── centrocusto.py       ← Cost center CRUD
│   │   ├── categorias.py        ← Category CRUD
│   │   ├── subtipos.py          ← Subtype CRUD
│   │   ├── fornecedores.py      ← Supplier CRUD
│   │   ├── localidades.py       ← Location CRUD
│   │   ├── funcoes.py           ← Job function CRUD
│   │   ├── ciclos.py            ← Cycle CRUD
│   │   ├── comentarios.py       ← Comments CRUD
│   │   └── termos.py            ← PDF term generation views
│   ├── templates/front/         ← All HTML templates
│   │   ├── base.html            ← Main layout with CSS token variables
│   │   ├── home.html            ← Home with 3D particle network animation
│   │   ├── dashboards/          ← Dashboard templates
│   │   │   ├── dashboard.html
│   │   │   ├── cc_custos_dashboard.html
│   │   │   ├── custos_diretoria.html   ← Custos por diretoria + drawer de detalhes
│   │   │   ├── preventiva_dashboard.html
│   │   │   ├── licencas_dashboard.html
│   │   │   ├── usuario_dashboard.html
│   │   │   ├── alertas_dashboard.html
│   │   │   └── dashboard_toner.html
│   │   ├── equipamentos/        ← Equipment templates (equipamentos_list.html has 3D canvas + KPI animations)
│   │   ├── plantas/             ← Plant map templates (Canvas 2D, vanilla JS)
│   │   │   ├── planta_list.html    ← Lista de plantas + chips de status PRTG por planta
│   │   │   ├── planta_editor.html  ← Editor drag-and-drop + autosave + histórico de versões
│   │   │   ├── planta_viewer.html  ← Visualizador somente-leitura + drawer de detalhes + toasts
│   │   │   └── planta_tv.html      ← Modo TV fullscreen com refresh automático
│   │   ├── usuarios/            ← Collaborator templates (import, hierarchy, organogram)
│   │   ├── licencas/            ← License templates
│   │   ├── preventivas/         ← Maintenance templates
│   │   ├── movimentacao/        ← Stock movement templates
│   │   ├── inteligencia/        ← Sistema de Inteligência templates
│   │   ├── noticias/            ← News/notifications templates
│   │   └── <other domains>/
│   ├── migrations/              ← 71 migrations applied (last: 0071_corrigir_diretor_marcos_oliveira)
│   └── management/commands/
│       ├── enviar_alertas.py
│       ├── agendar_relatorio.py
│       └── importar_itens_planilha.py
├── services/                    ← Business logic and integrations (outside the app)
│   ├── email_alertas.py         ← All e-mail alert functions
│   ├── movimentacao_service.py  ← Stock movement logic (MovimentacaoEstoqueService)
│   ├── item_create_service.py   ← Item creation logic
│   ├── termos.py                ← PDF term generation
│   ├── importador_planilha.py   ← Excel import logic (items)
│   ├── usuario_import_service.py ← Collaborator hierarchy import from spreadsheet
│   ├── prtg_service.py          ← Proxy seguro para API REST do PRTG (credenciais nunca chegam ao browser)
│   ├── sistema_inteligencia_service.py ← AI/intelligence layer service
│   └── sistema_noticias_service.py     ← News/notifications service
├── about/                       ← Secondary app (about page)
└── users/                       ← Secondary app (auth user extensions)
```

### Key Models (`ProjetoEstoque/models.py`)

All models extend `AuditModel` which adds `criado_por`, `atualizado_por`, `created_at`, `updated_at`.

| Model | Purpose |
|---|---|
| `Item` | Equipment/asset. `item_consumo='sim'` → consumable with stock control. `tem_lote=True` → batch-tracked. `valor` = acquisition cost. `locado='sim'` → has a `Locacao` contract. |
| `LoteEstoque` / `ItemLote` | Batch stock: a `LoteEstoque` holds NF info; `ItemLote` links items to batches with per-batch availability. |
| `MovimentacaoItem` | Stock movement. Types: `entrada`, `baixa`, `transferencia`, `transferencia_equipamento`, `envio_manutencao`, `retorno_manutencao`, `outros`. Field `custo` = operation cost. |
| `Usuario` | Collaborator (not Django User). `status='desligado'` → terminated. Has full 6-level hierarchy (see below). |
| `Licenca` / `MovimentacaoLicenca` / `LicencaLote` | Software license management with allocation tracking. |
| `Preventiva` | Scheduled maintenance instance. Links `Item` + `ChecklistModelo`. Tracks `proxima_calc` (next due date). |
| `ChecklistModelo` / `ChecklistPergunta` | Checklist templates with questions (`tipo_resposta`: texto/numero/booleano/escolha). |
| `ExecucaoPreventiva` / `RespostaChecklist` | Records of completed maintenance runs and answers. |
| `Fornecedor` | Supplier. Fields: `nome`, `cnpj`, `contrato`. No `nome_fantasia`. |
| `CentroCusto` | Cost center with `numero`, `departamento`, `pmb` (PMB flag). |
| `Locacao` | Equipment rental/leasing contract attached to an `Item`. Field `valor_mensal` = monthly fee used in cost dashboards. |
| `PlantaProjeto` | Mapa visual de infraestrutura. Campo `layout` (JSONField) armazena `{elements:[], connections:[]}`. Campos computados: `total_elementos`, `elementos_com_prtg`. |
| `PlantaLayoutHistorico` | Histórico de versões do layout de uma planta (FK → `PlantaProjeto`). |

### Hierarchy Model — `Usuario` fields

The `Usuario` model carries the full 6-level org chart as plain `CharField`s (populated via spreadsheet import):

```
diretor_geral  →  diretor  →  gestor  →  coordenador  →  supervisor  →  responsavel (= colaborador name)
```

- **Diretor Geral único**: MIGUEL PRADO
- The `diretor` field is the primary grouping dimension used in `custos_diretoria_dashboard`.
- When `diretor` is not populated the dashboard falls back to `gestor`.
- All fields are indexed for query performance (see migration `0066`).
- Data is populated via `usuario_import_service.py` from an HR spreadsheet.

**Important**: after reimporting the spreadsheet, migration `0071_corrigir_diretor_marcos_oliveira` must be re-applied (or the import service must correctly resolve MARCOS ANTONIO SOUSA OLIVEIRA ≠ MARCOS ANTONIO DE OLIVEIRA).

### Services Layer

Business logic lives in `controle/services/`, not in views. Always call services from views, not directly from templates or models.

- `MovimentacaoEstoqueService.registrar(form, user)` — handles all stock movement types, updates `ItemLote.quantidade_disponivel`, fires post-commit e-mail alerts via `transaction.on_commit()`.
- `email_alertas.py` — all alert functions return `bool`. Use `relatorio_diario(horas=24)` for consolidated daily digest. Individual: `alerta_estoque_critico()`, `alerta_licencas_desligados()`, `alerta_preventivas_proximas()`.
- `usuario_import_service.py` — imports collaborators with full hierarchy from an `.xlsx` spreadsheet. Uses a 5-strategy fuzzy name resolver (`SequenceMatcher`) to map abbreviated manager names to full collaborator names.
- `prtg_service.py` — proxy seguro para a API REST do PRTG. Ver seção "PRTG Integration" abaixo.
- `sistema_inteligencia_service.py` — powers the Sistema de Inteligência dashboard (global search, KPI aggregation).
- `sistema_noticias_service.py` — news/notifications feed service.

### Views

Each domain has its own file in `ProjetoEstoque/views/`. Every view function must also be exported from `views/__init__.py`, otherwise `urls.py` won't see it.

### URL Pattern Convention

- List: `/<domain>/`
- Create: `/<domain>/novo/`
- Detail: `/<domain>/<pk>/`
- Edit: `/<domain>/<pk>/editar/`
- Delete: `/<domain>/<pk>/excluir/`
- Export: `/<domain>/pdf/` or `/<domain>/exportar-excel/`
- AJAX/JSON endpoints: `/<domain>/<action>/` returning `JsonResponse`

### Key Dashboard URLs

| Name | URL | View |
|---|---|---|
| Home | `/` | `home` |
| Custos por Setor | `/dashboards/custos-cc/` | `cc_custos_dashboard` |
| Custos por Diretoria | `/dashboards/custos-diretoria/` | `custos_diretoria_dashboard` |
| Custos por Diretoria — Detalhe (AJAX) | `/dashboards/custos-diretoria/detalhe/` | `custos_diretoria_detalhe` |
| Preventivas Dashboard | `/dashboards/preventivas/` | `preventiva_dashboard` |
| Licenças Dashboard | `/dashboards/licencas/` | `licencas_dashboard` |
| Sistema de Inteligência | `/inteligencia/` | `sistema_inteligencia_dashboard` |
| Organograma | `/usuarios/organograma/` | `organograma_usuarios` |
| Hierarquia | `/usuarios/hierarquia/` | `hierarquia_usuarios` |
| Plantas — Lista | `/plantas/` | `planta_list` |
| Plantas — Editor | `/plantas/<pk>/editar/` | `planta_editor` |
| Plantas — Visualizador | `/plantas/<pk>/` | `planta_viewer` |
| Plantas — TV | `/plantas/<pk>/tv/` | `planta_tv` |
| PRTG Status API | `/plantas/prtg/status/` | `prtg_status_api` |
| PRTG Search API | `/plantas/prtg/buscar/` | `prtg_search_api` |

## PRTG Integration

### Arquitetura de segurança

Credenciais PRTG (`PRTG_URL`, `PRTG_USER`, `PRTG_PASSHASH`) ficam exclusivamente em `.env` e são lidas por `prtg_service.py`. **Nunca expor ao browser.** O cliente recebe apenas o JSON filtrado da API `/plantas/prtg/status/` (`@login_required`).

### Dois níveis de status

O PRTG distingue o status do *device* (agregado de todos os sensores) do status individual de cada *sensor*. Isso é crítico:

- Se o sensor de ping **não** for o sensor-raiz de dependência do device no PRTG, o device pode aparecer como "Up" enquanto o ping sensor está "Down".
- `prtg_service.get_devices_map()` resolve isso: busca devices (`content=devices`) **e** sensores de ping/ICMP (`content=sensors`, filtrado por tipo/nome), e usa o **pior** dos dois como `status` efetivo.

### Campos retornados por `get_devices_map()`

```python
{
  device_objid: {
    "objid":         int,    # ID do device no PRTG
    "name":          str,    # nome do device
    "host":          str,    # IP/hostname
    "group":         str,    # grupo PRTG
    "status":        int,    # STATUS EFETIVO (pior de device_status e ping_status)
    "device_status": int,    # status de device-level bruto
    "ping_status":   int|None, # status do sensor de ping (None = sem sensor de ping)
    "status_slug":   str,    # "up" | "warning" | "down" | "unknown" | ...
    "statustext":    str,    # texto descritivo do PRTG (ex: "Down (Ping)")
    "css_color":     str,    # var CSS para uso direto em style=""
  }
}
```

### Códigos de status PRTG

| Código | Slug | Significado |
|---|---|---|
| 3 | `up` | Online |
| 4 | `warning` | Instável |
| 5 | `down` | Offline |
| 10 | `unusual` | Incomum (tratado como warning) |
| 1 | `unknown` | Desconhecido |
| 2 | `collecting` | Coletando dados |
| 7–9, 12 | `paused_*` | Pausado |

### Cache

Devices e ping sensors são cacheados separadamente por 30s (`prtg_devices_v2` e `prtg_ping_sensors_v2`). Ao mudar chave de cache, **sempre** incrementar o sufixo de versão para bustar entradas antigas.

### `_status_int()` — parsing robusto

Prioridade: `status_raw` (int/float) → `int(float(status))` → mapeamento de texto (`"Down (Ping)"` → `"down"` → `5`). O split em `"("` garante parsing de textos compostos como `"Down (Ping test failed)"`.

### Viewer — refresh e detecção de mudanças

- `refreshStatus()` chama `/plantas/prtg/status/` a cada 30s e redesenha o canvas.
- `detectarMudancas(prev, newMap)` dispara toasts somente após a **segunda** carga (flag `_firstLoad`) para evitar alertas falsos na abertura da página.
- O campo `dev.ping_status` é exibido separadamente no drawer do elemento quando disponível.

## Preventivas — Lógica de Saúde

### Cálculo de `proxima_calc` (`views/equipamentos.py`)

Prioridade do intervalo:
1. `item.data_limite_preventiva` (campo inteiro em dias no próprio equipamento)
2. `preventiva.checklist_modelo.intervalo_dias` (intervalo do modelo de checklist)

```python
_JANELA_ATENCAO = 7  # dias antes do vencimento para alertar "atenção"
```

Estados de `status_saude`:
- `"ok"` — todas as preventivas em dia
- `"atencao"` — existe preventiva com `0 <= dias_restantes <= 7`
- `"critical"` — existe preventiva vencida (`dias_restantes < 0`)
- `"sem_data"` — sem data calculada para nenhuma preventiva

Os atributos `p.atrasado` e `p.atencao` são calculados na view e usados no template.

## Dashboard de Preventivas — Séries Temporais

### `TruncMonth` em `DateField` retorna `date`, não `datetime`

`dashboards.py` usa duas funções para alinhar séries:
- `_align_series(stamps, qs)` — para campos `DateTimeField` (usa `timezone.localtime`)
- `_align_series_date(stamps, qs)` — para campos `DateField` como `data_ultima` e `data_proxima` (usa `isinstance(mdt, date)`)

**Nunca** usar `isinstance(mdt, datetime)` como único teste para datas vindas de `TruncMonth` em `DateField` — retorna `date`, que é instância de `date` mas NÃO de `datetime`. O teste correto é `isinstance(mdt, datetime)` (datetime) e `elif isinstance(mdt, date)` (date puro).

O contexto do dashboard de preventivas passa `dt_ini` e `dt_fim` como objetos `date` (não `datetime`) para os inputs do formulário no template.

## CSS / Templates Design System

`base.html` defines a set of CSS custom properties (tokens) used across all templates. **Never override these in `:root` inside a template** — use them as-is.

Key tokens: `--brand`, `--brand-hover`, `--brand-soft`, `--text-primary`, `--text-tertiary`, `--bg-elevated`, `--separator-hairline`, `--radius-sm/md/lg/xl`, `--shadow-sm/md`.

### Template Pattern (mandatory for new pages)

```html
{% extends 'base.html' %}
{% block title %}Page Title{% endblock %}
{% block page_title %}Page Title{% endblock %}
{% block page_subtitle %}Optional subtitle{% endblock %}

{% block header_actions %}
  {# Buttons for the page header bar #}
{% endblock %}

{% block extra_css %}
<style>
/* Scope ALL styles with :where(.prefix-page) — never use :root */
:where(.prefix-page) { --local-var: value; }
.prefix-page { ... }
</style>
{% endblock %}

{% block content %}
<div class="prefix-page"> ... </div>
{% endblock %}

{% block extra_js %}<script>...</script>{% endblock %}
```

### CSS Class Prefix Convention

| Domain | Prefix |
|---|---|
| Equipamentos | `.eq-*` |
| Preventivas | `.pv-*` |
| Checklists / Perguntas | `.cl-*` |
| Centros de Custo | `.cc-*` |
| Localidades | `.loc-*` |
| Custos Diretoria | `.cdir-*` |
| Inteligência | `.si-*` |
| Usuários / Organograma | `.org-*` / `.hier-*` |
| Plantas (lista) | `.plt-*` |
| Plantas (viewer) | `.vw-*` |
| Plantas (editor) | `.pe-*` |

### 3D Animations

Two pages have a lightweight 3D Canvas animation (pure vanilla JS, no Three.js):

- **`home.html`**: dark navy hero section with a 3D particle network (38 nodes, sphere distribution, auto Y-rotation + mouse parallax). Card tilt + glint effect on KPI cards via CSS custom properties.
- **`equipamentos_list.html`**: light theme hero section with 26-node particle network in brand blue. KPI card tilt via event delegation on `#kpisWrap` (AJAX-safe — uses CSS custom properties, no DOM injection). Counter animation via `IntersectionObserver` (integers only — skips formatted values like "R$ 1.200,00").

Animation rules:
- Canvas uses `position: absolute; inset: 0` inside a `position: relative; overflow: hidden` hero container.
- `visibilitychange` event pauses canvas when tab is hidden.
- `ResizeObserver` handles canvas resizing.
- Counter animation only runs on elements whose text matches `/^\d+$/` to avoid corrupting formatted strings.

### Plant Map Canvas (Editor / Viewer / TV)

Os três templates (`planta_editor.html`, `planta_viewer.html`, `planta_tv.html`) compartilham a mesma arquitetura de Canvas 2D vanilla JS:

- **Layout JSON**: `{{ layout_json|json_script:"__layout_data" }}` + `JSON.parse(document.getElementById('__layout_data').textContent)`. **Nunca usar `|safe`** — XSS via label de elemento.
- **Elementos**: `{id, type, x, y, width, height, label, color, prtg_objid, item_id, ...}`. Tipos: `switch`, `router`, `ap`, `server`, `firewall`, `printer`, `storage`, `camera`, `forma`, `texto`.
- **Conexões**: `{id, from, to, type, color, strokeWidth, dash, arrow}`. Propriedades por conexão sobrepõem o `CN_CFG` global.
- **Formas**: propriedades de borda por elemento: `el.borderColor`, `el.borderWidth`, `el.borderStyle`.
- **Status PRTG no canvas**: `elStKey(el)` → `ST_MAP[parseInt(dev.status)]`. `dev.status` = status efetivo (pior de device + ping sensor) retornado pelo servidor.
- **`ST_MAP`** deve cobrir todos os 12 códigos PRTG (1–12). Código 10 (unusual) → `"warning"`.

### Custos por Diretoria Dashboard — Detail Drawer

`custos_diretoria.html` has a responsive detail drawer triggered by clicking any table row:

- **Desktop**: slides in from the right (`translateX`).
- **Mobile (≤559px)**: rises as a bottom sheet (`translateY`), full width, rounded top corners, `max-height: 88dvh`, safe-area insets.
- **Touch swipe to dismiss**: swipe down (mobile) or right (desktop). Axis is detected on first significant move. Respects inner scroll position.
- **Body scroll lock** preserves page position (`position: fixed` + `window.scrollY`).
- **AJAX endpoint**: `GET /dashboards/custos-diretoria/detalhe/?grupo=<nome>&campo=<diretor|gestor>` → `JsonResponse` with licenças, movimentações, itens (locação), and totals.
- **Resumo inteligente**: built in JS from the JSON response — natural language summary of the directorship's cost composition.

**Cost composition in this dashboard:**
1. `custo_licencas` — monthly software license cost (active `ATRIBUICAO` movements).
2. `custo_movimentacoes` — `MovimentacaoItem.custo` for movements linked to users in the directorship.
3. `custo_itens` — `Locacao.valor_mensal` (monthly rental fee) for items in cost centers associated with the directorship. **This is NOT `Item.valor` (acquisition cost).**

FontAwesome icons are loaded by `base.html`. Select2 is loaded globally and should be initialized in `{% block extra_js %}` when a form uses dropdowns. Chart.js 4.4.3 is loaded from CDN in dashboards that need charts.

## Architecture Rules

1. **All UI strings in Portuguese (pt-BR).** No English labels in templates.
2. **Forms never perform business logic.** Validation lives in `model.clean()` or services. Views call `form.save()` only for simple CRUD; complex operations go through a service.
3. **E-mail alerts are fire-and-forget via `transaction.on_commit()`.** Never block the request on SMTP. Email functions in `email_alertas.py` import models lazily (inside the function) to avoid circular imports.
4. **`Fornecedor.nome` is the supplier name field** — there is no `nome_fantasia`.
5. **New views must be added to `views/__init__.py`** before they can be referenced in `urls.py`.
6. **Template CSS must not leak.** Use `:where(.prefix-page)` scoping, not bare selectors or `:root` overrides.
7. **SQLite in dev; migrations must be backwards-safe** (no dropping columns without a transition period). Currently at migration `0071`.
8. **Windows Task Scheduler (`schtasks`) is the scheduling mechanism** — no Celery, no Django-Q, no cron (Linux). Use the `agendar_relatorio` management command to register tasks.
9. **AJAX views return `JsonResponse`** and must still be decorated with `@login_required`. Add them to `views/__init__.py` and `urls.py` following the same pattern as `custos_diretoria_detalhe`.
10. **Hierarchy data comes from spreadsheet import** — never set `diretor`, `gestor`, etc. manually in the UI. Use `usuario_import_service.py` to reimport from the HR spreadsheet.
11. **`custo_itens` in dashboards = `Locacao.valor_mensal`** (monthly rental fee), not `Item.valor` (acquisition cost). Do not confuse the two.
12. **PRTG credentials never reach the browser.** Toda chamada ao PRTG passa por `prtg_service.py` no servidor. O frontend acessa apenas `/plantas/prtg/status/` (`@login_required`).
13. **Status efetivo PRTG = pior entre device-level e ping sensor.** Nunca assumir que o status de device-level do PRTG reflete corretamente o ping. Usar `prtg_service.get_devices_map()` que já faz a resolução.
14. **`layout_json` no contexto de plantas deve ser o dict Python** (não `json.dumps()`). O filtro `{{ layout_json|json_script:"id" }}` serializa corretamente; `|safe` é XSS e está proibido.
