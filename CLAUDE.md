# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Sistema de Controle de TI para Santa Colomba Agropecuária. Gerencia equipamentos (itens), licenças de software, colaboradores (usuários), movimentações de estoque, preventivas (manutenção), e emite alertas por e-mail. Interface 100% em português (pt-BR).

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
```

## Architecture

### Directory Layout

```
controle/               ← Django project root (manage.py lives here)
├── controle/           ← Project settings package (settings.py, urls.py)
├── ProjetoEstoque/     ← Main Django app
│   ├── models.py       ← All models in one file
│   ├── forms.py        ← All forms in one file
│   ├── admin.py        ← Django admin registration
│   ├── urls.py         ← All URL patterns
│   ├── views/          ← Views split by domain (one file per domain)
│   │   ├── __init__.py ← Re-exports all views (add new views here too)
│   │   ├── equipamentos.py, usuarios.py, licencas.py, preventivas.py ...
│   ├── templates/front/    ← All HTML templates
│   │   ├── base.html       ← Main layout with CSS token variables
│   │   └── <domain>/       ← e.g. equipamentos/, preventivas/, licencas/
│   └── management/commands/    ← Custom management commands
├── services/           ← Business logic and integrations (outside the app)
│   ├── email_alertas.py        ← All e-mail alert functions
│   ├── movimentacao_service.py ← Stock movement logic (MovimentacaoEstoqueService)
│   ├── item_create_service.py  ← Item creation logic
│   ├── termos.py               ← PDF term generation
│   └── importador_planilha.py  ← Excel import logic
├── about/              ← Secondary app (about page)
└── users/              ← Secondary app (auth user extensions)
```

### Key Models (`ProjetoEstoque/models.py`)

All models extend `AuditModel` which adds `criado_por`, `atualizado_por`, `created_at`, `updated_at`.

| Model | Purpose |
|---|---|
| `Item` | Equipment/asset. `item_consumo='sim'` → consumable with stock control. `tem_lote=True` → batch-tracked. |
| `LoteEstoque` / `ItemLote` | Batch stock: a `LoteEstoque` holds NF info; `ItemLote` links items to batches with per-batch availability. |
| `MovimentacaoItem` | Stock movement. Types: `entrada`, `baixa`, `transferencia`, `transferencia_equipamento`, `envio_manutencao`, `retorno_manutencao`, `outros`. |
| `Usuario` | Collaborator (not Django User). `status='desligado'` → terminated. Has `centro_custo`, `localidade`, `funcao`. |
| `Licenca` / `MovimentacaoLicenca` / `LicencaLote` | Software license management with allocation tracking. |
| `Preventiva` | Scheduled maintenance instance. Links `Item` + `ChecklistModelo`. Tracks `proxima_calc` (next due date). |
| `ChecklistModelo` / `ChecklistPergunta` | Checklist templates with questions (`tipo_resposta`: texto/numero/booleano/escolha). |
| `ExecucaoPreventiva` / `RespostaChecklist` | Records of completed maintenance runs and answers. |
| `Fornecedor` | Supplier. Fields: `nome`, `cnpj`, `contrato`. No `nome_fantasia`. |
| `CentroCusto` | Cost center with `numero`, `departamento`, `pmb` (PMB flag). |
| `Locacao` | Equipment rental/leasing contract attached to an `Item`. |

### Services Layer

Business logic lives in `controle/services/`, not in views. Always call services from views, not directly from templates or models.

- `MovimentacaoEstoqueService.registrar(form=form, user=request.user)` — handles all stock movement types, updates `ItemLote.quantidade_disponivel`, and fires post-commit e-mail alerts via `transaction.on_commit()`.
- `email_alertas.py` — all alert functions return `bool`. Use `relatorio_diario(horas=24)` for the consolidated daily digest. Individual alerts: `alerta_estoque_critico()`, `alerta_licencas_desligados()`, `alerta_preventivas_proximas()`.

### Views

Each domain has its own file in `ProjetoEstoque/views/`. Every view function must also be exported from `views/__init__.py`, otherwise `urls.py` won't see it.

### URL Pattern Convention

- List: `/<domain>/`
- Create: `/<domain>/novo/`
- Detail: `/<domain>/<pk>/`
- Edit: `/<domain>/<pk>/editar/`
- Delete: `/<domain>/<pk>/excluir/`
- Export: `/<domain>/pdf/` or `/<domain>/exportar-excel/`

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

CSS class prefix convention by domain:
- Equipamentos: `.eq-*`
- Preventivas list/detail/start/exec: `.pv-*`
- Checklists/perguntas: `.cl-*`
- Centros de custo / localidades: `.cc-*` / `.loc-*`

FontAwesome icons are loaded by `base.html`. Select2 is loaded globally and should be initialized in `{% block extra_js %}` when a form uses dropdowns.

## Architecture Rules

1. **All UI strings in Portuguese (pt-BR).** No English labels in templates.
2. **Forms never perform business logic.** Validation lives in `model.clean()` or services. Views call `form.save()` only for simple CRUD; complex operations go through a service.
3. **E-mail alerts are fire-and-forget via `transaction.on_commit()`.** Never block the request on SMTP. Email functions in `email_alertas.py` import models lazily (inside the function) to avoid circular imports.
4. **`Fornecedor.nome` is the supplier name field** — there is no `nome_fantasia`.
5. **New views must be added to `views/__init__.py`** before they can be referenced in `urls.py`.
6. **Template CSS must not leak.** Use `:where(.prefix-page)` scoping, not bare selectors or `:root` overrides.
7. **SQLite in dev; migrations must be backwards-safe** (no dropping columns without a transition period).
8. **Windows Task Scheduler (`schtasks`) is the scheduling mechanism** — no Celery, no Django-Q, no cron (Linux). Use the `agendar_relatorio` management command to register tasks.
