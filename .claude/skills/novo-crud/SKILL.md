---
name: novo-crud
description: Cria um novo domínio de cadastro CRUD simples (model/form/view/urls/templates) seguindo exatamente o padrão já usado em Subtipo/CentroCusto/Localidade/Fornecedor. Usar quando o pedido for "criar cadastro de X", "adicionar tela de Y", "novo CRUD de Z".
---

# Novo domínio CRUD

Referência canônica no código: `controle/ProjetoEstoque/views/subtipos.py` +
`controle/ProjetoEstoque/templates/front/subtipo/*.html`. Copie a estrutura de lá,
não reinvente.

## Passo a passo

1. **Model** (se ainda não existir) em `ProjetoEstoque/models.py`. Estender
   `AuditModel` (dá `criado_por`, `atualizado_por`, `created_at`, `updated_at`)
   a menos que exista razão explícita para não auditar.

2. **Migration**: `python manage.py makemigrations && python manage.py migrate`
   (rodar de dentro de `controle/`). Ver skill `migracao-deploy` antes de aplicar
   em produção.

3. **Form** em `ProjetoEstoque/forms.py` — `ModelForm` simples, widgets com
   `attrs={"class": "ctrl", ...}` (ou `BASE_CTRL_CSS | {...}` se o form usa essa
   constante). Validação cross-field vai em `form.clean()`, nunca na view.

4. **View file** `ProjetoEstoque/views/<dominio>.py` com as funções no padrão:
   `<dominio>_list`, `<dominio>_create`, `<dominio>_update`, `<dominio>_delete`,
   `<dominio>_detail` (se fizer sentido ter detalhe). Todas com `@login_required`.
   No delete, verificar vínculos antes de apagar e bloquear com mensagem clara
   (ver `subtipo_delete` — conta `itens_count`/`checklists_count` e impede se > 0
   em vez de deixar o `ProtectedError` estourar). Regra de negócio complexa (não
   um simples `form.save()`) vai em `services/`, nunca na view.

5. **Registrar em `views/__init__.py`** — sem isso o `urls.py` não enxerga as
   funções. Adicionar um bloco `# ── <Domínio> ──` seguindo o padrão dos
   existentes, importando todas as views do novo arquivo.

6. **URLs em `ProjetoEstoque/urls.py`** seguindo a convenção estrita do projeto:
   ```
   path("<dominio>/",                views.<dominio>_list,   name="<dominio>_list"),
   path("<dominio>/novo/",           views.<dominio>_create, name="<dominio>_create"),
   path("<dominio>/<int:pk>/",       views.<dominio>_detail, name="<dominio>_detail"),   # opcional
   path("<dominio>/<int:pk>/editar/",views.<dominio>_update, name="<dominio>_update"),
   path("<dominio>/<int:pk>/excluir/",views.<dominio>_delete, name="<dominio>_delete"),
   ```

7. **Templates** em `templates/front/<dominio_singular>/` (singular, ex.:
   `subtipo/`, não `subtipos/`): `<dominio>_list.html`, `<dominio>_form.html`,
   `<dominio>_confirm_delete.html`, `<dominio>_detail.html` (se houver).
   - `{% extends 'base.html' %}` + blocks `title`/`page_title`/`page_subtitle`.
   - `{% block header_actions %}` **só** para o botão primário "Novo X" que
     linka em `<dominio>_create` — é o padrão estabelecido (ver
     `subtipo_list.html`). Não adicionar outros botões ali por conta própria
     (Exportar, etc. vão no corpo da página); e nunca tocar na navbar lateral de
     `base.html` sem pedido explícito do usuário nessa mensagem.
   - CSS: `{% block extra_css %}` com `:where(.<prefixo>-page) { --var: var(--token, fallback); }`
     — nunca `:root`. Escolher um prefixo curto de 2-4 letras não usado ainda
     (ver tabela de prefixos no CLAUDE.md).

8. **Toda string de UI em pt-BR.** Mensagens de `messages.success/error` seguem
   o tom já usado ("Subtipo cadastrado com sucesso!").

## Checklist final
- [ ] Migration aplicada em dev
- [ ] View exportada em `views/__init__.py`
- [ ] URLs seguem `/dominio/`, `/dominio/novo/`, `/dominio/<pk>/editar/`, `/dominio/<pk>/excluir/`
- [ ] Templates com CSS escopado por `:where(.prefixo-page)`
- [ ] Nenhum item novo na navbar lateral sem pedido explícito
- [ ] Se o domínio precisa aparecer em relatórios/exports, avaliar se entra em `relatorios.py`
